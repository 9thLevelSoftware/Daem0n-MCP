---
name: openspec-daem0n-bridge
description: Bridges OpenSpec proposals and archived outcomes with replay-safe Daem0n v7 memory.
---

# OpenSpec–Daem0n v7 Bridge

Use this skill when a workspace contains `openspec/`, or when the user asks to
sync specifications, prepare a proposal with historical context, or preserve an
archived change outcome.

Daem0n is accessed only through admitted v7 MCP tools. Use the opaque
`workspace_id` supplied for the current workspace. Never use a directory as a
selector, invent identity inputs, write directly to storage, or launch a
memory-writing command.

## Required v7 setup

The core tools are `session_brief`, `memory_preflight`, `memory_recall`,
`memory_store`, `memory_record_outcome`, and `system_health`. Claude Code
normally exposes them as `mcp__daem0nmcp__<tool>`. Bare and
`daem0nmcp_<tool>` forms are valid only when the host actually exposes those
exact names.

Daem0n supports stdio and Streamable HTTP at `/mcp`. A typical Streamable HTTP
endpoint is `http://127.0.0.1:9876/mcp`. If the v7 tools are missing or the
server is unhealthy, call:

```text
mcp__daem0nmcp__system_health(
    workspace_id="<opaque-workspace-id>"
)
```

The generated capability migration reference is
[`docs/v6-to-v7-tools.json`](../../../docs/v6-to-v7-tools.json). Use that file
instead of copying an older invocation into this workflow.

## Session gate

The first Daem0n call in a session briefs automatically; the compact brief is returned in `meta.covenant.auto_brief`. Call `session_brief` for the full brief.

Call `memory_store` (or any protected tool) directly. If it returns `COUNSEL_REQUIRED`, read `error.counsel` (guidance and reasons), then retry exactly `error.remedy`. `memory_preflight` remains available for planning a change in advance.

Use `daem0n_tools_search(query)`, then `daem0n_tool_call(workspace_id, tool, arguments)`.

Tokens remain exact-argument, single-use, and valid for 300 seconds. Defaults
are guided/core. `DAEM0NMCP_COVENANT_MODE=strict` requires explicit
`session_brief`, exact `memory_preflight`, and token-bearing writes;
`DAEM0NMCP_TOOL_SURFACE=full` lists every registered tool.

Respect the returned warnings, failed outcomes, applicable rules, and next
steps. If the tool is unavailable, continue with OpenSpec file work only; do
not attempt an alternate memory write path.

## Detect OpenSpec

Check for these workspace-relative locations with the host's normal file tools:

- `openspec/specs/`
- `openspec/changes/`
- `openspec/changes/archive/`

Do not treat detection as permission to write memory automatically. First read
the relevant files, summarize bounded evidence, and show the user what will be
stored when the requested workflow calls for a durable write.

## Workflow 1: synchronize specifications

Use this workflow for “sync specs”, “import OpenSpec”, or “refresh OpenSpec”.

1. Enumerate `openspec/specs/*/spec.md` with bounded file discovery.
2. Read each selected specification.
3. Extract:

   - purpose and recommended behavior as `pattern` records;
   - prohibitions and known limitations as `warning` records;
   - design choices and rationale as `decision` records only when the spec
     actually makes a choice.

4. Before storing each record, recall existing OpenSpec evidence:

```text
mcp__daem0nmcp__memory_recall(
    workspace_id="<opaque-workspace-id>",
    query="OpenSpec <spec-name>",
    limit=10,
    tags=["openspec", "spec", "<spec-name>"]
)
```

5. Construct one exact, stable request. The key must identify the same logical
   content on retry and must change when the content changes:

```text
{
    "record_type": "pattern",
    "content": "<bounded specification summary>",
    "rationale": "OpenSpec specification is the source of truth",
    "tags": ["openspec", "spec", "<spec-name>"],
    "relative_file_path": "openspec/specs/<spec-name>/spec.md",
    "idempotency_key": "openspec-spec-<stable-content-key>"
}
```

6. Call `memory_store` directly with the reviewed fields:

```text
mcp__daem0nmcp__memory_store(
    workspace_id="<opaque-workspace-id>",
    record_type="pattern",
    content="<bounded specification summary>",
    rationale="OpenSpec specification is the source of truth",
    tags=["openspec", "spec", "<spec-name>"],
    relative_file_path="openspec/specs/<spec-name>/spec.md",
    idempotency_key="openspec-spec-<stable-content-key>"
)
```

Keep every returned opaque `record_id`. Report stored, replayed, skipped, and
failed counts separately. Never claim a sync succeeded without the tool result.

## Workflow 2: inform a proposal

Use this workflow before drafting a proposal or plan.

1. Read the current specification and any relevant pending change files.
2. Recall evidence for the feature:

```text
mcp__daem0nmcp__memory_recall(
    workspace_id="<opaque-workspace-id>",
    query="OpenSpec proposal <feature>",
    limit=20,
    tags=["openspec"]
)
```

3. Present a compact evidence section with relevant specs, successful patterns,
   warnings, failed outcomes, and applicable rules. Distinguish evidence from
   inference.
4. Draft the proposal only after applying `must_do`, `must_not`, and `ask_first`
   guidance.
5. If the proposal decision should be durable, preflight and store it with the
   same exact-request pattern as Workflow 1. Recommended fields are:

```text
{
    "record_type": "decision",
    "content": "Create OpenSpec proposal <change-id>: <bounded intent>",
    "rationale": "<reviewed proposal rationale>",
    "tags": ["openspec", "proposal", "pending", "<change-id>"],
    "relative_file_path": "openspec/changes/<change-id>/proposal.md",
    "idempotency_key": "openspec-proposal-<stable-content-key>"
}
```

Retain the returned `record_id`; Workflow 3 needs it. Do not substitute a
search result or a numeric identifier.

## Workflow 3: archive to verified outcomes

Use this workflow after an OpenSpec change is actually archived.

1. Read the archived proposal, tasks, and specification deltas under
   `openspec/changes/archive/<change-id>/`.
2. Verify completed and incomplete tasks from those files and from test or
   deployment evidence supplied by the user.
3. Locate the stored proposal decision with bounded `memory_recall` if its
   opaque `record_id` is not already in the conversation.
4. Record the verified outcome with a stable key:

```text
mcp__daem0nmcp__memory_record_outcome(
    workspace_id="<opaque-workspace-id>",
    record_id="<record-id-from-proposal-memory_store>",
    outcome_text="Archived <change-id>: <verified result>",
    worked=true,
    idempotency_key="openspec-outcome-<change-id>-0001"
)
```

Use `worked=false` when the implementation failed, was reverted, or did not
meet the archived specification. State the observed failure precisely.

5. Store new reusable learning through direct `memory_store`; on
   `COUNSEL_REQUIRED`, review `error.counsel` and retry exactly `error.remedy`.
   Do not convert speculation into a learning.

## Read-only context resources

These bounded v7 resources may supplement tool results:

```text
memory://workspaces/{workspace_id}/warnings
memory://workspaces/{workspace_id}/failures
memory://workspaces/{workspace_id}/rules
memory://workspaces/{workspace_id}/active-context
```

They are read-only views. Use their opaque IDs exactly as returned.

## Completion report

For every workflow, report:

- OpenSpec files inspected;
- memory evidence recalled;
- warnings or rules that changed the plan;
- protected writes attempted and their returned record IDs;
- idempotent replays or conflicts;
- the verified outcome, if one was recorded;
- any degradation reported by `system_health`.

Never imply that merely reading or editing an OpenSpec file updated Daem0n.
