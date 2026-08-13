"""Worker loop (opflow-spec.md M4, INTERFACES.md §2).

run_worker_round is the full M4 loop: recover stale 'running' jobs (crash
resume), poll due 'pending'/'retrying' jobs, claim each with the atomic
UPDATE guard, run the executor, then complete the job (success / retry /
failed).

execute_job is a CONTROLLABLE STUB for M4/M5 plus the real M6 agent path:

    - default (no controls): the M6 capability-gated agent - run_agent calls
      DeepSeek V4 via OpenRouter and acts ONLY through app.gate.call_tool
      (INTERFACES.md §5).
    - payload {"_fail": truthy}          -> raise RuntimeError("stub failure")
    - payload {"_action": "insert_po"}  -> REAL create_purchase_order call
      through the Capability Gate, keyed on created_by_job_id == job.id, so
      a re-run is an idempotent no-op. The gate also writes the audit_log
      row for the call. Kept for M4's crash-resume proof and other tests.

Nothing here talks to external services except the agent's LLM call
(notify_vendor stays log-only).

Sessions are injected explicitly - no hidden global state.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.agent import deepseek_v4_llm, run_agent
from app.gate import call_tool
from app.jobs import claim_job, complete_job
from app.models import InventoryItem, Job


def execute_job(
    session: Session,
    job: Job,
    llm_fn: Callable[[dict[str, Any]], dict[str, Any]] = deepseek_v4_llm,
) -> None:
    """Stub executor (M4/M5) plus the real agent path (M6).

    _fail / _action controls are kept so M4's crash-resume proof and other
    tests still run. The default path (no controls) runs the capability-gated
    agent: every side effect goes through app.gate.call_tool.
    """
    payload = job.payload or {}

    if payload.get("_fail"):
        raise RuntimeError("stub failure")

    if payload.get("_action") == "insert_po":
        item = session.get(InventoryItem, job.inventory_item_id)
        if item is None:  # pragma: no cover - test constructs valid items
            raise RuntimeError(
                f"execute_job: inventory_item_id {job.inventory_item_id} not found"
            )
        call_tool(
            session,
            job_id=job.id,
            process_type_id=job.process_type_id,
            tool_name="create_purchase_order",
            tool_input={
                "vendor_id": item.vendor_id,
                "inventory_item_id": item.id,
                "quantity": item.reorder_quantity,
                "created_by_job_id": job.id,
            },
            decision_reasoning="execute_job: item below reorder threshold",
        )
        return

    # default (M6): the real capability-gated agent for low_stock_reorder.
    run_agent(session, job, llm_fn=llm_fn)


def recover_stale_running(session: Session, stale_after_seconds: int) -> int:
    """Reset 'running' jobs whose updated_at is past the lease window to 'pending'.

    A worker that died mid-job (crash / kill -9 / lost DB connection) leaves
    its job stuck in 'running' forever; this is what lets a restarted worker
    resume it. Returns the number of jobs recovered.
    """
    cutoff = func.now() - timedelta(seconds=stale_after_seconds)
    result = session.execute(
        update(Job)
        .where(Job.status == "running")
        .where(Job.updated_at < cutoff)
        .values(status="pending", updated_at=func.now())
    )
    session.commit()
    return result.rowcount


def run_worker_round(
    session: Session,
    execute_fn: Callable[[Session, Job], None] = execute_job,
    lease_seconds: int = 300,
) -> list[int]:
    """One full worker pass: recover stale, poll due jobs, claim, execute, complete.

    Returns the ids of the jobs processed this round.
    """
    recover_stale_running(session, stale_after_seconds=lease_seconds)

    due = session.scalars(
        select(Job)
        .where(Job.status.in_(("pending", "retrying")))
        .where(Job.next_run_at <= func.now())
        .order_by(Job.id)
    ).all()

    processed: list[int] = []
    for job in due:
        if not claim_job(session, job.id):
            continue  # lost the race to another worker

        try:
            execute_fn(session, job)
        except Exception as exc:  # noqa: BLE001 - worker must never crash the loop
            session.rollback()
            complete_job(session, job.id, success=False, error=str(exc))
        else:
            complete_job(session, job.id, success=True)
        processed.append(job.id)

    return processed
