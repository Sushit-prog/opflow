"""Poller (INTERFACES.md §4) - read-only against inventory.

poll_low_stock(session): finds every inventory item below its reorder
threshold and enqueues an idempotent, date-scoped job for each.

The poller NEVER mutates inventory_items - it only ever INSERTs into jobs.
A given item can trigger at most one job per calendar day (the idempotency
key is hash(process_type_id, item_id, today)), so a re-trigger the next day
creates a new job only if the item is still below threshold.

Sessions are injected explicitly - no hidden global state.
"""
from __future__ import annotations

import hashlib
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.jobs import enqueue_job
from app.models import InventoryItem, Job, ProcessType

LOW_STOCK_PROCESS_TYPE = "low_stock_reorder"


def _low_stock_process_type_id(session: Session) -> int:
    """Resolve the process_type id by name (configuration-driven, not hardcoded)."""
    pt = session.scalar(
        select(ProcessType).where(ProcessType.name == LOW_STOCK_PROCESS_TYPE)
    )
    if pt is None:
        raise RuntimeError(
            f"process_type {LOW_STOCK_PROCESS_TYPE!r} not found - run migrations + seed"
        )
    return pt.id


def _idempotency_key(process_type_id: int, item_id: int, today_iso: str) -> str:
    """Date-scoped idempotency key per INTERFACES.md §4."""
    raw = f"{process_type_id}:{item_id}:{today_iso}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _job_payload(item: InventoryItem) -> dict[str, Any]:
    """Trigger context stored on the job (agent still re-queries a fresh snapshot)."""
    return {
        "inventory_item_id": item.id,
        "sku": item.sku,
        "name": item.name,
        "quantity_on_hand": item.quantity_on_hand,
        "reorder_threshold": item.reorder_threshold,
        "reorder_quantity": item.reorder_quantity,
    }


def poll_low_stock(session: Session) -> list[Job]:
    """Find below-threshold items and enqueue one (idempotent) job each.

    Read-only against inventory_items; only inserts into jobs. Returns the
    jobs it touched (new or pre-existing), in item id order.
    """
    process_type_id = _low_stock_process_type_id(session)
    today_iso = date.today().isoformat()

    low_items = session.scalars(
        select(InventoryItem).where(
            InventoryItem.quantity_on_hand < InventoryItem.reorder_threshold
        )
    ).all()

    touched: list[Job] = []
    for item in sorted(low_items, key=lambda i: i.id):
        key = _idempotency_key(process_type_id, item.id, today_iso)
        job = enqueue_job(
            session,
            process_type_id=process_type_id,
            idempotency_key=key,
            inventory_item_id=item.id,
            payload=_job_payload(item),
        )
        touched.append(job)

    session.commit()
    return touched
