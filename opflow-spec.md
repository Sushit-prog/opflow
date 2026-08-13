# OpFlow — Implementation Spec

**Purpose:** Portfolio project built specifically for the JD Jones & Co. "AI & Coding Intern" application (Kolkata manufacturer, fluid sealing products). Target: prove production-grade backend + AI-agent engineering, not another RAG chatbot.

**Tooling:** Cline (VS Code) + DeepSeek V4 as the AI coding agent for implementation.

---

## Why this project (JD → project mapping)

The JD's three real filters, in order of how hard they're screening for them:

1. **Background job / worker patterns** — queues, polling loops, retries, idempotent processing (named explicitly, unusual specificity for an internship posting)
2. **Schema/config-driven systems** that generalize instead of hardcoded one-offs
3. **AI agents that perform real actions** — tool-calling that triggers workflows on operational data, explicitly *not* chat-only (they reject "chatbot-only" projects by name)

Everything else (Python/SQL competence, AI coding tool experience, "real project not tutorial") is table stakes to get read at all.

**The project:** OpFlow — a schema-driven operational automation engine. Process types (e.g. "low stock → draft PO → notify vendor") are defined as data (`process_types` table), not code. A poller finds triggered work, enqueues idempotent jobs, a worker executes them with retry/backoff, and a capability-gated tool-calling agent performs the actual action — inventory query, PO creation, vendor notification — logged to an audit trail.

Explicitly **not** using SOPVM as a dependency for this build — kept as a standalone repo purpose-built for this JD, no risk of looking bolted-on to a prior project.

---

## Requirement → Feature Mapping

| JD Requirement | OpFlow Feature | Evidence to produce |
|---|---|---|
| Background jobs: queues, polling, retries, idempotency | Hand-rolled DB-backed `jobs` table + poller + worker | Kill worker mid-job, restart, prove no duplicate action. Submit same trigger twice, prove only one job row exists (idempotency_key unique constraint). |
| Schema/config-driven generalization | `process_types` table drives behavior, not if/else code | Add a second process type as a data row only, no redeploy |
| SQL: complex queries, schema design, optimization | Normalized schema, indexed poller query | `EXPLAIN ANALYZE` on the hot-path query showing index scan |
| AI agents performing real actions | Tool-calling agent gated by `process_types.allowed_tools` | Deliberately call a non-whitelisted tool, show it's blocked and logged |
| Backend: APIs, DB, server-side logic | FastAPI + Postgres + SQLAlchemy + Alembic | Working endpoints, migration history |
| AI-native dev tooling | Built via Cline + DeepSeek V4 | Documented honestly in README |
| Real project, not tutorial | Realistic failure modes: concurrency, retries, partial failure | The whole system is the evidence |
| Robotics (JD skill tag) | Not addressed | Flagged honestly as out of scope — not fabricated |

---

## Architecture

- **Language/API:** Python, FastAPI
- **DB:** PostgreSQL (Docker Compose locally; Neon/Supabase free tier if deployed)
- **Queue:** hand-rolled DB-backed job table (no Redis/Celery) — deliberately explainable end-to-end, fits 8GB RAM/no-GPU hardware
- **Agent:** DeepSeek V4 (via OpenRouter) as tool-calling agent, gated by a capability whitelist per process type
- **Migrations:** Alembic, real history from commit 1
- **CI:** GitHub Actions — lint, type check, tests against a Postgres service container
- **Observability:** structured `audit_log` table (tool called, input, output, agent reasoning) — answers "why did this fail/what did the agent decide"
- **Deployment:** Docker Compose, single-node, explicitly labeled as such (with an honest "at 10x scale I'd move to Redis/Celery, here's why" note — don't build what you can't defend)

---

## Schema (v1 — concrete reorder flow, generalize later)

```sql
CREATE TABLE vendors (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    contact_email   TEXT NOT NULL,
    lead_time_days  INTEGER NOT NULL DEFAULT 7,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE inventory_items (
    id                SERIAL PRIMARY KEY,
    sku               TEXT NOT NULL UNIQUE,
    name              TEXT NOT NULL,
    quantity_on_hand  INTEGER NOT NULL DEFAULT 0 CHECK (quantity_on_hand >= 0),
    reorder_threshold INTEGER NOT NULL,
    reorder_quantity  INTEGER NOT NULL,
    vendor_id         INTEGER NOT NULL REFERENCES vendors(id),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_inventory_vendor ON inventory_items(vendor_id);
CREATE INDEX idx_inventory_reorder_check ON inventory_items(quantity_on_hand, reorder_threshold);

CREATE TABLE process_types (
    id                     SERIAL PRIMARY KEY,
    name                   TEXT NOT NULL UNIQUE,
    description            TEXT,
    poll_interval_seconds  INTEGER NOT NULL DEFAULT 60,
    allowed_tools          JSONB NOT NULL,
    max_attempts           INTEGER NOT NULL DEFAULT 3,
    enabled                BOOLEAN NOT NULL DEFAULT true,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE jobs (
    id               SERIAL PRIMARY KEY,
    process_type_id  INTEGER NOT NULL REFERENCES process_types(id),
    idempotency_key  TEXT NOT NULL UNIQUE,
    inventory_item_id INTEGER REFERENCES inventory_items(id),
    status           TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending','running','succeeded','failed','retrying')),
    attempts         INTEGER NOT NULL DEFAULT 0,
    next_run_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload          JSONB,
    error            TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_jobs_poll ON jobs(status, next_run_at);

CREATE TABLE purchase_orders (
    id                  SERIAL PRIMARY KEY,
    vendor_id           INTEGER NOT NULL REFERENCES vendors(id),
    inventory_item_id   INTEGER NOT NULL REFERENCES inventory_items(id),
    quantity            INTEGER NOT NULL CHECK (quantity > 0),
    status              TEXT NOT NULL DEFAULT 'draft'
                          CHECK (status IN ('draft','sent','confirmed')),
    created_by_job_id   INTEGER REFERENCES jobs(id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE audit_log (
    id                 SERIAL PRIMARY KEY,
    job_id             INTEGER NOT NULL REFERENCES jobs(id),
    tool_called        TEXT NOT NULL,
    tool_input         JSONB,
    tool_output        JSONB,
    decision_reasoning TEXT,
    timestamp          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_audit_job ON audit_log(job_id);

INSERT INTO process_types (name, description, poll_interval_seconds, allowed_tools, max_attempts)
VALUES (
    'low_stock_reorder',
    'When an item drops below its reorder threshold, draft a PO and notify the vendor.',
    60,
    '["query_inventory","create_purchase_order","notify_vendor"]'::jsonb,
    3
);
```

---

## Milestone plan (Phase 0 first, then sequential — drift-check each against the real diff before moving on)

**Phase 0 — Spec (write before any code)**
- `INTERFACES.md` — exact input/output contracts for `query_inventory`, `create_purchase_order`, `notify_vendor`
- `TEST_TAXONOMY.md` — what must be tested per milestone
- `docker-compose.yml` + Alembic scaffolding

**M1 — Scaffold**
FastAPI + SQLAlchemy + Alembic project, migrations from the DDL above, docker-compose with Postgres, `/health` endpoint checking DB connectivity. No business logic yet.
*Drift check:* `alembic upgrade head` creates every table/constraint correctly — diff by hand against the DDL.

**M2 — Seed data**
2 vendors, 5 inventory_items (at least one below `reorder_threshold`), the `low_stock_reorder` process_type row.
*Drift check:* query the DB yourself, confirm the trigger condition is true for one row.

**M3 — Poller (read-only)**
Query `inventory_items` for below-threshold rows; for each, upsert a `jobs` row with a computed idempotency_key (hash of process_type_id + inventory_item_id + date), only if that key doesn't already exist. Test: run poller twice, assert only one job row exists.
*Drift check:* this test must be real, not mocked — it's the idempotency proof.

**M4 — Worker loop**
Poll `jobs` where `status IN ('pending','retrying') AND next_run_at <= now()`, lock (`status='running'`), call stub `execute_job()`. On failure: increment attempts, exponential backoff on `next_run_at`, `retrying` until `max_attempts` then `failed`.
*Drift check:* `kill -9` the process mid-`execute_job`, restart, confirm resume without duplication.

**M5 — Capability-gated tools**
Implement `query_inventory`, `create_purchase_order`, `notify_vendor` (log-only). Check every tool call against `process_types.allowed_tools` before execution; raise + log if not whitelisted. Test: deliberately call a non-whitelisted tool, assert it's blocked.
*This is the single highest-signal milestone for the interview — don't rush it.*

**M6 — Agent wiring**
Wire DeepSeek V4 via OpenRouter as the tool-calling agent inside `execute_job` for `low_stock_reorder`: given item state, decide to call `create_purchase_order` then `notify_vendor`. Log reasoning + every tool call to `audit_log`.

**M7 — Failure injection tests**
DB connection dropped mid-poll, malformed job payload, tool call raising mid-execution. Assert jobs never get stuck in `running` forever.

**M8 — CI**
GitHub Actions: lint, mypy, tests against a Postgres service container, on every PR.

**M9 — README + demo**
Write by hand, not via Cline. Architecture diagram, problem/solution, "implemented vs. simulated vs. future production" section, honest limitations, demo clip.

---

## Interview talking points to have ready

- "Why a DB-backed queue instead of Redis/Celery?" → hardware/scope-appropriate, and hand-building it proves you understand what those tools abstract away
- "How do you guarantee idempotency?" → unique constraint on `idempotency_key`, enforced at the DB layer even under concurrent workers
- "What happens when the LLM call fails mid-job?" → job stays `retrying`, never marked `succeeded` until the DB write actually commits — no partial state
- "What would you change at 10x scale?" → move the queue to Redis/Celery or SQS; the `process_types` schema-driven design stays unchanged, it already generalizes
- "Why is a non-whitelisted tool call blocked?" → walk through M5's capability gate and the specific blocked-call test

## Draft resume bullet

"Built a schema-driven operations-automation backend with a Postgres-backed job queue (idempotent retries, exponential backoff) and a capability-gated tool-calling agent that autonomously triggers reorder workflows on simulated manufacturing data — new automation types added via config, zero code changes."
