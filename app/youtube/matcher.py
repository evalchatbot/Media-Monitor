"""Exact keyword location inside timestamped transcript segments."""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.keywords import exact_pattern, find_matches, normalize

_WORD_RE = re.compile(r"\w+", re.UNICODE)

# Two hits of the same keyword closer than this are the same spoken utterance
# (re-found via an overlapping segment or a chunk boundary), not two mentions.
_MIN_HIT_GAP_S = 3.0

# Whisper's own reliability thresholds. Speech it reports with this little
# confidence — or over audio it flags as non-speech — is where it invents
# words, so a keyword "found" there is a hallucination, not a mention.
_NO_SPEECH_MAX = 0.6
_AVG_LOGPROB_MIN = -1.0


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


def segment_is_reliable(seg: dict) -> bool:
    """False when Whisper itself signalled this span is not dependable speech.

    Segments transcribed before quality was recorded carry neither field; they
    are trusted so existing transcripts keep working.
    """
    ns = seg.get("no_speech_prob")
    if isinstance(ns, (int, float)) and ns > _NO_SPEECH_MAX:
        return False
    lp = seg.get("avg_logprob")
    if isinstance(lp, (int, float)) and lp < _AVG_LOGPROB_MIN:
        return False
    return True


def _reliable_segments(segments: list[dict]) -> list[dict]:
    return [s for s in segments or [] if segment_is_reliable(s)]


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
        if e < start_s - window or s > start_s + window:
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
    # 0 is a legitimate timestamp — headlines are read at 0:00 — so only a
    # missing or negative offset is invalid.
    if start is None or int(start) < 0:
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
    """Keep only stored JSON hits that pass excerpt verification.

    Also collapses same-utterance duplicates, so rows written before the
    location fix stop showing repeats without waiting for a re-match.
    """
    out: list[dict] = []
    for h in sorted(
        (x for x in (hits or []) if isinstance(x, dict)),
        key=lambda x: int(x.get("start") or 0),
    ):
        start = int(h.get("start") or 0)
        if not hit_is_verified(keyword, language, start, h.get("excerpt") or ""):
            continue
        if out and start - int(out[-1].get("start") or 0) < _MIN_HIT_GAP_S:
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
    if segments:
        # Search only speech Whisper stands behind. Falling back to `text` here
        # would put the discarded low-confidence spans straight back in.
        trusted = _reliable_segments(segments)
        corpus = _text_from_segments(trusted)
    else:
        trusted = []
        corpus = text

    matched = find_matches(corpus, keywords)
    if not matched:
        return {}

    out: dict[str, list[KeywordHit]] = {}
    for m in matched:
        hits = _locate_keyword(trusted, m.keyword, m.language)
        hits = [h for h in hits if _keyword_hit_verified(h, trusted)]
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
    word_level = _segments_word_level(segments)
    if word_level:
        # Word-level Groq output: every token carries its own exact timing, so
        # walking the token stream is both the most precise path and immune to
        # punctuation glued onto a word ("خان،").
        hits.extend(_locate_token_stream(segments, kw_tokens, keyword, language))

    if not hits:
        hits.extend(_locate_in_long_segments(segments, norm_kw, kw_tokens, keyword, language))

    if not hits and not word_level:
        hits.extend(_locate_token_stream(segments, kw_tokens, keyword, language))

    # Collapse re-finds of one utterance; keep genuinely separate occurrences.
    deduped: list[KeywordHit] = []
    for h in sorted(hits, key=lambda x: x.start):
        if deduped and h.start - deduped[-1].start < _MIN_HIT_GAP_S:
            continue
        deduped.append(h)

    loops = _loop_ranges(segments, kw_tokens, language)
    if loops:
        deduped = [
            h for h in deduped
            if not any(lo - 1 <= h.start <= hi + 1 for lo, hi in loops)
        ]
    return deduped


def _loop_ranges(
    segments: list[dict], kw_tokens: list[str], language: str
) -> list[tuple[float, float]]:
    """Time spans where the phrase repeats back-to-back.

    A decoder stuck in a loop emits "<phrase> <phrase> <phrase>" over silence or
    noise. Nobody reads the news that way, so those spans are dropped — while
    real repeat mentions elsewhere in the bulletin are left alone.
    """
    n = len(kw_tokens)
    stream: list[tuple[str, float]] = []
    for seg in segments or []:
        start = float(seg.get("start", 0) or 0)
        for w in _WORD_RE.findall(normalize(seg.get("text") or "", language)):
            stream.append((w, start))

    idxs = [
        i
        for i in range(len(stream) - n + 1)
        if all(stream[i + j][0] == kw_tokens[j] for j in range(n))
    ]

    ranges: list[tuple[float, float]] = []
    run: list[int] = []

    def close(r: list[int]) -> None:
        if len(r) >= 3:  # three back-to-back repeats = loop, not speech
            ranges.append((stream[r[0]][1], stream[min(r[-1] + n - 1, len(stream) - 1)][1]))

    for i in idxs:
        if run and i - run[-1] <= n + 1:  # at most one word between repeats
            run.append(i)
        else:
            close(run)
            run = [i]
    close(run)
    return ranges


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
