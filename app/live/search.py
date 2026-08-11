"""Ephemeral live-search implementations for each module.

Every function here scrapes/transcribes on demand and streams result cards into
an in-memory job (app.live.jobs). NOTHING is written to the database or the
storage volume: e-paper scans and YouTube audio go to OS temp files that are
deleted immediately after matching. These run only when the user clicks
"Search live results".
"""
from __future__ import annotations

import logging
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from app.core.keywords import find_matches
from app.live import jobs

logger = logging.getLogger(__name__)

# Bounds so one click can't run unbounded. Streaming means partial results show
# up immediately, so these keep a search responsive and its cost predictable.
NEWS_BODIES_PER_SITE = 15     # article bodies fetched per publication (speed)
NEWS_PARALLEL = 4             # newspapers scraped at once (their own browsers)
EPAPER_PAGES_PER_PAPER = 12   # e-paper pages OCR'd per paper (bounds cost + time)
EPAPER_PARALLEL = 4           # e-paper pages OCR'd at once
YT_VIDEOS_PER_CHANNEL = 5     # uploads transcribed per channel (Groq cost)


def _match_labels(haystack: str, keywords: list[tuple[str, str]]) -> list[str]:
    matches = find_matches(haystack, keywords)
    return sorted({m.keyword for m in matches}) if matches else []


def _scraper_labels(scraper) -> set[str]:
    """Every name a scraper can be selected by, casefolded.

    The picker shows a scraper's DISPLAY name (e.g. "Nawa-i-Waqt") but the scraper
    object is keyed by an internal slug ("nawaiwaqt"). Matching only the slug meant
    selecting any paper but Dawn scraped nothing. Match both."""
    labels: set[str] = set()
    name = (getattr(scraper, "name", "") or "").strip()
    if name:
        labels.add(name.casefold())
    cfg = getattr(scraper, "cfg", None)
    src = getattr(cfg, "source", "") if cfg is not None else ""
    if src:
        labels.add(src.strip().casefold())
    if name.casefold() == "dawn":
        labels.add("dawn")
    return labels


def search_press(jid: str, keywords: list[tuple[str, str]],
                 sources: set[str] | None, date_iso: str | None) -> None:
    """The newspaper page shows websites + e-papers together, so one click runs
    both into the same job (websites first — they're fast — then e-papers)."""
    search_newspaper(jid, keywords, sources)
    if not jobs.is_cancelled(jid):
        search_epaper(jid, keywords, sources, date_iso)


# ==========================================================================
# Newspaper — front-page crawl, fetch bodies, match. No screenshots, no DB.
# ==========================================================================
def search_newspaper(jid: str, keywords: list[tuple[str, str]],
                     sources: set[str] | None) -> None:
    from app.scrapers.sites import build_scrapers

    scrapers = build_scrapers()
    if sources:
        sel = {s.casefold() for s in sources}
        scrapers = [s for s in scrapers if _scraper_labels(s) & sel]
    total = len(scrapers)
    jobs.set_progress(jid, phase="newspapers", total=total, checked=0)
    if not scrapers:
        return

    lock = threading.Lock()
    state = {"done": 0, "read": 0, "found": 0}

    def _one(scraper) -> None:
        if jobs.is_cancelled(jid):
            return
        try:
            _scrape_one_newspaper(jid, scraper, keywords, lock, state)
        except Exception as exc:
            logger.warning("live news %s failed: %s", getattr(scraper, "name", "?"), exc)
        finally:
            try:
                scraper.close()
            except Exception:
                pass
            with lock:
                state["done"] += 1
                jobs.set_progress(jid, checked=state["done"])

    # Scrape the selected papers concurrently — each has its own browser, so a
    # 5-paper search finishes in roughly the time of the slowest one, not the sum.
    with ThreadPoolExecutor(max_workers=min(max(total, 1), NEWS_PARALLEL)) as ex:
        list(ex.map(_one, scrapers))


def _scrape_one_newspaper(jid, scraper, keywords, lock, state) -> None:
    from app.newspaper.pipeline import _make_snippet

    try:
        articles = scraper.list_articles()
    except Exception as exc:
        logger.warning("live news %s: listing failed: %s", scraper.name, exc)
        return

    fetched = 0
    for art in articles:
        if jobs.is_cancelled(jid) or fetched >= NEWS_BODIES_PER_SITE:
            break
        # Cheap title check first; otherwise fetch the body (costs a render).
        if _match_labels(art.title, keywords):
            body = ""
        else:
            try:
                body = scraper.fetch_body(art) if hasattr(scraper, "fetch_body") else art.body
            except Exception:
                body = ""
            fetched += 1
        haystack = f"{art.title}\n{body}"
        labels = _match_labels(haystack, keywords)
        with lock:
            state["read"] += 1
            if labels:
                state["found"] += 1
            jobs.set_progress(
                jid, current=f"{state['read']} read · {state['found']} found")
        if not labels:
            continue
        jobs.add_result(jid, {
            "module": "newspaper",
            "source": art.source,
            "title": art.title,
            "url": art.url,
            "section": art.section,
            "snippet": _make_snippet(haystack, labels),
            "keywords": labels,
            "meta": "",
        })


# ==========================================================================
# E-paper — list today's pages, download to temp, vision-read, match.
# ==========================================================================
def search_epaper(jid: str, keywords: list[tuple[str, str]],
                  sources: set[str] | None, date_iso: str | None) -> None:
    from app.epaper import sources as ep_sources
    from app.epaper.reader import has_key, read_page
    from app.epaper.pipeline import _download, _snippet

    if not has_key():
        jobs.set_progress(jid, phase="epaper", current="no vision key")
        return

    d = None
    if date_iso:
        try:
            d = datetime.strptime(date_iso, "%Y-%m-%d").date()
        except ValueError:
            d = None

    slugs = ep_sources.enabled_slugs()
    if sources:
        sel = {s.casefold() for s in sources}
        slugs = [s for s in slugs if ep_sources.SOURCES[s][0].strip().casefold() in sel]

    # Gather pages up front (bounded per paper) for a real progress total.
    all_pages: list = []
    for slug in slugs:
        if jobs.is_cancelled(jid):
            break
        pages = ep_sources.list_pages(slug, d)[:EPAPER_PAGES_PER_PAPER]
        all_pages.extend(pages)
    jobs.set_progress(jid, phase="epaper", total=len(all_pages), checked=0)
    if not all_pages:
        return

    lock = threading.Lock()
    state = {"done": 0}

    def _one_page(pg) -> None:
        if jobs.is_cancelled(jid):
            return
        dest = _download(pg)              # temp scan; deleted right after reading
        try:
            if dest is not None:
                try:
                    text = read_page(dest)
                except Exception as exc:
                    logger.warning("live epaper read failed %s p%s: %s", pg.paper, pg.page_no, exc)
                    text = ""
                labels = _match_labels(text, keywords)
                if labels:
                    jobs.add_result(jid, {
                        "module": "epaper",
                        "source": pg.source,
                        "title": f"{pg.source} — page {pg.page_no}",
                        "url": pg.viewer_url,
                        "section": f"Page {pg.page_no}",
                        "snippet": _snippet(text, labels),
                        "keywords": labels,
                        "meta": pg.date,
                    })
        finally:
            if dest is not None:
                try:
                    Path(dest).unlink(missing_ok=True)
                except Exception:
                    pass
            with lock:
                state["done"] += 1
                jobs.set_progress(jid, current=f"e-paper page {state['done']}/{len(all_pages)}",
                                  checked=state["done"])

    # OCR several pages at once — vision calls are network-bound, so a handful in
    # flight cuts the wall-clock sharply.
    with ThreadPoolExecutor(max_workers=EPAPER_PARALLEL) as ex:
        list(ex.map(_one_page, all_pages))


# ==========================================================================
# YouTube — discover uploads in a window, download+transcribe to temp, match.
# ==========================================================================
def search_youtube(jid: str, keywords: list[tuple[str, str]],
                   channels: list[dict], after_iso: str, before_iso: str) -> None:
    from app.youtube import discovery, matcher
    from app.youtube.media_source import acquire_audio, cleanup_asset
    from app.youtube.transcribe import transcribe_audio

    try:
        after = datetime.fromisoformat(after_iso)
        before = datetime.fromisoformat(before_iso)
    except ValueError:
        jobs.set_progress(jid, phase="youtube", current="bad window")
        return

    # Discover candidate uploads per channel (network only, no cost yet).
    candidates: list[tuple[str, object]] = []  # (channel_name, Video)
    for ch in channels:
        if jobs.is_cancelled(jid):
            break
        try:
            vids = discovery.fetch_uploads_in_range(
                ch["channel_id"],
                published_after=after,
                published_before=before,
                playlist_id=ch.get("playlist_id") or "",
            )
        except Exception as exc:
            logger.warning("live yt discovery failed %s: %s", ch.get("name"), exc)
            vids = []
        for v in vids[:YT_VIDEOS_PER_CHANNEL]:
            candidates.append((ch.get("name") or v.channel_name, v))

    jobs.set_progress(jid, phase="youtube", total=len(candidates), checked=0)

    for i, (chan_name, v) in enumerate(candidates):
        if jobs.is_cancelled(jid):
            break
        jobs.set_progress(jid, current=v.title[:60], checked=i)
        asset = None
        try:
            asset = acquire_audio(video_id=v.video_id, video_url=v.url)
            if asset is None:
                continue
            text, segments, _meta = transcribe_audio(asset.path, language="ur")
            if not text and not segments:
                continue
            hits = matcher.find_all_hits(text, segments, keywords)
            if not hits:
                continue
            labels = sorted(hits.keys())
            first_second = None
            excerpt = ""
            for label in labels:
                for h in hits[label]:
                    if first_second is None or (h.start is not None and h.start < first_second):
                        first_second = int(h.start) if h.start is not None else first_second
                    if not excerpt and getattr(h, "excerpt", ""):
                        excerpt = h.excerpt
            jobs.add_result(jid, {
                "module": "youtube",
                "source": chan_name,
                "title": v.title,
                "url": discovery.deep_link(v.video_id, first_second),
                "section": chan_name,
                "snippet": excerpt or (text[:180]),
                "keywords": labels,
                "meta": (f"@{first_second // 60}:{first_second % 60:02d}"
                         if first_second is not None else ""),
            })
        except Exception as exc:
            logger.warning("live yt transcribe failed %s: %s", v.video_id, exc)
        finally:
            cleanup_asset(asset)     # deletes the temp audio dir
        jobs.set_progress(jid, checked=i + 1)
