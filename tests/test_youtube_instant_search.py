# -*- coding: utf-8 -*-
"""Instant keyword search over already-stored transcripts.

Searching used to pull every transcript in the window — word-level segments and
all — to grep them in Python. The narrowing below pushes that into SQL, so the
guarantee that matters is: it must never hide a transcript the exact matcher
would have matched.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.core.keywords import find_matches, normalize
from app.youtube import pipeline

CASES = [
    ("عمران خان", "ur", "آج عمران خان نے پریس کانفرنس کی"),
    ("علیمہ خان", "ur", "علیمہ خان عدالت پہنچیں"),
    ("child abuse", "en", "a child abuse case was reported today"),
    ("Child Protection and Welfare Bureau", "en",
     "the Child Protection and Welfare Bureau said"),
]


def _prefilter_token(kw: str, lang: str) -> str:
    """The single token the SQL narrowing tests for."""
    return max(normalize(kw, lang).split(), key=len, default="")


@pytest.mark.parametrize("kw,lang,text", CASES)
def test_narrowing_never_hides_a_real_match(kw, lang, text):
    """If the matcher would match, the SQL token test must also pass."""
    assert find_matches(text, [(kw, lang)]), "precondition: this is a real match"
    token = _prefilter_token(kw, lang)
    assert token, "a keyword must yield a token to narrow on"
    assert token in normalize(text, lang), (
        "SQL would exclude a transcript the matcher matches"
    )


def test_narrowing_survives_arabic_urdu_variants():
    """Whisper may emit Arabic forms; SQL folds them the same way we do."""
    kw = "عمران خان"
    arabic_form = "عمران خان".replace("ی", "ي")  # Arabic yeh instead of Urdu
    token = _prefilter_token(kw, "ur")
    folded = normalize(arabic_form, "ur")
    assert token in folded


def _clause(dialect: str, keywords):
    session = MagicMock()
    session.get_bind.return_value.dialect.name = dialect
    return pipeline._keyword_prefilter(session, keywords)


def test_postgres_gets_a_narrowing_clause():
    assert _clause("postgresql", [("عمران خان", "ur")]) is not None


def test_other_dialects_fall_back_to_scanning_everything():
    """Correctness first: no inline folding means no narrowing, not a wrong one."""
    assert _clause("sqlite", [("عمران خان", "ur")]) is None


def test_single_character_keyword_disables_narrowing():
    """Too generic to narrow on — scan rather than risk a bad LIKE."""
    assert _clause("postgresql", [("a", "en")]) is None


def test_multi_word_phrase_narrows_on_a_token_not_the_phrase():
    """Whitespace inside a phrase may differ in the transcript, so never LIKE it."""
    clause = _clause("postgresql", [("child abuse", "en")])
    sql = str(clause.compile(compile_kwargs={"literal_binds": True}))
    assert "child abuse" not in sql, "must not LIKE the whole phrase"
    assert "child" in sql
