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
import tempfile
from pathlib import Path

from config import settings

logger = logging.getLogger(__name__)


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


def find_keyword_second(segments: list[dict], keyword: str) -> int | None:
    """Return the start second of the earliest mention of `keyword`.

    `segments` may be word-level ([{start, text=<word>}, ...]) or segment-level;
    this scans the token stream so a multi-word keyword matches across adjacent
    words, returning the start time of the FIRST word — giving word-level deep-
    link precision when word timestamps are available, segment-level otherwise.
    """
    if not segments:
        return None
    kw_tokens = keyword.lower().split()
    if not kw_tokens:
        return None

    def clean(t: str) -> str:
        return t.lower().strip(".,!?;:\"'()[]—–…")

    stream: list[tuple[str, float]] = []  # (token, start_second)
    for seg in segments:
        start = seg.get("start", 0) or 0
        for tok in (seg.get("text") or "").split():
            stream.append((clean(tok), start))

    n = len(kw_tokens)
    for i in range(len(stream) - n + 1):
        if all(kw_tokens[j] in stream[i + j][0] for j in range(n)):
            return int(stream[i][1])
    # Fallback: whole keyword appears within a single segment's text.
    for seg in segments:
        if keyword.lower() in (seg.get("text") or "").lower():
            return int(seg.get("start", 0) or 0)
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


def _transcribe_local(audio: Path) -> tuple[str, list[dict]]:
    from faster_whisper import WhisperModel

    # device/compute_type left to the user's GPU setup; defaults are CPU-safe.
    model = WhisperModel(settings.whisper_model, device="auto", compute_type="auto")
    seg_iter, _info = model.transcribe(str(audio), word_timestamps=True)
    segments, texts = [], []
    for s in seg_iter:
        texts.append(s.text)
        if getattr(s, "words", None):
            for w in s.words:
                segments.append({"start": w.start, "text": w.word})
        else:
            segments.append({"start": s.start, "text": s.text})
    return " ".join(texts).strip(), segments
