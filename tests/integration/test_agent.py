"""M6 - capability-gated agent proof-tests (INTERFACES.md §5).

Covers:
- test_agent_produces_structured_decision
- test_agent_reasoning_persisted_to_audit_log
- test_agent_never_writes_db_directly         (static source check)
- test_agent_uses_fresh_inventory_snapshot_not_stale_payload
- test_agent_decision_to_skip_does_not_call_tools

The LLM call itself is MOCKED (a fake llm_fn injected into run_agent) - no
live API calls in the test suite. Everything downstream of the agent's
decision - the fresh query_inventory read, the PO insert, notify_vendor, and
every audit_log row - hits the REAL Postgres.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.agent import AgentResult, run_agent
from app.models import AuditLog, InventoryItem, Job, PurchaseOrder

MakeJob = Callable[..., Job]
FakeLLM = Callable[[dict[str, Any]], dict[str, Any]]

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _audit_tools(engine: Engine, job_id: int) -> list[AuditLog]:
    with Session(engine) as session:
        return list(
            session.scalars(
                select(AuditLog)
                .where(AuditLog.job_id == job_id)
                .order_by(AuditLog.id)
            )
        )


@pytest.mark.integration
def test_agent_produces_structured_decision(
    seeded: bool, clean_jobs: None, engine: Engine, make_job: MakeJob
) -> None:
    """Agent output is the structured AgentResult shape, not free text."""
    with Session(engine) as session:
        item = session.scalar(select(InventoryItem).order_by(InventoryItem.id).limit(1))
    assert item is not None, "precondition: seed must contain an inventory item"

    job = make_job(
        idempotency_key="m6-structured-1",
        inventory_item_id=item.id,
        payload={"quantity_on_hand": item.quantity_on_hand, "reorder_threshold": item.reorder_threshold},
    )

    fake_llm: FakeLLM = lambda context: {  # noqa: E731 - test-only fake
        "action": "create_po_and_notify",
        "reasoning": "stock is below the reorder threshold",
    }

    with Session(engine) as session:
        result = run_agent(session, session.get(Job, job.id), llm_fn=fake_llm)

    assert isinstance(result, AgentResult)
    assert result.action in ("create_po_and_notify", "skip")
    assert result.reasoning == "stock is below the reorder threshold"

    # the fresh read itself was audited through the gate
    audit = _audit_tools(engine, job.id)
    assert [row.tool_called for row in audit] == [
        "query_inventory",
        "create_purchase_order",
        "notify_vendor",
    ]


@pytest.mark.integration
def test_agent_reasoning_persisted_to_audit_log(
    seeded: bool, clean_jobs: None, engine: Engine, make_job: MakeJob
) -> None:
    """The agent's reasoning lands in audit_log.decision_reasoning."""
    with Session(engine) as session:
        item = session.scalar(select(InventoryItem).order_by(InventoryItem.id).limit(1))
    assert item is not None, "precondition: seed data"

    job = make_job(
        idempotency_key="m6-reasoning-1",
        inventory_item_id=item.id,
        payload={"quantity_on_hand": 0, "reorder_threshold": 10},
    )

    fake_llm: FakeLLM = lambda context: {  # noqa: E731
        "action": "create_po_and_notify",
        "reasoning": "R-42: below threshold, reorder now",
    }

    with Session(engine) as session:
        run_agent(session, session.get(Job, job.id), llm_fn=fake_llm)

    audit = _audit_tools(engine, job.id)
    action_rows = [
        row for row in audit
        if row.tool_called in ("create_purchase_order", "notify_vendor")
    ]
    assert len(action_rows) == 2
    for row in action_rows:
        assert row.decision_reasoning == "R-42: below threshold, reorder now"


@pytest.mark.integration
def test_agent_never_writes_db_directly() -> None:
    """Static check: the agent module has no SQLAlchemy / raw SQL access."""
    source = (PROJECT_ROOT / "app" / "agent.py").read_text(encoding="utf-8")

    forbidden = [
        "from sqlalchemy",
        "import sqlalchemy",
        "session.execute",
        "session.add(",
        "session.commit(",
        "session.flush",
        "Session(",
        "text(",
    ]
    hits = [pattern for pattern in forbidden if pattern in source]
    assert not hits, (
        "app/agent.py must never touch the database directly - "
        f"found forbidden patterns: {hits}"
    )


@pytest.mark.integration
def test_agent_uses_fresh_inventory_snapshot_not_stale_payload(
    seeded: bool, clean_jobs: None, engine: Engine, make_job: MakeJob
) -> None:
    """The agent must act on the live DB state, not the payload's old one.

    The job's payload snapshot claims the item is below threshold (0 on hand).
    After enqueueing, the DB is mutated so the item is well above threshold.
    A fake LLM that decides purely from the FRESH snapshot must choose skip -
    proving run_agent feeds it the fresh query_inventory result, not payload.
    """
    with Session(engine) as session:
        item = session.scalar(select(InventoryItem).order_by(InventoryItem.id).limit(1))
    assert item is not None, "precondition: seed data"
    assert item.quantity_on_hand < item.reorder_threshold, (
        "precondition: seed item is below threshold so the test is meaningful"
    )

    job = make_job(
        idempotency_key="m6-fresh-1",
        inventory_item_id=item.id,
        # stale payload snapshot from enqueue time: item looks out of stock
        payload={
            "quantity_on_hand": 0,
            "reorder_threshold": item.reorder_threshold,
        },
    )

    # mutate the DB AFTER enqueueing: item is now well stocked
    with Session(engine) as session:
        live = session.get(InventoryItem, item.id)
        live.quantity_on_hand = item.reorder_threshold + 100
        session.commit()

    def fake_llm(context: dict[str, Any]) -> dict[str, Any]:
        snapshot = context["inventory_snapshot"]
        if snapshot["quantity_on_hand"] >= snapshot["reorder_threshold"]:
            return {"action": "skip", "reasoning": "fresh snapshot shows enough stock"}
        return {"action": "create_po_and_notify", "reasoning": "fresh snapshot is low"}

    with Session(engine) as session:
        result = run_agent(session, session.get(Job, job.id), llm_fn=fake_llm)

    assert result.action == "skip", (
        "agent must decide from the FRESH snapshot (in stock), "
        f"not the stale payload (out of stock): {result}"
    )
    assert result.reasoning == "fresh snapshot shows enough stock"


@pytest.mark.integration
def test_agent_decision_to_skip_does_not_call_tools(
    seeded: bool, clean_jobs: None, engine: Engine, make_job: MakeJob
) -> None:
    """A skip decision must produce no create_purchase_order / notify_vendor."""
    with Session(engine) as session:
        item = session.scalar(select(InventoryItem).order_by(InventoryItem.id).limit(1))
    assert item is not None, "precondition: seed data"

    job = make_job(
        idempotency_key="m6-skip-1",
        inventory_item_id=item.id,
        payload={"quantity_on_hand": item.quantity_on_hand, "reorder_threshold": item.reorder_threshold},
    )

    fake_llm: FakeLLM = lambda context: {  # noqa: E731
        "action": "skip",
        "reasoning": "no action needed",
    }

    with Session(engine) as session:
        result = run_agent(session, session.get(Job, job.id), llm_fn=fake_llm)

    assert result.action == "skip"

    with Session(engine) as session:
        pos = session.scalars(
            select(PurchaseOrder).where(PurchaseOrder.created_by_job_id == job.id)
        ).all()
        assert pos == [], "skip must not create a purchase order"

    audit = _audit_tools(engine, job.id)
    assert [row.tool_called for row in audit] == ["query_inventory"], (
        "skip must not call create_purchase_order / notify_vendor"
    )
