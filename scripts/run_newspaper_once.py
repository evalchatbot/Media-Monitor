"""Run a single newspaper scan end-to-end:  python -m scripts.run_newspaper_once

This is the quickest way to verify the whole pipeline (scrape -> match ->
screenshot -> store -> alert) without starting the scheduler or web server.
"""
from __future__ import annotations

import logging

from app.db.base import init_db
from app.newspaper.pipeline import run_newspaper_scan

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

if __name__ == "__main__":
    init_db()
    summary = run_newspaper_scan()
    print("\nScan complete:", summary)
