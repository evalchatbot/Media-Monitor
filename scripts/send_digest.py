"""Build and send/preview the daily digest now:  python -m scripts.send_digest

Emails if SMTP is configured; otherwise writes an HTML file you can open.
"""
from __future__ import annotations

import logging

from app.db.base import init_db
from app.digest.sender import send_daily_digest

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

if __name__ == "__main__":
    init_db()
    print("Digest result:", send_daily_digest())
