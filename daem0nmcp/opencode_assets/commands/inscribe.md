---
description: Store a replay-safe Daem0n v7 memory record
---

Store the following as a v7 memory record:

$ARGUMENTS

Choose `record_type` from the content:
- "decision" for architectural or design choices
- "pattern" for recurring approaches to follow
- "warning" for things to avoid
- "learning" for lessons from experience

Create one stable idempotency key. Call `daem0nmcp_memory_preflight` with
`target_tool="memory_store"` and the exact target arguments, then call:

```text
daem0nmcp_memory_store(
    workspace_id="<workspace_id>",
    record_type="<chosen>",
    content="$ARGUMENTS",
    rationale="<why this matters>",
    idempotency_key="<stable-key>",
    preflight_token="<token-from-memory_preflight>"
)
```

Report the returned `record_id`. After verification, use
`daem0nmcp_memory_record_outcome` with that ID and a separate stable
idempotency key.
