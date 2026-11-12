# OpenCode integration research — 2026-11-12

This is the source-research snapshot before live testing. Subsequent outcomes
are tracked in the [multi-CLI acceptance report](../release/v7/multi-cli-acceptance-2026-11-12.md).

## Version and source boundary

The installed `opencode-ai` executable reports **1.18.21**. Its npm package
manifest agrees. `npm view opencode-ai version` reports **1.19.5** as the current
stable release; no installed client was upgraded. The public documentation's
default route now describes V2, so it must not be copied into a V1 installation.
Sources: installed package manifest and CLI help; [npm registry metadata](https://registry.npmjs.org/opencode-ai/latest),
[V1 docs](https://opencode.ai/v1/docs/mcp-servers/),
[current MCP docs](https://opencode.ai/docs/mcp-servers).

The installed release's first-party source is pinned to
[`v1.18.21`](https://github.com/anomalyco/opencode/tree/v1.18.21).
Downloaded source evidence is retained outside version control under
`.test_tmp/multi-cli-20261112/research/`. Source review is not live acceptance.

## Native MCP and configuration

V1 natively starts stdio servers from `mcp.<name>` with `type: local`, an argv
`command` array, optional `cwd`, `environment`, and numeric timeout. Remote
servers use `type: remote` and `url`; OAuth and headers have separate options.
`enabled` controls startup. Prefer the installed Daem0n interpreter, the existing
safe runpy bootstrap, an explicit project root, and project scope. A slow local
model startup needs an intentional startup allowance rather than the default
connection timeout. [V1 MCP reference](https://opencode.ai/v1/docs/mcp-servers/)

OpenCode accepts JSON/JSONC and combines configuration layers. Project
`opencode.json(c)` and `.opencode/opencode.json(c)` are supported. Preserve
unrelated configuration, comments, permissions, providers and plugins; do not
replace the whole file with a sample. `OPENCODE_CONFIG`, `OPENCODE_CONFIG_DIR`
and inline configuration are additional sources, not a reason to assume the
normal user configuration is absent. [V1 configuration reference](https://opencode.ai/v1/docs/config/)

The pinned implementation creates one SDK client/transport for a server and
retains it in instance state until shutdown/reconfiguration. It inherits the
environment before applying server-specific variables, resolves `cwd` from the
project, and exposes resource and resource-template operations. This is suitable
for Daem0n's session-bound counsel/preflight sequence without a transport shim.
[Pinned MCP implementation](https://github.com/anomalyco/opencode/blob/v1.18.21/packages/opencode/src/mcp/index.ts)

The normal V1 MCP execution wrapper supplies the raw result to
`tool.execute.after` before rendering content for the model. Any receipt-related
adapter must inspect that observed result, never parse the model's explanation
as proof. Daem0n's structured errors and exact remedies must remain intact.
[Pinned tool execution](https://github.com/anomalyco/opencode/blob/v1.18.21/packages/opencode/src/session/tools.ts#L390-L428)

## Instructions and lifecycle integration

Project `AGENTS.md` is the native instruction surface; `CLAUDE.md` is a fallback
when the native file is absent. Global instruction files have their own
precedence. Add an owned covenant section while preserving existing project
instructions, and use the tool names actually discovered by the client.
[V1 rules](https://opencode.ai/v1/docs/rules/)

V1 local plugins load from `.opencode/plugins/` and can use system-prompt,
before/after-tool and session lifecycle hooks. Their exports are plugin
functions returning hook objects. Local package dependencies belong in the
configuration directory's package manifest. The existing Daem0n V1 plugin is a
thin Python-hook wrapper; lifecycle translation should remain host-specific.
[V1 plugins](https://opencode.ai/v1/docs/plugins/)

Current Daem0n native-edit hooks are reminders. The historical P6 acceptance
report describes older blocking behavior; it is not proof of current policy.
See `daem0nmcp/claude_hooks/pre_edit.py`, the ordinary event path in
`pre_bash.py`, and `tests/native_edit_host.py`. Full core memory functionality
does not require restoring the old edit bridge.

## V2 is a separate compatibility target

V2 places servers under `mcp.servers`, replaces `enabled` with `disabled`, and
defaults to Code Mode. `codemode: false` exposes tools directly. Keep Daem0n's
connection unpooled and project-specific; public V2 supports connection sharing
across directories. Its resource operations are explicit built-in tools.
These are documented V2 features, not claims about the installed V1 binary.
[V2 MCP reference](https://opencode.ai/docs/mcp-servers)

V2 plugins use `Plugin.define` and `setup`, with tool lifecycle registration
through `ctx.tool.hook`. A dual-version object export requires V1 **1.18.29** or
later; the installed **1.18.21** can still expect function exports. Thus replacing
the current V1 function with the new object would break the version under test.
Retain a compatible V1 entry point and validate any V2 entry point separately.
[Current plugin API and V1 compatibility](https://opencode.ai/docs/build/plugins/server#support-v1)

## Existing installer gaps

Code inspection found two concrete integration problems in
`daem0nmcp/opencode_install.py`:

- Preserving an existing config with no Daem0n entry can leave no MCP binding,
  while installation still reports success. A safe owned-entry merge or an
  explicit incomplete result is required.
- The legacy installer provisions edit-bridge pairing by default. That is not a
  prerequisite for the current reminder hooks and should not be silently added
  to a new multi-client setup path.

Preserve the existing symlink/ancestor checks, fixed interpreter selection and
exclusive new-file creation. Existing JSONC needs preservation-aware editing or
a clearly diagnosed unsupported merge; `--force` is not an automatic-install
strategy. These are repository findings, not upstream OpenCode defects.

## Disposable acceptance plan

The installed CLI supports `run --format json --dir <project>` and explicit
`--model` selection. `mcp list` can check connectivity without a model turn.
Provider login state and MCP OAuth state are distinct. `auth list` currently
shows seven configured providers without exposing credentials; this establishes
presence only, not that any token is valid. The previous OpenCode acceptance
stopped at a provider refresh 401, so it must be retested rather than inherited.
[V1 CLI and environment reference](https://opencode.ai/v1/docs/cli/)

Planned gates, in order:

1. An isolated project/home/config/data/cache, pinned executable, disabled update
   checks, unrelated configuration and instruction sentinels, and private auth
   reuse only where supported. Record original-user hashes and never print keys.
2. Fresh install, second identical install, malformed/linked-file refusal,
   preservation of pre-existing settings, and actual MCP connection/tool listing.
3. A real model turn: full brief, health, guided write/remedy, matching recall,
   outcome, gateway discovery/call, and a separate hook event check.
4. Restart the real host; recall persisted data and replay the same write key
   without duplicating the record. Verify provider readiness before counting
   semantic retrieval as passed.
5. Report native MCP, reminder hooks, resource access, provider authentication,
   and live model use as separate results. No global installation, provider,
   permission or client-version changes are needed for these checks.

The last section is the proposed verification method. No new OpenCode model
acceptance is claimed by this research note.
