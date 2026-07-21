# -*- coding: utf-8 -*-
"""A bulletin title names its own edition — use it to pick the right slot.

Without this the midnight slot scored "10PM News Bulletin" and "12AM News
Bulletin" almost identically and the pair fell into needs_review.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.youtube.classifier import _title_hours, classify_candidates, pick_best
from app.youtube.discovery import Video


@pytest.mark.parametrize("title,want", [
    ("12AM News Bulletin | 21 July 2026 | City 42", {0}),
    ("10PM News Bulletin | 20 July 2026", {22}),
    ("US-Iran War LIVE | 3 PM Headlines", {15}),
    ("05AM News Headlines | 21 July 2026", {5}),
    ("12PM News Headlines", {12}),
    ("9 a.m. bulletin", {9}),
])
def test_reads_the_edition_from_the_title(title, want):
    assert _title_hours(title) == want


@pytest.mark.parametrize("title", [
    "Heavy Rain in Pakistan | Rains Breaks 10 Year Record",
    "Iran Claims 17 US Troops Killed",
    "Meeting at 9:30 today",
    "Budget 2026 highlights",
])
def test_ignores_numbers_that_are_not_clock_times(title):
    assert _title_hours(title) == set()


def _video(vid, title, published, dur=700):
    return Video(
        video_id=vid, title=title, description="", published=published,
        channel_name="City42", channel_id="UCx",
        url=f"https://youtu.be/{vid}", duration_seconds=dur, live=False,
    )


def test_midnight_slot_prefers_the_12am_edition_over_10pm():
    """The exact case that was landing in needs_review."""
    near = datetime(2026, 7, 20, 19, 10, tzinfo=timezone.utc)  # ~00:10 PKT
    videos = [
        _video("a", "10PM News Bulletin | 20 July 2026 | City 42", near, 960),
        _video("b", "12AM News Bulletin | 21 July 2026 | City 42", near, 1080),
    ]
    scored = classify_candidates(
        videos, slot_date="2026-07-21", local_time="00:00:00",
        title_rules=["bulletin", "news", "12am"], tz="Asia/Karachi",
    )
    winner, _, needs_review = pick_best(scored)
    assert not needs_review, "the title says which edition it is"
    assert winner.video.video_id == "b"


def test_a_title_naming_another_edition_is_penalised():
    near = datetime(2026, 7, 21, 10, 5, tzinfo=timezone.utc)  # ~15:05 PKT
    videos = [
        _video("wrong", "US-Iran War LIVE | 5 PM Headlines", near),
        _video("right", "US-Iran War LIVE | 3 PM Headlines", near),
    ]
    scored = classify_candidates(
        videos, slot_date="2026-07-21", local_time="15:00:00",
        title_rules=["headlines", "news"], tz="Asia/Karachi",
    )
    assert scored[0].video.video_id == "right"


def test_untimed_titles_are_left_alone():
    """No clock in the title means no opinion — neither bonus nor penalty."""
    near = datetime(2026, 7, 21, 10, 5, tzinfo=timezone.utc)
    videos = [_video("x", "Heavy Rain in Pakistan | News Headlines", near)]
    scored = classify_candidates(
        videos, slot_date="2026-07-21", local_time="15:00:00",
        title_rules=["headlines"], tz="Asia/Karachi",
    )
    assert scored, "an untimed bulletin must still be a candidate"
    assert not any("other_slot_time" in r for r in scored[0].reasons)
