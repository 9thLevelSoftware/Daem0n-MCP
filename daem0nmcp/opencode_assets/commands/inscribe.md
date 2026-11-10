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

Create one stable idempotency key and call `memory_store` directly:

```text
daem0nmcp_memory_store(
    workspace_id="<workspace_id>",
    record_type="<chosen>",
    content="$ARGUMENTS",
    idempotency_key="<stable-key>"
)
```

If `COUNSEL_REQUIRED`, review `error.counsel` (guidance and reasons), then retry
exactly `error.remedy`, preserving the idempotency key. `memory_preflight`
remains available for planning a change in advance. The first Daem0n call
briefs automatically in `meta.covenant.auto_brief`; `session_brief` gives the full brief.

Report the returned `record_id`. After verification, use
`daem0nmcp_memory_record_outcome` with that ID and a separate stable
idempotency key.
