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


def _round_ts(seconds: float) -> int:
    return max(0, int(round(seconds)))


def _text_from_segments(segments: list[dict]) -> str:
    return " ".join(
        (s.get("text") or "").strip()
        for s in segments
        if (s.get("text") or "").strip()
    )


def _segments_word_level(segments: list[dict]) -> bool:
    """True when Groq returned mostly one short token per segment."""
    if not segments:
        return False
    sample = segments[: min(40, len(segments))]
    short = sum(1 for s in sample if len((s.get("text") or "").split()) <= 2)
    return short / len(sample) >= 0.7


def _keyword_at_timestamp(
    segments: list[dict],
    start_s: int,
    keyword: str,
    language: str,
    *,
    window: float = 3.0,
) -> bool:
    """Confirm the keyword is spoken within a few seconds of the stored timestamp."""
    parts: list[str] = []
    for seg in segments:
        s = float(seg.get("start", 0) or 0)
        e = float(seg.get("end") or s)
        if e < start_s - window or s > start_s + window + 0.5:
            continue
        parts.append((seg.get("text") or "").strip())
    if not parts:
        return False
    return bool(find_matches(" ".join(parts), [(keyword, language)]))


def hit_is_verified(
    keyword: str,
    language: str,
    start: int,
    excerpt: str,
    segments: list[dict] | None = None,
) -> bool:
    """Timed transcript hit must contain the keyword near the stored timestamp."""
    if int(start or 0) <= 0:
        return False
    text = (excerpt or "").strip()
    if not text or not find_matches(text, [(keyword, language)]):
        return False
    if segments and not _keyword_at_timestamp(segments, int(start), keyword, language):
        return False
    return True


def _keyword_hit_verified(h: KeywordHit, segments: list[dict] | None = None) -> bool:
    return hit_is_verified(h.keyword, h.language, h.start, h.excerpt, segments)


def verified_json_hits(keyword: str, language: str, hits: list) -> list[dict]:
    """Keep only stored JSON hits that pass excerpt verification."""
    out: list[dict] = []
    for h in hits or []:
        if not isinstance(h, dict):
            continue
        if not hit_is_verified(
            keyword, language, int(h.get("start") or 0), h.get("excerpt") or "",
        ):
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
    """Return every exact spoken occurrence per keyword."""
    corpus = _text_from_segments(segments) or text
    matched = find_matches(corpus, keywords)
    if not matched:
        return {}

    out: dict[str, list[KeywordHit]] = {}
    for m in matched:
        hits = _locate_keyword(segments, m.keyword, m.language)
        hits = [h for h in hits if _keyword_hit_verified(h, segments)]
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

    hits: list[KeywordHit] = []
    if _segments_word_level(segments):
        hits.extend(_locate_word_segments(segments, kw_tokens, keyword, language))

    if not hits:
        hits.extend(_locate_in_long_segments(segments, norm_kw, kw_tokens, keyword, language))

    if not hits:
        hits.extend(_locate_token_stream(segments, kw_tokens, keyword, language))

    # Deduplicate near-identical timestamps for the same keyword.
    deduped: list[KeywordHit] = []
    seen_starts: set[int] = set()
    for h in sorted(hits, key=lambda x: x.start):
        if h.start in seen_starts:
            continue
        seen_starts.add(h.start)
        deduped.append(h)
    return deduped


def _locate_word_segments(
    segments: list[dict],
    kw_tokens: list[str],
    keyword: str,
    language: str,
) -> list[KeywordHit]:
    """Match consecutive Groq word segments (one token each) for precise timing."""
    stream: list[tuple[str, float, float | None]] = []
    for seg in segments:
        raw = (seg.get("text") or "").strip()
        if not raw:
            continue
        norm = normalize(raw, language)
        token = norm.split()[0] if norm.split() else ""
        if not token:
            tokens = _WORD_RE.findall(norm)
            token = tokens[0] if tokens else ""
        if not token:
            continue
        start = float(seg.get("start", 0) or 0)
        end = seg.get("end")
        stream.append((token, start, float(end) if end is not None else None))

    n = len(kw_tokens)
    hits: list[KeywordHit] = []
    for i in range(len(stream) - n + 1):
        if not all(stream[i + j][0] == kw_tokens[j] for j in range(n)):
            continue
        start_s = _round_ts(stream[i][1])
        end_raw = stream[i + n - 1][2]
        end_i = _round_ts(end_raw) if end_raw is not None else start_s
        hits.append(
            KeywordHit(
                keyword=keyword,
                language=language,
                start=start_s,
                end=end_i,
                excerpt=_excerpt_around(segments, start_s, language),
            )
        )
    return hits


def _locate_in_long_segments(
    segments: list[dict],
    norm_kw: str,
    kw_tokens: list[str],
    keyword: str,
    language: str,
) -> list[KeywordHit]:
    """Find keyword inside long Whisper segments and interpolate the timestamp."""
    pat = re.compile(exact_pattern(norm_kw))
    hits: list[KeywordHit] = []
    for seg in segments:
        raw = (seg.get("text") or "").strip()
        if not raw:
            continue
        seg_text = normalize(raw, language)
        mt = pat.search(seg_text)
        if not mt:
            continue
        start = float(seg.get("start", 0) or 0)
        end = float(seg.get("end") or start)
        if end <= start:
            start_s = _round_ts(start)
        else:
            pos = mt.start() / max(len(seg_text), 1)
            start_s = _round_ts(start + pos * (end - start))
        end_raw = seg.get("end")
        end_i = _round_ts(float(end_raw)) if end_raw is not None else start_s
        hits.append(
            KeywordHit(
                keyword=keyword,
                language=language,
                start=start_s,
                end=end_i,
                excerpt=raw[:240],
            )
        )
    return hits


def _locate_token_stream(
    segments: list[dict],
    kw_tokens: list[str],
    keyword: str,
    language: str,
) -> list[KeywordHit]:
    """Fallback: tokenize each segment and walk the stream."""
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
        if not all(stream[i + j][0] == kw_tokens[j] for j in range(n)):
            continue
        start_s = _round_ts(stream[i][1])
        end_s = stream[i + n - 1][2]
        end_i = _round_ts(end_s) if end_s is not None else start_s
        hits.append(
            KeywordHit(
                keyword=keyword,
                language=language,
                start=start_s,
                end=end_i,
                excerpt=_excerpt_around(segments, start_s, language),
            )
        )
    return hits


def _excerpt_around(segments: list[dict], start_s: int, language: str, radius: float = 6.0) -> str:
    parts = []
    for seg in segments:
        s = float(seg.get("start", 0) or 0)
        e = float(seg.get("end") or s)
        if e < start_s - radius or s > start_s + radius:
            continue
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
