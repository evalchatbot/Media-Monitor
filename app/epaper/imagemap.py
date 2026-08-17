"""Extract e-paper articles from the publisher's own clickable image-map.

Several e-paper platforms overlay an HTML image-map on the page scan: one
`<area>` per article, whose polygon is that article's EXACT region and whose
link opens a detail page carrying the clean article text. That is
publisher-provided segmentation — pixel-perfect boxes and real, correctly
spelled text — so for these papers we skip vision OCR entirely.

This matters most for Urdu. Nastaliq OCR is the weakest link in the whole
system, and Jang / Nawa-i-Waqt are precisely the papers that hand us perfect
Urdu text for free. Verified live: Jang page 1 yields 36 regions and ~109k
characters of correct Urdu via pure httpx — no LLM, no key, no rate limit.

Supported:
  jang       e.jang.com.pk    detail link in <area href>
  thenews    e.thenews.pk     detail link in <area href>
  nawaiwaqt  nawaiwaqt.com.pk detail link in <area url=…> (href is javascript:)

Dunya also ships a map, but its detail page is a JS shell with no text, so it
stays on the vision path. Other papers have no map at all.
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")}

# Detail pages are independent GETs, so fetching them serially is the single
# biggest cost of this path: Jang's 36 regions took 44s one at a time and ~3s
# in parallel. Keep the pool modest so we stay a polite client.
_DETAIL_WORKERS = 12


@dataclass(frozen=True)
class _Paper:
    # How to recognise a detail link inside an <area> tag.
    detail_re: str
    # Resolved against this when the captured link is relative ("" = already absolute).
    detail_base: str
    # Selectors tried in order to pull the article TEXT out of the detail page.
    # These must isolate the story: every one of these pages ends with a "more
    # news" list of unrelated headlines, and including it would match keywords
    # from OTHER articles and report them against this one.
    selectors: tuple[str, ...]
    # Selectors for the SCREENSHOT target. Usually a slightly wider container
    # than the text one so the headline and byline are in frame.
    shot_selectors: tuple[str, ...] = ()


_PAPERS: dict[str, _Paper] = {
    "jang": _Paper(
        detail_re=r"https?://e\.jang\.com\.pk/detail/\d+",
        detail_base="",
        # Only div.detail_view_content is the story alone. div.detail-content
        # (and every wider container) also holds the "مزید خبریں" block, whose
        # unrelated headlines would both pollute the match and drag the
        # screenshot past an ad into a list of links.
        selectors=("div.detail_view_content",),
    ),
    "thenews": _Paper(
        detail_re=r"https?://e\.thenews\.pk/detail\?id=\d+",
        detail_base="",
        selectors=("div.story-detail", "article"),
        shot_selectors=("div.detailsInner", "div.story-detail"),
    ),
    "nawaiwaqt": _Paper(
        # href is javascript:void(0); the real target sits in a url="…" attribute
        # as a path relative to /E-Paper/, e.g. "lahore/2026-08-17/page-1/detail-3".
        detail_re=r'url\s*=\s*["\']([\w./-]*detail-\d+)["\']',
        detail_base="https://www.nawaiwaqt.com.pk/E-Paper/",
        selectors=("div.content-inner", "div.entry-content", "div.news-detail"),
        shot_selectors=("div.content-inner", "div.entry-content"),
    ),
}


def supports(paper: str) -> bool:
    return paper in _PAPERS


def article_selectors(paper: str) -> tuple[str, ...]:
    """CSS selectors to aim the screenshot at this paper's story container.

    These detail pages carry a "more news" sidebar and a keyword often appears
    there first, so highlighting page-wide scrolls to the sidebar and the shot
    frames a list of links instead of the matched story.
    """
    cfg = _PAPERS.get(paper)
    if not cfg:
        return ()
    # The TEXT selectors are the right scope: they isolate the story, and the
    # screenshot now clips to the box its content occupies rather than trusting
    # the container's rendered height — so a float-collapsed container is fine.
    return cfg.selectors


@dataclass
class Region:
    box: dict          # {"l","t","r","b"} as percent, in `space`
    text: str          # clean article text ("" if the detail fetch failed)
    detail_url: str    # the article's own page — what "open it" opens
    coords: str = ""   # raw <area coords>, kept so the box can be re-derived
    space: tuple = ()  # (w, h) the coords were interpreted against

    def box_for(self, w: int, h: int) -> dict:
        """This region's box as a percentage of a scan that is `w`x`h`.

        The caller usually learns the scan's true size only after downloading
        it — which happens after matching, and only for pages that actually hit.
        Re-deriving here means the match phase never has to fetch image headers
        just to place a box it may never draw.
        """
        if not self.coords or not (w and h):
            return self.box
        return _coords_to_box(self.coords, w, h) or self.box


def extract(paper: str, viewer_url: str, scan_w: int, scan_h: int
            ) -> tuple[list[Region], str] | None:
    """Return (regions, page_text) for a supported paper, or None on failure.

    `scan_w/scan_h` are the page-scan pixel dims used as a fallback coordinate
    space; when the map's own `<img usemap>` declares a size we prefer that,
    because the coords are authored against the DISPLAYED image, which is not
    always the scan we downloaded.
    """
    cfg = _PAPERS.get(paper)
    if cfg is None:
        return None
    try:
        html = httpx.get(viewer_url, headers=_UA, timeout=30,
                         follow_redirects=True).text
    except Exception as exc:
        logger.warning("imagemap %s: page fetch failed %s: %s", paper, viewer_url, exc)
        return None

    tags = re.findall(r"<area\b[^>]*>", html, re.I)
    if not tags:
        logger.info("imagemap %s: no <area> tags on %s", paper, viewer_url)
        return None

    space_w, space_h = _coord_space(html, tags, scan_w, scan_h)
    if not (space_w and space_h):
        return None

    seen: set[str] = set()
    staged: list[tuple[dict, str, str]] = []   # (box, detail_url, raw_coords)
    for tag in tags:
        link = _detail_link(tag, cfg)
        if not link or link in seen:
            continue
        coords_m = re.search(r'coords\s*=\s*["\']([\d.,\s-]+)["\']', tag, re.I)
        if not coords_m:
            continue
        box = _coords_to_box(coords_m.group(1), space_w, space_h)
        if not box:
            continue
        seen.add(link)
        staged.append((box, link, coords_m.group(1)))

    if not staged:
        logger.info("imagemap %s: no article regions on %s", paper, viewer_url)
        return None

    # Fetch every detail page at once — this is what makes the path fast.
    with ThreadPoolExecutor(max_workers=min(len(staged), _DETAIL_WORKERS)) as ex:
        texts = list(ex.map(lambda s: _detail_text(s[1], cfg.selectors), staged))

    regions = [Region(box=box, text=text, detail_url=link, coords=raw,
                      space=(space_w, space_h))
               for (box, link, raw), text in zip(staged, texts)]
    page_text = "\n\n".join(r.text for r in regions if r.text)
    logger.info("imagemap %s: %d regions, %d chars of text (%s)",
                paper, len(regions), len(page_text), viewer_url)
    return regions, page_text


def _detail_link(tag: str, cfg: _Paper) -> str | None:
    """Pull the article URL out of one <area> tag, absolutised."""
    m = re.search(cfg.detail_re, tag, re.I)
    if not m:
        return None
    # A capturing group means the pattern targets an attribute value; otherwise
    # the whole match IS the URL.
    raw = (m.group(1) if m.groups() else m.group(0)).strip()
    if not raw:
        return None
    if raw.startswith("http"):
        return raw
    return urljoin(cfg.detail_base, raw.lstrip("/")) if cfg.detail_base else None


def _coord_space(html: str, tags: list[str], scan_w: int, scan_h: int) -> tuple[int, int]:
    """The pixel space the <area> coords are expressed in.

    Prefer explicit width/height on the mapped <img>. Otherwise fall back to the
    downloaded scan's dims — but only if the coords actually fit inside them; a
    map authored against a larger display image would otherwise produce boxes
    that all clamp to the right/bottom edge.
    """
    img_m = re.search(r"<img\b[^>]*usemap[^>]*>", html, re.I)
    if img_m:
        w_m = re.search(r'\bwidth\s*=\s*["\']?(\d+)', img_m.group(0), re.I)
        h_m = re.search(r'\bheight\s*=\s*["\']?(\d+)', img_m.group(0), re.I)
        if w_m and h_m and int(w_m.group(1)) > 100 and int(h_m.group(1)) > 100:
            return int(w_m.group(1)), int(h_m.group(1))

    max_x = max_y = 0.0
    for tag in tags:
        m = re.search(r'coords\s*=\s*["\']([\d.,\s-]+)["\']', tag, re.I)
        if not m:
            continue
        nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", m.group(1))]
        if len(nums) >= 4:
            max_x = max(max_x, max(nums[0::2]))
            max_y = max(max_y, max(nums[1::2]))
    if not (max_x and max_y):
        return scan_w, scan_h
    # Coords fit the scan we hold → they're in its space.
    if scan_w and scan_h and max_x <= scan_w * 1.02 and max_y <= scan_h * 1.02:
        return scan_w, scan_h
    # They don't fit: the map was authored against a different render. Assume the
    # map spans essentially the full page and normalise by its own extents.
    logger.info("imagemap: coords exceed scan %sx%s (max %.0fx%.0f) — "
                "normalising by map extents", scan_w, scan_h, max_x, max_y)
    return int(max_x), int(max_y)


def _coords_to_box(coords: str, w: int, h: int) -> dict | None:
    """Polygon/rect 'x1,y1,x2,y2,…' in page-pixel space -> percent bounding box."""
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
    for junk in soup(["script", "style", "nav", "header", "footer", "form",
                      "aside", "noscript"]):
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
