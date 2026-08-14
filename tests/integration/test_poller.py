"""M3 - poller proof-tests against the real DB (TEST_TAXONOMY.md M3).

Covers:
- test_poller_creates_job_for_low_stock_item
- test_poller_idempotent_on_rerun          (load-bearing idempotency proof)
- test_poller_ignores_items_above_threshold
- test_poller_does_not_mutate_inventory    (byte-identical before/after)
- test_jobs_idempotency_key_immutable_trigger (migration 0002: direct UPDATE
  of idempotency_key is rejected by the DB)

All hit Postgres via the `seeded` + `clean_jobs` fixtures - never mocked.
"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from app.models import InventoryItem, Job, ProcessType
from app.poller import poll_low_stock


def _below_threshold_items(engine: Engine) -> list[InventoryItem]:
    with Session(engine) as session:
        return list(
            session.scalars(
                select(InventoryItem).where(
                    InventoryItem.quantity_on_hand < InventoryItem.reorder_threshold
                )
            ).all()
        )


def _jobs_for(engine: Engine, item_id: int) -> list[Job]:
    with Session(engine) as session:
        return list(
            session.scalars(select(Job).where(Job.inventory_item_id == item_id)).all()
        )


@pytest.mark.integration
def test_poller_creates_job_for_low_stock_item(
    seeded: bool, clean_jobs: None, engine: Engine
) -> None:
    below = _below_threshold_items(engine)
    assert below, "precondition: seed must contain a below-threshold item"

    with Session(engine) as session:
        jobs = poll_low_stock(session)

    assert jobs, "poller created no jobs"
    for item in below:
        job_rows = _jobs_for(engine, item.id)
        assert any(j.status == "pending" for j in job_rows), (
            f"no pending job for below-threshold item id={item.id}"
        )


@pytest.mark.integration
def test_poller_idempotent_on_rerun(
    seeded: bool, clean_jobs: None, engine: Engine
) -> None:
    """Load-bearing: run the poller twice, assert exactly one job per item."""
    below = _below_threshold_items(engine)

    with Session(engine) as session:
        poll_low_stock(session)  # first run
    with Session(engine) as session:
        poll_low_stock(session)  # second run

    for item in below:
        job_rows = _jobs_for(engine, item.id)
        assert len(job_rows) == 1, (
            f"item id={item.id} has {len(job_rows)} job rows, expected exactly 1"
        )


@pytest.mark.integration
def test_poller_ignores_items_above_threshold(
    seeded: bool, clean_jobs: None, engine: Engine
) -> None:
    with Session(engine) as session:
        poll_low_stock(session)

    below_ids = {i.id for i in _below_threshold_items(engine)}
    with Session(engine) as session:
        all_items = list(session.scalars(select(InventoryItem)).all())
    above_threshold = [i for i in all_items if i.id not in below_ids]
    if not above_threshold:
        pytest.skip("no above-threshold item in seed dataset")

    for item in above_threshold:
        assert not _jobs_for(engine, item.id), (
            f"above-threshold item id={item.id} unexpectedly got a job"
        )


@pytest.mark.integration
def test_poller_does_not_mutate_inventory(
    seeded: bool, engine: Engine
) -> None:
    with Session(engine) as session:
        before = session.execute(
            select(
                InventoryItem.id,
                InventoryItem.sku,
                InventoryItem.name,
                InventoryItem.quantity_on_hand,
                InventoryItem.reorder_threshold,
                InventoryItem.reorder_quantity,
                InventoryItem.vendor_id,
                InventoryItem.updated_at,
            ).order_by(InventoryItem.id)
        ).all()

    with Session(engine) as session:
        poll_low_stock(session)

    with Session(engine) as session:
        after = session.execute(
            select(
                InventoryItem.id,
                InventoryItem.sku,
                InventoryItem.name,
                InventoryItem.quantity_on_hand,
                InventoryItem.reorder_threshold,
                InventoryItem.reorder_quantity,
                InventoryItem.vendor_id,
                InventoryItem.updated_at,
            ).order_by(InventoryItem.id)
        ).all()

    assert before == after, "poller mutated inventory_items rows"


@pytest.mark.integration
def test_poller_process_type_resolves_by_name(seeded: bool, engine: Engine) -> None:
    """The poller must be configuration-driven (resolves the row, not a hardcoded id)."""
    with Session(engine) as session:
        pt_id = session.scalar(
            select(ProcessType.id).where(ProcessType.name == "low_stock_reorder")
        )
        jobs = poll_low_stock(session)
        # Capture attributes while the session is still open (commit expires them).
        resolved_ids = [j.process_type_id for j in jobs]

    assert pt_id is not None
    assert jobs and all(rid == pt_id for rid in resolved_ids)


@pytest.mark.integration
def test_jobs_idempotency_key_immutable_trigger(
    seeded: bool, engine: Engine
) -> None:
    """A direct UPDATE of jobs.idempotency_key is rejected by the DB trigger.

    Migration 0002 adds a BEFORE UPDATE ... WHEN (NEW.idempotency_key IS
    DISTINCT FROM OLD.idempotency_key) trigger that raises, so the poller's
    deterministic dedup key can never be tampered with behind its back (the
    duplicate-job attack proven in scripts/injection_check.py). Updates to
    OTHER columns must still work - the WHEN clause only fires on key change.
    """
    with Session(engine) as session:
        pt_id = session.scalar(
            select(ProcessType.id).where(ProcessType.name == "low_stock_reorder")
        )
        job = Job(
            process_type_id=pt_id,
            idempotency_key="trigger-immutability-probe",
            status="pending",
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.id
        original_key = job.idempotency_key

    # --- direct UPDATE of idempotency_key must fail at the DB level --------
    with Session(engine) as session:
        with pytest.raises(ProgrammingError) as excinfo:
            session.execute(
                update(Job)
                .where(Job.id == job_id)
                .values(idempotency_key="tampered-key")
            )
            session.commit()
        session.rollback()
    assert "immutable" in str(excinfo.value).lower(), (
        f"expected the immutability error, got: {excinfo.value}"
    )

    # the row must be unchanged after the rejected update
    with Session(engine) as session:
        row = session.get(Job, job_id)
        assert row is not None
        assert row.idempotency_key == original_key, "key must survive the rejected update"

    # --- updates to OTHER columns must still succeed ------------------------
    with Session(engine) as session:
        session.execute(
            update(Job).where(Job.id == job_id).values(attempts=2)
        )
        session.commit()
        row = session.get(Job, job_id)
        assert row.attempts == 2, "non-key updates must not be blocked"
        assert row.idempotency_key == original_key

    # cleanup so the next run starts from the same state
    with Session(engine) as session:
        session.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": job_id})
        session.commit()
