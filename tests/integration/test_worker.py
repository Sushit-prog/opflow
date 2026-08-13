"""M4 - worker loop proof-tests against the real DB (TEST_TAXONOMY.md M4).

Covers:
- test_claim_job_marks_running
- test_claim_job_race_only_one_worker_wins   (two real threads, Barrier-synced)
- test_failure_increments_attempts_and_backs_off
- test_max_attempts_moves_to_failed
- test_crash_resume_no_duplicate_action      (load-bearing crash-resume proof)

All hit Postgres via the `seeded` + `clean_jobs` + `make_job` fixtures -
never mocked. The race test uses real threads against the same row, and the
crash-resume test really interrupts a job mid-flight (no complete_job call),
exactly as the taxonomy requires.
"""
from __future__ import annotations

import threading
from collections.abc import Callable

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.jobs import backoff, claim_job, complete_job
from app.models import InventoryItem, Job, PurchaseOrder
from app.worker import execute_job, recover_stale_running, run_worker_round

MakeJob = Callable[..., Job]


@pytest.mark.integration
def test_claim_job_marks_running(
    seeded: bool, clean_jobs: None, engine: Engine, make_job: MakeJob
) -> None:
    job = make_job(idempotency_key="m4-claim-1")

    with Session(engine) as session:
        claimed = claim_job(session, job.id)

    assert claimed is True
    with Session(engine) as session:
        row = session.get(Job, job.id)
        assert row is not None
        assert row.status == "running"


@pytest.mark.integration
def test_claim_job_race_only_one_worker_wins(
    seeded: bool, clean_jobs: None, engine: Engine, make_job: MakeJob
) -> None:
    """Two real threads, own sessions, Barrier-synced, same job id.

    Exactly one claim_job call must return True. This is not sequential or
    simulated - both UPDATEs genuinely race, and Postgres row locking makes
    the loser re-check the WHERE clause and match 0 rows.
    """
    job = make_job(idempotency_key="m4-race-1")
    barrier = threading.Barrier(2)
    results: list[bool] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            with Session(engine) as session:
                barrier.wait(timeout=10)
                ok = claim_job(session, job.id)
            with lock:
                results.append(ok)
        except BaseException as exc:  # pragma: no cover - surfaces thread failures
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"worker thread raised: {errors}"
    assert results.count(True) == 1, (
        f"expected exactly one claim to win, got {results.count(True)} of {results}"
    )
    assert results.count(False) == 1


@pytest.mark.integration
def test_failure_increments_attempts_and_backs_off(
    seeded: bool, clean_jobs: None, engine: Engine, make_job: MakeJob
) -> None:
    job = make_job(idempotency_key="m4-backoff-1")

    with Session(engine) as session:
        assert claim_job(session, job.id)
        complete_job(session, job.id, success=False, error="boom")

    with Session(engine) as session:
        row = session.get(Job, job.id)
        assert row is not None
        assert row.status == "retrying"
        assert row.attempts == 1
        assert row.error == "boom"
        # Exact formula: next_run_at = updated_at + backoff(1) = +10s (same
        # transaction, so now() is identical for both columns).
        delta = (row.next_run_at - row.updated_at).total_seconds()
        assert abs(delta - backoff(1)) < 0.5, f"expected +{backoff(1)}s, got {delta}s"


@pytest.mark.integration
def test_max_attempts_moves_to_failed(
    seeded: bool, clean_jobs: None, engine: Engine, make_job: MakeJob
) -> None:
    # low_stock_reorder has max_attempts=3; start at 2 so this failure is the
    # 3rd attempt and must flip the job to 'failed'.
    job = make_job(idempotency_key="m4-max-1", attempts=2)

    with Session(engine) as session:
        assert claim_job(session, job.id)
        complete_job(session, job.id, success=False, error="boom")

    with Session(engine) as session:
        row = session.get(Job, job.id)
        assert row is not None
        assert row.status == "failed"
        assert row.attempts == 3
        assert row.error == "boom"


@pytest.mark.integration
def test_crash_resume_no_duplicate_action(
    seeded: bool, clean_jobs: None, engine: Engine, make_job: MakeJob
) -> None:
    """Load-bearing: a job killed mid-execute must resume without duplicating.

    Simulated crash is a REAL interruption: claim the job, execute it (the
    idempotent PO side effect commits), then never call complete_job - the job
    is left genuinely stuck in 'running'. A fresh 'restart' then recovers it
    and re-runs the round; the PO must not be inserted twice.
    """
    with Session(engine) as session:
        item = session.scalar(
            select(InventoryItem)
            .where(InventoryItem.quantity_on_hand < InventoryItem.reorder_threshold)
            .limit(1)
        )
    assert item is not None, "precondition: seed must contain a low-stock item"

    job = make_job(
        idempotency_key="m4-crash-1",
        inventory_item_id=item.id,
        payload={"_action": "insert_po"},
    )

    # --- "worker 1" claims and executes, then crashes before complete_job ---
    with Session(engine) as session:
        assert claim_job(session, job.id)
        execute_job(session, session.get(Job, job.id))
        # crash: session closed WITHOUT complete_job - job stays 'running'
    with Session(engine) as session:
        assert session.get(Job, job.id).status == "running"
        pos = session.scalars(
            select(PurchaseOrder).where(PurchaseOrder.created_by_job_id == job.id)
        ).all()
        assert len(pos) == 1, "crash committed exactly one PO side effect"

    # --- fresh "restart": recover stale, then a normal worker round ---
    with Session(engine) as session:
        recovered = recover_stale_running(session, stale_after_seconds=0)
        assert recovered == 1, "exactly the crashed job must be recovered"
        processed = run_worker_round(session)
        assert job.id in processed, "restart must re-process the recovered job"

    with Session(engine) as session:
        row = session.get(Job, job.id)
        assert row is not None
        assert row.status == "succeeded"
        pos = session.scalars(
            select(PurchaseOrder).where(PurchaseOrder.created_by_job_id == job.id)
        ).all()
        assert len(pos) == 1, "resumed job must NOT duplicate the PO side effect"
