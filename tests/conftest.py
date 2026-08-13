"""Pytest fixtures for integration tests.

Every fixture here talks to a REAL Postgres instance (the docker-compose
`db` service by default). There are NO mocks in this file — these tests
exist to prove the migrations and SQL really work.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from app.core.config import get_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _alembic_cfg() -> Config:
    """Build an Alembic Config rooted at the repository's alembic.ini."""
    return Config(str(PROJECT_ROOT / "alembic.ini"))


@pytest.fixture(scope="session")
def database_url() -> str:
    """The database URL under test (DATABASE_URL / defaults)."""
    return get_settings().database_url


@pytest.fixture(scope="session")
def migrate() -> Callable[[str], None]:
    """Run `alembic upgrade <revision>` (idempotent) against the real DB."""

    def _run(revision: str = "head") -> None:
        alembic_command.upgrade(_alembic_cfg(), revision)

    return _run


@pytest.fixture(scope="session")
def engine(database_url: str) -> Engine:
    """SQLAlchemy engine talking to the real Postgres."""
    eng = create_engine(database_url)
    try:
        yield eng
    finally:
        eng.dispose()
