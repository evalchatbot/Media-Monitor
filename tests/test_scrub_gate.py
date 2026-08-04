# -*- coding: utf-8 -*-
"""The home page used to run a full `SELECT * FROM mentions` on every load via
_scrub_deleted_keywords. That scan only has work to do when a keyword was
deleted, so it must be skipped while the active keyword set is unchanged —
otherwise it slows and churns the mentions table as data grows.
"""
from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.models import Base, Keyword, Mention


def _sqlite():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False},
                      poolclass=StaticPool)
    Base.metadata.create_all(e)
    return e


def test_scrub_skips_until_keyword_set_changes(tmp_path, monkeypatch):
    import app.main as main
    from config import settings

    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage", raising=False)
    engine = _sqlite()
    with Session(engine) as s:
        s.add(Keyword(text="flood", module="newspaper", active=True))
        s.add(Mention(module="newspaper", external_id="n1", source="Dawn",
                      title="t", url="u", matched_keywords=["flood"]))
        s.commit()

        first = main._scrub_deleted_keywords(s)
        assert not first.get("skipped"), "first run must actually scrub (no fingerprint yet)"

        second = main._scrub_deleted_keywords(s)
        assert second.get("skipped"), "unchanged keyword set must skip the full scan"

        third = main._scrub_deleted_keywords(s)
        assert third.get("skipped"), "still skipped while nothing changed"

        # Remove the keyword ROW (the scrub keys on row existence, not the
        # active flag) -> the set changes -> scrub must run again and drop the
        # now-orphaned mention.
        kw = s.execute(select(Keyword)).scalar_one()
        s.delete(kw)
        s.commit()

        after_delete = main._scrub_deleted_keywords(s)
        assert not after_delete.get("skipped"), "a keyword change must re-run the scrub"
        assert s.execute(select(Mention)).first() is None, "orphaned mention removed"

        # And it settles back to skipping.
        assert main._scrub_deleted_keywords(s).get("skipped")
