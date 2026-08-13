"""Job lifecycle operations (INTERFACES.md §2).

Implemented so far:
    enqueue_job(...)     - used by the poller (M3); idempotent insert by key

Deferred to M4 (worker loop):
    claim_job(...) / complete_job(...)

Sessions are always injected explicitly - no hidden global state.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import Job


def enqueue_job(
    session: Session,
    process_type_id: int,
    idempotency_key: str,
    inventory_item_id: int | None,
    payload: dict[str, Any] | None,
) -> Job:
    """Insert a job or, on idempotency-key conflict, return the existing row.

    Mirrors INTERFACES.md §2:

        INSERT INTO jobs (...) VALUES (...) ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING *   -- if no row returned (conflict), SELECT the existing row

    The unique constraint on jobs.idempotency_key is the DB-level guard that
    makes this safe under concurrent pollers: exactly one row can ever exist
    per key.
    """
    stmt = (
        pg_insert(Job)
        .values(
            process_type_id=process_type_id,
            idempotency_key=idempotency_key,
            inventory_item_id=inventory_item_id,
            status="pending",
            payload=payload,
        )
        .on_conflict_do_nothing(index_elements=[Job.__table__.c.idempotency_key])
        .returning(Job)
    )

    row = session.execute(stmt).first()
    if row is not None:
        return row[0]

    # Conflict: the job already exists for this key - return the existing row.
    job = session.scalar(
        select(Job).where(Job.idempotency_key == idempotency_key)
    )
    if job is None:  # pragma: no cover - defensive; unique constraint guarantees it
        raise RuntimeError(
            f"idempotency conflict but no row found for key {idempotency_key!r}"
        )
    return job
