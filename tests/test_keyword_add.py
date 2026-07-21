# -*- coding: utf-8 -*-
"""Adding watchlist keywords is one round trip, not one per keyword.

The database is remote, so a SELECT and COMMIT per keyword was most of the
delay a user felt when adding terms.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.db.models import Base, Keyword
from app.main import _upsert_watch_keywords


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.info["engine"] = engine
        yield s


def _round_trips(session, fn):
    engine = session.info["engine"]
    n = [0]
    handler = lambda *a: n.__setitem__(0, n[0] + 1)  # noqa: E731
    event.listen(engine, "before_cursor_execute", handler)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", handler)
    return n[0]


def test_adding_many_keywords_does_not_scale_round_trips(db):
    three = _round_trips(db, lambda: _upsert_watch_keywords(
        db, ["alpha", "beta", "gamma"], "en", module="youtube"))
    six = _round_trips(db, lambda: _upsert_watch_keywords(
        db, ["a1", "a2", "a3", "a4", "a5", "a6"], "en", module="youtube"))
    assert six <= three + 3, "cost must not grow one commit per keyword"


def test_all_keywords_are_persisted(db):
    _upsert_watch_keywords(db, ["one", "two", "three"], "en", module="youtube")
    got = db.execute(
        select(Keyword.text).where(Keyword.module == "youtube")
    ).scalars().all()
    assert sorted(got) == ["one", "three", "two"]


def test_existing_keyword_is_reactivated_not_duplicated(db):
    _upsert_watch_keywords(db, ["spain"], "en", module="youtube")
    kw = db.execute(select(Keyword).where(Keyword.text == "spain")).scalar_one()
    kw.active = False
    db.commit()

    _upsert_watch_keywords(db, ["spain"], "en", module="youtube")

    rows = db.execute(select(Keyword).where(Keyword.text == "spain")).scalars().all()
    assert len(rows) == 1
    assert rows[0].active is True


def test_case_insensitive_match_avoids_duplicates(db):
    _upsert_watch_keywords(db, ["Sarah Ahmed"], "en", module="youtube")
    _upsert_watch_keywords(db, ["sarah ahmed"], "en", module="youtube")
    rows = db.execute(
        select(Keyword).where(Keyword.module == "youtube")
    ).scalars().all()
    assert len(rows) == 1


def test_blank_input_is_ignored(db):
    assert _upsert_watch_keywords(db, ["  ", ""], "en", module="youtube") == []


def test_urdu_keyword_keeps_its_language(db):
    """An Urdu term tagged English loses Urdu letter folding when matching."""
    _upsert_watch_keywords(db, ["سوشل میڈیا"], "ur", module="youtube")
    kw = db.execute(select(Keyword)).scalar_one()
    assert kw.language == "ur"
