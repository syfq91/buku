"""Database connection, engine configuration, and migration management."""

from __future__ import annotations

import logging
from collections.abc import Generator
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from buku.config import get_settings

logger = logging.getLogger("buku.db")

_global_engine: Engine | None = None
_global_sessionmaker: sessionmaker[Session] | None = None


def get_engine(db_url: str | None = None) -> Engine:
    """Create or return SQLAlchemy Engine configured with SQLite optimizations."""
    global _global_engine
    if db_url is None and _global_engine is not None:
        return _global_engine

    target_url = db_url or get_settings().effective_database_url
    is_sqlite = target_url.startswith("sqlite")

    connect_args: dict[str, Any] = {}
    if is_sqlite:
        connect_args["check_same_thread"] = False

    engine = create_engine(
        target_url,
        connect_args=connect_args,
        echo=False,
    )

    if is_sqlite:
        # Enforce foreign keys, WAL mode, and busy timeout for concurrent access
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection: Any, connection_record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys = ON;")
            cursor.execute("PRAGMA journal_mode = WAL;")
            cursor.execute("PRAGMA busy_timeout = 5000;")
            cursor.close()

    if db_url is None:
        _global_engine = engine

    return engine


def get_session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    """Return configured SQLAlchemy session factory."""
    global _global_sessionmaker
    active_engine = engine or get_engine()
    if engine is None and _global_sessionmaker is not None:
        return _global_sessionmaker

    factory = sessionmaker(
        bind=active_engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )

    if engine is None:
        _global_sessionmaker = factory

    return factory


def get_db() -> Generator[Session]:
    """Dependency / context manager yielding a database session."""
    session_factory = get_session_factory()
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def get_alembic_config(db_url: str | None = None) -> Config:
    """Build Alembic configuration targeting the specified or configured database."""
    target_url = db_url or get_settings().effective_database_url

    # Project root is 3 levels up from src/buku/db.py
    root_dir = Path(__file__).resolve().parent.parent.parent
    ini_path = root_dir / "alembic.ini"
    migrations_dir = root_dir / "migrations"

    if ini_path.is_file():
        cfg = Config(str(ini_path))
    else:
        cfg = Config()
        cfg.set_main_option("script_location", str(migrations_dir))

    cfg.set_main_option("sqlalchemy.url", target_url)
    return cfg


def run_migrations(db_url: str | None = None) -> None:
    """Run database schema migrations to the latest revision (head)."""
    settings = get_settings()
    settings.ensure_directories()
    cfg = get_alembic_config(db_url)
    logger.info("Executing database migrations on %s", cfg.get_main_option("sqlalchemy.url"))
    command.upgrade(cfg, "head")


def reset_engine() -> None:
    """Reset cached global engine and sessionmaker (primarily for testing)."""
    global _global_engine, _global_sessionmaker
    if _global_engine is not None:
        _global_engine.dispose()
    _global_engine = None
    _global_sessionmaker = None
