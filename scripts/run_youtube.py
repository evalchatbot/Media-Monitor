"""Run one YouTube channel scan:  python -m scripts.run_youtube

  --keyword-ids 1,2   restrict to these keywords
  --channel-ids 3     restrict to these channels
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone

from config import BASE_DIR
from app.db.base import init_db
from app.youtube.pipeline import run_youtube_live_scan, run_youtube_scan

_STATUS_FILE = BASE_DIR / "data" / "last_youtube_scan.json"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--keyword-ids", default="")
    parser.add_argument("--channel-ids", default="")
    parser.add_argument("--label", default="")
    parser.add_argument("--live", action="store_true", help="check live streams instead of uploads")
    args = parser.parse_args()

    kw = [int(x) for x in args.keyword_ids.split(",") if x.strip()] or None
    ch = [int(x) for x in args.channel_ids.split(",") if x.strip()] or None

    init_db()
    if args.live:
        summary = run_youtube_live_scan(keyword_ids=kw, channel_ids=ch)
    else:
        summary = run_youtube_scan(keyword_ids=kw, channel_ids=ch)

    _STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATUS_FILE.write_text(
        json.dumps({"summary": summary, "finished_at": datetime.now(timezone.utc).isoformat()}),
        encoding="utf-8",
    )
    print("YouTube scan complete:", summary)


if __name__ == "__main__":
    main()
