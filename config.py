"""Central application configuration.

All settings load from environment variables (via a `.env` file in dev).
Nothing secret is ever hardcoded — see `.env.example` for the full reference.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root = directory containing this file.
BASE_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Core ---
    app_env: str = "development"
    log_level: str = "INFO"
    timezone: str = "Asia/Karachi"
    # Set false to run the web UI without the background scheduler (auto-scans).
    scheduler_enabled: bool = True

    # --- Database ---
    database_url: str = "sqlite:///./data/media_monitoring.db"

    # --- Supabase (optional) ---
    # The app talks to Postgres directly via DATABASE_URL. These are only used if
    # a Supabase REST/JS client is added later (a Next.js frontend, etc.) — they
    # do NOT authenticate the database connection.
    supabase_url: str = ""
    supabase_publishable_key: str = ""

    # --- Storage ---
    storage_dir: Path = BASE_DIR / "data" / "storage"

    # --- Newspaper module ---
    newspaper_scrape_interval_minutes: int = 30
    respect_robots_txt: bool = True
    # Max NEW (unseen) article bodies fetched PER SITE per scan. Bounds cold-start
    # cost across all sites so a scan always finishes promptly; already-cached
    # articles are still matched every scan, so coverage compounds over time.
    newspaper_max_articles_per_scan: int = 20
    # Comma-separated site slugs to scan (empty = all): dawn,thenews,tribune,jang,nawaiwaqt,ary,dunya
    newspaper_sites: str = ""
    # Full-page screenshots of long articles allocate large bitmaps (memory
    # spikes). Off by default — the cropped article shot is what's displayed.
    capture_full_page_screenshots: bool = False
    # Block images/media/fonts while scraping: big memory + speed win, keeps the
    # browser lean so it won't destabilise the web server. Screenshots become
    # text-only (still readable). Set false for image-rich screenshots.
    block_media_in_scans: bool = True

    # --- Notifications ---
    notifier: str = "console"
    whatsapp_phone_number_id: str = ""
    whatsapp_access_token: str = ""
    whatsapp_recipient: str = ""

    # --- LLM scoring (relevance + sentiment via Claude) ---
    anthropic_api_key: str = ""
    llm_model: str = "claude-sonnet-5"   # current latest; override via LLM_MODEL
    enable_llm_scoring: bool = False

    # --- Email digest (SMTP; works with SES SMTP, Gmail, Outlook, etc.) ---
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    digest_sender: str = ""
    digest_recipients: str = ""
    digest_hour_pkt: int = 7

    @property
    def digest_recipient_list(self) -> list[str]:
        return [r.strip() for r in self.digest_recipients.split(",") if r.strip()]

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host and self.digest_sender and self.digest_recipient_list)

    # --- YouTube module ---
    youtube_transcriber: str = "stub"   # stub | openai | local
    youtube_scan_interval_minutes: int = 10
    openai_api_key: str = ""
    whisper_model: str = "large-v3"     # for local faster-whisper
    # Local faster-whisper tuning:
    whisper_device: str = "auto"        # auto | cuda | cpu
    whisper_compute_type: str = "auto"  # auto | int8_float16 (GPU) | int8 (CPU) | float16
    whisper_cpu_threads: int = 8
    whisper_language: str = ""          # "" = auto-detect, or "ur" / "en"
    youtube_max_videos_per_scan: int = 15
    # Live-stream monitoring (needs ffmpeg + a non-stub transcriber to do real work)
    youtube_live_enabled: bool = False
    youtube_live_chunk_seconds: int = 30
    youtube_live_check_interval_minutes: int = 2

    # --- Data retention (days) ---
    retention_screenshots_days: int = 90
    retention_transcripts_days: int = 365
    retention_logs_days: int = 730


@lru_cache
def get_settings() -> Settings:
    """Cached accessor so we parse the environment exactly once."""
    return Settings()


settings = get_settings()
