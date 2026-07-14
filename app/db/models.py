"""ORM models for the media monitoring system.

Design notes:
- `Mention` is the shared table both modules (newspaper websites + e-paper
  print editions) write into, so alerts and the daily digest are source-agnostic.
- `external_id` + `module` are uniquely constrained to deduplicate: the same
  article or e-paper page seen on repeated scans must not re-alert.
- Keyword matches and other list-ish data are stored as JSON for MVP simplicity;
  they can graduate to association tables later without touching the pipeline.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Keyword(Base):
    __tablename__ = "keywords"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    text: Mapped[str] = mapped_column(String(255), nullable=False)
    # 'en' or 'ur'
    language: Mapped[str] = mapped_column(String(8), default="en", nullable=False)
    # Kept for schema compat (always 'newspaper' now). Every keyword is matched
    # against BOTH newspaper websites and e-paper print editions.
    module: Mapped[str] = mapped_column(String(16), default="newspaper", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        UniqueConstraint("text", "language", "module", name="uq_keyword_text_lang_module"),
    )


class Mention(Base):
    __tablename__ = "mentions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # 'newspaper' (website article) | 'epaper' (print-edition page)
    module: Mapped[str] = mapped_column(String(16), nullable=False)
    # Stable identifier for dedup within a module (e.g. article URL, or
    # "paper:city:date:pN" for an e-paper page).
    external_id: Mapped[str] = mapped_column(String(512), nullable=False)

    source: Mapped[str] = mapped_column(String(128), nullable=False)  # e.g. "Dawn"
    section: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)  # headline / video title
    url: Mapped[str] = mapped_column(Text, nullable=False)

    matched_keywords: Mapped[list] = mapped_column(JSON, default=list)
    snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    # LLM outputs (nullable until scored)
    relevance: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sentiment: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # Artifacts
    screenshot_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    full_screenshot_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Legacy column (kept for schema compat with existing DBs); unused.
    deeplink_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    notified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    __table_args__ = (
        UniqueConstraint("module", "external_id", name="uq_mention_module_extid"),
        Index("ix_mention_detected_at", "detected_at"),
    )


class ArticleCache(Base):
    """Cached article text so keyword re-matching (e.g. per-keyword scans, or a
    newly added keyword) never has to re-scrape a page we've already fetched.

    Populated whenever a scan fetches an article body. Matching runs against the
    cache, so a per-keyword scan is instant after the first full scrape.
    """

    __tablename__ = "article_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    module: Mapped[str] = mapped_column(String(16), default="newspaper", nullable=False)
    external_id: Mapped[str] = mapped_column(String(512), nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    section: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, default="")
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        UniqueConstraint("module", "external_id", name="uq_cache_module_extid"),
    )


class EPaperPage(Base):
    """One page of a paper's daily PRINT edition (the e-paper).

    E-paper pages are scanned images, so keyword matching needs the page read
    into text first: `ocr_text` holds that extraction (Claude vision), and
    `ocr_status` tracks where each page is in the pipeline. Matching then runs
    on `ocr_text` with the same matcher the website articles use.
    """

    __tablename__ = "epaper_pages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    paper: Mapped[str] = mapped_column(String(32), nullable=False)   # slug, e.g. "jang"
    source: Mapped[str] = mapped_column(String(128), nullable=False)  # display, e.g. "Jang"
    city: Mapped[str] = mapped_column(String(32), default="lahore", nullable=False)
    date: Mapped[str] = mapped_column(String(10), nullable=False)     # "YYYY-MM-DD"
    page_no: Mapped[int] = mapped_column(Integer, nullable=False)
    image_url: Mapped[str] = mapped_column(Text, nullable=False)      # remote full-size image
    image_path: Mapped[str | None] = mapped_column(Text, nullable=True)  # local copy
    viewer_url: Mapped[str] = mapped_column(Text, default="")         # human-facing page link
    ocr_text: Mapped[str] = mapped_column(Text, default="")
    # 'pending' (not read yet) | 'done' | 'failed' | 'no_key' (needs ANTHROPIC_API_KEY)
    ocr_status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        UniqueConstraint("paper", "city", "date", "page_no", name="uq_epaper_page"),
        Index("ix_epaper_date", "date"),
    )


class ScrapeRun(Base):
    """Audit trail of each scrape attempt — powers uptime + blocked-scrape alerts."""

    __tablename__ = "scrape_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 'ok' | 'blocked' | 'error'
    status: Mapped[str] = mapped_column(String(16), default="ok", nullable=False)
    articles_found: Mapped[int] = mapped_column(Integer, default=0)
    mentions_created: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
