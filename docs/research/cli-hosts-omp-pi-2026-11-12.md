# OMP and Pi support research for Daem0n v7

This is the source-research snapshot before live testing. Subsequent outcomes
are tracked in the [multi-CLI acceptance report](../release/v7/multi-cli-acceptance-2026-11-12.md).

**Snapshot:** 2026-11-12

**Purpose:** Establish the smallest trustworthy installation route for Daem0n in Oh My Pi (`omp`) and Pi, and identify what a native MCP entry does and does not provide.

**Evidence:** Installed binary/version and help output, the installed Pi package documentation, and each project's official repository documentation. No user client configuration was read or changed. No account credentials, model calls, or full host acceptance were used.

## Installed clients are two products

| Shell command | Installed artifact and version | Confirmed from this environment |
|---|---|---|
| `omp` | Oh My Pi, `omp/18.16.0`; standalone Windows executable at `C:\Users\dasbl\AppData\Local\omp\omp.exe` | `omp --version` and `omp --help`. Its help exposes extension/plugin loading, profiles, project cwd selection, print/json/RPC modes, session suppression, and approval-mode controls. |
| `pi` | `@earendil-works/pi-coding-agent` **1.0.0**, `pi` binary in the package at `C:\Users\dasbl\AppData\Roaming\npm\node_modules\@earendil-works\pi-coding-agent`; repository metadata is `https://github.com/earendil-works/pi.git` | The PowerShell launcher invokes that package's `dist/bundle/cli.js`; the installed `package.json` and `pi --help` confirm the version. |

**Treat them as distinct adapters.** The installed `pi` is the upstream Earendil Works package. Installed `omp` is Oh My Pi, a separate product/fork with its own versioned executable, settings root, MCP discovery, profile model, extension API and tool runtime. Pi paths such as `.pi/mcp.json` must not be written for OMP, nor OMP's `.omp/mcp.json` for Pi. The user also has `pstack-pi@0.14.3-pi.1` in OMP's plugin list; that is a third-party plugin, not evidence that OMP is upstream Pi.

## Direct MCP installation

The installer prefixes the maintained bootstrap with `-E -u -X utf8`. In
particular, OMP 18.16.0 discards empty environment values, so an empty
`PYTHONPATH` override cannot reliably clear an inherited value. `-E` makes
Python ignore import environment settings before loading startup code. This
applies to both the MCP server and the reported workspace-ID command.
[Pinned OMP environment policy test](https://github.com/can1357/oh-my-pi/blob/v18.16.0/packages/coding-agent/test/mcp-env-policy.test.ts)

Both clients can start MCP stdio servers and document remote HTTP MCP transports. For Daem0n's local installation, use the project's dedicated Python interpreter as the executable and generate its interpreter arguments with the maintained `daem0nmcp.claude_hooks.install.module_launch_args("daem0nmcp.server")` helper. The helper returns `-c` plus a `runpy` launcher that removes empty current-directory entries from `sys.path` before loading the module. This prevents a project-local package from shadowing the installed Daem0n package; plain `python -m daem0nmcp.server` does not provide that protection. Use an absolute interpreter path and explicit project `cwd`. Do not put shell syntax or secrets in `command`; neither host's MCP `command` field is a shell script.

### OMP

The preferred project file is `<project>/.omp/mcp.json`. A user server normally belongs in `~/.omp/agent/mcp.json`; named profile `work` instead uses `~/.omp/profiles/work/agent/mcp.json`. `OMP_PROFILE` (or legacy `PI_PROFILE`) selects that profile. `PI_CODING_AGENT_DIR` relocates the default profile's agent directory but is ignored for named profiles. Project `.omp/mcp.json` is independent of profiles and therefore applies to all of them. These paths and precedence are documented in the [official OMP MCP configuration guide](https://github.com/can1357/oh-my-pi/blob/main/docs/mcp-config.md).

Illustrative installer inputs (pseudocode, **not** a valid JSON fragment):

```text
command = <absolute path to the project's venv Python>
args = ["-E", "-u", "-X", "utf8", *module_launch_args("daem0nmcp.server")]
cwd = <absolute project root>
```

Use `.venv/bin/python` on macOS/Linux. `args` and `cwd` are part of the same server object. OMP's MCP `timeout` is in **milliseconds** (`120_000` is 120 seconds); Pi's corresponding timeout is in seconds. Add the `mcpServers.daem0nmcp` entry while preserving other JSON members and unrelated server entries. Inspect `/mcp list` for the chosen server and its source; `/mcp test daem0nmcp` tests a single server; `/mcp resources`, `/mcp prompts`, and `/mcp notifications` inspect other MCP capabilities. A profile isolates user servers, but does not isolate project `.omp` configuration.

OMP also discovers MCP entries in other products' configs, including Claude Code, Codex, OpenCode, Cursor, VS Code, and root-level fallback `mcp.json` / `.mcp.json`. In this environment it would be unsafe to assume that a model tool source is OMP's own file or edit an imported file. Prefer the OMP-owned `.omp/mcp.json`, use a unique server name, inspect the resolved source, and fail clearly on a conflicting definition. OMP's cross-provider rules use source priority and first-definition wins; they do not deep-merge duplicate MCP definitions except for OpenCode's own config layers. (Official [MCP discovery and precedence](https://github.com/can1357/oh-my-pi/blob/main/docs/mcp-config.md#discovery-and-precedence).)

OMP's project settings directory is `<project>/.omp`; project context and extension discovery follow their own providers. **Current core parity does not require a host lifecycle adapter or startup MCP call:** the installed managed instructions can guide the agent to its first Daem0n call, and a first guided `memory_recall` call returns the compact brief in `meta.covenant.auto_brief` when the session has not yet been briefed. OMP supports TypeScript/JavaScript [extensions](https://github.com/can1357/oh-my-pi/blob/main/docs/extensions.md), `session_start` and `before_agent_start` handlers, pre/post tool events, plugin installation, and JSON/RPC/print launch modes. Its `/mcp` integrations connect servers and expose their tools, resources, prompts and notifications. The installed `omp --help` advertises `--profile`, `--cwd`, `--mode=json|rpc|rpc-ui`, `--no-ui`, `--no-session`, and `--approval-mode`. A proactive host reminder is an optional enhancement and should be tested separately from the core MCP flow.

An older upstream issue, [#2194](https://github.com/can1357/oh-my-pi/issues/2194), is a historical signal about calling an already-connected MCP tool from extension lifecycle code. It does **not** establish behavior for the installed `omp/18.16.0`; lifecycle-to-MCP invocation was not tested against that binary. Treat this as an open research question only if implementing optional proactive startup behavior. Native MCP plus managed instructions meet the current baseline without resolving it. If a future adapter is built, it must keep one persistent Daem0n MCP client/session for lifecycle and model calls; a separate brief-only client would create a different server-issued session.

### Pi

Pi reads user MCP servers from `<PI_CODING_AGENT_DIR>/mcp.json` (default `~/.pi/agent/mcp.json`) and project MCP servers from `<project>/.pi/mcp.json`. The CLI setting `PI_CODING_AGENT_DIR` relocates the user config, sessions and other agent data. Pi project's MCP config and extensions are loaded **only after project trust is granted**; this is a deliberate code-loading boundary. If the install writes `.pi/mcp.json` or `.pi/extensions`, let the host request that trust and wait for the user to approve. Do not silently set `-a/--approve` or work around a declined decision. Source: installed `docs/configuration.md`, `docs/mcp.md`, and `docs/security.md`; upstream [configuration](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/configuration.md), [MCP](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/mcp.md), and [security](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/security.md) references.

Illustrative installer inputs (pseudocode, **not** a valid JSON fragment):

```text
command = <absolute path to the project's venv Python>
args = ["-E", "-u", "-X", "utf8", *module_launch_args("daem0nmcp.server")]
cwd = <absolute project root>
```

Pi's MCP timeout is in **seconds**. `command` is one executable, never a shell command; pass flags in `args`. Relative `cwd` resolves against Pi's session directory. An existing project definition with the same name replaces a user definition. Its CLI provides `pi mcp add`, `pi mcp remove`, and `pi mcp list`; the project variants take `--local`. For an installer, a structure-aware JSON member merge is safer than replacing either settings file, and no Pi CLI list command should be run against the user's existing config during a disposable acceptance test.

Pi loads `AGENTS.md` and `CLAUDE.md` context files separately from its `.pi` files. Project `.pi/APPEND_SYSTEM.md` appends to the normal system prompt, while `.pi/SYSTEM.md` replaces it. Both `.pi/mcp.json` and `.pi/extensions` require trust. Preserve the user's existing `AGENTS.md` and Pi prompt files; add only a clearly delimited owned section or a standalone extension. A project prompt file alone is guidance, not a host hook.

Pi's first-party client supports stdio and Streamable HTTP. It exposes standard MCP resource list/template/read tools when connected servers implement resources. MCP tool names use `mcp__<server>__<tool>`; other `codemode`/`tool_search` exposure may make tools indirect or initially absent from the model's direct tool list. The [MCP result documentation](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/mcp.md) states codemode scripts get the full `CallToolResult` (`content`, `structuredContent`, `isError`). Pi extensions can observe session startup/shutdown and `before_agent_start`, register model-callable tools, and intercept tool calls/results ([extension guide](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md)).

**Current core parity does not require a startup lifecycle adapter:** native persistent MCP plus managed project instructions is sufficient, because guided `memory_recall` can brief an unbriefed session and return `meta.covenant.auto_brief`. Pi's `.pi/APPEND_SYSTEM.md` or a managed `AGENTS.md` section can explain that behavior and the v7 workflow. A proactive lifecycle reminder is optional and needs an independent host-version acceptance check. Whether an extension lifecycle event can invoke a tool from Pi's native MCP manager was not established by the installed docs reviewed here; do not infer a limitation from the absence of a documented example. If a future adapter is built, use one persistent MCP client for the host session and its calls; a separate brief-only client would create a distinct server-issued session.

## Current core baseline and optional host automation

A native MCP entry plus managed instructions is the current integration baseline. Guided `memory_recall`, `memory_preflight`, `memory_store`, and `memory_record_outcome` can automatically brief an unbriefed session, returning `meta.covenant.auto_brief`. Diagnostic and discovery calls do not trigger briefing; explicit `session_brief` remains available for the full brief. SessionStart hooks in the currently maintained Claude/OpenCode integration only remind the agent to make/use the MCP call. The native edit hooks are reminders too. The optional legacy edit bridge is not required for core v7 functionality.

For the baseline:

- Preserve Daem0n's opaque `workspace_id` as obtained from its workspace flow; never invent one from `cwd`.
- Keep the host's native MCP connection persistent across calls and retries. Do not spawn a fresh stdio server or reinitialize a client per tool call: counsel challenges are session-bound and preflight remediation must replay the exact argument set with the exact remedy.
- Preserve complete structured `CallToolResult` data, including `error.counsel`, `error.remedy`, and `meta`; do not turn a result into prose or retry writes with guessed arguments. Pi documents full MCP result access through codemode. Verify the exact result surface exposed by OMP 18.16.0 during host acceptance.
- Validate host-visible MCP names and v7 discovery. The default core surface is small; use documented gateway/search tools when the AI needs tools beyond the core listing. Exercise a gateway response carrying counsel as well as a normal read.
- Managed instructions should explain automatic briefing on guided memory calls, protected-write counsel/remedy retry, stable idempotency keys, and outcome recording. Host prompts are guidance, not proof that a call occurred.

Optional enhancements, each with separate acceptance evidence:

- A proactive session-start reminder or startup MCP call. The startup call must be delivered through the same persistent server-issued MCP session used by model-visible tools; don't create a brief-only connection. Verify trigger count and new/resumed-session behavior.
- Native edit interception or receipts. These are not part of current core parity; any implementation must derive evidence from host-observed structured tool calls/results, never model narration. The existing native edit hooks only remind. Don't claim a file edit or receipt capability without testing host approvals, skipped/failed edits, and exact call identity.
- Adapter-managed lifecycle/reconnect handling. If a future adapter is built, verify reset/restore on new session, resume/switch, reload, shutdown and interrupted startup, while preserving the same MCP session for retries within a host session.

## Minimum host acceptance plan

Run each host independently in a disposable copy/worktree and separate profile/config root. The disposable tree should contain a synthetic pre-existing unrelated MCP server, custom instructions/settings, and a nested hook sibling. Record the installed client/version and snapshot hashes of the isolated config before and after; never use the user's normal profile, modify their actual host config, or print provider credentials. A provider-backed turn is required for host workflow acceptance; report it as unrun when no host provider authentication is available.

1. Confirm installer detection, OS-specific interpreter command/args/cwd, idempotent insertion, preservation of unrelated JSON/settings/instructions, safe secrets handling and uninstall scoped to only Daem0n-owned entries.
2. Start the real client from the project root and honor its actual trust/approval request. Confirm the server's stdio connection remains alive across several calls. Assert version, tool/resource listing and host-returned structured content.
3. Verify that the first guided `memory_recall` in an unbriefed session returns `meta.covenant.auto_brief` to the host and that managed instructions tell the agent how to use it; call `session_brief` only when full briefing is desired. Confirm the same native MCP connection remains active across subsequent calls. Separately, if a proactive reminder/startup call is implemented, verify its once-per-session behavior and ensure it uses that same connection.
4. Exercise `memory_recall`, `memory_preflight`, then a protected store returning `COUNSEL_REQUIRED`; prove the host surfaces the original fields and the allowed retry sends precisely `error.remedy` in the *same* MCP session. Check no duplicate write on restart/idempotent replay.
5. Core acceptance does not require native edit hooks or edit receipts. Verify that instructions accurately describe the host's edit behavior (including that current hooks are reminders). If optional edit interception/receipt capture is claimed, separately test an actual edit, denied approval, skipped edit, failed result, duplicate tools in a batch, and ensure any receipt comes from the host-observed structured result and exact call identity.
6. Restart the host against the same isolated project, retrieve the original record through semantic search, and repeat install to prove hashes and sentinels stay unchanged. Run offline package/store verification after shutdown.
7. Separately check JSON/RPC/headless execution, cancellation, lost/reconnected stdio, trust refusal, malformed config, duplicate imported server name, stale project root, fresh-store cold start, existing-version migration and platform interpreter resolution.

For the two installed clients here, only version/help/docs and package paths were inspected. No client config was opened, no model request or native edit was made, and none of the end-to-end host cases above are certified. The earlier Claude Code disposable acceptance in this project is not transferable to Pi or OMP.

## First-party source index

**Oh My Pi (OMP)**

- [OMP 18.16.0 MCP configuration, discovery, source priority, validation, timeout units and diagnostics](https://github.com/can1357/oh-my-pi/blob/v18.16.0/docs/mcp-config.md); the pinned [MCP server type](https://github.com/can1357/oh-my-pi/blob/v18.16.0/packages/coding-agent/src/mcp/types.ts#L70) defines `timeout` in milliseconds.
- [Official MCP runtime connection/readiness lifecycle](https://github.com/can1357/oh-my-pi/blob/main/docs/mcp-runtime-lifecycle.md)
- [Official extension and tool interception API](https://github.com/can1357/oh-my-pi/blob/main/docs/extensions.md)
- [Official session and tool hook events](https://github.com/can1357/oh-my-pi/blob/main/docs/hooks.md)
- [Official profile/config/environment variables reference](https://github.com/can1357/oh-my-pi/blob/main/docs/environment-variables.md)
- [Historical upstream issue #2194 about invoking MCP tools from extension lifecycle code; not verified against installed OMP 18.16.0](https://github.com/can1357/oh-my-pi/issues/2194)

**Pi (`@earendil-works/pi-coding-agent`)**

- [Official MCP server, exposure, result, resource and authorization behavior](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/mcp.md)
- [Official project/user config and system/context-file precedence](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/configuration.md)
- [Official extension lifecycle and tool API](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md)
- [Official project-trust and execution limits](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/security.md)
- [Official CLI print/JSON/RPC/session flags](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/cli.md)
