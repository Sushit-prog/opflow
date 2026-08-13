"""SQLAlchemy ORM models mirroring the exact DDL from opflow_spec.md.

These models exist so application/service code has typed, real access to the
schema. They must match migrations/versions/0001_initial_schema.py exactly
(columns, types, nullability, server defaults, constraints, indexes) - the
integration drift test test_orm_models_match_live_db enforces this against the
live Postgres.

Phase 0/1 constraint: schema + scaffolding only. No poller/worker/tool/agent
logic anywhere in this module.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class Vendor(Base):
    __tablename__ = "vendors"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    contact_email: Mapped[str] = mapped_column(sa.Text, nullable=False)
    lead_time_days: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="7"
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )

    inventory_items: Mapped[list[InventoryItem]] = relationship(
        back_populates="vendor"
    )


class InventoryItem(Base):
    __tablename__ = "inventory_items"
    __table_args__ = (
        sa.CheckConstraint(
            "quantity_on_hand >= 0",
            name="inventory_items_quantity_on_hand_check",
        ),
        sa.Index("idx_inventory_vendor", "vendor_id"),
        sa.Index("idx_inventory_reorder_check", "quantity_on_hand", "reorder_threshold"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    sku: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    quantity_on_hand: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="0"
    )
    reorder_threshold: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    reorder_quantity: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    vendor_id: Mapped[int] = mapped_column(
        sa.Integer, sa.ForeignKey("vendors.id"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )

    vendor: Mapped[Vendor] = relationship(back_populates="inventory_items")


class ProcessType(Base):
    __tablename__ = "process_types"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    poll_interval_seconds: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="60"
    )
    allowed_tools: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    max_attempts: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="3"
    )
    enabled: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )

class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        sa.CheckConstraint(
            "status IN ('pending','running','succeeded','failed','retrying')",
            name="jobs_status_check",
        ),
        sa.Index("idx_jobs_poll", "status", "next_run_at"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    process_type_id: Mapped[int] = mapped_column(
        sa.Integer, sa.ForeignKey("process_types.id"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    inventory_item_id: Mapped[int | None] = mapped_column(
        sa.Integer, sa.ForeignKey("inventory_items.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="pending")
    attempts: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="0"
    )
    next_run_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    __table_args__ = (
        sa.CheckConstraint("quantity > 0", name="purchase_orders_quantity_check"),
        sa.CheckConstraint(
            "status IN ('draft','sent','confirmed')",
            name="purchase_orders_status_check",
        ),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    vendor_id: Mapped[int] = mapped_column(
        sa.Integer, sa.ForeignKey("vendors.id"), nullable=False
    )
    inventory_item_id: Mapped[int] = mapped_column(
        sa.Integer, sa.ForeignKey("inventory_items.id"), nullable=False
    )
    quantity: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="draft")
    created_by_job_id: Mapped[int | None] = mapped_column(
        sa.Integer, sa.ForeignKey("jobs.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (sa.Index("idx_audit_job", "job_id"),)

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        sa.Integer, sa.ForeignKey("jobs.id"), nullable=False
    )
    tool_called: Mapped[str] = mapped_column(sa.Text, nullable=False)
    tool_input: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    tool_output: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    decision_reasoning: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )

