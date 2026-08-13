"""Real capability tools (INTERFACES.md §1).

Three tools, exact signatures from the interface contract:

    query_inventory(sku=...) | query_inventory(inventory_item_id=...)  -> item dict
    create_purchase_order(vendor_id, inventory_item_id, quantity, created_by_job_id) -> PO dict
    notify_vendor(vendor_id, purchase_order_id, message)               -> {sent, vendor_contact_email}

Rules that apply to every tool in this module:

- They are NEVER called directly by application code. The only execution path
  is app.gate.call_tool (the Capability Gate, §3), which checks the
  process_type's allowed_tools whitelist and writes one audit_log row per call.
- Sessions are injected explicitly - no hidden global state.
- Domain failures raise OpFlowError subclasses (app/errors.py, §6) so the gate
  can serialize them into audit_log.tool_output as structured JSON.

M5 scope: notify_vendor is log-only (stdout/logger) - no real email send.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import (
    InvalidQuantity,
    ItemNotFound,
    PONotFound,
    VendorNotFound,
)
from app.models import InventoryItem, PurchaseOrder, Vendor

logger = logging.getLogger(__name__)


def _iso(dt: Any) -> str:
    """Serialize a DB timestamp to ISO 8601 for tool output dicts."""
    return dt.isoformat() if dt is not None else None


def query_inventory(
    session: Session,
    *,
    sku: str | None = None,
    inventory_item_id: int | None = None,
) -> dict[str, Any]:
    """Read-only inventory lookup (§1). Exactly one of sku / inventory_item_id.

    Errors: ItemNotFound. Output: {id, sku, name, quantity_on_hand,
    reorder_threshold, reorder_quantity, vendor_id, updated_at}.
    """
    if (sku is None) == (inventory_item_id is None):
        raise ValueError("query_inventory: exactly one of sku or inventory_item_id required")

    stmt = select(InventoryItem)
    if sku is not None:
        stmt = stmt.where(InventoryItem.sku == sku)
    else:
        stmt = stmt.where(InventoryItem.id == inventory_item_id)

    item = session.scalar(stmt)
    if item is None:
        raise ItemNotFound(sku=sku, inventory_item_id=inventory_item_id)

    return {
        "id": item.id,
        "sku": item.sku,
        "name": item.name,
        "quantity_on_hand": item.quantity_on_hand,
        "reorder_threshold": item.reorder_threshold,
        "reorder_quantity": item.reorder_quantity,
        "vendor_id": item.vendor_id,
        "updated_at": _iso(item.updated_at),
    }


def create_purchase_order(
    session: Session,
    *,
    vendor_id: int,
    inventory_item_id: int,
    quantity: int,
    created_by_job_id: int,
) -> dict[str, Any]:
    """Draft a purchase order (§1).

    Errors: VendorNotFound, ItemNotFound, InvalidQuantity.

    Idempotency (the crash-resume safety mechanism): if a purchase_orders row
    already exists with created_by_job_id == the input, return THAT row instead
    of inserting a duplicate. This is what makes a retried / crash-resumed job
    safe - exactly what the M4 stub proved, now against the real tool.

    Side effect: INSERT INTO purchase_orders (or no-op on idempotent match).
    Commits its own transaction, mirroring jobs.py's commit-inside-service
    convention, so the PO survives even if complete_job never runs (crash).
    """
    if not isinstance(quantity, int) or quantity <= 0:
        raise InvalidQuantity(quantity)

    vendor = session.get(Vendor, vendor_id)
    if vendor is None:
        raise VendorNotFound(vendor_id)

    item = session.get(InventoryItem, inventory_item_id)
    if item is None:
        raise ItemNotFound(inventory_item_id=inventory_item_id)

    existing = session.scalar(
        select(PurchaseOrder).where(
            PurchaseOrder.created_by_job_id == created_by_job_id
        )
    )
    if existing is not None:
        return {
            "id": existing.id,
            "status": existing.status,
            "created_at": _iso(existing.created_at),
        }

    po = PurchaseOrder(
        vendor_id=vendor_id,
        inventory_item_id=inventory_item_id,
        quantity=quantity,
        status="draft",
        created_by_job_id=created_by_job_id,
    )
    session.add(po)
    session.commit()
    session.refresh(po)
    return {
        "id": po.id,
        "status": po.status,
        "created_at": _iso(po.created_at),
    }


def notify_vendor(
    session: Session,
    *,
    vendor_id: int,
    purchase_order_id: int,
    message: str,
) -> dict[str, Any]:
    """Notify the vendor of a PO (§1).

    Errors: VendorNotFound, PONotFound. Output: {sent, vendor_contact_email}.

    M5/M6: log-only - writes to the logger/stdout, NOT to audit_log (that
    write happens at the gate level, §3). No real email is sent.
    """
    vendor = session.get(Vendor, vendor_id)
    if vendor is None:
        raise VendorNotFound(vendor_id)

    po = session.get(PurchaseOrder, purchase_order_id)
    if po is None:
        raise PONotFound(purchase_order_id)

    logger.info(
        "notify_vendor: PO %s for vendor %s (%s): %s",
        purchase_order_id,
        vendor_id,
        vendor.contact_email,
        message,
    )
    return {"sent": True, "vendor_contact_email": vendor.contact_email}
