# -*- coding: utf-8 -*-
"""Verify the e-paper reading stack on the machine that actually runs it.

    python -m scripts.check_ocr

Reports which vision providers are configured, then tests ONLY the newspapers
and e-papers the user saved in the database (NewsSource). It does not probe
built-in papers (Jang, Express, Dawn, …) unless the user added that link.

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
from sqlalchemy import select

from app.core.keywords import find_matches
from app.db.base import SessionLocal
from app.db.models import NewsSource
from app.epaper import imagemap, reader, sources
from app.live.search import _slug_for_epaper_url

_PKT = timezone(timedelta(hours=5))
_OK, _NO = "PASS", "FAIL"


def _line(label: str, verdict: str, detail: str = "") -> None:
    print(f"  [{verdict:4s}] {label}" + (f" — {detail}" if detail else ""))


def _user_sources() -> list[dict]:
    db = SessionLocal()
    try:
        rows = db.execute(
            select(NewsSource).where(NewsSource.active.is_(True))
            .order_by(NewsSource.kind, NewsSource.name)
        ).scalars().all()
        return [
            {"name": r.name, "url": r.url, "kind": r.kind or "newspaper",
             "language": r.language or "en"}
            for r in rows
        ]
    finally:
        db.close()


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
        print("    will be SKIPPED rather than reported with false matches.")
        print("    Set GEMINI_API_KEY to enable them.")
        print("    Note: Gemini keys start with 'AIza'; 'gsk_' is a Groq key.")


def _list_saved(rows: list[dict]) -> None:
    print("\n2. YOUR SAVED SOURCES (database — what live search uses)")
    if not rows:
        print("  (none) Add a newspaper or e-paper in the app with “+ Add more”.")
        print("  This check will not test any paper until you do.")
        return
    for r in rows:
        kind = "e-paper" if r["kind"] == "epaper" else "website"
        print(f"  · {r['name']}  [{kind}]  {r['url']}")


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


def _clickable_test(epapers: list[dict]) -> bool:
    """The no-key path: publisher image-map -> exact regions + real text."""
    day = datetime.now(_PKT).date()
    tested = False
    ok = True
    for src in epapers:
        slug = _slug_for_epaper_url(src["url"])
        if not slug or not imagemap.supports(slug):
            continue
        tested = True
        pages = sources.list_pages(slug, day)
        if not pages:
            _line(src["name"], "--", f"no edition for {day} yet")
            continue
        t = time.time()
        res = imagemap.extract(slug, pages[0].viewer_url, 0, 0)
        if not res:
            _line(src["name"], _NO, "image-map returned no article regions")
            ok = False
            continue
        regions, text = res
        with_text = [r for r in regions if len(r.text) > 60]
        good = bool(with_text)
        _line(src["name"], _OK if good else _NO,
              f"{len(regions)} regions, {len(with_text)} with text, "
              f"{len(text)} chars in {time.time() - t:.1f}s")
        if not good:
            ok = False
    if not tested:
        print("  (none of your saved e-papers use a clickable publisher map)")
    return ok


def _ocr_test(epapers: list[dict]) -> bool:
    """Vision OCR for saved image-only e-papers."""
    ok = True
    tested = False
    for src in epapers:
        slug = _slug_for_epaper_url(src["url"])
        if not slug:
            _line(src["name"], "--",
                  "not a recognised e-paper host — live search will skip the full edition")
            continue
        if imagemap.supports(slug):
            continue
        lang = src.get("language") or "en"
        known = sources.SOURCES.get(slug)
        if known:
            lang = known[1]
        if lang == "ur" and not reader.can_read_urdu():
            _line(src["name"], "--", "skipped — no Nastaliq-capable key set")
            continue
        tested = True
        probe = "کے" if lang == "ur" else "the"
        if not _read_test(src["name"], slug, lang, probe):
            ok = False
    if not tested:
        print("  (none of your saved e-papers need vision OCR)")
    return ok


def main() -> int:
    print("=" * 68)
    print("E-PAPER READING SELF-CHECK")
    print("=" * 68)
    _providers()

    rows = _user_sources()
    _list_saved(rows)
    epapers = [r for r in rows if r["kind"] == "epaper"]
    websites = [r for r in rows if r["kind"] != "epaper"]

    if websites:
        print(f"\n   {len(websites)} website(s) saved — they scrape article text,")
        print("   not page photos, so they are not part of this OCR check.")

    print("\n3. CLICKABLE E-PAPERS among your saved sources")
    print("   (no API key needed — real publisher text)")
    ok_click = _clickable_test(epapers)

    print("\n4. IMAGE-ONLY E-PAPERS among your saved sources")
    print("   (vision OCR required)")
    ok_ocr = _ocr_test(epapers)

    print("\n" + "=" * 68)
    if not rows:
        print("RESULT: nothing to read — add papers in the app first.")
        return 0
    if not epapers:
        print("RESULT: no e-papers saved. Website sources do not need OCR.")
        return 0
    if ok_click and ok_ocr:
        print("RESULT: reading stack is healthy for your saved e-papers.")
        if not reader.can_read_urdu():
            print("        (Urdu image-only papers remain disabled — set GEMINI_API_KEY.)")
        return 0
    print("RESULT: something is broken above. A FAIL on OCR usually means the")
    print("        configured model id no longer exists, or the model produced")
    print("        degenerate output and was correctly discarded.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
