"""Keyword detection: exact whole-word matching, English and Urdu.

Matching strategy
-----------------
1. Normalize both text and keyword (lowercase for EN; strip Urdu diacritics
   and unify Arabic/Urdu code-point variants for UR).
2. Exact whole-word / whole-phrase match only — no fuzzy edits, no
   inflection suffixes. "BLA" will not match "black"; "aleema khan" only
   matches that phrase as written (after normalization).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Arabic/Urdu character normalization: map visually-equivalent code points.
_UR_CHAR_MAP = {
    "ي": "ی",  # Arabic yeh -> Urdu yeh
    "ك": "ک",  # Arabic kaf -> Urdu keheh
    "ة": "ہ",  # teh marbuta -> heh goal
    "أ": "ا",  # alef with hamza above -> alef
    "إ": "ا",  # alef with hamza below -> alef
    "آ": "ا",  # alef with madda -> alef
}
# Combining marks (harakat/diacritics) to strip for UR.
_UR_DIACRITICS = re.compile(r"[ً-ْٰ]")


@dataclass(frozen=True)
class KeywordMatch:
    keyword: str
    language: str
    exact: bool


def normalize(text: str, language: str = "en") -> str:
    text = unicodedata.normalize("NFC", text)
    if language == "ur":
        for src, dst in _UR_CHAR_MAP.items():
            text = text.replace(src, dst)
        text = _UR_DIACRITICS.sub("", text)
        return re.sub(r"\s+", " ", text).strip()
    return re.sub(r"\s+", " ", text).lower().strip()


def exact_pattern(norm_kw: str) -> str:
    """Word-boundary regex for an exact keyword / phrase (no inflection)."""
    # Allow flexible whitespace between phrase words after normalization.
    parts = [re.escape(p) for p in norm_kw.split() if p]
    if not parts:
        return r"(?!)"  # never matches
    body = r"\s+".join(parts)
    return rf"(?<!\w){body}(?!\w)"


def fuzzy_budget(norm_kw: str, cap: int = 2) -> int:
    """Kept for API compatibility; exact-only matching always returns 0."""
    return 0


def find_matches(
    text: str,
    keywords: list[tuple[str, str]],
    max_distance: int = 0,
) -> list[KeywordMatch]:
    """Return exact keyword matches found in `text`.

    Parameters
    ----------
    text : str
        The article body / transcript segment to search.
    keywords : list[(keyword_text, language)]
        Active keywords with their language tag.
    max_distance : int
        Ignored — matching is exact-only (kept for call-site compatibility).
    """
    matches: list[KeywordMatch] = []
    norm_cache: dict[str, str] = {}

    for kw_text, lang in keywords:
        if lang not in norm_cache:
            norm_cache[lang] = normalize(text, lang)
        norm_text = norm_cache[lang]
        norm_kw = normalize(kw_text, lang)
        if not norm_kw:
            continue

        if re.search(exact_pattern(norm_kw), norm_text):
            matches.append(KeywordMatch(kw_text, lang, exact=True))

    return matches
