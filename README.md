# Daem0nMCP

```
        ,     ,
       /(     )\
      |  \   /  |      "I am Daem0n, keeper of memories,
       \  \ /  /        guardian of decisions past..."
        \  Y  /
         \ | /
          \|/
           *
```

**AI Memory & Decision System** - Give AI agents persistent memory and consistent decision-making with *actual* semantic understanding.

## v7 Protocol Cutover

The public v7 MCP surface is guided and workspace-scoped:

1. Guided mode automatically briefs an unbriefed session on `memory_recall`, `memory_preflight`, `memory_store`, or `memory_record_outcome`, returning `meta.covenant.auto_brief`. Diagnostic and discovery calls do not trigger briefing. Call `session_brief` for the full brief.
2. Call `memory_store` (or any protected tool) directly. If it returns `COUNSEL_REQUIRED`, read `error.counsel` (guidance and reasons), then retry exactly `error.remedy`. `memory_preflight` remains available for planning a change in advance.
3. Use `daem0n_tools_search(query)`, then `daem0n_tool_call(workspace_id, tool, arguments)`.

The default listing contains nine tools; all 77 remain registered and callable.
Set `DAEM0NMCP_TOOL_SURFACE=full` to list every tool, or
`DAEM0NMCP_COVENANT_MODE=strict` to require explicit briefing and preflight.
Tokens remain exact-argument bound, single-use, and valid for 300 seconds.
Destructive operations always require a counsel challenge. Linked workspaces
still require their own explicit `session_brief`; task submission stays strict.
Run long-running targets directly as MCP tasks, using the full listing if
needed; the gateway does not submit tasks. Use `system_health` for diagnostics.

The server states this protocol itself: the MCP initialize result carries
`instructions` generated for the active covenant mode and tool listing.

Each scoped call uses an opaque `workspace_id`. The supported transports are
stdio and Streamable HTTP at `/mcp`. Read-only warnings, failures, rules, and
active context use the `memory://workspaces/{workspace_id}/...` resources. See
[`docs/v6-to-v7-tools.json`](docs/v6-to-v7-tools.json) for the generated
migration mapping.

## v7 setup and operations

v7 is not published yet. Until release, install from a source checkout or a
wheel you built from this checkout:

```bash
pip install .
# or: python -m build && pip install dist/daem0nmcp-7.0.0.dev0-py3-none-any.whl
```

After v7 is published, `pip install daem0nmcp` will install the small core.
Add only the feature profiles the deployment needs: `tasks`, `local`, `graph`,
`apps`, `models-local`, `late-interaction`, `models-hosted`, `agency-e2b`, or `observability`.
For example, `pip install "daem0nmcp[apps,graph]"`. The development and
compatibility profiles (`dev` and `tracing`) are not required by a normal MCP
server. `models-local` and `late-interaction` are optional and require Python
3.11 or newer. Their secure ONNX runtime dependency has no CPython 3.10 wheel.
On Python 3.10, the resolver omits these profiles' dependencies and the runtime
reports Python-version remediation while core installation remains supported.

The server owns workspace registration. Set `DAEM0NMCP_PROJECT_ROOT` to the
default workspace and, when needed, `DAEM0NMCP_WORKSPACE_ROOTS` to the allowed
additional roots. Operators obtain the stable opaque ID from the local CLI
and provide it in the protected client configuration or pairing flow;
`session_brief` then confirms that ID. Use the local `workspace-id` CLI to inspect
the project's recorded identity; do not recalculate an ID after moving an existing
workspace. Clients do not choose filesystem paths. Leave
`DAEM0NMCP_STORAGE_PATH` unset when serving multiple registered workspaces.

A workspace ID is an identifier, not a secret. New IDs derive from the canonical
root path, while a moved workspace can retain its recorded ID. Read the current
identity with the CLI rather than inferring it from the current path.
"Opaque" means only that it is not a path and carries no meaning for clients.
Access is decided by the authenticated principal and its grants, never by
knowledge of the ID.

For authenticated remote principals, set `DAEM0NMCP_WORKSPACE_ACCESS_FILE` to
the server-managed grants file. If unset, the server uses
`<managed-storage>/v7-workspace-access.json`. The file and its parent directory
must be owner-only. Its strict JSON shape is
`{"schema_version":1,"grants":{"oauth-sub:<subject>":["ws_<opaque>"]}}`;
the server rereads it at authorization points, so removing a grant takes effect
without a restart. The CLI has no grant-editing command: configure the file and
environment before launching `python -m daem0nmcp.server`.

Start the stdio server with `python -m daem0nmcp.server`, or use
`python -m daem0nmcp.server --transport streamable-http --host 127.0.0.1 --port
9876` for loopback HTTP. A remote HTTP deployment requires configured transport
authentication and security policy; it is not a redirect-based setup.

Loopback HTTP without `FASTMCP_SERVER_AUTH` does not authenticate callers: every
process and user that can reach the port on that host (other accounts,
containers, WSL) acts as the server's own principal on every registered
workspace. On a shared machine use stdio or configure authentication. A
loopback listener behind a reverse proxy is not local: when
`DAEM0NMCP_TRUSTED_PROXY_IPS` is set or `DAEM0NMCP_ALLOWED_HOSTS` names a host
other than `localhost` or a loopback address, the server refuses to start with
`PROXIED_LISTENER_REQUIRES_AUTH` unless a production token verifier is
configured. That check sees a proxy only through those two settings. A proxy
that forwards the loopback authority as `Host` (for example nginx without
`proxy_set_header Host $host`) needs neither, is not detected, and exposes the
unauthenticated listener to its clients, so configure authentication for every
proxied deployment.

`python -m` resolves `daem0nmcp` from the current directory first, so type these
commands (and the `python -m daem0nmcp.cli` ones) from a directory you trust.
Commands that a host launches for you inside a project never use `-m`: the
installers and the [registration command](#stdio-all-supported-platforms) drop
the current directory from the import path first.

The complete setup, exact preflight examples, migrations, native client
installers, diagnostics, and all **77** registered v7 tools are in
[the v7 operations guide](docs/v7-operations.md). The release ledger remains
in progress; these instructions do not claim a published v7 release.

## Historical v6 Migration Context: v6.6.6

### ModernBERT Deep Sight (BREAKING)
The daemon's vision has been fundamentally sharpened. The old `all-MiniLM-L6-v2` embedding model is replaced by **ModernBERT** with asymmetric query/document encoding and optional ONNX acceleration.

| Aspect | Old (v5.x) | New (v6.6.6) |
|--------|------------|--------------|
| **Model** | `all-MiniLM-L6-v2` | `nomic-ai/modernbert-embed-base` |
| **Dimensions** | 384 | 256 (Matryoshka truncation) |
| **Encoding** | Single `encode()` | Dual: `encode_query()` / `encode_document()` |
| **Backend** | PyTorch only | ONNX quantized (with torch fallback) |
| **Prefixes** | None | `search_query: ` / `search_document: ` |

**This is a BREAKING CHANGE for format 6** — existing format-6 embeddings must be re-encoded with the deprecated legacy command:
```bash
python -m daem0nmcp.migrations.migrate_embedding_model --project-path /path/to/.daem0nmcp
```

Architecture format 7 does not write canonical `memories.vector_embedding` data.
Rebuild its dense retrieval projection instead:

```bash
python -m daem0nmcp.cli rebuild-projection --projection dense --workspace-id <workspace-id>
```

### Background Dreaming
When the user goes idle, the daemon autonomously re-evaluates past failed decisions using current evidence:
- `IdleDreamScheduler` monitors tool call activity
- After configurable idle timeout (default 60s), `FailedDecisionReview` strategy runs
- Classifies decisions as **revised**, **confirmed_failure**, or **needs_more_data**
- Insights persisted as `learning` memories with `dream` tag and full provenance
- Yields immediately when user returns (cooperative scheduling)

### Cognitive Tools (3 new standalone MCP tools)
Meta-reasoning tools for daemon introspection:

| Tool | Purpose |
|------|---------|
| `simulate_decision` | **Temporal Scrying** — replay a past decision with current knowledge, revealing what changed |
| `evolve_rule` | **Rule Entropy Analysis** — examine rules for staleness, code drift, and outcome correlation |
| `debate_internal` | **Adversarial Council** — structured evidence-grounded debate with convergence detection |

### Auto-Zoom Retrieval Routing
Query-aware search dispatch that routes to the optimal retrieval strategy:
- **SIMPLE** queries → Vector-only search (fast path)
- **MEDIUM** queries → Hybrid BM25+vector with RRF fusion
- **COMPLEX** queries → GraphRAG multi-hop traversal + community summaries
- Shadow mode (default) logs classifications without changing behavior
- All strategies fall back to hybrid on failure

### Claude Code Native Hooks
New `daem0nmcp/claude_hooks/` module with 5 lifecycle hooks and automated installation:
```bash
python -m daem0nmcp.cli install-claude-hooks      # Install to ~/.claude/settings.json
python -m daem0nmcp.cli uninstall-claude-hooks     # Remove
```

| Hook | Purpose |
|------|---------|
| `session_start` | Explain automatic briefing and optional full `session_brief` |
| `pre_edit` | Remind agents to recall file guidance through `daem0n_tool_call` |
| `pre_bash` | Rule enforcement on bash commands |
| `post_edit` | Suggest replay-safe memory calls for significant changes |
| `stop` | Suggest memory and outcome calls without writing directly |

### Stats
- **8 workflow tools** + **3 cognitive tools** (11 MCP tools total)
- **59 workflow actions** across 8 workflows
- **500+ tests** passing

---

## Historical v6 Migration Context: v5.1.0

### Workflow Consolidation
v5.1 consolidates **67 individual MCP tools into 8 workflow-oriented tools**, dramatically reducing context overhead for AI agents while preserving all capabilities.

#### 8 Workflow Tools

| Workflow | Purpose | Actions |
|----------|---------|---------|
| **`commune`** | Session start & status | `briefing`, `active_context`, `triggers`, `health`, `covenant`, `updates` |
| **`consult`** | Pre-action intelligence | `preflight`, `recall`, `recall_file`, `recall_entity`, `recall_hierarchical`, `search`, `check_rules`, `compress` |
| **`inscribe`** | Memory writing & linking | `remember`, `remember_batch`, `link`, `unlink`, `pin`, `activate`, `deactivate`, `clear_active`, `ingest` |
| **`reflect`** | Outcomes & verification | `outcome`, `verify`, `execute` |
| **`understand`** | Code comprehension | `index`, `find`, `impact`, `todos`, `refactor` |
| **`govern`** | Rules & triggers | `add_rule`, `update_rule`, `list_rules`, `add_trigger`, `list_triggers`, `remove_trigger` |
| **`explore`** | Graph & discovery | `related`, `chain`, `graph`, `stats`, `communities`, `community_detail`, `rebuild_communities`, `entities`, `backfill_entities`, `evolution`, `versions`, `at_time` |
| **`maintain`** | Housekeeping & federation | `prune`, `archive`, `cleanup`, `compact`, `rebuild_index`, `export`, `import_data`, `link_project`, `unlink_project`, `list_projects`, `consolidate` |

#### How It Worked

Format 6 grouped individual operations behind an `action` discriminator. Those
calls are not executable v7 guidance. Use the generated
[`docs/v6-to-v7-tools.json`](docs/v6-to-v7-tools.json) mapping for the exact
replacement of every old operation.

#### Why Consolidate?
- **88% fewer tool definitions** in context (8 vs 67)
- **Lower token overhead** — AI agents load fewer tool schemas
- **Logical grouping** — related operations live in one tool
- **Backward compatible** — legacy individual functions remain importable for internal dispatch, but only the consolidated workflow and cognitive tools are exposed to MCP clients

---

## Historical v6 Migration Context: v5.0.0

### Visions of the Void (MCP Apps)
This v6 release added interactive HTML interfaces via MCP Apps (SEP-1865).
Those action-dispatch examples are intentionally omitted because they are not
valid v7 calls; use the generated migration mapping for current tool names.

Features: D3.js v7 bundled (105KB, no CDN), restrictive CSP, canvas-based graph (10,000+ nodes at 60fps), graceful text fallback for non-visual hosts.

---

## What's New in v4.0.0

### Cognitive Architecture
Five major capabilities:

- **GraphRAG & Leiden Communities**: Knowledge graph construction with hierarchical community detection, multi-hop queries, global search via community summaries
- **Bi-Temporal Knowledge**: Dual timestamps (`valid_time` vs `transaction_time`), `happened_at` backfilling, `as_of_time` point-in-time queries, contradiction detection
- **Metacognitive Reflexion**: Actor-Evaluator-Reflector loop, `verify` action validates claims against stored knowledge, reflection persistence
- **Context Engineering**: LLMLingua-2 for 3x-6x compression, code entity preservation, adaptive rates, `compress` action for on-demand optimization
- **Dynamic Agency**: `execute` action for sandboxed Python execution via E2B Firecracker microVMs

---

## What's New in v3.1.0

### 2026 AI Memory Research Enhancements
- **BM25 + RRF Hybrid Retrieval**: Okapi BM25 replaces TF-IDF, Reciprocal Rank Fusion combines keyword and vector search
- **TiMem-Style Recall Planner**: Complexity-aware retrieval adapting to query difficulty (simple/medium/complex)
- **Titans-Inspired Surprise Scoring**: Novelty detection with `surprise_score` (0.0-1.0)
- **Importance-Weighted Learning**: EWC-inspired protection for valuable memories via `importance_score`
- **Fact Model**: Verified facts promote to immutable O(1) lookup after threshold successful outcomes

---

## What's New in v3.0.0

### FastMCP 3.0 Upgrade
- **CovenantMiddleware**: Sacred Covenant enforcement via FastMCP 3.0 middleware pattern
- **Component Versioning**: All tools include version metadata
- **OpenTelemetry Tracing** (Optional): `pip install daem0nmcp[tracing]`

---

## Previous Versions

<details>
<summary>v2.16.0 — Sacred Covenant Enforcement</summary>

- Tools block with `COMMUNION_REQUIRED`/`COUNSEL_REQUIRED` until proper rituals observed
- Preflight tokens with 5-minute validity
- MCP Resources for dynamic context injection (the retired URI forms are
  mapped to v7 in `docs/v6-to-v7-tools.json`)
</details>

<details>
<summary>v2.14.0–v2.15.0 — Active Context, Temporal Versioning, Entities</summary>

- Active Working Context (MemGPT-style always-hot memories, max 10)
- Temporal versioning with `versions` and `at_time` queries
- Hierarchical summarization via Leiden communities
- Auto entity extraction from memory content
- Contextual recall triggers (auto-recall on file/tag/entity patterns)
- Configurable hybrid search weight, result diversity, tag inference
- Qualified entity names (`module.Class.method`), incremental indexing
</details>

<details>
<summary>v2.11.0–v2.13.0 — Passive Capture, Linked Projects</summary>

- Passive Capture hooks (auto-remember decisions from conversation)
- Endless Mode (condensed recall with 50-75% token reduction)
- Linked Projects for cross-repo memory awareness
</details>

<details>
<summary>v2.7.0–v2.10.0 — Code Understanding, Enforcement, File Watcher</summary>

- Multi-language AST parsing via tree-sitter (Python, TypeScript, JavaScript, Go, Rust, Java, C, C++, C#, Ruby, PHP)
- Qdrant vector backend for persistent vector storage
- Proactive file watcher with desktop/log/editor-poll notifications
- Pre-commit enforcement hooks blocking commits with stale decisions
- CLI tools for status, record-outcome, install-hooks
</details>

<details>
<summary>Core Features (v2.1+)</summary>

- TF-IDF semantic search with real similarity matching
- Memory decay (30-day half-life for decisions/learnings, eternal patterns/warnings)
- Conflict detection and failed decision boosting (1.5x relevance)
- File-level memory associations
- Vector embeddings for enhanced semantic matching
</details>

## Why Daem0nMCP?

AI agents start each session fresh. They don't remember:
- What decisions were made and why
- Patterns that should be followed
- Warnings from past mistakes

**Markdown files don't solve this** - the AI has to know to read them and might ignore them.

**Daem0nMCP provides ACTIVE memory** - it surfaces relevant context when the AI asks about a topic, enforces rules before actions, and learns from outcomes.

### What Makes This Different

- **Semantic matching**: "creating REST endpoint" matches rules about "adding API route"
- **Time decay**: A decision from yesterday matters more than one from 6 months ago
- **Conflict warnings**: "You tried this approach before and it failed"
- **Learning loops**: Record outcomes, and failures get boosted in future recalls
- **Surprise scoring**: Novel information surfaces above routine knowledge
- **Graph reasoning**: Multi-hop traversal across linked memories and communities
- **Background dreaming**: Idle-time re-evaluation of past failed decisions

## Quick Start

### Let your CLI perform the ritual

1. Copy [Summon_Daem0n.md](Summon_Daem0n.md) into the project you want Daem0n to remember.
2. Open your selected AI CLI in that project and say:

   > Read Summon_Daem0n.md and perform the complete summoning ritual for this project.

3. Let the AI inspect the machine, install the source checkout into a dedicated
   environment, configure the workspace and its native MCP connection, preserve
   project instructions, and verify the result. Complete the host's trust or
   restart handoff when requested, then ask it to resume the ritual.

The grimoire selects the current host, or the one named in your request. It
does not register every CLI found on the machine.

| Host | Project installation | Native MCP config |
| --- | --- | --- |
| Claude Code (`claude`) | Claude branch of the grimoire, including maintained reminder hooks | `.mcp.json` |
| Codex (`codex`) | `install-client codex` | `.codex/config.toml` |
| Antigravity CLI (`agy`) | `install-client agy` | `.agents/mcp_config.json` |
| OpenCode V1 (`opencode`) | `install-client opencode` | `opencode.json(c)` or `.opencode/opencode.json(c)` |
| Oh My Pi (`omp`) | `install-client omp` | `.omp/mcp.json` |
| Earendil Works Pi (`pi`) | `install-client pi` | `.pi/mcp.json` |

The project client installer adds native MCP registration and a concise owned
covenant in the effective instruction file: normally `AGENTS.md`, a Codex
override/project fallback, or OpenCode's existing `CLAUDE.md` fallback. It preserves
unrelated configuration and comments, reports backups/conflicts, and leaves
global settings, provider credentials and permission policies alone. The five
new client paths do not automatically install host hooks or legacy edit-bridge
pairing. Guided memory calls return the automatic brief for an unbriefed session;
proactive host reminders are a separate feature.

Client support is version-specific. Real Windows client tests passed the core
memory workflow, restart recall and replay checks on Codex 0.167.0, agy 1.4.7,
OpenCode 1.18.21, omp 18.16.0 and Earendil Works Pi 1.0.0. See the source-backed
[Codex/agy](docs/research/cli-hosts-codex-agy-2026-11-12.md),
[OpenCode](docs/research/cli-hosts-opencode-2026-11-12.md) and
[OMP/Pi](docs/research/cli-hosts-omp-pi-2026-11-12.md) findings for feature and
acceptance boundaries. OpenCode V2 has a different schema and requires separate
integration evidence. Actual setup and live-client results are recorded in the
[multi-CLI acceptance report](docs/release/v7/multi-cli-acceptance-2026-11-12.md).

For agy, keep your normal Google-account sign-in. A Gemini API key is a separate,
optional billing route. To use Google AI credits after your included quota runs
out, enable **Use AI Credits** in agy's `/settings` or `/config`; `/credits`
shows the balance. Daem0n's installer does not change this spending preference.
See [Antigravity's credit instructions](https://antigravity.google/docs/cli/credits).

The file is a self-contained instruction manual for the AI. Copying it alone
does not execute it. The default ritual uses Python 3.12 when available,
project-scoped stdio on Windows, macOS and Linux, and the `local`, `graph`,
`apps`, and `models-local` extras. These local models need downloads and disk
space; the grimoire also covers an explicitly requested small-core setup.
E2B cloud execution, Redis tasks and telemetry are optional, not setup
prerequisites. Existing memories, unrelated settings and other MCP servers
are preserved. Authentication and the selected host's trust prompts may require
your participation; the AI must report those handoffs instead of claiming success.

### Register a configured environment manually

After the grimoire's source/environment and workspace-ID steps, run from the
trusted source checkout using its dedicated interpreter. Replace `<CLIENT>`
with one of `codex`, `agy`, `opencode`, `omp` or `pi`, and pass the selected
client's observed `--version` output:

```text
<VENV_PYTHON> -m daem0nmcp.cli --project-path "<PROJECT>" --json install-client <CLIENT> --client-version "<OBSERVED_VERSION>" --dry-run
<VENV_PYTHON> -m daem0nmcp.cli --project-path "<PROJECT>" --json install-client <CLIENT> --client-version "<OBSERVED_VERSION>"
```

Review the returned file actions and complete the normal host trust/restart
flow. Configuration recognition is followed by actual brief, health, write,
recall, outcome and restart checks from the grimoire. For OpenCode-specific
protocol details, see [its companion](Summon_Daem0n_OpenCode.md).

### Manual Installation

For the complete install, existing-data migration, upgrade and verification
sequence, use [the grimoire](Summon_Daem0n.md). These commands show the package
installation portion from a trusted source directory:

```bash
# Clone the repository
git clone https://github.com/9thLevelSoftware/Daem0n-MCP.git "$HOME/Daem0n-MCP"
cd "$HOME/Daem0n-MCP"

# macOS/Linux; use the selected Python 3.11 or 3.12 interpreter
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[local,graph,apps,models-local]'
```

```powershell
# Windows, from the source checkout
py -3.12 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -e '.[local,graph,apps,models-local]'
```

## Installation by Platform

### Stdio (all supported platforms)

Stdio is the default, including on Windows. The selected native MCP host starts and stops the
server; no separate terminal, port, Startup shortcut or background service is
needed. Codex, agy, OpenCode V1, omp and pi use the project installer above.
For Claude Code, run registration from the target project, using the absolute path of
the installed virtual environment's interpreter and the absolute project root:

```bash
claude mcp add daem0nmcp --scope project --transport stdio -e "DAEM0NMCP_PROJECT_ROOT=<PROJECT_ROOT>" -- "<PYTHON_PATH>" -c "import sys; sys.path = [p for p in sys.path if p]; import runpy; runpy.run_module('daem0nmcp.server', run_name='__main__', alter_sys=True)"
```

If a Daem0n entry already exists, follow the grimoire's inspect-and-merge flow
instead of deleting it. Obtain the workspace selector with the installed
interpreter's `python -m daem0nmcp.cli --json workspace-id`, with
`DAEM0NMCP_PROJECT_ROOT` set to the same project. Store the returned ID in the
project protocol, then restart Claude Code and approve the project server when
prompted. Run `session_brief` and `system_health` with that ID before the
grimoire's write/recall/restart acceptance checks.

Do not register `<PYTHON_PATH> -m daem0nmcp.server`. Claude Code
starts the server inside the opened project, and `python -m`
puts that directory first on `sys.path`, so a repository that contains its own
`daem0nmcp/` directory would run in place of the installed server. The command
above runs the same module after dropping the working directory from the import
path; it works on Python 3.10 and newer and with `--user`, virtualenv and
editable installs. Use the full interpreter path rather than a bare `python`.

The command leaves your own `PYTHONPATH` alone. An empty or relative element in
it (a leading or trailing `:`, or `.`) names the working directory, which puts
the opened project back on the import path: keep `PYTHONPATH` unset or absolute
in the environment your MCP host starts from.

### Optional Streamable HTTP

Use HTTP when you deliberately want a separately managed server. It is not a
Windows requirement. Set `DAEM0NMCP_PROJECT_ROOT` in the server's environment
before starting it from a trusted directory with the installed interpreter:

```bash
python -m daem0nmcp.server --transport streamable-http --host 127.0.0.1 --port 9876
```

Merge this entry into the target project's existing `.mcp.json`:
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

Start Claude Code after the HTTP server is ready. Keep storage derived per
workspace. Remote or shared-host HTTP requires the authentication configuration
described in [the operations guide](docs/v7-operations.md); local stdio needs no
network listener.

### Transport Modes

| Method | Transport | Default Port | Use Case |
|--------|-----------|-------------|----------|
| `python -m daem0nmcp.server` | `stdio` | n/a | Local process channel |
| `python start_server.py` | Streamable HTTP at `/mcp` | 9876 | Optional separately managed HTTP server |

`python -m daem0nmcp.server` is for a terminal you started yourself. When an MCP
host launches the stdio server for you, register the command from
[Stdio](#stdio-all-supported-platforms) instead: hosts start it inside
whatever project is open.

For the five other CLI hosts, use the project-scoped
[client installer](#register-a-configured-environment-manually). OpenCode's
optional legacy plugin workflow is described under [OpenCode Integration](#opencode-integration).

## v7 MCP API

The v7 public API uses strict input schemas and opaque workspace scope. The
default core-plus-gateway listing is:

| Tool | Purpose |
|---|---|
| `session_brief` | Return the full briefing; guided memory calls automatically brief an unbriefed session |
| `memory_preflight` | Plan ahead with guidance and a capability for one exact protected request |
| `memory_recall` | Retrieve bounded, evidence-bearing records |
| `memory_store` | Append one replay-safe memory record |
| `memory_record_outcome` | Append a verified success or failure outcome |
| `system_health` | Return bounded service and projection diagnostics |
| `edit_preflight` | Admit native edit-bridge requests |
| `daem0n_tools_search` | Discover other registered tools and their input schemas |
| `daem0n_tool_call` | Call a discovered tool and relay its guidance or result |

`daem0n_tool_call` can reach destructive, non-idempotent and open-world tools,
so it advertises `destructiveHint: true`, `idempotentHint: false` and
`openWorldHint: true`; a host that prompts on those hints prompts for every
gateway call. Each `daem0n_tools_search` result carries the target's own
`read_only`, `destructive`, `idempotent` and `open_world` hints.

The read-only JSON resources are:

- `memory://workspaces/{workspace_id}/warnings`
- `memory://workspaces/{workspace_id}/failures`
- `memory://workspaces/{workspace_id}/rules`
- `memory://workspaces/{workspace_id}/active-context`

A read returns `application/json`. Every refusal (an unknown workspace, one the
caller may not read, or one this session has not briefed) is the same JSON-RPC
error, code `-32002` with the message `Resource unavailable`; brief the
workspace first.

See [`docs/v6-to-v7-tools.json`](docs/v6-to-v7-tools.json) for the generated
mapping of every v6 operation to its v7 replacement.

### Historical v6: session and status workflow

| Action | Purpose |
|--------|---------|
| `briefing` | Smart session start with git awareness |
| `active_context` | Get all hot memories for current focus |
| `triggers` | Check context triggers for auto-recalled memories |
| `health` | Server health, version, and statistics |
| `covenant` | Sacred Covenant status and phase |
| `updates` | Poll for knowledge changes (real-time notifications) |

### Historical v6: pre-action workflow

| Action | Purpose |
|--------|---------|
| `preflight` | Combined recall + rules check before changes |
| `recall` | Semantic memory retrieval by topic (supports `condensed`, `visual`) |
| `recall_file` | Get memories linked to a specific file |
| `recall_entity` | Get memories mentioning a code entity |
| `recall_hierarchical` | GraphRAG-style layered retrieval with community summaries |
| `search` | Full-text search across all memories (supports `highlight`, `visual`) |
| `check_rules` | Validate action against decision rules |
| `compress` | LLMLingua-2 context compression |

### Historical v6: memory writing workflow

| Action | Purpose |
|--------|---------|
| `remember` | Store a memory with conflict detection |
| `remember_batch` | Store multiple memories in one transaction |
| `link` | Create causal relationships between memories |
| `unlink` | Remove relationships between memories |
| `pin` | Pin/unpin memories to prevent pruning |
| `activate` | Add memory to always-hot working context |
| `deactivate` | Remove memory from active context |
| `clear_active` | Clear all active context |
| `ingest` | Import external documentation from URL |

### Historical v6: outcomes workflow

| Action | Purpose |
|--------|---------|
| `outcome` | Record whether a decision worked or failed |
| `verify` | Validate factual claims against stored knowledge |
| `execute` | Sandboxed Python execution (E2B) |

### Historical v6: code comprehension workflow

| Action | Purpose |
|--------|---------|
| `index` | Index code entities via tree-sitter |
| `find` | Semantic search across code entities |
| `impact` | Analyze blast radius of code changes |
| `todos` | Scan for TODO/FIXME/HACK/XXX/BUG comments |
| `refactor` | Generate refactor suggestions with causal history |

### Historical v6: rules and triggers workflow

| Action | Purpose |
|--------|---------|
| `add_rule` | Create decision tree rules |
| `update_rule` | Modify existing rules |
| `list_rules` | Show all configured rules |
| `add_trigger` | Create auto-recall context triggers |
| `list_triggers` | List all context triggers |
| `remove_trigger` | Remove a context trigger |

### Historical v6: graph and discovery workflow

| Action | Purpose |
|--------|---------|
| `related` | Find related memories via graph traversal |
| `chain` | Find causal paths between memories |
| `graph` | Visualize memory relationships (JSON/Mermaid, supports `visual`) |
| `stats` | Knowledge graph metrics |
| `communities` | List Leiden community clusters (supports `visual`) |
| `community_detail` | Drill down into a community |
| `rebuild_communities` | Detect communities via Leiden algorithm |
| `entities` | List most frequently mentioned entities |
| `backfill_entities` | Extract entities from existing memories |
| `evolution` | Trace knowledge evolution over time |
| `versions` | Get version history for a memory |
| `at_time` | Query memory state at a point in time |

### Historical v6: housekeeping and federation workflow

| Action | Purpose |
|--------|---------|
| `prune` | Remove old, low-value memories (with protection) |
| `archive` | Archive/restore memories |
| `cleanup` | Find and merge duplicate memories |
| `compact` | Consolidate episodic memories into summaries |
| `rebuild_index` | Force rebuild TF-IDF and vector indexes |
| `export` | Export all memories and rules as JSON |
| `import_data` | Import memories and rules from JSON |
| `link_project` | Link to another project for cross-repo awareness |
| `unlink_project` | Remove a project link |
| `list_projects` | List all linked projects |
| `consolidate` | Merge memories from linked projects |

### Historical v6: standalone cognitive tools

| Tool | Purpose |
|------|---------|
| `simulate_decision` | Temporal Scrying — replay a past decision with current knowledge |
| `evolve_rule` | Rule Entropy — examine rules for staleness and drift |
| `debate_internal` | Adversarial Council — evidence-grounded debate with convergence detection |

### Tool Names by Client

Recognize only the exact canonical name or its host prefix:

| Canonical | Claude Code | OpenCode |
|---|---|---|
| `session_brief` | `mcp__daem0nmcp__session_brief` | `daem0nmcp_session_brief` |
| `memory_preflight` | `mcp__daem0nmcp__memory_preflight` | `daem0nmcp_memory_preflight` |
| `memory_recall` | `mcp__daem0nmcp__memory_recall` | `daem0nmcp_memory_recall` |
| `memory_store` | `mcp__daem0nmcp__memory_store` | `daem0nmcp_memory_store` |
| `memory_record_outcome` | `mcp__daem0nmcp__memory_record_outcome` | `daem0nmcp_memory_record_outcome` |
| `system_health` | `mcp__daem0nmcp__system_health` | `daem0nmcp_system_health` |
| `daem0n_tools_search` | `mcp__daem0nmcp__daem0n_tools_search` | `daem0nmcp_daem0n_tools_search` |
| `daem0n_tool_call` | `mcp__daem0nmcp__daem0n_tool_call` | `daem0nmcp_daem0n_tool_call` |

Host metadata is never an identity source. Authentication comes from the
transport, and every scoped request is resolved through `workspace_id`.
Codex, agy and Pi may expose tools through lazy discovery or a native wrapper.
Use the client's discovered `daem0nmcp` server and exact v7 tool basename, keeping
arguments and structured counsel/remedy fields intact. A missing direct tool
name does not establish that a lazy tool is unavailable. MCP resource browsing
and rich app rendering are separate host capabilities; they are not implied by
a successful memory tool call.

## Usage Examples

### Session Start

Guided `memory_recall` returns a compact automatic brief for an unbriefed session. Request the full brief
explicitly when needed:

```text
session_brief(
    workspace_id="<workspace_id>",
    focus_areas=["authentication", "API"]
)
```

### Retrieve Memories

```text
memory_recall(
    workspace_id="<workspace_id>",
    query="authentication",
    limit=10,
    token_budget=2400
)
```

### Store a Memory

Call directly; no preflight is needed for a benign write in guided mode:

```text
memory_store(
    workspace_id="<workspace_id>",
    record_type="decision",
    content="Use signed session cookies",
    rationale="Avoid server-side session state",
    tags=["auth", "architecture"],
    relative_file_path="src/auth/session.py",
    idempotency_key="decision-auth-session-0001"
)
```

If the response is `COUNSEL_REQUIRED`, review `error.counsel`, then call
`error.remedy.tool` with exactly `error.remedy.arguments`. The remedy already
includes its pre-minted token. `memory_preflight` remains available for advance
planning and is required in strict mode. Keep the idempotency key on retries.

Every failed call, `COUNSEL_REQUIRED` included, is an `ok: false` envelope and
sets the MCP result's `isError: true`. The envelope is still delivered in full,
as `structuredContent` and as JSON text; a client library that raises on
`isError` must be told to return the result instead so the remedy can be read.

### Track Outcomes

```text
memory_record_outcome(
    workspace_id="<workspace_id>",
    record_id="<mem_id>",
    outcome_text="The implementation passed integration tests",
    worked=true,
    idempotency_key="outcome-auth-session-0001"
)
```

## AI Agent Protocol

The recommended workflow for AI agents:

```text
memory_recall or memory_preflight (automatic brief in guided mode)
    -> memory_recall (when relevant)
    -> protected tool directly
    -> review error.counsel and retry error.remedy (only when challenged)
    -> memory_record_outcome (after verification, replay-safe)
```

Use `system_health` for bounded diagnostics. The complete protocol lives in
`AGENTS.md` and `.claude/skills/daem0nmcp-protocol/SKILL.md`.

## Claude Code Integration

### Hook Installation

```bash
python -m daem0nmcp.cli install-claude-hooks
```

This writes the packaged hook entries to the user-level `~/.claude/settings.json`, so
they run in every project; each runs `daem0nmcp.claude_hooks.<hook>`
with the interpreter you installed from. A hook only acts inside a Daem0n
project: the session directory, `CLAUDE_PROJECT_DIR`, or one of their parent
directories contains `.daem0nmcp/` (your home directory does not count, since
Daem0n keeps its own state in `~/.daem0nmcp/`). Other hooks in that file are kept, and any old
`hooks/daem0n_*.py` entries are removed.

Each command is `"<python>" -c "<bootstrap>"`: the bootstrap drops the working
directory from `sys.path` and then runs the hook module, so a repository that
contains its own `daem0nmcp/` directory cannot replace the installed hooks the
way it could with `python -m`. Commands written by an earlier version keep the
`-m` form until you re-run `install-claude-hooks`, which replaces them and
reports how many it replaced; until then the `SessionStart` hook shows a
one-line reminder in every session. The same `PYTHONPATH` limit as for the
[registration command](#stdio-all-supported-platforms) applies.

The installer currently uses the operating-system home directory, not
`CLAUDE_CONFIG_DIR`. The grimoire handles custom Claude configuration directories
by generating and merging the Daem0n hook entries into the actual settings file.

| Event (matcher) | Hook | What it does | Input | Reminds only |
|-----------------|------|--------------|-------|--------------|
| `SessionStart` | `session_start` | Explains guided memory calls auto-brief an unbriefed session; diagnostics and discovery do not; `session_brief` returns the full brief | `CLAUDE_PROJECT_DIR` env | Yes |
| `PreToolUse` (`Edit\|Write\|NotebookEdit`) | `pre_edit` | Inside a Daem0n project, adds a one-line `additionalContext` reminder to call `daem0n_tool_call` targeting `memory_recall_file`; silent elsewhere | stdin event | Yes |
| `PreToolUse` (`Bash`) | `pre_bash` | Checks the command against active Daem0n rules (`governance_rules` / `rules`) in read-only SQLite mode and emits matching `additionalContext` reminders | stdin event (`TOOL_INPUT` fallback) | Yes (always exits 0) |
| `PostToolUse` (`mcp__.*__edit_preflight`) | `post_edit_preflight` | Retained edit-bridge plumbing: stages a receipt for an explicit host-created request. The current remind-only hooks create no requests, so pairing alone does not activate this flow | stdin event | Yes |
| `PostToolUse` (`Edit\|Write\|NotebookEdit`) | `post_edit` | Retained edit-bridge plumbing: reports an approved edit as a capture candidate. The current remind-only hooks create no requests, so ordinary client edits produce no bridge capture | stdin event | Yes |
| `Stop`, `SubagentStop` | `stop` | When the transcript shows a finished task with no recorded outcome, suggests direct `memory_store` and `memory_record_outcome` calls with counsel-challenge retry guidance as a `systemMessage`. Never writes memory | stdin event (`transcript_path`) | Yes |

No hook blocks a tool call or keeps the agent running: every hook exits 0 and none
returns a `permissionDecision` or `decision`.

**What optional pairing writes.** When `--enable-edit-bridge` (or explicit remote bridge flags) are passed, the installer also pairs the current project (or
`--project-path`) with the local edit bridge:

- an owner-only credential at `~/.daem0nmcp/edit-bridges/<authority>/credential.json`
  (the secret is never written into the project), with the bridge socket under
  `~/.daem0nmcp/edit-bridges/run/<authority[:16]>/`;
- the env keys `DAEM0NMCP_EDIT_BRIDGE_CREDENTIAL_FILE`,
  `DAEM0NMCP_EDIT_BRIDGE_RUNTIME_DIR`, `DAEM0NMCP_PROJECT_ROOT` and
  `DAEM0NMCP_STORAGE_PATH` in `<project>/.claude/settings.local.json`.

To remove the hooks, the project's pairing env keys (only if the installer
paired it) and, optionally, its credential (the project defaults to the current
directory):

```bash
python -m daem0nmcp.cli uninstall-claude-hooks [--project-path <project>] [--remove-credentials]
```

The old manual scripts in `hooks/` are deprecated stubs that only print a
notice and exit 0. If your settings still point at them, re-run
`install-claude-hooks`.

### Protocol Skill

A Claude Code skill is included at `.claude/skills/daem0nmcp-protocol/SKILL.md` that enforces the memory protocol automatically.

## OpenCode Integration

OpenCode V1 connects through its native MCP manager. For a new project use
`install-client opencode` from [Quick Start](#quick-start), then finish the
grimoire's real MCP and restart verification. The new installer creates the
project binding and managed covenant without requiring the legacy plugin.

The optional existing V1 plugin provides Covenant guidance and edit/Bash reminders. Its installed
hooks do not create native edit-bridge requests or enforce a
deny/preflight/retry flow; bridge API acceptance is separate from this
remind-only client integration.

### Configuration

For inspecting an existing **V1** entry, the shape is below. Replace the
interpreter and project placeholders with absolute paths; the project installer
performs the preserving merge for a new registration:

```json
{
  "$schema": "https://opencode.ai/config.json",
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

The command is the cwd-free equivalent of `python -m daem0nmcp.server`: OpenCode starts
the server inside the opened project, and `-m` would import a `daem0nmcp/`
directory found there. `install-client opencode` fixes the interpreter to the
dedicated environment it runs under. Its `-E` flag ignores inherited Python import
settings before startup, including hosts that discard empty environment overrides.
It does not replace unrelated servers or provider settings. Public OpenCode V2
docs use `mcp.servers`/`disabled`; this V1 example must not be copied into V2.

For a separately managed Streamable HTTP server, use:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "daem0nmcp": {
      "type": "remote",
      "url": "http://localhost:9876/mcp",
      "enabled": true
    }
  }
}
```

### System Instructions

OpenCode reads `AGENTS.md` from the project root, with `CLAUDE.md` as a fallback.
The installer adds the covenant to the existing effective file so that it does
not hide the project's other instructions.

If both `AGENTS.md` and `CLAUDE.md` exist in a project, OpenCode uses only `AGENTS.md`.

### Optional legacy plugin workflow

The older `install-opencode` command scaffolds the V1 plugin and can provision
edit-bridge pairing. It is separate from `install-client opencode` and is not
required for core memory functionality. Inspect its dry-run and existing files
before choosing it. In particular, `--force` overwrites configuration and can
discard other MCP servers; do not use it as an automatic upgrade path. Existing
plugins and shortcuts may be retained while their own behavior is verified.

### Tool Names

OpenCode uses a different tool name format than Claude Code:

| Client | Format | Example |
|--------|--------|---------|
| Claude Code | `mcp__servername__toolname` | `mcp__daem0nmcp__session_brief` |
| OpenCode | `servername_toolname` | `daem0nmcp_session_brief` |

Both formats resolve to the same MCP server tools. Use the exact form matching
your client. See [Tool Names by Client](#tool-names-by-client).

## How It Works

### Hybrid Search (BM25 + Vector + RRF)
- **BM25** for keyword matching with term saturation and length normalization
- **ModernBERT** vector embeddings (256-dim, asymmetric encoding) for deep semantic understanding
- **Reciprocal Rank Fusion** combines both with configurable weights
- "blocking database calls" matches memories about "synchronous queries"

### Auto-Zoom Retrieval Routing
Query-aware dispatch classifies complexity and routes to the optimal strategy:
- Simple queries → vector-only (fast path)
- Medium queries → hybrid BM25+vector
- Complex queries → GraphRAG multi-hop with community summaries

### Memory Decay
```
weight = e^(-lambda*t) where lambda = ln(2)/half_life_days
```
Default half-life is 30 days. Patterns and warnings are permanent (no decay).

### Conflict Detection
When storing a new memory, it's compared against recent memories:
- If similar content failed before → warning about the failure
- If it matches an existing warning → warning surfaced
- If highly similar content exists → potential duplicate flagged

### Failed Decision Boosting
Memories with `worked=False` get a 1.5x relevance boost in recalls.
Warnings get a 1.2x boost. Past mistakes surface prominently.

### Background Dreaming
During idle periods, the daemon re-evaluates failed decisions against current evidence. `FailedDecisionReview` strategy finds `worked=False` decisions, recalls current evidence, and persists actionable insights as `learning` memories. Yields cooperatively when the user returns.

## Data Storage

Each project gets isolated storage at:
```
<project_root>/.daem0nmcp/storage/daem0nmcp.db
```

`<project_root>/.daem0nmcp/workspace-id` records which workspace those rows
belong to, so the memories follow the folder when it is moved or renamed. Move
the whole folder, `.daem0nmcp/` included; re-clone by copying `.daem0nmcp/` into
the new clone. Two registered folders may not claim one identity — settle a
copy with `python -m daem0nmcp.cli --project-path <root> workspace-id --new`.
See `docs/v7-operations.md` for the full rules.

## Configuration

All settings are configurable via environment variables with `DAEM0NMCP_` prefix.

A `.env` file in the working directory is not read: the server is usually
launched inside a project, so that file belongs to the repository, not to the
operator. To keep settings in a file, set `DAEM0NMCP_ENV_FILE` to its absolute
path in the server's environment. A relative path or a missing file is refused
at startup, and real environment variables override values from the file.

That file may hold only the settings listed in the tables below. Any other
line stops startup, including variables the server reads straight from its
environment: the transport and proxy settings (`DAEM0NMCP_ALLOWED_HOSTS`,
`DAEM0NMCP_ALLOWED_ORIGINS`, `DAEM0NMCP_TRUSTED_PROXY_IPS`),
`DAEM0NMCP_WORKSPACE_ACCESS_FILE`, `DAEM0NMCP_TOKEN_SECRET`, the
`DAEM0NMCP_EDIT_BRIDGE_*` and task variables, and every `FASTMCP_*` variable.
Keep those in the process environment.

The FastMCP library has its own `.env` loading for `FASTMCP_*` settings. The
server turns it off by setting `FASTMCP_ENV_FILE` to the null device when it is
unset, so a project's `.env` cannot change them either. To load `FASTMCP_*`
settings from a file, set `FASTMCP_ENV_FILE` yourself to an absolute path; a
relative value is resolved against the working directory. Authentication
variables (`FASTMCP_SERVER_AUTH*`) are read only from the process environment.

### Core Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `DAEM0NMCP_PROJECT_ROOT` | `.` | Project root path |
| `DAEM0NMCP_STORAGE_PATH` | auto | Override storage location |
| `DAEM0NMCP_LOG_LEVEL` | `INFO` | Logging level |
| `DAEM0NMCP_QDRANT_URL` | `None` | Remote Qdrant URL (overrides local) |

### Embedding Model (v6.6.6+)

| Variable | Default | Description |
|----------|---------|-------------|
| `DAEM0NMCP_EMBEDDING_MODEL` | `nomic-ai/modernbert-embed-base` | Model name |
| `DAEM0NMCP_EMBEDDING_DIMENSION` | `256` | Matryoshka truncation dimension |
| `DAEM0NMCP_EMBEDDING_BACKEND` | auto-detected | `onnx` or `torch` |
| `DAEM0NMCP_EMBEDDING_QUERY_PREFIX` | `search_query: ` | Prefix for query encoding |
| `DAEM0NMCP_EMBEDDING_DOCUMENT_PREFIX` | `search_document: ` | Prefix for document encoding |

### Search Tuning

| Variable | Default | Description |
|----------|---------|-------------|
| `DAEM0NMCP_HYBRID_VECTOR_WEIGHT` | `0.3` | Vector weight in hybrid search (0.0-1.0) |
| `DAEM0NMCP_BM25_K1` | `1.5` | BM25 term frequency saturation |
| `DAEM0NMCP_BM25_B` | `0.75` | BM25 document length normalization |
| `DAEM0NMCP_RRF_K` | `60` | RRF fusion dampening constant |
| `DAEM0NMCP_LEXICAL_CONTENT_TIERING` | `false` | Rank content/rationale matches ahead of tag-assisted matches in lexical recall |
| `DAEM0NMCP_SEARCH_DIVERSITY_MAX_PER_FILE` | `3` | Max results from same file |

### Auto-Zoom Routing

| Variable | Default | Description |
|----------|---------|-------------|
| `DAEM0NMCP_AUTO_ZOOM_ENABLED` | `false` | Master switch for query-aware routing |
| `DAEM0NMCP_AUTO_ZOOM_SHADOW` | `true` | Log classifications without routing |
| `DAEM0NMCP_AUTO_ZOOM_CONFIDENCE_THRESHOLD` | `0.25` | Below this → hybrid fallback |
| `DAEM0NMCP_AUTO_ZOOM_GRAPH_EXPANSION_DEPTH` | `2` | Multi-hop depth for complex queries |

### Background Dreaming

| Variable | Default | Description |
|----------|---------|-------------|
| `DAEM0NMCP_DREAM_ENABLED` | `true` | Master switch for dreaming |
| `DAEM0NMCP_DREAM_IDLE_TIMEOUT` | `60.0` | Seconds of idle before dreaming starts |
| `DAEM0NMCP_DREAM_MAX_DECISIONS_PER_SESSION` | `5` | Max failed decisions to re-evaluate |
| `DAEM0NMCP_DREAM_MIN_DECISION_AGE_HOURS` | `1` | Min age before re-evaluation eligible |

### Cognitive Tools

| Variable | Default | Description |
|----------|---------|-------------|
| `DAEM0NMCP_COGNITIVE_DEBATE_MAX_ROUNDS` | `5` | Max rounds for adversarial council |
| `DAEM0NMCP_COGNITIVE_EVOLVE_MAX_RULES` | `10` | Max rules to analyze for staleness |
| `DAEM0NMCP_COGNITIVE_STALENESS_AGE_WEIGHT` | `0.3` | Time-based decay weight |

### Recall Planner

| Variable | Default | Description |
|----------|---------|-------------|
| `DAEM0NMCP_SURPRISE_K_NEAREST` | `5` | Neighbors for surprise calculation |
| `DAEM0NMCP_SURPRISE_BOOST_THRESHOLD` | `0.7` | Boost threshold (0.0-1.0) |
| `DAEM0NMCP_RECALL_SIMPLE_MAX_MEMORIES` | `5` | Max memories for simple queries |
| `DAEM0NMCP_RECALL_MEDIUM_MAX_MEMORIES` | `10` | Max memories for medium queries |
| `DAEM0NMCP_RECALL_COMPLEX_MAX_MEMORIES` | `20` | Max memories for complex queries |
| `DAEM0NMCP_FACT_PROMOTION_THRESHOLD` | `3` | Outcomes to promote to fact |

See [Summon_Daem0n.md](Summon_Daem0n.md) for automatic setup and feature choices,
and [the v7 operations guide](docs/v7-operations.md) for deployment settings.

## Architecture

```
daem0nmcp/
├── api/v7/              # Strict v7 models, handlers, policy, and adapters
├── server.py            # MCP server composition and transport entry point
├── mcp_instance.py      # FastMCP instance creation
├── config.py            # Pydantic settings (~50 configurable options)
├── memory.py            # Memory storage & semantic retrieval
├── rules.py             # Rule engine with BM25 matching
├── similarity.py        # TF-IDF index, decay, conflict detection
├── vectors.py           # ModernBERT embeddings (ONNX/torch, asymmetric encoding)
├── surprise.py          # Titans-inspired novelty detection
├── recall_planner.py    # TiMem-style complexity classification
├── retrieval_router.py  # Auto-Zoom query-aware search dispatch
├── query_classifier.py  # Exemplar-based query complexity classification
├── qdrant_store.py      # Qdrant vector database backend
├── active_context.py    # MemGPT-style always-hot working memory
├── entity_extractor.py  # Entity extraction from memory content
├── entity_manager.py    # Entity lifecycle management
├── communities.py       # Leiden community detection & summaries
├── context_triggers.py  # Auto-recall trigger system
├── context_manager.py   # Multi-project context management
├── covenant.py          # Sacred Covenant enforcement & preflight tokens
├── database.py          # SQLite async database
├── models.py            # 10+ tables: memories, rules, relationships, etc.
├── enforcement.py       # Pre-commit enforcement & session tracking
├── hooks.py             # Git hook templates & installation
├── cli.py               # Command-line interface
├── workflows/           # Legacy v6 compatibility implementation (migration only)
│   ├── commune.py       # Session start & status
│   ├── consult.py       # Pre-action intelligence
│   ├── inscribe.py      # Memory writing & linking
│   ├── reflect.py       # Outcomes & verification
│   ├── understand.py    # Code comprehension
│   ├── govern.py        # Rules & triggers
│   ├── explore.py       # Graph & discovery
│   └── maintain.py      # Housekeeping & federation
├── tools/               # MCP tool registrations
│   ├── workflows.py     # Legacy v6 workflow definitions (migration only)
│   ├── cognitive_tools.py # simulate_decision, evolve_rule, debate_internal
│   ├── memory.py        # Legacy memory tools (deprecated)
│   ├── briefing.py      # Legacy briefing tools (deprecated)
│   ├── code_tools.py    # Legacy code tools (deprecated)
│   ├── context_tools.py # Legacy context tools (deprecated)
│   ├── entity_tools.py  # Legacy entity tools (deprecated)
│   ├── graph_tools.py   # Legacy graph tools (deprecated)
│   ├── rules.py         # Legacy rule tools (deprecated)
│   ├── temporal.py      # Legacy temporal tools (deprecated)
│   ├── verification.py  # Legacy verification tools (deprecated)
│   ├── federation.py    # Legacy federation tools (deprecated)
│   ├── maintenance.py   # Legacy maintenance tools (deprecated)
│   └── agency_tools.py  # Legacy agency tools (deprecated)
├── cognitive/           # Cognitive tool implementations
│   ├── simulate.py      # Temporal scrying (decision replay)
│   ├── evolve.py        # Rule entropy analysis
│   └── debate.py        # Adversarial council
├── dreaming/            # Background dreaming system
│   ├── scheduler.py     # IdleDreamScheduler (idle detection + dispatch)
│   ├── strategies.py    # FailedDecisionReview strategy
│   └── persistence.py   # Dream session/result models & persistence
├── claude_hooks/        # Claude Code native hooks
│   ├── install.py       # install/uninstall-claude-hooks CLI
│   ├── session_start.py # Suggest scoped v7 briefing
│   ├── pre_edit.py      # Remind-only file recall guidance
│   ├── pre_bash.py      # Read-only command-rule reminders
│   ├── post_edit.py     # Retained edit-bridge capture reporting
│   ├── stop.py          # Read-only outcome/memory suggestions
│   └── _client.py       # Hook helper utilities
├── compression/         # LLMLingua-2 context compression
├── graph/               # Knowledge graph (NetworkX + Leiden)
├── reflexion/           # Metacognitive architecture (LangGraph)
├── agency/              # Dynamic agency (E2B sandboxing)
├── transforms/          # FastMCP 3.0 middleware
│   └── covenant.py      # CovenantMiddleware & CovenantTransform
├── ui/                  # MCP Apps visual interfaces (D3.js)
├── channels/            # Notification channels (desktop, log, editor-poll)
├── migrations/          # Database schema & embedding migrations
├── code_indexer.py      # Code understanding via tree-sitter
├── watcher.py           # Proactive file watcher daemon
├── tracing.py           # OpenTelemetry integration (optional)
├── prompt_templates.py  # AutoPDL-inspired modular prompts
└── tool_search.py       # Dynamic tool discovery index

.claude/
└── skills/
    └── daem0nmcp-protocol/
        └── SKILL.md       # Protocol enforcement skill

.opencode/
├── commands/
│   ├── commune.md       # Host shortcut for session_brief
│   ├── counsel.md       # Host shortcut for memory_preflight
│   ├── inscribe.md      # Host shortcut for memory_store
│   └── recall.md        # Host shortcut for memory_recall
└── plugins/
    └── daem0n.ts        # Covenant enforcement plugin

Summon_Daem0n.md              # Complete host-selecting installation grimoire
Summon_Daem0n_OpenCode.md     # OpenCode V1 protocol and binding companion
Banish_Daem0n.md              # Uninstallation instructions (both clients)
start_server.py               # HTTP server launcher (streamable-http transport)
```

## CLI Commands

```bash
# Index code entities
python -m daem0nmcp.cli index [--path PATH] [--patterns **/*.py **/*.ts ...]

# Scan for TODO/FIXME/HACK comments
python -m daem0nmcp.cli scan-todos [--auto-remember] [--path PATH]

# Check a file against memories and rules
python -m daem0nmcp.cli check <filepath>

# Run database migrations (usually automatic; vector backfill is format 6 only)
python -m daem0nmcp.cli migrate [--backfill-vectors]

# Inspect a v7 retrieval projection
python -m daem0nmcp.cli projection-status --workspace-id <workspace_id>

# Rebuild a v7 retrieval projection from canonical events
python -m daem0nmcp.cli rebuild-projection --projection dense --workspace-id <workspace_id>
```

### Client & Hook Commands

```bash
# Register one native CLI host in the selected project (from trusted source cwd)
python -m daem0nmcp.cli --project-path /path/to/project install-client codex --client-version 0.167.0 --dry-run
# Replace codex with agy, opencode, omp or pi and use its observed version.
# Omit --dry-run after inspecting the proposed owned changes.

# Install Claude Code hooks (user-level, all projects)
python -m daem0nmcp.cli install-claude-hooks [--dry-run]

# Remove Claude Code hooks
python -m daem0nmcp.cli uninstall-claude-hooks [--dry-run]

# Optional legacy OpenCode V1 plugin/bridge scaffolding (inspect before use)
python -m daem0nmcp.cli install-opencode [--dry-run] [--force]
# Creates: opencode.json, .opencode/ directories, .opencode/plugins/daem0n.ts
# AGENTS.md and command files are not auto-created

# Install git pre-commit hooks
python -m daem0nmcp.cli install-hooks [--force]

# Remove git pre-commit hooks
python -m daem0nmcp.cli uninstall-hooks

```

Use MCP `memory_record_outcome` for v7 outcomes. It requires an opaque
`workspace_id`, a v7 `record_id`, and a stable idempotency key.

## Upgrading

Ask the selected AI CLI to read the current [Summon_Daem0n.md](Summon_Daem0n.md) and
resume the ritual. It inventories the existing installation, preserves data and
configuration, upgrades the selected environment, refreshes its own integration
entries, and verifies persistence. It does not reset a workspace or overwrite
unrelated settings. Use the following notes for manual maintenance.

### 1. Update the Code

```bash
# Inspect before updating; retain local work and record the prior revision.
cd "$HOME/Daem0n-MCP"
git status --short --branch
git rev-parse HEAD
# With a clean checkout on the intended branch:
git pull --ff-only
# Use the existing environment's Python and the previously selected extras.
.venv/bin/python -m pip install -e '.[local,graph,apps,models-local]'
```

On Windows use `& .\.venv\Scripts\python.exe` for the final command. Stop
affected Daem0n processes before upgrading their environment or migrating their
storage; do not terminate unrelated Python or Claude processes.

### 2. Re-encode Embeddings (legacy format 6 only)

The embedding model changed from `all-MiniLM-L6-v2` (384-dim) to `nomic-ai/modernbert-embed-base` (256-dim).

> **Deprecated (format 6 only):** These legacy writers update the mutable
> `memories.vector_embedding` column or legacy Qdrant collections. They refuse
> an active format-7 database before making changes.

```bash
# Re-encode the format-6 SQLite column (and local Qdrant, when present)
python -m daem0nmcp.migrations.migrate_embedding_model --project-path /path/to/.daem0nmcp

# Copy existing format-6 SQLite vectors to legacy Qdrant
python -m daem0nmcp.migrations.migrate_vectors --project-path /path/to/project

# Backfill missing format-6 SQLite vectors
python -m daem0nmcp.cli --project-path /path/to/project migrate --backfill-vectors
```

For architecture format 7, rebuild the dense retrieval projection from canonical
events. Supply the registered workspace ID reported by the v7 migration/status
commands:

```bash
python -m daem0nmcp.cli rebuild-projection --projection dense --workspace-id <workspace-id>
```

### 3. Install Claude Code Hooks (Claude Code only)

```bash
python -m daem0nmcp.cli install-claude-hooks
```

Re-run this after upgrading. It replaces the hook commands earlier versions
wrote, including the `python -m` form that imports a `daem0nmcp/` directory
from any project you open. If you registered the stdio server at user scope as
`<PYTHON_PATH> -m daem0nmcp.server`, remove it with
`claude mcp remove daem0nmcp --scope user` and register the command under
[Installation by Platform](#installation-by-platform) instead. In repositories
where you ran `install-hooks`, re-run it as well: the git pre-commit hook it
wrote used `python -m` too.

### 4. Restart the selected host

After updating, restart/reconnect the selected CLI to load the new MCP binding
and tools, completing its normal trust prompt. Resume the grimoire's affected
protocol and persistence checks.

### 4b. Refresh project client configuration

```text
<VENV_PYTHON> -m daem0nmcp.cli --project-path "<PROJECT>" --json install-client <CLIENT> --client-version "<OBSERVED_VERSION>" --dry-run
```

For Codex, agy, OpenCode V1, omp or pi, inspect this dry-run with the same
project and selected host. Equivalent entries remain unchanged. A different
interpreter, stale root or custom existing binding may require a scoped,
backup-backed update after inspecting the reported conflict. Preserve unrelated
configuration and existing data. Do not replace the file with a template or run
the legacy OpenCode installer's `--force` as a shortcut.

### 5. Migrations

A format 7 database is never upgraded in place. When a newer build needs a
newer schema, the server and the legacy commands answer `MIGRATION_REQUIRED`
until `migrate-v7 --apply` has run; that command snapshots the store first and
can be rolled back. A retained format 6 database only receives format 6 schema
migrations. If `active-db.json` is lost, commands answer
`ACTIVE_POINTER_MISSING`; see `docs/v7-operations.md` for
`migrate-v7 --recover-pointer`.

Moving an existing v6 workspace to the v7 storage format is **not** automatic.
It is an offline, reversible command, and a v6 workspace answers
`MIGRATION_REQUIRED` until it has run:

```bash
# Stop the server first, then:
python -m daem0nmcp.cli --project-path /path/to/project migrate-v7 --apply

# Undo it; refuses if events were recorded since activation unless
# --discard-v7-writes is passed.
python -m daem0nmcp.cli --project-path /path/to/project migrate-v7 --rollback
```

### 6. Index Your Codebase

```bash
python -m daem0nmcp.cli index
```

Supports Python, TypeScript, JavaScript, Go, Rust, Java, C, C++, C#, Ruby, PHP via tree-sitter.

## Outcome-informed memory

`memory_store.informed_by` and `memory_record_outcome.informed_by` accept up to
32 existing record IDs in the same workspace. They record explicit provenance;
recall remains read-only. Outcomes may include
`verification={"kind": "test", "command": "pytest", "exit_code": 0}`; verification
is caller-reported evidence, not a command executed by the server.

Schema 33 derives learning signals and provenance from canonical events. The
v7 upgrader restores missing derived record parents only after complete canonical
authority replay succeeds, then backfills learning evidence in the same
transaction. Invalid authority fails closed; failed upgrades leave the active
database and pointer unchanged.

Utility ranking defaults to **shadow**: it reports estimates and diagnostics
without changing retrieval order. Shadow still computes those estimates and can
add database work and query latency; use `off` to skip that computation. Opt in
to ranking changes with:

```bash
DAEM0NMCP_RETRIEVAL_UTILITY_MODE=apply
DAEM0NMCP_RETRIEVAL_UTILITY_CREDIT=trace
```

`single_step` uses immediate outcome credit; `trace` also propagates bounded,
discounted credit through explicit provenance. The default ranking weight is
0.1. Set `DAEM0NMCP_RETRIEVAL_UTILITY_MODE=off` to disable computation. Failed
decisions remain warnings rather than being demoted by their own failures.
Utility only reorders candidates whose rank prior and fused score are within 5%
of each other, so outcome history breaks near-ties and never outranks a clearly
better match.

### Bind guidance to repository code

`memory_store.code_refs` accepts up to 16
`{"relative_file_path": "src/x.py", "qualified_name": "f"}` references.
Omit `qualified_name` to bind the whole file. Qualified names select a specific
symbol; a bare name binds every matching declaration in that file. Fingerprints
ignore CRLF/LF differences, and symbol bindings ignore edits to sibling symbols.
The server manages `context.code_bindings`; callers cannot set it themselves.

Recall reports `applicability` (`current`, `needs_revalidation`, or
`unverifiable`) and `changed_bindings`. Parser unavailability is not proof that
code changed. The default validity mode is `shadow`; set
`DAEM0NMCP_MEMORY_VALIDITY_MODE=apply` to include revalidation labels in context,
or `off` to skip checks. After checking changed code, record a successful
outcome with `rebind_code=true` to refresh existing bindings.

### Keep task-specific evidence under a context budget

`memory_recall` accepts an optional `intent`: `explore`, `implement`, `debug`,
or `review`. Implement/debug retention reserves room for procedures and matching
code bindings, with shorter ordinary excerpts; review also bounds stale guidance.
The default `DAEM0NMCP_RETRIEVAL_RETENTION_MODE=shadow` computes retention diagnostics
without changing context or ranking. Set it to `apply` to change evidence packing,
or `off` to skip computation. Without an intent, context packing is unchanged.
These settings also apply to linked-workspace recall.

### Optional ColBERT reranking

Install `pip install "daem0nmcp[late-interaction]"` and set:

```bash
DAEM0NMCP_RETRIEVAL_RERANK_ENABLED=true
DAEM0NMCP_RETRIEVAL_RERANKER=late_interaction
```

Request `memory_recall(..., rerank=true)`. The default model is
`colbert-ir/colbertv2.0`, configured by
`DAEM0NMCP_RETRIEVAL_LATE_INTERACTION_MODEL`. It loads lazily and may download
weights on first use; warm the model before measuring latency. Cold downloads
can exceed the provider timeout (`DAEM0NMCP_QDRANT_TIMEOUT_SECONDS`) and report
`RERANKER_FAILED` while preserving the original order. An unavailable profile
reports `RERANKER_UNAVAILABLE`; it never silently substitutes embedding reranking.

## Troubleshooting

### MCP Tools Not Available in Claude Session

**Symptom:** `claude mcp list` shows daem0nmcp connected, but Claude can't use `mcp__daem0nmcp__*` tools.

**Fixes:**

1. Inspect `claude mcp list` and `claude mcp get daem0nmcp` from the target
   project. Check the configured interpreter, transport and project root.
2. Restart Claude Code after configuration changes and complete its project
   server trust prompt. A running session does not automatically gain tools
   from a newly written configuration file.
3. For stdio, let Claude Code launch the configured process. For deliberately
   configured HTTP, start that server first and verify its `/mcp` endpoint with
   an MCP client. Do not switch transports just because the host is Windows.
4. Resume the grimoire's diagnostics; retain other MCP entries and inspect
   local/project/user scope conflicts before changing registration.

### Hooks Not Firing

1. Check the hook commands in Claude's actual user settings and any project
   settings; ensure their interpreter still exists. See the custom
   `CLAUDE_CONFIG_DIR` note above.
2. Check that the project has a `.daem0nmcp/` directory and that hook events
   identify the expected project.
3. Hooks remind; they do not block edits or write memories automatically.
   MCP server connectivity is a separate check.

### Covenant Errors

- `COMMUNION_REQUIRED` → Call `session_brief(workspace_id="<workspace_id>")` first
- `COUNSEL_REQUIRED` → Follow the returned remedy and call
  `memory_preflight(workspace_id="<workspace_id>", target_tool="<exact-tool>", target_arguments={<exact-arguments>})`

### OpenCode: Tools Not Found

**Symptom:** OpenCode reports "tool not found" when using `mcp__daem0nmcp__*` format.

**Fix:** OpenCode uses the exact single-underscore form
`daem0nmcp_session_brief`; Claude Code uses
`mcp__daem0nmcp__session_brief`. See [Tool Names by Client](#tool-names-by-client).

### OpenCode: MCP Connection Timeout

**Symptom:** First tool call times out, but subsequent calls work.

**Fix:** The embedding model loads on first call (several seconds). Add `"timeout": 30000` to your opencode.json MCP config.

### OpenCode: Covenant Not Enforced

**Symptom:** No preflight warnings, no post-edit suggestions in OpenCode.

**Fix:**
1. Verify plugin exists: `ls .opencode/plugins/daem0n.ts`
2. Verify AGENTS.md contains the Sacred Covenant section
3. Check OpenCode logs for plugin errors

## Development

```bash
# Install in development mode
pip install -e .[dev]

# Run tests (500+ tests)
pytest tests/ -v --asyncio-mode=auto

# Run server directly (stdio)
python -m daem0nmcp.server

# Run HTTP server (Windows)
python start_server.py --port 9876
```

### Graded coding-memory evaluation

```bash
python -m benchmarks.coding_memory_eval --mode lexical_only --topics 24 --seed 20261004 --output coding-eval.json
# Optional real-provider comparison (install local, models-local, graph and late-interaction):
python -m benchmarks.coding_memory_eval --mode fully_enabled --topics 24 --seed 20261004 --output coding-eval-full.json
```

This isolated synthetic workload exercises outcome reuse, multi-hop provenance,
symbol/file validity and budgeted procedure retention through the production
writer and recall service. The lexical mode requires no model downloads.
Event times are assigned in write order, not read from the wall clock, and the
seed only shuffles the order of the writes: every
seed reports the same metrics, so repeating a run with other seeds adds no
evidence. Latency measurements are wall-clock and excluded from deterministic
comparisons. Each run creates its own temporary directory and removes it when
it ends, so concurrent runs never share storage and a directory left by a
killed run does not block the next one.

Reports expose all arm settings. Utility weight is **0.2 in the evaluation**
(the product default stays 0.1). Retention distractors are same-category
background procedure drafts with task tags: this prevents category diversity
from automatically rescuing the actionable procedure and tests actual packing
under a 256-token budget. Every arm uses the product default for lexical content
tiering (off) except `tiering_apply` and `all_apply_tiering`, which turn it on:
with the tier the procedure already ranks first, so those two arms retain the
required fact without the retention policy and cannot show its effect. These
synthetic gains are not a competitor comparison.
The release-frozen retrieval corpus and its digest remain unchanged.

`bash autoresearch.sh` runs the same offline lexical workload with 24 topics,
seed 20261004 and a fixed event clock, emitting `METRIC coding_memory_ndcg`
(higher is better) plus ranking, retention, validity and token metrics. A fresh
offline MCP workspace also reports `listed_tool_count`, `guided_write_calls`,
`strict_explicit_write_calls` and `destructive_challenge_calls`. The strict
workload separately verifies rejection of an unbriefed direct write before
measuring the explicit briefing/preflight/write flow; destructive call counts
exclude the selection preview. It uses the project virtual environment and
excludes wall-clock latency.

## Support

If Daem0nMCP has been useful to you, consider supporting its development:

[![Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/vitruvianredux)

## Uninstallation

See `Banish_Daem0n.md` for complete removal instructions (covers both Claude Code and OpenCode), or quick version:

### Claude Code

```bash
python -m daem0nmcp.cli uninstall-claude-hooks
claude mcp remove daem0nmcp --scope user
pip uninstall daem0nmcp
rm -rf ~/Daem0n-MCP
rm -rf .daem0nmcp/
```

### OpenCode

```bash
rm opencode.json
rm -rf .opencode/commands/ .opencode/plugins/daem0n.ts
pip uninstall daem0nmcp
rm -rf ~/Daem0n-MCP
rm -rf .daem0nmcp/
```

---

```
    "The system learns from YOUR outcomes.
     Record them faithfully..."
                              ~ Daem0n
```

*Daem0nMCP v7 development: persistent memory, replay-safe decisions and outcomes,
workspace-scoped tools, and optional local search, models, graphs and visual
portals. Read the grimoire. Complete the ritual. Verify what awakened.*
