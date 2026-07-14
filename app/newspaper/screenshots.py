"""Screenshot backfill for website detections that don't have one yet.

⚡ Quick Scans create mentions instantly WITHOUT screenshots (no browser in the
web process). This module captures those images afterwards in a Playwright-safe
subprocess: scheduled/manual scans run it automatically at the end of each
cycle, and the web app kicks a backfill-only run right after a quick scan finds
something new — so cards get their visuals within a couple of minutes, hands-off.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from config import settings

from app.db.base import SessionLocal
from app.db.models import Mention
from app.scrapers.base import Article
from app.scrapers.sites import build_scrapers

logger = logging.getLogger(__name__)


def backfill_screenshots(limit: int = 25) -> dict:
    """Capture screenshots for up to `limit` newspaper mentions missing one.
    Newest first, so fresh detections get their visuals before old ones."""
    summary = {"missing": 0, "captured": 0}
    session = SessionLocal()
    by_source: dict = {}
    try:
        rows = session.execute(
            select(Mention)
            .where(Mention.module == "newspaper", Mention.screenshot_path.is_(None))
            .order_by(Mention.detected_at.desc())
        ).scalars().all()
        summary["missing"] = len(rows)
        if not rows:
            return summary

        # Map display source -> scraper (for crop selectors + storage dirs).
        for sc in build_scrapers():
            display = sc.cfg.source if hasattr(sc, "cfg") else "Dawn"
            by_source[display] = sc

        for m in rows[:limit]:
            sc = by_source.get(m.source)
            if sc is None:
                continue
            art = Article(source=m.source, title=m.title, url=m.url,
                          section=m.section, external_id=m.external_id)
            try:
                full, crop = sc.capture_screenshots(
                    art, settings.storage_dir / sc.name,
                    getattr(sc, "ARTICLE_CROP_SELECTOR", None),
                )
            except Exception as exc:
                logger.warning("backfill: screenshot failed for %s: %s", m.url, exc)
                continue
            if crop or full:
                m.screenshot_path = str(crop) if crop else None
                m.full_screenshot_path = str(full) if full else None
                session.commit()
                summary["captured"] += 1
        return summary
    finally:
        for sc in by_source.values():
            try:
                sc.close()
            except Exception:
                pass
        session.close()
