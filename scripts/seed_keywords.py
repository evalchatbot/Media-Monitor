"""Seed a few starter keywords:  python -m scripts.seed_keywords

Replace these with real ones via the admin panel (POST /keywords).
"""
from __future__ import annotations

from sqlalchemy import select

from app.db.base import SessionLocal, init_db
from app.db.models import Keyword

STARTER = [
    ("Pakistan", "en"),
    ("economy", "en"),
    ("inflation", "en"),
    ("پاکستان", "ur"),
]


def main() -> None:
    init_db()
    session = SessionLocal()
    try:
        for text, lang in STARTER:
            exists = session.execute(
                select(Keyword).where(Keyword.text == text, Keyword.language == lang)
            ).first()
            if not exists:
                session.add(Keyword(text=text, language=lang, active=True))
        session.commit()
        print(f"Seeded {len(STARTER)} keywords (skipping duplicates).")
    finally:
        session.close()


if __name__ == "__main__":
    main()
