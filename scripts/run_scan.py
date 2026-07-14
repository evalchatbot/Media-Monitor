"""Run one uncapped newspaper scan as a standalone process.

Invoked by the web app (scan_manager) as a subprocess so Playwright runs in its
own process main thread — where it's stable — instead of a thread under the
async server. Writes a small status file the web app polls for the result.

Usage:
  python -m scripts.run_scan                 # all active keywords
  python -m scripts.run_scan --keyword-ids 1,3
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone

from config import BASE_DIR
from app.db.base import init_db
from app.newspaper.pipeline import run_newspaper_scan

_STATUS_FILE = BASE_DIR / "data" / "last_scan.json"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--keyword-ids", default="", help="comma-separated keyword ids")
    parser.add_argument("--label", default="", help="keyword label (for status display)")
    parser.add_argument("--capped", action="store_true", help="bounded fetch (scheduled scans)")
    parser.add_argument("--backfill-only", action="store_true",
                        help="skip scanning; only capture missing detection screenshots")
    args = parser.parse_args()

    keyword_ids = [int(x) for x in args.keyword_ids.split(",") if x.strip()] or None

    init_db()
    from app.newspaper.screenshots import backfill_screenshots

    if args.backfill_only:
        summary = backfill_screenshots(limit=40)
    else:
        summary = run_newspaper_scan(keyword_ids=keyword_ids, uncapped=not args.capped)
        # Give quick-scan detections their visuals while the browser is warm.
        summary["screenshots_backfilled"] = backfill_screenshots(limit=25).get("captured", 0)

    _STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATUS_FILE.write_text(
        json.dumps(
            {
                "summary": summary,
                "label": args.label or None,
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    print("Scan complete:", summary)


if __name__ == "__main__":
    main()
