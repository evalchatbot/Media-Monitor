"""Data-retention cleanup (runs daily via the scheduler).

Windows (configurable in .env):
  screenshots  90 days   — delete PNGs under data/storage; clear dead DB paths
  transcripts  12 months — delete ArticleCache rows (article bodies + YT
                           transcripts) past the window
  logs         24 months — delete ScrapeRun audit rows past the window

alerts.log is append-only; rotate it with an OS log-rotation tool if it grows.
Deleting old files/rows never touches Mention records, so detection history and
the digest stay intact; only the heavy artifacts/caches are pruned.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from config import settings
from app.db.base import SessionLocal
from app.db.models import ArticleCache, Mention, ScrapeRun

logger = logging.getLogger(__name__)


def run_retention() -> dict:
    now = datetime.now(timezone.utc)
    summary = {"screenshots_deleted": 0, "cache_rows_deleted": 0, "scrape_rows_deleted": 0}

    # --- Screenshots ---
    cutoff_shots = now - timedelta(days=settings.retention_screenshots_days)
    storage = settings.storage_dir
    if storage.exists():
        for png in storage.rglob("*.png"):
            try:
                mtime = datetime.fromtimestamp(png.stat().st_mtime, tz=timezone.utc)
                if mtime < cutoff_shots:
                    png.unlink()
                    summary["screenshots_deleted"] += 1
            except Exception as exc:  # pragma: no cover
                logger.warning("retention: could not delete %s: %s", png, exc)

    session = SessionLocal()
    try:
        # Clear DB references to screenshots that no longer exist on disk.
        for m in session.execute(select(Mention)).scalars():
            changed = False
            for attr in ("screenshot_path", "full_screenshot_path"):
                p = getattr(m, attr)
                if p and not _exists(p):
                    setattr(m, attr, None)
                    changed = True
            if changed:
                session.add(m)
        session.commit()

        # --- Transcripts / cached bodies ---
        cutoff_tx = now - timedelta(days=settings.retention_transcripts_days)
        res = session.execute(delete(ArticleCache).where(ArticleCache.fetched_at < cutoff_tx))
        summary["cache_rows_deleted"] = res.rowcount or 0

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
