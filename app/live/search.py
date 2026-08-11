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


def search_press(jid: str, keywords: list[tuple[str, str]],
                 sources: list[dict], date_iso: str | None) -> None:
    """One click over the user's sources. `sources` is a list of
    {name, url, kind, language} rows the user added — no built-in papers.
    Websites (fast) first, then e-papers."""
    news = [s for s in sources if (s.get("kind") or "newspaper") != "epaper"]
    epapers = [s for s in sources if (s.get("kind") or "") == "epaper"]
    if news:
        search_newspaper(jid, keywords, news)
    if epapers and not jobs.is_cancelled(jid):
        search_epaper(jid, keywords, epapers, date_iso)


# ==========================================================================
# Newspaper — scrape the URL the user added: find article links, read text,
# match. Generic (no per-site selectors). No screenshots, no DB.
# ==========================================================================
def _generic_scraper(src: dict):
    from app.scrapers.configurable import ConfigurableScraper, SiteConfig
    name = src.get("name") or src.get("url") or "source"
    return ConfigurableScraper(SiteConfig(
        name=name,
        source=name,
        base_url=src["url"],
        sections={"Home": src["url"]},
        language=src.get("language", "en"),
        min_title_len=12,
    ))


def search_newspaper(jid: str, keywords: list[tuple[str, str]],
                     sources: list[dict]) -> None:
    total = len(sources)
    jobs.set_progress(jid, phase="newspapers", total=total, checked=0)
    if not sources:
        return

    lock = threading.Lock()
    state = {"done": 0, "read": 0, "found": 0}

    def _one(src) -> None:
        if jobs.is_cancelled(jid):
            return
        scraper = None
        try:
            scraper = _generic_scraper(src)
            _scrape_one_newspaper(jid, scraper, keywords, lock, state)
        except Exception as exc:
            logger.warning("live news %s failed: %s", src.get("name"), exc)
        finally:
            if scraper is not None:
                try:
                    scraper.close()
                except Exception:
                    pass
            with lock:
                state["done"] += 1
                jobs.set_progress(jid, checked=state["done"])

    # Each source has its own browser → all scrape at once.
    with ThreadPoolExecutor(max_workers=min(max(total, 1), NEWS_PARALLEL)) as ex:
        list(ex.map(_one, sources))


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
def _epaper_image_urls(page_url: str) -> list[str]:
    """Best-effort: render the e-paper URL and collect the page-scan image links."""
    from urllib.parse import urljoin
    from bs4 import BeautifulSoup
    from app.scrapers.base import BaseScraper

    bs = BaseScraper(name="epaper", base_url=page_url)
    try:
        html = bs.render(page_url, wait_ms=2800)
    except Exception as exc:
        logger.warning("live epaper render failed %s: %s", page_url, exc)
        return []
    finally:
        try:
            bs.close()
        except Exception:
            pass
    soup = BeautifulSoup(html, "lxml")
    urls, seen = [], set()
    for img in soup.select("img[src], img[data-src]"):
        src = img.get("src") or img.get("data-src") or ""
        if not src:
            continue
        full = urljoin(page_url, src)
        base = full.split("?")[0].lower()
        if not base.endswith((".jpg", ".jpeg", ".png", ".webp")):
            continue
        if full not in seen:
            seen.add(full)
            urls.append(full)
    return urls


def _download_image(url: str) -> Path | None:
    """Download an image to an OS temp file (nothing persists). None if it's tiny
    (a logo/icon, not a page scan) or fails."""
    import httpx
    verify = "e.dunya.com.pk" not in url
    try:
        with httpx.stream("GET", url, timeout=45, follow_redirects=True, verify=verify,
                          headers={"User-Agent": "Mozilla/5.0"}) as r:
            r.raise_for_status()
            fd = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
            size = 0
            for chunk in r.iter_bytes():
                fd.write(chunk)
                size += len(chunk)
            fd.close()
            if size < 15000:                 # too small to be a readable page
                Path(fd.name).unlink(missing_ok=True)
                return None
            return Path(fd.name)
    except Exception:
        return None


def search_epaper(jid: str, keywords: list[tuple[str, str]],
                  sources: list[dict], date_iso: str | None) -> None:
    """OCR the page images on each user-added e-paper URL and match. Best-effort:
    JS-only viewers may expose no <img> tags, in which case nothing is found."""
    from app.epaper.reader import has_key, read_page
    from app.epaper.pipeline import _snippet

    if not has_key():
        jobs.set_progress(jid, phase="epaper", current="no vision key configured")
        return

    # (source_name, viewer_url, image_url) — bounded per source.
    candidates: list[tuple[str, str, str]] = []
    for src in sources:
        if jobs.is_cancelled(jid):
            break
        name = src.get("name") or src["url"]
        for img in _epaper_image_urls(src["url"])[:EPAPER_PAGES_PER_PAPER]:
            candidates.append((name, src["url"], img))
    jobs.set_progress(jid, phase="epaper", total=len(candidates), checked=0)
    if not candidates:
        return

    lock = threading.Lock()
    state = {"done": 0}

    def _one(item) -> None:
        name, viewer, img_url = item
        if jobs.is_cancelled(jid):
            return
        tmp = _download_image(img_url)
        try:
            if tmp is not None:
                try:
                    text = read_page(tmp)
                except Exception as exc:
                    logger.warning("live epaper read failed %s: %s", img_url, exc)
                    text = ""
                labels = _match_labels(text, keywords)
                if labels:
                    jobs.add_result(jid, {
                        "module": "epaper",
                        "source": name,
                        "title": f"{name} — e-paper page",
                        # Open the exact page scan the word was read from, not the
                        # site homepage; the same image is the clickable preview.
                        "url": img_url,
                        "image": img_url,
                        "section": "E-paper",
                        "snippet": _snippet(text, labels),
                        "keywords": labels,
                        "meta": "",
                    })
        finally:
            if tmp is not None:
                try:
                    Path(tmp).unlink(missing_ok=True)
                except Exception:
                    pass
            with lock:
                state["done"] += 1
                jobs.set_progress(jid, current=f"e-paper page {state['done']}/{len(candidates)}",
                                  checked=state["done"])

    with ThreadPoolExecutor(max_workers=EPAPER_PARALLEL) as ex:
        list(ex.map(_one, candidates))


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
