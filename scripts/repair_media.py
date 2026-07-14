"""Rebuild image files after moving hosts (e.g. the first cloud deploy).

The database stores image PATHS; the files themselves live on whichever
machine captured them. On a brand-new host those paths are dead, so cards
render without images. This one-shot repair, run ON the new host:

1. re-downloads every e-paper page scan from its source URL into STORAGE_DIR
   (the extracted text is already in the DB — no LLM cost)
2. rebuilds the stamped e-paper detection shots from those files
3. clears dead website-screenshot paths, then re-captures them live
   (bounded backfill rounds; scheduled scans keep topping up afterwards)

Usage:  python -m scripts.repair_media [--backfill-rounds 3]
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from sqlalchemy import select

from app.db.base import SessionLocal, init_db
from app.db.models import EPaperPage, Mention


def _alive(path: str | None) -> bool:
    try:
        return bool(path) and Path(path).exists()
    except OSError:
        return False


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill-rounds", type=int, default=3,
                    help="website screenshot re-capture rounds (40 shots each)")
    args = ap.parse_args()

    init_db()
    from app.epaper import pipeline as ep
    from app.epaper.sources import EPage

    session = SessionLocal()
    try:
        # -- 1. e-paper page scans ------------------------------------------
        pages = session.execute(select(EPaperPage)).scalars().all()
        restored = 0
        for r in pages:
            if _alive(r.image_path):
                continue
            dest = ep._download(EPage(paper=r.paper, source=r.source, city=r.city,
                                      date=r.date, page_no=r.page_no,
                                      image_url=r.image_url, viewer_url=r.viewer_url))
            if dest:
                r.image_path = str(dest)
                session.commit()
                restored += 1
        print(f"[1/3] e-paper page files restored: {restored} "
              f"(of {len(pages)} rows; already-present files skipped)")

        # -- 2. e-paper detections' stamped shots ---------------------------
        by_key = {f"{r.paper}:{r.city}:{r.date}:p{r.page_no}": r for r in pages}
        rebuilt = 0
        for m in session.execute(select(Mention).where(Mention.module == "epaper")).scalars():
            if _alive(m.screenshot_path):
                continue
            row = by_key.get(m.external_id)
            shot = ep._detection_shot(row) if (row and _alive(row.image_path)) else None
            m.screenshot_path = shot
            session.commit()
            rebuilt += bool(shot)
        print(f"[2/3] e-paper detection shots rebuilt: {rebuilt}")

        # -- 3. website screenshots -----------------------------------------
        cleared = 0
        for m in session.execute(select(Mention).where(Mention.module == "newspaper")).scalars():
            changed = False
            for attr in ("screenshot_path", "full_screenshot_path"):
                if getattr(m, attr) and not _alive(getattr(m, attr)):
                    setattr(m, attr, None)
                    changed = True
            if changed:
                cleared += 1
        session.commit()
        print(f"[3/3] dead website-screenshot paths cleared: {cleared} — re-capturing…")
    finally:
        session.close()

    from app.newspaper.screenshots import backfill_screenshots

    for i in range(args.backfill_rounds):
        r = backfill_screenshots(limit=40)
        print(f"      backfill round {i + 1}: captured {r.get('captured', 0)}, "
              f"{max(r.get('missing', 0) - r.get('captured', 0), 0)} still queued")
        if not r.get("missing"):
            break
    print("Done. Scheduled scans keep re-capturing any remainder automatically.")


if __name__ == "__main__":
    main()
