"""M5 - capability-gated tools proof-tests against the real DB (INTERFACES.md §3).

Covers:
- test_whitelisted_tool_call_succeeds
- test_non_whitelisted_tool_call_is_blocked   (highest-signal: rejection row
  must land in audit_log)
- test_every_tool_call_writes_audit_log_row   (allowed AND rejected: one row each)
- test_create_purchase_order_idempotent_on_same_job_id

All hit Postgres via the `seeded` + `clean_jobs` + `make_job` fixtures -
never mocked. Every tool call goes through app.gate.call_tool, the only
execution path; no tool function is invoked directly.
"""
from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.errors import InvalidJobReference, ToolNotWhitelisted
from app.gate import call_tool
from app.models import AuditLog, InventoryItem, Job, PurchaseOrder, Vendor

MakeJob = Callable[..., Job]


def _audit_count(engine: Engine, job_id: int) -> int:
    with Session(engine) as session:
        return session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.job_id == job_id)
        )


@pytest.mark.integration
def test_whitelisted_tool_call_succeeds(
    seeded: bool, clean_jobs: None, engine: Engine, make_job: MakeJob
) -> None:
    """query_inventory (whitelisted) returns the item dict through the gate."""
    with Session(engine) as session:
        item = session.scalar(select(InventoryItem).order_by(InventoryItem.id).limit(1))
    assert item is not None, "precondition: seed must contain an inventory item"

    job = make_job(idempotency_key="m5-whitelisted-1")
    with Session(engine) as session:
        result = call_tool(
            session,
            job_id=job.id,
            process_type_id=job.process_type_id,
            tool_name="query_inventory",
            tool_input={"sku": item.sku},
            decision_reasoning="test: whitelisted read",
        )

    assert result["id"] == item.id
    assert result["sku"] == item.sku
    assert result["quantity_on_hand"] == item.quantity_on_hand
    assert result["vendor_id"] == item.vendor_id


@pytest.mark.integration
def test_non_whitelisted_tool_call_is_blocked(
    seeded: bool, clean_jobs: None, engine: Engine, make_job: MakeJob
) -> None:
    """Highest-signal test: a non-whitelisted call raises AND audits a rejection.

    low_stock_reorder's allowed_tools is the seeded
    ["query_inventory","create_purchase_order","notify_vendor"], so
    "delete_everything" must be rejected - and the rejection must land in
    audit_log with the structured ToolNotWhitelisted payload.
    """
    job = make_job(idempotency_key="m5-blocked-1")

    with Session(engine) as session:
        with pytest.raises(ToolNotWhitelisted) as excinfo:
            call_tool(
                session,
                job_id=job.id,
                process_type_id=job.process_type_id,
                tool_name="delete_everything",
                tool_input={},
                decision_reasoning="test: this must be blocked",
            )

    assert excinfo.value.tool_name == "delete_everything"
    assert excinfo.value.process_type_id == job.process_type_id

    assert _audit_count(engine, job.id) == 1, "rejection must write exactly one row"
    with Session(engine) as session:
        row = session.scalar(
            select(AuditLog).where(AuditLog.job_id == job.id)
        )
    assert row is not None
    assert row.tool_called == "delete_everything"
    assert row.tool_output is not None
    assert row.tool_output["error"] == "ToolNotWhitelisted"
    assert row.decision_reasoning == "test: this must be blocked"


@pytest.mark.integration
def test_every_tool_call_writes_audit_log_row(
    seeded: bool, clean_jobs: None, engine: Engine, make_job: MakeJob
) -> None:
    """Allowed and rejected calls each produce exactly one audit_log row."""
    with Session(engine) as session:
        item = session.scalar(select(InventoryItem).order_by(InventoryItem.id).limit(1))
    assert item is not None, "precondition: seed must contain an inventory item"

    job = make_job(idempotency_key="m5-audit-1")

    # allowed call -> exactly one row
    with Session(engine) as session:
        call_tool(
            session,
            job_id=job.id,
            process_type_id=job.process_type_id,
            tool_name="query_inventory",
            tool_input={"inventory_item_id": item.id},
            decision_reasoning="test: allowed",
        )
    assert _audit_count(engine, job.id) == 1

    # rejected call -> exactly one more row (total 2)
    with Session(engine) as session:
        with pytest.raises(ToolNotWhitelisted):
            call_tool(
                session,
                job_id=job.id,
                process_type_id=job.process_type_id,
                tool_name="drop_database",
                tool_input={},
                decision_reasoning="test: rejected",
            )
    assert _audit_count(engine, job.id) == 2

    with Session(engine) as session:
        rows = session.scalars(
            select(AuditLog).where(AuditLog.job_id == job.id).order_by(AuditLog.id)
        ).all()
    assert [r.tool_called for r in rows] == ["query_inventory", "drop_database"]
    assert rows[0].tool_output is not None and "error" not in rows[0].tool_output
    assert rows[1].tool_output is not None and rows[1].tool_output["error"] == "ToolNotWhitelisted"


@pytest.mark.integration
def test_create_purchase_order_idempotent_on_same_job_id(
    seeded: bool, clean_jobs: None, engine: Engine, make_job: MakeJob
) -> None:
    """Two create_purchase_order calls with the same created_by_job_id -> one row."""
    with Session(engine) as session:
        item = session.scalar(select(InventoryItem).order_by(InventoryItem.id).limit(1))
        vendor = session.get(Vendor, item.vendor_id)
    assert item is not None and vendor is not None, "precondition: seed data"

    job = make_job(
        idempotency_key="m5-idempotent-1",
        inventory_item_id=item.id,
    )

    kwargs = dict(
        job_id=job.id,
        process_type_id=job.process_type_id,
        tool_name="create_purchase_order",
        decision_reasoning="test: idempotent PO",
    )
    with Session(engine) as session:
        first = call_tool(
            session,
            tool_input={
                "vendor_id": vendor.id,
                "inventory_item_id": item.id,
                "quantity": item.reorder_quantity,
                "created_by_job_id": job.id,
            },
            **kwargs,
        )
    with Session(engine) as session:
        second = call_tool(
            session,
            tool_input={
                "vendor_id": vendor.id,
                "inventory_item_id": item.id,
                "quantity": item.reorder_quantity,
                "created_by_job_id": job.id,
            },
            **kwargs,
        )

    assert first["status"] == "draft"
    assert second["id"] == first["id"], "second call must return the existing PO"

    with Session(engine) as session:
        pos = session.scalars(
            select(PurchaseOrder).where(PurchaseOrder.created_by_job_id == job.id)
        ).all()
    assert len(pos) == 1, "idempotent: exactly one PO row for this job"
    assert pos[0].quantity == item.reorder_quantity

    # each call audited exactly once
    assert _audit_count(engine, job.id) == 2


@pytest.mark.integration
def test_create_purchase_order_rejects_malformed_job_id(
    seeded: bool, clean_jobs: None, engine: Engine, make_job: MakeJob
) -> None:
    """A malformed created_by_job_id raises InvalidJobReference, not a raw
    psycopg ProgrammingError, and the rejection is audited as structured JSON.

    Regression for the adversarial case found in injection_check.py:
    create_purchase_order(created_by_job_id="1; DROP TABLE jobs; --") used to
    let sqlalchemy.exc.ProgrammingError (psycopg.errors.UndefinedFunction)
    escape. It must surface as the clean domain error instead - and, because
    InvalidJobReference is an OpFlowError, the gate audits it with the
    structured to_dict() payload rather than a generic InternalError.
    """
    with Session(engine) as session:
        item = session.scalar(select(InventoryItem).order_by(InventoryItem.id).limit(1))
        vendor = session.get(Vendor, item.vendor_id)
    assert item is not None and vendor is not None, "precondition: seed data"

    job = make_job(idempotency_key="m5-malformed-job-id-1")

    malformed = "1; DROP TABLE jobs; --"
    with Session(engine) as session:
        with pytest.raises(InvalidJobReference) as excinfo:
            call_tool(
                session,
                job_id=job.id,
                process_type_id=job.process_type_id,
                tool_name="create_purchase_order",
                tool_input={
                    "vendor_id": vendor.id,
                    "inventory_item_id": item.id,
                    "quantity": 5,
                    "created_by_job_id": malformed,
                },
                decision_reasoning="test: malformed job id must fail cleanly",
            )

    assert excinfo.value.created_by_job_id == malformed
    assert excinfo.value.to_dict() == {
        "error": "InvalidJobReference",
        "message": excinfo.value.message,
        "created_by_job_id": malformed,
    }

    # the failed call is audited exactly once, with the structured payload
    assert _audit_count(engine, job.id) == 1
    with Session(engine) as session:
        row = session.scalar(select(AuditLog).where(AuditLog.job_id == job.id))
    assert row is not None
    assert row.tool_called == "create_purchase_order"
    assert row.tool_output is not None
    assert row.tool_output["error"] == "InvalidJobReference"
    assert row.tool_output["created_by_job_id"] == malformed

    # no purchase_orders row may be created by the failed call
    with Session(engine) as session:
        pos = session.scalars(
            select(PurchaseOrder).where(PurchaseOrder.created_by_job_id == job.id)
        ).all()
    assert pos == [], "malformed job id must not create a purchase order"
