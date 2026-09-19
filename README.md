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

The public v7 MCP surface is strict and workspace-scoped. Begin with
`session_brief`, use `memory_recall` for bounded context, obtain an exact
`memory_preflight` capability before protected work, write decisions through
replay-safe `memory_store`, and finish with `memory_record_outcome`. Use
`system_health` for diagnostics.

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
`apps`, `models-local`, `models-hosted`, `agency-e2b`, or `observability`.
For example, `pip install "daem0nmcp[apps,graph]"`. The development and
compatibility profiles (`dev` and `tracing`) are not required by a normal MCP
server. `models-local` is optional and requires Python 3.11 or newer. Its
secure ONNX runtime dependency has no CPython 3.10 wheel. On Python 3.10, the
resolver omits this profile's dependencies and the runtime reports the
Python-version remediation while core installation remains supported.

The server owns workspace registration. Set `DAEM0NMCP_PROJECT_ROOT` to the
default workspace and, when needed, `DAEM0NMCP_WORKSPACE_ROOTS` to the allowed
additional roots. Operators derive the stable opaque ID from that configured
root and provide it in the protected client configuration or pairing flow;
`session_brief` then confirms that ID. Clients do not choose filesystem paths. Leave
`DAEM0NMCP_STORAGE_PATH` unset when serving multiple registered workspaces.

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

The complete setup, exact preflight examples, migrations, native client
installers, diagnostics, and all **75** registered v7 tools are in
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
| `session_start` | Suggest scoped `session_brief` at session dawn |
| `pre_edit` | Fail closed with exact `memory_preflight` guidance |
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

### Claude Code (The Easy Way)

1. Copy `Summon_Daem0n.md` to your project
2. Start a Claude Code session in that project
3. Claude will read the file and perform the summoning ritual automatically

### OpenCode

1. Install Daem0n-MCP: `pip install -e ~/Daem0n-MCP`
2. Run the installer: `python -m daem0nmcp.cli install-opencode`
3. Launch OpenCode in your project directory
4. Run `/commune <workspace_id>`; this host shortcut calls `session_brief`

For the full ritual walkthrough, see `Summon_Daem0n_OpenCode.md`.

### Manual Installation

```bash
# Clone the repository
git clone https://github.com/9thlevelsoftware/Daem0n-MCP.git ~/Daem0n-MCP

# Install
pip install -e ~/Daem0n-MCP

# Run the MCP server (Linux/macOS — stdio transport)
python -m daem0nmcp.server

# Run the MCP server (Windows — HTTP transport required)
python ~/Daem0n-MCP/start_server.py --port 9876
```

## Installation by Platform

### Linux / macOS (stdio transport)

```bash
# Find your Python path
python3 -c "import sys; print(sys.executable)"

# Register with Claude Code (replace <PYTHON_PATH>)
claude mcp add daem0nmcp --scope user -- <PYTHON_PATH> -m daem0nmcp.server

# Restart Claude Code
```

### Windows (HTTP transport required)

Windows has a known bug where Python MCP servers using stdio transport hang indefinitely. Use HTTP transport instead:

1. **Start the server** (keep this terminal open):
```bash
python ~/Daem0n-MCP/start_server.py --port 9876
```
Or use `start_daem0nmcp_server.bat`

2. **Add to `~/.claude.json`**:
```json
{
  "mcpServers": {
    "daem0nmcp": {
      "type": "http",
      "url": "http://localhost:9876/mcp"
    }
  }
}
```

3. **Start Claude Code** (after server is running)

### Transport Modes

| Method | Transport | Default Port | Use Case |
|--------|-----------|-------------|----------|
| `python -m daem0nmcp.server` | `stdio` | n/a | Local process channel |
| `python start_server.py` | Streamable HTTP at `/mcp` | 9876 | Windows HTTP, remote access |

For OpenCode setup, see the [OpenCode Integration](#opencode-integration) section below, or run `python -m daem0nmcp.cli install-opencode` for automated setup.

## v7 MCP API

The v7 public API uses strict, workspace-scoped tools rather than action
dispatch. The core protocol surface is:

| Tool | Purpose |
|---|---|
| `session_brief` | Establish the server-issued session scope and return a bounded briefing |
| `memory_preflight` | Recall guidance and issue a capability for one exact protected request |
| `memory_recall` | Retrieve bounded, evidence-bearing records |
| `memory_store` | Append one replay-safe memory record |
| `memory_record_outcome` | Append a verified success or failure outcome |
| `system_health` | Return bounded service and projection diagnostics |

The read-only JSON resources are:

- `memory://workspaces/{workspace_id}/warnings`
- `memory://workspaces/{workspace_id}/failures`
- `memory://workspaces/{workspace_id}/rules`
- `memory://workspaces/{workspace_id}/active-context`

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

Host metadata is never an identity source. Authentication comes from the
transport, and every scoped request is resolved through `workspace_id`.

## Usage Examples

### Session Start

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

First request a capability bound to the exact target arguments:

```text
memory_preflight(
    workspace_id="<workspace_id>",
    target_tool="memory_store",
    target_arguments={
        "record_type": "decision",
        "content": "Use signed session cookies",
        "rationale": "Avoid server-side session state",
        "tags": ["auth", "architecture"],
        "relative_file_path": "src/auth/session.py",
        "idempotency_key": "decision-auth-session-0001"
    }
)
```

Then use the same arguments and returned token:

```text
memory_store(
    workspace_id="<workspace_id>",
    record_type="decision",
    content="Use signed session cookies",
    rationale="Avoid server-side session state",
    tags=["auth", "architecture"],
    relative_file_path="src/auth/session.py",
    idempotency_key="decision-auth-session-0001",
    preflight_token="<token-from-memory_preflight>"
)
```

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
session_brief
    -> memory_recall (when relevant)
    -> memory_preflight (exact protected tool + exact arguments)
    -> protected tool with preflight_token
    -> memory_store (for durable decisions, replay-safe)
    -> memory_record_outcome (after verification, replay-safe)
```

Use `system_health` for bounded diagnostics. The complete protocol lives in
`AGENTS.md` and `.claude/skills/daem0nmcp-protocol/SKILL.md`.

## Claude Code Integration

### Hook Installation

```bash
python -m daem0nmcp.cli install-claude-hooks
```

This writes six hook entries to the user-level `~/.claude/settings.json`, so
they run in every project; each runs `"<python>" -m daem0nmcp.claude_hooks.<hook>`
with the interpreter you installed from, and stays silent in directories without
`.daem0nmcp/`. Other hooks in that file are kept, and any old
`hooks/daem0n_*.py` entries are removed.

| Event (matcher) | Hook | What it does | Input | Reminds only |
|-----------------|------|--------------|-------|--------------|
| `SessionStart` | `session_start` | Adds a line to the model's context asking it to call `session_brief` with this workspace's `workspace_id` | `CLAUDE_PROJECT_DIR` env | Yes |
| `PreToolUse` (`Edit\|Write\|NotebookEdit`) | `pre_edit` | In a project with `.daem0nmcp/`, adds a one-line `additionalContext` reminder to call `memory_recall_file` for the file and `memory_preflight` for the change; silent elsewhere | stdin event | Yes |
| `PreToolUse` (`Bash`) | `pre_bash` | Checks the command against Daem0n rules. Currently inert: it reads a `TOOL_INPUT` env var that Claude Code does not set | `TOOL_INPUT` env | Yes (always exits 0) |
| `PostToolUse` (`mcp__.*__edit_preflight`) | `post_edit_preflight` | Edit-bridge plumbing: stages an `edit_preflight` receipt for a bridge edit request. Nothing in Claude Code creates those requests any more, so it is a no-op | stdin event | Yes |
| `PostToolUse` (`Edit\|Write\|NotebookEdit`) | `post_edit` | Edit-bridge plumbing: reports a bridge-approved edit as a capture candidate. Only loads the bridge when the project is paired; a no-op in Claude Code today | stdin event | Yes |
| `Stop`, `SubagentStop` | `stop` | When the transcript shows a finished task with no recorded outcome, shows you suggested `memory_preflight`/`memory_store`/`memory_record_outcome` calls as a `systemMessage`. Never writes memory | stdin event (`transcript_path`) | Yes |

No hook blocks a tool call or keeps the agent running: every hook exits 0 and none
returns a `permissionDecision` or `decision`.

**What pairing writes.** The installer also pairs the current project (or
`--project-path`) with the local edit bridge:

- an owner-only credential at `~/.daem0nmcp/edit-bridges/<authority>/credential.json`
  (the secret is never written into the project), with the bridge socket under
  `~/.daem0nmcp/edit-bridges/run/<authority[:16]>/`;
- the env keys `DAEM0NMCP_EDIT_BRIDGE_CREDENTIAL_FILE`,
  `DAEM0NMCP_EDIT_BRIDGE_RUNTIME_DIR`, `DAEM0NMCP_PROJECT_ROOT` and
  `DAEM0NMCP_STORAGE_PATH` in `<project>/.claude/settings.local.json`.

To remove the hooks, and optionally the project's pairing and credential:

```bash
python -m daem0nmcp.cli --project-path <project> uninstall-claude-hooks [--remove-credentials]
```

The old manual scripts in `hooks/` are deprecated stubs that only print a
notice and exit 0. If your settings still point at them, re-run
`install-claude-hooks`.

### Protocol Skill

A Claude Code skill is included at `.claude/skills/daem0nmcp-protocol/SKILL.md` that enforces the memory protocol automatically.

## OpenCode Integration

OpenCode connects to Daem0n-MCP via the same MCP server. No server changes required -- the daemon serves all clients equally.

### Configuration

Create `opencode.json` at your project root:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "daem0nmcp": {
      "type": "local",
      "command": ["python", "-m", "daem0nmcp"],
      "enabled": true,
      "environment": {
        "PYTHONUNBUFFERED": "1"
      }
    }
  }
}
```

For Windows (where HTTP transport is required), use:

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

OpenCode reads `AGENTS.md` from the project root for system prompt injection. The Sacred Covenant protocol is included in `AGENTS.md` with OpenCode-compatible tool names.

If both `AGENTS.md` and `CLAUDE.md` exist in a project, OpenCode uses only `AGENTS.md`.

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

## Configuration

All settings are configurable via environment variables with `DAEM0NMCP_` prefix.

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

See `Summon_Daem0n.md` for the complete configuration reference (~50 settings).

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
├── bm25_index.py        # BM25 Okapi keyword retrieval
├── fusion.py            # Reciprocal Rank Fusion for hybrid search
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
│   ├── pre_edit.py      # Fail-closed v7 preflight guidance
│   ├── pre_bash.py      # Rule enforcement on commands
│   ├── post_edit.py     # Significance detection
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

Summon_Daem0n.md              # Claude Code installation grimoire
Summon_Daem0n_OpenCode.md     # OpenCode installation grimoire
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

### Hook & Enforcement Commands

```bash
# Install Claude Code hooks (user-level, all projects)
python -m daem0nmcp.cli install-claude-hooks [--dry-run]

# Remove Claude Code hooks
python -m daem0nmcp.cli uninstall-claude-hooks [--dry-run]

# Install OpenCode configuration (project-level)
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

### 1. Update the Code

```bash
# If installed from source (recommended)
cd ~/Daem0n-MCP && git pull && pip install -e .

```

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

### 3. Install Claude Code Hooks

```bash
python -m daem0nmcp.cli install-claude-hooks
```

### 4. Restart Claude Code

After updating, restart Claude Code to load the new MCP tools.

### 4b. Update OpenCode Configuration (if using OpenCode)

```bash
python -m daem0nmcp.cli install-opencode --force
```

This regenerates `opencode.json` and the plugin to match the latest version.

### 5. Migrations Run Automatically

Database schema migrations are applied automatically when any MCP tool runs. No manual migration step required.

### 6. Index Your Codebase

```bash
python -m daem0nmcp.cli index
```

Supports Python, TypeScript, JavaScript, Go, Rust, Java, C, C++, C#, Ruby, PHP via tree-sitter.

## Troubleshooting

### MCP Tools Not Available in Claude Session

**Symptom:** `claude mcp list` shows daem0nmcp connected, but Claude can't use `mcp__daem0nmcp__*` tools.

**Fixes:**

1. **Start server before Claude Code (Windows):**
   ```bash
   # Terminal 1: Start Daem0n server first
   python ~/Daem0n-MCP/start_server.py --port 9876
   # Wait for "Uvicorn running on http://localhost:9876"

   # Terminal 2: Then start Claude Code
   claude
   ```

2. **Re-register the server:**
   ```bash
   claude mcp remove daem0nmcp -s user
   claude mcp add daem0nmcp http://localhost:9876/mcp -s user
   ```

### Hooks Not Firing

1. MCP server running: `curl http://localhost:9876/mcp` should respond
2. Hooks configured: check `~/.claude/settings.json` or `.claude/settings.json`
3. Project has `.daem0nmcp/` directory

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

*Daem0nMCP v6.6.6: ModernBERT Deep Sight (256-dim Matryoshka, ONNX quantized, asymmetric encoding). 8 workflow tools with 59 actions + 3 cognitive tools (simulate_decision, evolve_rule, debate_internal). Background Dreaming, Auto-Zoom Retrieval Routing, Active Context, Visual Portals (MCP Apps), GraphRAG, bi-temporal knowledge, LLMLingua-2 compression. Claude Code native hooks + OpenCode integration (plugins, commands, covenant enforcement). 500+ tests. The daemon reaches beyond the veil.*
