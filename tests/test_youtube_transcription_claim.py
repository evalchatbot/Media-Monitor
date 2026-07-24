# -*- coding: utf-8 -*-
"""Transcription is billed per audio-hour, so it must run at most once per video
even when two *processes* race — a redeploy that restarts a scan mid-flight, or a
stray second instance pointed at the same database. The claim below uses the
transcripts.video_id unique row as a cross-process lock.

Regression guard for the bug where ~90% of daily Whisper calls were the same
bulletins transcribed 2–9× and thrown away on the unique-constraint collision.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.models import Base, BulletinSlot, Transcript, YouTubeBulletin, YouTubeChannel
from app.youtube import pipeline
from app.youtube.discovery import Video


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _now():
    return datetime.now(timezone.utc)


def _claim_row(session, video_id="vid1", *, age_s=0, transcriber="transcribing"):
    row = Transcript(
        video_id=video_id, transcriber=transcriber, model="", text="", segments=[],
        created_at=_now() - timedelta(seconds=age_s),
    )
    session.add(row)
    session.commit()
    return row


# --- the claim primitive ---------------------------------------------------

def test_absent_video_two_workers_only_one_claims(session):
    """Both workers see no transcript and both try to claim; exactly one wins.
    The loser gets None and must skip the Groq call."""
    first = pipeline._claim_transcription(session, "vid1", existing=None)
    assert first is not None, "first worker should win the claim"

    second = pipeline._claim_transcription(session, "vid1", existing=None)
    assert second is None, "second worker must lose and skip transcription"

    rows = session.execute(select(Transcript).where(Transcript.video_id == "vid1")).scalars().all()
    assert len(rows) == 1, "only one claim row may exist"


def test_metadata_stub_is_claimed_for_upgrade(session):
    stub = Transcript(video_id="vid1", transcriber="metadata", model="metadata",
                      text="title only", segments=[], created_at=_now())
    session.add(stub)
    session.commit()

    assert pipeline._needs_transcription(stub) is True
    claimed = pipeline._claim_transcription(session, "vid1", existing=stub)
    assert claimed is not None
    assert (claimed.transcriber or "").lower() == "transcribing"


def test_fresh_claim_blocks_a_second_worker(session):
    claim = _claim_row(session, age_s=0)
    assert pipeline._is_fresh_claim(claim) is True
    assert pipeline._needs_transcription(claim) is False

    lost = pipeline._claim_transcription(session, "vid1", existing=claim)
    assert lost is None, "a fresh claim held elsewhere must not be reclaimable"


def test_stale_claim_is_reclaimable(session):
    claim = _claim_row(session, age_s=pipeline._CLAIM_STALE_S + 60)
    assert pipeline._is_fresh_claim(claim) is False
    assert pipeline._needs_transcription(claim) is True

    regained = pipeline._claim_transcription(session, "vid1", existing=claim)
    assert regained is not None, "a dead worker's stale claim must be reclaimable"


def test_real_transcript_needs_nothing(session):
    real = Transcript(video_id="vid1", transcriber="groq", model="whisper-large-v3-turbo",
                      text="full transcript", segments=[{"start": 1, "text": "x"}],
                      created_at=_now())
    session.add(real)
    session.commit()
    assert pipeline._is_fresh_claim(real) is False
    assert pipeline._needs_transcription(real) is False


def test_finalize_fills_and_releases_the_claim(session):
    claim = pipeline._claim_transcription(session, "vid1", existing=None)
    ch = YouTubeChannel(channel_id="UC1", name="Geo News", url="http://x")
    v = Video("vid1", "Geo 12pm", "", _now(), "Geo News", "UC1", "http://x", duration_seconds=780)
    pipeline._finalize_transcript(
        session, claim, v=v, ch=ch, text="hello world",
        segments=[{"start": 3, "text": "hello"}],
        meta={"model": "whisper-large-v3-turbo", "language": "ur"}, bulletin_id=None,
    )
    session.commit()
    assert claim.transcriber == "groq"
    assert claim.text == "hello world"
    assert pipeline._needs_transcription(claim) is False
    assert pipeline._is_fresh_claim(claim) is False


def test_release_removes_an_empty_claim(session):
    claim = pipeline._claim_transcription(session, "vid1", existing=None)
    pipeline._release_claim(session, claim)
    rows = session.execute(select(Transcript).where(Transcript.video_id == "vid1")).scalars().all()
    assert rows == [], "an unfulfilled empty claim must not linger as a phantom row"


# --- end-to-end through _process_bulletin ----------------------------------

class _Cand:
    def __init__(self, video, score=100.0):
        self.video = video
        self.score = score
        self.reasons = []


def _wire_discovery(monkeypatch, video):
    monkeypatch.setattr(pipeline, "_uploads_for_bulletin_day", lambda ch, b: [video])
    monkeypatch.setattr(pipeline.classifier, "classify_candidates", lambda *a, **k: [_Cand(video)])
    monkeypatch.setattr(pipeline.classifier, "pick_best", lambda scored, **k: (_Cand(video), [], False))
    monkeypatch.setattr(pipeline, "_emit_mention", lambda *a, **k: None)


def _bulletin_setup(session):
    ch = YouTubeChannel(channel_id="UC1", name="Geo News", url="http://x",
                        media_source="ytdlp", media_source_config={})
    session.add(ch)
    session.flush()
    slot = BulletinSlot(channel_id=ch.id, local_time="12:00:00", label="12 PM",
                        title_rules=["12pm"], min_duration_sec=120,
                        max_duration_sec=3600, enabled=True)
    session.add(slot)
    session.flush()
    b = _new_bulletin(session, ch, slot)
    return ch, b, slot


def _new_bulletin(session, ch, slot):
    b = YouTubeBulletin(channel_db_id=ch.id, slot_id=slot.id, slot_date="2026-07-24",
                        discovery_status="pending", transcription_status="pending", attempts=0)
    session.add(b)
    session.commit()
    return b


def test_process_bulletin_skips_groq_when_a_fresh_claim_exists(session, monkeypatch):
    """The core regression: a second worker arriving while another is mid-transcription
    must NOT call Whisper again."""
    v = Video("vidX", "Geo 12pm 24 July", "", _now(), "Geo News", "UC1",
              "http://x", duration_seconds=780)
    _wire_discovery(monkeypatch, v)
    _claim_row(session, video_id="vidX", age_s=5)   # another worker holds it, fresh

    def _boom(*a, **k):
        raise AssertionError("transcribe_audio must not run while a fresh claim exists")
    monkeypatch.setattr(pipeline.transcribe, "transcribe_audio", _boom)
    monkeypatch.setattr(pipeline.media_source, "acquire_audio", _boom)

    ch, b, slot = _bulletin_setup(session)
    pipeline._process_bulletin(session, None, b, ch, slot, [("rain", "en")],
                               defaultdict(int), force=False)

    rows = session.execute(select(Transcript).where(Transcript.video_id == "vidX")).scalars().all()
    assert len(rows) == 1, "no duplicate transcript row should be created"


def test_process_bulletin_transcribes_once_then_is_idempotent(session, monkeypatch):
    v = Video("vidY", "Geo 12pm 24 July", "", _now(), "Geo News", "UC1",
              "http://x", duration_seconds=780)
    _wire_discovery(monkeypatch, v)

    calls = {"n": 0}

    class _Asset:
        path = "/tmp/fake.flac"
    monkeypatch.setattr(pipeline.media_source, "acquire_audio", lambda **k: _Asset())
    monkeypatch.setattr(pipeline.media_source, "cleanup_asset", lambda a: None)
    monkeypatch.setattr(pipeline, "_upsert_cache", lambda *a, **k: None)

    def _fake_transcribe(path, **k):
        calls["n"] += 1
        return "prime minister spoke", [{"start": 4, "text": "prime"}], {
            "model": "whisper-large-v3-turbo", "language": "ur"}
    monkeypatch.setattr(pipeline.transcribe, "transcribe_audio", _fake_transcribe)

    ch, b, slot = _bulletin_setup(session)
    pipeline._process_bulletin(session, None, b, ch, slot, [("rain", "en")],
                               defaultdict(int), force=False)
    assert calls["n"] == 1, "first pass should transcribe once"
    stored = session.execute(select(Transcript).where(Transcript.video_id == "vidY")).scalar_one()
    assert stored.transcriber == "groq"
    assert stored.text == "prime minister spoke"

    # A later scan of the same bulletin must not pay Groq again.
    pipeline._process_bulletin(session, None, b, ch, slot, [("rain", "en")],
                               defaultdict(int), force=False)
    assert calls["n"] == 1, "second pass must reuse the stored transcript, not re-transcribe"
