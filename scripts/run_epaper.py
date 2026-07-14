"""Run one e-paper cycle (fetch editions -> read pages -> match keywords) as a
standalone process, launched by the web app (app.epaper.scan_runner) or the
scheduler. Writes a status file the web app polls for the result.

Usage:
  python -m scripts.run_epaper                    # fetch + read + match, all papers
  python -m scripts.run_epaper --no-fetch         # re-read/re-match stored pages only
  python -m scripts.run_epaper --papers jang,dawn # limit to specific papers
  python -m scripts.run_epaper --keyword-ids 1,3  # match only these keywords
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone

from config import BASE_DIR
from app.db.base import init_db
from app.epaper.pipeline import run_epaper_scan

_STATUS_FILE = BASE_DIR / "data" / "last_epaper_scan.json"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--keyword-ids", default="", help="comma-separated keyword ids")
    parser.add_argument("--label", default="", help="label for status display")
    parser.add_argument("--papers", default="", help="comma-separated paper slugs")
    parser.add_argument("--no-fetch", action="store_true", help="skip edition download")
    args = parser.parse_args()

    keyword_ids = [int(x) for x in args.keyword_ids.split(",") if x.strip()] or None
    papers = [p.strip() for p in args.papers.split(",") if p.strip()] or None

    init_db()
    summary = run_epaper_scan(keyword_ids=keyword_ids, fetch=not args.no_fetch, papers=papers)

    _STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATUS_FILE.write_text(
        json.dumps({
            "summary": summary,
            "label": args.label or None,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }),
        encoding="utf-8",
    )
    print("E-paper cycle complete:", summary)


if __name__ == "__main__":
    main()
