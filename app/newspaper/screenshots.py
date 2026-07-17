"""Screenshot backfill for website detections that don't have one yet.

⚡ Quick Scans create mentions instantly WITHOUT screenshots (no browser in the
web process). This module captures those images afterwards in a Playwright-safe
subprocess: scheduled/manual scans run it automatically at the end of each
cycle, and the keyword queue kicks a backfill-only run after exact matches —
so cards get their visuals without a full site crawl.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from config import settings

from app.db.base import SessionLocal
from app.db.models import Keyword, Mention
from app.scrapers.base import Article
from app.scrapers.sites import build_scrapers

logger = logging.getLogger(__name__)


def backfill_screenshots(limit: int = 25, keyword_ids: list[int] | None = None) -> dict:
    """Capture screenshots for up to `limit` newspaper mentions missing one.
    Newest first. Optionally restrict to mentions of specific keyword ids."""
    summary = {"missing": 0, "captured": 0}
    session = SessionLocal()
    try:
        rows = session.execute(
            select(Mention)
            .where(Mention.module == "newspaper", Mention.screenshot_path.is_(None))
            .order_by(Mention.detected_at.desc())
        ).scalars().all()

        kw_q = select(Keyword)
        if keyword_ids:
            kw_q = kw_q.where(Keyword.id.in_(keyword_ids))
        else:
            kw_q = kw_q.where(Keyword.active.is_(True))
        keywords = session.execute(kw_q).scalars().all()
        # casefold label -> display text (for highlights)
        label_map = {(k.text or "").casefold(): k.text for k in keywords if k.text}
        filter_folds = set(label_map.keys())

        rows = [
            m for m in rows
            if any((label or "").casefold() in filter_folds for label in (m.matched_keywords or []))
        ]
        summary["missing"] = len(rows)
        if not rows:
            return summary

        by_source = {}
        for sc in build_scrapers():
            display = sc.cfg.source if hasattr(sc, "cfg") else "Dawn"
            by_source[display] = sc

        # ONE browser at a time: sync Playwright refuses a second instance in
        # the same thread, so process mentions grouped by source and close each
        # source's browser before opening the next.
        todo = rows[:limit]
        for source in {m.source for m in todo}:
            sc = by_source.get(source)
            if sc is None:
                continue
            try:
                for m in (x for x in todo if x.source == source):
                    highlights = [
                        label_map[label.casefold()]
                        for label in (m.matched_keywords or [])
                        if label and label.casefold() in label_map
                    ]
                    art = Article(source=m.source, title=m.title, url=m.url,
                                  section=m.section, external_id=m.external_id)
                    try:
                        full, crop = sc.capture_screenshots(
                            art, settings.storage_dir / sc.name,
                            getattr(sc, "ARTICLE_CROP_SELECTOR", None),
                            wait_until="domcontentloaded",
                            highlight=highlights,
                        )
                    except Exception as exc:
                        logger.warning("backfill: screenshot failed for %s: %s", m.url, exc)
                        continue
                    if crop or full:
                        m.screenshot_path = str(crop) if crop else None
                        m.full_screenshot_path = str(full) if full else None
                        session.commit()
                        summary["captured"] += 1
            finally:
                try:
                    sc.close()
                except Exception:
                    pass
        return summary
    finally:
        session.close()
