"""Tool-calling agent for low_stock_reorder (INTERFACES.md §5).

    run_agent(session, job, llm_fn=deepseek_v4_llm) -> AgentResult

Flow (per §5):
1. Fresh inventory snapshot: the agent NEVER trusts job.payload's possibly
   stale snapshot. It fetches a current query_inventory() result through the
   Capability Gate (app.gate.call_tool) - reads are tool calls too, and every
   gate call is audited.
2. The LLM (DeepSeek V4 via OpenRouter, injected for tests) receives
   {job_payload, inventory_snapshot} and returns a STRUCTURED decision:
   {action: "create_po_and_notify" | "skip", reasoning: str}.
3. Acting is done ONLY through the gate - this module never writes to the
   database itself. For create_po_and_notify it calls create_purchase_order
   then notify_vendor through the gate, in that order; the decision's
   reasoning string is persisted as audit_log.decision_reasoning on those
   calls (the gate writes the rows).

The real LLM call (deepseek_v4_llm) is an OpenRouter chat-completions request
with a single tool (decide_next_action) so the model's output is forced into
the structured shape. Tests inject a fake llm_fn and never hit the network;
everything downstream of the decision hits the real Postgres.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Literal

import httpx

from app.core.config import get_settings
from app.gate import call_tool
from app.models import Job

AgentAction = Literal["create_po_and_notify", "skip"]

OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

_SYSTEM_PROMPT = (
    "You are the reorder agent for OpFlow, an inventory operations system. "
    "Given a job payload and a FRESH inventory snapshot, decide the single "
    "next action: 'create_po_and_notify' when the item is at or below its "
    "reorder threshold and a purchase order should be drafted and the vendor "
    "notified, or 'skip' when no action is needed. You never touch the "
    "database yourself - your decision is executed for you. Always provide a "
    "concise reasoning string explaining the decision."
)

_DECIDE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "decide_next_action",
        "description": "Structured decision for a low-stock inventory item.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create_po_and_notify", "skip"],
                    "description": "create_po_and_notify if a PO should be drafted and the vendor notified, else skip.",
                },
                "reasoning": {
                    "type": "string",
                    "description": "Why this action was chosen, based on the fresh inventory snapshot.",
                },
            },
            "required": ["action", "reasoning"],
            "additionalProperties": False,
        },
    },
}


@dataclass(frozen=True)
class AgentResult:
    """Structured agent output (§5): an action plus its reasoning."""

    action: AgentAction
    reasoning: str


def deepseek_v4_llm(context: dict[str, Any]) -> dict[str, Any]:
    """Real LLM call: DeepSeek V4 via OpenRouter, forced structured output.

    Raises RuntimeError (missing key, network/HTTP failure, malformed model
    output) - the worker catches it and retries the job with backoff.
    """
    settings = get_settings()
    if not settings.openrouter_api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY is not set; cannot call DeepSeek V4 via OpenRouter"
        )

    body = {
        "model": settings.openrouter_model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(context, default=str)},
        ],
        "tools": [_DECIDE_TOOL],
        "tool_choice": "required",
    }
    response = httpx.post(
        OPENROUTER_ENDPOINT,
        headers={
            "Authorization": f"Bearer {settings.openrouter_api_key}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=60.0,
    )
    response.raise_for_status()

    message = response.json()["choices"][0]["message"]
    tool_calls = message.get("tool_calls") or []
    if tool_calls:
        arguments = json.loads(tool_calls[0]["function"]["arguments"])
    else:
        # Fallback: model answered in content instead of calling the tool.
        arguments = json.loads(message.get("content") or "{}")
    return arguments


def _validate_decision(decision: Any) -> AgentResult:
    """Coerce the LLM's raw output into an AgentResult or raise RuntimeError."""
    if not isinstance(decision, dict):
        raise RuntimeError(f"agent returned a non-object decision: {decision!r}")
    action = decision.get("action")
    reasoning = decision.get("reasoning")
    if action not in ("create_po_and_notify", "skip"):
        raise RuntimeError(f"agent returned invalid action {action!r}")
    if not isinstance(reasoning, str) or not reasoning.strip():
        raise RuntimeError(f"agent returned empty reasoning for action {action!r}")
    return AgentResult(action=action, reasoning=reasoning.strip())


def run_agent(
    session: Any,
    job: Job,
    llm_fn: Callable[[dict[str, Any]], dict[str, Any]] = deepseek_v4_llm,
) -> AgentResult:
    """Run the agent for one job (§5). See module docstring for the flow.

    `session` is passed straight through to the gate - this module performs
    no database access of its own.
    """
    if job.inventory_item_id is None:
        raise RuntimeError(
            f"run_agent: job {job.id} has no inventory_item_id to evaluate"
        )

    # 1. Fresh snapshot through the gate - never the payload's stale one.
    snapshot = call_tool(
        session,
        job_id=job.id,
        process_type_id=job.process_type_id,
        tool_name="query_inventory",
        tool_input={"inventory_item_id": job.inventory_item_id},
        decision_reasoning="agent: fetch fresh inventory snapshot",
    )

    # 2. Structured decision from the LLM.
    decision = _validate_decision(
        llm_fn(
            {
                "job_payload": job.payload or {},
                "inventory_snapshot": snapshot,
            }
        )
    )

    # 3. Act only through the gate, reasoning persisted to audit rows.
    if decision.action == "create_po_and_notify":
        po = call_tool(
            session,
            job_id=job.id,
            process_type_id=job.process_type_id,
            tool_name="create_purchase_order",
            tool_input={
                "vendor_id": snapshot["vendor_id"],
                "inventory_item_id": snapshot["id"],
                "quantity": snapshot["reorder_quantity"],
                "created_by_job_id": job.id,
            },
            decision_reasoning=decision.reasoning,
        )
        call_tool(
            session,
            job_id=job.id,
            process_type_id=job.process_type_id,
            tool_name="notify_vendor",
            tool_input={
                "vendor_id": snapshot["vendor_id"],
                "purchase_order_id": po["id"],
                "message": (
                    f"Low-stock reorder: {snapshot['name']} ({snapshot['sku']}), "
                    f"PO {po['id']} for {snapshot['reorder_quantity']} units"
                ),
            },
            decision_reasoning=decision.reasoning,
        )

    return decision
