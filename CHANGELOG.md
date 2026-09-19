# Changelog

## [7.0.0-dev.0] - In progress

### Added
- Reproducible v7 core foundation with opt-in dependency profiles and lazy capability reporting.
- `uninstall-claude-hooks` strips the pairing env keys from the project's `.claude/settings.local.json` (the project is `--project-path` or the current directory, and only a project the installer paired is touched); `--remove-credentials` also deletes its local edit-bridge credential and socket directories.

### Changed
- Claude Code hooks remind instead of block. `pre_edit` adds a one-line `memory_recall_file`/`memory_preflight` reminder to the model's context and never denies an edit; `stop` reads its stdin event, shows suggestions as a `systemMessage` and never keeps the agent running.
- OpenCode edits are no longer gated either: the OpenCode plugin runs the same `pre_edit` hook, which now always allows the edit. The plugin does not yet show the reminder; that rework comes with the edit-bridge removal.
- `install-claude-hooks` and `uninstall-claude-hooks` refuse to touch a `~/.claude/settings.json` that is not valid JSON, instead of replacing it.
- The manual `hooks/daem0n_*.py` scripts are deprecated stubs that print a notice and exit 0, and `hooks/settings.json.example` is removed. If you installed them by hand, re-run `python -m daem0nmcp.cli install-claude-hooks`; it also removes the old entries.

## [6.0.0] - 2026-01-29

### Breaking Changes
- Legacy individual MCP tools removed from registration — all capabilities flow through 8 workflow tools + 3 cognitive tools
- Server decomposed from monolithic `server.py` (6,467 lines) into 149-line composition root + 15 tool modules under `daem0nmcp/tools/`

### Added
- **Auto-Zoom Retrieval Router**: Query-aware search dispatch (SIMPLE → vector, MEDIUM → hybrid, COMPLEX → GraphRAG) with shadow mode and fallback guarantees
- **JIT Compression**: Tiered automatic compression of retrieval results (soft >4K, hard >8K, emergency >16K tokens)
- **Background Dreaming**: Idle-triggered `FailedDecisionReview` with cooperative yielding and dream insight persistence
- **Cognitive Tools**: 3 new standalone MCP tools — `simulate_decision` (temporal scrying), `evolve_rule` (rule entropy), `debate_internal` (adversarial council)
- **Code-Augmented Reflexion**: Python assertion generation and sandboxed execution with failure classification

## [5.1.0] - 2026-01-26

### Breaking Changes
- 67 individual MCP tools consolidated into 8 workflow-oriented tools
- Visual tools no longer standalone — accessed via `visual=True` parameter on workflow tools

### Changed
- All capabilities accessed through `commune`, `consult`, `inscribe`, `reflect`, `understand`, `govern`, `explore`, `maintain` workflow tools
- Each workflow tool accepts an `action` parameter to select the operation
- 60 total actions across 8 workflows

## [5.0.0] - 2026-01-24

### Added
- **MCP Apps (SEP-1865)**: 6 interactive HTML visual portals — Search Results, Briefing Dashboard, Covenant Status, Community Cluster Map, Memory Graph Viewer, Real-Time Updates
- D3.js self-contained bundle (105KB, no CDN dependencies)
- SecureMessenger with origin-validated iframe communication
- CSP security (`default-src 'none'`)
- Text fallback for non-MCP-Apps hosts
- 6 new visual tools: `recall_visual`, `get_briefing_visual`, `get_covenant_status_visual`, `list_communities_visual`, `get_graph_visual`, `check_for_updates`

## [4.0.0] - 2026-01-22

### Added
- **GraphRAG & Leiden Communities**: Entity extraction, NetworkX graph, hierarchical community detection, multi-hop queries, global search
- **Bi-Temporal Knowledge**: Dual timestamps (`valid_time`/`transaction_time`), `happened_at` parameter, point-in-time queries, contradiction detection
- **Metacognitive Architecture (Reflexion)**: Actor-Evaluator-Reflector loop via LangGraph, `verify_facts` tool, chain of verification
- **Context Engineering**: LLMLingua-2 integration (3x-6x compression), code entity preservation, `compress_context` tool
- **Dynamic Agency**: Ritual phase tracking, tool masking, `execute_python` sandboxed execution via E2B
- 7 new tools: `verify_facts`, `compress_context`, `execute_python`, `trace_chain`, `trace_evolution`, `get_related_memories`, `get_graph_stats`

## [3.0.0] - 2026-01-20

### Breaking Changes
- Requires FastMCP 3.0.0b1+ (import path changed from `mcp.server.fastmcp` to `fastmcp`)
- Covenant decorators deprecated (use CovenantMiddleware)

### Added
- CovenantMiddleware: Middleware-style covenant enforcement via FastMCP 3.0
- CovenantTransform: Transform for tool access checking
- Component versioning for all 53 MCP tools
- OpenTelemetry tracing support (optional, install with `pip install daem0nmcp[tracing]`)

### Changed
- Import from `fastmcp` instead of `mcp.server.fastmcp`
- Covenant enforcement now uses FastMCP 3.0 Middleware pattern alongside decorators
- Tool decorators now include `version="3.0.0"` metadata

### Deprecated
- `@requires_communion` decorator (use CovenantMiddleware)
- `@requires_counsel` decorator (use CovenantMiddleware)

## [2.16.0] - 2025-12-XX

### Added
- Sacred Covenant enforcement for session discipline
- MCP resources support
- Claude Code compatibility improvements
