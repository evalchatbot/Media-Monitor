# -*- coding: utf-8 -*-
"""Instant per-keyword search over stored article text.

The SQL prefilter that makes this fast must never drop an article the exact
matcher would have matched — a false negative is a missed mention.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.keywords import find_matches, normalize
from app.db.models import ArticleCache, Base
from app.newspaper import pipeline


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_prefilter_returns_none_on_sqlite_so_full_corpus_is_used(db):
    """The SQL fold is Postgres-only; elsewhere it must decline, not mis-filter."""
    assert pipeline._articles_containing(db, [("عمران خان", "ur")]) is None


# The token strategy the SQL applies, mirrored so we can assert the invariant
# without a live Postgres.
def _strong_tokens(kw: str, lang: str) -> list[str]:
    toks = [t for t in normalize(kw, lang).split() if len(t) >= 2]
    return [t for t in toks if len(t) >= 4] or ([max(toks, key=len)] if toks else [])


@pytest.mark.parametrize("kw,lang,text", [
    ("عمران خان", "ur", "آج عمران خان نے کہا"),
    ("child abuse", "en", "a child abuse case in Lahore"),
    ("شہباز شریف", "ur", "وزیراعظم شہباز شریف نے"),
    ("فیلڈ مارشل", "ur", "فیلڈ مارشل عاصم منیر"),
])
def test_every_strong_token_is_present_in_a_real_match(kw, lang, text):
    """If the exact matcher matches, each token the SQL requires must be in the
    text — otherwise the AND-of-tokens prefilter would exclude a real match."""
    assert find_matches(text, [(kw, lang)]), "precondition: a real match"
    folded = normalize(text, lang)
    for tok in _strong_tokens(kw, lang):
        assert tok in folded, f"prefilter token {tok!r} missing — would drop this match"


def test_strong_tokens_skip_short_common_words():
    """3-char tokens like خان are dropped when a longer token exists (they blow
    up the trigram scan for no selectivity)."""
    assert _strong_tokens("عمران خان", "ur") == ["عمران"]


def test_strong_tokens_keep_the_longest_when_all_short():
    """A keyword made only of short words still narrows on something."""
    toks = _strong_tokens("جنگ", "ur")
    assert toks == ["جنگ"]


def test_run_quick_match_still_works_via_corpus_fallback(db, monkeypatch):
    """On SQLite (no prefilter) the whole instant-match path still finds hits."""
    from app.db.models import Keyword

    base = datetime.now(timezone.utc)
    db.add(Keyword(text="سیلاب", language="ur", module="newspaper", active=True))
    for i in range(3):
        db.add(ArticleCache(
            module="newspaper", external_id=f"a{i}", source="Dawn",
            title=f"خبر {i}", url=f"http://x/{i}",
            body=("ملک میں سیلاب کی صورتحال" if i < 2 else "کھیل کی خبر"),
            fetched_at=base - timedelta(hours=i),
        ))
    db.commit()

    class _Notifier:
        def send(self, *a, **k):
            return True

    monkeypatch.setattr(pipeline, "SessionLocal", lambda: db)
    monkeypatch.setattr(pipeline, "get_notifier", lambda: _Notifier())
    # e-paper matching is exercised elsewhere; stub it here.
    monkeypatch.setattr("app.epaper.pipeline.match_stored_pages",
                        lambda *a, **k: None)

    summary = pipeline.run_quick_match(keyword_ids=None)
    assert summary["mentions"] == 2, "both سیلاب articles become mentions, sports one does not"
