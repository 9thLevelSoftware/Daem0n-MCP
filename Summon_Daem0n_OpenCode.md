# Summon Daem0n v7 in OpenCode

This guide is the maintained OpenCode ritual for Daem0n v7. It uses the
authenticated MCP scope and an opaque `workspace_id`; a local directory is
never a workspace selector.

## Connect Daem0n

Daem0n v7 supports stdio and Streamable HTTP at `/mcp`.

Install the package for a stdio connection:

```bash
python -m pip install -e "/path/to/Daem0n-MCP"
```

Then configure the OpenCode MCP entry to run:

```text
python -m daem0nmcp.server
```

For Streamable HTTP, start:

```bash
python start_server.py --port 9876
```

and configure the remote MCP URL as:

```text
http://127.0.0.1:9876/mcp
```

Restart OpenCode after changing its MCP configuration.

## Detect exact tool names

OpenCode normally exposes the core tools with a `daem0nmcp_` prefix:

- `daem0nmcp_session_brief`
- `daem0nmcp_memory_preflight`
- `daem0nmcp_memory_recall`
- `daem0nmcp_memory_store`
- `daem0nmcp_memory_record_outcome`
- `daem0nmcp_system_health`

The bare and Claude Code-prefixed forms may appear in other hosts. Detect only
the exact host forms documented by the v7 Covenant; never guess an older tool
name.

## Run the v7 ritual

The first Daem0n call in a session briefs automatically; the compact brief is returned in `meta.covenant.auto_brief`. Call `session_brief` for the full brief.

Recall bounded history when it is relevant:

```text
daem0nmcp_memory_recall(
    workspace_id="<opaque-workspace-id>",
    query="authentication",
    limit=10
)
```

Call `memory_store` (or any protected tool) directly. If it returns `COUNSEL_REQUIRED`, read `error.counsel` (guidance and reasons), then retry exactly `error.remedy`. `memory_preflight` remains available for planning a change in advance.

Use `daem0n_tools_search(query)`, then `daem0n_tool_call(workspace_id, tool, arguments)`.

Tokens remain exact-argument, single-use capabilities valid for 300 seconds,
bound to workspace, principal, session, and tool. Defaults are guided covenant
and core tool listing. Set `DAEM0NMCP_COVENANT_MODE=strict` for explicit
`session_brief`, exact `memory_preflight`, and token-bearing writes;
`DAEM0NMCP_TOOL_SURFACE=full` lists all tools.

For a durable decision:

```text
daem0nmcp_memory_store(
    workspace_id="<opaque-workspace-id>",
    record_type="decision",
    content="Use signed session cookies",
    rationale="Avoid shared server-side session state",
    idempotency_key="decision-auth-cookie-0001"
)
```

Retain the returned opaque `record_id`. When the result is verified:

```text
daem0nmcp_memory_record_outcome(
    workspace_id="<opaque-workspace-id>",
    record_id="<record-id-from-memory_store>",
    outcome_text="Focused and integration tests passed",
    worked=true,
    idempotency_key="outcome-auth-cookie-0001"
)
```

Use `worked=false` for a failed approach and describe the failure. Reuse a
stable idempotency key only for a retry of the same logical write.

For diagnostics:

```text
daem0nmcp_system_health(
    workspace_id="<opaque-workspace-id>"
)
```

Transport metadata, headers, addresses, and client descriptions are not caller
identity. Do not invent identity inputs in tool arguments.

## Read bounded resources

OpenCode can read these exact v7 resource templates:

```text
memory://workspaces/{workspace_id}/warnings
memory://workspaces/{workspace_id}/failures
memory://workspaces/{workspace_id}/rules
memory://workspaces/{workspace_id}/active-context
```

They are read-only. Do not bypass admitted MCP tools with direct storage or a
memory-writing command.

## OpenCode protocol files

The maintained plugin is `.opencode/plugins/daem0n.ts`, and the maintained
commands live under `.opencode/commands/`. They use opaque workspace selectors
and the exact v7 tools above.

The generated migration source of truth is
[`docs/v6-to-v7-tools.json`](docs/v6-to-v7-tools.json). Use it whenever an old
guide or prompt names a capability that moved or split in v7.

The guided ritual is automatic briefing, bounded recall, direct replay-safe
`memory_store` with challenge retries when needed, and verified
`memory_record_outcome`; `memory_preflight` is optional advance planning.
