"""Publication registry — every scraped newspaper in one place.

Add a publication by appending a SiteConfig (or, for a bespoke site, a scraper
subclass in `build_scrapers`). Selectors/patterns below were derived by probing
each site's live listing pages.

Geo (urdu.geo.tv) is intentionally NOT here: its headlines are loaded by a
client-side JS API and aren't in the served HTML, so the standard approach can't
scrape it reliably. It needs a dedicated API-based scraper — flagged separately.
"""
from __future__ import annotations

from config import settings

from app.scrapers.configurable import ConfigurableScraper, SiteConfig
from app.scrapers.dawn import DawnScraper

SITE_CONFIGS: list[SiteConfig] = [
    SiteConfig(
        name="thenews",
        source="The News",
        base_url="https://www.thenews.com.pk",
        sections={
            "latest": "https://www.thenews.com.pk/latest",
            "world": "https://www.thenews.com.pk/latest/category/world",
            "opinion": "https://www.thenews.com.pk/latest/category/opinion",
        },
        language="en",
        article_url_pattern=r"thenews\.com\.pk/latest/\d{5,}-",
        crop_selector="div.detail-content, article, main",
    ),
    SiteConfig(
        name="tribune",
        source="Express Tribune",
        base_url="https://tribune.com.pk",
        sections={
            "latest": "https://tribune.com.pk/latest",
            "world": "https://tribune.com.pk/world",
            "opinion": "https://tribune.com.pk/opinion",
        },
        language="en",
        article_url_pattern=r"tribune\.com\.pk/story/\d+",
        crop_selector="span.story-text, div.story-text, article, main",
    ),
    SiteConfig(
        name="jang",
        source="Jang",
        base_url="https://jang.com.pk",
        sections={
            "latest": "https://jang.com.pk/category/latest-news",
            "world": "https://jang.com.pk/category/world",
            "columns": "https://jang.com.pk/columns",
        },
        language="ur",
        article_url_pattern=r"jang\.com\.pk/news/\d+",
        crop_selector="div.detail_view_content, div.content-area, article, main",
    ),
    SiteConfig(
        name="nawaiwaqt",
        source="Nawa-i-Waqt",
        base_url="https://www.nawaiwaqt.com.pk",
        sections={
            "national": "https://www.nawaiwaqt.com.pk/national",
            "international": "https://www.nawaiwaqt.com.pk/international",
            "columns": "https://www.nawaiwaqt.com.pk/columns",
        },
        language="ur",
        link_selector="h3.jeg_post_title a",
        article_url_pattern=r"nawaiwaqt\.com\.pk/\d{2}-\w{3}-\d{4}/\d+",
        crop_selector="div.content-inner, div.jeg_post, article, main",
    ),
    SiteConfig(
        name="ary",
        source="ARY News",
        base_url="https://urdu.arynews.tv",
        sections={
            # Homepage is very heavy and times out; category pages are reliable.
            "pakistan": "https://urdu.arynews.tv/category/pakistan/",
            "world": "https://urdu.arynews.tv/category/international/",
        },
        language="ur",
        link_selector="h1.entry-title a, h2.entry-title a, h3.entry-title a",
        crop_selector="div.td-post-content, article, main",
    ),
    SiteConfig(
        name="dunya",
        source="Dunya News",
        base_url="https://urdu.dunyanews.tv",
        sections={
            "pakistan": "https://urdu.dunyanews.tv/index.php/ur/Pakistan",
            "world": "https://urdu.dunyanews.tv/index.php/ur/World",
        },
        language="ur",
        article_url_pattern=r"/index\.php/ur/\w+/\d+",
        crop_selector="div.news-detail, div.detail, article, main",
    ),
    SiteConfig(
        name="express",
        source="Express Urdu",
        base_url="https://www.express.pk",
        sections={
            "front": "https://www.express.pk/",
            "latest": "https://www.express.pk/latest-news/",
            "world": "https://www.express.pk/world/",
            "columns": "https://www.express.pk/columns",
        },
        language="ur",
        # Article links are /story/<id>/<slug>; homepage also carries video/live-blog
        # links which this pattern filters out.
        article_url_pattern=r"express\.pk/story/\d+",
        crop_selector="div.story-content, span.story-text, article, main",
    ),
]


def build_scrapers():
    """All active newspaper scrapers. Dawn keeps its bespoke class; the rest are
    config-driven. NEWSPAPER_SITES (comma-separated slugs) limits which run;
    empty = all."""
    respect = settings.respect_robots_txt
    only = {s.strip() for s in settings.newspaper_sites.split(",") if s.strip()}
    scrapers = [DawnScraper(respect_robots=respect)]
    scrapers += [ConfigurableScraper(cfg, respect_robots=respect) for cfg in SITE_CONFIGS]
    if only:
        scrapers = [s for s in scrapers if s.name in only]
    return scrapers
