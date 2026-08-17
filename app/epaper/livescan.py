"""Live e-paper scanning: read today's full print edition and clip every match.

This is the on-demand engine behind "Search live results". Nothing is persisted
to the database; page text lives in a short-lived in-process cache and clipping
images are written to a per-job folder that is deleted when the job is evicted.

Two tiers, chosen per paper — the difference is enormous, so we always take the
better one when it exists:

  TIER 1 — CLICKABLE (Jang, The News, Nawa-i-Waqt)
      The publisher ships an HTML image-map: one region per article with its
      exact polygon and a link to that article's own page. We match keywords
      against the REAL article text (no OCR at all, correct Urdu), and on a hit
      we open the article page in a browser, highlight the keyword in the live
      DOM and screenshot it — exactly the artefact a press desk wants.
      Cost: zero LLM calls. A page with no match never downloads its scan.

  TIER 2 — IMAGE ONLY (Dawn, Tribune, Express Urdu, Jehan, Dunya)
      No map, so the page scan is read by a vision model, guarded against
      degenerate output, then matched. On a hit the matched item is located on
      the page and cut out, with the keyword outlined inside the cutout.

Speed comes from doing as little as possible: Tier 1 short-circuits before any
image is fetched, every page of every paper is processed concurrently, and both
the region text and the OCR text are cached per (paper, date, page) so a second
search for a different keyword costs nothing.
"""
from __future__ import annotations

import logging
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date as _date
from pathlib import Path

import httpx

from config import settings

from app.core.keywords import find_matches
from app.epaper import imagemap, reader, sources

logger = logging.getLogger(__name__)

_UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
       "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
       "Accept-Language": "en-US,en;q=0.9,ur;q=0.8"}

# Pages read concurrently. Tier 1 is pure HTTP so it scales freely; Tier 2 is
# gated by the vision provider's rate limit, hence the smaller pool.
MAP_WORKERS = 6
OCR_WORKERS = 4

# A broad term ("پاکستان") legitimately appears in most articles of an edition.
# Without caps one click would cut out and screenshot every article on every
# page — minutes of work for a wall of near-identical cards. Matching itself is
# never capped, so the counts we report stay honest; only the expensive
# artefacts are bounded.
MAX_HITS_PER_PAGE = 4
MAX_HITS_PER_SOURCE = 24
# Screenshots are the single most expensive artefact (a real browser navigation
# each). Past this many, a hit still gets its exact cutout — just no live shot.
MAX_SHOTS_PER_SOURCE = max(0, settings.epaper_max_shots)
# Browsers taking those screenshots at once — the main memory lever on a small
# host, since each worker runs its own Chromium.
SHOT_WORKERS = max(1, settings.epaper_shot_workers)

# Where per-job clipping images land. Served via the app's /media mount and
# removed with the job.
LIVE_ROOT = settings.storage_dir / "live"


# ---------------------------------------------------------------- caching ----
# (paper, date, page_no) -> {"regions": [Region]|None, "text": str, "reason": str}
_PAGE_CACHE: dict[tuple, dict] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 3600          # one edition-day of reuse is plenty
_CACHE_MAX = 400


def _cache_get(key):
    with _CACHE_LOCK:
        hit = _PAGE_CACHE.get(key)
        if hit and time.time() - hit["at"] < _CACHE_TTL:
            return hit
        _PAGE_CACHE.pop(key, None)
        return None


def _cache_put(key, value):
    with _CACHE_LOCK:
        value["at"] = time.time()
        _PAGE_CACHE[key] = value
        if len(_PAGE_CACHE) > _CACHE_MAX:
            for k in sorted(_PAGE_CACHE, key=lambda k: _PAGE_CACHE[k]["at"])[:80]:
                _PAGE_CACHE.pop(k, None)


_swept_at = 0.0


def job_dir(job_id: str) -> Path:
    global _swept_at
    d = LIVE_ROOT / job_id
    d.mkdir(parents=True, exist_ok=True)
    # Opportunistic sweep, at most once every 10 minutes.
    if time.time() - _swept_at > 600:
        _swept_at = time.time()
        sweep_orphans()
    return d


def cleanup_job(job_id: str) -> None:
    """Delete a finished job's clipping images."""
    try:
        shutil.rmtree(LIVE_ROOT / job_id, ignore_errors=True)
    except Exception:
        pass


def sweep_orphans(max_age_seconds: int = 3600) -> int:
    """Delete clipping folders left behind by jobs this process doesn't know.

    Per-job cleanup only fires when a LATER job triggers eviction, so a restart
    (or simply no further searches) strands every folder from the previous run.
    On a hosted volume that grows without bound, so sweep at startup and again
    whenever a new job begins. Returns the number of folders removed.
    """
    removed = 0
    try:
        if not LIVE_ROOT.exists():
            return 0
        cutoff = time.time() - max_age_seconds
        for d in LIVE_ROOT.iterdir():
            try:
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
                    removed += 1
            except Exception:
                continue
    except Exception as exc:  # pragma: no cover - never break a scan
        logger.debug("live storage sweep failed: %s", exc)
    if removed:
        logger.info("live storage: swept %d orphaned clipping folder(s)", removed)
    return removed


def media_url(path: str | Path | None) -> str:
    """Map an on-disk clipping to its /media URL."""
    if not path:
        return ""
    try:
        return "/media/" + Path(path).resolve().relative_to(
            settings.storage_dir.resolve()).as_posix()
    except Exception:
        return ""


# ------------------------------------------------------------------ images ---
def _download(url: str, dest: Path) -> tuple[Path | None, str]:
    """Fetch a page scan to `dest`. Returns (path, "") or (None, reason)."""
    verify = "e.dunya.com.pk" not in url          # broken TLS chain on that host
    try:
        with httpx.stream("GET", url, headers=_UA, timeout=60,
                          follow_redirects=True, verify=verify) as r:
            if r.status_code >= 400:
                return None, f"HTTP {r.status_code}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            size = 0
            with open(dest, "wb") as fh:
                for chunk in r.iter_bytes():
                    fh.write(chunk)
                    size += len(chunk)
        if size < 15000:
            dest.unlink(missing_ok=True)
            return None, f"response too small ({size} bytes) — likely a block page"
        return dest, ""
    except Exception as exc:
        return None, f"{type(exc).__name__}: {str(exc)[:120]}"


# ------------------------------------------------------------------ tier 1 ---
def _tier1_page(pg, keywords) -> dict:
    """Read one clickable page's regions (cached) and return keyword hits.

    Returns {"ok": bool, "reason": str, "hits": [(Region, [labels])]}
    """
    key = (pg.paper, pg.date, pg.page_no)
    cached = _cache_get(key)
    if cached is None:
        # No scan dimensions yet, deliberately: matching runs on TEXT, so a page
        # with no hit never costs an image request at all. Regions keep their raw
        # coords and the box is re-derived against the real scan at crop time.
        res = imagemap.extract(pg.paper, pg.viewer_url, 0, 0)
        if not res:
            cached = {"regions": None, "reason": "no article regions on this page"}
        else:
            regions, _text = res
            cached = {"regions": regions, "reason": ""}
        _cache_put(key, cached)

    regions = cached.get("regions")
    if not regions:
        return {"ok": False, "reason": cached.get("reason") or "no regions", "hits": []}

    hits = []
    for reg in regions:
        if not reg.text:
            continue
        m = find_matches(reg.text, keywords)
        if m:
            hits.append((reg, sorted({x.keyword for x in m})))
    return {"ok": True, "reason": "", "hits": hits}


# ------------------------------------------------------------------ tier 2 ---
def _tier2_page(pg, keywords, workdir: Path, language: str) -> dict:
    """OCR one image-only page (cached) and return keyword hits + the scan path.

    Returns {"ok", "reason", "labels", "text", "scan"}
    """
    key = (pg.paper, pg.date, pg.page_no)
    scan = workdir / f"{pg.paper}_{pg.date}_p{pg.page_no:03d}.jpg"

    cached = _cache_get(key)
    if cached is None or not cached.get("text"):
        if not scan.exists():
            got, why = _download(pg.image_url, scan)
            if got is None:
                cached = {"text": "", "reason": f"page image download failed — {why}"}
                _cache_put(key, cached)
                return {"ok": False, "reason": cached["reason"], "labels": [],
                        "text": "", "scan": None}
        try:
            text = reader.read_page(scan, language=language)
            cached = {"text": text, "reason": ""}
        except Exception as exc:
            cached = {"text": "", "reason": str(exc)[:200]}
        _cache_put(key, cached)

    text = cached.get("text") or ""
    if not text:
        return {"ok": False, "reason": cached.get("reason") or "page unreadable",
                "labels": [], "text": "", "scan": scan if scan.exists() else None}

    m = find_matches(text, keywords)
    return {"ok": True, "reason": "", "labels": sorted({x.keyword for x in m}) if m else [],
            "text": text, "scan": scan if scan.exists() else None}


# -------------------------------------------------------------- screenshots --
# First selector that exists AND holds real text. Returned as a selector string
# so the highlighter (which takes a selector) and the clipper agree on scope.
_PICK_SCOPE_JS = r"""(sels) => {
  for (const sel of sels) {
    const el = document.querySelector(sel);
    if (el && (el.innerText || '').trim().length >= 80) return sel;
  }
  return '';
}"""

# The document-space rectangle the element's TEXT actually occupies. Using the
# element's own rect breaks when floats collapse it; unioning the boxes of its
# text-bearing descendants gives the real extent of the story.
_CONTENT_RECT_JS = r"""(sel) => {
  const root = document.querySelector(sel);
  if (!root) return null;
  const sx = window.scrollX, sy = window.scrollY;
  let l = Infinity, t = Infinity, r = -Infinity, b = -Infinity;
  const consider = (n) => {
    const q = n.getBoundingClientRect();
    if (q.width < 2 || q.height < 2) return;
    l = Math.min(l, q.left); t = Math.min(t, q.top);
    r = Math.max(r, q.right); b = Math.max(b, q.bottom);
  };
  consider(root);
  root.querySelectorAll('*').forEach(n => {
    // Skip invisible nodes; include anything that paints text or an image.
    const st = getComputedStyle(n);
    if (st.display === 'none' || st.visibility === 'hidden') return;
    if ((n.innerText || '').trim() || n.tagName === 'IMG') consider(n);
  });
  if (!isFinite(l) || r <= l || b <= t) return null;
  // Nastaliq ascenders paint well above the CSS line box, so a tight top edge
  // shears the first line. Vertical padding is deliberately generous.
  const padX = 14, padY = 34;
  const x = Math.max(0, l + sx - padX), y = Math.max(0, t + sy - padY);
  const w = Math.min(r - l + padX * 2, document.documentElement.scrollWidth - x);
  // Very long columns would produce an unusable strip; cap the height.
  const h = Math.min(b - t + padY * 2, 4000);
  if (w < 80 || h < 80) return null;
  return {x: Math.round(x), y: Math.round(y), width: Math.round(w),
          height: Math.round(h)};
}"""


def _shoot_batch(batch: list[dict], out_dir: Path) -> None:
    """Screenshot one batch of articles in a single browser (one thread)."""
    from app.scrapers.base import _HIGHLIGHT_JS, _USER_AGENT

    pw = browser = None
    try:
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        browser = pw.chromium.launch(args=[
            "--disable-dev-shm-usage", "--disable-gpu", "--disable-extensions",
            "--disable-background-networking",
        ])
        ctx = browser.new_context(
            user_agent=_USER_AGENT,
            viewport={"width": 1100, "height": 1400},
            device_scale_factor=2, locale="en-US")
        for item in batch:
            page = ctx.new_page()
            try:
                page.goto(item["url"], wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(700)

                # Aim at the story container, not the page. These detail pages
                # carry a "more news" sidebar whose links often contain the
                # keyword too; highlighting body-wide scrolls to the sidebar and
                # the shot shows a list of links instead of the matched story.
                # Pick the story container and clip to the box its CONTENT
                # actually occupies. Two traps this avoids: a container whose
                # floated children collapse it to a sliver (Jang's
                # div.detail-content renders 66px tall while holding the whole
                # story), and a container wide enough to swallow the "more news"
                # sidebar, where the same keyword often appears first.
                scope = page.evaluate(_PICK_SCOPE_JS,
                                      list(item.get("selectors") or ()))
                try:
                    page.evaluate(_HIGHLIGHT_JS,
                                  {"kws": item["keywords"], "sel": scope or "body"})
                    page.wait_for_timeout(220)
                except Exception:
                    pass
                out = out_dir / item["out"]
                clip = page.evaluate(_CONTENT_RECT_JS, scope) if scope else None
                if clip:
                    page.screenshot(path=str(out), clip=clip, animations="disabled")
                else:
                    page.screenshot(path=str(out), animations="disabled")
                item["shot"] = str(out)
            except Exception as exc:
                logger.info("article shot failed %s: %s", item["url"],
                            type(exc).__name__)
            finally:
                page.close()
    except Exception as exc:
        logger.warning("article screenshot browser unavailable: %s", exc)
    finally:
        try:
            if browser:
                browser.close()
            if pw:
                pw.stop()
        except Exception:
            pass


def shoot_articles(jobs_list: list[dict], out_dir: Path) -> None:
    """Open each clickable article, highlight its keywords, screenshot it.

    `jobs_list` items are {"url", "keywords", "out"} and get "shot" filled in on
    success. This is the most expensive artefact in the whole scan — a real
    browser navigation each — so the batch is split across several browsers
    running at once. Playwright's sync API is not thread-safe across threads, so
    each worker starts its OWN playwright/browser rather than sharing one.
    """
    if not jobs_list:
        return
    n = min(SHOT_WORKERS, len(jobs_list))
    if n <= 1:
        _shoot_batch(jobs_list, out_dir)
        return
    batches = [jobs_list[i::n] for i in range(n)]
    with ThreadPoolExecutor(max_workers=n) as ex:
        list(ex.map(lambda b: _shoot_batch(b, out_dir), batches))


# ------------------------------------------------------------------- clips ---
def clip_region(scan: Path, box: dict, source: str, page_no: int, link: str,
                out: Path) -> str | None:
    """Crop an exact publisher region out of the page scan and stamp it."""
    from app.epaper import clip as _clip

    if not _clip._crop(scan, box, out, pad_pct=1.2):
        return None
    _clip._enhance(out)
    try:
        from app.scrapers.footer import add_footer
        add_footer(out, source, f"E-Paper p{page_no} · clipping", link)
    except Exception:
        pass
    return str(out)


def snippet(text: str, keywords, width: int = 200) -> str:
    from app.core.keywords import normalize

    for kw in keywords:
        for lang in ("en", "ur"):
            hay, needle = normalize(text, lang), normalize(kw, lang)
            idx = hay.find(needle)
            if idx != -1:
                start = max(0, idx - width // 2)
                end = min(len(hay), idx + len(needle) + width // 2)
                return (("…" if start else "") + hay[start:end].strip()
                        + ("…" if end < len(hay) else ""))
    return re.sub(r"\s+", " ", text)[:width].strip()


# ------------------------------------------------------------------ driver ---
def scan_source(*, job_id: str, name: str, slug: str | None, url: str,
                keywords: list[tuple[str, str]], day: _date,
                emit, progress, cancelled, stats: dict) -> None:
    """Scan ONE e-paper source's full edition for `day`, emitting result cards.

    `emit(card)` is called per match, `progress(**fields)` for the UI, and
    `cancelled()` is polled so the user can stop a long run.
    """
    if slug is None:
        stats.setdefault("notes", []).append(
            f"{name}: not a recognised e-paper site — add the publisher's own "
            f"e-paper link (e.g. e.jang.com.pk) to read the full edition.")
        return

    pages = sources.list_pages(slug, day)
    if not pages:
        stats.setdefault("notes", []).append(
            f"{name}: no pages published for {day.isoformat()} yet.")
        return

    clickable = imagemap.supports(slug)
    lang = "ur" if (sources.SOURCES.get(slug) or ("", "en"))[1] == "ur" else "en"
    workdir = job_dir(job_id)

    stats["pages_total"] = stats.get("pages_total", 0) + len(pages)
    # Publish the denominator immediately — otherwise the UI shows "n/0" and its
    # progress bar stays indeterminate for the whole run.
    progress(total=stats["pages_total"], checked=stats.get("pages_done", 0))
    logger.info("live epaper %s: %d pages, tier=%s, lang=%s",
                slug, len(pages), "clickable" if clickable else "ocr", lang)

    # Tier 2 on an Urdu paper with no Nastaliq-capable provider would produce
    # confident nonsense. Say so instead of fabricating matches.
    if not clickable and lang == "ur" and not reader.can_read_urdu():
        stats.setdefault("notes", []).append(
            f"{name}: Urdu print pages need a Nastaliq-capable vision key. "
            f"Set GEMINI_API_KEY (free) — Groq cannot read Nastaliq, so this "
            f"paper was skipped rather than reported with false matches.")
        stats["skipped_urdu"] = stats.get("skipped_urdu", 0) + len(pages)
        return
    if not clickable and not reader.has_key():
        stats.setdefault("notes", []).append(
            f"{name}: reading print pages needs a vision API key (none set).")
        return

    shot_queue: list[dict] = []
    lock = threading.Lock()

    def _one(pg):
        if cancelled():
            return
        try:
            if clickable:
                _handle_clickable(pg, keywords, name, slug, workdir, emit,
                                  shot_queue, lock, stats)
            else:
                _handle_image_only(pg, keywords, name, slug, workdir, lang,
                                   emit, lock, stats)
        except Exception as exc:
            logger.warning("live epaper %s p%s failed: %s", slug, pg.page_no, exc)
            with lock:
                if not stats.get("first_err"):
                    stats["first_err"] = f"{type(exc).__name__}: {str(exc)[:140]}"
        finally:
            with lock:
                stats["pages_done"] = stats.get("pages_done", 0) + 1
                progress(checked=stats["pages_done"],
                         current=f"{name} · page {pg.page_no}")

    workers = MAP_WORKERS if clickable else OCR_WORKERS
    with ThreadPoolExecutor(max_workers=min(len(pages), workers)) as ex:
        list(ex.map(_one, pages))

    # Screenshots last, in one browser, for every clickable hit found above.
    if shot_queue and not cancelled():
        progress(current=f"{name} · capturing {len(shot_queue)} article shot(s)")
        shoot_articles(shot_queue, workdir)
        for item in shot_queue:
            if item.get("shot"):
                item["card"]["image"] = media_url(item["shot"])


def _handle_clickable(pg, keywords, name, slug, workdir, emit, shot_queue,
                      lock, stats) -> None:
    res = _tier1_page(pg, keywords)
    if not res["ok"]:
        with lock:
            stats["map_fail"] = stats.get("map_fail", 0) + 1
            if not stats.get("first_err"):
                stats["first_err"] = f"{name} p{pg.page_no}: {res['reason']}"
        return
    with lock:
        stats["pages_read"] = stats.get("pages_read", 0) + 1
    if not res["hits"]:
        return

    with lock:
        stats["matched_regions"] = stats.get("matched_regions", 0) + len(res["hits"])
        if stats.get("hits", 0) >= MAX_HITS_PER_SOURCE:
            stats["capped"] = True
            return
    hits = res["hits"][:MAX_HITS_PER_PAGE]
    if len(res["hits"]) > len(hits):
        with lock:
            stats["capped"] = True

    # Only now is the page scan worth downloading.
    scan = workdir / f"{slug}_{pg.date}_p{pg.page_no:03d}.jpg"
    if not scan.exists():
        got, why = _download(pg.image_url, scan)
        if got is None:
            logger.info("scan download failed for %s p%s: %s", slug, pg.page_no, why)
            scan = None
    scan_wh = (0, 0)
    if scan is not None:
        try:
            from PIL import Image
            with Image.open(scan) as im:
                scan_wh = im.size
        except Exception:
            scan = None

    for idx, (reg, labels) in enumerate(hits):
        cut = None
        if scan is not None:
            out = workdir / f"{slug}_{pg.date}_p{pg.page_no:03d}_r{idx}.jpg"
            cut = clip_region(scan, reg.box_for(*scan_wh), name, pg.page_no,
                              reg.detail_url or pg.viewer_url, out)
        card = {
            "module": "epaper",
            "source": name,
            "title": f"{name} — page {pg.page_no}",
            # The article's own page: this is what "open it" opens.
            "url": reg.detail_url or pg.viewer_url,
            "image": media_url(cut),
            "section": f"E-Paper · page {pg.page_no} · clickable",
            "snippet": snippet(reg.text, labels),
            "keywords": labels,
            "meta": "exact cutout",
        }
        emit(card)
        with lock:
            stats["hits"] = stats.get("hits", 0) + 1
            if reg.detail_url and len(shot_queue) < MAX_SHOTS_PER_SOURCE:
                shot_queue.append({
                    "url": reg.detail_url,
                    "keywords": labels,
                    "selectors": imagemap.article_selectors(slug),
                    "out": f"{slug}_{pg.date}_p{pg.page_no:03d}_r{idx}_shot.png",
                    "card": card,
                })


def _handle_image_only(pg, keywords, name, slug, workdir, lang, emit, lock,
                       stats) -> None:
    res = _tier2_page(pg, keywords, workdir, lang)
    if not res["ok"]:
        with lock:
            stats["ocr_fail"] = stats.get("ocr_fail", 0) + 1
            if not stats.get("first_err"):
                stats["first_err"] = f"{name} p{pg.page_no}: {res['reason']}"
        return
    with lock:
        stats["pages_read"] = stats.get("pages_read", 0) + 1
        stats["ocr_chars"] = stats.get("ocr_chars", 0) + len(res["text"])
    labels = res["labels"]
    if not labels or res["scan"] is None:
        return

    from app.epaper import clip as _clip

    snip = snippet(res["text"], labels)
    cut = None
    try:
        cut = _clip.make_clipping(res["scan"], labels[0], snip, name, pg.page_no,
                                  pg.viewer_url or pg.image_url, language=lang)
    except Exception as exc:
        logger.info("clip failed %s p%s: %s", slug, pg.page_no, exc)
    card = {
        "module": "epaper",
        "source": name,
        "title": f"{name} — page {pg.page_no}",
        "url": pg.viewer_url or pg.image_url,
        # The cutout if we could locate the item, else the whole page scan.
        "image": media_url(cut) or pg.image_url,
        "section": f"E-Paper · page {pg.page_no}",
        "snippet": snip,
        "keywords": labels,
        "meta": "cutout" if cut else "full page",
    }
    emit(card)
    with lock:
        stats["hits"] = stats.get("hits", 0) + 1
