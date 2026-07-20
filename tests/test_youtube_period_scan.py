"""Custom period scan helpers."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.youtube.discovery import Video, _filter_uploads_in_range, _in_publish_range
from app.youtube.pipeline import period_bounds_from_parts, period_label, parse_period_iso

_PKT = ZoneInfo("Asia/Karachi")


def test_period_bounds_from_parts_pkt():
    start, end = period_bounds_from_parts("2026-07-15", "2026-07-16", "09:00", "18:00")
    assert start == datetime(2026, 7, 15, 4, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 7, 16, 13, 0, tzinfo=timezone.utc)


def test_parse_period_iso_roundtrip():
    start, end = period_bounds_from_parts("2026-07-01", "2026-07-02", "00:00", "23:59")
    s2, e2 = parse_period_iso(start.isoformat(), end.isoformat())
    assert s2 == start
    assert e2 == end


def test_period_label_same_day():
    start = datetime(2026, 7, 15, 9, 0, tzinfo=_PKT).astimezone(timezone.utc)
    end = datetime(2026, 7, 15, 18, 0, tzinfo=_PKT).astimezone(timezone.utc)
    assert "15 Jul 2026" in period_label(start, end)
    assert "09:00" in period_label(start, end)


def test_in_publish_range_excludes_outside():
    after = datetime(2026, 7, 15, 4, 0, tzinfo=timezone.utc)
    before = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)
    inside = datetime(2026, 7, 15, 6, 0, tzinfo=timezone.utc)
    outside = datetime(2026, 7, 14, 6, 0, tzinfo=timezone.utc)
    assert _in_publish_range(inside, after, before)
    assert not _in_publish_range(outside, after, before)


def test_filter_uploads_skips_live():
    after = datetime(2026, 7, 15, 0, 0, tzinfo=timezone.utc)
    before = datetime(2026, 7, 16, 0, 0, tzinfo=timezone.utc)
    pub = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    vids = [
        Video("a", "live", "", pub, "ch", "UC1", "url", live=True),
        Video("b", "ok", "", pub, "ch", "UC1", "url", live=False),
    ]
    out = _filter_uploads_in_range(vids, published_after=after, published_before=before)
    assert len(out) == 1
    assert out[0].video_id == "b"
