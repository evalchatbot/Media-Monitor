# -*- coding: utf-8 -*-
"""Adding a keyword ONLY saves it to the watchlist — it must not scan.

Live-only model: scraping/transcription happens exclusively when the user
clicks "Search live results". Adding a keyword therefore must not enqueue a
scan, start an instant match, or launch any subprocess — no background work and
no cost. The AJAX add returns the created chip(s) with scanning=False.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main as app_main


@pytest.fixture
def client(monkeypatch):
    """Record any scan/match trigger so the test can assert none fire."""
    triggered: list[str] = []
    monkeypatch.setattr(
        app_main, "start_instant_youtube_match",
        lambda ids: triggered.append(f"yt_match:{list(ids)}"),
    )
    monkeypatch.setattr(
        app_main.keyword_scan_queue, "enqueue_many",
        lambda *a, **k: triggered.append("enqueue_many"),
    )
    monkeypatch.setattr(
        app_main.scan_manager, "start_scan",
        lambda *a, **k: triggered.append("news_scan") or True,
    )
    monkeypatch.setattr(
        app_main.yt_scan_runner, "start_scan",
        lambda *a, **k: triggered.append("yt_scan") or True,
    )
    with TestClient(app_main.app) as c:
        yield c, triggered


def _post(client, texts, module="youtube", accept=None):
    headers = {"Accept": accept} if accept else {}
    return client.post(
        "/ui/keywords/batch",
        data={"texts": texts, "language": "en", "module": module, "scan": "1"},
        headers=headers,
        follow_redirects=False,
    )


def test_plain_add_saves_without_triggering_any_scan(client):
    c, triggered = client
    r = _post(c, "عمران خان")
    assert r.status_code == 303
    assert triggered == [], f"adding must not scan/match, but fired: {triggered}"


def test_ajax_add_returns_chip_and_does_not_scan(client):
    c, triggered = client
    r = _post(c, "شہباز شریف", accept="application/json")
    assert r.status_code == 200
    payload = r.json()
    assert payload["ok"] is True
    assert payload["scanning"] is False
    assert payload["created"] and payload["created"][0]["text"] == "شہباز شریف"
    assert triggered == [], f"AJAX add must not scan/match, but fired: {triggered}"


def test_newspaper_add_also_does_not_scan(client):
    c, triggered = client
    r = _post(c, "monsoon", module="newspaper", accept="application/json")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert triggered == [], f"newspaper add must not scan, but fired: {triggered}"


def test_blank_submission_saves_nothing_and_scans_nothing(client):
    c, triggered = client
    _post(c, "   ")
    assert triggered == []
