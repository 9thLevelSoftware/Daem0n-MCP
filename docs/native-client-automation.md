# Native client automation

`install-client` keeps MCP registration and optional automation separate. Omit
`--automation` to preserve existing hooks and retain the earlier MCP-only
installation behavior. An explicit mode is project-scoped:

```text
python -m daem0nmcp.cli --project-path <project> install-client codex --automation remind
python -m daem0nmcp.cli --project-path <project> install-client agy --automation remind
python -m daem0nmcp.cli --project-path <project> install-client omp --automation remind
python -m daem0nmcp.cli --project-path <project> install-client pi --automation remind
python -m daem0nmcp.cli --project-path <project> install-client opencode --client-version 1.18.21 --automation enforce
```

Use `--dry-run` to parse and preview all project changes without creating storage
or provisioning a bridge credential. JSON output includes `automation.mode`,
`automation.covered_tools`, `automation.events`, and `automation.enforced`.
When the option is omitted, mode is `unchanged` and `enforced` is `null`; the
installer makes no claim that pre-existing automation is disabled.
Native acceptance evidence must be checked separately for the installed host
version; successful configuration alone does not establish host execution.

`--tool-surface core|full` independently selects MCP tool exposure. New bindings
default to `core`, with discovery and the generic gateway. `full` exposes the
registered tools with their individual input schemas, so clients can invoke
typed code-search/indexing tools directly. It increases the tool catalog; it
does not relax argument validation, workspace binding, counsel or edit gates.
Omitting the option preserves an existing selection. JSON output reports the
configured `tool_surface` (`inherited` if the existing binding sets no value)
and `tool_surface_requested`. Selecting a surface does not change automation.

```text
python -m daem0nmcp.cli --project-path <project> install-client opencode --client-version 1.18.21 --tool-surface full --dry-run
```

Remove `--dry-run` to apply the reviewed project-local change. Native acceptance
records the selected surface separately; a full-surface pass does not establish
that the same workflow passed through the core gateway.

Installed guidance treats health as optional diagnostics, not a prerequisite
for indexing or memory calls. Explicit task tool limits and call order still
apply. OpenCode's full and simplified injected contexts carry the same guidance
as the managed project covenant; refreshing only the project file can leave an
older installed plugin in effect.

| Client | Reminder installation | Enforced native edits |
| --- | --- | --- |
| Codex | Project `.codex/hooks.json`: SessionStart, native patch reminders, Stop | Unsupported |
| agy | Project `.agents/hooks.json`: SessionStart, file-edit reminders, Stop | Unsupported |
| OMP | Project `.omp/extensions/daem0n.ts`: before-agent-start reminder | Unsupported |
| Pi | Project `.pi/extensions/daem0n.ts`: before-agent-start reminder | Unsupported |
| OpenCode V1 | Project plugin: covenant, lifecycle, and native edit reminders | `edit`, `write`, `apply_patch` through the existing edit bridge |
| OpenCode V2 >=2.2.7 | Separate project plugin: session context reminder | `edit`, `write`, `patch` through its version-specific adapter; check native acceptance evidence |

Claude Code uses `install-claude-hooks --automation off|remind|enforce`. Enforce
requires `--client-version 2.1.295` or later for native hook-failure blocking.
Omitting the automation option preserves that command's earlier hook behavior.

`off` removes only owned Codex/agy reminder commands. Owned OpenCode plugins and
OMP/Pi extensions remain present with their automation disabled. Unrelated
handlers, settings, and extensions remain intact. Customized managed plugins or
extensions produce `CONFIG_CONFLICT` so local changes can be preserved before
reinstallation. Credentials are retained when disabling automation.

`remind` provides context without approving or blocking native tools. OMP and Pi
install turn-start reminders only, so their reported `covered_tools` are empty.
Clients without an enforcement adapter return `UNSUPPORTED_AUTOMATION` before
writing project files; unsupported V2 versions return
`UNSUPPORTED_CLIENT_VERSION`. The installer never grants project trust or broad
tool approval.

OpenCode V1 `enforce` provisions an owner-only local bridge credential outside the
project, publishes only its path into project configuration, and installs a
plugin with an embedded enforcement mode. Covered edits require the existing
exact, short-lived `edit_preflight` receipt. Hook or bridge failure denies a
covered edit. Native shell commands and other filesystem-writing routes remain
outside this declared tool coverage. Review the native-client acceptance matrix
before relying on a specific host/version combination.

## Explicit OpenCode V2 configuration

V1 remains the default. V2 requires an observed V2 version and an explicit
interface selection; it uses `mcp.servers`, `disabled`, per-phase timeout
objects, an unpooled project-specific connection, and the legacy MCP handshake.

```text
python -m daem0nmcp.cli --project-path <project> install-client opencode --client-version 2.0.0 --opencode-interface v2
python -m daem0nmcp.cli --project-path <project> install-client opencode --client-version 2.0.0 --opencode-interface v2 --opencode-code-mode direct
python -m daem0nmcp.cli --project-path <project> install-client opencode --client-version 2.2.7 --opencode-interface v2 --automation enforce
```

The first command selects default Code Mode; the second exposes direct MCP
tools. Repeat the selected flags on subsequent installations. Existing V1
settings are not reinterpreted as V2 settings, and a mismatched observed version
or connection policy is rejected before publication. See the official
[OpenCode MCP configuration reference](https://opencode.ai/docs/mcp-servers).

V2 automation has its own asset and sidecar, preserving the V1 plugin. Its
context hook provides reminders for each model invocation. Enforce additionally
uses V2 execution hooks and the shared approval bridge. Code Mode inner tools
can share an outer call ID, so the adapter also binds each original tool name
and argument digest. Real structured `result.output` is the approval evidence;
rendered model text is not. Failed native executions settle their exact spent
host binding without capturing success or reviving the receipt.

Provider login, native host trust, MCP Apps rendering, resource browsing, and
actual edit execution are separate acceptance checks.

For the tested V2 2.2.7 resource helper, Daem0n-only metadata discovery uses
`opencode_list_mcp_resources({"server":"daem0nmcp"})`. Omitting `server` asks
about every configured server, including disabled entries, and can correctly
fail a Daem0n-only permission grant. Resource discovery is not tool discovery:
use directly listed typed tools or the native tool catalog when finding tools.
Only read resource contents when the task needs them, retaining the discovered
server and exact workspace URI. The installer does not broaden host permissions
to accommodate unscoped requests.
