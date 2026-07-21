"""Data-retention cleanup (runs daily via the scheduler).

Windows (configurable in .env):
  scan media   90 days   — delete screenshots + e-paper page scans under
                           data/storage, plus the YouTube transcripts and
                           bulletin rows they belong to; clear dead DB paths.
                           These share one cutoff and always expire together.
  cached text  12 months — delete ArticleCache + EPaperPage rows past the window
                           (RETENTION_TRANSCRIPTS_DAYS — the name is historical;
                           it governs cached article text, NOT YouTube transcripts)
  logs         24 months — delete ScrapeRun audit rows past the window

alerts.log is append-only; rotate it with an OS log-rotation tool if it grows.
Keyword results are retained for their configured rolling window (90 days by
default), then their Mention rows expire. Live search only looks back the
configured search window (30 days by default). Each keyword also keeps only its
newest configured number of results (25 by default).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from config import settings
from app.core import result_policy
from app.db.base import SessionLocal
from app.db.models import ArticleCache, EPaperPage, Mention, ScrapeRun, Transcript, YouTubeBulletin

logger = logging.getLogger(__name__)


def run_retention() -> dict:
    now = datetime.now(timezone.utc)
    summary = {"screenshots_deleted": 0, "cache_rows_deleted": 0,
               "epaper_rows_deleted": 0, "scrape_rows_deleted": 0,
               "transcripts_deleted": 0, "bulletins_deleted": 0,
               "mentions_expired": 0, "keyword_links_trimmed": 0}

    # --- Screenshots, e-paper page scans, transcripts ---
    # One cutoff for every artefact of a scan: a transcript whose frames are gone
    # is unusable, and a frame whose transcript is gone can't be re-verified, so
    # they must expire together no matter how the windows are configured.
    # Never prune a visual before its keyword result expires.
    cutoff_media = now - timedelta(days=max(
        settings.retention_screenshots_days,
        settings.keyword_result_retention_days,
    ))
    storage = settings.storage_dir
    if storage.exists():
        for pattern in ("*.png", "*.jpg", "*.jpeg"):
            for img in storage.rglob(pattern):
                try:
                    mtime = datetime.fromtimestamp(img.stat().st_mtime, tz=timezone.utc)
                    if mtime < cutoff_media:
                        img.unlink()
                        summary["screenshots_deleted"] += 1
                except Exception as exc:  # pragma: no cover
                    logger.warning("retention: could not delete %s: %s", img, exc)

    session = SessionLocal()
    try:
        policy = result_policy.enforce_limits(session)
        summary["mentions_expired"] = policy["expired"]
        summary["keyword_links_trimmed"] = policy["trimmed"]
        summary["screenshots_deleted"] += policy["files_deleted"]

        # Clear DB references to screenshots that no longer exist on disk.
        for m in session.execute(select(Mention)).scalars():
            changed = False
            for attr in ("screenshot_path", "full_screenshot_path"):
                p = getattr(m, attr)
                if p and not _exists(p):
                    setattr(m, attr, None)
                    changed = True
            media = dict(m.keyword_media or {})
            live_media = {label: path for label, path in media.items() if _exists(path)}
            if len(live_media) != len(media):
                m.keyword_media = live_media
                changed = True
            if changed:
                session.add(m)
        session.commit()

        # --- Cached text (article bodies + e-paper page reads) ---
        cutoff_tx = now - timedelta(days=settings.retention_transcripts_days)
        res = session.execute(delete(ArticleCache).where(ArticleCache.fetched_at < cutoff_tx))
        summary["cache_rows_deleted"] = res.rowcount or 0
        res = session.execute(delete(EPaperPage).where(EPaperPage.fetched_at < cutoff_tx))
        summary["epaper_rows_deleted"] = res.rowcount or 0

        # --- YouTube transcripts + old bulletin rows ---
        # Same cutoff as the frames above, so a bulletin's transcript and its
        # screenshots always disappear on the same day.
        res = session.execute(delete(Transcript).where(Transcript.created_at < cutoff_media))
        summary["transcripts_deleted"] = res.rowcount or 0
        res = session.execute(
            delete(YouTubeBulletin).where(YouTubeBulletin.created_at < cutoff_media)
        )
        summary["bulletins_deleted"] = res.rowcount or 0

        # --- Logs (scrape audit rows) ---
        cutoff_log = now - timedelta(days=settings.retention_logs_days)
        res = session.execute(delete(ScrapeRun).where(ScrapeRun.started_at < cutoff_log))
        summary["scrape_rows_deleted"] = res.rowcount or 0

        session.commit()
    finally:
        session.close()

    return summary


def _exists(path_str: str) -> bool:
    from pathlib import Path

    try:
        return Path(path_str).exists()
    except Exception:
        return False
