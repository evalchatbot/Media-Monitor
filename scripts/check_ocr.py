# -*- coding: utf-8 -*-
"""Verify the e-paper reading stack on the machine that actually runs it.

    python -m scripts.check_ocr

Reports which vision providers are configured, then reads one real English page
and one real Urdu page from today's editions and says plainly whether each
worked. Also checks a clickable paper, which needs no key at all.

Run this after changing any API key — a key that is merely PRESENT proves
nothing: the previous Groq vision model was retired and returned 404, and the
one before that returned confident nonsense for Urdu.
"""
from __future__ import annotations

import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from app.core.keywords import find_matches
from app.epaper import imagemap, reader, sources

_PKT = timezone(timedelta(hours=5))
_OK, _NO = "PASS", "FAIL"


def _line(label: str, verdict: str, detail: str = "") -> None:
    print(f"  [{verdict:4s}] {label}" + (f" — {detail}" if detail else ""))


def _providers() -> None:
    print("\n1. PROVIDERS CONFIGURED")
    from config import settings

    for name, key in (("GROQ_API_KEY", settings.groq_api_key),
                      ("GEMINI_API_KEY", settings.gemini_api_key),
                      ("ANTHROPIC_API_KEY", settings.anthropic_api_key)):
        # Never print a key: length + prefix is enough to spot a wrong paste.
        if key:
            _line(name, _OK, f"{len(key)} chars, starts {key[:4]}…")
        else:
            _line(name, "--", "not set")
    print(f"\n  primary provider : {reader.provider()}")
    print(f"  can read Urdu    : {reader.can_read_urdu()}")
    if not reader.can_read_urdu():
        print("\n  ! Groq alone CANNOT read Urdu Nastaliq. Image-only Urdu papers")
        print("    (Express Urdu, Jehan Pakistan) will be SKIPPED rather than")
        print("    reported with false matches. Set GEMINI_API_KEY to enable them.")
        print("    Note: Gemini keys start with 'AIza'; 'gsk_' is a Groq key.")


def _fetch(url: str) -> Path | None:
    try:
        r = httpx.get(url, timeout=90, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0"},
                      verify="e.dunya.com.pk" not in url)
        if r.status_code >= 400 or len(r.content) < 15000:
            return None
        p = Path(tempfile.gettempdir()) / f"ocrcheck_{abs(hash(url))}.jpg"
        p.write_bytes(r.content)
        return p
    except Exception:
        return None


def _read_test(label: str, slug: str, language: str, probe: str) -> bool:
    """OCR page 1 of `slug` and check a word that must appear on any real page."""
    day = datetime.now(_PKT).date()
    pages = sources.list_pages(slug, day)
    if not pages:
        _line(label, "--", f"no pages published for {day} yet")
        return True                       # not an OCR failure
    img = _fetch(pages[0].image_url)
    if img is None:
        _line(label, "--", "page scan could not be downloaded")
        return True
    t = time.time()
    try:
        text = reader.read_page(img, language=language)
    except Exception as exc:
        _line(label, _NO, f"{str(exc)[:150]}")
        return False
    finally:
        img.unlink(missing_ok=True)
    dt = time.time() - t
    hit = bool(find_matches(text, [(probe, language)]))
    detail = f"{len(text)} chars in {dt:.1f}s; probe {probe!r} {'found' if hit else 'ABSENT'}"
    if len(text) < 400:
        _line(label, _NO, detail + " — suspiciously short")
        return False
    _line(label, _OK, detail)
    return True


def _clickable_test() -> bool:
    """The no-key path: publisher image-map -> exact regions + real text."""
    day = datetime.now(_PKT).date()
    for slug in ("jang", "thenews", "nawaiwaqt"):
        pages = sources.list_pages(slug, day)
        if not pages:
            _line(slug, "--", f"no edition for {day} yet")
            continue
        t = time.time()
        res = imagemap.extract(slug, pages[0].viewer_url, 0, 0)
        if not res:
            _line(slug, _NO, "image-map returned no article regions")
            continue
        regions, text = res
        with_text = [r for r in regions if len(r.text) > 60]
        _line(slug, _OK if with_text else _NO,
              f"{len(regions)} regions, {len(with_text)} with text, "
              f"{len(text)} chars in {time.time() - t:.1f}s")
    return True


def main() -> int:
    print("=" * 68)
    print("E-PAPER READING SELF-CHECK")
    print("=" * 68)
    _providers()

    print("\n2. CLICKABLE PAPERS (no API key needed — real publisher text)")
    _clickable_test()

    print("\n3. IMAGE-ONLY PAPERS (vision OCR required)")
    ok_en = _read_test("English (Dawn)", "dawn", "en", "the")
    ok_ur = True
    if reader.can_read_urdu():
        ok_ur = _read_test("Urdu (Express)", "express", "ur", "کے")
    else:
        _line("Urdu (Express)", "--", "skipped — no Nastaliq-capable key set")

    print("\n" + "=" * 68)
    if ok_en and ok_ur:
        print("RESULT: reading stack is healthy.")
        if not reader.can_read_urdu():
            print("        (Urdu image-only papers remain disabled — set GEMINI_API_KEY.)")
        return 0
    print("RESULT: something is broken above. A FAIL on OCR usually means the")
    print("        configured model id no longer exists, or the model produced")
    print("        degenerate output and was correctly discarded.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
