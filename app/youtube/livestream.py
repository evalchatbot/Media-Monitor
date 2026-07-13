"""Live-stream detection + 30-second audio chunk tapping (credential-free detect,
resource-gated tap).

- `detect_live(channel_id)` uses yt-dlp on the channel's `/live` URL to tell if a
  channel is currently live. No API key needed.
- `tap_live_chunk(video_url, seconds)` records ~N seconds of the live audio to an
  mp3 using yt-dlp's section download (needs **ffmpeg** on PATH).

The upload pipeline handles VODs; this handles *live* broadcasts, tapped in short
chunks so a keyword spoken on-air is caught within a few minutes. Transcription
of the chunk still goes through `transcribe.py` (stub/openai/local flag), so the
whole live path only does real work when `YOUTUBE_LIVE_ENABLED=true`, a non-stub
transcriber is configured, and ffmpeg is installed. Otherwise it degrades to a
logged no-op — never a crash.
"""
from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


class _QuietLogger:
    """Swallow yt-dlp's own log lines (e.g. 'channel is not currently live')."""

    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


@dataclass
class LiveStream:
    video_id: str
    title: str
    url: str
    # seconds since the broadcast started (for the deep-link), or None if unknown
    elapsed: int | None = None


def detect_live(channel_id: str) -> LiveStream | None:
    """Return a LiveStream if the channel is broadcasting live right now, else None."""
    try:
        import yt_dlp
    except ImportError:
        logger.warning("yt-dlp not installed; cannot detect live streams")
        return None

    url = f"https://www.youtube.com/channel/{channel_id}/live"
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "logger": _QuietLogger()}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:
        # Not live / channel has no live page -> yt-dlp raises; treat as not-live.
        return None

    if not info or not info.get("is_live"):
        return None

    vid = info.get("id")
    if not vid:
        return None
    # Approximate elapsed time from the live duration if exposed.
    elapsed = info.get("duration")
    return LiveStream(
        video_id=vid,
        title=info.get("title", ""),
        url=f"https://www.youtube.com/watch?v={vid}",
        elapsed=int(elapsed) if isinstance(elapsed, (int, float)) else None,
    )


def tap_live_chunk(video_url: str, seconds: int = 30) -> Path | None:
    """Record ~`seconds` of live audio to an mp3. Returns the path or None.

    Needs ffmpeg on PATH. The caller owns the returned file's parent temp dir via
    the returned path (kept alive by the module-level TemporaryDirectory below).
    """
    try:
        import yt_dlp
        from yt_dlp.utils import download_range_func
    except ImportError:
        logger.warning("yt-dlp not installed; cannot tap live audio")
        return None

    tmp = tempfile.mkdtemp(prefix="live_")
    out = Path(tmp) / "chunk.%(ext)s"
    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(out),
        "quiet": True,
        "no_warnings": True,
        "logger": _QuietLogger(),
        # Grab only the first `seconds` of the current live edge.
        "download_ranges": download_range_func(None, [(0, seconds)]),
        "force_keyframes_at_cuts": True,
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}],
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([video_url])
    except Exception as exc:
        logger.warning("live tap failed for %s: %s (ffmpeg installed?)", video_url, exc)
        return None
    mp3 = Path(tmp) / "chunk.mp3"
    return mp3 if mp3.exists() else None
