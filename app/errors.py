"""Domain error taxonomy (INTERFACES.md §6).

All domain errors subclass OpFlowError and carry enough structured data to
log to audit_log.tool_output as JSON (via to_dict()) - not just a stringified
exception. The capability gate (app/gate.py) serializes these into the audit
row.
"""
from __future__ import annotations

from typing import Any


class OpFlowError(Exception):
    """Base class for every OpFlow domain error (§6)."""

    code: str = "OpFlowError"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        """Structured JSON payload for audit_log.tool_output."""
        return {"error": self.code, "message": self.message}


class ItemNotFound(OpFlowError):
    code = "ItemNotFound"

    def __init__(
        self,
        *,
        sku: str | None = None,
        inventory_item_id: int | None = None,
    ) -> None:
        self.sku = sku
        self.inventory_item_id = inventory_item_id
        super().__init__(
            f"inventory item not found: sku={sku!r} inventory_item_id={inventory_item_id!r}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **super().to_dict(),
            "sku": self.sku,
            "inventory_item_id": self.inventory_item_id,
        }


class VendorNotFound(OpFlowError):
    code = "VendorNotFound"

    def __init__(self, vendor_id: int) -> None:
        self.vendor_id = vendor_id
        super().__init__(f"vendor not found: vendor_id={vendor_id!r}")

    def to_dict(self) -> dict[str, Any]:
        return {**super().to_dict(), "vendor_id": self.vendor_id}


class PONotFound(OpFlowError):
    code = "PONotFound"

    def __init__(self, purchase_order_id: int) -> None:
        self.purchase_order_id = purchase_order_id
        super().__init__(f"purchase order not found: purchase_order_id={purchase_order_id!r}")

    def to_dict(self) -> dict[str, Any]:
        return {**super().to_dict(), "purchase_order_id": self.purchase_order_id}


class InvalidQuantity(OpFlowError):
    code = "InvalidQuantity"

    def __init__(self, quantity: int) -> None:
        self.quantity = quantity
        super().__init__(f"quantity must be a positive integer, got {quantity!r}")

    def to_dict(self) -> dict[str, Any]:
        return {**super().to_dict(), "quantity": self.quantity}


class InvalidJobReference(OpFlowError):
    """created_by_job_id is not a usable job reference.

    Raised when the value cannot be compared against the integer
    purchase_orders.created_by_job_id column at all (e.g. a malformed /
    injection-style string) - the DB rejects it before any row is touched.
    """

    code = "InvalidJobReference"

    def __init__(self, created_by_job_id: Any) -> None:
        self.created_by_job_id = created_by_job_id
        super().__init__(
            f"invalid job reference: created_by_job_id={created_by_job_id!r}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {**super().to_dict(), "created_by_job_id": self.created_by_job_id}


class ToolNotWhitelisted(OpFlowError):
    code = "ToolNotWhitelisted"

    def __init__(self, tool_name: str, process_type_id: int) -> None:
        self.tool_name = tool_name
        self.process_type_id = process_type_id
        super().__init__(
            f"tool {tool_name!r} is not whitelisted for process_type {process_type_id}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **super().to_dict(),
            "tool_name": self.tool_name,
            "process_type_id": self.process_type_id,
        }
