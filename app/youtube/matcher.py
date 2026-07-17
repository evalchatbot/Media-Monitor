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
            hits.append(
                KeywordHit(
                    keyword=keyword,
                    language=language,
                    start=start_s,
                    end=end_i,
                    excerpt=excerpt,
                )
            )

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
            hits.append(
                KeywordHit(
                    keyword=keyword,
                    language=language,
                    start=start_s,
                    end=end_i,
                    excerpt=(seg.get("text") or "").strip()[:240],
                )
            )
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
