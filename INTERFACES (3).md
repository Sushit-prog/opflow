# OpFlow — Interfaces (Phase 0)

Contracts must be implemented exactly as specified here. Any deviation found during a milestone drift-check must either be fixed to match this doc, or this doc must be updated first (change-control, not silent redesign) — same discipline as SOPVM's INTERFACES.md.

---

## 1. Tool Interfaces

All tools are called only through the Capability Gate (§3). No tool is ever invoked directly by agent code.

### `query_inventory`
- **Input:** `{ sku: str }` **or** `{ inventory_item_id: int }` (exactly one)
- **Output:** `{ id, sku, name, quantity_on_hand, reorder_threshold, reorder_quantity, vendor_id, updated_at }`
- **Errors:** `ItemNotFound`
- **Side effects:** none (read-only)

### `create_purchase_order`
- **Input:** `{ vendor_id: int, inventory_item_id: int, quantity: int (>0), created_by_job_id: int }`
- **Output:** `{ id, status: "draft", created_at }`
- **Errors:** `VendorNotFound`, `ItemNotFound`, `InvalidQuantity`
- **Idempotency:** if a `purchase_orders` row already exists with `created_by_job_id` equal to the input, return that row instead of inserting a duplicate. This is the mechanism that makes a retried job safe.
- **Side effects:** `INSERT INTO purchase_orders` (or no-op if idempotent match found)

### `notify_vendor`
- **Input:** `{ vendor_id: int, purchase_order_id: int, message: str }`
- **Output:** `{ sent: bool, vendor_contact_email: str }`
- **Errors:** `VendorNotFound`, `PONotFound`
- **Side effects:** log-only in M5/M6 (no real email send) — write to stdout/logger, not audit_log directly (audit_log write happens at the gate level, see §3)

---

## 2. Job Lifecycle Interface

```
enqueue_job(process_type_id, idempotency_key, inventory_item_id, payload) -> Job
    INSERT INTO jobs (...) VALUES (...) ON CONFLICT (idempotency_key) DO NOTHING
    RETURNING *  -- if no row returned (conflict), SELECT the existing row and return it

claim_job(job_id) -> bool
    UPDATE jobs SET status='running', updated_at=now()
    WHERE id=%s AND status IN ('pending','retrying') AND next_run_at <= now()
    -- returns True iff rowcount == 1. This single UPDATE is the concurrency guard —
    -- two workers racing on the same job_id will only have one succeed.

complete_job(job_id, success: bool, error: str | None)
    on success: status='succeeded', updated_at=now()
    on failure:
        attempts += 1
        if attempts >= process_type.max_attempts:
            status='failed'
        else:
            status='retrying'
            next_run_at = now() + backoff(attempts)
        error = <error text>, updated_at=now()
```

**Backoff formula (fixed for this project — document any change):**
`backoff(attempts) = min(5 * 2**attempts, 300)` seconds (base 5s, cap 5min)

---

## 3. Capability Gate Interface

```
check_tool_allowed(process_type_id, tool_name) -> bool
    - look up process_types.allowed_tools (JSONB array) for process_type_id
    - if tool_name not in that array: raise ToolNotWhitelisted(tool_name, process_type_id)
    - EVERY call through this gate — allowed or rejected — writes one row to audit_log
      (tool_called, tool_input, tool_output OR error, decision_reasoning, timestamp)
    - this is the only path by which a tool may be executed; nothing calls a tool function directly
```

---

## 4. Poller Interface

```
poll_low_stock() -> list[Job]
    SELECT * FROM inventory_items WHERE quantity_on_hand < reorder_threshold
    for each row:
        idempotency_key = sha256(f"{process_type_id}:{item_id}:{today_date_iso}").hexdigest()
        enqueue_job(process_type_id, idempotency_key, item_id, payload={...})
    return the jobs it touched (new or pre-existing)
```

Read-only against `inventory_items` — the poller never mutates inventory itself. Idempotency key is date-scoped: an item can trigger at most one job per calendar day, re-triggering the next day if still below threshold.

---

## 5. Agent Interface

```
run_agent(job: Job) -> AgentResult
    input:  job.payload + a fresh query_inventory() snapshot of the item (never trust stale payload data)
    output: { action: "create_po_and_notify" | "skip", reasoning: str }
    - every tool call the agent decides to make goes through the Capability Gate (§3)
    - the agent NEVER writes to the database directly — only via tool calls
    - reasoning string is persisted to audit_log.decision_reasoning
```

---

## 6. Error Taxonomy

All domain errors are subclasses of `OpFlowError` and carry enough structured data to log to `audit_log.tool_output` as JSON (not just a stringified exception).
