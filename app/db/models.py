"""ORM models for the media monitoring system.

Design notes:
- `Mention` is the shared results table for newspaper websites, e-paper pages,
  and YouTube bulletins so alerts and the daily digest stay source-agnostic.
- `external_id` + `module` are uniquely constrained to deduplicate: the same
  article, page, or video seen on repeated scans must not re-alert.
- Keyword matches and other list-ish data are stored as JSON for MVP simplicity.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
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
    # 'newspaper' (websites + e-paper) | 'youtube'
    module: Mapped[str] = mapped_column(String(16), default="newspaper", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        UniqueConstraint("text", "language", "module", name="uq_keyword_text_lang_module"),
    )


class Mention(Base):
    __tablename__ = "mentions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # 'newspaper' | 'epaper' | 'youtube'
    module: Mapped[str] = mapped_column(String(16), nullable=False)
    # Stable identifier for dedup within a module (article URL, paper:city:date:pN,
    # or YouTube video_id).
    external_id: Mapped[str] = mapped_column(String(512), nullable=False)

    source: Mapped[str] = mapped_column(String(128), nullable=False)  # e.g. "Dawn"
    section: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)  # headline / video title
    url: Mapped[str] = mapped_column(Text, nullable=False)

    matched_keywords: Mapped[list] = mapped_column(JSON, default=list)
    # Per-keyword visual artifacts. Keys are canonical keyword text; values are
    # clipping / frame paths.
    keyword_media: Mapped[dict] = mapped_column(JSON, default=dict)
    # Per-keyword spoken occurrences for YouTube:
    # {keyword: [{"start": sec, "end": sec, "excerpt": str, "confidence": float,
    #             "screenshot": path|null}, ...]}
    keyword_hits: Mapped[dict] = mapped_column(JSON, default=dict)
    snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    # LLM outputs (nullable until scored)
    relevance: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sentiment: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # Artifacts
    screenshot_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    full_screenshot_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # YouTube deep-link timestamp (first match seconds); null for other modules.
    deeplink_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    notified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    __table_args__ = (
        UniqueConstraint("module", "external_id", name="uq_mention_module_extid"),
        Index("ix_mention_detected_at", "detected_at"),
    )


class ArticleCache(Base):
    """Cached article / transcript text so keyword re-matching never re-fetches."""

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
    """One page of a paper's daily PRINT edition (the e-paper)."""

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
    # For papers that expose a publisher image-map: [{"box":{l,t,r,b %},"text"}],
    # one per article — used for exact, AI-free press clippings.
    regions: Mapped[list] = mapped_column(JSON, default=list)
    # 'pending' | 'done' | 'failed' | 'no_key'
    ocr_status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        UniqueConstraint("paper", "city", "date", "page_no", name="uq_epaper_page"),
        Index("ix_epaper_date", "date"),
    )


class YouTubeChannel(Base):
    """A monitored official YouTube news channel."""

    __tablename__ = "youtube_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[str] = mapped_column(String(64), nullable=False)  # UC...
    name: Mapped[str] = mapped_column(String(255), default="")
    handle: Mapped[str] = mapped_column(String(128), default="")
    url: Mapped[str] = mapped_column(Text, default="")
    uploads_playlist_id: Mapped[str] = mapped_column(String(64), default="")
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Karachi", nullable=False)
    # 'ytdlp' (bulletin URL → audio → Groq) | 'authorized' | 'stub'
    media_source: Mapped[str] = mapped_column(String(32), default="authorized", nullable=False)
    media_source_config: Mapped[dict] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (UniqueConstraint("channel_id", name="uq_youtube_channel_id"),)


class BulletinSlot(Base):
    """Expected daily bulletin airtime for one channel (local timezone)."""

    __tablename__ = "bulletin_slots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("youtube_channels.id", ondelete="CASCADE"), nullable=False
    )
    # Local time-of-day, e.g. 09:00:00 in the channel timezone.
    local_time: Mapped[str] = mapped_column(String(8), nullable=False)  # "HH:MM:SS"
    label: Mapped[str] = mapped_column(String(64), default="")  # "9 AM", "Midnight"
    # Title tokens / regex hints used by the classifier.
    title_rules: Mapped[list] = mapped_column(JSON, default=list)
    min_duration_sec: Mapped[int] = mapped_column(Integer, default=180)
    max_duration_sec: Mapped[int] = mapped_column(Integer, default=3600)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Optional YYYY-MM-DD bounds for temporary exceptions.
    effective_from: Mapped[str | None] = mapped_column(String(10), nullable=True)
    effective_to: Mapped[str | None] = mapped_column(String(10), nullable=True)

    __table_args__ = (
        UniqueConstraint("channel_id", "local_time", name="uq_bulletin_slot_channel_time"),
        Index("ix_bulletin_slot_channel", "channel_id"),
    )


class YouTubeBulletin(Base):
    """One expected bulletin instance for a channel on a calendar date."""

    __tablename__ = "youtube_bulletins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_db_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("youtube_channels.id", ondelete="CASCADE"), nullable=False
    )
    slot_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bulletin_slots.id", ondelete="CASCADE"), nullable=False
    )
    # Pakistan calendar date this slot belongs to (midnight owned by this date).
    slot_date: Mapped[str] = mapped_column(String(10), nullable=False)  # YYYY-MM-DD
    video_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    title: Mapped[str] = mapped_column(Text, default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # waiting|discovering|needs_review|transcribing|ready|no_match|missing|failed
    discovery_status: Mapped[str] = mapped_column(String(24), default="waiting", nullable=False)
    transcription_status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Rejected / ambiguous candidates for audit.
    candidates: Mapped[list] = mapped_column(JSON, default=list)
    last_processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        UniqueConstraint("channel_db_id", "slot_id", "slot_date", name="uq_yt_bulletin_slot_date"),
        Index("ix_yt_bulletin_date", "slot_date"),
        Index("ix_yt_bulletin_video", "video_id"),
    )


class Transcript(Base):
    """Full YouTube transcript for a video — text + word/segment timestamps."""

    __tablename__ = "transcripts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_id: Mapped[str] = mapped_column(String(32), nullable=False)
    bulletin_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("youtube_bulletins.id", ondelete="SET NULL"), nullable=True
    )
    channel_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(255), default="")  # channel name
    title: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(Text, default="")
    language: Mapped[str | None] = mapped_column(String(8), nullable=True)
    text: Mapped[str] = mapped_column(Text, default="")
    # [{"start": sec, "end": sec|null, "text": word|phrase}]
    segments: Mapped[list] = mapped_column(JSON, default=list)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    transcriber: Mapped[str] = mapped_column(String(32), default="groq")
    model: Mapped[str] = mapped_column(String(64), default="")
    confidence: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
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
