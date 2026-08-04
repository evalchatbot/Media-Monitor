# -*- coding: utf-8 -*-
"""Removing a newspaper or YouTube channel must make it disappear everywhere:
drop out of the picker, stop being scraped, and clear its stored result cards.
Also guards that the three new e-papers are registered.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import sources_probe
from app.db.models import Base, Mention
from app.epaper import sources as epaper_sources
from app.scrapers import sites


@pytest.fixture
def hidden_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(sources_probe, "_HIDDEN_PATH", tmp_path / "hidden_papers.json",
                        raising=False)
    return tmp_path


def test_hide_unhide_roundtrip_is_case_insensitive(hidden_tmp):
    assert sources_probe.hidden_papers() == set()
    sources_probe.hide_paper("Dawn")
    assert sources_probe.is_hidden("dawn")
    assert sources_probe.is_hidden("  DAWN ")
    sources_probe.unhide_paper("DAWN")
    assert not sources_probe.is_hidden("Dawn")


def test_removed_paper_drops_from_scrapers_and_epaper(hidden_tmp):
    assert any(s.name == "dawn" for s in sites.build_scrapers())
    assert "jang" in epaper_sources.enabled_slugs()

    sources_probe.hide_paper("Dawn")     # a website scraper
    sources_probe.hide_paper("Jang")     # an e-paper source

    assert not any(s.name == "dawn" for s in sites.build_scrapers())
    assert "jang" not in epaper_sources.enabled_slugs()

    sources_probe.unhide_paper("Jang")
    assert "jang" in epaper_sources.enabled_slugs()


def test_new_epapers_are_registered():
    for slug, name in [("jehanpakistan", "Jehan Pakistan"), ("dunya", "Dunya"),
                       ("jang", "Jang")]:
        assert slug in epaper_sources.SOURCES, slug
        assert epaper_sources.SOURCES[slug][0] == name
    assert "dunya" not in epaper_sources.UNSUPPORTED


def test_purge_source_results_removes_only_that_source():
    import app.main as main

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add_all([
            Mention(module="youtube", external_id="v1", source="Geo News", title="a", url="x"),
            Mention(module="youtube", external_id="v2", source="ARY News", title="b", url="y"),
            Mention(module="newspaper", external_id="n1", source="Dawn", title="c", url="z"),
            Mention(module="epaper", external_id="e1", source="Dawn", title="d", url="w"),
        ])
        s.commit()

        # Removing a channel clears only its youtube cards.
        assert main._purge_source_results(s, "Geo News", ("youtube",)) == 1
        assert {m.source for m in s.execute(select(Mention)).scalars()} == {
            "ARY News", "Dawn", "Dawn"}

        # Removing a paper clears its newspaper AND e-paper cards, nothing else.
        assert main._purge_source_results(s, "Dawn", ("newspaper", "epaper")) == 2
        assert {m.source for m in s.execute(select(Mention)).scalars()} == {"ARY News"}

        # A name with no rows is a no-op.
        assert main._purge_source_results(s, "Nonexistent", ("youtube",)) == 0
