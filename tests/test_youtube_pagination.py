# -*- coding: utf-8 -*-
"""YouTube results paginate: 25 at a time with a "Show next" control."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import main as app_main
from app.db.models import Base, Keyword, Mention


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(Keyword(text="test", language="en", module="youtube", active=True))
        base = datetime(2026, 7, 21, tzinfo=timezone.utc)
        for i in range(60):
            s.add(Mention(
                module="youtube", external_id=f"vid{i}", source="City42",
                section="9 AM · 2026-07-21", title=f"bulletin {i}",
                url=f"https://youtu.be/vid{i}",
                matched_keywords=["test"],
                keyword_hits={"test": [{"start": 5, "end": 7, "excerpt": "a test here"}]},
                keyword_media={},
                published_at=(base - timedelta(hours=i)).replace(tzinfo=None),
            ))
        s.commit()
        yield s


def _render(db, max_results):
    html_out, _ = app_main._youtube_results_html(
        db, keyword="", keyword_ids=None, results_scanning=False,
        max_results=max_results,
    )
    return html_out


def test_first_page_shows_25_and_a_show_more_button(db):
    html_out = _render(db, 25)
    assert html_out.count('class="det') == 25, "first page is one result_limit"
    assert 'id="yt-more"' in html_out
    assert 'data-next="50"' in html_out, "button asks for the next page"
    assert "Show next 25" in html_out


def test_second_page_shows_50(db):
    html_out = _render(db, 50)
    assert html_out.count('class="det') == 50
    assert 'data-next="60"' in html_out
    assert "Show next 10" in html_out, "last step is only what remains"


def test_final_page_has_no_button(db):
    html_out = _render(db, 1000)
    assert html_out.count('class="det') == 60
    assert 'id="yt-more"' not in html_out
    assert "Showing 60 of 60" in html_out


def test_default_without_max_is_one_page(db):
    html_out = _render(db, None)
    assert html_out.count('class="det') == 25
    assert 'id="yt-more"' in html_out
