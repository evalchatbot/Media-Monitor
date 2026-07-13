"""Keyword detection: exact + fuzzy matching, English and Urdu.

Matching strategy
-----------------
1. Normalize both text and keyword (lowercase for EN; strip Urdu diacritics
   and unify Arabic/Urdu code-point variants for UR).
2. Exact substring check on the normalized forms (fast path).
3. Fuzzy fallback: slide over the text's word n-grams and accept a match when
   Levenshtein distance <= 2 (rapidfuzz), catching misspellings/mispronunciations.

Urdu caveat (flagged): reliable Urdu fuzzy matching needs a curated test set;
normalization here covers the common variants but should be tuned against real
broadcast/print data before trusting the fuzzy path in production.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from rapidfuzz.distance import Levenshtein

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
        # Collapse whitespace; keep script intact.
        return re.sub(r"\s+", " ", text).strip()
    # English / latin
    return re.sub(r"\s+", " ", text).lower().strip()


def _tokenize(text: str) -> list[str]:
    return re.findall(r"\w+", text, flags=re.UNICODE)


def _fuzzy_contains(haystack_tokens: list[str], needle: str, max_distance: int) -> bool:
    """True if any word n-gram of `haystack_tokens` is within edit distance."""
    needle_word_count = len(needle.split())
    n = max(1, needle_word_count)
    for i in range(len(haystack_tokens) - n + 1):
        window = " ".join(haystack_tokens[i : i + n])
        # Length guard: skip windows that cannot be within max_distance.
        if abs(len(window) - len(needle)) > max_distance:
            continue
        if Levenshtein.distance(window, needle, score_cutoff=max_distance) <= max_distance:
            return True
    return False


# Inflectional suffixes allowed on an exact match so "protest" also catches
# "protests/protested/protesting" — without the substring noise that made
# "rape" hit "grape"/"scrape". Only applied to keywords >= 4 chars: on shorter
# ones a bounded suffix still over-matches (e.g. "war" -> "ward"), so those match
# as whole words only.
_INFLECT = "(?:s|es|ed|d|ing)?"


def exact_pattern(norm_kw: str) -> str:
    """Word-boundary regex for `norm_kw` (whole word + light inflection)."""
    suffix = _INFLECT if len(norm_kw.replace(" ", "")) >= 4 else ""
    return rf"(?<!\w){re.escape(norm_kw)}{suffix}(?!\w)"


def fuzzy_budget(norm_kw: str, cap: int = 2) -> int:
    """Edit-distance budget scaled to keyword length.

    Short keywords get 0 so a 2-edit budget can't match dozens of unrelated
    common words (rape ~ rate/rare/race/rope/ripe). 5-7 chars get 1, 8+ get 2.
    """
    n = len(norm_kw.replace(" ", ""))
    if n <= 4:
        return 0
    if n <= 7:
        return 1
    return min(2, cap)


def find_matches(
    text: str,
    keywords: list[tuple[str, str]],
    max_distance: int = 2,
) -> list[KeywordMatch]:
    """Return keyword matches found in `text`.

    Parameters
    ----------
    text : str
        The article body / transcript segment to search.
    keywords : list[(keyword_text, language)]
        Active keywords with their language tag.
    max_distance : int
        Max Levenshtein distance for the fuzzy fallback (default 2).
    """
    matches: list[KeywordMatch] = []
    # Cache normalized text per language so we don't redo work per keyword.
    norm_cache: dict[str, tuple[str, list[str]]] = {}

    for kw_text, lang in keywords:
        if lang not in norm_cache:
            norm_text = normalize(text, lang)
            norm_cache[lang] = (norm_text, _tokenize(norm_text))
        norm_text, tokens = norm_cache[lang]
        norm_kw = normalize(kw_text, lang)
        if not norm_kw:
            continue

        # Whole-word match (not substring) so "rape" no longer matches inside
        # "grape"/"scrape"; light inflection keeps "raped"/"rapes".
        if re.search(exact_pattern(norm_kw), norm_text):
            matches.append(KeywordMatch(kw_text, lang, exact=True))
            continue
        # Fuzzy fallback, but only with a length-scaled edit budget (0 for short
        # keywords) so it can't turn "rape" into rate/rare/race/rope/ripe.
        budget = fuzzy_budget(norm_kw, max_distance)
        if budget and _fuzzy_contains(tokens, norm_kw, budget):
            matches.append(KeywordMatch(kw_text, lang, exact=False))

    return matches
