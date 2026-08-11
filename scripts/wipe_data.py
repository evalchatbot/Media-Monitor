"""One-time data reset for the live-only model.

Clears everything that used to be *stored* — result cards, cached article bodies,
e-paper pages, transcripts, bulletin runs — plus the current keyword watchlist,
and empties the storage volume. KEEPS YouTube channels and their bulletin slots
(source config, not results). Keywords are backed up to a JSON file first.

Run:  python -m scripts.wipe_data
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime

from sqlalchemy import delete

from config import settings
from app.db.base import SessionLocal
from app.db.models import (
    ArticleCache, EPaperPage, Keyword, Mention, ScrapeRun, Transcript, YouTubeBulletin,
)


def _backup_keywords(session) -> str:
    rows = session.query(Keyword).all()
    data = [
        {"text": k.text, "language": k.language, "module": k.module, "active": k.active}
        for k in rows
    ]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = settings.storage_dir.parent / f"keywords_backup_{stamp}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


# Every table that holds scraped/stored RESULTS (not source config).
RESULT_TABLES = [Mention, ArticleCache, EPaperPage, Transcript, YouTubeBulletin, ScrapeRun]


def main() -> None:
    session = SessionLocal()
    try:
        before = {t.__name__: session.query(t).count() for t in RESULT_TABLES}
        kw_count = session.query(Keyword).count()

        backup = _backup_keywords(session)
        print(f"Backed up {kw_count} keywords -> {backup}")

        for table in RESULT_TABLES:
            n = session.execute(delete(table)).rowcount
            print(f"  cleared {table.__name__}: {n} rows")
        n = session.execute(delete(Keyword)).rowcount
        print(f"  cleared Keyword: {n} rows")
        session.commit()

        print("before result-row counts:", before)
    finally:
        session.close()

    # Empty the storage volume (screenshots, e-paper images, YouTube frames,
    # ticker cutouts) but keep the directory itself.
    storage = settings.storage_dir
    removed = 0
    if storage.exists():
        for child in storage.iterdir():
            try:
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
                removed += 1
            except Exception as exc:  # pragma: no cover
                print(f"  could not remove {child}: {exc}")
    storage.mkdir(parents=True, exist_ok=True)
    print(f"cleared {removed} entries under {storage}")
    print("DONE. Channels + bulletin slots kept; keywords + all results cleared.")


if __name__ == "__main__":
    main()
