"""Create database tables. Run once before first use:  python -m scripts.init_db"""
from __future__ import annotations

from app.db.base import init_db

if __name__ == "__main__":
    init_db()
    print("Database initialized.")
