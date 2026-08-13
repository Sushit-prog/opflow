"""Database engine and session factory.

This module is shared by Alembic's env.py and by the test harness so that
both run migrations and connect through the exact same configuration.
"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


def create_db_engine(database_url: str | None = None) -> Engine:
    """Create a SQLAlchemy engine for the given (or default) DATABASE_URL."""
    url = database_url or get_settings().database_url
    return create_engine(url, pool_pre_ping=True)


# Default engine/session factory bound to the configured DATABASE_URL.
engine = create_db_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session() -> Session:
    """Yield a Session as a FastAPI dependency."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
