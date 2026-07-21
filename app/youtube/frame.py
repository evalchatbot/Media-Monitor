"""Capture a still frame from a YouTube video at an exact timestamp.

Speed rules:
  - Never block ~30s on a dead embed player.
  - Prefer ffmpeg seek when media_source=ytdlp.
  - Always fall back to the public YouTube thumbnail (instant) so mentions
    still get an image and deep-links remain authoritative.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import time
from pathlib import Path

import httpx

from config import settings

logger = logging.getLogger(__name__)

# Hard caps — embed used to hang 30s+ per keyword and stall the whole scan.
_EMBED_GOTO_MS = 8_000
_EMBED_SHOT_MS = 5_000
_FFMPEG_FRAME_SEC = 25

# Resolving a stream URL costs a full yt-dlp round trip. One video yields one
# frame per matched keyword, so without this every extra keyword re-paid it.
# Google signs these URLs for hours; 30 min is a safe reuse window.
_STREAM_TTL_S = 1800
_stream_cache: dict[str, tuple[float, str]] = {}


def capture_frame(
    video_id: str,
    seconds: int,
    out_path: Path,
    *,
    mode: str | None = None,
) -> bool:
    """Save a JPEG frame at `seconds`. Return True on success."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists() and out_path.stat().st_size > 0:
        return True
    seek = max(0, int(seconds) - 1)
    mode = (mode or settings.youtube_media_source or "ytdlp").lower()

    if mode == "ytdlp":
        if _capture_ytdlp(f"https://www.youtube.com/watch?v={video_id}", seek, out_path):
            return True
    elif _capture_embed(video_id, seek, out_path):
        return True
    elif mode != "stub" and _capture_ytdlp(
        f"https://www.youtube.com/watch?v={video_id}", seek, out_path
    ):
        return True

    return _capture_thumbnail(video_id, out_path)


def stamp_frame(path: Path, *, channel: str, slot_label: str, slot_date: str, mmss: str) -> None:
    """Overlay a small footer on the captured frame."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return
    if not path.exists():
        return
    try:
        img = Image.open(path).convert("RGB")
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("DejaVuSans.ttf", 16)
        except Exception:
            font = ImageFont.load_default()
        label = f"{channel} · {slot_label} {slot_date} · {mmss}"
        pad = 8
        bbox = draw.textbbox((0, 0), label, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        y0 = img.height - th - pad * 2
        draw.rectangle((0, y0, img.width, img.height), fill=(10, 20, 40))
        draw.text((pad, y0 + pad), label, fill=(255, 255, 255), font=font)
        img.save(path, format="JPEG", quality=88)
    except Exception as exc:
        logger.warning("frame stamp failed: %s", exc)


def _capture_embed(video_id: str, seconds: int, out_path: Path) -> bool:
    """Playwright + official embed seek. Fail fast — never hang the scan."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False

    embed = (
        f"https://www.youtube.com/embed/{video_id}"
        f"?start={seconds}&autoplay=1&mute=1&controls=0&rel=0&modestbranding=1"
    )
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    viewport={"width": 1280, "height": 720},
                    device_scale_factor=1,
                )
                page = context.new_page()
                page.set_default_timeout(_EMBED_SHOT_MS)
                page.goto(embed, wait_until="domcontentloaded", timeout=_EMBED_GOTO_MS)
                page.wait_for_timeout(800)
                page.evaluate(
                    """(sec) => {
                      const v = document.querySelector('video');
                      if (v) { try { v.currentTime = sec; v.pause(); } catch (e) {} }
                    }""",
                    seconds,
                )
                page.wait_for_timeout(400)
                loc = page.locator("video").first
                if loc.count() > 0 and loc.is_visible(timeout=1500):
                    loc.screenshot(path=str(out_path), type="jpeg", quality=85, timeout=_EMBED_SHOT_MS)
                else:
                    page.screenshot(path=str(out_path), type="jpeg", quality=85, timeout=_EMBED_SHOT_MS)
                context.close()
            finally:
                browser.close()
    except Exception as exc:
        logger.info("embed frame skipped for %s@%ss: %s", video_id, seconds, exc)
        return False
    return out_path.exists() and out_path.stat().st_size > 0


def _capture_ytdlp(video_url: str, seconds: int, out_path: Path) -> bool:
    if shutil.which("ffmpeg") is None:
        return False
    stream = _stream_url(video_url)
    if not stream:
        return False
    cmd = [
        "ffmpeg", "-y", "-nostdin", "-loglevel", "error",
        "-ss", str(max(0, int(seconds))), "-i", stream,
        "-frames:v", "1", "-q:v", "3", str(out_path),
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=_FFMPEG_FRAME_SEC)
    except Exception as exc:
        logger.info("ffmpeg frame skipped: %s", exc)
        return False
    return out_path.exists() and out_path.stat().st_size > 0


def _capture_thumbnail(video_id: str, out_path: Path) -> bool:
    """Instant fallback — official poster frame (not seek-accurate)."""
    for name in ("hqdefault", "mqdefault", "default"):
        url = f"https://i.ytimg.com/vi/{video_id}/{name}.jpg"
        try:
            r = httpx.get(url, timeout=8, follow_redirects=True)
            if r.status_code == 200 and r.content and len(r.content) > 500:
                out_path.write_bytes(r.content)
                return True
        except Exception:
            continue
    return False


def _stream_url(video_url: str) -> str | None:
    cached = _stream_cache.get(video_url)
    if cached and time.monotonic() - cached[0] < _STREAM_TTL_S:
        return cached[1]

    url = _resolve_stream_url(video_url)
    if url:
        _stream_cache[video_url] = (time.monotonic(), url)
    return url


def _resolve_stream_url(video_url: str) -> str | None:
    try:
        import yt_dlp
    except ImportError:
        return None

    class _Quiet:
        def debug(self, m): pass
        def info(self, m): pass
        def warning(self, m): pass
        def error(self, m): pass

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "logger": _Quiet(),
        "format": "best[height<=720][acodec!=none][vcodec!=none]/bestvideo[height<=720]/best",
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
    except Exception as exc:
        logger.info("frame stream resolve failed: %s", exc)
        return None
    if info.get("url"):
        return info["url"]
    for f in info.get("requested_formats") or []:
        if f.get("vcodec") not in (None, "none") and f.get("url"):
            return f["url"]
    return None


def format_mmss(seconds: int) -> str:
    s = max(0, int(seconds))
    return f"{s // 60:d}:{s % 60:02d}"
