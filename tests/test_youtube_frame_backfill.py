# -*- coding: utf-8 -*-
"""Frames are captured after results are on screen, not before.

Grabbing a frame costs ~8s per video (stream resolution + ffmpeg), which was
the entire wait on an interactive keyword search. Mentions are now written
without media and the frames are filled in behind them.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.models import Base, Mention, YouTubeChannel
from app.youtube import pipeline


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(YouTubeChannel(
            channel_id="UCtest", name="City42", active=True, media_source="ytdlp",
        ))
        s.commit()
        yield s


@pytest.fixture
def captures(monkeypatch):
    """Record capture calls; pretend every grab succeeds."""
    calls: list[tuple[str, int]] = []

    def fake_capture(video_id, seconds, out_path, *, mode=None):
        calls.append((video_id, int(seconds)))
        return True

    monkeypatch.setattr(pipeline.frame, "capture_frame", fake_capture)
    monkeypatch.setattr(pipeline.frame, "stamp_frame", lambda *a, **k: None)
    return calls


def _mention(**over):
    base = dict(
        module="youtube", external_id="vid123", source="City42",
        section="9 AM · 2026-07-21", title="bulletin",
        url="https://youtu.be/vid123",
        matched_keywords=["عمران خان"],
        keyword_hits={"عمران خان": [{"start": 283, "end": 286, "excerpt": "..."}]},
        keyword_media={},
    )
    base.update(over)
    return Mention(**base)


def test_backfill_captures_missing_frames(session, captures):
    session.add(_mention())
    session.commit()

    filled = pipeline.backfill_youtube_frames(session)

    assert filled == 1
    assert captures == [("vid123", 283)]
    m = session.execute(select(Mention)).scalar_one()
    assert "عمران خان" in (m.keyword_media or {})
    assert m.screenshot_path, "card needs a thumbnail once a frame exists"


def test_backfill_skips_mentions_that_already_have_media(session, captures):
    session.add(_mention(keyword_media={"عمران خان": "/data/storage/youtube/a.jpg"}))
    session.commit()

    assert pipeline.backfill_youtube_frames(session) == 0
    assert captures == [], "must not re-grab a frame that already exists"


def test_backfill_records_the_path_on_the_hit(session, captures):
    """The hit's screenshot field is what the UI reads per keyword."""
    session.add(_mention())
    session.commit()

    pipeline.backfill_youtube_frames(session)

    m = session.execute(select(Mention)).scalar_one()
    assert m.keyword_hits["عمران خان"][0]["screenshot"]


def test_backfill_respects_its_limit(session, captures):
    for i in range(5):
        session.add(_mention(
            external_id=f"vid{i}",
            keyword_hits={"عمران خان": [{"start": 10 + i, "excerpt": "..."}]},
        ))
    session.commit()

    assert pipeline.backfill_youtube_frames(session, limit=2) == 2
    assert len(captures) == 2, "limit bounds work per run so a scan cannot stall"


def test_hit_without_timestamp_is_skipped(session, captures):
    session.add(_mention(keyword_hits={"عمران خان": [{"excerpt": "no start"}]}))
    session.commit()

    assert pipeline.backfill_youtube_frames(session) == 0
    assert captures == []
