---
name: summon-daem0n
description: Connect a project to Daem0n v7 and begin a workspace-scoped memory session
---

# Summon Daem0n v7

Use this skill when the user wants to connect, initialize, or diagnose Daem0n.
Never delete or recreate `.daem0nmcp` data as an initialization shortcut.

## 1. Confirm the transport

Daem0n v7 supports stdio and Streamable HTTP. The HTTP MCP endpoint is `/mcp`
(for example, `http://127.0.0.1:9876/mcp`). Do not configure an SSE endpoint.

## 2. Detect exact v7 tool names

The core tools are:

- `session_brief`
- `memory_preflight`
- `memory_recall`
- `memory_store`
- `memory_record_outcome`
- `system_health`

Claude Code may expose `session_brief` as
`mcp__daem0nmcp__session_brief`; OpenCode may expose it as
`daem0nmcp_session_brief`. Treat only the exact bare or host-prefixed forms as
the same tool.

## 3. Verify health and begin the session

Use the opaque ID assigned to the configured project root:

```text
mcp__daem0nmcp__system_health(workspace_id="<workspace_id>")
```
Call `session_brief` only when you want the full briefing.

Do not send project paths, request headers, IP addresses, client information,
or `_client_meta` as identity. The transport supplies identity and the server
resolves `workspace_id` against its registry.

## 4. Read useful context

```text
mcp__daem0nmcp__memory_recall(
    workspace_id="<workspace_id>",
    query="current architecture and warnings",
    limit=10
)
```

The four bounded JSON resources are:

- `memory://workspaces/{workspace_id}/warnings`
- `memory://workspaces/{workspace_id}/failures`
- `memory://workspaces/{workspace_id}/rules`
- `memory://workspaces/{workspace_id}/active-context`

## 5. Explain the write protocol

The first Daem0n call in a session briefs automatically; the compact brief is returned in `meta.covenant.auto_brief`. Call `session_brief` for the full brief.

Call `memory_store` (or any protected tool) directly. If it returns `COUNSEL_REQUIRED`, read `error.counsel` (guidance and reasons), then retry exactly `error.remedy`. `memory_preflight` remains available for planning a change in advance.

Use `daem0n_tools_search(query)`, then `daem0n_tool_call(workspace_id, tool, arguments)`.

Include a stable `idempotency_key`, retain `record_id`, and record verified
results using `memory_record_outcome` with another stable key. Tokens remain
exact-argument, single-use, and valid for 300 seconds. Defaults are guided/core;
`DAEM0NMCP_COVENANT_MODE=strict` requires explicit briefing and preflight,
and `DAEM0NMCP_TOOL_SURFACE=full` lists every registered tool.

For migrations from v6, use the generated mapping at
[`docs/v6-to-v7-tools.json`](../../../docs/v6-to-v7-tools.json). Do not invent
an equivalent from memory.
