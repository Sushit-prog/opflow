"""Application settings, loaded from environment variables.

The DATABASE_URL defaults to the docker-compose.yml Postgres instance so
that a bare `alembic upgrade head` / dev run works with zero configuration.
Override via the DATABASE_URL environment variable (e.g. for CI or prod).
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for OpFlow.

    Values come from environment variables (highest priority), then the
    optional local .env file, then the defaults below.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # docker-compose.yml db service credentials by default.
    # PHASE 0 DEVIATION (documented): host port remapped to 5433 because a
    # native Windows PostgreSQL 18 service owns 5432 (no admin access to stop
    # it). Revert to 5432 when that conflict is resolved.
    database_url: str = (
        "postgresql+psycopg://opflow:REDACTED@localhost:5433/opflow"
    )


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (import-time singleton)."""
    return Settings()
