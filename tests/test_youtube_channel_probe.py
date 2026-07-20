"""Tests for YouTube channel bulletin slot detection."""
from __future__ import annotations

from datetime import datetime, timezone

from app.youtube.channel_probe import detect_slots_from_videos
from app.youtube.discovery import Video


def _vid(title, hour_pkt, minute=0):
    # PKT = UTC+5
    utc = datetime(2026, 7, 20, hour_pkt - 5, minute, tzinfo=timezone.utc)
    return Video(
        video_id="x",
        title=title,
        description="",
        published=utc,
        channel_name="Test News",
        channel_id="UCtest123456789012345678",
        url="https://www.youtube.com/watch?v=x",
        duration_seconds=600,
    )


def test_detects_standard_bulletin_slots():
    videos = [
        _vid("9 PM News Headlines 20 July 2026", 21, 5),
        _vid("Headlines 9 PM Bulletin", 21, 10),
        _vid("12 PM Headlines Bulletin", 12, 8),
        _vid("6 PM News Headlines", 18, 0),
        _vid("Morning Headlines 9 AM", 9, 0),
    ]
    slots = detect_slots_from_videos(videos)
    labels = {s["label"] for s in slots}
    assert "9 PM" in labels
    assert "12 PM" in labels
    assert len(slots) <= 5


def test_ignores_short_clips():
    v = _vid("9 PM Headlines", 21)
    v.duration_seconds = 45
    assert detect_slots_from_videos([v]) == []
