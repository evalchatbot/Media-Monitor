"""Groq Whisper transcription with word/segment timestamps."""
from __future__ import annotations

import bisect
import logging
from pathlib import Path

from config import settings

logger = logging.getLogger(__name__)

# Groq free-tier file limit is 25MB; developer tier 100MB. Chunk above this.
_MAX_BYTES = 20 * 1024 * 1024
_CHUNK_SEC = 10 * 60
_OVERLAP_SEC = 5


def transcribe_audio(
    audio_path: Path,
    *,
    language: str | None = "ur",
    prompt: str = "",
    use_fallback: bool = False,
) -> tuple[str, list[dict], dict]:
    """Return (full_text, segments, meta).

    segments: [{"start": float, "end": float|None, "text": str}, ...]
    Prefer word-level timing when Groq provides it.
    """
    if not settings.groq_api_key:
        logger.warning("GROQ_API_KEY not set — cannot transcribe")
        return "", [], {"error": "no_key"}

    model = (
        settings.groq_whisper_fallback_model
        if use_fallback
        else settings.groq_whisper_model
    )
    size = audio_path.stat().st_size if audio_path.exists() else 0
    if size > _MAX_BYTES:
        return _transcribe_chunked(audio_path, language=language, prompt=prompt, model=model)

    text, segs, meta = _transcribe_one(audio_path, language=language, prompt=prompt, model=model)
    meta["model"] = model
    # Low-confidence Urdu retry with Large V3 once.
    if (
        not use_fallback
        and language in (None, "ur", "hi")
        and _looks_low_confidence(meta)
        and settings.groq_whisper_fallback_model
        and settings.groq_whisper_fallback_model != model
    ):
        logger.info("Low confidence transcript — retrying with %s", settings.groq_whisper_fallback_model)
        return transcribe_audio(
            audio_path, language=language, prompt=prompt, use_fallback=True,
        )
    return text, segs, meta


def estimate_cost_usd(duration_seconds: float, *, model: str | None = None) -> float:
    """Rough Groq ASR cost. Minimum billable length is 10 seconds."""
    hours = max(duration_seconds, 10) / 3600.0
    mid = (model or settings.groq_whisper_model or "").lower()
    rate = 0.111 if "large-v3" in mid and "turbo" not in mid else 0.04
    return round(hours * rate, 6)


def _transcribe_one(
    audio_path: Path,
    *,
    language: str | None,
    prompt: str,
    model: str,
    time_offset: float = 0.0,
) -> tuple[str, list[dict], dict]:
    from groq import Groq

    client = Groq(api_key=settings.groq_api_key)
    kwargs = {
        "model": model,
        "response_format": "verbose_json",
        "timestamp_granularities": ["word", "segment"],
        "temperature": 0.0,
    }
    if language:
        # Whisper treats Hindi/Urdu similarly; force ur for our watchlist.
        kwargs["language"] = "ur" if language in ("ur", "hi") else language
    if prompt:
        kwargs["prompt"] = prompt[:800]

    with open(audio_path, "rb") as fh:
        resp = client.audio.transcriptions.create(file=fh, **kwargs)

    text = getattr(resp, "text", "") or ""
    words = getattr(resp, "words", None) or []
    segments_raw = getattr(resp, "segments", None) or []
    quality = _quality_spans(segments_raw)
    segments: list[dict] = []
    if words:
        for w in words:
            start = float(_attr(w, "start", 0) or 0)
            segments.append({
                "start": start + time_offset,
                "end": _maybe_float(_attr(w, "end", None), time_offset),
                "text": (_attr(w, "word", None) or _attr(w, "text", "") or "").strip(),
                # Word timings carry no quality of their own — inherit the
                # enclosing segment's so hallucinated speech stays detectable.
                **_quality_at(quality, start),
            })
    else:
        for s in segments_raw:
            segments.append({
                "start": float(_attr(s, "start", 0) or 0) + time_offset,
                "end": _maybe_float(_attr(s, "end", None), time_offset),
                "text": (_attr(s, "text", "") or "").strip(),
                **_quality_of(s),
            })

    conf = _confidence_from_segments(segments_raw)
    meta = {
        "model": model,
        "language": getattr(resp, "language", language),
        "duration": getattr(resp, "duration", None),
        "confidence": conf,
        "chunk_offset": time_offset,
    }
    return text.strip(), [s for s in segments if s.get("text")], meta


def _transcribe_chunked(
    audio_path: Path,
    *,
    language: str | None,
    prompt: str,
    model: str,
) -> tuple[str, list[dict], dict]:
    """Split long audio with overlap and reconcile timestamps."""
    import shutil
    import subprocess
    import tempfile

    if shutil.which("ffmpeg") is None:
        return _transcribe_one(audio_path, language=language, prompt=prompt, model=model)

    duration = _probe_duration(audio_path) or 0
    if duration <= 0:
        return _transcribe_one(audio_path, language=language, prompt=prompt, model=model)

    texts: list[str] = []
    segments: list[dict] = []
    metas: list[dict] = []
    start = 0.0
    tmp = Path(tempfile.mkdtemp(prefix="yt-chunk-"))
    try:
        while start < duration:
            chunk = tmp / f"chunk_{int(start)}.flac"
            length = min(_CHUNK_SEC + _OVERLAP_SEC, max(duration - start, 1))
            cmd = [
                "ffmpeg", "-y", "-nostdin", "-loglevel", "error",
                "-ss", str(start), "-t", str(length),
                "-i", str(audio_path),
                "-ar", "16000", "-ac", "1", "-c:a", "flac", str(chunk),
            ]
            subprocess.run(cmd, capture_output=True, timeout=180)
            if not chunk.exists():
                break
            t, segs, meta = _transcribe_one(
                chunk, language=language, prompt=prompt, model=model, time_offset=start,
            )
            texts.append(t)
            segments.extend(_dedupe_overlap(segments, segs, start))
            metas.append(meta)
            start += _CHUNK_SEC
            try:
                chunk.unlink(missing_ok=True)
            except Exception:
                pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    avg_conf = {}
    if metas:
        vals = [m.get("confidence", {}).get("avg_logprob") for m in metas
                if isinstance(m.get("confidence"), dict)]
        vals = [v for v in vals if isinstance(v, (int, float))]
        if vals:
            avg_conf = {"avg_logprob": sum(vals) / len(vals)}
    return " ".join(texts).strip(), segments, {
        "model": model, "confidence": avg_conf, "chunked": True, "chunks": len(metas),
    }


def _dedupe_overlap(existing: list[dict], new_segs: list[dict], chunk_start: float) -> list[dict]:
    """Drop overlapping words from the overlap region of a new chunk.

    Cut at where the previous chunk actually stopped, not at a fixed fraction of
    the overlap — a fixed cutoff re-transcribes the tail of the previous chunk
    and duplicates that speech in the token stream.
    """
    if not existing:
        return new_segs
    tail = existing[-1]
    last_end = float(tail.get("end") or tail.get("start") or 0)
    cutoff = max(chunk_start, last_end)
    return [s for s in new_segs if float(s.get("start") or 0) >= cutoff]


def _probe_duration(path: Path) -> float | None:
    import shutil
    import subprocess

    if shutil.which("ffprobe") is None:
        return None
    try:
        out = subprocess.check_output(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            timeout=30,
        )
        return float(out.decode().strip())
    except Exception:
        return None


def _quality_of(seg) -> dict:
    """Whisper's own reliability signals for one segment."""
    out = {}
    lp = _attr(seg, "avg_logprob", None)
    ns = _attr(seg, "no_speech_prob", None)
    if isinstance(lp, (int, float)):
        out["avg_logprob"] = float(lp)
    if isinstance(ns, (int, float)):
        out["no_speech_prob"] = float(ns)
    return out


def _quality_spans(segments_raw) -> list[tuple[float, float, dict]]:
    spans: list[tuple[float, float, dict]] = []
    for s in segments_raw or []:
        q = _quality_of(s)
        if not q:
            continue
        start = float(_attr(s, "start", 0) or 0)
        end = float(_attr(s, "end", start) or start)
        spans.append((start, end, q))
    return spans


def _quality_at(spans: list[tuple[float, float, dict]], t: float) -> dict:
    """Quality of the segment containing `t`. Spans are time-ordered."""
    if not spans:
        return {}
    i = bisect.bisect_right(spans, (t, float("inf"), {})) - 1
    if i < 0:
        return {}
    start, end, q = spans[i]
    return q if start <= t <= end else {}


def _attr(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _maybe_float(val, offset: float = 0.0) -> float | None:
    if val is None:
        return None
    try:
        return float(val) + offset
    except (TypeError, ValueError):
        return None


def _confidence_from_segments(segments_raw) -> dict:
    probs = []
    for s in segments_raw or []:
        p = _attr(s, "avg_logprob", None)
        if isinstance(p, (int, float)):
            probs.append(float(p))
    if not probs:
        return {}
    return {"avg_logprob": sum(probs) / len(probs), "n": len(probs)}


def _looks_low_confidence(meta: dict) -> bool:
    conf = meta.get("confidence") or {}
    avg = conf.get("avg_logprob")
    if avg is None:
        return False
    return avg < -0.45
