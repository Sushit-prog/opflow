"""M7 - failure injection proof-tests (TEST_TAXONOMY.md M7).

Prove the system degrades safely under REAL failure conditions:

- test_db_connection_drop_mid_poll      - the poller's backend connection is
  genuinely killed by Postgres (pg_terminate_backend) mid-call; the poll
  fails with a clean exception, leaves no partial rows, and a healthy poll
  recovers immediately.
- test_malformed_job_payload_fails_cleanly - jobs whose payloads break
  execute_job/run_agent fall through the normal backoff path (retrying,
  attempts+1) and the worker round keeps processing the OTHER jobs.
- test_tool_call_raises_mid_execution   - a real tool call through the gate
  raises (VendorNotFound from a real DB lookup); no partial purchase_orders
  row is committed and the job's attempts/status update correctly - the same
  guarantee M4's crash-resume test proved, triggered by a tool failure.

Failures are simulated AT THE BOUNDARY (Postgres killing the connection,
malformed data, a real failing tool input) - business logic is never mocked.
"""
from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.gate import call_tool
from app.models import AuditLog, InventoryItem, Job, PurchaseOrder
from app.poller import poll_low_stock
from app.worker import run_worker_round

MakeJob = Callable[..., Job]


@pytest.mark.integration
def test_db_connection_drop_mid_poll(
    seeded: bool,
    clean_jobs: None,
    engine: Engine,
    database_url: str,
) -> None:
    """A killed DB connection fails the poll cleanly; the next poll recovers.

    The backend connection is REALLY terminated by the server
    (pg_terminate_backend) while the poller session holds it - not mocked.
    The poller must raise a clean SQLAlchemyError, leave zero partial rows,
    and a subsequent poll on a healthy connection must work normally.
    """
    with Session(engine) as session:
        before = session.scalar(select(func.count()).select_from(Job))
    assert before == 0, "clean_jobs must leave an empty jobs table"

    dead_engine = create_engine(database_url)
    session = Session(dead_engine)
    try:
        # grab the backend pid on the session's OWN connection, then kill that
        # exact backend from a SEPARATE, healthy connection (the fixture engine)
        pid = session.execute(text("SELECT pg_backend_pid()")).scalar_one()
        with Session(engine) as killer:
            killed = killer.execute(
                text("SELECT pg_terminate_backend(:pid)"), {"pid": pid}
            ).scalar_one()
        assert killed is True, "pg_terminate_backend must report the kill"

        # the session's connection is now genuinely dead: the poller's first
        # statement must fail with a clean SQLAlchemyError (AdminShutdown)
        with pytest.raises(SQLAlchemyError):
            poll_low_stock(session)
    finally:
        try:
            session.close()
        except SQLAlchemyError:  # pragma: no cover - close on a dead conn
            pass
        dead_engine.dispose()

    # failed poll left no partial state behind
    with Session(engine) as session:
        after = session.scalar(select(func.count()).select_from(Job))
    assert after == before, "a failed poll must not leave partial job rows"

    # a healthy poll on the live engine recovers normally
    with Session(engine) as session:
        jobs = poll_low_stock(session)
    assert len(jobs) >= 1, "seed has below-threshold items; healthy poll must enqueue them"
    with Session(engine) as session:
        assert (
            session.scalar(select(func.count()).select_from(Job)) == len(jobs)
        ), "healthy poll committed exactly the enqueued jobs"


@pytest.mark.integration
def test_malformed_job_payload_fails_cleanly(
    seeded: bool, clean_jobs: None, engine: Engine, make_job: MakeJob
) -> None:
    """Bad payloads fail via the normal backoff path; the round keeps going.

    Two kinds of malformed job: a payload that isn't a dict (AttributeError
    in execute_job) and a job with no inventory_item_id (run_agent refuses to
    evaluate). Both must land in 'retrying' with attempts=1 - never crash the
    worker - while a healthy job in the SAME round still succeeds.
    """
    with Session(engine) as session:
        item = session.scalar(
            select(InventoryItem)
            .where(InventoryItem.quantity_on_hand < InventoryItem.reorder_threshold)
            .limit(1)
        )
    assert item is not None, "precondition: seed must contain a low-stock item"

    bad_type = make_job(
        idempotency_key="m7-bad-payload-1",
        inventory_item_id=item.id,
        payload="garbage-not-a-dict",
    )
    bad_no_item = make_job(idempotency_key="m7-bad-none-1")
    good = make_job(
        idempotency_key="m7-good-1",
        inventory_item_id=item.id,
        payload={"_action": "insert_po"},
    )

    with Session(engine) as session:
        processed = run_worker_round(session)

    # the round survived and touched every job, including the good one
    assert set(processed) == {bad_type.id, bad_no_item.id, good.id}

    with Session(engine) as session:
        t = session.get(Job, bad_type.id)
        n = session.get(Job, bad_no_item.id)
        g = session.get(Job, good.id)
        assert t.status == "retrying" and t.attempts == 1, (
            f"non-dict payload must retry, got {t.status}/attempts={t.attempts}"
        )
        assert n.status == "retrying" and n.attempts == 1, (
            f"missing inventory_item_id must retry, got {n.status}/attempts={n.attempts}"
        )
        assert g.status == "succeeded", f"healthy job must succeed, got {g.status}"


@pytest.mark.integration
def test_tool_call_raises_mid_execution(
    seeded: bool, clean_jobs: None, engine: Engine, make_job: MakeJob
) -> None:
    """A tool failure mid-job leaves no partial PO and updates attempts/status.

    The worker's execute_fn is the documented injection seam; here it drives
    the REAL gate with an input that fails a REAL DB lookup (vendor 999999
    does not exist) -> VendorNotFound. The same guarantee M4's crash-resume
    test proved for a process crash, now triggered by a tool failure: no
    partial purchase_orders row, job moves to retrying with attempts=1, and
    the gate audited the failed call.
    """
    with Session(engine) as session:
        item = session.scalar(select(InventoryItem).order_by(InventoryItem.id).limit(1))
    assert item is not None, "precondition: seed data"

    job = make_job(idempotency_key="m7-tool-fail-1", inventory_item_id=item.id)

    def failing_execute(session: Session, job: Job) -> None:
        call_tool(
            session,
            job_id=job.id,
            process_type_id=job.process_type_id,
            tool_name="create_purchase_order",
            tool_input={
                "vendor_id": 999_999,  # does not exist -> real VendorNotFound
                "inventory_item_id": item.id,
                "quantity": 5,
                "created_by_job_id": job.id,
            },
            decision_reasoning="test: force a real tool failure mid-job",
        )

    with Session(engine) as session:
        processed = run_worker_round(session, execute_fn=failing_execute)

    assert job.id in processed, "the failed job must still be reported as processed"

    with Session(engine) as session:
        row = session.get(Job, job.id)
        assert row.status == "retrying", f"expected retrying, got {row.status}"
        assert row.attempts == 1, f"expected attempts=1, got {row.attempts}"
        assert "vendor not found: vendor_id=999999" in (row.error or ""), (
            f"error must name the tool failure: {row.error!r}"
        )

        pos = session.scalars(
            select(PurchaseOrder).where(PurchaseOrder.created_by_job_id == job.id)
        ).all()
        assert pos == [], "no partial purchase_orders row may be committed"

        audit = session.scalars(
            select(AuditLog).where(AuditLog.job_id == job.id)
        ).all()
        assert len(audit) == 1, "the failed tool call must be audited exactly once"
        assert audit[0].tool_output is not None
        assert audit[0].tool_output["error"] == "VendorNotFound"
