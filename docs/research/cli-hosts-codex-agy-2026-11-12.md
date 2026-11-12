# Codex CLI and agy host integration research

Research date: 2026-11-12

Repository: `D:\Daem0n-MCP`, branch `codex/multi-cli-installation`

Scope: installed Codex CLI and agy (Antigravity CLI), native MCP, project instructions, hooks, trust, and disposable acceptance.

This is the source-research snapshot before live testing. Subsequent outcomes
are tracked in the [multi-CLI acceptance report](../release/v7/multi-cli-acceptance-2026-11-12.md).

## Findings at a glance

| Host | Local installation observed | Project MCP config | Instructions and hooks | Current evidence |
|---|---|---|---|---|
| Codex CLI | `codex-cli 0.167.0` | `.codex/config.toml`, trusted projects only; stdio and Streamable HTTP | `AGENTS.md`, `.agents/skills`; hooks include SessionStart/PreToolUse/PostToolUse/Stop | CLI help and docs checked; no model or Daem0n session run |
| agy | Antigravity CLI `1.4.7`; `C:\Users\dasbl\AppData\Local\agy\bin\agy.exe` | `.agents/mcp_config.json`; stdio and Streamable HTTP | `AGENTS.md`, `GEMINI.md`, `.agents/skills`; command hooks include SessionStart/PreToolUse/PostToolUse/Stop | CLI help and docs checked; no model or Daem0n session run |

`agy` is the installed Antigravity CLI, not an assumed alias for another Gemini client. Its local version/help and the first-party [Antigravity v1.4.7 changelog](https://antigravity.google/docs/changelog?tab=hub) support that identity/version. Local versions are workstation snapshots, not support or end-to-end certification.

## Codex CLI

### Native MCP and safe project configuration

Local `codex mcp add --help` showed `codex mcp add <NAME> (--url <URL> | -- <COMMAND>...)`, with stdio `--env`. First-party docs specify stdio `command`, `args`, `env`, `env_vars`, `cwd`; Streamable HTTP `url`, headers and bearer/OAuth auth; and Codex consumption of the MCP initialization `instructions` field. User config is `~/.codex/config.toml`; a project may use `.codex/config.toml` **only after that project is trusted**. The `codex mcp add` command writes user config by default, so project installers should merge the project file and preserve unrelated TOML and entries. [Codex MCP docs](https://developers.openai.com/codex/mcp) [Codex config basics](https://learn.chatgpt.com/docs/config-file/config-basic)

The Daem0n command must use the maintained `module_launch_args("daem0nmcp.server")` helper from `daem0nmcp.claude_hooks.install`, rather than `python -m daem0nmcp` or bare `-m daem0nmcp.server`. That helper generates the safe `-c`/`runpy` bootstrap and removes empty current-directory entries from `sys.path`; this avoids importing a project-shadow package. The Python executable should be the absolute path to the dedicated environment that contains Daem0n. Example shape, with `args` supplied by that helper:

```toml
[mcp_servers.daem0nmcp]
command = "D:/path/to/project/.venv/Scripts/python.exe"
args = ["-E", "-u", "-X", "utf8", "-c", "<safe runpy bootstrap returned by module_launch_args('daem0nmcp.server')>"]
cwd = "D:/path/to/project"
env = { PYTHONUNBUFFERED = "1" }
startup_timeout_sec = 120
tool_timeout_sec = 600
enabled = true
```

The project installer prefixes that helper with `-E -u -X utf8`: Python ignores inherited import settings before startup, writes unbuffered output, and uses UTF-8. Clearing `PYTHONPATH` alone is insufficient for hosts that discard empty environment overrides. On POSIX, set `command` to the venv `bin/python` and `cwd` to the project root. For HTTP, Codex uses `url` instead of `command`/`args`; Daem0n's local endpoint is Streamable HTTP `/mcp`. The 120-second startup and 600-second call limits are conservative policy headroom, not measured Daem0n requirements. Codex's documented defaults are 10 and 60 seconds, respectively; test cold startup before reducing these values. [Codex MCP docs](https://developers.openai.com/codex/mcp) [Daem0n launch helper](../../daem0nmcp/claude_hooks/install.py) [Daem0n server launcher](../../daem0nmcp/api/v7/launcher.py)

Codex supports MCP tool approval values `auto`, `prompt`, `writes`, and `approve`. Do not inject approval/trust settings as an installer default; preserve the user's host policy and let Codex project trust take effect through its normal flow. [Codex MCP docs](https://developers.openai.com/codex/mcp) [Codex config basics](https://learn.chatgpt.com/docs/config-file/config-basic)

### Instructions, skills, hooks, and naming

Codex discovers a global `AGENTS.override.md` or `AGENTS.md` and then project `AGENTS.override.md`/`AGENTS.md` from repo root to current directory. Other filenames are ignored unless `project_doc_fallback_filenames` is configured, so the existing `Summon_Daem0n.md` is not automatically read. The combined default instruction limit is 32 KiB. Codex repo skills live under `.agents/skills/<name>/SKILL.md`, are discoverable by metadata and can be invoked explicitly; a focused project summon skill is a better fit for the long installation ritual than global user configuration. Keep shared `AGENTS.md` guidance concise. [Codex AGENTS docs](https://developers.openai.com/codex/guides/agents-md) [Codex skills docs](https://learn.chatgpt.com/docs/build-skills)

Daem0n v7 already supplies `instructions=server_instructions(...)` at FastMCP initialization (`daem0nmcp/api/v7/fastmcp.py:985,1096`). Those instructions carry the active covenant mode and opaque `workspace_id` guidance. Codex documents that it consumes this server initialization field. The host skill still needs to explain tool discovery and how to use the workspace binding; do not assume native instructions alone create the project binding. [Codex MCP docs](https://developers.openai.com/codex/mcp) [Daem0n server source](../../daem0nmcp/api/v7/fastmcp.py)

Codex hook locations include user `~/.codex/hooks.json`/`config.toml` and project `.codex/hooks.json`/`config.toml`; project hooks load only when the project is trusted. Non-managed hooks require review and trust of the exact hook hash. MCP-tool hooks use an already-connected MCP server; they do not start/reconnect it, and a SessionStart hook may run before the server is ready. Thus a SessionStart MCP call is best-effort and cannot alone guarantee an automatic brief. Hook matchers use Codex names such as `mcp__<server>__<tool>`. [Codex hooks docs](https://developers.openai.com/codex/hooks)

In the installed `codex features list`, `mcp_tool_list_changed` was disabled/development. Treat live tool-list changes as unproven on this version; reconnect/restart after registration or tool-surface changes. Codex CLI supports `codex exec --sandbox read-only|workspace-write|danger-full-access`, `-C`, `--ephemeral`, and `--ignore-user-config`. A disposable acceptance should use its own project config, normal approval, and a restrictive sandbox; do not use approval-bypass flags. `codex login status` is documented as a status-only auth check, but this audit did not run it or inspect credentials. [Codex CLI commands](https://learn.chatgpt.com/docs/developer-commands?surface=cli)

## agy / Antigravity CLI

### Native MCP and safe project configuration

Local `agy mcp add --help` documents stdio as default, `--type stdio|http`, repeated `--env`, repeated HTTP `--header`, and `<name> <commandOrUrl> [args...]`; it has no scope flag. First-party docs state both workspace `.agents/mcp_config.json` and global `~/.gemini/config/mcp_config.json` are loaded. Use the project file for project setup, preserving unrelated JSON; do not use `agy mcp add` unless the installed client's write scope is verified. [Antigravity MCP docs](https://antigravity.google/docs/mcp?tab=cli) [Customization discovery](https://antigravity.google/docs/customization-discovery)

The JSON entry uses `command`, `args`, `env`, `cwd`; pass `args` produced by Daem0n's `module_launch_args("daem0nmcp.server")` helper. Do not use `python -m daem0nmcp`: package shadowing can run the opened project's copy. Example shape:

```json
{
  "mcpServers": {
    "daem0nmcp": {
      "command": "D:/path/to/project/.venv/Scripts/python.exe",
      "args": ["-E", "-u", "-X", "utf8", "-c", "<safe runpy bootstrap returned by module_launch_args('daem0nmcp.server')>"],
      "cwd": "D:/path/to/project",
      "env": { "PYTHONUNBUFFERED": "1" },
      "timeoutSeconds": 600
    }
  }
}
```

Antigravity supports Streamable HTTP via `serverUrl` (or `url`) and `headers`; legacy HTTP+SSE is not supported by the CLI docs. `timeoutSeconds` bounds both connect/tool listing and tool calls; defaults are 300 seconds for connect and 180 seconds per call. A shared 600-second value is policy headroom, not a measured Daem0n requirement. [Antigravity MCP docs](https://antigravity.google/docs/mcp?tab=cli)

### Instructions, dynamic tools, hooks, and permissions

Antigravity CLI loads workspace `AGENTS.md`, `GEMINI.md`, and `.agents/` customizations from the start directory up to repository root; it does not search child folders below the starting workspace. Project skills belong in `.agents/skills/<name>/SKILL.md`. User rules/skills live separately under `~/.gemini/` and `~/.gemini/config/`; avoid those for a project installation. [Antigravity customization discovery](https://antigravity.google/docs/customization-discovery) [Antigravity skills](https://antigravity.google/docs/skills)

Antigravity MCP tools are lazy-loaded by default: schema files and optional server `instructions.md` are cached under its CLI app data, then tools run through `call_mcp_tool` with server/tool/arguments. Marking individual tools eager exposes `mcp_<server>_<tool>` directly and increases prompt cost. For Daem0n's 77-tool inventory, start lazy and have the skill discover tools dynamically. Resource browsing by this CLI is not established by the page: Antigravity docs describe MCP resources as available in the IDE, but the CLI section documents tool discovery/invocation. Treat CLI resource calls as unverified. [Antigravity MCP docs](https://antigravity.google/docs/mcp?tab=cli) [Daem0n inventory](../release/v7/inventory.md)

Project hooks live at `.agents/hooks.json`; global hooks are `~/.gemini/config/hooks.json`. CLI events include SessionStart (before first model call), PreToolUse, PostToolUse, PreInvocation, PostInvocation, and Stop. Hook handlers are shell commands with JSON stdin/stdout. No Daem0n-specific agy hook was exercised. Existing repository acceptance evidence describes the Claude Code and OpenCode hooks as reminders-only; that is not a native edit-approval bridge and does not certify Codex/agy enforcement. [Antigravity hooks](https://antigravity.google/docs/hooks) [Daem0n acceptance evidence](../release/v7/summoning-acceptance-2026-11-12.md)

Keep workspace trust and the host permission preset unchanged. Antigravity's default/request-review mode asks for review as configured and unconfigured MCP tools are Ask. In `agy -p` headless mode there is no interactive approval UI: an approval-required tool can be soft-denied while the process exits zero, so acceptance must check stderr and expected tool/result events rather than exit code alone. Do not use `--dangerously-skip-permissions` or broad Turbo permission as an installer shortcut. [Antigravity permissions](https://antigravity.google/docs/permissions) [Headless mode](https://antigravity.google/docs/cli/headless)

## Acceptance boundary and recommendations

Without a provider/model call, a disposable workspace can verify version/help, project-only config merge and parsing, interpreter resolution, direct server initialize/tool-list/resource-list behavior, and clean shutdown/file diffs. That is not host end-to-end acceptance. Authenticated acceptance should use a disposable project with a fresh workspace identity and exercise actual host discovery, the server instructions, `session_brief`, one idempotent write/outcome/recall, restart persistence, and normal approvals. Capture structured host events, server diagnostics, Daem0n state, and changed files.

Codex's `codex login status` is a documented non-secret status check. Antigravity headless print mode uses cached credentials and returns an auth-required error if none exist, but it sends a model request when already authenticated; this research did not use it as a probe. No client config or auth values were read or written, and no provider request was made. [Codex commands](https://learn.chatgpt.com/docs/developer-commands?surface=cli) [Antigravity headless docs](https://antigravity.google/docs/cli/headless)

Implementation direction: add project-scoped TOML/JSON merges using existing parser conventions; preserve user trust and approval settings; use a shared project skill for the summon workflow; keep `AGENTS.md` short; consume server-provided covenant instructions; and certify tools, resources, edit mediation, and automatic session behavior separately for each host. The installed Codex MCP tool-list-change feature is not enabled, and agy CLI resource behavior remains uncertain.

## Live-testing addendum: account credits and project discovery

Subsequent Windows acceptance passed the core memory workflow and restart for
both clients; see the linked acceptance report for raw-evidence locations,
models, authentication boundaries and remaining scopes. The observations above
describe the earlier research phase.

Antigravity's normal account mode uses the OS keyring shared with its other
local surfaces. Google AI credits can cover overages after included quota is
exhausted: enable **Use AI Credits** in `/config` or `/settings`, inspect balance
with `/credits`, and inspect quota with `/usage`. The corresponding setting is
`useG1Credits: true`. This is a user spending preference, not an MCP installation
requirement. [Official CLI credit guide](https://antigravity.google/docs/cli/credits)

Gemini API-key mode is separate: it requires `modelProvider: "gemini"` together
with `GEMINI_API_KEY`, skips the account session, and sends requests to the Gemini
API. The environment variable alone does not change normal account login.
Removing the provider setting and restarting returns to account authentication.
[Official authentication guide](https://antigravity.google/docs/install/)

The isolated API-mode test returned HTTP 402 `RESOURCE_EXHAUSTED` for depleted
prepayment credits before any Daem0n call. It did not establish anything about
the user's Google AI credit balance. A later disposable-project run used normal
consumer/keyring login and completed real Daem0n calls and restart checks. Only
the private test settings enabled credit overages; no global spending policy
changed, and no actual credit debit was measured. The shared keyring means this
second route's authentication was not isolated from the user's normal login.

On this installed agy build, `agy mcp list` returned no configured servers while
native initialization had cached nine Daem0n tool schemas and instructions;
subsequent native model calls succeeded. The listing is therefore not reliable
negative evidence for project loading in this fixture. The model explicitly
read the project's `AGENTS.md`; covenant use was observed, but automatic
preloading was not independently established.

## Original research evidence boundary

The initial research phase was source-backed integration research plus local
CLI version/help inspection, without provider authentication or execution.
Later installer tests and real model calls are reported separately in the
[acceptance report](../release/v7/multi-cli-acceptance-2026-11-12.md); they do not
retroactively turn documented hooks, resource browsing or UI behavior into
tested features.
