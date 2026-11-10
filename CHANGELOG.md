# Changelog

## [7.0.0-dev.0] - In progress

### Added
- Reproducible v7 core foundation with opt-in dependency profiles and lazy capability reporting.
- `uninstall-claude-hooks` strips the pairing env keys from the project's `.claude/settings.local.json` (the project is `--project-path` or the current directory, and only a project the installer paired is touched); `--remove-credentials` also deletes its local edit-bridge credential and socket directories.
- Schema 33 projects explicit `informed_by` memory provenance and verification-weighted outcomes. Existing native outcomes are backfilled during upgrade; legacy imports do not invent learning signals. `memory_store` and `memory_record_outcome` accept the new evidence fields without changing idempotency hashes for unchanged requests.
- Shadow-first utility ranking with immediate or discounted provenance credit, repository code-binding validity and successful-outcome rebinding, and intent-aware evidence retention across direct and linked-workspace recall.
- Optional Python 3.11+ `late-interaction` profile for lazy ColBERT/MaxSim reranking. Missing profiles report unavailable instead of falling back to embedding reranking.
- Graded coding-memory evaluation with deterministic lexical fixtures and optional real-provider arms, reporting ranking quality, validity, retained procedure facts, tokens and latency without changing the frozen retrieval benchmark.
- Canonical offline `bash autoresearch.sh` harness reports coding-memory nDCG and deterministic quality metrics using the existing fixed lexical workload, plus real MCP tool-list and guided, strict and destructive-challenge call counts.

### Changed
- Guided covenant defaults: the first gated call attaches a compact automatic brief; benign protected calls proceed with inline counsel, while relevant risk and every destructive operation return guidance and an exact retry capability. `DAEM0NMCP_COVENANT_MODE=strict` restores explicit ceremony; tokens retain exact binding, single use and 300-second expiry.
- Core-plus-gateway listing defaults to nine tools, with all 77 still registered and callable. `daem0n_tools_search` discovers hidden schemas and `daem0n_tool_call` relays typed user text, bounded argument diagnostics and callable challenge remedies; `DAEM0NMCP_TOOL_SURFACE=full` lists every tool.
- Discovery reads return retryable `DATABASE_IN_USE` for SQLite contention, including lexical catch-up checks, and healthy graph-generation advances during recall. Invalid schemas, corrupt projections and permanent retrieval failures remain non-retryable.
- Intent-aware evidence retention matches full or boundary-suffix paths and qualified symbols without promoting unrelated same-named evidence. Query path spellings normalize Windows separators and repeated slashes while preserving explicit `./` root scope; bare names remain broader.
- Lexical recall can rank content/rationale matches ahead of tag-assisted matches within the existing query ladder. This content tier is opt-in through `DAEM0NMCP_LEXICAL_CONTENT_TIERING=true`; the default is weighted BM25 over content, rationale and tags. Content-tier ranking excludes tag frequency while metadata-only discovery and original diagnostic BM25 scores remain unchanged; no index rebuild is required.
- Code-binding validity re-reads bounded source content, detecting same-size timestamp-preserving edits. Recall batches share resolved-file reads under a 32 MiB raw-byte budget (plus one overflow probe); excess remains honestly `unverifiable`, and parsed source caching is capped at 32 MiB and 256 files.
- Symbol-binding fingerprints include AST-owned decorators, including multiline Python wrappers and TypeScript decorator/comment siblings, without changing code-navigation spans or projection identities. Older decorated bindings conservatively require revalidation once.
- Citation-lookalike text is neutralized in federated excerpts and revalidation labels, preventing forged context citations and direct-recall denial while preserving canonical content and raw binding paths.
- Retained federated evidence and citation manifests export channel arrays in canonical order, eliminating hash-seed-dependent payload drift across server restarts.
- Explore-intent shadow retention reuses the identical primary composition in direct and federated recall, avoiding duplicate packing while preserving evidence and diagnostics.
- Symbol-bound writes distinguish unavailable or failed parsing (`CAPABILITY_DEGRADED`, retryable) from invalid symbol references (`INVALID_ARGUMENT`), preserving atomic capture and replay-safe outcome rebinding.
- Symbol bindings prefer exact qualified identities and reject ambiguous short names instead of hashing unrelated definitions together; existing aliases that become ambiguous are `unverifiable`.
- Binding checks report oversized or environmentally inaccessible source as `unverifiable`, not inferred changed content. Definite missing files, directory replacements and workspace escapes still require revalidation; environmental capture failures are retryable capability errors.
- Public retrieval envelopes enforce exact ordered citation-marker parity with their manifests, rejecting missing, repeated, reordered and unbacked citations instead of accepting forged rendered context.
- Intent-aware context packing applies source-specific excerpt limits consistently to structured outcomes and each procedure step, preventing long stale facts from bypassing body limits while preserving step counts and no-intent/explore behavior.
- Unchanged binding sources reuse cache-owned bytes within each recall batch after exact verification, avoiding repeated full-file comparisons without skipping subsequent-recall reads or path checks.
- Binding validation shares parsed symbol spans and line offsets across symbols in an unchanged file, retaining exact fingerprints and parser-availability checks. Root-scoped caches prevent package-initializer qualifications leaking between nested workspaces.
- Store and outcome binding capture share one fresh bounded source snapshot per resolved file, preventing mixed-version symbol sets and duplicate reads/parses while preserving per-reference containment checks and later-call freshness.
- Gateway calls sanitize malformed adapter/service responses as `INTERNAL_ERROR` while preserving `INVALID_ARGUMENT` for caller input errors, single-pass input validation and user-authored path text.
- Discovery contention detection uses SQLite BUSY/LOCKED codes with a narrow code-less fallback, avoiding retryable errors for unrelated failures mentioning “busy” or “locked”.
- Explore recall now labels enabled retention `RETENTION_NOT_APPLICABLE`, preserving its baseline evidence and token usage rather than implying alternate packing.
- Schema 33 upgrades recover missing derived learning parents from complete validated canonical replay before strict backfill, preserving transactional rollback and fail-closed authority validation. Malformed record payloads surface the schema-upgrade error contract instead of raw key/type exceptions.
- Entity recall preserves retryable lexical catchup when a generation publishes between retrieval and readiness inspection, avoiding terminal capability errors for evidence that becomes available immediately after the query. Same-generation indexed misses remain terminal.
- Local Qdrant collection deletion closes persistence before dropping ownership, including dense staging cleanup, retired-generation garbage collection, portable import cleanup and legacy dimension replacement. Windows storage files and directories are released immediately after normal shutdown rather than waiting for cyclic garbage collection.
- Claude Code hooks remind instead of block. `pre_edit` adds a one-line gateway file-recall reminder; `stop` suggests direct replay-safe memory calls with counsel-challenge retry guidance. Neither hook writes memory, denies an edit, or keeps the agent running.
- OpenCode edits are no longer gated either: the OpenCode plugin runs the same `pre_edit` hook, which now always allows the edit. The plugin does not yet show the reminder; that rework comes with the edit-bridge removal.
- `install-claude-hooks` and `uninstall-claude-hooks` refuse to touch a `~/.claude/settings.json` that is not valid JSON, instead of replacing it.
- The manual `hooks/daem0n_*.py` scripts are deprecated stubs that print a notice and exit 0, and `hooks/settings.json.example` is removed. If you installed them by hand, re-run `python -m daem0nmcp.cli install-claude-hooks`; it also removes the old entries.
- **Behaviour change — installed profiles now turn themselves on.** An unset (or
  empty) `DAEM0NMCP_<PROFILE>_ENABLED` means "auto": the profile is ready when
  its extra is installed and disabled otherwise. `false` still turns it off, and
  `true` with packages missing is still degraded. An empty value used to mean
  `false`. What this costs if you installed an extra and never set the flag:
  `[graph]` imports networkx, igraph and leidenalg at startup, adds a graph
  provider to recall (so rankings change) and lets dreaming refresh communities;
  `[local]`/`[models-local]` import sentence-transformers and onnxruntime at
  startup, download the embedding model on first use, and rebuild the dense
  projection, which re-embeds the whole workspace once. Opt out per profile with
  `DAEM0NMCP_<PROFILE>_ENABLED=false`.
- `sandbox_execute_python` now honours `DAEM0NMCP_AGENCY_E2B_ENABLED=false`; it
  previously required only `E2B_API_KEY`.
- `system_health` and `meta.capability_states` report the profile's real
  remediation (install command or environment variable) instead of "Review the
  &lt;name&gt; capability profile."
- `start_daem0nmcp_server.bat` serves the directory you run it from (or its first
  argument, or an exported `DAEM0NMCP_PROJECT_ROOT`) instead of the Daem0n-MCP
  checkout, and `install-opencode` installs the four `.opencode/commands/` files.
- **Migration payload change — re-migrate a store migrated by an earlier
  v7 development build.** `migrate-v7` no longer copies a v6 host path into the
  event it writes: the columns that hold one (`file_path`, `project_path`,
  `database_path`, and a `file_path_relative` that is not workspace-relative)
  are recorded as a `{"$host_path": {...}}` digest, so an export bundle from a
  migrated workspace no longer discloses the origin host's directory layout.
  The raw value stays in the retained v6 tables and in
  `.daem0nmcp/storage/migrations/v7/<id>/source.snapshot.db`. Because the
  digest changes the lossless payload, a store migrated before this build fails
  `verify-v7` with "retained legacy source provenance differs" and must be
  migrated again from its intact v6 source (`migrate-v7 --rollback`, then
  `migrate-v7 --apply`). v7 is unreleased, so this affects development stores
  only.
- **Migrated v6 workspaces are usable end to end.** `session_brief`, recall,
  outcomes, the four resources and `workspace_export` all work on a migrated
  store; `migrate-v7` reports in `warnings` whatever it could not copy
  faithfully; an un-migrated v6 workspace answers the new non-retryable
  `MIGRATION_REQUIRED` naming the command instead of a retryable error with no
  remedy; and `migrate-v7 --rollback` refuses with `ROLLBACK_WOULD_HIDE_WRITES`
  rather than silently stranding writes made since activation (pass
  `--discard-v7-writes` to accept that).
- Removed the unused v6 BM25 modules (`daem0nmcp/bm25_index.py`, `daem0nmcp/fusion.py`) and the `rank-bm25` dependency from the `local` extra; v7 lexical retrieval is SQLite FTS5 and nothing imported them. The `bm25_k1`/`bm25_b` settings are still accepted and ignored.

### Security
- Hooks, the OpenCode plugin, the git pre-commit hook and the documented server registrations no longer launch with `python -m`, which imported a `daem0nmcp/` package from the opened project. The OpenCode plugin no longer takes its interpreter from the project's `opencode.json`. Existing installs keep the old command until `install-claude-hooks` and `install-opencode` are re-run (delete `.opencode/plugins/daem0n.ts` first if it is reported as outdated); SessionStart shows a reminder until then. The launch command does not alter `PYTHONPATH`: an empty or relative entry there still exposes the working directory.
- Scanned TODO comments and URL-ingested pages, including those migrated from v6, are withheld from the session brief and from the warnings, failures and active-context resources. They are reported as a `withheld_untrusted` count beside a notice that record text is data, not instructions, and remain available through `memory_recall`. Any record carrying the same markers (`context.code_todo`, `context.document`, `context.source_url` or the `auto_scanned` tag) is treated the same way.
- Settings are no longer read from a `.env` in the working directory, and FastMCP no longer loads `FASTMCP_*` settings from one. Set `DAEM0NMCP_ENV_FILE` (or `FASTMCP_ENV_FILE`) to an absolute path to load a settings file; a relative or missing `DAEM0NMCP_ENV_FILE` is refused at startup.
- A loopback HTTP listener configured with `DAEM0NMCP_TRUSTED_PROXY_IPS` or a non-loopback `DAEM0NMCP_ALLOWED_HOSTS` entry refuses to start without a production token verifier (`PROXIED_LISTENER_REQUIRES_AUTH`). Plain loopback HTTP still trusts every local process and user; the documentation now says so.
- The remote edit-bridge client no longer follows redirects or uses environment proxies, and `FASTMCP_SERVER_AUTH_JWT_JWKS_URI` must be `https://` (plain `http://` only for loopback). Documentation no longer describes workspace IDs as unguessable: they are a truncated hash of the root path, and grants decide access.
- One client can no longer exhaust covenant capabilities or scopes for everyone: consumed capability handles are released immediately, live handles and scopes are bounded per session and per principal, and exhausted capacity is reported as a retryable `CAPABILITY_DEGRADED` instead of `COUNSEL_REQUIRED` without a challenge. A retried remedy stays `TOKEN_REPLAYED` for its full lifetime.
- Scanned or ingested records can no longer force, or hide, covenant counsel: `RELATED_WARNING` and `RELATED_FAILURE` come only from records the workspace's callers stored.
- Guidance returned by `memory_preflight` and in `COUNSEL_REQUIRED` counsel withholds scanned TODO comments and URL-ingested pages as the brief does, reporting them in `guidance.withheld_untrusted` beside `guidance.content_notice`; they remain available through `memory_recall`.
- Resource reads return the same JSON-RPC error (`-32002`, `Resource unavailable`) whether the workspace is unknown, unauthorised or not yet briefed, and the admission limits no longer distinguish a registered workspace from an unknown one for a caller without access.
- Rejected tool arguments are described by field path and rule without echoing caller input, including unknown tool and prompt names.

### Fixed
- Opening a format 7 database no longer upgrades its schema in place. The server, the legacy commands and the generic `migrate` command answer `MIGRATION_REQUIRED` and name `migrate-v7 --apply`; `migrate` on a retained format 6 store stops at the last format 6 schema version.
- Rolling back a v7 schema upgrade or projection repair restores the predecessor's own `active-db.json`, so the workspace starts again and `migrate-v7 --apply` can be re-run. Rollback can unwind the run beneath a rolled-back schema upgrade, and an interrupted reactivation is completed by re-running `migrate-v7`.
- A format 7 store whose `active-db.json` was lost is no longer opened or re-migrated as format 6. Commands stop with `ACTIVE_POINTER_MISSING`; `migrate-v7 --recover-pointer` republishes the pointer when the default database is provably the active one, and otherwise the pointer must be restored from a backup.
- Prune, duplicate cleanup and dream purge close the relationships of the records they delete (cleanup moves them to the kept record), and graph reads skip relationships left dangling by earlier versions, naming them in a warning, instead of returning `CAPABILITY_DEGRADED` for the whole workspace. Maintenance can delete records that carry a migrated self-relationship.
- `memory_prune` keeps any memory that a later store or outcome names in `informed_by`, and its preview reports how many records that rule and the migrated recall counter each kept. v7 does not count recalls, so `min_recall_count` only applies to counters carried over by migration or import.
- Dense retrieval no longer goes down until restart with embedded Qdrant: recall, projection rebuilds, generation GC and portable vector transfer share one client per storage folder. A folder held by another process is reported as `DENSE_STORAGE_LOCKED` and retried instead of dead-lettering the rebuild job, and a manual dense rebuild reports the dense failure code instead of `PROJECTION_REBUILD_FAILED`.
- Dense retrieval no longer rehashes the workspace event log on every query. Events that change no record content (pin, archive) bring the active dense generation up to date in place, after verifying every stored vector, instead of rebuilding it. Dense still reports `DENSE_REBUILD_REQUIRED` between a memory write and the next dense job.
- Lexical recall falls back to single-term matches when a query of three or more terms has no stricter match (ranked below other providers' hits), and control characters or terminal escape sequences in a query are separators instead of `INVALID_FTS_QUERY`.
- Recall filters (categories, tags, record IDs) are applied inside the lexical candidate query instead of after an unfiltered top-N. A query with no searchable term plus such a filter lists the filtered records most recently changed first. Archived records no longer occupy lexical candidate slots or make `memory_search_text` fail.
- A backward or forward wall-clock step no longer blocks guided writes or changes the 300 second lifetime of a preflight capability; lifetimes are measured on a monotonic clock.
- The CI type check passes under pydantic 2.14, and three legacy test modules skip instead of failing collection when `sentence-transformers` is not installed.
- Recall runs the dense provider alongside the temporal, procedure and outcome providers instead of awaiting every optional provider in turn, so a slow dense search no longer adds to their time.
- Recall category diversity is bounded: a hit from a new category must rank within twice the limit, such hits take at most a third of the result slots, and results are returned in rank order instead of pick order.
- Token budgets hold for text without word spaces: the core tokenizer counts each character of CJK, kana, Hangul, Thai and the other unsegmented scripts as a token instead of one token per unbroken run. Counts for other scripts are unchanged.
- A path or symbol at the end of a sentence keeps its code-binding affinity during recall packing; the trailing full stop is no longer part of the identifier.
- The pooled ONNX encoder identity covers only the graph, its external tensor files and the tokenizer files, taken after the tokenizer is in place. A first cold load no longer fails and unrelated downloads into the model snapshot no longer invalidate dense projections. Installs whose snapshot held other files rebuild dense once (`rebuild-projection --projection dense`).
- Utility ranking reorders only candidates of near-equal relevance (rank prior and fused score within 5%), so outcome history can no longer lift a distant match over the best match or sink a strong match at any weight. In shadow mode the diagnostic can read `UTILITY_SHADOW_SAME` where it read `UTILITY_SHADOW_REORDER`.
- Single-step utility credit is scaled only by the direct and first-hop outcomes that produced it, and utility evidence for all recall candidates is read in a bounded number of indexed set-based queries instead of several queries per candidate.
- Rust symbol bindings fingerprint the item's outer attributes (`#[inline]`, `#[cfg(...)]`, `#[derive(...)]`), so changing one requires revalidation; older bindings on attributed Rust items require revalidation once.
- Class-qualified binding names (`AuthService.refresh`) resolve without the module prefix when they name one identity in the file, for Python, TypeScript/JavaScript, Java, Kotlin and Rust inherent impls. A name that is also another symbol's full identity is rejected as ambiguous, and a stored binding that becomes so reads `unverifiable`.
- Code binding paths are normalized when captured in-process and when read (Windows separators, `./`, repeated and trailing slashes), so a binding stored with Windows separators no longer reads as changed on POSIX. Drive, UNC, rooted and `..` paths remain rejected, and the tool boundary still requires normalized paths.
- The coding-memory evaluation no longer repeats retention queries with different expected records (which capped overall nDCG at 0.85), counts a required fact only when it is rendered from the expected procedure, asks validity queries as questions, accepts at most 24 topics and runs in a unique temporary directory. Every arm now runs at the product default (content tiering off) with the tier in separate `tiering_apply` and `all_apply_tiering` arms; the report states that its seed only shuffles write order.
- Terminal escape stripping in recall queries covers two-character escapes with a lowercase final byte. The in-place dense catch-up no longer applies to generations without a vector space hash, and the pooled ONNX encoder fingerprint also covers the vocabulary files the loaded tokenizer names.
- Concurrent first starts and concurrent migration runs no longer crash or open a half-built store: store creation is serialised under the exclusive storage lock, and the migration ledger decides what is pending under the SQLite write lock. Contention reports the retryable `DATABASE_IN_USE`. Several processes first opening a store written by an older build no longer fail with "table already exists".
- A stale `active-db.json.tmp` left by a crash no longer blocks every later pointer write, including recovery, with `UNSAFE_ACTIVE_POINTER`; it is discarded under the exclusive storage lock while links and non-regular entries are still refused.
- A store that lost `active-db.json` no longer opens its retained format 6 file when a published migration candidate cannot be read or `migrations/v7` cannot be listed: start, `migrate-v7` and `--recover-pointer` refuse and name the candidate. `migrate-v7` inspects a pointerless source read-only and answers `SOURCE_UNREADABLE` instead of treating an unreadable database as format 6; an unlistable migration root reports `UNSAFE_MIGRATION_PATH` instead of a traceback.
- Database paths containing `?`, `#` or `%` are no longer read as URL syntax, which had created the database outside the workspace or made the v7 store unavailable.
- Outcome, pin, archive and active-context writes to a deleted record answer `NOT_FOUND` instead of appending an event; retries of writes made before the deletion still replay.
- Maintenance and intelligence tools that answer `TASK_REQUIRED` for a large workspace now complete when run as a durable task, which reads the workspace in bounded batches.
- Duplicate cleanup keeps the other copy's rationale, context and code bindings on the surviving record when both copies have the same visibility, preserves conflicting values under `context.merged_duplicates`, and the preview counts what will be kept.
- Dream purge recognises records promoted from v7 dreaming candidates by their capture provenance and keeps the most recent analysis per strategy and source set.
- Prune, duplicate cleanup, compaction and dream purge no longer answer `CONFLICT` for writes unrelated to the previewed selection; writes that touch a selected record still do.
- `memory_versions_list` no longer fails permanently with `IMPORT_INVALID` after a backdated outcome or a future `happened_at`, and `memory_at_time_get` no longer returns a stale version for such records.
- Vector export and import work on default local installs: they use the Qdrant folder beside the database and share the embedded client the dense lane already holds.
- Retrying a committed `memory_store` with code bindings no longer fails with `INVALID_ARGUMENT` after the bound file was removed.
- `memory_verify` reads contracted negations ("don't", "can't"), scopes a negation to the sentence and clause that mention the claim, and answers `unknown` for double or conflicting negation instead of inverting the verdict. The heuristic is English-only.
- A retried `active_context_clear` replays its committed receipt after a server restart or token expiry instead of failing with `TOKEN_TAMPERED`/`TOKEN_EXPIRED`.
- `code_todos_scan` pages past 20,000 findings, and `note` is reported only in marker form (`NOTE`, `note:`, `Note(...)`).
- Changing `embedding_model` or `embedding_dimension` queues a dense projection rebuild at the next start instead of waiting for the next write.
- Federated recall verifies workspace links and composes results on worker threads instead of blocking the event loop.
- Dreaming treats a record as evidence for a decision only when they share several distinctive terms, checks every live record instead of the newest 64, judges unanimity on all related records, never builds an automatic outcome on another inferred outcome, and labels an automatically recorded outcome as inferred and unverified. It no longer re-proposes the same record connection after every graph rebuild, and community refresh backs off after exceeding its build deadline.
- Importing the v7 dreaming runtime no longer loads the legacy dreaming managers, SQLAlchemy and the v6 models.
- The event store refuses a self-edge asserted through a `relationship.removed` event that opens a relationship stream.
- Tool results set `isError: true` exactly when the envelope has `ok: false`; the full envelope, including `error.counsel` and `error.remedy`, is still returned as structured content and text.
- `daem0n_tool_call` advertises the destructive, non-idempotent and open-world hints of the tools it can dispatch, and `daem0n_tools_search` results carry each tool's `destructive`, `idempotent` and `open_world` hints beside `read_only`.
- Resource reads return `application/json` as declared. The server no longer advertises `listChanged` notifications it never sends, and the initialize result carries `instructions` for the active covenant mode and tool surface.
- MCP tasks: a task-augmented call is processed normally when tasks are not configured; `tools/list` advertises `execution.taskSupport: "optional"` only on tools that can really run as a task; a task refused by the covenant returns the tool's counsel and remedy in the JSON-RPC error data; task refusals are distinguishable by `error.data.code`, with `-32601` for a tool that cannot run as a task. The gateway still does not relay tasks.
- Every v7 tool has a hand-written description and a `title` in `tools/list`, and `daem0n_tools_search` ignores function words, folds word endings and weights rare and name words, so natural queries such as "delete old memories" find the right tool.
- Client-visible `expires_at` and `briefed_at` are placed on the current wall clock rather than the process's steady scale, so they stay correct after a wall-clock step.

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
