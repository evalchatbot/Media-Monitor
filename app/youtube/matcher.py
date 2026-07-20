"""Exact keyword location inside timestamped transcript segments."""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.keywords import exact_pattern, find_matches, normalize

_WORD_RE = re.compile(r"\w+", re.UNICODE)


@dataclass
class KeywordHit:
    keyword: str
    language: str
    start: int
    end: int | None
    excerpt: str
    confidence: float | None = None


def hit_is_verified(keyword: str, language: str, start: int, excerpt: str) -> bool:
    """Timed transcript hit must contain the exact keyword in its excerpt."""
    if int(start or 0) <= 0:
        return False
    text = (excerpt or "").strip()
    if not text:
        return False
    return bool(find_matches(text, [(keyword, language)]))


def _keyword_hit_verified(h: KeywordHit) -> bool:
    return hit_is_verified(h.keyword, h.language, h.start, h.excerpt)


def verified_json_hits(keyword: str, language: str, hits: list) -> list[dict]:
    """Keep only stored JSON hits that pass verification."""
    out: list[dict] = []
    for h in hits or []:
        if not isinstance(h, dict):
            continue
        if not hit_is_verified(keyword, language, int(h.get("start") or 0), h.get("excerpt") or ""):
            continue
        out.append(h)
    return out


def prune_stored_hits(
    keyword_hits: dict,
    keyword_langs: dict[str, str],
) -> tuple[list[str], dict]:
    """Drop unverified keywords/hits from a stored mention row."""
    clean: dict = {}
    labels: list[str] = []
    for kw, hits in (keyword_hits or {}).items():
        lang = keyword_langs.get(kw, "ur")
        verified = verified_json_hits(kw, lang, hits if isinstance(hits, list) else [])
        if verified:
            labels.append(kw)
            clean[kw] = verified
    return sorted(labels), clean


def mention_verified_keywords(
    matched_keywords: list | None,
    keyword_hits: dict | None,
    keyword_langs: dict[str, str],
    *,
    active_fold: dict[str, str] | None = None,
) -> list[str]:
    """Keywords on a mention that still have verified transcript hits."""
    seen: set[str] = set()
    out: list[str] = []
    for kw in matched_keywords or []:
        fold = (kw or "").casefold()
        if not kw or fold in seen:
            continue
        if active_fold is not None and fold not in active_fold:
            continue
        lang = keyword_langs.get(kw, "ur")
        if verified_json_hits(kw, lang, (keyword_hits or {}).get(kw) or []):
            seen.add(fold)
            canonical = (active_fold or {}).get(fold, kw)
            out.append(canonical)
    return out


def find_all_hits(
    text: str,
    segments: list[dict],
    keywords: list[tuple[str, str]],
) -> dict[str, list[KeywordHit]]:
    """Return every exact spoken occurrence per keyword.

    keywords: list of (text, language)
    """
    matched = find_matches(text, keywords)
    if not matched:
        # Title/description-only matches still useful later; no timestamps.
        return {}

    out: dict[str, list[KeywordHit]] = {}
    for m in matched:
        hits = _locate_keyword(segments, m.keyword, m.language)
        hits = [h for h in hits if _keyword_hit_verified(h)]
        if hits:
            out[m.keyword] = hits
    return out


def find_keyword_second(
    segments: list[dict], keyword: str, language: str = "en"
) -> int | None:
    hits = _locate_keyword(segments, keyword, language)
    return hits[0].start if hits else None


def _locate_keyword(segments: list[dict], keyword: str, language: str) -> list[KeywordHit]:
    if not segments:
        return []
    norm_kw = normalize(keyword, language)
    kw_tokens = [t for t in norm_kw.split() if t]
    if not kw_tokens:
        return []
    n = len(kw_tokens)

    stream: list[tuple[str, float, float | None]] = []
    for seg in segments:
        start = float(seg.get("start", 0) or 0)
        end = seg.get("end")
        end_f = float(end) if end is not None else None
        for w in _WORD_RE.findall(normalize(seg.get("text") or "", language)):
            stream.append((w, start, end_f))

    hits: list[KeywordHit] = []
    for i in range(len(stream) - n + 1):
        if all(stream[i + j][0] == kw_tokens[j] for j in range(n)):
            start_s = int(stream[i][1])
            end_s = stream[i + n - 1][2]
            end_i = int(end_s) if end_s is not None else start_s
            excerpt = _excerpt_around(segments, start_s, language)
            hit = KeywordHit(
                keyword=keyword,
                language=language,
                start=start_s,
                end=end_i,
                excerpt=excerpt,
            )
            if _keyword_hit_verified(hit):
                hits.append(hit)

    if hits:
        return hits

    # Fallback: whole keyword inside a segment's text.
    pat = re.compile(exact_pattern(norm_kw))
    for seg in segments:
        seg_text = normalize(seg.get("text") or "", language)
        if pat.search(seg_text):
            start_s = int(float(seg.get("start", 0) or 0))
            end_raw = seg.get("end")
            end_i = int(float(end_raw)) if end_raw is not None else start_s
            hit = KeywordHit(
                keyword=keyword,
                language=language,
                start=start_s,
                end=end_i,
                excerpt=(seg.get("text") or "").strip()[:240],
            )
            if _keyword_hit_verified(hit):
                hits.append(hit)
    return hits


def _excerpt_around(segments: list[dict], start_s: int, language: str, radius: float = 8.0) -> str:
    parts = []
    for seg in segments:
        s = float(seg.get("start", 0) or 0)
        if abs(s - start_s) <= radius:
            t = (seg.get("text") or "").strip()
            if t:
                parts.append(t)
    text = " ".join(parts).strip()
    return text[:240] if text else ""


def hits_to_json(hits_map: dict[str, list[KeywordHit]]) -> dict:
    out: dict = {}
    for kw, hits in hits_map.items():
        out[kw] = [
            {
                "start": h.start,
                "end": h.end,
                "excerpt": h.excerpt,
                "confidence": h.confidence,
                "screenshot": None,
            }
            for h in hits
        ]
    return out
