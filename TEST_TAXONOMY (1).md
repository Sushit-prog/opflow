# OpFlow — Test Taxonomy (Phase 0)

Rule carried over from SOPVM: every milestone below is closed only after its drift check passes against the real diff — not after code merely "looks right." A milestone is not done until its proof-test exists and is run, not mocked around.

---

## M1 — Scaffold
- `test_health_endpoint` — `/health` returns 200 with DB connectivity confirmed (not just app-up)
- `test_migrations_create_schema` — run `alembic upgrade head` against a clean DB, assert every table + constraint from the DDL exists (query `information_schema`, don't eyeball it)

## M2 — Seed data
- `test_seed_produces_below_threshold_item` — after seeding, assert at least one `inventory_items` row has `quantity_on_hand < reorder_threshold`
- `test_seed_process_type_exists` — `low_stock_reorder` row present with correct `allowed_tools`

## M3 — Poller (read-only)
- `test_poller_creates_job_for_low_stock_item`
- `test_poller_idempotent_on_rerun` — **the load-bearing test.** Run `poll_low_stock()` twice in a row, assert exactly one `jobs` row exists for that item (not two). Must hit a real DB, not a mock.
- `test_poller_ignores_items_above_threshold`
- `test_poller_does_not_mutate_inventory` — assert `inventory_items` row is byte-identical before/after poll

## M4 — Worker loop
- `test_claim_job_marks_running`
- `test_claim_job_race_only_one_worker_wins` — two concurrent `claim_job()` calls on the same `job_id`, assert exactly one returns True
- `test_failure_increments_attempts_and_backs_off` — assert `next_run_at` moves forward per the fixed backoff formula
- `test_max_attempts_moves_to_failed`
- `test_crash_resume_no_duplicate_action` — **the load-bearing test.** Simulate a worker crashing mid-`execute_job` (kill between claim and complete), restart, assert the job resumes/retries without a duplicate `purchase_orders` row (this is what proves the idempotency story is real end-to-end, not just at the queue layer)

## M5 — Capability-gated tools
- `test_whitelisted_tool_call_succeeds`
- `test_non_whitelisted_tool_call_is_blocked` — **the highest-signal test in the project.** Deliberately call a tool not in `process_types.allowed_tools`, assert `ToolNotWhitelisted` is raised AND a rejection row lands in `audit_log`
- `test_every_tool_call_writes_audit_log_row` — allowed and rejected calls both produce exactly one row each
- `test_create_purchase_order_idempotent_on_same_job_id` — call the tool twice with the same `created_by_job_id`, assert only one `purchase_orders` row exists

## M6 — Agent wiring
- `test_agent_produces_structured_decision` — output matches `AgentResult` schema, not free text
- `test_agent_reasoning_persisted_to_audit_log`
- `test_agent_never_writes_db_directly` — static/code-level check: agent module has no SQLAlchemy session or raw SQL import
- `test_agent_uses_fresh_inventory_snapshot_not_stale_payload`

## M7 — Failure injection
- `test_db_connection_drop_mid_poll` — job never left stuck in `running` forever
- `test_malformed_job_payload_fails_cleanly` — goes to `retrying`/`failed`, not an unhandled exception that crashes the worker process
- `test_tool_call_raises_mid_execution` — job marked `retrying`/`failed`, no partial `purchase_orders` row committed

## M8 — CI
- GitHub Actions workflow runs lint + type check + full test suite against a real Postgres service container on every PR (not SQLite-in-CI-only — the schema uses Postgres-specific types like JSONB)

## M9 — README + demo
- Not a code test — verification is a human watching the demo clip and confirming: (1) idempotency proof visible, (2) crash-resume proof visible, (3) blocked-tool-call proof visible, (4) audit_log rows readable end-to-end for one full job

---

## Cross-cutting (run at every milestone from M3 onward)
- `test_full_pipeline_no_field_dropped` — the SOPVM postmortem lesson: when new fields get threaded through multiple layers (poller → job → worker → tool → audit_log), test the *full path*, not just the layer you just touched. Don't let a later layer silently drop what an earlier layer produced.
