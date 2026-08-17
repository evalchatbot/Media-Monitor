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
NEWS_BODIES_PER_SITE = 60     # article bodies fetched per publication
NEWS_PARALLEL = 4             # newspapers scraped at once (their own browsers)
EPAPER_PAGES_PER_PAPER = 24   # e-paper pages OCR'd per paper (a full edition)
EPAPER_PARALLEL = 5           # e-paper pages OCR'd at once

# Known e-paper domains → the app's adapter slug, so a user-added e-paper link is
# read as the FULL edition (every page) instead of only the front-page thumbnail
# the landing page happens to expose. Unknown domains fall back to generic image
# scraping.
_EPAPER_DOMAIN_SLUG = {
    "jang.com.pk": "jang",
    "thenews.com.pk": "thenews",
    "tribune.com.pk": "tribune",
    "nawaiwaqt.com.pk": "nawaiwaqt",
    "express.com.pk": "express",
    "express.pk": "express",
    "dunya.com.pk": "dunya",
    "jehanpakistan.com": "jehanpakistan",
    "dawn.com": "dawn",
}


def _slug_for_epaper_url(url: str) -> str | None:
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    for dom, slug in _EPAPER_DOMAIN_SLUG.items():
        if host == dom or host.endswith("." + dom):
            return slug
    return None
YT_VIDEOS_PER_CHANNEL = 5     # uploads transcribed per channel (Groq cost)


def _match_labels(haystack: str, keywords: list[tuple[str, str]]) -> list[str]:
    matches = find_matches(haystack, keywords)
    return sorted({m.keyword for m in matches}) if matches else []


_SENTIMENTS = {"Positive", "Negative", "Neutral"}


def _batch_sentiment(snippets: list[str]) -> list[str]:
    """One Groq request → a sentiment (Positive/Negative/Neutral) per snippet."""
    import json
    import httpx
    from config import settings

    n = len(snippets)
    if not snippets or not settings.groq_api_key:
        return [""] * n
    numbered = "\n".join(f"{i + 1}. {(s or '')[:400]}" for i, s in enumerate(snippets))
    prompt = (
        "Classify the overall sentiment of each numbered news excerpt as exactly "
        "one of: Positive, Negative, Neutral. "
        f'Reply ONLY with JSON {{"sentiments": [...]}} — {n} strings, same order.\n\n'
        + numbered
    )
    try:
        r = httpx.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.groq_api_key}"},
            json={
                "model": settings.groq_text_model,
                "temperature": 0,
                "max_tokens": 1200,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        r.raise_for_status()
        arr = json.loads(r.json()["choices"][0]["message"]["content"] or "{}").get("sentiments") or []
        out = []
        for i in range(n):
            v = str(arr[i]).strip().capitalize() if i < len(arr) else ""
            out.append(v if v in _SENTIMENTS else "")
        return out
    except Exception as exc:
        logger.warning("batch sentiment failed: %s", exc)
        return [""] * n


def _annotate_sentiment(jid: str) -> None:
    """Tag every result in the job with a sentiment, in ONE model request."""
    job = jobs.get(jid)
    if not job or not job.results:
        return
    results = list(job.results)
    sents = _batch_sentiment([(r.get("snippet") or r.get("title") or "") for r in results])
    for r, s in zip(results, sents):
        if s:
            r["sentiment"] = s
    jobs.set_progress(jid, current="tagging sentiment")


def _target_day(date_iso: str | None):
    """The edition/publication date being searched (defaults to today, PKT)."""
    from datetime import timedelta, timezone
    if date_iso:
        try:
            return datetime.strptime(date_iso, "%Y-%m-%d").date()
        except ValueError:
            pass
    return datetime.now(timezone(timedelta(hours=5))).date()


def search_press(jid: str, keywords: list[tuple[str, str]],
                 sources: list[dict], date_iso: str | None) -> None:
    """One click over the user's sources. `sources` is a list of
    {name, url, kind, language} rows the user added — no built-in papers.

    Websites run first because they return in seconds and give the user
    something to read while the e-paper editions — the slower, more valuable
    half — are read page by page.
    """
    day = _target_day(date_iso)
    news = [s for s in sources if (s.get("kind") or "newspaper") != "epaper"]
    epapers = [s for s in sources if (s.get("kind") or "") == "epaper"]
    if news:
        search_newspaper(jid, keywords, news, day)
    if epapers and not jobs.is_cancelled(jid):
        search_epaper(jid, keywords, epapers, date_iso)
    _annotate_sentiment(jid)          # one request tags every result


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
                     sources: list[dict], day=None) -> None:
    total = len(sources)
    jobs.set_progress(jid, phase="newspapers", total=total, checked=0)
    if not sources:
        return
    if day is None:
        day = _target_day(None)

    lock = threading.Lock()
    state = {"done": 0, "read": 0, "found": 0, "stale": 0}

    def _one(src) -> None:
        if jobs.is_cancelled(jid):
            return
        scraper = None
        try:
            scraper = _generic_scraper(src)
            _scrape_one_newspaper(jid, scraper, keywords, lock, state, day)
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
    if state["stale"]:
        logger.info("live news: skipped %d article(s) not published on %s",
                    state["stale"], day)


def _scrape_one_newspaper(jid, scraper, keywords, lock, state, day) -> None:
    from app.epaper.livescan import snippet as _make_snippet

    try:
        articles = scraper.list_articles()
    except Exception as exc:
        logger.warning("live news %s: listing failed: %s", scraper.name, exc)
        return

    fetched = 0
    for art in articles:
        if jobs.is_cancelled(jid) or fetched >= NEWS_BODIES_PER_SITE:
            break
        # One render yields both the body and the publication date, so filtering
        # to the requested day costs nothing extra.
        published = None
        if _match_labels(art.title, keywords):
            # Title already matches — still fetch, so we can date-check it and
            # show a real excerpt rather than just the headline.
            body, published = _fetch(scraper, art)
            fetched += 1
        else:
            body, published = _fetch(scraper, art)
            fetched += 1
        # Unknown date is kept: many sites publish none, and dropping those
        # would silently blind the search rather than narrow it.
        if published is not None and published != day:
            with lock:
                state["stale"] += 1
            continue
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
            "meta": published.strftime("%d %b") if published else "",
        })


def _fetch(scraper, art) -> tuple[str, object]:
    """(body, published_date|None) for one article, never raising."""
    try:
        if hasattr(scraper, "fetch_article"):
            return scraper.fetch_article(art)
        return (scraper.fetch_body(art) if hasattr(scraper, "fetch_body")
                else art.body), None
    except Exception:
        return "", None


# ==========================================================================
# E-paper — the priority path. Full current-date edition, per page, two tiers.
# See app/epaper/livescan.py for why clickable papers never touch a vision model.
# ==========================================================================
def search_epaper(jid: str, keywords: list[tuple[str, str]],
                  sources: list[dict], date_iso: str | None) -> None:
    """Read every page of each selected e-paper's edition for the chosen date and
    emit a clipping per keyword match.

    Each source is scanned in turn (pages within a source run concurrently) so
    progress reads sensibly paper by paper, and results stream as they are found.
    """
    from app.epaper import livescan

    day = _target_day(date_iso)
    jobs.set_progress(jid, phase="epaper", total=0, checked=0,
                      current=f"reading editions for {day.isoformat()}")

    stats: dict = {}
    resolved = [(s.get("name") or s["url"], _slug_for_epaper_url(s["url"]), s["url"])
                for s in sources]

    def emit(card):
        jobs.add_result(jid, card)

    def progress(**fields):
        jobs.set_progress(jid, **fields)

    for name, slug, url in resolved:
        if jobs.is_cancelled(jid):
            break
        try:
            livescan.scan_source(
                job_id=jid, name=name, slug=slug, url=url, keywords=keywords,
                day=day, emit=emit, progress=progress,
                cancelled=lambda: jobs.is_cancelled(jid), stats=stats,
            )
        except Exception as exc:
            logger.exception("live epaper %s failed", name)
            stats.setdefault("notes", []).append(f"{name}: {type(exc).__name__}: {exc}")
        jobs.set_progress(jid, total=stats.get("pages_total", 0),
                          checked=stats.get("pages_done", 0))

    _explain_epaper(jid, stats)
    logger.info("epaper done: %s", stats)


def _explain_epaper(jid: str, stats: dict) -> None:
    """Never let a silent zero be a mystery — say exactly what happened."""
    notes = list(stats.get("notes") or [])
    job = jobs.get(jid)
    hits = sum(1 for r in (job.results if job else []) if r.get("module") == "epaper")
    if hits == 0:
        total = stats.get("pages_total", 0)
        read = stats.get("pages_read", 0)
        if total == 0 and not notes:
            notes.append("No e-paper pages were published for that date yet.")
        elif read == 0 and total:
            notes.append(f"Found {total} page(s) but could not read any — "
                         f"{stats.get('first_err') or 'unknown error'}")
        elif read:
            notes.append(f"Read {read} page(s) of {total} — your keyword does not "
                         f"appear in this edition.")
    elif stats.get("map_fail") or stats.get("ocr_fail"):
        bad = stats.get("map_fail", 0) + stats.get("ocr_fail", 0)
        notes.append(f"{bad} page(s) could not be read and were skipped.")
    if notes:
        jobs.set_note(jid, " · ".join(notes[:3]))


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
    if not candidates:
        jobs.set_note(jid, "No non-live uploads found on your channels in the last 24h "
                           "(live streams are skipped). Try a wider date.")
        return

    stat = {"audio_ok": 0, "audio_fail": 0, "trans_ok": 0, "trans_chars": 0,
            "trans_fail": 0, "first_err": ""}

    for i, (chan_name, v) in enumerate(candidates):
        if jobs.is_cancelled(jid):
            break
        jobs.set_progress(jid, current=v.title[:60], checked=i)
        asset = None
        try:
            asset = acquire_audio(video_id=v.video_id, video_url=v.url)
            if asset is None:
                stat["audio_fail"] += 1
                if not stat["first_err"]:
                    stat["first_err"] = "audio download returned nothing (yt-dlp blocked?)"
                continue
            stat["audio_ok"] += 1
            text, segments, _meta = transcribe_audio(asset.path, language="ur")
            stat["trans_chars"] += len(text or "")
            if not text and not segments:
                stat["trans_fail"] += 1
                if not stat["first_err"]:
                    stat["first_err"] = f"transcription returned nothing ({_meta.get('error', 'unknown')})"
                continue
            stat["trans_ok"] += 1
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
            stat["trans_fail"] += 1
            if not stat["first_err"]:
                stat["first_err"] = f"{type(exc).__name__}: {str(exc)[:120]}"
            logger.warning("live yt transcribe failed %s: %s", v.video_id, exc)
        finally:
            cleanup_asset(asset)     # deletes the temp audio dir
        jobs.set_progress(jid, checked=i + 1)

    # Surface WHY YouTube found nothing.
    job = jobs.get(jid)
    yt_hits = sum(1 for r in (job.results if job else []) if r.get("module") == "youtube")
    if yt_hits == 0:
        if stat["audio_ok"] == 0:
            from app.youtube.media_source import last_error, cookies_configured
            reason = last_error() or stat["first_err"]
            hint = ("" if cookies_configured() else
                    " — set YOUTUBE_COOKIES (a logged-in cookies.txt) so YouTube lets "
                    "the server download audio")
            jobs.set_note(jid, f"Processed {len(candidates)} video(s) but couldn't get audio for "
                               f"any — {reason}{hint}")
        elif stat["trans_ok"] == 0:
            jobs.set_note(jid, f"Got audio but transcription failed on all "
                               f"{stat['audio_ok']} video(s) — {stat['first_err']}")
        else:
            jobs.set_note(jid, f"Transcribed {stat['trans_ok']} video(s) "
                               f"({stat['trans_chars']} chars) but your keyword wasn't spoken.")
    logger.info("youtube done: videos=%d audio_ok=%d trans_ok=%d chars=%d",
                len(candidates), stat["audio_ok"], stat["trans_ok"], stat["trans_chars"])

    _annotate_sentiment(jid)          # one request tags every result
