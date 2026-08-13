"""Job lifecycle operations (INTERFACES.md §2).

Implemented:
    enqueue_job(...)     - idempotent insert by key (M3, poller)
    claim_job(...)       - atomic running-claim, the concurrency guard (M4)
    complete_job(...)    - success / retry-with-backoff / failed (M4)
    backoff(...)         - exact retry delay formula (M4)

Sessions are always injected explicitly - no hidden global state.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import Job, ProcessType


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


def backoff(attempts: int) -> int:
    """Exact retry delay in seconds (INTERFACES.md §2): base 5s, cap 5min."""
    return min(5 * 2**attempts, 300)


def claim_job(session: Session, job_id: int) -> bool:
    """Atomically claim a job by flipping it to 'running'. Any job in
    'pending'/'retrying' past its next_run_at is claimable.

    Implementation note: `now()` is evaluated server-side, then the same
    value is written to updated_at, so the timestamp is the DB's clock - not
    the worker's.

    Mirrors INTERFACES.md §2:
        UPDATE jobs SET status='running', updated_at=now()
        WHERE id=%s AND status IN ('pending','retrying') AND next_run_at <= now()
        -- returns True iff rowcount == 1. This single UPDATE is the concurrency guard
    """
    now_sql = func.now()
    result = session.execute(
        update(Job)
        .where(Job.id == job_id)
        .where(Job.status.in_(("pending", "retrying")))
        .where(Job.next_run_at <= now_sql)
        .values(status="running", updated_at=now_sql)
    )
    claimed = result.rowcount == 1
    session.commit()
    return claimed


def complete_job(
    session: Session,
    job_id: int,
    success: bool,
    error: str | None = None,
) -> None:
    """Resolve a job after execution (INTERFACES.md §2).

    On success: status='succeeded'. On failure: attempts += 1; if attempts >=
    the process_type's max_attempts the job becomes 'failed', otherwise
    'retrying' with next_run_at = now() + backoff(attempts).
    """
    now_sql = func.now()

    if success:
        session.execute(
            update(Job).where(Job.id == job_id).values(
                status="succeeded",
                updated_at=now_sql,
            )
        )
        session.commit()
        return

    job = session.get(Job, job_id)
    if job is None:  # pragma: no cover - FK/dataset invariant
        session.rollback()
        raise RuntimeError(f"complete_job: job {job_id} not found")

    pt = session.get(ProcessType, job.process_type_id)
    max_attempts = pt.max_attempts if pt is not None else 3

    attempts_after = job.attempts + 1
    if attempts_after >= max_attempts:
        new_status = "failed"
        next_run_at = job.next_run_at  # terminal state: keep existing schedule
    else:
        new_status = "retrying"
        next_run_at = func.now() + timedelta(seconds=backoff(attempts_after))

    session.execute(
        update(Job).where(Job.id == job_id).values(
            attempts=attempts_after,
            status=new_status,
            next_run_at=next_run_at,
            error=error,
            updated_at=now_sql,
        )
    )
    session.commit()
