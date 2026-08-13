# OpFlow

A schema-driven operations-automation backend. Concrete v1 use case: when an
inventory item drops below its reorder threshold, a background worker drafts
a purchase order and notifies the vendor — via a capability-gated tool-calling
agent, not hardcoded if/else logic.

> **Status: Phase 0 — schema + scaffolding only.** No poller, worker, tools, or
> agent code exists yet. See `opflow-spec.md` for the milestone plan.

## Honest build provenance

This project is being built with **Cline (VS Code) + DeepSeek V4 as the AI
coding agent**, acting as the implementation engineer, with a human acting as
architect/reviewer. The architecture, DDL, and interfaces are specified in
`opflow-spec.md` / `INTERFACES.md` (source of truth) and are **not**
redesigned by the agent.

## Tech stack

- Python 3.12, FastAPI, SQLAlchemy 2, Alembic, Pydantic v2
- PostgreSQL 16 (Docker Compose; see `docker-compose.yml`)
- Hand-rolled DB-backed job queue (no Redis/Celery) — planned for later phases

## Getting started (Phase 0)

```bash
docker compose up -d db          # start Postgres (host port 5433 in Phase 0)
python -m venv .venv             # once
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\alembic upgrade head
.venv\Scripts\python scripts/seed.py     # idempotent demo seed (vendors + items)
.venv\Scripts\python -m pytest tests/integration
```

### Port note (Phase 0 environment deviation)

The dev machine has a native Windows PostgreSQL 18 service already bound to
host port **5432**. Because this shell lacks admin rights to stop it, the
compose `db` service is mapped to host port **5433** for Phase 0, and
`DATABASE_URL` defaults to `:5433`. Revert both when the 5432 conflict is
resolved.

## Layout

```
app/            application package (infra + config; no business logic yet)
migrations/     Alembic migrations (initial DDL in versions/0001_initial_schema.py)
tests/          unit/ and integration/ (integration hits real Postgres)
scripts/        dev utilities (migrate)
```

## License

Proprietary (portfolio project).
