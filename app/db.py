from __future__ import annotations

import json
import os
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker


DEFAULT_SQLITE_PATH = Path("data") / "ai_news.sqlite3"


def _normalize_database_url(url: str) -> str:
    """Return a SQLAlchemy URL that works locally and on hosted platforms."""
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    if url.startswith("postgresql://") and "+psycopg" not in url:
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


DATABASE_URL = _normalize_database_url(
    os.getenv("DATABASE_URL", f"sqlite:///{DEFAULT_SQLITE_PATH}")
)

if DATABASE_URL.startswith("sqlite:///"):
    db_path = DATABASE_URL.replace("sqlite:///", "", 1)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    _connect_args = {"check_same_thread": False, "timeout": 30}
else:
    _connect_args = {}

engine = create_engine(
    DATABASE_URL,
    connect_args=_connect_args,
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

if DATABASE_URL.startswith("sqlite:///"):
    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_connection, _connection_record) -> None:
        """Make concurrent crawler/log writes predictable on local SQLite."""
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()


def init_db() -> None:
    """Create missing tables.

    The app intentionally uses create_all for the first public version so hosted
    deployment stays close to Streamlit-level difficulty. If schema migrations
    become necessary later, Alembic can be added without touching business code.
    """
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _ensure_custom_source_columns()
    _normalize_historical_news_types()


def _ensure_custom_source_columns() -> None:
    """Add lightweight columns used by source overrides on existing installs."""
    inspector = inspect(engine)
    if "custom_sources" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("custom_sources")}
    statements = []
    if "source_key" not in columns:
        statements.append(
            "ALTER TABLE custom_sources "
            "ADD COLUMN source_key VARCHAR(320) NOT NULL DEFAULT ''"
        )
    if "is_builtin" not in columns:
        statements.append(
            "ALTER TABLE custom_sources "
            "ADD COLUMN is_builtin BOOLEAN NOT NULL DEFAULT FALSE"
        )
    if "api_key" not in columns:
        statements.append(
            "ALTER TABLE custom_sources "
            "ADD COLUMN api_key TEXT NOT NULL DEFAULT ''"
        )
    if not statements:
        return
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))


def _normalize_historical_news_types() -> int:
    """Promote legacy financing and embodied-AI records to dedicated topics."""
    from app.models import StructuredNewsRecord
    from news_processor import classify_special_news_type

    db = SessionLocal()
    updated = 0
    try:
        rows = db.query(StructuredNewsRecord).all()
        for row in rows:
            target_type = classify_special_news_type(
                row.event,
                row.detail,
                row.impact,
                row.news_type,
            )
            if target_type == row.news_type:
                continue
            row.news_type = target_type
            try:
                payload = json.loads(row.payload_json or "{}")
            except (TypeError, ValueError):
                payload = None
            if isinstance(payload, dict):
                payload["news_type"] = target_type
                row.payload_json = json.dumps(payload, ensure_ascii=False)
            updated += 1
        if updated:
            db.commit()
        return updated
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
