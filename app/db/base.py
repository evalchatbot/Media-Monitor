"""Database engine, session factory, and declarative base.

Uses SQLAlchemy so the same models run on SQLite (dev) and Postgres (prod);
switching is a `DATABASE_URL` change plus an Alembic migration.
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from config import settings


class Base(DeclarativeBase):
    pass


def _make_engine():
    url = settings.database_url
    connect_args = {}
    if url.startswith("sqlite"):
        # Ensure the SQLite file's directory exists.
        db_path = url.split("///")[-1]
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread: allow cross-thread use; timeout: wait on locks so a
        # scan process writing doesn't make the web process error with "locked".
        connect_args = {"check_same_thread": False, "timeout": 30}
    engine = create_engine(url, connect_args=connect_args, future=True)
    if url.startswith("sqlite"):
        # WAL lets a reader (web) and a writer (scan process) work concurrently.
        from sqlalchemy import event

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

    return engine


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def init_db() -> None:
    """Create all tables + run lightweight migrations. Safe to call repeatedly."""
    # Import models so they register on Base.metadata before create_all.
    from app.db import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _migrate_sqlite(models)
    _ensure_columns()


def _ensure_columns() -> None:
    """Add columns introduced after a table already exists (create_all won't
    ALTER). Portable across SQLite and Postgres/Supabase."""
    from sqlalchemy import inspect

    insp = inspect(engine)
    if "epaper_pages" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("epaper_pages")}
    if "regions" not in cols:
        json_t = "jsonb" if engine.dialect.name == "postgresql" else "json"
        with engine.begin() as conn:
            conn.exec_driver_sql(
                f"ALTER TABLE epaper_pages ADD COLUMN regions {json_t} DEFAULT '[]'"
            )


def _migrate_sqlite(models) -> None:
    """Idempotent SQLite migrations (Postgres/Supabase gets the schema fresh).

    Adds Keyword.module by rebuilding the table (also drops the old
    text+language unique constraint so a word can exist for both modules).
    """
    if not engine.url.get_backend_name().startswith("sqlite"):
        return
    with engine.begin() as conn:
        cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(keywords)").fetchall()]
        if cols and "module" not in cols:
            conn.exec_driver_sql("ALTER TABLE keywords RENAME TO keywords_old")
            models.Keyword.__table__.create(bind=conn)
            conn.exec_driver_sql(
                "INSERT INTO keywords (id, text, language, module, active, created_at) "
                "SELECT id, text, language, 'newspaper', active, created_at FROM keywords_old"
            )
            conn.exec_driver_sql("DROP TABLE keywords_old")
