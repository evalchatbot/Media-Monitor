"""Unit tests for YouTube bulletin classification and keyword location."""
from __future__ import annotations

from datetime import datetime, timezone

from app.youtube.classifier import classify_candidates, pick_best, slot_airtime
from app.youtube.discovery import Video, deep_link
from app.youtube.matcher import find_all_hits, find_keyword_second, hit_is_verified, verified_json_hits


def _vid(vid, title, published, duration=600):
    return Video(
        video_id=vid,
        title=title,
        description="",
        published=published,
        channel_name="Geo News",
        channel_id="UC_vt34wimdCzdkrzVejwX9g",
        url=f"https://www.youtube.com/watch?v={vid}",
        duration_seconds=duration,
    )


def test_slot_airtime_midnight_belongs_to_slot_date():
    air = slot_airtime("2026-07-17", "00:00:00")
    assert air.day == 17
    assert air.hour == 0


def test_classify_prefers_titled_bulletin_near_slot():
    # 12 PM PKT = 07:00 UTC
    pub = datetime(2026, 7, 17, 7, 20, tzinfo=timezone.utc)
    videos = [
        _vid("a", "Random clip", pub, duration=60),
        _vid("b", "Geo News 12 PM Headlines 17 July 2026", pub, duration=900),
        _vid("c", "Breaking something else", pub, duration=400),
    ]
    scored = classify_candidates(
        videos,
        slot_date="2026-07-17",
        local_time="12:00:00",
        title_rules=["12pm", "12 pm", "headline", "headlines"],
    )
    winner, rejected, needs_review = pick_best(scored)
    assert not needs_review
    assert winner is not None
    assert winner.video.video_id == "b"


def test_pick_best_marks_ambiguous_tie():
    pub = datetime(2026, 7, 17, 7, 10, tzinfo=timezone.utc)
    videos = [
        _vid("a", "Geo News 12 PM Headlines", pub, duration=800),
        _vid("b", "Geo News 12PM Headlines Special", pub, duration=820),
    ]
    scored = classify_candidates(
        videos,
        slot_date="2026-07-17",
        local_time="12:00:00",
        title_rules=["12pm", "12 pm", "headline", "headlines"],
    )
    winner, rejected, needs_review = pick_best(scored, tie_margin=5.0)
    assert needs_review
    assert winner is None
    assert len(rejected) >= 2


def test_exact_keyword_timestamp():
    segments = [
        {"start": 10.0, "end": 10.4, "text": "prime"},
        {"start": 10.5, "end": 11.0, "text": "minister"},
        {"start": 11.2, "end": 11.8, "text": "shehbaz"},
        {"start": 12.0, "end": 12.5, "text": "sharif"},
    ]
    text = "prime minister shehbaz sharif"
    hits = find_all_hits(text, segments, [("Shehbaz Sharif", "en")])
    assert "Shehbaz Sharif" in hits
    assert hits["Shehbaz Sharif"][0].start == 11
    assert find_keyword_second(segments, "Shehbaz Sharif", "en") == 11


def test_deep_link_format():
    assert deep_link("abc123", 95) == "https://www.youtube.com/watch?v=abc123&t=95s"
    assert deep_link("abc123", None) == "https://www.youtube.com/watch?v=abc123"


def test_hit_verification_rejects_vague_stored_hit():
    assert not hit_is_verified(
        "Sarah Ahmed", "en", 120, "Heavy rainfall warning until July 24",
    )
    assert hit_is_verified(
        "Sarah Ahmed", "en", 120,
        "Minister Sarah Ahmed addressed the press conference today",
    )


def test_verified_json_hits_filters_stale_rows():
    hits = [
        {"start": 95, "excerpt": "Prime Minister Shehbaz Sharif spoke today"},
        {"start": 200, "excerpt": "Weather update for Lahore only"},
    ]
    kept = verified_json_hits("Shehbaz Sharif", "en", hits)
    assert len(kept) == 1
    assert kept[0]["start"] == 95


def test_cost_estimate_turbo():
    from app.youtube.transcribe import estimate_cost_usd

    # 15 minutes on turbo = 0.25h * 0.04 = 0.01
    assert estimate_cost_usd(15 * 60, model="whisper-large-v3-turbo") == 0.01
