# -*- coding: utf-8 -*-
"""Transcript hit location: exact timestamps, no duplicate utterances."""
from __future__ import annotations

from app.youtube import matcher, transcribe

KW = "عمران خان"


def words(tokens: list[str], t0: float = 0.0, step: float = 0.5) -> list[dict]:
    """Groq word-level output: one token per segment with its own timing."""
    segs, t = [], t0
    for w in tokens:
        segs.append({"start": round(t, 2), "end": round(t + step, 2), "text": w})
        t += step
    return segs


def test_word_level_timing_survives_attached_punctuation():
    """Groq glues punctuation to words; that must not cost us the exact timing."""
    clean = words(["الف", "عمران", "خان", "بے"], t0=100.0)
    punct = words(["الف", "عمران", "خان،", "بے"], t0=100.0)
    assert matcher._locate_keyword(clean, KW, "ur")[0].start == 100
    assert matcher._locate_keyword(punct, KW, "ur")[0].start == 100


def test_one_utterance_yields_one_hit():
    """Overlapping segments re-find the same speech — report it once."""
    segs = [
        {"start": 30.0, "end": 38.0, "text": "اور اب خبر یہ ہے کہ عمران خان نے آج کہا"},
        {"start": 31.5, "end": 39.0, "text": "عمران خان نے آج کہا کہ حکومت"},
        {"start": 33.0, "end": 40.0, "text": "خان نے آج کہا کہ حکومت کو"},
    ]
    assert len(matcher._locate_keyword(segs, KW, "ur")) == 1


def test_separate_occurrences_are_both_kept():
    segs = words(["عمران", "خان", "نے"], t0=10.0, step=1.0) + words(
        ["پھر", "عمران", "خان", "بولے"], t0=200.0, step=1.0
    )
    hits = matcher._locate_keyword(segs, KW, "ur")
    assert [h.start for h in hits] == [10, 201]


def test_keyword_in_opening_second_is_not_dropped():
    """Headlines are read at 0:00 — start=0 is a real timestamp, not 'missing'."""
    segs = words(["عمران", "خان", "نے", "کہا"], t0=0.0)
    hits = matcher.find_all_hits("", segs, [(KW, "ur")])
    assert KW in hits
    assert hits[KW][0].start == 0
    assert matcher.hit_is_verified(KW, "ur", 0, "عمران خان نے کہا") is True


def test_negative_timestamp_still_rejected():
    assert matcher.hit_is_verified(KW, "ur", -1, "عمران خان نے کہا") is False


def test_unrelated_text_produces_no_hit():
    segs = words(["شہباز", "شریف", "نے", "کہا"], t0=5.0)
    assert matcher.find_all_hits("", segs, [(KW, "ur")]) == {}


def test_hit_in_non_speech_audio_is_rejected():
    """Whisper invents words over silence; a keyword 'found' there is not a mention."""
    segs = words(["عمران", "خان"], t0=100.0)
    for s in segs:
        s["no_speech_prob"] = 0.95
    assert matcher.find_all_hits("", segs, [(KW, "ur")]) == {}


def test_hit_in_low_confidence_decode_is_rejected():
    segs = words(["عمران", "خان"], t0=100.0)
    for s in segs:
        s["avg_logprob"] = -1.8
    assert matcher.find_all_hits("", segs, [(KW, "ur")]) == {}


def test_confident_speech_is_kept():
    segs = words(["عمران", "خان"], t0=100.0)
    for s in segs:
        s["no_speech_prob"], s["avg_logprob"] = 0.05, -0.25
    assert KW in matcher.find_all_hits("", segs, [(KW, "ur")])


def test_transcripts_without_quality_fields_still_match():
    """Rows written before quality was recorded must keep working."""
    assert KW in matcher.find_all_hits("", words(["عمران", "خان"], t0=100.0), [(KW, "ur")])


def test_repetition_loop_is_not_a_mention():
    """A decoder stuck repeating the phrase is a hallucination, not speech."""
    segs = words(["عمران", "خان"] * 4, t0=540.0, step=0.3)
    assert matcher.find_all_hits("", segs, [(KW, "ur")]) == {}


def test_loop_elsewhere_does_not_hide_a_real_mention():
    segs = words(["عمران", "خان", "نے", "کہا"], t0=283.0) + words(
        ["عمران", "خان"] * 4, t0=900.0, step=0.3
    )
    hits = matcher.find_all_hits("", segs, [(KW, "ur")])
    assert [h.start for h in hits[KW]] == [283]


def test_chunk_overlap_does_not_duplicate_speech():
    """Chunk 2 re-transcribes chunk 1's tail; those words must not be kept twice."""
    existing = words(["عمران", "خان", "نے", "کہا", "کہ"], t0=600.0, step=1.0)
    new = words(["عمران", "خان", "نے", "کہا", "کہ"], t0=600.0, step=1.0)
    kept = transcribe._dedupe_overlap(existing, new, 600.0)
    assert kept == []
    assert len(matcher._locate_keyword(existing + kept, KW, "ur")) == 1


def test_chunk_dedupe_keeps_speech_past_the_overlap():
    existing = words(["الف"], t0=600.0, step=1.0)  # ends 601
    new = words(["عمران", "خان"], t0=602.0, step=1.0)
    assert transcribe._dedupe_overlap(existing, new, 600.0) == new
