# -*- coding: utf-8 -*-
"""Adding a keyword must match stored transcripts even mid-scan.

The scan runner permits one subprocess at a time and refuses new work while
busy. Routing the instant match through it meant that during a bulletin
backlog — most of the time — the request was dropped and the new keyword
matched nothing until some later scan happened to pick it up.
"""
from __future__ import annotations

import threading

from app import main as app_main


def _wait_for(pred, timeout=5.0):
    done = threading.Event()

    def poll():
        import time

        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if pred():
                done.set()
                return
            time.sleep(0.02)

    t = threading.Thread(target=poll)
    t.start()
    t.join()
    return done.is_set()


def test_match_runs_even_though_the_scan_runner_is_busy(monkeypatch):
    called: list[list[int]] = []

    def fake_match(keyword_ids=None, **kw):
        called.append(list(keyword_ids or []))
        return {"mentions": 0}

    # The runner refuses work while a scan is in flight.
    monkeypatch.setattr(
        app_main.yt_scan_runner, "start_scan", lambda *a, **k: False
    )
    import app.youtube.pipeline as pipeline

    monkeypatch.setattr(pipeline, "run_quick_youtube_match", fake_match)

    app_main.start_instant_youtube_match([7, 8])

    assert _wait_for(lambda: called == [[7, 8]]), (
        "the match must not depend on the scan runner being free"
    )


def test_no_keywords_starts_nothing(monkeypatch):
    called: list[int] = []
    import app.youtube.pipeline as pipeline

    monkeypatch.setattr(
        pipeline, "run_quick_youtube_match",
        lambda *a, **k: called.append(1),
    )
    app_main.start_instant_youtube_match([])
    assert called == []


def test_a_failing_match_does_not_take_the_request_down(monkeypatch):
    import app.youtube.pipeline as pipeline

    def boom(*a, **k):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(pipeline, "run_quick_youtube_match", boom)
    app_main.start_instant_youtube_match([1])  # must not raise
