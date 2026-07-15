"""Base scraper contract for the newspaper module.

Each publication subclasses `BaseScraper` and implements `list_articles()`.
The base handles cross-cutting concerns: robots.txt compliance, rendering pages
through a shared headless Chromium (Pakistani news sites sit behind bot
protection that blocks plain HTTP clients), and capturing full-page + cropped
article screenshots.

A single browser is launched lazily and reused for every fetch/screenshot in a
scan, then torn down via `close()` — far cheaper than relaunching per page.

Requires Chromium:  python -m playwright install chromium
"""
from __future__ import annotations

import logging
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# Bot identity we present to robots.txt (we still honour its rules).
_ROBOTS_AGENT = "MediaMonitorBot"


class ScrapeBlockedError(RuntimeError):
    """Raised when robots.txt disallows a target URL."""


@dataclass
class Article:
    """A candidate article discovered on a listing page."""

    source: str
    title: str
    url: str
    section: str | None = None
    body: str = ""
    published_at: datetime | None = None
    external_id: str = ""

    def __post_init__(self):
        if not self.external_id:
            self.external_id = self.url


@dataclass
class BaseScraper:
    """Subclass and set `name`, `base_url`, `sections`; implement list_articles()."""

    name: str = "base"
    base_url: str = ""
    sections: dict[str, str] = field(default_factory=dict)
    respect_robots: bool = True

    # Lazily-initialised Playwright handles (shared across the scan).
    _pw = None
    _browser = None
    _context = None
    _shot_context = None  # separate context: 2x scale + media allowed (screenshots only)
    _robots_cache: dict[str, urllib.robotparser.RobotFileParser] = field(default_factory=dict)

    # -- Browser lifecycle -----------------------------------------------
    def _ensure_browser(self):
        if self._context is not None:
            return
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        # Lean launch flags: heavy news pages spike Chromium memory; these keep
        # the footprint low so the browser doesn't destabilise a co-running
        # process (e.g. the web server) in memory-constrained environments.
        self._browser = self._pw.chromium.launch(
            args=[
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-extensions",
                "--disable-background-networking",
                "--js-flags=--max-old-space-size=384",
            ]
        )
        self._context = self._browser.new_context(
            user_agent=_USER_AGENT,
            viewport={"width": 1366, "height": 1600},
            locale="en-US",
        )
        # Block images/media/fonts: the biggest memory cost on news pages, and
        # unneeded for text extraction, keyword matching, or a readable text
        # screenshot. Cuts Chromium's footprint dramatically so it won't
        # destabilise a co-running process. Configurable via BLOCK_MEDIA_IN_SCANS.
        from config import settings

        if settings.block_media_in_scans:
            self._context.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in ("image", "media", "font")
                else route.continue_(),
            )

    def _ensure_shot_context(self):
        """Context used ONLY for screenshots: 2x pixel density for crisp text
        and media UNBLOCKED so articles look like the real page. Scans keep the
        lean media-blocked context; screenshots are few, so the extra weight is
        paid exactly where it buys quality."""
        if self._shot_context is not None:
            return
        self._ensure_browser()
        from config import settings

        self._shot_context = self._browser.new_context(
            user_agent=_USER_AGENT,
            viewport={"width": 1280, "height": 1500},
            device_scale_factor=max(1, settings.screenshot_scale),
            locale="en-US",
        )

    def close(self) -> None:
        """Tear down the browser. Always call in a finally after a scan."""
        try:
            if self._shot_context:
                self._shot_context.close()
            if self._context:
                self._context.close()
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:  # pragma: no cover
            pass
        finally:
            self._shot_context = self._context = self._browser = self._pw = None

    # -- robots.txt ------------------------------------------------------
    def _robots_allows(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._robots_cache:
            self._robots_cache[origin] = self._load_robots(origin)
        rp = self._robots_cache[origin]
        if rp is None:
            return True  # couldn't read robots.txt (e.g. bot-blocked) → proceed
        return rp.can_fetch(_ROBOTS_AGENT, url)

    def _load_robots(self, origin: str):
        """Fetch robots.txt with our browser UA and parse it.

        Python's RobotFileParser.read() uses a Python-urllib UA that these sites
        403 (bot protection); a 403 makes it disallow EVERYTHING — a false block.
        Fetching with the real UA and parsing the text avoids that. If we truly
        can't read the file, we return None (proceed) and log it.
        """
        import httpx

        try:
            resp = httpx.get(
                f"{origin}/robots.txt",
                headers={"User-Agent": _USER_AGENT},
                timeout=15,
                follow_redirects=True,
            )
        except Exception as exc:
            logger.warning("robots.txt fetch failed for %s (%s); proceeding", origin, exc)
            return None
        if resp.status_code >= 400:
            logger.info("robots.txt at %s returned %d; proceeding", origin, resp.status_code)
            return None
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(resp.text.splitlines())
        return rp

    # -- Rendering -------------------------------------------------------
    def render(self, url: str, wait_ms: int = 1500, retries: int = 2) -> str:
        """Return fully-rendered HTML for `url` via headless Chromium.

        Retries transient navigation failures (e.g. ERR_NETWORK_IO_SUSPENDED,
        timeouts) which these sites throw intermittently under load.
        """
        full = urljoin(self.base_url, url)
        if not self._robots_allows(full):
            raise ScrapeBlockedError(f"robots.txt disallows fetching {full}")
        self._ensure_browser()

        last_exc = None
        for attempt in range(1, retries + 1):
            page = self._context.new_page()
            try:
                page.goto(full, wait_until="domcontentloaded", timeout=45000)
                if wait_ms:
                    page.wait_for_timeout(wait_ms)
                return page.content()
            except Exception as exc:
                last_exc = exc
                if attempt < retries:
                    logger.info("%s: retry %d for %s (%s)", self.name, attempt, full, type(exc).__name__)
                    page.wait_for_timeout(1500)
            finally:
                page.close()
        raise last_exc

    # -- To implement in subclasses --------------------------------------
    def list_articles(self) -> list[Article]:
        raise NotImplementedError

    # -- Screenshots ------------------------------------------------------
    def capture_screenshots(
        self,
        article: Article,
        out_dir: Path,
        crop_selector: str | None = None,
        wait_until: str = "load",
    ) -> tuple[Path | None, Path | None]:
        """Capture (full_page_png, cropped_article_png).

        Reuses the shared browser context. Returns (None, None) on failure so
        the pipeline degrades gracefully rather than crashing. `wait_until=
        "domcontentloaded"` is markedly more reliable on ad-heavy pages (their
        load event can outlast any sane timeout) at the cost of the odd
        late-rendering element — the right trade for backfill passes.
        """
        try:
            self._ensure_shot_context()
        except Exception as exc:
            logger.warning("Browser unavailable; skipping screenshots: %s", exc)
            return None, None

        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).astimezone()
        slug = _slugify(article.title)[:60]
        stem = f"{ts:%Y-%m-%d_%H%M}_{self.name}_{slug}"
        full_path = out_dir / f"{stem}_full.png"
        crop_path = out_dir / f"{stem}.png"

        page = self._shot_context.new_page()
        try:
            # Never `networkidle`: ad/tracker sockets keep the network busy
            # forever on these sites, so it reliably times out.
            page.goto(article.url, wait_until=wait_until, timeout=45000)
            page.wait_for_timeout(1500)  # let layout/images settle

            from config import settings

            # Full-page shot only if explicitly enabled (memory-heavy on long pages).
            full = None
            if settings.capture_full_page_screenshots:
                page.screenshot(path=str(full_path), full_page=True, animations="disabled")
                full = full_path

            # Cropped article shot (bounded size) — this is what the UI displays.
            # animations="disabled" + a short timeout matter: sites with infinite
            # CSS animations (e.g. Jang's tickers) never reach Playwright's
            # "stable" state, and an element shot would hang its full 30s and
            # sink the whole capture. If the element still won't settle, fall
            # through to a plain viewport shot rather than failing.
            cropped = None
            if crop_selector:
                el = page.query_selector(crop_selector)
                if el:
                    try:
                        el.screenshot(path=str(crop_path), timeout=8000, animations="disabled")
                        cropped = crop_path
                    except Exception as exc:
                        logger.info("%s: element shot fell back to viewport (%s)",
                                    self.name, type(exc).__name__)
            if cropped is None and full is None:
                page.screenshot(path=str(crop_path), animations="disabled")
                cropped = crop_path

            from app.scrapers.footer import add_footer

            for p in (full, cropped):
                if p:
                    add_footer(p, article.source, article.section, article.url, ts)
            return full, cropped
        except Exception as exc:  # pragma: no cover - depends on live page
            logger.warning("Screenshot failed for %s: %s", article.url, exc)
            return (full_path if full_path.exists() else None), None
        finally:
            page.close()


def _slugify(text: str) -> str:
    import re

    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip().lower()
    return re.sub(r"[\s]+", "-", text) or "article"


def extract_main_text(html: str, body_selector: str | None = None) -> str:
    """Generic article-body extractor used by all config-driven scrapers.

    If `body_selector` is given, extract paragraph text from that container.
    Otherwise strip page chrome (nav/header/footer/etc.) and return all
    paragraph text; if that's too thin (sites that don't use <p>), fall back to
    the single largest text block on the page. Works across EN + UR sites
    without per-site body selectors.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "noscript"]):
        tag.decompose()

    if body_selector:
        container = soup.select_one(body_selector)
        if container:
            paras = [p.get_text(" ", strip=True) for p in container.find_all("p")]
            text = "\n".join(t for t in paras if t)
            if len(text) >= 80:
                return text
            return container.get_text(" ", strip=True)

    paras = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
    text = "\n".join(t for t in paras if t)
    if len(text) >= 200:
        return text

    # Fallback: the DOM element with the most text (handles <p>-less layouts).
    best, best_len = "", 0
    for cont in soup.find_all(["article", "div", "section", "main"]):
        t = cont.get_text(" ", strip=True)
        if len(t) > best_len:
            best, best_len = t, len(t)
    return best or text
