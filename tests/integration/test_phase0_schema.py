"""Phase 0 schema proof-test.

Drift check mandated by opflow-spec.md / TEST_TAXONOMY.md M1:
`test_migrations_create_schema` — run `alembic upgrade head` against a real
Postgres and assert every table + constraint + index from the DDL exists by
querying information_schema / pg_catalog (not by eyeballing).

This runs against a REAL database (see tests/conftest.py) — never mocked.
The expected sets below were captured from Postgres AFTER a clean migration
(see the constraint/index dumps in the previous verification step), so they
reflect the actual objects the DDL produces.
"""
from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

# --- Tables from the DDL (opflow_spec.md) that must exist --------------------
EXPECTED_TABLES: set[str] = {
    "vendors",
    "inventory_items",
    "process_types",
    "jobs",
    "purchase_orders",
    "audit_log",
}

# --- Constraints (table, constraint_name, pg_constraint.contype) -------------
# contype: p = PRIMARY KEY, u = UNIQUE, c = CHECK, f = FOREIGN KEY
_EXPECTED_CONSTRAINTS: set[tuple[str, str, str]] = {
    ("vendors", "vendors_pkey", "p"),
    ("inventory_items", "inventory_items_pkey", "p"),
    ("inventory_items", "inventory_items_sku_key", "u"),
    ("inventory_items", "inventory_items_quantity_on_hand_check", "c"),
    ("inventory_items", "inventory_items_vendor_id_fkey", "f"),
    ("process_types", "process_types_pkey", "p"),
    ("process_types", "process_types_name_key", "u"),
    ("jobs", "jobs_pkey", "p"),
    ("jobs", "jobs_idempotency_key_key", "u"),
    ("jobs", "jobs_status_check", "c"),
    ("jobs", "jobs_process_type_id_fkey", "f"),
    ("jobs", "jobs_inventory_item_id_fkey", "f"),
    ("purchase_orders", "purchase_orders_pkey", "p"),
    ("purchase_orders", "purchase_orders_quantity_check", "c"),
    ("purchase_orders", "purchase_orders_status_check", "c"),
    ("purchase_orders", "purchase_orders_created_by_job_id_fkey", "f"),
    ("purchase_orders", "purchase_orders_inventory_item_id_fkey", "f"),
    ("purchase_orders", "purchase_orders_vendor_id_fkey", "f"),
    ("audit_log", "audit_log_pkey", "p"),
    ("audit_log", "audit_log_job_id_fkey", "f"),
}

# --- Explicit (non-PK/UNIQUE-backed) indexes from the DDL --------------------
EXPECTED_INDEXES: set[tuple[str, str]] = {
    ("inventory_items", "idx_inventory_vendor"),
    ("inventory_items", "idx_inventory_reorder_check"),
    ("jobs", "idx_jobs_poll"),
    ("audit_log", "idx_audit_job"),
}

# --- Trigger from migration 0002 (jobs.idempotency_key immutability) ---------
# The drift check queries pg_get_triggerdef() and asserts the live trigger
# renders exactly the DDL the migration file commits (name, timing, event,
# row-level, WHEN clause, and the raising function). The exact string below
# is Postgres' canonical render of the migration's CREATE TRIGGER (captured
# from a clean run of migration 0002) - schema-qualified table, doubled
# parens around the WHEN expression, lowercase new/old rows.
EXPECTED_TRIGGER_DEF: str = (
    "CREATE TRIGGER jobs_idempotency_key_immutable "
    "BEFORE UPDATE ON public.jobs "
    "FOR EACH ROW "
    "WHEN ((new.idempotency_key IS DISTINCT FROM old.idempotency_key)) "
    "EXECUTE FUNCTION jobs_idempotency_key_immutable()"
)

_TABLES_SQL = text(
    "SELECT table_name FROM information_schema.tables "
    "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
)
_CONSTRAINTS_SQL = text(
    "SELECT c.conname, c.contype "
    "FROM pg_constraint c "
    "JOIN pg_class t ON t.oid = c.conrelid "
    "JOIN pg_namespace n ON n.oid = t.relnamespace "
    "WHERE n.nspname = 'public'"
)
_INDEXES_SQL = text(
    "SELECT tablename, indexname FROM pg_indexes WHERE schemaname = 'public'"
)
_TRIGGERS_SQL = text(
    "SELECT pg_get_triggerdef(t.oid) "
    "FROM pg_trigger t "
    "JOIN pg_class c ON c.oid = t.tgrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'public' "
    "AND NOT t.tgisinternal "
    "AND t.tgname = 'jobs_idempotency_key_immutable'"
)


@pytest.mark.integration
def test_migrations_create_schema(
    migrate: Callable[[str], None],
    engine: Engine,
) -> None:
    """Run the migration, then assert every DDL object is present."""
    # Ensure the schema is applied (idempotent; safe to re-run).
    migrate("head")

    with engine.connect() as conn:
        # --- tables ---
        tables = {row[0] for row in conn.execute(_TABLES_SQL)}
        missing_tables = EXPECTED_TABLES - tables
        assert not missing_tables, f"missing tables: {sorted(missing_tables)}"

        # --- constraints ---
        actual_constraints = {
            (row[0], row[1]) for row in conn.execute(_CONSTRAINTS_SQL)
        }
        by_table: dict[str, set[tuple[str, str]]] = {}
        for table, cname, contype in _EXPECTED_CONSTRAINTS:
            by_table.setdefault(table, set()).add((cname, contype))
        for table, expected in by_table.items():
            present = expected & actual_constraints
            missing = expected - present
            assert not missing, f"missing constraints on {table}: {sorted(missing)}"

        # --- indexes ---
        actual_indexes = {
            (row[0], row[1]) for row in conn.execute(_INDEXES_SQL)
        }
        missing_indexes = EXPECTED_INDEXES - actual_indexes
        assert not missing_indexes, f"missing indexes: {sorted(missing_indexes)}"

        # --- jobs.idempotency_key immutability trigger (migration 0002) ---
        trigger_defs = [row[0] for row in conn.execute(_TRIGGERS_SQL)]
        assert len(trigger_defs) == 1, (
            f"expected exactly one jobs_idempotency_key_immutable trigger, "
            f"got {len(trigger_defs)}"
        )
        assert trigger_defs[0] == EXPECTED_TRIGGER_DEF, (
            f"trigger drift: live def does not match migration file.\n"
            f"live:      {trigger_defs[0]}\n"
            f"migration: {EXPECTED_TRIGGER_DEF}"
        )
