# -*- coding: utf-8 -*-
"""An unattended YouTube scan must not re-write mentions when nothing changed.

The scheduled scan re-matches every already-processed ("ready") bulletin on
every run so a newly added keyword reaches old bulletins. Doing that
unconditionally re-emits each mention, which re-adds any (mention, keyword)
association the rolling-window policy trimmed moments earlier — an endless
per-scan DELETE+INSERT/UPDATE tug-of-war (observed in prod: 762 live rows but
~104k dead tuples, 161 MB). The re-match is now gated on a fingerprint of the
active keyword set: unchanged keywords -> zero re-match -> zero mention writes.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db import models as M
from app.youtube import pipeline
from app.youtube import discovery, frame
from config import settings

_PKT = ZoneInfo("Asia/Karachi")


class _NullNotifier:
    def send(self, *a, **k):
        return False


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Isolated SQLite DB wired into the pipeline, with network + frame capture
    and the keyword-fingerprint state file all pointed somewhere harmless."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    monkeypatch.setattr(pipeline, "SessionLocal", TestSession)
    monkeypatch.setattr(pipeline, "get_notifier", lambda: _NullNotifier())
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage", raising=False)
    monkeypatch.setattr(frame, "capture_frame", lambda *a, **k: False)
    monkeypatch.setattr(frame, "stamp_frame", lambda *a, **k: None)
    monkeypatch.setattr(discovery, "fetch_uploads_in_range", lambda *a, **k: [])
    monkeypatch.setattr(discovery, "fetch_uploads", lambda *a, **k: [])

    # Count only the writes we care about: rows in the `mentions` table.
    writes = {"UPDATE": 0, "INSERT": 0, "DELETE": 0}
    armed = {"on": False}

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, params, context, executemany):
        if not armed["on"]:
            return
        head = statement.strip().split()[0].upper()
        if head in writes and "mentions" in statement.lower():
            writes[head] += 1

    return TestSession, writes, armed


def _seed(TestSession, *, n_bulletins: int, keyword: str = "flood"):
    """One channel, `n_bulletins` slots/bulletins today — all 'ready' with a real
    transcript that mentions `keyword`, so a scan re-matches every one of them."""
    today = datetime.now(_PKT).date().isoformat()
    with TestSession() as s:
        ch = M.YouTubeChannel(channel_id="UCtest", name="Test News",
                              uploads_playlist_id="UUtest", timezone="Asia/Karachi",
                              media_source="stub", active=True)
        s.add(ch)
        s.flush()
        for i in range(n_bulletins):
            slot = M.BulletinSlot(channel_id=ch.id, local_time=f"{i % 24:02d}:{i:02d}:00",
                                  label=f"S{i}", title_rules=["news"],
                                  min_duration_sec=120, max_duration_sec=3600, enabled=True)
            s.add(slot)
            s.flush()
            b = M.YouTubeBulletin(
                channel_db_id=ch.id, slot_id=slot.id, slot_date=today,
                video_id=f"vid{i}", title=f"bulletin {i}", duration_seconds=600,
                published_at=datetime(2026, 8, 1, tzinfo=timezone.utc) + timedelta(minutes=i),
                discovery_status="ready", transcription_status="done", attempts=1)
            s.add(b)
            s.add(M.Transcript(
                video_id=f"vid{i}", bulletin_id=b.id, channel_id="UCtest", source="Test News",
                title=f"bulletin {i}", url=f"https://youtu.be/vid{i}", language="en",
                text=f"there was a {keyword} today",
                segments=[{"start": 10, "end": 13, "text": f"there was a {keyword} today"}],
                duration_seconds=600, transcriber="groq", model="whisper-large-v3-turbo"))
        s.add(M.Keyword(text=keyword, language="en", module="youtube", active=True))
        s.commit()


def test_noop_unattended_scan_writes_nothing(env):
    TestSession, writes, armed = env
    # More than the per-keyword quota so the rolling-window policy trims some:
    # this is exactly the case that used to churn every single scan.
    _seed(TestSession, n_bulletins=settings.keyword_result_limit + 5)

    pipeline.run_youtube_scan()  # cold run: creates + trims to the quota
    with TestSession() as s:
        assert s.query(M.Mention).count() == settings.keyword_result_limit

    for _ in range(3):
        writes.update(UPDATE=0, INSERT=0, DELETE=0)
        armed["on"] = True
        pipeline.run_youtube_scan()   # nothing changed -> must write nothing
        armed["on"] = False
        assert writes == {"UPDATE": 0, "INSERT": 0, "DELETE": 0}, (
            f"a no-op scan churned the mentions table: {writes}"
        )


def test_adding_a_keyword_retriggers_one_rematch_then_settles(env):
    TestSession, writes, armed = env
    _seed(TestSession, n_bulletins=3)

    pipeline.run_youtube_scan()  # cold run
    # steady state: unchanged keywords write nothing
    writes.update(UPDATE=0, INSERT=0, DELETE=0)
    armed["on"] = True
    pipeline.run_youtube_scan()
    armed["on"] = False
    assert writes == {"UPDATE": 0, "INSERT": 0, "DELETE": 0}

    # Add a second keyword that the transcripts also contain -> the active set
    # changes, so the next scan re-matches and applies it to the old bulletins.
    with TestSession() as s:
        # every seeded transcript says "there was a flood today"
        s.add(M.Keyword(text="today", language="en", module="youtube", active=True))
        s.commit()

    pipeline.run_youtube_scan()  # fingerprint changed -> re-match runs
    with TestSession() as s:
        tagged = [
            m for m in s.execute(
                select(M.Mention).where(M.Mention.module == "youtube")
            ).scalars()
            if "today" in (m.matched_keywords or [])
        ]
    assert tagged, "a newly added keyword must reach already-processed bulletins"

    # ...and the scan after that is quiet again.
    writes.update(UPDATE=0, INSERT=0, DELETE=0)
    armed["on"] = True
    pipeline.run_youtube_scan()
    armed["on"] = False
    assert writes == {"UPDATE": 0, "INSERT": 0, "DELETE": 0}


def test_targeted_scan_is_not_gated(env):
    """A user-driven scan (explicit slot_date) must always re-match, even when
    the keyword set is unchanged — the gate is only for the unattended scan."""
    TestSession, writes, armed = env
    _seed(TestSession, n_bulletins=3)
    today = datetime.now(_PKT).date().isoformat()

    pipeline.run_youtube_scan()          # cold unattended run, stores fingerprint
    # A targeted date scan re-matches regardless: force a mention change it must
    # re-apply. Delete a mention; a gated scan would leave it gone, a re-matching
    # one re-creates it.
    with TestSession() as s:
        victim = s.execute(select(M.Mention).where(M.Mention.module == "youtube")).scalars().first()
        vid = victim.external_id
        s.delete(victim)
        s.commit()

    pipeline.run_youtube_scan(slot_date=today)  # targeted -> not gated -> re-matches
    with TestSession() as s:
        again = s.execute(
            select(M.Mention).where(M.Mention.module == "youtube",
                                    M.Mention.external_id == vid)
        ).scalar_one_or_none()
    assert again is not None, "a targeted scan must re-match (it is never gated)"
