# -*- coding: utf-8 -*-
"""Discovery lists only as far back as the oldest bulletin that needs work.

These channels publish 180-260 videos a day, so every extra day of listing
costs real pagination. A scan where only today's slot is due must not pay to
page through the whole catch-up window.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.youtube import pipeline


class _Ch:
    def __init__(self):
        self.channel_id = "UCtest"
        self.timezone = "Asia/Karachi"
        self.uploads_playlist_id = ""


def _bulletin(day):
    return SimpleNamespace(slot_date=day.isoformat())


@pytest.fixture
def spy(monkeypatch):
    """Record each range fetch and return one video per day in range."""
    calls: list[tuple[datetime, datetime]] = []

    def fake_range(channel_id, *, published_after, published_before, playlist_id="", max_pages=15):
        calls.append((published_after, published_before))
        out, cur = [], published_after
        while cur <= published_before:
            out.append(SimpleNamespace(
                video_id=f"v{cur.date()}", title=f"bulletin {cur.date()}",
                published=cur, live=False, duration_seconds=700,
            ))
            cur += timedelta(days=1)
        return out

    monkeypatch.setattr(pipeline.discovery, "fetch_uploads_in_range", fake_range)
    monkeypatch.setattr(pipeline.discovery, "fetch_uploads", lambda *a, **k: [])
    pipeline._day_uploads_cache.clear()
    return calls


def _today():
    return datetime.now(pipeline._PKT).date()


def test_todays_slot_lists_only_today(spy):
    pipeline._uploads_for_bulletin_day(_Ch(), _bulletin(_today()))
    assert len(spy) == 1
    span_days = (spy[0][1] - spy[0][0]).days
    assert span_days <= 2, "listing today's slot must not page back days"


def test_second_slot_same_day_reuses_the_listing(spy):
    ch, today = _Ch(), _today()
    pipeline._uploads_for_bulletin_day(ch, _bulletin(today))
    pipeline._uploads_for_bulletin_day(ch, _bulletin(today))
    assert len(spy) == 1, "one listing must serve every slot on that day"


def test_older_slot_expands_the_window_once(spy):
    ch, today = _Ch(), _today()
    pipeline._uploads_for_bulletin_day(ch, _bulletin(today))
    pipeline._uploads_for_bulletin_day(ch, _bulletin(today - timedelta(days=3)))
    assert len(spy) == 2, "reaching further back needs one deeper fetch"
    assert spy[1][0] < spy[0][0], "the second fetch must start earlier"
    # once deep, a mid-range day is already covered
    pipeline._uploads_for_bulletin_day(ch, _bulletin(today - timedelta(days=1)))
    assert len(spy) == 2, "a day inside the fetched window must not refetch"


def test_cache_is_per_channel(spy):
    a, b = _Ch(), _Ch()
    b.channel_id = "UCother"
    today = _today()
    pipeline._uploads_for_bulletin_day(a, _bulletin(today))
    pipeline._uploads_for_bulletin_day(b, _bulletin(today))
    assert len(spy) == 2, "channels must not share a listing"
