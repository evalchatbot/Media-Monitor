"""Probe a YouTube channel URL and infer likely daily bulletin slots."""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import YouTubeChannel
from app.youtube.discovery import Video, fetch_uploads, resolve_channel_id, uploads_playlist_id
from config import settings

_PKT = ZoneInfo(settings.youtube_timezone)
_MAX_SLOTS = 5
_SNAP_MINUTES = 90
_MIN_DURATION = 120

_BULLETIN_WORDS = (
    "headline", "headlines", "bulletin", "news bulletin", "breaking news",
    "headlines bulletin", "news headlines",
)

_CANONICAL_SLOTS: list[tuple[str, str, list[str]]] = [
    ("00:00:00", "Midnight", ["12am", "12 am", "00:00", "midnight", "0:00"]),
    ("06:00:00", "6 AM", ["6am", "6 am", "06:00", "6:00"]),
    ("09:00:00", "9 AM", ["9am", "9 am", "09:00", "9:00"]),
    ("12:00:00", "12 PM", ["12pm", "12 pm", "12:00", "noon"]),
    ("15:00:00", "3 PM", ["3pm", "3 pm", "15:00", "3:00"]),
    ("18:00:00", "6 PM", ["6pm", "6 pm", "18:00", "6:00"]),
    ("21:00:00", "9 PM", ["9pm", "9 pm", "21:00", "9:00"]),
]

_TIME_IN_TITLE = re.compile(
    r"\b(\d{1,2})\s*(:\d{2})?\s*(am|pm|a\.m\.|p\.m\.)\b",
    re.IGNORECASE,
)


def _handle_from_url(url: str) -> str:
    m = re.search(r"youtube\.com/(@[\w.-]+)", url or "", re.IGNORECASE)
    return m.group(1) if m else ""


def _looks_like_bulletin(v: Video) -> bool:
    title = (v.title or "").casefold()
    if v.live:
        return False
    if v.duration_seconds is not None and v.duration_seconds < _MIN_DURATION:
        return False
    if any(w in title for w in _BULLETIN_WORDS):
        return True
    if _TIME_IN_TITLE.search(v.title or ""):
        return True
    if "news" in title and any(w in title for w in ("am", "pm", "bulletin", "headline")):
        return True
    return False


def _snap_slot(local_dt: datetime) -> tuple[str, str, list[str]] | None:
    minutes = local_dt.hour * 60 + local_dt.minute
    best: tuple[str, str, list[str]] | None = None
    best_dist = 9999
    for local_time, label, rules in _CANONICAL_SLOTS:
        hh, mm, _ = (int(x) for x in local_time.split(":"))
        slot_min = hh * 60 + mm
        dist = min(abs(minutes - slot_min), 1440 - abs(minutes - slot_min))
        if dist < best_dist:
            best_dist = dist
            best = (local_time, label, rules)
    if best is None or best_dist > _SNAP_MINUTES:
        return None
    return best


def detect_slots_from_videos(videos: list[Video]) -> list[dict]:
    """Infer up to 5 bulletin slots from recent uploads (PKT)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=21)
    by_slot: dict[str, dict] = {}
    counts: Counter[str] = Counter()

    for v in videos:
        if not _looks_like_bulletin(v):
            continue
        pub = v.published
        if pub is None:
            continue
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc)
        if pub < cutoff:
            continue
        local = pub.astimezone(_PKT)
        snapped = _snap_slot(local)
        if not snapped:
            continue
        local_time, label, rules = snapped
        counts[local_time] += 1
        row = by_slot.setdefault(
            local_time,
            {
                "local_time": local_time,
                "label": label,
                "title_rules": rules + ["headline", "headlines", "bulletin", "news"],
                "samples": 0,
                "example_title": "",
            },
        )
        row["samples"] += 1
        if not row["example_title"]:
            row["example_title"] = (v.title or "")[:120]

    ordered = sorted(
        by_slot.values(),
        key=lambda s: (-counts[s["local_time"]], s["local_time"]),
    )
    return ordered[:_MAX_SLOTS]


def probe_channel(url_or_handle: str) -> dict:
    """Return probe summary for UI (mirrors sources_probe shape)."""
    raw = (url_or_handle or "").strip()
    if not raw:
        return {"ok": False, "summary": "Enter a YouTube channel URL or @handle.", "detail": {}}

    channel_id, name = resolve_channel_id(raw)
    if not channel_id:
        return {
            "ok": False,
            "summary": "Could not resolve a YouTube channel from that link.",
            "detail": {"input": raw},
        }

    url = raw if raw.startswith("http") else f"https://www.youtube.com/{raw.lstrip('@')}"
    if not url.startswith("http"):
        url = f"https://www.youtube.com/@{raw.lstrip('@')}"
    handle = _handle_from_url(url)
    playlist_id = uploads_playlist_id(channel_id)

    session = SessionLocal()
    try:
        existing = session.execute(
            select(YouTubeChannel).where(YouTubeChannel.channel_id == channel_id)
        ).scalar_one_or_none()
    finally:
        session.close()

    videos = fetch_uploads(channel_id, max_results=50, playlist_id=playlist_id)
    bulletinish = [v for v in videos if _looks_like_bulletin(v)]
    slots = detect_slots_from_videos(videos)

    if existing:
        return {
            "ok": False,
            "summary": f"“{existing.name or name}” is already in your channel list.",
            "detail": {
                "channel_id": channel_id,
                "name": existing.name or name,
                "already_added": True,
                "slots": slots,
            },
        }

    if not slots:
        return {
            "ok": False,
            "summary": (
                f"Found {name or 'channel'} but no headline bulletin pattern in recent uploads. "
                "Try a main news channel with titled bulletins (e.g. 9 PM Headlines)."
            ),
            "detail": {
                "channel_id": channel_id,
                "name": name,
                "url": url,
                "handle": handle,
                "uploads_playlist_id": playlist_id,
                "uploads_checked": len(videos),
                "bulletin_like": len(bulletinish),
                "slots": [],
            },
        }

    slot_line = ", ".join(s["label"] for s in slots)
    return {
        "ok": True,
        "summary": (
            f"Found {len(slots)} likely bulletin slot{'s' if len(slots) != 1 else ''} "
            f"for {name or 'this channel'}: {slot_line}."
        ),
        "detail": {
            "channel_id": channel_id,
            "name": name,
            "url": url,
            "handle": handle,
            "uploads_playlist_id": playlist_id,
            "uploads_checked": len(videos),
            "bulletin_like": len(bulletinish),
            "already_added": False,
            "slots": slots,
        },
    }


def save_channel(
    session,
    *,
    channel_id: str,
    name: str,
    url: str,
    handle: str,
    uploads_playlist_id: str,
    slots: list[dict],
) -> YouTubeChannel:
    """Insert channel + detected bulletin slots."""
    from app.db.models import BulletinSlot

    existing = session.execute(
        select(YouTubeChannel).where(YouTubeChannel.channel_id == channel_id)
    ).scalar_one_or_none()
    if existing:
        return existing

    row = YouTubeChannel(
        channel_id=channel_id,
        name=(name or channel_id).strip(),
        handle=(handle or "").strip(),
        url=(url or "").strip(),
        uploads_playlist_id=(uploads_playlist_id or "").strip(),
        timezone=settings.youtube_timezone,
        media_source=settings.youtube_media_source,
        active=True,
    )
    session.add(row)
    session.flush()

    for slot in slots[:_MAX_SLOTS]:
        local_time = slot.get("local_time")
        if not local_time:
            continue
        session.add(
            BulletinSlot(
                channel_id=row.id,
                local_time=local_time,
                label=slot.get("label") or local_time[:5],
                title_rules=list(slot.get("title_rules") or []),
                min_duration_sec=120,
                max_duration_sec=settings.youtube_max_duration_seconds,
                enabled=True,
            )
        )
    session.commit()
    session.refresh(row)
    return row
