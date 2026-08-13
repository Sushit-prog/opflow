"""CLI entrypoint for seeding the OpFlow demo dataset (M2 - data only).

Usage (from repo root):
    python scripts/seed.py

Requires DATABASE_URL to be set (via .env or environment) - the app's Settings
fail loudly if it is missing. Idempotent: safe to run repeatedly.
"""
from __future__ import annotations

from app.core.config import get_settings
from app.db import SessionLocal
from app.seed import seed_all


def main() -> None:
    # Force settings resolution so a missing DATABASE_URL fails loudly here.
    get_settings().database_url
    with SessionLocal() as session:
        result = seed_all(session)
    print(
        f"[seed] done: vendors inserted={result['vendors_created']}, "
        f"items inserted={result['items_created']}"
    )


if __name__ == "__main__":
    main()
