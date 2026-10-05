# Multi-Repository Setup Guide (v7)

Daem0n v7 identifies every registered repository with an opaque
`workspace_id`. Tool inputs and resource URIs use that ID, never a filesystem
root. Register roots in server configuration before starting either stdio or
Streamable HTTP at `/mcp`.

## Choose an ownership model

### Consolidated parent workspace

Use one registered parent workspace when the repositories share lifecycle and
access policy:

```text
/workspace/                 -> ws_parent
├── backend/
└── client/
```

The first call automatically briefs the parent workspace. Query its shared
record stream; call `session_brief` only for the full brief:

```text
memory_recall(
    workspace_id="ws_000000000000000000000001",
    query="authentication across backend and client",
    limit=10
)
```

### Linked workspaces

Register each repository separately when it needs independent ownership,
authorization, export, or archival:

```text
/workspace/backend/         -> ws_000000000000000000000002
/workspace/client/          -> ws_000000000000000000000003
```

Use `daem0n_tools_search(query="link workspaces")` to discover `workspace_link`,
then call it through the gateway:

```text
daem0n_tool_call(
    workspace_id="ws_000000000000000000000002",
    tool="workspace_link",
    arguments={
        "linked_workspace_id": "ws_000000000000000000000003",
        "relationship": "same-project"
    }
)
```

On `COUNSEL_REQUIRED`, review `error.counsel` and retry exactly `error.remedy`.
`memory_preflight` remains optional advance planning. Each federated linked
workspace still needs its own explicit `session_brief` before linked recall.

Linked recall remains explicit: provide authorized `linked_workspace_ids` to
`memory_recall`. The server resolves every ID before reading and does not infer
workspace scope from paths.

## Consolidating registered workspaces

Consolidation appends canonical v7 events to the target workspace. Discover it
with `daem0n_tools_search`, then make the protected replay-safe call directly:

```text
daem0n_tool_call(
    workspace_id="ws_000000000000000000000001",
    tool="workspace_consolidate",
    arguments={
        "source_workspace_ids": [
            "ws_000000000000000000000002",
            "ws_000000000000000000000003"
        ],
        "idempotency_key": "consolidate-product-2026-0001"
    }
)
```

On `COUNSEL_REQUIRED`, read `error.counsel`, then retry exactly `error.remedy`;
reuse the idempotency key. Tokens remain exact-argument, single-use, and valid
for 300 seconds. Defaults are guided/core. `DAEM0NMCP_COVENANT_MODE=strict`
requires explicit briefing and preflight; `DAEM0NMCP_TOOL_SURFACE=full` lists
all tools.

Use `workspace_consolidate_and_archive_sources` only when source archival is
intentional and separately authorized. Verify the target with `system_health`
and bounded recall before archiving anything.

## Read-only workspace context

Replace `{workspace_id}` with the exact registered ID:

- `memory://workspaces/{workspace_id}/warnings`
- `memory://workspaces/{workspace_id}/failures`
- `memory://workspaces/{workspace_id}/rules`
- `memory://workspaces/{workspace_id}/active-context`

For a v6 installation, migrate/register the repositories before using these
examples. The generated mapping at
[`docs/v6-to-v7-tools.json`](v6-to-v7-tools.json) documents every v6 split or
rename.
