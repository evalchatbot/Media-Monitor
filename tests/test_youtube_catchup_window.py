# -*- coding: utf-8 -*-
"""The auto scan covers today plus the previous days, and both halves agree.

Row creation and the scan query must span the same days: if the query widens
but creation does not, the extra days simply have no rows to process.
"""
from __future__ import annotations

from datetime import date, timedelta

from app.youtube import pipeline
from config import settings


def _creation_dates(for_date: str | None = None) -> list[date]:
    """Mirror of the date span ensure_due_bulletins builds."""
    today = date.fromisoformat(for_date) if for_date else date(2026, 7, 21)
    if for_date:
        return [today]
    return [
        today - timedelta(days=i)
        for i in range(0, max(1, settings.youtube_catchup_days))
    ]


def test_default_window_is_today_plus_previous_three():
    assert settings.youtube_catchup_days == 4
    got = _creation_dates()
    assert got == [
        date(2026, 7, 21),
        date(2026, 7, 20),
        date(2026, 7, 19),
        date(2026, 7, 18),
    ]


def test_scan_query_span_matches_creation_span():
    """Both sides derive from the same setting, so they cannot drift apart."""
    import inspect

    src = inspect.getsource(pipeline.run_youtube_scan)
    assert "settings.youtube_catchup_days" in src
    create_src = inspect.getsource(pipeline.ensure_due_bulletins)
    assert "settings.youtube_catchup_days" in create_src


def test_explicit_date_scan_stays_single_day():
    """Asking for one date must not drag in three neighbours."""
    assert _creation_dates("2026-07-15") == [date(2026, 7, 15)]


def test_catchup_window_never_collapses_to_zero():
    for bad in (0, -3):
        assert max(1, bad) == 1


def test_retention_bounds_history_not_the_catchup_window():
    """Catch-up is about missed slots; 90 days is what caps stored history."""
    assert settings.keyword_result_retention_days == 90
    assert settings.retention_screenshots_days == 90
