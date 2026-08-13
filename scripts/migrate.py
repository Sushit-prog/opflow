"""Development utility: run `alembic upgrade head` against the configured DB.

Usage (from repo root):
    python scripts/migrate.py [revision]

The default revision is "head". Requires the docker-compose `db` service to be
up and reachable at the configured DATABASE_URL (default localhost:5433 for
Phase 0). Not application logic — schema/scaffolding tooling only.
"""
from __future__ import annotations

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    revision = sys.argv[1] if len(sys.argv) > 1 else "head"
    cfg = Config(str(ROOT / "alembic.ini"))
    command.upgrade(cfg, revision)
    print(f"[migrate] upgraded to {revision}")


if __name__ == "__main__":
    main()
