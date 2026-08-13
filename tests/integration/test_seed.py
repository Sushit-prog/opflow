"""M2 - seed data proof-tests against the real DB.

TEST_TAXONOMY M2:
- test_seed_produces_below_threshold_item: at least one seeded inventory_items
  row has quantity_on_hand < reorder_threshold.
- test_seed_process_type_exists: low_stock_reorder exists with the correct
  allowed_tools (and max_attempts) - proving the migration's seed row is intact
  and unmodified by the M2 seed.

Both hit Postgres (via the `seeded` fixture) - never mocked.
"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models import InventoryItem, ProcessType

EXPECTED_ALLOWED_TOOLS = ["query_inventory", "create_purchase_order", "notify_vendor"]
EXPECTED_MAX_ATTEMPTS = 3


@pytest.mark.integration
def test_seed_produces_below_threshold_item(seeded: bool, engine: Engine) -> None:
    with Session(engine) as session:
        count = session.scalar(
            select(func.count())
            .select_from(InventoryItem)
            .where(InventoryItem.quantity_on_hand < InventoryItem.reorder_threshold)
        )
    assert count and count >= 1, (
        "seed produced no inventory item below its reorder threshold"
    )


@pytest.mark.integration
def test_seed_process_type_exists(seeded: bool, engine: Engine) -> None:
    with Session(engine) as session:
        pt = session.scalar(
            select(ProcessType).where(ProcessType.name == "low_stock_reorder")
        )
    assert pt is not None, "low_stock_reorder process_type missing"
    # Assert the migration-owned row is intact and unmodified.
    assert pt.allowed_tools == EXPECTED_ALLOWED_TOOLS
    assert pt.max_attempts == EXPECTED_MAX_ATTEMPTS
