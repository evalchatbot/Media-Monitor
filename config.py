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
    # The system monitors CURRENT coverage only: content dated before this is
    # never fetched or matched (e-paper editions, dated articles).
    monitor_since: str = "2026-01-01"

    @property
    def monitor_since_date(self):
        from datetime import date

        try:
            return date.fromisoformat(self.monitor_since)
        except ValueError:
            return date(2026, 1, 1)

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

    # --- LLM providers ---
    # Groq (default provider when set): powers e-paper page reading (vision)
    # and relevance/sentiment scoring. Llama-4 multimodal models.
    groq_api_key: str = ""
    groq_model: str = "meta-llama/llama-4-maverick-17b-128e-instruct"
    # Anthropic (used when no Groq key is set).
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

    # --- E-paper module (print editions) ---
    epaper_enabled: bool = True
    # Preferred city edition where a paper prints several (lahore|karachi|islamabad…)
    epaper_city: str = "lahore"
    # Comma-separated paper slugs to fetch (empty = all supported)
    epaper_papers: str = ""
    # Editions publish by early morning; fetch daily at this hour (PKT).
    epaper_fetch_hour_pkt: int = 8
    # Safety cap per edition (a daily paper is typically 8-20 pages)
    epaper_max_pages: int = 24
    # Reading a scanned page (image -> text) uses Claude vision; needs
    # ANTHROPIC_API_KEY. Model defaults to LLM_MODEL when empty.
    epaper_ocr_model: str = ""

    # --- Data retention (days) ---
    retention_screenshots_days: int = 90
    retention_transcripts_days: int = 365
    retention_logs_days: int = 730


@lru_cache
def get_settings() -> Settings:
    """Cached accessor so we parse the environment exactly once."""
    return Settings()


settings = get_settings()
