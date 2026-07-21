# -*- coding: utf-8 -*-
"""Adding a YouTube keyword through the real form triggers the match itself.

This is the automation, not a helper anyone runs by hand: posting the add form
must schedule the match against already-stored transcripts, for BOTH buttons on
the page, without any further action.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app import main as app_main


@pytest.fixture
def client(monkeypatch):
    """Exercise the route with the match trigger recorded instead of run."""
    started: list[list[int]] = []
    monkeypatch.setattr(
        app_main, "start_instant_youtube_match",
        lambda ids: started.append(list(ids)),
    )
    monkeypatch.setattr(
        app_main.keyword_scan_queue, "enqueue_many", lambda *a, **k: None
    )
    with TestClient(app_main.app) as c:
        yield c, started


def _post(client, texts, scan):
    return client.post(
        "/ui/keywords/batch",
        data={"texts": texts, "language": "en", "module": "youtube", "scan": scan},
        follow_redirects=False,
    )


def test_add_to_watchlist_button_triggers_the_match(client):
    c, started = client
    r = _post(c, "عمران خان", "1")
    assert r.status_code == 303
    assert started and started[0], "adding a keyword must schedule the match"


def test_add_only_button_also_triggers_the_match(client):
    """Matching cached transcripts is free, so it must not need the scan button."""
    c, started = client
    r = _post(c, "شہباز شریف", "0")
    assert r.status_code == 303
    assert started and started[0], (
        "'Add only' saved the keyword without ever matching it"
    )


def test_every_keyword_in_a_batch_is_matched(client):
    c, started = client
    _post(c, "جنگ\nآتش بازی\nسیلاب", "1")
    assert started, "batch add must schedule a match"
    assert len(started[0]) == 3, "all keywords in the batch, not just the first"


def test_blank_submission_schedules_nothing(client):
    c, started = client
    _post(c, "   ", "1")
    assert started == []
