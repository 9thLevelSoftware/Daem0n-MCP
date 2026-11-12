# Summon Daem0n v7 in OpenCode

This companion describes the OpenCode V1 binding and exact v7 protocol. For the
complete AI-led installation, source checkout, dedicated environment, local
models, existing-data migration, configuration preservation and restart proof,
read [the full grimoire](Summon_Daem0n.md) and select `opencode` in Ritual IV.
The full grimoire remains usable when copied alone into a target project.

Say to the AI in the target project:

> Read Summon_Daem0n.md and perform the complete summoning ritual for this project using OpenCode.

The binding uses authenticated MCP scope and an opaque `workspace_id`; a local
directory is never a workspace selector. Configuration support and successful
model-backed acceptance are separate results.

## Connect Daem0n

Daem0n v7 supports stdio and Streamable HTTP at `/mcp`. Prefer project-scoped
stdio on Windows, macOS and Linux. OpenCode's native MCP manager keeps the
initialized connection alive; counsel and its exact remedy must use that same
server-issued session.

After the full grimoire's installation and workspace-ID steps, run from the
trusted Daem0n source directory with its dedicated interpreter and the explicit
project environment:

```text
<VENV_PYTHON> -m daem0nmcp.cli --project-path "<PROJECT>" --json install-client opencode --client-version "<OBSERVED_VERSION>" --dry-run
<VENV_PYTHON> -m daem0nmcp.cli --project-path "<PROJECT>" --json install-client opencode --client-version "<OBSERVED_VERSION>"
```

Use `opencode --version` for the observed version. The installer preserves
unrelated JSON/JSONC members, comments and project instructions, writes the owned
MCP entry and covenant section, and reports backups or actionable conflicts.
It leaves provider credentials, permissions and global configuration unchanged.
Do not replace a file containing other servers or use `--force` to resolve a
conflict. Record the result in the full grimoire's checkpoint.

This path targets **OpenCode V1** (the researched installed snapshot was
1.18.21). Current public V2 documentation uses a different `mcp.servers` shape
and `disabled` flag. The installer rejects an explicit V2 selection; V2 needs
separate integration evidence. Do not silently upgrade OpenCode or write a V1
entry into a V2 configuration.

For inspecting an existing V1 registration, the intended shape is below.
Replace placeholders with the full dedicated interpreter and canonical project:

```json
{
  "mcp": {
    "daem0nmcp": {
      "type": "local",
      "command": ["<PYTHON_PATH>", "-E", "-u", "-X", "utf8", "-c", "import sys; sys.path = [p for p in sys.path if p]; import runpy; runpy.run_module('daem0nmcp.server', run_name='__main__', alter_sys=True)"],
      "cwd": "<PROJECT>",
      "enabled": true,
      "environment": {
        "PYTHONPATH": "",
        "DAEM0NMCP_PROJECT_ROOT": "<PROJECT>",
        "DAEM0NMCP_COVENANT_MODE": "guided",
        "DAEM0NMCP_TOOL_SURFACE": "core"
      }
    }
  }
}
```

Use that bootstrap rather than `python -m daem0nmcp.server`. OpenCode starts the server
inside the opened project, and `-m` puts that directory first on `sys.path`, so
a repository containing its own `daem0nmcp/` directory would run instead of the
installed server. `-E` ignores inherited Python import settings before startup;
the maintained installer writes this launcher using its own fixed interpreter.

Inspect `opencode mcp list` from the target project, honor the host's project/MCP
trust flow and restart/reconnect after configuration changes. Continue Ritual VI
of the full grimoire: actual brief, health, store/remedy, matching recall,
outcome, semantic readiness and post-restart recall. A connected list entry alone
does not complete the ritual. A provider refresh/login error must be reported
as a host-account blocker rather than hidden by changing Daem0n's transport.

For an explicitly chosen HTTP deployment, follow Ritual VIII of the full
grimoire and register its Streamable HTTP `/mcp` endpoint; HTTP is optional.

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

Guided mode automatically briefs an unbriefed session on `memory_recall`, `memory_preflight`, `memory_store`, or `memory_record_outcome`, returning `meta.covenant.auto_brief`. Diagnostic and discovery calls do not trigger briefing. Call `session_brief` for the full brief.

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

The native MCP entry and managed covenant section provide the core workflow.
The installer updates `AGENTS.md`, or the existing `CLAUDE.md` fallback when
`AGENTS.md` is absent, preserving the project's other effective instructions.
Optional existing V1 plugins at `.opencode/plugins/daem0n.ts` and command files
under `.opencode/commands/` can supply reminders and shortcuts. Their native edit
hooks are reminder-only; core memory use does not require the legacy edit bridge.
The older `install-opencode` path scaffolds that plugin integration and can
provision bridge pairing; it is not the default project client installer above.
Inspect its dry-run and existing files before choosing that additional workflow.
Its `--force` replaces files and can discard unrelated configuration.

The generated migration source of truth is
[`docs/v6-to-v7-tools.json`](docs/v6-to-v7-tools.json). Use it whenever an old
guide or prompt names a capability that moved or split in v7.

The guided ritual is automatic briefing, bounded recall, direct replay-safe
`memory_store` with challenge retries when needed, and verified
`memory_record_outcome`; `memory_preflight` is optional advance planning.
