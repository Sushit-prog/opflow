# OpFlow

A schema-driven operations-automation backend. Concrete v1 use case: when an
inventory item drops below its reorder threshold, a background worker drafts
a purchase order and notifies the vendor — via a capability-gated tool-calling
agent (DeepSeek V4 via OpenRouter), not hardcoded if/else logic.

The full pipeline is implemented and tested (M1–M8): poller → idempotent job
queue → atomic claim → agent → capability gate → audit log → crash-resume
retries — all behind a real Postgres, with CI on every PR.

## Tech stack

- Python 3.12, FastAPI, SQLAlchemy 2, Alembic, Pydantic v2, httpx
- PostgreSQL 16 (Docker Compose; see `docker-compose.yml`)
- Hand-rolled DB-backed job queue (no Redis/Celery)
- Capability-gated tool-calling agent (DeepSeek V4 via OpenRouter)

## Getting started

```bash
docker compose up -d db          # start Postgres (host port 5433 — see port note)
python -m venv .venv             # once
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\alembic upgrade head
.venv\Scripts\python scripts/seed.py     # idempotent demo seed (vendors + items)
.venv\Scripts\python -m pytest tests/integration   # expect 27 passed
```

### Port note (environment deviation)

The dev machine has a native Windows PostgreSQL 18 service already bound to
host port **5432**. Because this shell lacks admin rights to stop it, the
compose `db` service is mapped to host port **5433**, and `DATABASE_URL`
defaults to `:5433`. Revert both when the 5432 conflict is resolved.

## Environment (`.env`)

`.env` is gitignored — credentials live there, never in the repo. Copy the
template below and fill it in:

```dotenv
# REQUIRED — the app fails loudly at startup without it.
DATABASE_URL=postgresql+psycopg://opflow:YOUR_PASSWORD@localhost:5433/opflow

# REQUIRED only to run the agent for real (the worker calls DeepSeek V4 via
# OpenRouter). The test suite mocks the LLM and never needs it.
OPENROUTER_API_KEY=sk-or-v1-...

# OPTIONAL — model slug on OpenRouter (default shown).
OPENROUTER_MODEL=deepseek/deepseek-v4-flash
```

The worker itself (poll + job lifecycle + capability gate) runs with just
`DATABASE_URL`; only the agent step needs `OPENROUTER_API_KEY`.

## Running the worker loop

The worker is the scheduler: every round it polls for low-stock items
(idempotent, date-scoped), then claims and executes due jobs through the
agent, completing each with success / retry-backoff / failed.

```bash
# run forever, one round every 60s (Ctrl+C to stop)
.venv\Scripts\python -m app.runner

# single poll + worker round, then exit (cron-friendly)
.venv\Scripts\python -m app.runner --once

# override the sleep between rounds
.venv\Scripts\python -m app.runner --interval 30
```

Each round logs the jobs the poller touched and the worker processed. A round
processes at most one job per item per day, and a job killed mid-execution is
recovered on the next round without duplicating its side effects.

## Running the API

```bash
.venv\Scripts\uvicorn app.main:app --port 8000
```

| Endpoint | Purpose |
|---|---|
| `GET /health` | Liveness + real DB connectivity check |
| `POST /worker/round` | On-demand trigger: one poll + one full worker round; returns `{status, jobs_polled, jobs_processed}` |

Example:

```bash
curl -X POST http://localhost:8000/worker/round
# {"status":"ok","jobs_polled":[127],"jobs_processed":[130]}
```

## How a job flows

1. **Poller** (`app/poller.py`) finds items below threshold and enqueues a
   date-scoped, idempotent job (`app/jobs.py`).
2. **Worker** (`app/worker.py`) atomically claims due jobs (`UPDATE ... WHERE
   status IN ('pending','retrying') AND next_run_at <= now()` — the
   concurrency guard), then runs the executor.
3. **Agent** (`app/agent.py`) fetches a *fresh* inventory snapshot, asks
   DeepSeek V4 (via OpenRouter) for a structured `{action, reasoning}`
   decision, and acts only through the **capability gate**.
4. **Gate** (`app/gate.py`) checks `process_types.allowed_tools` and writes
   one `audit_log` row per tool call — allowed or rejected — with the agent's
   reasoning in `decision_reasoning`. Tools live in `app/tools.py`
   (`query_inventory`, `create_purchase_order` [idempotent on
   `created_by_job_id`], `notify_vendor` [log-only]).
5. Failures retry with exact exponential backoff; crashes are recovered by
   the lease-based stale-`running` reset; tool side effects never duplicate.

## CI

GitHub Actions (`.github/workflows/test.yml`) runs the full integration suite
against a real Postgres 16 service container on every PR and push to `main` —
Alembic migrations applied first, exactly as locally. Lint/type-check are not
configured in this repo yet.

## Layout

```
app/            application package
  agent.py        DeepSeek V4 tool-calling agent (INTERFACES.md §5)
  gate.py         capability gate — the only path tools may be called (§3)
  tools.py        the three real tools (§1)
  jobs.py         job lifecycle: enqueue / claim / complete / backoff (§2)
  worker.py       worker round: recover, claim, execute, complete
  runner.py       worker service loop (CLI scheduler)
  poller.py       low-stock poller (§4)
  errors.py       domain error taxonomy (§6)
  models.py       SQLAlchemy ORM mirroring the migration DDL
migrations/     Alembic migrations (initial DDL in versions/0001_initial_schema.py)
tests/          integration/ hits real Postgres (27 tests)
scripts/        dev utilities (seed)
```

## Milestones

| # | Delivered |
|---|---|
| M1 | FastAPI + `/health` + ORM models |
| M2 | Idempotent seed (vendors, inventory) |
| M3 | Poller + idempotent enqueue |
| M4 | Worker loop: atomic claim, backoff, crash-resume idempotency |
| M5 | Capability-gated tools + audit log |
| M6 | DeepSeek V4 agent via OpenRouter |
| M7 | Failure-injection tests (connection drop, malformed payloads, tool failures) |
| M8 | GitHub Actions CI on real Postgres |

## License

Proprietary (portfolio project).
