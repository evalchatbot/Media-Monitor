"""ORM models for the media monitoring system.

Design notes:
- `Mention` is the shared table both modules (newspaper + youtube) write into,
  so the alert pipeline and daily digest are source-agnostic.
- `external_id` + `module` are uniquely constrained to deduplicate: the same
  article or video seen on repeated scrapes must not re-alert.
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
    # 'newspaper' | 'youtube' — which module this keyword is searched in.
    module: Mapped[str] = mapped_column(String(16), default="newspaper", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    # Same word can exist independently for newspapers and YouTube.
    __table_args__ = (
        UniqueConstraint("text", "language", "module", name="uq_keyword_text_lang_module"),
    )


class Mention(Base):
    __tablename__ = "mentions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # 'newspaper' | 'youtube'
    module: Mapped[str] = mapped_column(String(16), nullable=False)
    # Stable identifier for dedup within a module (e.g. article URL, video id).
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
    # YouTube deep-link timestamp (seconds); null for newspaper
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


class YouTubeChannel(Base):
    """A monitored YouTube channel (admin-managed, like keywords)."""

    __tablename__ = "youtube_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[str] = mapped_column(String(64), nullable=False)  # UC...
    name: Mapped[str] = mapped_column(String(255), default="")
    url: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (UniqueConstraint("channel_id", name="uq_youtube_channel_id"),)


class Transcript(Base):
    """Full YouTube transcript for a video — text + word/segment-level timestamps.

    Written whenever a real (non-stub) transcription runs. `segments` is a JSON
    list of {"start": <sec>, "text": <word|phrase>} used for keyword deep-links.
    Persisted separately from ArticleCache so transcripts are first-class,
    queryable, and retained on their own schedule.
    """

    __tablename__ = "transcripts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_id: Mapped[str] = mapped_column(String(32), nullable=False)
    channel_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(255), default="")  # channel name
    title: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(Text, default="")
    language: Mapped[str | None] = mapped_column(String(8), nullable=True)
    text: Mapped[str] = mapped_column(Text, default="")
    segments: Mapped[list] = mapped_column(JSON, default=list)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    transcriber: Mapped[str] = mapped_column(String(16), default="stub")  # stub|openai|local
    is_live: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (UniqueConstraint("video_id", name="uq_transcript_video_id"),)


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
