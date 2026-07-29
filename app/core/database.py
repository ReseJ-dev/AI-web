"""Database engine and session configuration."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.core.settings import get_settings


def _enable_sqlite_foreign_keys(
    dbapi_connection: Any,
    connection_record: Any,
) -> None:
    """Enable foreign-key enforcement for each SQLite connection."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def _prepare_sqlite_directory(database_url: str) -> None:
    """Create the parent directory for a local file-backed SQLite database."""
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database:
        return
    if url.database == ":memory:":
        return
    Path(url.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def create_database_engine(database_url: str | None = None) -> Engine:
    """Build an engine suitable for SQLite locally and PostgreSQL in production."""
    url = database_url or get_settings().database_url
    _prepare_sqlite_directory(url)
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    database_engine = create_engine(
        url,
        connect_args=connect_args,
        pool_pre_ping=True,
    )
    if database_engine.dialect.name == "sqlite":
        event.listen(database_engine, "connect", _enable_sqlite_foreign_keys)
    return database_engine


engine = create_database_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db_session() -> Iterator[Session]:
    """Yield a database session and always close it after use."""
    with SessionLocal() as session:
        yield session
