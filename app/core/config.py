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

    # M6: the tool-calling agent (INTERFACES.md §5) calls DeepSeek V4 through
    # OpenRouter's OpenAI-compatible API. The key MUST come from .env / the
    # environment - never hardcode it. Optional at runtime: the agent path is
    # only exercised when a worker actually runs a low_stock_reorder job, and
    # the test suite injects a fake LLM, so the app must not fail at startup
    # when the key is absent.
    openrouter_api_key: str | None = None
    # Model slug on OpenRouter, env-overridable. "deepseek/deepseek-v4-flash"
    # is the stable unversioned slug (versioned builds exist, e.g.
    # deepseek/deepseek-v4-flash-0731); pin a versioned slug in prod if the
    # unversioned one's behavior must never drift.
    openrouter_model: str = "deepseek/deepseek-v4-flash"


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (import-time singleton)."""
    return Settings()
