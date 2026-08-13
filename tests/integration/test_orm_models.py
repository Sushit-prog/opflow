"""M1 drift proof: reflect the LIVE Postgres and assert the ORM models match.

This is not "the app boots and models exist" - it reflects the real tables
(columns, types, nullability, defaults, PK, FK, UNIQUE, CHECK, indexes) from
the database that `alembic upgrade head` produced and compares each against
the SQLAlchemy models in app.models. Any mismatch fails the test.

Comparison choices (see plan):
- CHECK constraints are compared by *name* because Postgres rewrites IN (...)/
  comparisons at DDL time into a form not textually identical to the ORM text.
- UNIQUE constraints are compared as column-sets (names differ by naming rule).
- Indexes: the explicit ORM-declared indexes must each exist in the live DB
  with matching columns + uniqueness (unique-constraint-backed indexes are
  covered by the UNIQUE comparison, not the index comparison).
"""
from __future__ import annotations

import re

import pytest
from sqlalchemy import CheckConstraint, UniqueConstraint, inspect
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Engine

from app.models import Base

_DIALECT = postgresql.dialect()

# The 6 tables this project owns (alembic_version is excluded).
EXPECTED_TABLES: set[str] = {
    "vendors",
    "inventory_items",
    "process_types",
    "jobs",
    "purchase_orders",
    "audit_log",
}

_CAST_RE = re.compile(r"::\"?[a-z_]+\"?", re.IGNORECASE)


def _normalize_default(raw: object) -> object:
    """Canonicalize a server default for comparison.

    - None -> None
    - rendercasts like 'pending'::text -> 'pending' (strip ::... casts)
    - unquotes single-quoted literals ('pending' -> pending)
    - keeps now()/true/false/numbers as-is
    """
    if raw is None:
        return None
    text_value = raw if isinstance(raw, str) else str(raw)
    text_value = _CAST_RE.sub("", text_value).strip()
    if len(text_value) >= 2 and text_value[0] == "'" and text_value[-1] == "'":
        text_value = text_value[1:-1]
    return text_value


def _orm_default(col: object) -> object:
    sd = col.server_default
    if sd is None:
        return None
    arg = sd.arg
    if hasattr(arg, "text"):  # TextClause keeps its raw text
        return arg.text
    return str(arg)


def _type_key(col: object) -> str:
    """Compile a column's type against the Postgres dialect.

    Accepts either an ORM ``Column`` or a reflected dict from
    ``inspector.get_columns()`` (keyed 'type').
    """
    type_obj = col.type if hasattr(col, "type") else col["type"]
    return type_obj.compile(dialect=_DIALECT)


@pytest.mark.integration
def test_orm_models_match_live_db(engine: Engine) -> None:
    orm_tables = Base.metadata.tables
    assert set(orm_tables) == EXPECTED_TABLES, (
        f"ORM metadata must contain exactly the 6 spec tables; got "
        f"{sorted(set(orm_tables) ^ EXPECTED_TABLES)}"
    )

    inspector = inspect(engine)

    for table_name in sorted(EXPECTED_TABLES):
        orm_table = orm_tables[table_name]
        refl_columns = {c["name"]: c for c in inspector.get_columns(table_name)}

        # --- column names ---
        assert set(orm_table.columns.keys()) == set(refl_columns), (
            f"{table_name}: column mismatch. ORM-only="
            f"{sorted(set(orm_table.columns.keys()) - set(refl_columns))} "
            f"DB-only={sorted(set(refl_columns) - set(orm_table.columns.keys()))}"
        )

        # --- per-column: type, nullability, server default ---
        for col_name, orm_col in orm_table.columns.items():
            refl_col = refl_columns[col_name]
            assert _type_key(orm_col) == _type_key(refl_col), (
                f"{table_name}.{col_name}: type {_type_key(orm_col)!r} != "
                f"{_type_key(refl_col)!r}"
            )
            assert orm_col.nullable == refl_col["nullable"], (
                f"{table_name}.{col_name}: nullable mismatch "
                f"(orm={orm_col.nullable}, db={refl_col['nullable']})"
            )
            # serial PK: DB reflects a nextval() default; ORM uses autoincrement
            if orm_col.primary_key and "nextval" in str(refl_col["default"] or ""):
                continue
            assert _normalize_default(_orm_default(orm_col)) == _normalize_default(
                refl_col["default"]
            ), f"{table_name}.{col_name}: server_default mismatch"

        # --- primary key ---
        refl_pk = set(inspector.get_pk_constraint(table_name)["constrained_columns"])
        orm_pk = {name for name, c in orm_table.columns.items() if c.primary_key}
        assert refl_pk == orm_pk, f"{table_name}: PK mismatch ({refl_pk} != {orm_pk})"

        # --- foreign keys ---
        orm_fks = {
            (fk.parent.name, fk.column.table.name, fk.column.name)
            for fk in orm_table.foreign_keys
        }
        refl_fks = {
            (row["constrained_columns"][0], row["referred_table"], row["referred_columns"][0])
            for row in inspector.get_foreign_keys(table_name)
        }
        assert orm_fks == refl_fks, f"{table_name}: FK mismatch ({orm_fks} != {refl_fks})"

        # --- unique constraints (as column-sets) ---
        orm_uniq = {
            frozenset(uc.columns.keys())
            for uc in orm_table.constraints
            if isinstance(uc, UniqueConstraint)
        }
        refl_uniq = {
            frozenset(uc["column_names"])
            for uc in inspector.get_unique_constraints(table_name)
        }
        assert orm_uniq == refl_uniq, f"{table_name}: UNIQUE mismatch ({orm_uniq} != {refl_uniq})"

        # --- check constraints (by name) ---
        orm_checks = {
            cc.name for cc in orm_table.constraints if isinstance(cc, CheckConstraint)
        }
        refl_checks = {cc["name"] for cc in inspector.get_check_constraints(table_name)}
        assert orm_checks == refl_checks, (
            f"{table_name}: CHECK mismatch ({sorted(orm_checks)} != {sorted(refl_checks)})"
        )

        # --- indexes: ORM-declared indexes must exist in DB with matching shape ---
        refl_indexes = {
            (ix["name"], tuple(ix["column_names"]), bool(ix["unique"]))
            for ix in inspector.get_indexes(table_name)
        }
        for ix in orm_table.indexes:
            key = (ix.name, tuple(c.name for c in ix.columns), bool(ix.unique))
            assert key in refl_indexes, (
                f"{table_name}: index {ix.name} not present as {key}; "
                f"DB has {sorted(refl_indexes)}"
            )