# -*- coding: utf-8 -*-
"""Everything retained must be searchable.

Data was kept for 90 days but searched over 30, so two thirds of the stored
transcripts were paid for and invisible to any newly added keyword.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.core import result_policy
from config import settings


def test_search_window_covers_everything_retained():
    assert settings.keyword_search_days >= settings.keyword_result_retention_days, (
        "retained data outside the search window can never be found"
    )


def test_search_floor_is_not_later_than_the_retention_floor():
    now = datetime.now(timezone.utc)
    search_age = (now - result_policy.search_cutoff()).days
    retention_age = (now - result_policy.cutoff()).days
    assert search_age >= retention_age


def test_retention_still_bounds_history_at_90_days():
    """Widening search must not quietly widen how much is stored."""
    assert settings.keyword_result_retention_days == 90
    assert settings.retention_screenshots_days == 90
