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
    # Connection pool (Postgres only). The web process and every scan subprocess
    # each open a pool, and the Supabase pooler caps total clients, so keep these
    # small. Prefer the transaction pooler (port 6543) in DATABASE_URL for a web
    # workload — it frees the server connection after each transaction.
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_pool_timeout: int = 30
    db_pool_recycle: int = 1800

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
    # Block images/media/fonts while SCRAPING (listing/body fetches): big memory
    # + speed win. Screenshots use their own context with media loaded.
    block_media_in_scans: bool = True
    # Device pixel ratio for screenshots (2 = retina-crisp text; 1 = lighter).
    screenshot_scale: int = 2

    # --- Notifications ---
    notifier: str = "console"
    whatsapp_phone_number_id: str = ""
    whatsapp_access_token: str = ""
    whatsapp_recipient: str = ""

    # --- LLM providers ---
    # Groq (default provider when set): powers e-paper page reading (vision)
    # and relevance/sentiment scoring. Llama-4 multimodal models.
    groq_api_key: str = ""
    # Scout is the Llama-4 vision model generally available on Groq accounts.
    groq_model: str = "meta-llama/llama-4-scout-17b-16e-instruct"
    # Optional: Google Gemini for PRECISE press-clipping boxes (grounding is
    # its strength). Free key at https://aistudio.google.com/apikey — when set,
    # the clipper uses Gemini for locate/verify; reading stays on Groq.
    # 'gemini-flash-latest' is an ALIAS to the current Flash — it keeps working
    # as Google rotates models (dated versions get 404'd for new keys).
    gemini_api_key: str = ""
    gemini_model: str = "gemini-flash-latest"
    # Live-ticker OCR crop (fraction of frame height/width). The main Urdu
    # ticker sits in the lower third; the right edge is trimmed to drop the
    # channel logo. Tunable per deployment if a channel's ticker sits elsewhere.
    ticker_crop_top: float = 0.72
    ticker_crop_bottom: float = 0.92
    ticker_crop_right: float = 0.90
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

    # --- Keyword results ---
    # A keyword owns at most this many unique results inside the retention window.
    keyword_result_limit: int = 25
    # How long results (and soft-hidden keywords' hits) are kept on disk/DB.
    keyword_result_retention_days: int = 90
    # Live search / Confirm / Results UI look back this many days from "today".
    # Keep this equal to keyword_result_retention_days: anything retained but
    # outside this window is stored and paid for yet invisible to a new keyword.
    keyword_search_days: int = 90
    # Screenshots, e-paper scans, YouTube transcripts and bulletin rows all
    # expire on this window (or keyword_result_retention_days, whichever is
    # longer) so a scan's transcript and its frames are never orphaned.
    retention_screenshots_days: int = 90
    # Historical name: governs CACHED ARTICLE TEXT (ArticleCache + EPaperPage),
    # not YouTube transcripts. Those follow retention_screenshots_days above.
    retention_transcripts_days: int = 365
    retention_logs_days: int = 730

    # --- YouTube bulletin module ---
    youtube_enabled: bool = True
    youtube_api_key: str = ""
    # Default Whisper model on Groq ($0.04/audio-hour). Large V3 is accuracy fallback.
    groq_whisper_model: str = "whisper-large-v3-turbo"
    groq_whisper_fallback_model: str = "whisper-large-v3"
    # Whisper decoder prompt. MUST NOT contain watchlist keywords: the prompt
    # biases the decoder toward its own text, so priming it with the terms being
    # searched makes Whisper emit them over silence and noise — the model then
    # "finds" keywords in bulletins that never said them. Empty = no bias.
    youtube_transcribe_prompt: str = ""
    youtube_timezone: str = "Asia/Karachi"
    # Process each bulletin this many minutes after its scheduled airtime.
    youtube_process_delay_minutes: int = 60
    # Extra retries after the first attempt (minutes after airtime).
    youtube_retry_offsets_minutes: str = "90,120"
    # Mark the slot missing this many minutes after airtime with no video.
    youtube_missing_after_minutes: int = 180
    # Discovery window around the expected slot (± minutes).
    youtube_discovery_window_minutes: int = 90
    youtube_max_duration_seconds: int = 3600
    youtube_transcribe_concurrency: int = 1
    # Bulletin days each auto scan covers: today plus the previous N-1 days.
    # This is the catch-up window — a slot missed while the host was down is
    # still picked up this many days later. It does NOT bound how much history
    # is kept; that is keyword_result_retention_days (90).
    youtube_catchup_days: int = 4
    # Opus bitrate for the 16 kHz mono audio sent to Groq. 32 kbps is transparent
    # for speech; raising it only slows uploads. Set 0 to force lossless FLAC.
    youtube_audio_bitrate_kbps: int = 32
    # ytdlp (bulletin watch URL → audio → Groq) | authorized | stub
    youtube_media_source: str = "ytdlp"
    # Monthly Groq spend alert threshold (USD). Soft limit used for logging.
    youtube_monthly_cost_alert_usd: float = 40.0
    youtube_metadata_only: bool = False  # True = discover/classify without Groq spend


@lru_cache
def get_settings() -> Settings:
    """Cached accessor so we parse the environment exactly once."""
    return Settings()


settings = get_settings()
