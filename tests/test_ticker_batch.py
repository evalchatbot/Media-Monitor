# -*- coding: utf-8 -*-
"""Ticker OCR: batched reads collapse a scrolling line into distinct entries.

A ticker line is on screen for many seconds, so successive OCR reads overlap.
The merge must keep one entry (richest wording, earliest time) per real line,
without dropping genuinely different lines.
"""
from __future__ import annotations

from app.youtube import livestream as ls


def test_scrolling_line_collapses_to_one_entry():
    entries = [
        (10, "ایران پر امریکی حملہ"),
        (11, "ایران پر امریکی حملہ جاری"),
        (12, "ایران پر امریکی حملہ جاری ہے"),
    ]
    merged = ls._merge_ticker(entries)
    assert len(merged) == 1, "one scrolling line = one entry"
    assert merged[0][0] == 10, "keeps the earliest timestamp"
    assert "جاری ہے" in merged[0][1], "keeps the fullest wording"


def test_distinct_lines_are_kept_separate():
    entries = [
        (10, "وزیراعظم کا دورہ"),
        (40, "بارش کی وارننگ جاری"),
        (70, "اسٹاک مارکیٹ میں تیزی"),
    ]
    merged = ls._merge_ticker(entries)
    assert len(merged) == 3, "unrelated lines must not merge"


def test_empty_and_blank_reads_dropped():
    merged = ls._merge_ticker([(1, ""), (2, "   "), (3, "خبر")])
    assert merged == [(3, "خبر")]


def test_overlap_scores():
    assert ls._overlap("ایران حملہ جاری", "ایران حملہ جاری ہے") >= 0.6
    assert ls._overlap("وزیراعظم دورہ", "اسٹاک مارکیٹ") == 0.0


def test_batch_ocr_returns_one_string_per_crop_on_failure(monkeypatch):
    """A failed/rate-limited call must still return the right-length list so the
    per-frame timestamp alignment never desyncs."""
    from PIL import Image

    crops = [Image.new("RGB", (100, 20)) for _ in range(5)]

    class _Resp:
        status_code = 500
        text = "boom"

    monkeypatch.setattr(ls.httpx, "post", lambda *a, **k: _Resp())
    monkeypatch.setattr(ls.settings, "gemini_api_key", "x", raising=False)
    out = ls._gemini_ocr_batch(crops)
    assert out == [""] * 5
