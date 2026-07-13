"""APScheduler setup with a persistent job store.

The SQLAlchemy job store writes scheduled jobs to the database, so after a
process restart the scheduler reloads them and monitoring resumes automatically
— satisfying the "no data loss / jobs resume after restart" requirement without
Celery or Redis. When we outgrow a single process, task functions are already
pure and can move to Celery workers unchanged.
"""
from __future__ import annotations

import logging

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler

from config import settings
from app.db.base import engine
from app.newspaper import scan_manager

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def _job_newspaper_scan():
    """Top-level function reference so APScheduler can persist/reload it.

    Delegates to scan_manager, which runs the scan as a subprocess (Playwright is
    unstable in a scheduler thread) and won't start if a manual scan is running.
    """
    started = scan_manager.start_scan(capped=True)
    logger.info("Scheduled scan trigger: %s", "started" if started else "skipped (scan already running)")


def _job_daily_digest():
    """Build + send the daily digest (email if SMTP set, else HTML file)."""
    from app.digest.sender import send_daily_digest

    result = send_daily_digest()
    logger.info("Daily digest job: %s", result)


def _job_youtube_scan():
    """Scan YouTube channels for new uploads matching keywords."""
    from app.youtube import scan_runner

    started = scan_runner.start_scan()
    logger.info("YouTube scan trigger: %s", "started" if started else "skipped (already running)")


def _job_youtube_live():
    """Check active channels for live broadcasts and tap them (flag-gated)."""
    from app.youtube import scan_runner

    started = scan_runner.start_scan(live=True)
    logger.info("YouTube live-check trigger: %s", "started" if started else "skipped (already running)")


def _job_retention_cleanup():
    """Prune data past its retention window."""
    from app.maintenance.retention import run_retention

    logger.info("Retention cleanup: %s", run_retention())


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler and _scheduler.running:
        return _scheduler

    jobstores = {"default": SQLAlchemyJobStore(engine=engine)}
    scheduler = BackgroundScheduler(jobstores=jobstores, timezone=settings.timezone)

    scheduler.add_job(
        _job_newspaper_scan,
        trigger="interval",
        minutes=settings.newspaper_scrape_interval_minutes,
        id="newspaper_scan",
        replace_existing=True,
        coalesce=True,          # collapse missed runs into one on resume
        max_instances=1,        # never overlap scans
        misfire_grace_time=300,
    )

    scheduler.add_job(
        _job_youtube_scan,
        trigger="interval",
        minutes=settings.youtube_scan_interval_minutes,
        id="youtube_scan",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=300,
    )

    # Daily digest at DIGEST_HOUR_PKT:00 Pakistan time.
    scheduler.add_job(
        _job_daily_digest,
        trigger="cron",
        hour=settings.digest_hour_pkt,
        minute=0,
        id="daily_digest",
        replace_existing=True,
        coalesce=True,
        misfire_grace_time=3600,
    )

    # Retention cleanup daily at 04:00 PKT (quiet hour).
    scheduler.add_job(
        _job_retention_cleanup,
        trigger="cron",
        hour=4,
        minute=0,
        id="retention_cleanup",
        replace_existing=True,
        coalesce=True,
        misfire_grace_time=3600,
    )

    # Live-stream checks — only when explicitly enabled (needs ffmpeg + transcriber).
    if settings.youtube_live_enabled:
        scheduler.add_job(
            _job_youtube_live,
            trigger="interval",
            minutes=settings.youtube_live_check_interval_minutes,
            id="youtube_live",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=120,
        )

    scheduler.start()
    _scheduler = scheduler
    logger.info(
        "Scheduler started: newspaper/%smin, youtube/%smin, digest @%02d:00 PKT, retention @04:00 PKT",
        settings.newspaper_scrape_interval_minutes,
        settings.youtube_scan_interval_minutes,
        settings.digest_hour_pkt,
    )
    return scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        _scheduler = None
