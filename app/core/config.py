"""Application settings, loaded from environment variables.

DATABASE_URL is REQUIRED - it must be provided via the .env file or the
environment. The app fails loudly at startup if it is missing, rather than
silently falling back to a hardcoded, real-looking connection string.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for OpFlow.

    Values come from environment variables (highest priority), then the
    optional local .env file.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # This process only declares DATABASE_URL. Ignore unrelated host env
        # vars (e.g. POSTGRES_* left over in the shell) instead of failing.
        extra="ignore",
    )

    # PHASE 0 DEVIATION (documented): the compose db service is on host port
    # 5433 (not 5432) because a native Windows PostgreSQL 18 service owns 5432
    # on the dev machine and can't be stopped without admin. Revert to 5432
    # when that conflict is resolved. DATABASE_URL carries the full DSN with
    # credentials and MUST come from .env / the environment - never hardcode it.
    database_url: str


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (import-time singleton)."""
    return Settings()
