"""Config-driven scraper: one class, many publications.

Most newspapers differ only in (a) which listing URLs to crawl and (b) how to
recognise an article link — either a CSS selector for the headline anchors or a
URL pattern that article links match. Bodies are pulled with the generic
`extract_main_text` extractor, so no per-site body selectors are needed.

Sites that need bespoke logic (like Dawn, or a JS-API site) still get their own
subclass; everything else is just a `SiteConfig` in `sites.py`.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from app.scrapers.base import (
    Article, BaseScraper, extract_main_text, extract_published,
)

logger = logging.getLogger(__name__)


@dataclass
class SiteConfig:
    name: str                      # slug, e.g. "thenews"
    source: str                    # display name, e.g. "The News"
    base_url: str
    sections: dict                 # {label: url}
    language: str = "en"           # informational; matching uses per-keyword lang
    link_selector: str | None = None   # CSS selector for headline anchors
    article_url_pattern: str | None = None  # regex an article href must match
    body_selector: str | None = None   # optional; else generic extractor
    crop_selector: str | None = None   # optional; for cropped screenshot
    min_title_len: int = 18


class ConfigurableScraper(BaseScraper):
    def __init__(self, cfg: SiteConfig, respect_robots: bool = True):
        super().__init__(
            name=cfg.name,
            base_url=cfg.base_url,
            sections=cfg.sections,
            respect_robots=respect_robots,
        )
        self.cfg = cfg
        self._pattern = re.compile(cfg.article_url_pattern) if cfg.article_url_pattern else None
        # Exposed for the pipeline's screenshot cropping.
        self.ARTICLE_CROP_SELECTOR = cfg.crop_selector

    def list_articles(self) -> list[Article]:
        articles: dict[str, Article] = {}
        for section, url in self.sections.items():
            try:
                html = self.render(url, wait_ms=2500)
            except Exception as exc:
                logger.warning("%s: failed to fetch %s (%s): %s", self.name, section, url, exc)
                continue
            for art in self._parse_listing(html, section):
                articles.setdefault(art.url, art)
        logger.info("%s: found %d articles across %d section(s)", self.name, len(articles), len(self.sections))
        return list(articles.values())

    def _abs(self, href: str) -> str:
        if href.startswith("http"):
            return href
        return self.base_url.rstrip("/") + "/" + href.lstrip("/")

    def _parse_listing(self, html: str, section: str) -> list[Article]:
        soup = BeautifulSoup(html, "lxml")
        anchors = (
            soup.select(self.cfg.link_selector)
            if self.cfg.link_selector
            else soup.find_all("a", href=True)
        )
        found: list[Article] = []
        for a in anchors:
            title = a.get_text(strip=True)
            href = a.get("href", "")
            if not title or not href or len(title) < self.cfg.min_title_len:
                continue
            full = self._abs(href)
            if self._pattern and not self._pattern.search(full):
                continue
            found.append(Article(source=self.cfg.source, title=title, url=full, section=section))
        return found

    def fetch_body(self, article: Article) -> str:
        return self.fetch_article(article)[0]

    def fetch_article(self, article: Article):
        """Return (body_text, published_date|None) from one render of the page.

        Both come from the same HTML so date filtering costs no extra fetch.
        """
        try:
            html = self.render(article.url, wait_ms=1500)
        except Exception as exc:
            logger.warning("%s: failed to fetch body for %s: %s", self.name, article.url, exc)
            return "", None
        return (extract_main_text(html, self.cfg.body_selector),
                extract_published(html, article.url))
