"""A scan with no active keywords must not transcribe anything.

Transcription is billed per audio-hour and its only consumer is the matcher, so
scanning with an empty watchlist spends money to produce nothing.
"""
from __future__ import annotations

from app.youtube import pipeline


def test_scan_stops_before_touching_bulletins_when_no_keywords(monkeypatch):
    calls: list[str] = []

    class _Session:
        def close(self):
            pass

    monkeypatch.setattr(pipeline, "SessionLocal", lambda: _Session())
    monkeypatch.setattr(pipeline, "get_notifier", lambda: None)
    monkeypatch.setattr(
        pipeline, "repair_youtube_mentions", lambda s, **k: {"repaired": 0}
    )
    monkeypatch.setattr(pipeline, "_active_youtube_keywords", lambda s, ids=None: [])

    def _boom(*a, **k):
        calls.append("ensure_due_bulletins")
        raise AssertionError("must not create or scan bulletins with no keywords")

    monkeypatch.setattr(pipeline, "ensure_due_bulletins", _boom)

    summary = pipeline.run_youtube_scan()

    assert summary.get("skipped_no_keywords") is True
    assert summary["transcribed"] == 0
    assert summary["mentions"] == 0
    assert calls == []


def test_period_scan_also_skips_with_no_keywords(monkeypatch):
    class _Session:
        def close(self):
            pass

    monkeypatch.setattr(pipeline, "SessionLocal", lambda: _Session())
    monkeypatch.setattr(pipeline, "get_notifier", lambda: None)
    monkeypatch.setattr(
        pipeline, "repair_youtube_mentions", lambda s, **k: {"repaired": 0}
    )
    monkeypatch.setattr(pipeline, "_active_youtube_keywords", lambda s, ids=None: [])

    from datetime import datetime, timezone

    summary = pipeline.run_youtube_period_scan(
        datetime(2026, 7, 1, tzinfo=timezone.utc),
        datetime(2026, 7, 2, tzinfo=timezone.utc),
    )
    assert summary["transcribed"] == 0
    assert summary["mentions"] == 0
