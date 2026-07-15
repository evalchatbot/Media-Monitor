"""E-paper pipeline: list pages -> download scans -> read (vision) -> match -> alert.

Mirrors the newspaper pipeline so both feed the same Mention table and alert
path. Each page is read into text ONCE (cached on EPaperPage.ocr_text); keyword
matching — including for keywords added later — runs on the cached text with
the exact same matcher (`find_matches`) the website articles use.
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from config import settings

from app.core import scoring
from app.core.keywords import find_matches
from app.db.base import SessionLocal
from app.db.models import EPaperPage, Keyword, Mention
from app.epaper import reader, sources
from app.notifiers import get_notifier
from app.notifiers.base import Alert
from app.scrapers.footer import add_footer

logger = logging.getLogger(__name__)

_PKT = timezone(timedelta(hours=5))
_UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")}
# Re-match window: new/changed keywords are matched against the last few days of
# already-read pages, so adding a keyword instantly searches the recent archive.
_MATCH_DAYS = 3


def _active_keywords(session, keyword_ids=None) -> list[tuple[str, str]]:
    stmt = select(Keyword).where(Keyword.active.is_(True))
    if keyword_ids:
        stmt = stmt.where(Keyword.id.in_(keyword_ids))
    return [(k.text, k.language) for k in session.execute(stmt).scalars()]


def run_epaper_scan(keyword_ids=None, fetch: bool = True, papers: list[str] | None = None) -> dict:
    """One full e-paper cycle. `fetch=False` skips edition download and only
    (re)reads + (re)matches what's already stored — used by per-keyword scans."""
    session = SessionLocal()
    notifier = get_notifier()
    summary = {"papers": 0, "pages": 0, "downloaded": 0, "read": 0,
               "no_key": 0, "mentions": 0, "alerts": 0}
    try:
        keywords = _active_keywords(session, keyword_ids)
        if fetch:
            _fetch_editions(session, papers, summary)
        _read_pages(session, summary)
        if keywords:
            match_stored_pages(session, keywords, notifier, summary, since_days=_MATCH_DAYS)
        return summary
    finally:
        session.close()


# ------------------------------------------------------------------ fetch ---
def _fetch_editions(session, papers, summary) -> None:
    today = datetime.now(_PKT).date()
    slugs = papers or sources.enabled_slugs()
    for slug in slugs:
        summary["papers"] += 1
        for pg in sources.list_pages(slug, today):
            summary["pages"] += 1
            row = session.execute(select(EPaperPage).where(
                EPaperPage.paper == pg.paper, EPaperPage.city == pg.city,
                EPaperPage.date == pg.date, EPaperPage.page_no == pg.page_no,
            )).scalar_one_or_none()
            if row is None:
                row = EPaperPage(paper=pg.paper, source=pg.source, city=pg.city,
                                 date=pg.date, page_no=pg.page_no,
                                 image_url=pg.image_url, viewer_url=pg.viewer_url)
                session.add(row)
                try:
                    session.commit()
                except IntegrityError:
                    session.rollback()
                    continue
            if not row.image_path or not Path(row.image_path).exists():
                path = _download(pg)
                if path:
                    row.image_path = str(path)
                    session.commit()
                    summary["downloaded"] += 1


def _download(pg: sources.EPage) -> Path | None:
    out = settings.storage_dir / "epaper" / pg.paper / pg.date
    out.mkdir(parents=True, exist_ok=True)
    dest = out / f"{pg.city}_p{pg.page_no:02d}.jpg"
    try:
        with httpx.stream("GET", pg.image_url, headers=_UA, timeout=60,
                          follow_redirects=True) as r:
            r.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in r.iter_bytes():
                    fh.write(chunk)
        if dest.stat().st_size < 5000:  # error page, not a scan
            dest.unlink(missing_ok=True)
            return None
        return dest
    except Exception as exc:
        logger.warning("e-paper download failed %s: %s", pg.image_url, exc)
        return None


# ------------------------------------------------------------------- read ---
def _read_pages(session, summary) -> None:
    # 'failed' is retried too — failures are usually transient (rate limits,
    # network); a page that keeps failing just costs one request per cycle.
    pending = session.execute(select(EPaperPage).where(
        EPaperPage.ocr_status.in_(("pending", "no_key", "failed")),
        EPaperPage.image_path.is_not(None),
    ).order_by(EPaperPage.date.desc(), EPaperPage.page_no)).scalars().all()
    if not pending:
        return

    from app.epaper import imagemap

    # 1) Papers with a publisher image-map: exact regions + clean article text,
    #    straight from the page — no LLM, no key, no rate limit.
    map_pages = [p for p in pending if imagemap.supports(p.paper)]
    for row in map_pages:
        if _read_via_map(session, row, summary):
            summary["read"] = summary.get("read", 0) + 1

    # 2) Everything else needs the vision reader (a key).
    vision = [p for p in pending if not imagemap.supports(p.paper) and p.ocr_status != "done"]
    if not vision:
        return
    if not reader.has_key():
        for row in vision:
            if row.ocr_status != "no_key":
                row.ocr_status = "no_key"
        session.commit()
        summary["no_key"] = len(vision)
        logger.info("e-paper: %d pages await a vision API key for reading", len(vision))
        return
    import time as _time

    for i, row in enumerate(vision):
        if i:
            _time.sleep(1.2)  # stay well inside free-tier rate limits
        try:
            row.ocr_text = reader.read_page(row.image_path)
            row.ocr_status = "done"
            summary["read"] += 1
        except Exception as exc:
            logger.warning("e-paper read failed %s p%d: %s", row.paper, row.page_no, exc)
            row.ocr_status = "failed"
        session.commit()


def _read_via_map(session, row, summary) -> bool:
    """Populate a page's regions + text from the publisher image-map. Returns
    True on success; leaves the row 'failed' (retryable) if the map is
    unavailable this run (e.g. an ad-only page with no article regions)."""
    from app.epaper import imagemap

    try:
        from PIL import Image

        with Image.open(row.image_path) as im:
            w, h = im.size
    except Exception:
        row.ocr_status = "failed"
        session.commit()
        return False
    res = imagemap.extract(row.paper, row.viewer_url, w, h)
    if not res:
        row.ocr_status = "failed"
        session.commit()
        return False
    regions, page_text = res
    row.regions = [{"box": r.box, "text": r.text} for r in regions]
    row.ocr_text = page_text
    row.ocr_status = "done"
    session.commit()
    return True


# ------------------------------------------------------------------ match ---
def match_stored_pages(session, keywords, notifier, summary,
                       since_days: int | None = _MATCH_DAYS) -> None:
    """Match keywords against already-read pages. `since_days=None` = every
    stored page (used by the instant per-keyword Quick Scan); scans use a small
    window since older pages were already matched when they arrived. Content is
    never matched from before the monitoring window (MONITOR_SINCE)."""
    floor = settings.monitor_since_date.isoformat()
    since = floor if since_days is None else max(
        floor, (datetime.now(_PKT).date() - timedelta(days=since_days)).isoformat()
    )
    pages = session.execute(select(EPaperPage).where(
        EPaperPage.ocr_status == "done", EPaperPage.date >= since,
    )).scalars().all()
    for row in pages:
        if not row.ocr_text:
            continue
        summary["pages_checked"] = summary.get("pages_checked", 0) + 1
        matches = find_matches(row.ocr_text, keywords)
        if not matches:
            continue
        matched_kw = sorted({m.keyword for m in matches})
        ext_id = f"{row.paper}:{row.city}:{row.date}:p{row.page_no}"

        mention = session.execute(select(Mention).where(
            Mention.module == "epaper", Mention.external_id == ext_id,
        )).scalar_one_or_none()

        if mention is not None:
            # Self-heal: if the stamped shot's file is gone (host move, pruned
            # volume), rebuild it from the page scan.
            if not (mention.screenshot_path and Path(mention.screenshot_path).exists()):
                shot = _detection_shot(row)
                if shot:
                    mention.screenshot_path = shot
                    session.commit()
            new_kw = [k for k in matched_kw if k not in (mention.matched_keywords or [])]
            if not new_kw:
                continue
            mention.matched_keywords = sorted(set(mention.matched_keywords or []) | set(matched_kw))
            session.commit()
            _alert(notifier, session, mention, new_kw, summary)
            continue

        snippet = _snippet(row.ocr_text, matched_kw)
        scored = scoring.score(f"{row.source} e-paper page {row.page_no}",
                               row.ocr_text, matched_kw) or {}
        shot = _detection_shot(row)
        # Press-clipping: cut the matched item out of the page (verified crop).
        # Card shows the clipping; the stamped full page stays a click away.
        kw_lang = {m.keyword: m.language for m in matches}
        clipping = _make_clip(row, matched_kw, kw_lang, snippet)
        mention = Mention(
            module="epaper",
            external_id=ext_id,
            source=row.source,
            section=f"E-Paper · {row.city.title()} · page {row.page_no}",
            title=f"{row.source} print edition — {_nice_date(row.date)}, page {row.page_no}",
            url=row.viewer_url or row.image_url,
            matched_keywords=matched_kw,
            snippet=snippet,
            summary=scored.get("summary") or snippet,
            relevance=scored.get("relevance"),
            sentiment=scored.get("sentiment"),
            screenshot_path=clipping or shot,
            full_screenshot_path=shot,
        )
        session.add(mention)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            continue
        summary["mentions"] += 1
        _alert(notifier, session, mention, matched_kw, summary)


def _make_clip(row: EPaperPage, matched_kw: list[str], kw_lang: dict,
               snippet: str) -> str | None:
    """A press-clipping for this detection, best method first:
    1. EXACT — if the page has publisher image-map regions, find the region
       whose text matches the keyword and crop it precisely (no AI).
    2. VISION — otherwise ask the vision model to locate + verify a crop.
    Returns a clip path, or None (caller falls back to the full page)."""
    if not row.image_path:
        return None
    from app.epaper import clip as _clip

    link = row.viewer_url or row.image_url
    # 1) exact region crop
    for kw in matched_kw:
        lang = kw_lang.get(kw, "en")
        for reg in (row.regions or []):
            text, box = reg.get("text"), reg.get("box")
            if text and box and find_matches(text, [(kw, lang)]):
                clip = _clip.clip_from_box(row.image_path, box, row.source, row.page_no, link)
                if clip:
                    return clip
    # 2) vision fallback
    return _clip.make_clipping(row.image_path, matched_kw[0], snippet, row.source,
                               row.page_no, link, language=kw_lang.get(matched_kw[0], "en"))


def _detection_shot(row: EPaperPage) -> str | None:
    """Footer-stamped copy of the page scan for the detection card."""
    if not row.image_path or not Path(row.image_path).exists():
        return None
    src = Path(row.image_path)
    dest = src.with_name(src.stem + "_det.jpg")
    try:
        if not dest.exists():
            shutil.copyfile(src, dest)
            add_footer(dest, row.source, f"E-Paper p{row.page_no}",
                       row.viewer_url or row.image_url)
        return str(dest)
    except Exception:
        return str(src)


def _alert(notifier, session, mention: Mention, keywords, summary) -> None:
    ok = notifier.send(Alert(
        source=mention.source, title=mention.title, summary=mention.summary or "",
        sentiment=mention.sentiment, url=mention.url,
        image_path=mention.screenshot_path, matched_keywords=keywords,
    ))
    if ok:
        mention.notified = True
        session.commit()
        summary["alerts"] += 1


def _nice_date(iso: str) -> str:
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%d %b %Y")
    except ValueError:
        return iso


def _snippet(text: str, keywords, width: int = 180) -> str:
    from app.core.keywords import normalize

    for kw in keywords:
        for lang in ("en", "ur"):
            hay, needle = normalize(text, lang), normalize(kw, lang)
            idx = hay.find(needle)
            if idx != -1:
                start = max(0, idx - width // 2)
                end = min(len(hay), idx + len(needle) + width // 2)
                return ("…" if start else "") + hay[start:end].strip() + ("…" if end < len(hay) else "")
    return text[:width].strip()
