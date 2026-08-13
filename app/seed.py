"""Seed data for the OpFlow reorder demo (M2 - data only).

seed_all(session) idempotently upserts the demonstration dataset by natural
key, so running it repeatedly never creates duplicates:

    - vendors:        unique by contact_email
    - inventory_items: unique by sku

It deliberately does NOT touch process_types - the low_stock_reorder row is
owned by the migration (versions/0001) and must remain exactly as-is.

No poller/worker/tool/agent logic belongs here.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import InventoryItem, Vendor

# --- 2 vendors (realistic names for a Kolkata fluid-sealing manufacturer) ---
VENDORS: list[dict] = [
    {
        "name": "Precision Seals Pvt. Ltd.",
        "contact_email": "contact@precisionseals.in",
        "lead_time_days": 14,
    },
    {
        "name": "Kolkata Gasket Works",
        "contact_email": "orders@gasketworks.in",
        "lead_time_days": 21,
    },
]

# --- 5 inventory items distributed across both vendors ---------------------
# Two items (FP-1001, GP-3001) are below their reorder_threshold on purpose,
# guaranteeing the poller trigger condition exists for the demo.
ITEMS: list[dict] = [
    {
        "sku": "FP-1001",
        "name": "PTFE Gasket 3 inch",
        "quantity_on_hand": 40,
        "reorder_threshold": 60,
        "reorder_quantity": 120,
        "vendor_email": "contact@precisionseals.in",
    },
    {
        "sku": "OR-2002",
        "name": "Nitrile O-Ring 50mm",
        "quantity_on_hand": 120,
        "reorder_threshold": 40,
        "reorder_quantity": 200,
        "vendor_email": "contact@precisionseals.in",
    },
    {
        "sku": "GP-3001",
        "name": "Graphite Spiral Wound Gasket",
        "quantity_on_hand": 15,
        "reorder_threshold": 20,
        "reorder_quantity": 60,
        "vendor_email": "orders@gasketworks.in",
    },
    {
        "sku": "HP-4001",
        "name": "Hydraulic Seal Kit",
        "quantity_on_hand": 55,
        "reorder_threshold": 25,
        "reorder_quantity": 50,
        "vendor_email": "orders@gasketworks.in",
    },
    {
        "sku": "RM-5001",
        "name": "Rubber Sheet 1m x 1m",
        "quantity_on_hand": 200,
        "reorder_threshold": 100,
        "reorder_quantity": 80,
        "vendor_email": "contact@precisionseals.in",
    },
]


def seed_all(session: Session) -> dict[str, int]:
    """Upsert vendors + inventory items; return counts of rows INSERTed.

    Idempotent: re-running only updates existing rows by natural key, never
    inserts duplicates. Leaves process_types untouched.
    """
    vendors_created = 0
    items_created = 0

    # vendor_email -> Vendor (entity loaded/created this run)
    vendor_by_email: dict[str, Vendor] = {}

    for spec in VENDORS:
        vendor = session.scalar(
            select(Vendor).where(Vendor.contact_email == spec["contact_email"])
        )
        if vendor is None:
            vendor = Vendor(
                name=spec["name"],
                contact_email=spec["contact_email"],
                lead_time_days=spec["lead_time_days"],
            )
            session.add(vendor)
            vendors_created += 1
        else:
            # Upsert mutable fields on re-run (still idempotent by key).
            vendor.name = spec["name"]
            vendor.lead_time_days = spec["lead_time_days"]
        vendor_by_email[spec["contact_email"]] = vendor

    session.flush()  # assign vendor ids

    for spec in ITEMS:
        vendor = vendor_by_email[spec["vendor_email"]]
        item = session.scalar(select(InventoryItem).where(InventoryItem.sku == spec["sku"]))
        if item is None:
            item = InventoryItem(
                sku=spec["sku"],
                name=spec["name"],
                quantity_on_hand=spec["quantity_on_hand"],
                reorder_threshold=spec["reorder_threshold"],
                reorder_quantity=spec["reorder_quantity"],
                vendor_id=vendor.id,
            )
            session.add(item)
            items_created += 1
        else:
            item.name = spec["name"]
            item.quantity_on_hand = spec["quantity_on_hand"]
            item.reorder_threshold = spec["reorder_threshold"]
            item.reorder_quantity = spec["reorder_quantity"]
            if item.vendor_id != vendor.id:
                item.vendor_id = vendor.id

    session.commit()
    return {"vendors_created": vendors_created, "items_created": items_created}
