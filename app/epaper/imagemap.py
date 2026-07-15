"""Extract e-paper articles from the publisher's own clickable image-map.

Some e-paper platforms overlay an HTML image-map on the page scan: one <area>
per article, whose polygon is the article's EXACT region and whose link points
at a detail page with the clean article text. That is publisher-provided
segmentation — pixel-perfect boxes and real text — so for these papers we skip
vision OCR/clipping entirely: crop the polygon straight off the page scan, and
match keywords on the detail text.

Supported (verified): Jang, The News (same Jang-group platform). Both serve the
map AND the detail links in STATIC html, so this is pure httpx — no browser,
no LLM, no rate limits. Other papers fall back to the vision pipeline.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")}

# paper -> (regex capturing a detail URL in an <area href>, detail-text selectors)
_PAPERS = {
    "jang": (r"https?://e\.jang\.com\.pk/detail/\d+",
             ("div.detail-page", "div.detail_view_content", "div.content-area")),
    "thenews": (r"https?://e\.thenews\.pk/detail\?id=\d+",
                ("div.story-detail", "div.detailsInner", "article")),
}


def supports(paper: str) -> bool:
    return paper in _PAPERS


@dataclass
class Region:
    box: dict   # {"l","t","r","b"} in percent of the page scan
    text: str   # clean article text ("" if the detail fetch failed)


def extract(paper: str, viewer_url: str, scan_w: int, scan_h: int
            ) -> tuple[list[Region], str] | None:
    """Return (regions, page_text) for a supported paper, or None on failure.
    `scan_w/scan_h` are the page-scan pixel dims the <area> coords map onto."""
    if paper not in _PAPERS or not (scan_w and scan_h):
        return None
    detail_re, selectors = _PAPERS[paper]
    try:
        html = httpx.get(viewer_url, headers=_UA, timeout=30, follow_redirects=True).text
    except Exception as exc:
        logger.warning("imagemap %s: page fetch failed %s: %s", paper, viewer_url, exc)
        return None

    seen: set[str] = set()
    regions: list[Region] = []
    for tag in re.findall(r"<area\b[^>]*>", html, re.I):
        href_m = re.search(detail_re, tag)
        coords_m = re.search(r'coords\s*=\s*["\']([\d.,\s]+)["\']', tag, re.I)
        if not (href_m and coords_m):
            continue
        href = href_m.group(0)
        if href in seen:
            continue
        seen.add(href)
        box = _coords_to_box(coords_m.group(1), scan_w, scan_h)
        if not box:
            continue
        regions.append(Region(box, _detail_text(href, selectors)))

    if not regions:
        logger.info("imagemap %s: no article regions on %s", paper, viewer_url)
        return None
    page_text = "\n\n".join(r.text for r in regions if r.text)
    logger.info("imagemap %s: %d regions, %d chars of text (%s)",
                paper, len(regions), len(page_text), viewer_url)
    return regions, page_text


def _coords_to_box(coords: str, w: int, h: int) -> dict | None:
    """Polygon/rect 'x1,y1,x2,y2,…' (page-pixel space) -> percent bounding box."""
    nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", coords)]
    if len(nums) < 4:
        return None
    xs, ys = nums[0::2], nums[1::2]
    l, r = max(0.0, min(xs)), min(float(w), max(xs))
    t, b = max(0.0, min(ys)), min(float(h), max(ys))
    if r - l < 25 or b - t < 25:          # header slivers / bad coords
        return None
    box = {"l": l / w * 100, "t": t / h * 100, "r": r / w * 100, "b": b / h * 100}
    if (box["r"] - box["l"]) * (box["b"] - box["t"]) > 92 * 100:   # whole page
        return None
    return box


def _detail_text(url: str, selectors: tuple[str, ...]) -> str:
    try:
        html = httpx.get(url, headers=_UA, timeout=25, follow_redirects=True).text
    except Exception:
        return ""
    soup = BeautifulSoup(html, "lxml")
    for junk in soup(["script", "style", "nav", "header", "footer", "form", "aside", "noscript"]):
        junk.decompose()
    for sel in selectors:
        el = soup.select_one(sel)
        if el:
            txt = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
            if len(txt) >= 60:
                return txt
    # Fallback: the single largest text block on the page.
    best = ""
    for d in soup.find_all(["div", "article", "section"]):
        t = d.get_text(" ", strip=True)
        if len(t) > len(best):
            best = t
    return re.sub(r"\s+", " ", best).strip()
