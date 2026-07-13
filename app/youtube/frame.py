"""Grab a still frame from a YouTube video at an exact second.

When a keyword is found in a video's transcript at time T, we already build a
deep-link (youtube.com/watch?v=ID&t=Ts). This adds the visual: a screenshot of
the actual video frame at T — so the detection shows *what was on screen* the
moment the word was said, without anyone opening the video.

How: yt-dlp resolves a direct progressive/video stream URL, then ffmpeg seeks to
T and decodes a single frame (`-ss T -i URL -frames:v 1`). Fast (byte-range
keyframe seek — it does not download the whole video). Needs ffmpeg on PATH; if
ffmpeg or yt-dlp is missing, or the stream can't be resolved, it degrades to
None and the detection still carries the deep-link.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


class _Quiet:
    def debug(self, m): pass
    def info(self, m): pass
    def warning(self, m): pass
    def error(self, m): pass


def _stream_url(video_url: str) -> str | None:
    """Resolve a single downloadable video stream URL for frame extraction."""
    try:
        import yt_dlp
    except ImportError:
        logger.warning("yt-dlp not installed; cannot capture video frame")
        return None
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "logger": _Quiet(),
        # Prefer a combined progressive stream (single URL, has audio+video);
        # fall back to a video-only stream. Cap resolution to keep it light.
        "format": "best[height<=720][acodec!=none][vcodec!=none]/bestvideo[height<=720]/best",
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
    except Exception as exc:
        logger.warning("frame: could not resolve stream for %s: %s", video_url, exc)
        return None
    if info.get("url"):
        return info["url"]
    # Merged selection (video+audio): pick the video track's URL.
    for f in info.get("requested_formats") or []:
        if f.get("vcodec") not in (None, "none") and f.get("url"):
            return f["url"]
    return None


def capture_frame(video_url: str, seconds: int, out_path: Path) -> bool:
    """Save a single frame at `seconds` to `out_path` (jpg). Return True on success."""
    if shutil.which("ffmpeg") is None:
        logger.info("ffmpeg not on PATH — skipping video frame capture")
        return False
    stream = _stream_url(video_url)
    if not stream:
        return False
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # -ss BEFORE -i => fast input seek (keyframe, byte-range). -frames:v 1 => one frame.
    cmd = [
        "ffmpeg", "-y", "-nostdin", "-loglevel", "error",
        "-ss", str(max(0, int(seconds))), "-i", stream,
        "-frames:v", "1", "-q:v", "3", str(out_path),
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=90)
    except Exception as exc:
        logger.warning("frame: ffmpeg failed for %s@%ss: %s", video_url, seconds, exc)
        return False
    ok = out_path.exists() and out_path.stat().st_size > 0
    if not ok:
        logger.warning("frame: no frame produced for %s@%ss", video_url, seconds)
    return ok
