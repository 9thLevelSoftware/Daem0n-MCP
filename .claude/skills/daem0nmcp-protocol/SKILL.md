---
name: daem0nmcp-protocol
description: Enforce the Daem0n v7 guided session, inline counsel, replay-safe memory, and outcome protocol
---

# Daem0n v7 Protocol

Apply this skill whenever the Daem0n v7 tools are available.

## Detection

Recognize each canonical tool in bare form and with either host prefix:

| Canonical | OpenCode | Claude Code |
|---|---|---|
| `session_brief` | `daem0nmcp_session_brief` | `mcp__daem0nmcp__session_brief` |
| `memory_preflight` | `daem0nmcp_memory_preflight` | `mcp__daem0nmcp__memory_preflight` |
| `memory_recall` | `daem0nmcp_memory_recall` | `mcp__daem0nmcp__memory_recall` |
| `memory_store` | `daem0nmcp_memory_store` | `mcp__daem0nmcp__memory_store` |
| `memory_record_outcome` | `daem0nmcp_memory_record_outcome` | `mcp__daem0nmcp__memory_record_outcome` |
| `system_health` | `daem0nmcp_system_health` | `mcp__daem0nmcp__system_health` |

Do not treat a substring or an older workflow name as a match.

## Required sequence

The first Daem0n call in a session briefs automatically; the compact brief is returned in `meta.covenant.auto_brief`. Call `session_brief` for the full brief.

Recall relevant context when needed:

   ```text
   mcp__daem0nmcp__memory_recall(
       workspace_id="<workspace_id>",
       query="the planned change",
       limit=10
   )
   ```

Call `memory_store` (or any protected tool) directly. If it returns `COUNSEL_REQUIRED`, read `error.counsel` (guidance and reasons), then retry exactly `error.remedy`. `memory_preflight` remains available for planning a change in advance.

Use `daem0n_tools_search(query)`, then `daem0n_tool_call(workspace_id, tool, arguments)`.

Store a durable decision directly:

   ```text
   mcp__daem0nmcp__memory_store(
       workspace_id="<workspace_id>",
       record_type="decision",
       content="Use append-only events",
       idempotency_key="decision-events-0001"
   )
   ```

After verification, record the outcome:

   ```text
   mcp__daem0nmcp__memory_record_outcome(
       workspace_id="<workspace_id>",
       record_id="<mem_id>",
       outcome_text="The event replay tests passed",
       worked=true,
       idempotency_key="outcome-events-0001"
   )
   ```

Use `worked=false` for failed approaches and explain the failure. Retry a write
with the same idempotency key; never mint a new key merely because a response
was lost.

## Boundaries

- A `preflight_token` authorizes only the exact workspace, tool, arguments,
  principal, and session for which it was issued.
- Tokens are single-use and valid for 300 seconds.
- Defaults are `DAEM0NMCP_COVENANT_MODE=guided` and `DAEM0NMCP_TOOL_SURFACE=core`.
  Set `DAEM0NMCP_COVENANT_MODE=strict` for explicit `session_brief`, exact
  `memory_preflight`, and token-bearing writes; `DAEM0NMCP_TOOL_SURFACE=full`
  lists all registered tools.
- Never infer identity from headers, network address, client information, or
  `_client_meta`.
- Treat `must_not` guidance as a hard constraint.
- Use `system_health(workspace_id="<workspace_id>")` for diagnostics.
- Use stdio or Streamable HTTP at `/mcp`.

Read-only context is also available at:

- `memory://workspaces/{workspace_id}/warnings`
- `memory://workspaces/{workspace_id}/failures`
- `memory://workspaces/{workspace_id}/rules`
- `memory://workspaces/{workspace_id}/active-context`

For a v6 migration, consult the generated
[`docs/v6-to-v7-tools.json`](../../../docs/v6-to-v7-tools.json) mapping. It is
the authoritative reference for renamed and split operations.
