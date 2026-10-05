# Summon Daem0n v7 in Claude Code

This is the maintained Claude Code ritual for Daem0n v7. It uses opaque
workspace selectors, replay-safe writes, and the authenticated MCP invocation
scope. Do not substitute a filesystem path for `workspace_id`, and do not
invent transport identity fields.

## 1. Detect the v7 tools

The core ritual tools are:

- `session_brief`
- `memory_preflight`
- `memory_recall`
- `memory_store`
- `memory_record_outcome`
- `system_health`

Claude Code normally exposes them as `mcp__daem0nmcp__<tool>`. Hosts may also
show the bare name or the `daem0nmcp_<tool>` form. Detect only those exact
forms. If none is present, install or reconnect the MCP server; do not guess a
retired tool name.

## 2. Connect the server

Daem0n v7 supports stdio and Streamable HTTP.

For a user-scoped stdio connection:

```bash
python -m pip install -e "/path/to/Daem0n-MCP"
claude mcp add daem0nmcp --scope user -- python -m daem0nmcp.server
claude mcp list
```

For Streamable HTTP, start the launcher and point the Claude MCP configuration
at its single MCP endpoint:

```bash
python start_server.py --port 9876
```

```json
{
  "mcpServers": {
    "daem0nmcp": {
      "type": "http",
      "url": "http://127.0.0.1:9876/mcp"
    }
  }
}
```

Restart Claude Code after changing MCP configuration. Use `system_health` for
diagnostics once the tools are visible:

```text
mcp__daem0nmcp__system_health(
    workspace_id="<opaque-workspace-id>"
)
```

## 3. Begin every scoped session

Use the configured, opaque workspace selector exactly as issued.

The first Daem0n call in a session briefs automatically; the compact brief is returned in `meta.covenant.auto_brief`. Call `session_brief` for the full brief.

The server-issued session and authenticated transport establish scope. Request
headers, addresses, client descriptions, and arbitrary caller-supplied metadata
are not identity inputs.

## 4. Recall relevant history

Use bounded recall when prior decisions, warnings, or failures may affect the
task:

```text
mcp__daem0nmcp__memory_recall(
    workspace_id="<opaque-workspace-id>",
    query="authentication",
    limit=10
)
```

Treat returned evidence as counsel. Respect `must_not`, warnings, and failed
approaches before protected work.

## 5. Direct protected calls and advance planning

Call `memory_store` (or any protected tool) directly. If it returns `COUNSEL_REQUIRED`, read `error.counsel` (guidance and reasons), then retry exactly `error.remedy`. `memory_preflight` remains available for planning a change in advance.

Use `daem0n_tools_search(query)`, then `daem0n_tool_call(workspace_id, tool, arguments)`.

Tokens remain exact-argument, single-use capabilities valid for 300 seconds,
bound to workspace, principal, session, and tool. Respect all returned guidance.
Defaults are `DAEM0NMCP_COVENANT_MODE=guided` and `DAEM0NMCP_TOOL_SURFACE=core`.
Set `DAEM0NMCP_COVENANT_MODE=strict` for explicit `session_brief`, exact
`memory_preflight`, and token-bearing writes; `DAEM0NMCP_TOOL_SURFACE=full`
lists all registered tools.

## 6. Store durable knowledge replay-safely

Every write needs a stable idempotency key. Retries of the same logical write
must reuse the same key and exact payload:

```text
mcp__daem0nmcp__memory_store(
    workspace_id="<opaque-workspace-id>",
    record_type="decision",
    content="Use signed session cookies",
    rationale="Avoid shared server-side session state",
    idempotency_key="decision-auth-cookie-0001"
)
```

Keep the returned opaque `record_id`. Never replace it with a legacy numeric
identifier.

## 7. Record the verified outcome

When the result is known, record success or failure with a separate stable
idempotency key:

```text
mcp__daem0nmcp__memory_record_outcome(
    workspace_id="<opaque-workspace-id>",
    record_id="<record-id-from-memory_store>",
    outcome_text="Focused and integration tests passed",
    worked=true,
    idempotency_key="outcome-auth-cookie-0001"
)
```

Failures are durable evidence. Use `worked=false` and state precisely what
failed.

## 8. Read bounded workspace resources

The maintained v7 resources are:

```text
memory://workspaces/{workspace_id}/warnings
memory://workspaces/{workspace_id}/failures
memory://workspaces/{workspace_id}/rules
memory://workspaces/{workspace_id}/active-context
```

These resources are read-only views. Domain writes go through admitted v7 MCP
tools, never through a direct database, script, or memory-writing CLI command.

## 9. Hook behavior

Install the packaged hooks with:

```bash
python -m daem0nmcp.cli install-claude-hooks
```

This writes six entries to the user-level `~/.claude/settings.json`. The hooks
remind; they never block an edit, a command, or the end of a turn, and none of
them writes Daem0n memory.

| Event (matcher) | Hook | What it does | Input | Reminds only |
|-----------------|------|--------------|-------|--------------|
| `SessionStart` | `session_start` | Explains automatic session briefing and optional `session_brief` for the full brief | `CLAUDE_PROJECT_DIR` env | Yes |
| `PreToolUse` (`Edit\|Write\|NotebookEdit`) | `pre_edit` | Adds a one-line `additionalContext` reminder to call `memory_recall_file` through `daem0n_tool_call` for past decisions and warnings; silent outside a Daem0n project | stdin event | Yes |
| `PreToolUse` (`Bash`) | `pre_bash` | Checks the command against Daem0n rules. Currently inert: it reads a `TOOL_INPUT` env var that Claude Code does not set | `TOOL_INPUT` env | Yes (always exits 0) |
| `PostToolUse` (`mcp__.*__edit_preflight`) | `post_edit_preflight` | Edit-bridge plumbing: stages an `edit_preflight` receipt for a bridge edit request. Nothing in Claude Code creates those requests any more, so it is a no-op | stdin event | Yes |
| `PostToolUse` (`Edit\|Write\|NotebookEdit`) | `post_edit` | Edit-bridge plumbing: reports a bridge-approved edit as a capture candidate. Only loads the bridge when the project is paired; a no-op in Claude Code today | stdin event | Yes |
| `Stop`, `SubagentStop` | `stop` | Suggests direct `memory_store` calls with challenge-retry guidance and `memory_record_outcome` calls as a `systemMessage`. Never writes memory | stdin event (`transcript_path`) | Yes |

No hook blocks a tool call or keeps the agent running: every hook exits 0 and none
returns a `permissionDecision` or `decision`.

Run from a project, the installer also pairs that project with the local edit
bridge: an owner-only credential under `~/.daem0nmcp/edit-bridges/<authority>/`
and bridge path env keys in `<project>/.claude/settings.local.json`. The old
`hooks/daem0n_*.py` scripts are deprecated stubs; re-run `install-claude-hooks`
if your settings still point at them.

## 10. Migration reference

The generated source of truth for renamed or split v6 capabilities is
[`docs/v6-to-v7-tools.json`](docs/v6-to-v7-tools.json). Consult that mapping
instead of copying an older invocation into a prompt, hook, or skill.

The guided ritual is automatic briefing, bounded `memory_recall`, direct
replay-safe `memory_store` with challenge retries when needed, and verified
`memory_record_outcome`; `memory_preflight` is available for advance planning.
