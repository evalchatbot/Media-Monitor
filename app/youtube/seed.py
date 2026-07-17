"""Seed the five official news channels and their default bulletin slots."""
from __future__ import annotations

import logging

from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import BulletinSlot, YouTubeChannel
from config import settings

logger = logging.getLogger(__name__)

# Verified official main channels (as of plan research).
_CHANNELS = [
    {
        "channel_id": "UC_vt34wimdCzdkrzVejwX9g",
        "name": "Geo News",
        "handle": "@geonews",
        "url": "https://www.youtube.com/@geonews",
        "uploads_playlist_id": "UU_vt34wimdCzdkrzVejwX9g",
        # Geo has a prominent 8 AM bulletin; keep requested 9 AM as optional extra.
        "slots": [
            ("06:00:00", "6 AM", ["6am", "6 am", "06:00", "6:00"]),
            ("08:00:00", "8 AM", ["8am", "8 am", "08:00", "8:00"]),
            ("09:00:00", "9 AM", ["9am", "9 am", "09:00", "9:00"]),
            ("12:00:00", "12 PM", ["12pm", "12 pm", "12:00", "noon"]),
            ("15:00:00", "3 PM", ["3pm", "3 pm", "15:00", "3:00"]),
            ("18:00:00", "6 PM", ["6pm", "6 pm", "18:00", "6:00"]),
            ("21:00:00", "9 PM", ["9pm", "9 pm", "21:00", "9:00"]),
            ("00:00:00", "Midnight", ["12am", "12 am", "00:00", "midnight", "0:00"]),
        ],
    },
    {
        "channel_id": "UCMmpLL2ucRHAXbNHiCPyIyg",
        "name": "ARY News",
        "handle": "@ARYNEWSofficial",
        "url": "https://www.youtube.com/@ARYNEWSofficial",
        "uploads_playlist_id": "UUMmpLL2ucRHAXbNHiCPyIyg",
        "slots": [
            ("06:00:00", "6 AM", ["6am", "6 am", "06:00"]),
            ("09:00:00", "9 AM", ["9am", "9 am", "09:00"]),
            ("12:00:00", "12 PM", ["12pm", "12 pm", "12:00", "noon"]),
            ("15:00:00", "3 PM", ["3pm", "3 pm", "15:00"]),
            ("18:00:00", "6 PM", ["6pm", "6 pm", "18:00"]),
            ("21:00:00", "9 PM", ["9pm", "9 pm", "21:00"]),
            ("00:00:00", "Midnight", ["12am", "12 am", "00:00", "midnight"]),
        ],
    },
    {
        "channel_id": "UCTur7oM6mLL0rM2k0znuZpQ",
        "name": "Express News",
        "handle": "@ExpressNewspkofficial",
        "url": "https://www.youtube.com/@ExpressNewspkofficial",
        "uploads_playlist_id": "UUTur7oM6mLL0rM2k0znuZpQ",
        "slots": [
            ("06:00:00", "6 AM", ["6am", "6 am", "06:00"]),
            ("09:00:00", "9 AM", ["9am", "9 am", "09:00"]),
            ("12:00:00", "12 PM", ["12pm", "12 pm", "12:00", "noon"]),
            ("15:00:00", "3 PM", ["3pm", "3 pm", "15:00", "4pm", "4 pm", "16:00"]),
            ("18:00:00", "6 PM", ["6pm", "6 pm", "18:00"]),
            ("21:00:00", "9 PM", ["9pm", "9 pm", "21:00"]),
            ("00:00:00", "Midnight", ["12am", "12 am", "00:00", "midnight"]),
        ],
    },
    {
        "channel_id": "UCJekW1Vj5fCVEGdye_mBN6Q",
        "name": "SAMAA TV",
        "handle": "@samaatv",
        "url": "https://www.youtube.com/@samaatv",
        "uploads_playlist_id": "UUJekW1Vj5fCVEGdye_mBN6Q",
        "slots": [
            ("06:00:00", "6 AM", ["6am", "6 am", "06:00"]),
            ("09:00:00", "9 AM", ["9am", "9 am", "09:00"]),
            ("12:00:00", "12 PM", ["12pm", "12 pm", "12:00", "12:30", "noon"]),
            ("15:00:00", "3 PM", ["3pm", "3 pm", "15:00"]),
            ("18:00:00", "6 PM", ["6pm", "6 pm", "18:00"]),
            ("21:00:00", "9 PM", ["9pm", "9 pm", "21:00"]),
            ("00:00:00", "Midnight", ["12am", "12 am", "00:00", "12:30 am", "midnight"]),
        ],
    },
    {
        "channel_id": "UCdTup4kK7Ze08KYp7ReiuMw",
        "name": "City42",
        "handle": "@City42Lahore",
        "url": "https://www.youtube.com/@City42Lahore",
        "uploads_playlist_id": "UUdTup4kK7Ze08KYp7ReiuMw",
        "slots": [
            ("06:00:00", "6 AM", ["6am", "6 am", "06:00"]),
            ("09:00:00", "9 AM", ["9am", "9 am", "09:00"]),
            ("12:00:00", "12 PM", ["12pm", "12 pm", "12:00", "noon"]),
            ("15:00:00", "3 PM", ["3pm", "3 pm", "15:00"]),
            ("18:00:00", "6 PM", ["6pm", "6 pm", "18:00"]),
            # City42 often publishes a full evening bulletin around 10 PM.
            ("21:00:00", "9 PM", ["9pm", "9 pm", "21:00"]),
            ("22:00:00", "10 PM", ["10pm", "10 pm", "22:00"]),
            ("00:00:00", "Midnight", ["12am", "12 am", "00:00", "midnight"]),
        ],
    },
]


def ensure_seed_channels() -> None:
    """Idempotently insert the five channels and their default slots."""
    if not settings.youtube_enabled:
        return
    session = SessionLocal()
    try:
        for spec in _CHANNELS:
            row = session.execute(
                select(YouTubeChannel).where(YouTubeChannel.channel_id == spec["channel_id"])
            ).scalar_one_or_none()
            if row is None:
                row = YouTubeChannel(
                    channel_id=spec["channel_id"],
                    name=spec["name"],
                    handle=spec["handle"],
                    url=spec["url"],
                    uploads_playlist_id=spec["uploads_playlist_id"],
                    timezone=settings.youtube_timezone,
                    media_source=settings.youtube_media_source,
                    active=True,
                )
                session.add(row)
                session.flush()
                logger.info("Seeded YouTube channel %s", spec["name"])
            else:
                # Keep stable IDs/handles up to date without clobbering media_source.
                row.name = row.name or spec["name"]
                row.handle = row.handle or spec["handle"]
                row.url = row.url or spec["url"]
                if not row.uploads_playlist_id:
                    row.uploads_playlist_id = spec["uploads_playlist_id"]

            existing_times = {
                s.local_time
                for s in session.execute(
                    select(BulletinSlot).where(BulletinSlot.channel_id == row.id)
                ).scalars()
            }
            for local_time, label, rules in spec["slots"]:
                if local_time in existing_times:
                    continue
                session.add(
                    BulletinSlot(
                        channel_id=row.id,
                        local_time=local_time,
                        label=label,
                        title_rules=rules + ["headline", "headlines", "bulletin", "news"],
                        min_duration_sec=120,
                        max_duration_sec=settings.youtube_max_duration_seconds,
                        enabled=True,
                    )
                )
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("YouTube channel seed failed")
    finally:
        session.close()
