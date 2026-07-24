"""Live-stream keyword search — a flow of its own, separate from bulletins
and custom period scans.

Pick a channel's CURRENT live stream, choose a time window inside its DVR, and
that window's audio is pulled, transcribed with Groq, and matched against the
active YouTube watchlist. Matches come back with stream timestamps that deep-
link into the player.

Nothing here writes mentions or touches the scan pipelines: jobs live in
memory, results render in the live panel only.
"""
from __future__ import annotations

import logging
import shutil
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

from config import settings

logger = logging.getLogger(__name__)

_API = "https://www.googleapis.com/youtube/v3"

# One transcription window is capped so a mis-typed range can't pull hours of
# audio: 30 min ≈ $0.02 of Groq and ~2 min of wall clock.
MAX_WINDOW_S = 30 * 60
MIN_WINDOW_S = 10

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_MAX_JOBS_KEPT = 20


# ---------------------------------------------------------------------------
# Listing current live streams
# ---------------------------------------------------------------------------

def list_live(session) -> list[dict]:
    """Streams that are live RIGHT NOW on the active channels.

    Live broadcasts appear in a channel's uploads playlist while running, so
    one playlistItems page plus one videos.list per channel is enough — a few
    quota units per refresh, vs 100/channel for search.list(eventType=live).
    """
    from sqlalchemy import select

    from app.db.models import YouTubeChannel
    from app.youtube.discovery import uploads_playlist_id

    key = settings.youtube_api_key
    if not key:
        raise RuntimeError("YOUTUBE_API_KEY is required to list live streams")

    channels = session.execute(
        select(YouTubeChannel).where(YouTubeChannel.active.is_(True))
        .order_by(YouTubeChannel.name)
    ).scalars().all()

    now = datetime.now(timezone.utc)
    out: list[dict] = []
    for ch in channels:
        pid = ch.uploads_playlist_id or uploads_playlist_id(ch.channel_id)
        try:
            items = httpx.get(
                f"{_API}/playlistItems",
                params={"part": "contentDetails", "playlistId": pid,
                        "maxResults": 15, "key": key},
                timeout=20,
            ).json().get("items") or []
            vids = [
                v for v in (
                    (it.get("contentDetails") or {}).get("videoId") for it in items
                ) if v
            ]
            if not vids:
                continue
            resp = httpx.get(
                f"{_API}/videos",
                params={"part": "snippet,liveStreamingDetails",
                        "id": ",".join(vids[:50]), "key": key},
                timeout=20,
            ).json().get("items") or []
        except Exception as exc:
            logger.warning("live listing failed for %s: %s", ch.name, exc)
            continue

        for v in resp:
            sn = v.get("snippet") or {}
            if sn.get("liveBroadcastContent") != "live":
                continue  # excludes "upcoming" and finished streams
            lsd = v.get("liveStreamingDetails") or {}
            started_raw = lsd.get("actualStartTime") or ""
            elapsed = None
            if started_raw:
                try:
                    started = datetime.fromisoformat(started_raw.replace("Z", "+00:00"))
                    elapsed = max(0, int((now - started).total_seconds()))
                except ValueError:
                    pass
            out.append({
                "channel": ch.name,
                "video_id": v.get("id"),
                "title": sn.get("title") or "",
                "url": f"https://www.youtube.com/watch?v={v.get('id')}",
                "started_at": started_raw,
                "elapsed_seconds": elapsed,
                "viewers": lsd.get("concurrentViewers"),
            })
    return out


# ---------------------------------------------------------------------------
# Transcribe-and-match jobs
# ---------------------------------------------------------------------------

def start_job(video_id: str, start_s: int, end_s: int) -> str:
    """Kick off a background transcribe+match for [start_s, end_s] of a stream."""
    start_s, end_s = int(start_s), int(end_s)
    if end_s - start_s < MIN_WINDOW_S:
        raise ValueError("window too short — pick at least 10 seconds")
    if end_s - start_s > MAX_WINDOW_S:
        raise ValueError("window too long — 30 minutes max per run")
    if start_s < 0:
        raise ValueError("window start cannot be negative")

    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "video_id": video_id,
            "window": [start_s, end_s],
            "state": "queued",
            "detail": "",
            "matches": None,
            "error": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        # Bound the registry; these are interactive one-offs, not records.
        while len(_jobs) > _MAX_JOBS_KEPT:
            oldest = min(_jobs, key=lambda k: _jobs[k]["created_at"])
            _jobs.pop(oldest, None)

    threading.Thread(
        target=_run_job, args=(job_id, video_id, start_s, end_s),
        name=f"yt-live-{job_id}", daemon=True,
    ).start()
    return job_id


def job_status(job_id: str) -> dict | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def _set(job_id: str, **fields) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(fields)


def _run_job(job_id: str, video_id: str, start_s: int, end_s: int) -> None:
    tmp: Path | None = None
    try:
        _set(job_id, state="downloading",
             detail=f"pulling audio {_mmss(start_s)}–{_mmss(end_s)} from the stream")
        try:
            audio, tmp, offset_s = _download_live_section(video_id, start_s, end_s)
        except LiveError as exc:
            _set(job_id, state="error", error=str(exc))
            return

        _set(job_id, state="transcribing", detail="Groq Whisper is transcribing the window")
        from app.youtube import transcribe

        text, segments, meta = transcribe.transcribe_audio(audio, language="ur")
        if not text and not segments:
            _set(job_id, state="error", error="transcription returned nothing")
            return

        # Segment times are relative to the downloaded window; shift by the
        # fragment-aligned offset so every timestamp is STREAM time and the
        # deep links land on the moment.
        for seg in segments:
            seg["start"] = float(seg.get("start") or 0) + offset_s
            if seg.get("end") is not None:
                seg["end"] = float(seg["end"]) + offset_s

        _set(job_id, state="matching", detail="matching the watchlist")
        from sqlalchemy import select

        from app.db.base import SessionLocal
        from app.db.models import Keyword
        from app.youtube import matcher

        session = SessionLocal()
        try:
            keywords = [
                (k.text, k.language or "ur")
                for k in session.execute(
                    select(Keyword).where(
                        Keyword.active.is_(True), Keyword.module == "youtube"
                    )
                ).scalars()
                if k.text
            ]
        finally:
            session.close()

        hits = matcher.find_all_hits(text, segments, keywords)
        matches = {
            kw: [
                {
                    "start": h.start,
                    "end": h.end,
                    "excerpt": h.excerpt,
                    "url": f"https://www.youtube.com/watch?v={video_id}&t={h.start}s",
                }
                for h in kw_hits
            ]
            for kw, kw_hits in hits.items()
        }
        _set(job_id, state="done", detail="", matches=matches,
             transcript_chars=len(text or ""))
    except Exception as exc:
        logger.exception("live job %s failed", job_id)
        _set(job_id, state="error", error=str(exc)[:300])
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


class LiveError(RuntimeError):
    """A problem worth showing to the user verbatim."""


# Resolving a stream (yt-dlp extract) costs a few seconds; the picked stream is
# then probed for its timeline AND downloaded, so cache the resolution briefly.
_resolve_cache: dict[str, tuple[float, dict]] = {}
_RESOLVE_TTL_S = 240


def _resolve_stream(video_id: str) -> dict:
    """DVR audio endpoint + true timeline for an ongoing stream.

    yt-dlp's live_from_start can't do ranged downloads (the generator protocol
    refuses), but it hands us the raw googlevideo URL — and YouTube live audio
    is plain 5s fMP4 fragments addressable with &sq=N. The head response's
    x-head-seqnum / x-head-time-sec headers are the ground truth for how much
    DVR actually exists (streams restart; the Data API's start time can be a
    session old).
    """
    import time as _time

    cached = _resolve_cache.get(video_id)
    if cached and _time.monotonic() - cached[0] < _RESOLVE_TTL_S:
        return cached[1]

    try:
        import yt_dlp
    except ImportError:
        raise LiveError("yt-dlp is not installed on the server")

    opts = {"quiet": True, "no_warnings": True, "live_from_start": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}", download=False
            )
    except Exception as exc:
        raise LiveError(f"couldn't open the stream: {str(exc)[:160]}")

    if not info.get("is_live"):
        raise LiveError("that stream is no longer live")

    fmts = [
        f for f in (info.get("formats") or [])
        if f.get("acodec") not in (None, "none") and f.get("url")
        and "dash" in str(f.get("protocol") or "")
    ]
    if not fmts:
        raise LiveError("no seekable audio track on this stream (DVR may be off)")
    # Audio-only preferred (vcodec none), then lowest bitrate — speech needs little.
    fmts.sort(key=lambda f: (f.get("vcodec") not in (None, "none"), f.get("abr") or 1e9))
    fmt = fmts[0]

    headers = fmt.get("http_headers") or {}
    try:
        head = httpx.get(fmt["url"], headers=headers, timeout=30, follow_redirects=True)
    except Exception as exc:
        raise LiveError(f"stream endpoint unreachable: {str(exc)[:120]}")
    head_seq = int(head.headers.get("x-head-seqnum") or -1)
    head_time = float(head.headers.get("x-head-time-sec") or 0)
    if head.status_code != 200 or head_seq < 0 or head_time <= 0:
        raise LiveError("stream does not expose a rewindable (DVR) timeline")
    seg_dur = head_time / head_seq if head_seq > 0 else float(fmt.get("target_duration") or 5.0)

    resolved = {
        "url": fmt["url"],
        "headers": headers,
        "seg_dur": seg_dur,
        "head_seq": head_seq,
        "head_time": head_time,
    }
    _resolve_cache[video_id] = (_time.monotonic(), resolved)
    return resolved


def stream_timeline(video_id: str) -> dict:
    """How much of the stream is actually addressable right now."""
    r = _resolve_stream(video_id)
    return {
        "video_id": video_id,
        "head_seconds": int(r["head_time"]),
        "segment_seconds": r["seg_dur"],
    }


def _download_live_section(
    video_id: str, start_s: int, end_s: int
) -> tuple[Path, Path, int]:
    """Audio for [start_s, end_s] of the stream's DVR, 16 kHz mono for Groq.

    Returns (audio_path, tmp_dir, actual_offset_s). The offset is the first
    fragment's start — fragment-aligned, so it can be a couple of seconds
    before the requested start; timestamps are shifted by THIS value.
    """
    r = _resolve_stream(video_id)
    head_time = r["head_time"]
    if start_s >= head_time:
        raise LiveError(
            f"the stream only has {_mmss(int(head_time))} of rewind so far — "
            "pick a window inside that"
        )
    end_c = min(float(end_s), head_time)
    seg = r["seg_dur"]
    sq_a = int(start_s // seg)
    sq_b = max(sq_a, int((end_c - 0.001) // seg))

    tmp = Path(tempfile.mkdtemp(prefix="yt-live-"))
    raw = tmp / "live.m4a"
    try:
        with httpx.Client(
            headers=r["headers"], timeout=30, follow_redirects=True
        ) as client, open(raw, "wb") as out:
            for sq in range(sq_a, sq_b + 1):
                resp = client.get(f"{r['url']}&sq={sq}")
                if resp.status_code != 200:
                    raise LiveError(
                        f"stream fragment {sq} returned HTTP {resp.status_code} — "
                        "that part of the stream may have left the DVR window"
                    )
                out.write(resp.content)
    except LiveError:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        raise LiveError(f"download failed: {str(exc)[:120]}")

    if raw.stat().st_size == 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise LiveError("no audio came back for that window")

    from app.youtube.media_source import _to_flac

    audio = _to_flac(raw)
    if audio is None:
        shutil.rmtree(tmp, ignore_errors=True)
        raise LiveError("audio conversion failed on the server")
    return audio, tmp, int(sq_a * seg)


def _mmss(seconds: int) -> str:
    s = max(0, int(seconds))
    if s >= 3600:
        return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


# ---------------------------------------------------------------------------
# Ticker OCR (visual) — read the scrolling Urdu ticker, match the watchlist
# ---------------------------------------------------------------------------

# Windows are short on purpose: OCR is one Gemini call per changed frame, and a
# free-tier key rate-limits fast. 8 min at ~1 changed frame/3s stays usable.
TICKER_WINDOW_MAX_S = 8 * 60
_OCR_MIN_GAP_S = 3          # never OCR more often than this (bounds cost)
_MAX_OCR_PER_JOB = 120      # hard ceiling per run
_DEDUP_WINDOW_S = 20        # same keyword seen again within this = same event


def start_ticker_job(video_id: str, start_s: int, end_s: int) -> str:
    start_s, end_s = int(start_s), int(end_s)
    if end_s - start_s < MIN_WINDOW_S:
        raise ValueError("window too short — pick at least 10 seconds")
    if end_s - start_s > TICKER_WINDOW_MAX_S:
        raise ValueError(f"window too long — {TICKER_WINDOW_MAX_S // 60} minutes max for ticker reading")
    if not settings.gemini_api_key:
        raise ValueError("ticker reading needs GEMINI_API_KEY (Urdu OCR)")

    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id, "kind": "ticker", "video_id": video_id,
            "window": [start_s, end_s], "state": "queued", "detail": "",
            "matches": None, "error": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        while len(_jobs) > _MAX_JOBS_KEPT:
            _jobs.pop(min(_jobs, key=lambda k: _jobs[k]["created_at"]), None)

    threading.Thread(
        target=_run_ticker_job, args=(job_id, video_id, start_s, end_s),
        name=f"yt-ticker-{job_id}", daemon=True,
    ).start()
    return job_id


def _run_ticker_job(job_id: str, video_id: str, start_s: int, end_s: int) -> None:
    tmp: Path | None = None
    try:
        _set(job_id, state="downloading",
             detail=f"pulling video {_mmss(start_s)}–{_mmss(end_s)} from the stream")
        try:
            frames, tmp, offset_s = _extract_ticker_frames(video_id, start_s, end_s)
        except LiveError as exc:
            _set(job_id, state="error", error=str(exc))
            return
        if not frames:
            _set(job_id, state="error", error="no video frames came back for that window")
            return

        from sqlalchemy import select

        from app.db.base import SessionLocal
        from app.db.models import Keyword

        session = SessionLocal()
        try:
            keywords = [
                (k.text, k.language or "ur")
                for k in session.execute(
                    select(Keyword).where(
                        Keyword.active.is_(True), Keyword.module == "youtube"
                    )
                ).scalars()
                if k.text
            ]
        finally:
            session.close()
        if not keywords:
            _set(job_id, state="error", error="no active YouTube keywords to match")
            return

        from app.core.keywords import find_matches

        # Walk frames once/second. Skip a frame whose ticker crop is unchanged
        # from the last OCR'd one (paging tickers, freeze frames); otherwise OCR
        # at most every _OCR_MIN_GAP_S so a scrolling ticker can't run up cost.
        last_hash = None
        last_ocr_t = -999
        ocr_count = 0
        seen_ts: dict[str, float] = {}   # keyword.casefold() -> last stream-time hit
        matches: dict[str, list] = {}
        total = len(frames)
        for idx, (t_rel, frame_path) in enumerate(frames):
            t_stream = t_rel + offset_s
            crop = _crop_ticker(frame_path)
            if crop is None:
                continue
            h = _crop_hash(crop)
            if last_hash is not None and _hash_close(h, last_hash):
                continue
            if t_stream - last_ocr_t < _OCR_MIN_GAP_S:
                last_hash = h
                continue
            if ocr_count >= _MAX_OCR_PER_JOB:
                _set(job_id, detail=f"reached the {_MAX_OCR_PER_JOB}-frame cap — shorten the window for full coverage")
                break

            _set(job_id, state="reading",
                 detail=f"reading ticker · frame {idx + 1}/{total} · {ocr_count} OCR calls")
            text = _gemini_ocr(crop)
            last_hash = h
            last_ocr_t = t_stream
            ocr_count += 1
            if not text:
                continue

            for m in find_matches(text, keywords):
                fold = m.keyword.casefold()
                prev = seen_ts.get(fold)
                if prev is not None and t_stream - prev < _DEDUP_WINDOW_S:
                    seen_ts[fold] = t_stream   # extend the same event
                    continue
                seen_ts[fold] = t_stream
                matches.setdefault(m.keyword, []).append({
                    "start": int(t_stream),
                    "excerpt": text[:200],
                    "url": f"https://www.youtube.com/watch?v={video_id}&t={int(t_stream)}s",
                })

        _set(job_id, state="done", detail="", matches=matches, ocr_calls=ocr_count)
    except Exception as exc:
        logger.exception("ticker job %s failed", job_id)
        _set(job_id, state="error", error=str(exc)[:300])
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


def _extract_ticker_frames(
    video_id: str, start_s: int, end_s: int
) -> tuple[list[tuple[int, Path]], Path, int]:
    """Download the video window (480p) and pull one frame per second.

    Returns ([(seconds_into_window, frame_path)], tmp_dir, offset_s).
    """
    import subprocess

    r = _resolve_video_stream(video_id)
    head_time = r["head_time"]
    if start_s >= head_time:
        raise LiveError(
            f"the stream only has {_mmss(int(head_time))} of rewind so far — "
            "pick a window inside that"
        )
    end_c = min(float(end_s), head_time)
    seg = r["seg_dur"]
    sq_a = int(start_s // seg)
    sq_b = max(sq_a, int((end_c - 0.001) // seg))

    tmp = Path(tempfile.mkdtemp(prefix="yt-ticker-"))
    raw = tmp / "video.mp4"
    try:
        with httpx.Client(headers=r["headers"], timeout=30, follow_redirects=True) as client, \
                open(raw, "wb") as out:
            for sq in range(sq_a, sq_b + 1):
                resp = client.get(f"{r['url']}&sq={sq}")
                if resp.status_code != 200:
                    raise LiveError(
                        f"video fragment {sq} returned HTTP {resp.status_code} — "
                        "that part may have left the DVR window"
                    )
                out.write(resp.content)
    except LiveError:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        raise LiveError(f"video download failed: {str(exc)[:120]}")

    if shutil.which("ffmpeg") is None:
        shutil.rmtree(tmp, ignore_errors=True)
        raise LiveError("ffmpeg is not available on the server")

    fdir = tmp / "frames"
    fdir.mkdir()
    subprocess.run(
        ["ffmpeg", "-y", "-nostdin", "-loglevel", "error", "-i", str(raw),
         "-vf", "fps=1", "-q:v", "4", str(fdir / "f_%04d.jpg")],
        capture_output=True, timeout=180,
    )
    frames = [
        (i, p) for i, p in enumerate(sorted(fdir.glob("f_*.jpg")))
    ]
    return frames, tmp, int(sq_a * seg)


def _resolve_video_stream(video_id: str) -> dict:
    """Same as _resolve_stream but for a 480p video-only DASH track."""
    import time as _time

    key = f"v:{video_id}"
    cached = _resolve_cache.get(key)
    if cached and _time.monotonic() - cached[0] < _RESOLVE_TTL_S:
        return cached[1]

    try:
        import yt_dlp
    except ImportError:
        raise LiveError("yt-dlp is not installed on the server")
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "live_from_start": True}) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    if not info.get("is_live"):
        raise LiveError("that stream is no longer live")

    vids = [
        f for f in (info.get("formats") or [])
        if f.get("vcodec") not in (None, "none") and f.get("url")
        and "dash" in str(f.get("protocol") or "") and f.get("height")
    ]
    if not vids:
        raise LiveError("no seekable video track on this stream (DVR may be off)")
    # 480p is enough for ticker text and a fraction of 720p's bytes.
    vids.sort(key=lambda f: abs((f.get("height") or 0) - 480))
    fmt = vids[0]

    headers = fmt.get("http_headers") or {}
    head = httpx.get(fmt["url"], headers=headers, timeout=30, follow_redirects=True)
    head_seq = int(head.headers.get("x-head-seqnum") or -1)
    head_time = float(head.headers.get("x-head-time-sec") or 0)
    if head.status_code != 200 or head_seq < 0 or head_time <= 0:
        raise LiveError("stream does not expose a rewindable (DVR) timeline")
    resolved = {
        "url": fmt["url"], "headers": headers,
        "seg_dur": head_time / head_seq if head_seq > 0 else float(fmt.get("target_duration") or 2.0),
        "head_seq": head_seq, "head_time": head_time,
    }
    _resolve_cache[key] = (_time.monotonic(), resolved)
    return resolved


def _crop_ticker(frame_path: Path):
    """Crop the main lower-third Urdu ticker band; drop the channel logo corner."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        img = Image.open(frame_path).convert("RGB")
    except Exception:
        return None
    w, h = img.size
    top = settings.ticker_crop_top
    bot = settings.ticker_crop_bottom
    right = settings.ticker_crop_right
    return img.crop((0, int(h * top), int(w * right), int(h * bot)))


def _crop_hash(crop) -> list[int]:
    """Coarse average-hash of the crop — cheap sameness check between frames."""
    small = crop.convert("L").resize((32, 8))
    px = list(small.getdata())
    avg = sum(px) / len(px)
    return [1 if p > avg else 0 for p in px]


def _hash_close(a: list[int], b: list[int], tol: int = 6) -> bool:
    return sum(1 for x, y in zip(a, b) if x != y) <= tol


def _gemini_ocr(crop) -> str:
    """Transcribe the Urdu ticker text in the crop with Gemini. '' on failure."""
    import base64
    import io
    import time as _time

    buf = io.BytesIO()
    crop.save(buf, format="JPEG", quality=88)
    b64 = base64.b64encode(buf.getvalue()).decode()
    model = settings.gemini_model or "gemini-flash-latest"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "contents": [{"parts": [
            {"text": "Transcribe ONLY the Urdu ticker text in this image, verbatim, "
                     "as plain Urdu on one line. Do not translate or add anything. "
                     "Empty string if nothing readable."},
            {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
        ]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 300},
    }
    for attempt in range(4):
        try:
            r = httpx.post(url, params={"key": settings.gemini_api_key}, json=body, timeout=60)
        except Exception:
            _time.sleep(2)
            continue
        if r.status_code == 200:
            try:
                return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            except Exception:
                return ""
        if r.status_code in (429, 503):
            _time.sleep(2.5 * (attempt + 1))  # rate-limited / overloaded — back off
            continue
        logger.info("ticker OCR HTTP %s: %s", r.status_code, r.text[:120])
        return ""
    return ""
