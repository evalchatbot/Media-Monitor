"""Media acquisition adapters for YouTube transcription.

Groq Whisper needs a local audio file — a YouTube watch-page link cannot be
passed to the API directly. Flow for bulletin monitoring:

  1. Prefer a broadcaster-provided direct audio URL when configured.
  2. Otherwise download audio from the discovered bulletin watch URL (yt-dlp)
     and hand the file to Groq.

Adapters:
  ytdlp      — download audio from the bulletin YouTube URL (default)
  authorized — broadcaster direct media URL; falls back to ytdlp if missing
  stub       — no audio (metadata / discovery only)
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from config import settings

logger = logging.getLogger(__name__)


@dataclass
class MediaAsset:
    path: Path
    cleanup: bool = True


def acquire_audio(
    *,
    video_id: str,
    video_url: str,
    media_source: str | None = None,
    media_source_config: dict | None = None,
) -> MediaAsset | None:
    """Return a local audio file ready for Groq, or None."""
    mode = (media_source or settings.youtube_media_source or "ytdlp").lower()
    cfg = media_source_config or {}

    if mode == "stub" or settings.youtube_metadata_only:
        return None

    if mode == "authorized":
        # Prefer per-video override, then a template with {video_id}, then a fixed URL.
        direct = (
            cfg.get("audio_urls", {}).get(video_id)
            or (cfg.get("audio_url_template") or "").format(video_id=video_id)
            or cfg.get("audio_url")
            or ""
        )
        if direct:
            asset = _download_direct(direct)
            if asset is not None:
                return asset
            logger.info(
                "authorized audio failed for %s — falling back to bulletin URL",
                video_id,
            )
        else:
            logger.info(
                "no authorized audio URL for %s — using bulletin watch URL",
                video_id,
            )
        return _download_ytdlp(video_url or f"https://www.youtube.com/watch?v={video_id}")

    if mode == "ytdlp":
        url = video_url or f"https://www.youtube.com/watch?v={video_id}"
        logger.info("fetching bulletin audio via yt-dlp: %s", url)
        return _download_ytdlp(url)

    logger.warning("Unknown youtube media_source=%s", mode)
    return None


def _download_direct(url: str) -> MediaAsset | None:
    tmp = Path(tempfile.mkdtemp(prefix="yt-audio-"))
    dest = tmp / "source.bin"
    try:
        with httpx.stream("GET", url, timeout=120, follow_redirects=True) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in resp.iter_bytes():
                    fh.write(chunk)
    except Exception as exc:
        logger.warning("authorized audio download failed: %s", exc)
        shutil.rmtree(tmp, ignore_errors=True)
        return None
    flac = _to_flac(dest)
    if flac is None:
        shutil.rmtree(tmp, ignore_errors=True)
        return None
    try:
        dest.unlink(missing_ok=True)
    except Exception:
        pass
    return MediaAsset(path=flac, cleanup=True)


def _download_ytdlp(video_url: str) -> MediaAsset | None:
    try:
        import yt_dlp
    except ImportError:
        logger.warning("yt-dlp not installed")
        return None
    tmp = Path(tempfile.mkdtemp(prefix="yt-ytdlp-"))
    outtmpl = str(tmp / "audio.%(ext)s")
    opts = {
        # Speech ASR gains nothing above ~64 kbps once we downmix to 16 kHz mono,
        # so take the smallest adequate audio-only stream and skip downloading a
        # high-bitrate track we immediately throw away.
        "format": "bestaudio[abr<=64]/bestaudio[abr<=96]/bestaudio/best",
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        # Avoid brittle extract-audio postprocess; we convert to flac ourselves.
        "prefer_ffmpeg": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([video_url])
    except Exception as exc:
        logger.warning("ytdlp audio download failed: %s", exc)
        shutil.rmtree(tmp, ignore_errors=True)
        return None
    downloaded = next(
        (p for p in tmp.iterdir() if p.is_file() and p.suffix.lower() not in (".part", ".ytdl")),
        None,
    )
    if downloaded is None:
        logger.warning("ytdlp produced no file for %s", video_url)
        shutil.rmtree(tmp, ignore_errors=True)
        return None
    flac = _to_flac(downloaded)
    if flac is None:
        shutil.rmtree(tmp, ignore_errors=True)
        return None
    try:
        if downloaded.resolve() != flac.resolve():
            downloaded.unlink(missing_ok=True)
    except Exception:
        pass
    return MediaAsset(path=flac, cleanup=True)


def _to_flac(src: Path) -> Path | None:
    """Encode to 16 kHz mono for Groq.

    Opus by default: Whisper resamples to 16 kHz mono regardless, so lossless
    FLAC only buys upload size. FLAC runs ~1 MB/min, which pushed bulletins past
    the 20 MB limit and forced multi-request chunking; Opus at 32 kbps is ~4x
    smaller and keeps even a 40-minute bulletin in a single request.
    Falls back to FLAC when the Opus encoder is unavailable.
    """
    if shutil.which("ffmpeg") is None:
        logger.warning("ffmpeg not on PATH — cannot convert audio for Groq")
        return None

    kbps = max(8, int(settings.youtube_audio_bitrate_kbps or 32))
    attempts = [
        (".ogg", ["-c:a", "libopus", "-b:a", f"{kbps}k", "-vbr", "on"]),
        (".flac", ["-c:a", "flac"]),
    ]
    for suffix, codec_args in attempts:
        dest = src.with_suffix(suffix)
        cmd = [
            "ffmpeg", "-y", "-nostdin", "-loglevel", "error",
            "-i", str(src),
            "-ar", "16000", "-ac", "1", "-map", "0:a",
            *codec_args,
            str(dest),
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=180, check=False)
        except Exception as exc:
            logger.warning("ffmpeg convert failed (%s): %s", suffix, exc)
            continue
        if dest.exists() and dest.stat().st_size > 0:
            return dest
        logger.info("audio encode produced nothing for %s — trying next codec", suffix)
    return None


def cleanup_asset(asset: MediaAsset | None) -> None:
    if not asset or not asset.cleanup:
        return
    try:
        parent = asset.path.parent
        if asset.path.exists():
            asset.path.unlink()
        if parent.exists() and parent.name.startswith(("yt-audio-", "yt-ytdlp-")):
            shutil.rmtree(parent, ignore_errors=True)
    except Exception:
        pass
