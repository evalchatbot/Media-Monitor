"""E-paper (print edition) page discovery — one adapter per publication.

Each adapter answers: "for this date and city, what are today's print pages and
where is the full-size scan of each?" Patterns were derived by probing the live
sites (see git history). All adapters are raise-safe: on any failure they log
and return [], so one broken paper never blocks the others.

Supported today:
  tribune    Express Tribune (EN)  — direct image listing (i.tribune.com.pk)
  express    Express Urdu (UR)     — Index.aspx per city (…-thumb.jpg -> full)
  nawaiwaqt  Nawa-i-Waqt (UR)      — /E-Paper/{city}/{date}/page-1 viewer
  jang       Jang (UR)             — e.jang.com.pk static_pages probe
  thenews    The News (EN)         — e.thenews.pk (same platform as Jang)
  dawn       Dawn (EN)             — e.dawn.com direct page scans

Not available: Dunya (broken TLS on e.dunya.com.pk), ARY/Geo (TV channels — no
print edition).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date as _date

import httpx

from config import settings

logger = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
_H = {"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"}


@dataclass
class EPage:
    paper: str        # slug ("jang")
    source: str       # display name ("Jang")
    city: str
    date: str         # YYYY-MM-DD
    page_no: int
    image_url: str    # full-size scan
    viewer_url: str   # human-facing link


def _get(url: str, timeout: int = 30) -> str:
    r = httpx.get(url, headers=_H, timeout=timeout, follow_redirects=True)
    r.raise_for_status()
    return r.text


# ---------------------------------------------------------------- tribune ---
def _tribune(d: _date, city: str) -> list[EPage]:
    """Express Tribune (English). tribune.com.pk/epaper lists every page's
    thumbnail at i.tribune.com.pk/epaper/{City}/{YYYYMMDD}/thumbnails/{id}.jpg;
    the full-size scan is the same URL without /thumbnails/."""
    html = _get("https://tribune.com.pk/epaper")
    ymd = d.strftime("%Y%m%d")
    thumbs = re.findall(
        rf'(https://i\.tribune\.com\.pk/epaper/(\w+)/{ymd}/thumbnails/[a-f0-9]+\.jpg)', html
    )
    pages, seen = [], set()
    for url, ed_city in thumbs:
        if url in seen:
            continue
        seen.add(url)
        pages.append(EPage(
            paper="tribune", source="Express Tribune", city=ed_city.lower(),
            date=d.isoformat(), page_no=len(pages) + 1,
            image_url=url.replace("/thumbnails/", "/"),
            viewer_url="https://tribune.com.pk/epaper",
        ))
    return pages


# ---------------------------------------------------------------- express ---
_EXPRESS_ISSUES = {"lahore": "NP_LHE", "karachi": "NP_KHI", "islamabad": "NP_ISB",
                   "faisalabad": "NP_FSB", "multan": "NP_MUL", "peshawar": "NP_PEW"}


def _express(d: _date, city: str) -> list[EPage]:
    """Express Urdu (Roznama Express). Per-city index page lists each page as
    /images/{ISSUE}/{YYYYMMDD}/{YYYYMMDD}-{ISSUE}-{Name}_{N}-thumb.jpg; the
    full-size scan is the same path without -thumb."""
    issue = _EXPRESS_ISSUES.get(city.lower(), "NP_LHE")
    viewer = f"https://www.express.com.pk/epaper/Index.aspx?Issue={issue}"
    html = _get(viewer)
    ymd = d.strftime("%Y%m%d")
    thumbs = re.findall(rf'["\'](/images/{issue}/{ymd}/[^"\']*-thumb\.jpg)["\']', html, re.I)
    pages, seen = [], set()
    for t in thumbs:
        full = "https://www.express.com.pk" + t.replace("-thumb", "")
        if full in seen:
            continue
        seen.add(full)
        # Number sequentially in listing (reading) order — the filename's _N
        # suffix repeats across sections (e.g. Editorial_6 AND Commerce_6),
        # which would collide with the (paper, city, date, page_no) unique key.
        pages.append(EPage(
            paper="express", source="Express Urdu", city=city.lower(),
            date=d.isoformat(), page_no=len(pages) + 1,
            image_url=full, viewer_url=viewer,
        ))
    return pages


# -------------------------------------------------------------- nawaiwaqt ---
def _nawaiwaqt(d: _date, city: str) -> list[EPage]:
    """Nawa-i-Waqt (Urdu). The page-1 viewer carries small thumbnails of every
    page in order at epaper_image/small/{date}/{City}/epaper_img_{id}.jpg; the
    full-size scan swaps small -> large."""
    ds = d.isoformat()
    viewer = f"https://www.nawaiwaqt.com.pk/E-Paper/{city.lower()}/{ds}/page-1"
    html = _get(viewer)
    small = re.findall(
        rf'["\'](https://www\.nawaiwaqt\.com\.pk/epaper_image/small/{ds}/\w+/epaper_img_\d+\.jpg)["\']',
        html,
    )
    pages, seen = [], set()
    for s in small:
        full = s.replace("/small/", "/large/")
        if full in seen:
            continue
        seen.add(full)
        n = len(pages) + 1
        pages.append(EPage(
            paper="nawaiwaqt", source="Nawa-i-Waqt", city=city.lower(),
            date=ds, page_no=n, image_url=full,
            viewer_url=f"https://www.nawaiwaqt.com.pk/E-Paper/{city.lower()}/{ds}/page-{n}",
        ))
    return pages


# ------------------------------------------------------- jang / thenews -----
def _probe_image(url: str, min_bytes: int = 30_000) -> bool:
    """True if `url` serves a real page scan (an image of meaningful size).
    Jang-family hosts answer 200 for MISSING pages too (a soft-404 HTML/tiny
    placeholder), so status alone can't be trusted — check type + size."""
    try:
        r = httpx.get(url, headers={**_H, "Range": "bytes=0-2047"},
                      timeout=20, follow_redirects=True)
    except Exception:
        return False
    if r.status_code not in (200, 206):
        return False
    if not r.headers.get("content-type", "").lower().startswith("image/"):
        return False
    total = r.headers.get("content-range", "").rpartition("/")[2] or r.headers.get("content-length")
    if total and total.isdigit() and int(total) < min_bytes:
        return False
    return True


def _jang_family(d: _date, city: str, *, host: str, paper: str, source: str,
                 default_city: str) -> list[EPage]:
    """Jang-group platform (e.jang.com.pk and e.thenews.com.pk are the same
    codebase): page scans live at a constructable URL —
      https://{host}/static_pages/{M-D-YYYY}/{city}/mainpage/page{N}.jpg
    so we probe page1..N until the pages run out."""
    c = (city or default_city).lower()
    mdy = f"{d.month}-{d.day}-{d.year}"          # no leading zeros
    ddmmyyyy = d.strftime("%d-%m-%Y")            # the human viewer's date form
    img_base = f"https://{host}/static_pages/{mdy}/{c}/mainpage"

    if not _probe_image(f"{img_base}/page1.jpg"):
        if c != default_city:  # e.g. a city this edition doesn't print in
            c = default_city
            img_base = f"https://{host}/static_pages/{mdy}/{c}/mainpage"
            if not _probe_image(f"{img_base}/page1.jpg"):
                return []
        else:
            return []

    # Pages upload progressively through the morning and some numbers 404 while
    # neighbours exist — so probe EVERY number up to the cap and keep the hits.
    pages = []
    for n in range(1, settings.epaper_max_pages + 1):
        url = f"{img_base}/page{n}.jpg"
        if n > 1 and not _probe_image(url):
            continue
        pages.append(EPage(
            paper=paper, source=source, city=c, date=d.isoformat(),
            page_no=n, image_url=url,
            viewer_url=f"https://{host}/{c}/{ddmmyyyy}/page{n}",
        ))
    return pages


def _jang(d: _date, city: str) -> list[EPage]:
    return _jang_family(d, city, host="e.jang.com.pk", paper="jang",
                        source="Jang", default_city="lahore")


def _thenews(d: _date, city: str) -> list[EPage]:
    # NB: the working host is e.thenews.pk — e.thenews.COM.pk is an empty shell.
    return _jang_family(d, city, host="e.thenews.pk", paper="thenews",
                        source="The News", default_city="karachi")


# ------------------------------------------------------------------- dawn ---
def _dawn(d: _date, city: str) -> list[EPage]:
    """Dawn (English). The epaper.dawn.com viewer is a JS app, but the page
    scans behind it are directly fetchable (found via the viewer's network
    traffic): https://e.dawn.com/{YYYY}/{MM}/{DD}/pages/{DD_MM_YYYY}_{NNN}.jpg
    — so probe page numbers just like the Jang family."""
    stamp = d.strftime("%d_%m_%Y")
    base = f"https://e.dawn.com/{d:%Y}/{d:%m}/{d:%d}/pages"
    pages = []
    for n in range(1, settings.epaper_max_pages + 1):
        url = f"{base}/{stamp}_{n:03d}.jpg"
        if not _probe_image(url):
            continue
        pages.append(EPage(
            paper="dawn", source="Dawn", city="national", date=d.isoformat(),
            page_no=n, image_url=url,
            viewer_url=f"https://epaper.dawn.com/?page={stamp}_{n:03d}",
        ))
    return pages


# -------------------------------------------------------------- jehan -------
_JEHAN_CITIES = ["karachi", "lahore", "islamabad", "multan", "gujranwala"]


def _jehan(d: _date, city: str) -> list[EPage]:
    """Jehan Pakistan (Urdu). Full page scans sit at a constructable URL
      /epaper/epaper/{city}/{DDMMYY}/p{N}.jpg
    so we probe page1..N until they run out, like the Jang/Dawn adapters. (The
    epaper.php index only carries low-res thumbnails.)"""
    ddmmyy = d.strftime("%d%m%y")
    tried, order = set(), [(city or "karachi").lower()] + _JEHAN_CITIES
    for c in order:
        if c in tried:
            continue
        tried.add(c)
        base = f"https://jehanpakistan.com/epaper/epaper/{c}/{ddmmyy}"
        if not _probe_image(f"{base}/p1.jpg"):
            continue
        index = f"https://jehanpakistan.com/epaper/epaper.php?edition={c}&date={ddmmyy}"
        pages = []
        for n in range(1, settings.epaper_max_pages + 1):
            url = f"{base}/p{n}.jpg"
            if n > 1 and not _probe_image(url):
                continue
            pages.append(EPage(
                paper="jehanpakistan", source="Jehan Pakistan", city=c,
                date=d.isoformat(), page_no=n, image_url=url, viewer_url=index,
            ))
        if pages:
            return pages
    return []


# -------------------------------------------------------------- dunya -------
# e.dunya.com.pk presents a broken TLS chain (missing intermediate), so every
# request to it skips verification. The edition download in the pipeline does
# the same for this host.
_DUNYA_EDITIONS = {"lahore": "LHR", "karachi": "KCH", "islamabad": "ISL",
                   "gujranwala": "GUJ", "multan": "MUL", "faisalabad": "FAB"}


def _dunya(d: _date, city: str) -> list[EPage]:
    """Dunya (Urdu). The edition at e.dunya.com.pk/index.php?e_name={CODE} lists
    the day's page scans under /news/{YYYY}/{Month}/{YYYY-MM-DD}/{CODE}/…jpg."""
    code = _DUNYA_EDITIONS.get((city or "lahore").lower(), "LHR")
    url = f"https://e.dunya.com.pk/index.php?e_name={code}"
    try:
        r = httpx.get(url, headers=_H, timeout=30, follow_redirects=True, verify=False)
        r.raise_for_status()
        html = r.text
    except Exception as exc:
        logger.warning("dunya: index fetch failed: %s", exc)
        return []
    ds = d.isoformat()
    imgs = re.findall(rf"(/news/\d{{4}}/\w+/{ds}/{code}/[\w/]+?/[\w.-]+\.jpg)", html)
    seen, pages = set(), []
    for path in imgs:
        if path in seen:
            continue
        seen.add(path)
        pages.append(EPage(
            paper="dunya", source="Dunya", city=code.lower(), date=ds,
            page_no=len(pages) + 1,
            image_url="https://e.dunya.com.pk" + path, viewer_url=url,
        ))
    return pages


SOURCES: dict[str, tuple] = {
    # slug: (display name, language, adapter)
    "dawn": ("Dawn", "en", _dawn),
    "tribune": ("Express Tribune", "en", _tribune),
    "thenews": ("The News", "en", _thenews),
    "jang": ("Jang", "ur", _jang),
    "express": ("Express Urdu", "ur", _express),
    "nawaiwaqt": ("Nawa-i-Waqt", "ur", _nawaiwaqt),
    "jehanpakistan": ("Jehan Pakistan", "ur", _jehan),
    "dunya": ("Dunya", "ur", _dunya),
}

# Papers in the monitoring set with no e-paper we can fetch (shown in the UI so
# the coverage story is explicit rather than silently absent).
UNSUPPORTED: dict[str, str] = {
    "ary": "ARY News — TV channel, no print edition",
}


def list_pages(slug: str, d: _date | None = None, city: str | None = None) -> list[EPage]:
    """All pages of `slug`'s edition for the date (default today, PKT).
    Raise-safe: returns [] on any failure."""
    from datetime import datetime, timedelta, timezone

    d = d or datetime.now(timezone(timedelta(hours=5))).date()
    if d < settings.monitor_since_date:
        logger.info("e-paper %s: %s predates the monitoring window (%s) — skipped",
                    slug, d.isoformat(), settings.monitor_since)
        return []
    city = city or settings.epaper_city
    entry = SOURCES.get(slug)
    if not entry:
        return []
    _, _, fn = entry
    try:
        pages = fn(d, city)[: settings.epaper_max_pages]
        logger.info("e-paper %s: %d pages for %s", slug, len(pages), d.isoformat())
        return pages
    except Exception as exc:
        logger.warning("e-paper %s: listing failed for %s: %s", slug, d.isoformat(), exc)
        return []


def enabled_slugs() -> list[str]:
    from app import sources_probe

    only = {s.strip() for s in settings.epaper_papers.split(",") if s.strip()}
    hidden = sources_probe.hidden_papers()   # display names the user removed
    slugs = [s for s in SOURCES if SOURCES[s][0].strip().casefold() not in hidden]
    return [s for s in slugs if s in only] if only else slugs
