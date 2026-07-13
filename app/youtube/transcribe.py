"""Transcription for YouTube videos — pick the backend with a config flag.

YOUTUBE_TRANSCRIBER:
  stub    (default) — no transcription; keyword matching runs on the video's
                      title + description only. Fully runnable today, no GPU/key.
  openai            — Whisper via the OpenAI API (needs OPENAI_API_KEY + ffmpeg).
                      Cheap, no GPU, ~$0.006/min. Good default once you add a key.
  local             — Whisper large-v3 via faster-whisper on a local GPU
                      (needs faster-whisper + CUDA + ffmpeg). Zero per-minute
                      cost, best for high volume, needs the GPU you mentioned.

All paths return (full_text, segments) where segments = [{"start": sec, "text"}],
so keyword deep-link timestamps work identically regardless of backend.

Audio is downloaded with yt-dlp (needs ffmpeg on PATH). If download or the
chosen backend is unavailable, we log and fall back to an empty transcript so a
scan never crashes.
"""
from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path

from rapidfuzz.distance import Levenshtein

from app.core.keywords import normalize
from config import settings

logger = logging.getLogger(__name__)

# Word tokenizer (unicode-aware) — matches app.core.keywords._tokenize so the
# deep-link locator sees the same tokens the keyword matcher did.
_WORD_RE = re.compile(r"\w+", re.UNICODE)


def transcribe(video_url: str) -> tuple[str, list[dict]]:
    backend = settings.youtube_transcriber
    if backend == "stub":
        return "", []
    if backend == "openai":
        return _with_audio(video_url, _transcribe_openai)
    if backend == "local":
        return _with_audio(video_url, _transcribe_local)
    logger.warning("Unknown YOUTUBE_TRANSCRIBER=%s; using stub", backend)
    return "", []


def transcribe_file(audio) -> tuple[str, list[dict]]:
    """Transcribe an already-downloaded audio file (used by the live path)."""
    backend = settings.youtube_transcriber
    try:
        if backend == "openai":
            return _transcribe_openai(audio)
        if backend == "local":
            return _transcribe_local(audio)
    except Exception as exc:  # pragma: no cover
        logger.warning("transcribe_file failed: %s", exc)
    return "", []


def find_keyword_second(
    segments: list[dict], keyword: str, language: str = "en", max_distance: int = 2
) -> int | None:
    """Return the start second of the earliest mention of `keyword`.

    Matching mirrors ``app.core.keywords.find_matches`` so any keyword the
    matcher accepted can also be *located* here:

    * both keyword and transcript are normalized per language (Urdu code-point
      unification + harakat strip; English lowercase),
    * compared by whole-word EQUALITY first — so "war" no longer latches onto
      "award" (which would give a wrong second, wrong frame, wrong deep-link),
    * then a Levenshtein<=2 fuzzy pass so a mistranscribed "Bhuto" still locates
      the keyword "Bhutto" (the metadata matcher's fuzzy hits get a timestamp).

    `segments` may be word-level ([{start, text=<word>}, ...]) for word-level
    precision, or segment-level. Punctuation-only tokens are dropped so they
    can't break multi-word adjacency. Returns the FIRST word's start second.
    """
    if not segments:
        return None
    norm_kw = normalize(keyword, language)
    kw_tokens = norm_kw.split()
    if not kw_tokens:
        return None
    n = len(kw_tokens)

    # Normalized (token, start) stream — same normalization the matcher used.
    stream: list[tuple[str, float]] = []
    for seg in segments:
        start = float(seg.get("start", 0) or 0)
        for w in _WORD_RE.findall(normalize(seg.get("text") or "", language)):
            stream.append((w, start))

    # 1) Exact whole-word match (word boundaries, not substring).
    for i in range(len(stream) - n + 1):
        if all(stream[i + j][0] == kw_tokens[j] for j in range(n)):
            return int(stream[i][1])

    # 2) Fuzzy fallback mirroring find_matches (guarded so a 2-edit budget can't
    #    over-match very short keywords onto the first unrelated word).
    if len(norm_kw) >= 4:
        for i in range(len(stream) - n + 1):
            window = " ".join(stream[i + j][0] for j in range(n))
            if abs(len(window) - len(norm_kw)) > max_distance:
                continue
            if Levenshtein.distance(window, norm_kw, score_cutoff=max_distance) <= max_distance:
                return int(stream[i][1])

    # 3) Last resort: the whole (normalized) keyword within one segment's text.
    for seg in segments:
        if norm_kw in normalize(seg.get("text") or "", language):
            return int(float(seg.get("start", 0) or 0))
    return None


# -- audio download ------------------------------------------------------
def _download_audio(video_url: str, dest_dir: Path) -> Path | None:
    try:
        import yt_dlp
    except ImportError:
        logger.warning("yt-dlp not installed; cannot download audio")
        return None

    out = dest_dir / "audio.%(ext)s"
    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(out),
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}],
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([video_url])
    except Exception as exc:
        logger.warning("audio download failed for %s: %s (ffmpeg installed?)", video_url, exc)
        return None
    mp3 = dest_dir / "audio.mp3"
    return mp3 if mp3.exists() else None


def _with_audio(video_url: str, fn) -> tuple[str, list[dict]]:
    with tempfile.TemporaryDirectory() as tmp:
        audio = _download_audio(video_url, Path(tmp))
        if not audio:
            return "", []
        try:
            return fn(audio)
        except Exception as exc:
            logger.exception("transcription failed for %s: %s", video_url, exc)
            return "", []


# -- backends ------------------------------------------------------------
def _transcribe_openai(audio: Path) -> tuple[str, list[dict]]:
    from openai import OpenAI

    client = OpenAI(api_key=settings.openai_api_key)
    with open(audio, "rb") as fh:
        resp = client.audio.transcriptions.create(
            model="whisper-1",
            file=fh,
            response_format="verbose_json",
            timestamp_granularities=["word", "segment"],
        )
    text = getattr(resp, "text", "") or ""
    # Prefer word-level timing; fall back to segment-level.
    words = getattr(resp, "words", None) or []
    if words:
        segments = [
            {"start": _attr(w, "start", 0), "text": _attr(w, "word", "")} for w in words
        ]
    else:
        segments = [
            {"start": _attr(s, "start", 0), "text": _attr(s, "text", "")}
            for s in (getattr(resp, "segments", None) or [])
        ]
    return text, segments


def _attr(obj, name, default=None):
    """Read a field whether the SDK returns objects or dicts."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


_LOCAL_MODEL = None


def _get_local_model():
    """Load faster-whisper large-v3 once and reuse it (loading is expensive)."""
    global _LOCAL_MODEL
    if _LOCAL_MODEL is None:
        from faster_whisper import WhisperModel

        # device='auto' picks CUDA if available else CPU; compute_type='auto'
        # picks an efficient type (int8_float16 on GPU, int8 on CPU) per your setup.
        # Override via WHISPER_DEVICE / WHISPER_COMPUTE_TYPE.
        _LOCAL_MODEL = WhisperModel(
            settings.whisper_model,
            device=settings.whisper_device,
            compute_type=settings.whisper_compute_type,
            cpu_threads=settings.whisper_cpu_threads,
        )
    return _LOCAL_MODEL


def _transcribe_local(audio: Path) -> tuple[str, list[dict]]:
    model = _get_local_model()
    forced = settings.whisper_language or None  # "" -> auto-detect; "ur"/"en" to force

    def _run(lang):
        return model.transcribe(
            str(audio),
            language=lang,
            word_timestamps=True,
            vad_filter=True,  # skip silence — big speedup, fewer hallucinations
        )

    # faster-whisper runs language detection up front and exposes it on `info`
    # BEFORE the (lazy) segment generator is consumed, so we can cheaply re-decode.
    seg_iter, info = _run(forced)
    detected = getattr(info, "language", None)
    # Urdu and Hindi are the same spoken language; large-v3 auto-detect often
    # tags Urdu speech as 'hi' and decodes it into Devanagari, which then never
    # matches Perso-Arabic Urdu keywords. This tool only monitors Urdu+English,
    # so treat a 'hi' detection as Urdu and re-decode in the correct script.
    if forced is None and detected == "hi":
        logger.info("faster-whisper: detected 'hi' — re-decoding as Urdu (ur/en scope)")
        seg_iter, info = _run("ur")
        detected = getattr(info, "language", None)
    logger.info("faster-whisper: language=%s (p=%.2f)",
                detected, getattr(info, "language_probability", 0.0))
    segments, texts = [], []
    for s in seg_iter:
        texts.append(s.text)
        if getattr(s, "words", None):
            for w in s.words:
                # w.start is the deep-link timestamp for that word.
                segments.append({"start": w.start, "text": w.word})
        else:
            segments.append({"start": s.start, "text": s.text})
    return " ".join(texts).strip(), segments
