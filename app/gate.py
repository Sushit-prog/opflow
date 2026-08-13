"""Capability Gate (INTERFACES.md §3) - the ONLY path by which tools run.

    check_tool_allowed(session, process_type_id, tool_name)
        - looks up process_types.allowed_tools (JSONB array)
        - raises ToolNotWhitelisted if tool_name is not in the array

    call_tool(session, job_id, process_type_id, tool_name, tool_input, decision_reasoning)
        - the executable gate: check_tool_allowed, then dispatch to the tool
        - EVERY call - allowed or rejected - writes exactly one audit_log row
          (tool_called, tool_input, tool_output-or-error, decision_reasoning,
          timestamp), committed before returning/raising
        - nothing in the codebase invokes a tool function directly; agent /
          worker code may only call call_tool

Rejected calls land in audit_log with tool_output = the OpFlowError's
structured to_dict() payload (app/errors.py §6), not a stringified exception.
"""
from __future__ import annotations

from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import OpFlowError, ToolNotWhitelisted
from app.models import AuditLog, ProcessType
from app.tools import create_purchase_order, notify_vendor, query_inventory

# tool_name -> implementation. Registered here so the gate can dispatch
# without any direct tool invocation in caller code.
_TOOL_REGISTRY: dict[str, Callable[..., dict[str, Any]]] = {
    "query_inventory": query_inventory,
    "create_purchase_order": create_purchase_order,
    "notify_vendor": notify_vendor,
}


def _audit_row(
    session: Session,
    *,
    job_id: int,
    tool_name: str,
    tool_input: dict[str, Any] | None,
    tool_output: dict[str, Any] | None,
    decision_reasoning: str | None,
) -> None:
    session.add(
        AuditLog(
            job_id=job_id,
            tool_called=tool_name,
            tool_input=tool_input,
            tool_output=tool_output,
            decision_reasoning=decision_reasoning,
        )
    )
    session.commit()


def check_tool_allowed(
    session: Session, process_type_id: int, tool_name: str
) -> bool:
    """Whitelist check (§3). Returns True or raises ToolNotWhitelisted.

    A missing process_type is treated as an empty whitelist (nothing allowed).
    """
    allowed = session.scalar(
        select(ProcessType.allowed_tools).where(
            ProcessType.id == process_type_id
        )
    )
    if allowed is None:
        allowed = []

    if tool_name not in allowed:
        raise ToolNotWhitelisted(tool_name, process_type_id)
    return True


def call_tool(
    session: Session,
    *,
    job_id: int,
    process_type_id: int,
    tool_name: str,
    tool_input: dict[str, Any] | None = None,
    decision_reasoning: str | None = None,
) -> dict[str, Any]:
    """Execute a tool through the gate, writing exactly one audit_log row.

    - rejected (not whitelisted): one audit row with the ToolNotWhitelisted
      error payload, then the exception propagates
    - allowed: dispatch, then one audit row with the tool's output dict
    - unexpected (non-OpFlowError) tool failure: one audit row with a generic
      InternalError payload, then the exception propagates
    """
    tool_input = tool_input or {}

    try:
        check_tool_allowed(session, process_type_id, tool_name)
    except ToolNotWhitelisted as exc:
        _audit_row(
            session,
            job_id=job_id,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_output=exc.to_dict(),
            decision_reasoning=decision_reasoning,
        )
        raise

    tool_fn = _TOOL_REGISTRY[tool_name]
    try:
        result = tool_fn(session, **tool_input)
    except OpFlowError as exc:
        _audit_row(
            session,
            job_id=job_id,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_output=exc.to_dict(),
            decision_reasoning=decision_reasoning,
        )
        raise
    except Exception as exc:  # noqa: BLE001 - gate must audit, then re-raise
        _audit_row(
            session,
            job_id=job_id,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_output={"error": "InternalError", "message": str(exc)},
            decision_reasoning=decision_reasoning,
        )
        raise

    _audit_row(
        session,
        job_id=job_id,
        tool_name=tool_name,
        tool_input=tool_input,
        tool_output=result,
        decision_reasoning=decision_reasoning,
    )
    return result
