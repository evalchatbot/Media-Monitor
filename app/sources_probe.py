"""Probe a candidate newspaper or e-paper URL and report a short capability summary.

Does not wire the source into scrapers — only checks reachability and structure
(article links for websites; clickable imagemap vs image-only for e-papers).
"""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from config import settings

logger = logging.getLogger(__name__)

_UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")}

_CUSTOM_PATH = settings.storage_dir.parent / "custom_sources.json"


def custom_sources() -> list[dict]:
    if not _CUSTOM_PATH.exists():
        return []
    try:
        data = json.loads(_CUSTOM_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_custom_source(entry: dict) -> None:
    rows = custom_sources()
    name = (entry.get("name") or "").strip()
    rows = [r for r in rows if (r.get("name") or "").casefold() != name.casefold()]
    rows.append(entry)
    _CUSTOM_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CUSTOM_PATH.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")


def remove_custom_source(name: str) -> bool:
    rows = custom_sources()
    keep = [r for r in rows if (r.get("name") or "").casefold() != name.strip().casefold()]
    if len(keep) == len(rows):
        return False
    _CUSTOM_PATH.write_text(json.dumps(keep, indent=2, ensure_ascii=False), encoding="utf-8")
    return True


def probe(kind: str, url: str) -> dict:
    """Return {ok, summary, detail, kind, url, ...} for UI display."""
    kind = (kind or "").strip().lower()
    url = (url or "").strip()
    if kind not in ("newspaper", "epaper"):
        return {"ok": False, "summary": "Choose newspaper or e-paper.", "detail": {}}
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "summary": "Enter a full http(s) URL.", "detail": {}}
    try:
        if kind == "newspaper":
            return _probe_newspaper(url)
        return _probe_epaper(url)
    except Exception as exc:
        logger.warning("probe failed %s %s: %s", kind, url, exc)
        return {"ok": False, "summary": f"Could not check — {exc}", "detail": {}}


def _fetch(url: str) -> tuple[int, str, str]:
    r = httpx.get(url, headers=_UA, timeout=25, follow_redirects=True)
    return r.status_code, str(r.url), r.text


def _probe_newspaper(url: str) -> dict:
    status, final, text = _fetch(url)
    if status >= 400:
        return {"ok": False, "summary": f"Link failed (HTTP {status}).",
                "detail": {"status": status, "final_url": final}}
    soup = BeautifulSoup(text, "lxml")
    host = urlparse(final).netloc.lower()
    hrefs: list[str] = []
    for a in soup.find_all("a", href=True):
        href = urljoin(final, a["href"].strip())
        if not href.startswith("http"):
            continue
        if urlparse(href).netloc.lower() != host:
            continue
        path = urlparse(href).path or "/"
        if path in ("/",) or path.count("/") < 2:
            continue
        if re.search(r"\.(css|js|jpg|jpeg|png|gif|svg|pdf|zip)(\?|$)", href, re.I):
            continue
        hrefs.append(href.split("#")[0])
    uniq = list(dict.fromkeys(hrefs))
    n = len(uniq)
    ok = n >= 3
    summary = (f"Works · ~{n} on-site article-like links found."
               if ok else
               f"Page loads (HTTP {status}) but few article links ({n}). "
               f"May need a custom scraper.")
    return {
        "ok": ok or status < 400,
        "summary": summary,
        "detail": {"status": status, "final_url": final, "article_links": n,
                   "samples": uniq[:5]},
        "kind": "newspaper",
        "url": final,
    }


def _probe_epaper(url: str) -> dict:
    status, final, text = _fetch(url)
    if status >= 400:
        return {"ok": False, "summary": f"Link failed (HTTP {status}).",
                "detail": {"status": status, "final_url": final}}

    areas = re.findall(r"<area\b[^>]*>", text, re.I)
    clickable = 0
    for tag in areas:
        if re.search(r'href\s*=\s*["\'][^"\']+["\']', tag, re.I) and \
           re.search(r'coords\s*=\s*["\'][\d.,\s]+["\']', tag, re.I):
            clickable += 1

    imgs = 0
    for m in re.finditer(r'<img\b[^>]*src\s*=\s*["\']([^"\']+)["\']', text, re.I):
        src = m.group(1).lower()
        if any(k in src for k in ("page", "epaper", "edition", ".jpg", ".jpeg", ".png", "scan")):
            imgs += 1

    if clickable >= 3:
        summary = (f"Works · clickable cutouts (imagemap) — {clickable} linked regions. "
                   f"Exact clips possible like Jang/The News.")
        mode = "imagemap"
    elif clickable:
        summary = (f"Works · partial imagemap ({clickable} linked regions). "
                   f"May need vision for full coverage.")
        mode = "partial_imagemap"
    elif imgs or status < 400:
        summary = ("Works · image-only e-paper (no clickable cutouts). "
                   "Would need vision OCR to read & clip.")
        mode = "image_only"
    else:
        summary = "Page loads but no page images or cutout map found."
        mode = "unknown"

    return {
        "ok": True,
        "summary": summary,
        "detail": {"status": status, "final_url": final, "imagemap_regions": clickable,
                   "page_images": imgs, "mode": mode},
        "kind": "epaper",
        "url": final,
    }
