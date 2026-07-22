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
    engine_kwargs: dict = {"future": True}
    if url.startswith("sqlite"):
        # Ensure the SQLite file's directory exists.
        db_path = url.split("///")[-1]
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread: allow cross-thread use; timeout: wait on locks so a
        # scan process writing doesn't make the web process error with "locked".
        connect_args = {"check_same_thread": False, "timeout": 30}
    else:
        # Postgres/Supabase. The web process and each scan subprocess open their
        # own pool, and the Supabase pooler caps total clients, so keep each
        # pool small and hand connections back quickly.
        engine_kwargs.update(
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout,
            # Recover connections the pooler dropped instead of erroring on the
            # next query — the usual cause of a request hanging then failing.
            pool_pre_ping=True,
            pool_recycle=settings.db_pool_recycle,
        )
        if url.startswith("postgresql+psycopg"):
            # Safe on the transaction pooler (port 6543): that pooler may route
            # successive statements to different server connections, so a
            # server-side prepared statement can vanish mid-flight. Disabling
            # them is also harmless on the session pooler (5432).
            connect_args["prepare_threshold"] = None
    engine = create_engine(url, connect_args=connect_args, **engine_kwargs)
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
    from app.youtube.seed import ensure_seed_channels

    ensure_seed_channels()


def _ensure_columns() -> None:
    """Add columns introduced after a table already exists (create_all won't
    ALTER). Portable across SQLite and Postgres/Supabase."""
    from sqlalchemy import inspect

    insp = inspect(engine)
    tables = set(insp.get_table_names())
    json_t = "jsonb" if engine.dialect.name == "postgresql" else "json"
    if "epaper_pages" in tables:
        cols = {c["name"] for c in insp.get_columns("epaper_pages")}
        if "regions" not in cols:
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    f"ALTER TABLE epaper_pages ADD COLUMN regions {json_t} DEFAULT '[]'"
                )
    if "mentions" in tables:
        cols = {c["name"] for c in insp.get_columns("mentions")}
        if "keyword_media" not in cols:
            default = "'{}'::jsonb" if engine.dialect.name == "postgresql" else "'{}'"
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    f"ALTER TABLE mentions ADD COLUMN keyword_media {json_t} DEFAULT {default}"
                )
        if "keyword_hits" not in cols:
            default = "'{}'::jsonb" if engine.dialect.name == "postgresql" else "'{}'"
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    f"ALTER TABLE mentions ADD COLUMN keyword_hits {json_t} DEFAULT {default}"
                )
    if "transcripts" in tables:
        cols = {c["name"] for c in insp.get_columns("transcripts")}
        additions = []
        if "bulletin_id" not in cols:
            additions.append("bulletin_id INTEGER")
        if "model" not in cols:
            additions.append("model VARCHAR(64) DEFAULT ''")
        if "confidence" not in cols:
            default = "'{}'::jsonb" if engine.dialect.name == "postgresql" else "'{}'"
            additions.append(f"confidence {json_t} DEFAULT {default}")
        if "version" not in cols:
            additions.append("version INTEGER DEFAULT 1")
        if additions:
            with engine.begin() as conn:
                for clause in additions:
                    conn.exec_driver_sql(f"ALTER TABLE transcripts ADD COLUMN {clause}")
    if "youtube_channels" in tables:
        cols = {c["name"] for c in insp.get_columns("youtube_channels")}
        with engine.begin() as conn:
            if "handle" not in cols:
                conn.exec_driver_sql(
                    "ALTER TABLE youtube_channels ADD COLUMN handle VARCHAR(128) DEFAULT ''"
                )
            if "uploads_playlist_id" not in cols:
                conn.exec_driver_sql(
                    "ALTER TABLE youtube_channels ADD COLUMN uploads_playlist_id "
                    "VARCHAR(64) DEFAULT ''"
                )
            if "timezone" not in cols:
                conn.exec_driver_sql(
                    "ALTER TABLE youtube_channels ADD COLUMN timezone VARCHAR(64) "
                    "DEFAULT 'Asia/Karachi'"
                )
            if "media_source" not in cols:
                conn.exec_driver_sql(
                    "ALTER TABLE youtube_channels ADD COLUMN media_source VARCHAR(32) "
                    "DEFAULT 'authorized'"
                )
            if "media_source_config" not in cols:
                default = "'{}'::jsonb" if engine.dialect.name == "postgresql" else "'{}'"
                conn.exec_driver_sql(
                    f"ALTER TABLE youtube_channels ADD COLUMN media_source_config "
                    f"{json_t} DEFAULT {default}"
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
