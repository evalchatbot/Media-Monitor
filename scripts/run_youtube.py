"""Run one YouTube bulletin scan as a standalone process."""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone

from config import BASE_DIR
from app.db.base import init_db
from app.youtube.pipeline import run_youtube_scan

_STATUS_FILE = BASE_DIR / "data" / "last_youtube_scan.json"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--keyword-ids", default="", help="comma-separated keyword ids")
    parser.add_argument("--channel-ids", default="", help="comma-separated channel db ids")
    parser.add_argument("--label", default="", help="label for status display")
    parser.add_argument("--slot-date", default="", help="YYYY-MM-DD")
    parser.add_argument("--slot-times", default="", help="comma-separated HH:MM:SS bulletin times")
    parser.add_argument("--match-only", action="store_true",
                        help="match cached transcripts only (no discovery/transcription)")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    keyword_ids = [int(x) for x in args.keyword_ids.split(",") if x.strip()] or None
    channel_ids = [int(x) for x in args.channel_ids.split(",") if x.strip()] or None
    slot_times = [x.strip() for x in args.slot_times.split(",") if x.strip()] or None

    init_db()
    summary = run_youtube_scan(
        keyword_ids=keyword_ids,
        channel_ids=channel_ids,
        slot_date=args.slot_date or None,
        slot_times=slot_times,
        force=args.force,
        match_only=args.match_only,
    )

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
    print("YouTube scan complete:", summary)


if __name__ == "__main__":
    main()
