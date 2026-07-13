"""Dawn (dawn.com) scraper — English, static HTML, no anti-bot.

Chosen as the first publication because its listing pages are clean, server-
rendered HTML we can parse with BeautifulSoup without a headless browser.
Article discovery here; full body + screenshot fetched later in the pipeline.
"""
from __future__ import annotations

import logging

from bs4 import BeautifulSoup

from app.scrapers.base import Article, BaseScraper

logger = logging.getLogger(__name__)


class DawnScraper(BaseScraper):
    def __init__(self, respect_robots: bool = True):
        super().__init__(
            name="dawn",
            base_url="https://www.dawn.com",
            sections={
                "front": "https://www.dawn.com/latest-news",
                "opinion": "https://www.dawn.com/opinion",
            },
            respect_robots=respect_robots,
        )

    def list_articles(self) -> list[Article]:
        articles: dict[str, Article] = {}
        for section, url in self.sections.items():
            try:
                html = self.render(url)
            except Exception as exc:
                logger.warning("Dawn: failed to fetch %s section (%s): %s", section, url, exc)
                continue
            for art in self._parse_listing(html, section):
                articles.setdefault(art.url, art)
        return list(articles.values())

    def _parse_listing(self, html: str, section: str) -> list[Article]:
        soup = BeautifulSoup(html, "lxml")
        found: list[Article] = []
        # Dawn story cards use <article> with an <h2 class="story__title"> anchor.
        for h2 in soup.select("h2.story__title a, h2.story__link a, article a.story__link"):
            title = h2.get_text(strip=True)
            href = h2.get("href", "")
            if not title or not href or "/news/" not in href:
                continue
            found.append(
                Article(
                    source="Dawn",
                    title=title,
                    url=href if href.startswith("http") else self.base_url + href,
                    section=section,
                )
            )
        return found

    def fetch_body(self, article: Article) -> str:
        """Fetch and extract the article body text for keyword matching."""
        try:
            html = self.render(article.url)
        except Exception as exc:
            logger.warning("Dawn: failed to fetch body for %s: %s", article.url, exc)
            return ""
        soup = BeautifulSoup(html, "lxml")
        # Dawn wraps the story body in <div class="story__content">.
        container = soup.select_one("div.story__content") or soup.select_one("article")
        if not container:
            return ""
        paragraphs = [p.get_text(" ", strip=True) for p in container.find_all("p")]
        return "\n".join(paragraphs)

    # Selector used by the pipeline to crop the article screenshot.
    ARTICLE_CROP_SELECTOR = "div.story, article"
