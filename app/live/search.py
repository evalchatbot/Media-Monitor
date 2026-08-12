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
EPAPER_PAGES_PER_PAPER = 18   # e-paper pages OCR'd per paper (bounds cost + time)
EPAPER_PARALLEL = 4           # e-paper pages OCR'd at once

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


def _download_image(url: str) -> tuple[Path | None, str]:
    """Download an image to an OS temp file (nothing persists).

    Returns (path, "") on success, or (None, reason) so a failure is explainable
    (HTTP 403, timeout, too-small response, TLS error, …). Browser-like headers +
    a Referer help with image hosts that block plain clients / hotlinking."""
    import httpx
    from urllib.parse import urlparse
    p = urlparse(url)
    origin = f"{p.scheme}://{p.netloc}"
    verify = "e.dunya.com.pk" not in url
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Referer": origin + "/",
        "Accept-Language": "en-US,en;q=0.9,ur;q=0.8",
    }
    try:
        with httpx.stream("GET", url, timeout=45, follow_redirects=True, verify=verify,
                          headers=headers) as r:
            if r.status_code >= 400:
                return None, f"HTTP {r.status_code}"
            fd = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
            size = 0
            for chunk in r.iter_bytes():
                fd.write(chunk)
                size += len(chunk)
            fd.close()
            if size < 15000:                 # too small to be a readable page scan
                Path(fd.name).unlink(missing_ok=True)
                return None, f"response too small ({size} bytes) — likely a block/error page"
            return Path(fd.name), ""
    except Exception as exc:
        return None, f"{type(exc).__name__}: {str(exc)[:120]}"


def search_epaper(jid: str, keywords: list[tuple[str, str]],
                  sources: list[dict], date_iso: str | None) -> None:
    """OCR the page images on each user-added e-paper URL and match. Best-effort:
    JS-only viewers may expose no <img> tags, in which case nothing is found."""
    from app.epaper import sources as ep_sources
    from app.epaper.reader import has_key, provider, read_page
    from app.epaper.pipeline import _snippet

    if not has_key() or provider() == "none":
        jobs.set_progress(jid, phase="epaper",
                          current="e-paper reading needs a vision API key (not set)")
        logger.warning("epaper OCR skipped: no vision key")
        return

    d = None
    if date_iso:
        try:
            d = datetime.strptime(date_iso, "%Y-%m-%d").date()
        except ValueError:
            d = None

    # (source_name, link_url, image_url, page_label) — bounded per source.
    candidates: list[tuple[str, str, str, str]] = []
    for src in sources:
        if jobs.is_cancelled(jid):
            break
        name = src.get("name") or src["url"]
        slug = _slug_for_epaper_url(src["url"])
        if slug:
            # Known e-paper → full edition (every page), via the site's adapter.
            try:
                pages = ep_sources.list_pages(slug, d)[:EPAPER_PAGES_PER_PAPER]
            except Exception as exc:
                logger.warning("live epaper list_pages %s failed: %s", slug, exc)
                pages = []
            for pg in pages:
                candidates.append((name, pg.viewer_url or pg.image_url,
                                   pg.image_url, f"page {pg.page_no}"))
        else:
            # Unknown site → best-effort images on the landing page.
            for img in _epaper_image_urls(src["url"])[:EPAPER_PAGES_PER_PAPER]:
                candidates.append((name, img, img, "page"))
    jobs.set_progress(jid, phase="epaper", total=len(candidates), checked=0)
    if not candidates:
        return

    lock = threading.Lock()
    state = {"done": 0, "downloaded": 0, "ocr_ok": 0, "ocr_chars": 0,
             "ocr_err": 0, "first_err": ""}

    def _one(item) -> None:
        name, link_url, img_url, page_label = item
        if jobs.is_cancelled(jid):
            return
        tmp, dl_reason = _download_image(img_url)
        try:
            if tmp is None:
                with lock:
                    if not state["first_err"]:
                        state["first_err"] = f"image download failed — {dl_reason}"
            else:
                with lock:
                    state["downloaded"] += 1
                try:
                    text = read_page(tmp)
                    with lock:
                        state["ocr_ok"] += 1
                        state["ocr_chars"] += len(text)
                except Exception as exc:
                    logger.warning("live epaper read failed %s: %s", img_url, exc)
                    text = ""
                    with lock:
                        state["ocr_err"] += 1
                        if not state["first_err"]:
                            state["first_err"] = f"OCR error: {type(exc).__name__}: {str(exc)[:160]}"
                labels = _match_labels(text, keywords)
                if labels:
                    jobs.add_result(jid, {
                        "module": "epaper",
                        "source": name,
                        "title": f"{name} — {page_label}",
                        # Open the exact page (the viewer at that page, or the scan
                        # itself); the scan image is the click-to-zoom preview.
                        "url": link_url,
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

    # Surface WHY e-paper found nothing, so a silent 0 is never a mystery.
    job = jobs.get(jid)
    epaper_hits = sum(1 for r in (job.results if job else []) if r.get("module") == "epaper")
    if epaper_hits == 0:
        if state["downloaded"] == 0:
            jobs.set_note(jid, f"Couldn't download any of the {len(candidates)} e-paper page "
                               f"images — {state['first_err']}")
        elif state["ocr_err"] and state["ocr_ok"] == 0:
            jobs.set_note(jid, f"Downloaded pages but OCR failed on all of them — "
                               f"{state['first_err']}")
        elif state["ocr_ok"] and state["ocr_chars"] < 50 * state["ocr_ok"]:
            jobs.set_note(jid, "E-paper pages read but returned almost no text "
                               "(the vision model may not be transcribing).")
    logger.info("epaper done: pages=%d downloaded=%d ocr_ok=%d ocr_err=%d chars=%d",
                len(candidates), state["downloaded"], state["ocr_ok"],
                state["ocr_err"], state["ocr_chars"])


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
            from app.youtube.media_source import last_error
            reason = last_error() or stat["first_err"]
            jobs.set_note(jid, f"Processed {len(candidates)} video(s) but couldn't get audio for "
                               f"any — {reason}")
        elif stat["trans_ok"] == 0:
            jobs.set_note(jid, f"Got audio but transcription failed on all "
                               f"{stat['audio_ok']} video(s) — {stat['first_err']}")
        else:
            jobs.set_note(jid, f"Transcribed {stat['trans_ok']} video(s) "
                               f"({stat['trans_chars']} chars) but your keyword wasn't spoken.")
    logger.info("youtube done: videos=%d audio_ok=%d trans_ok=%d chars=%d",
                len(candidates), stat["audio_ok"], stat["trans_ok"], stat["trans_chars"])

    _annotate_sentiment(jid)          # one request tags every result
