# P3 lifecycle implementation report

Date: 2026-09-17

## Implemented slice

- Projection drain scheduling now processes repeated bounded slices instead of
  stopping after the five-job foreground cap. It waits for durable retry times
  and expired leases, coalesces writes during an active drain without an exit
  race, retries local worker-pool contention, and supports scoped shutdown
  quiescence. Shutdown waits for the admitted slice and leaves future durable
  work for startup recovery.
- Transient drain/open/deadline failures use an interruptible exponential
  retry (50 ms through 5 s). Permanent schema/configuration failures stop with
  a stable log entry, while durable jobs remain available for repair or
  restart. A queued write wakes the scheduler before its deadline query, which
  closes the final coalescing/exit race.
- Projection jobs persist stable sanitized failure codes. Unavailable optional
  projections and invalid durable payloads dead-letter immediately instead of
  consuming every retry. Python 3.10 SQLite lock diagnostics are recognized so
  a write-holding builder can publish a final renewed lease before completion.
- Production startup schedules each valid active v7 database. Production
  shutdown quiesces only the databases started by that server.
- Foreground lexical refresh has reserved worker capacity, so optional startup
  projection work cannot starve canonical write visibility. Timed-out
  foreground workers remain database-owned and are awaited before disposal.
- `Task8RecallService` caches retrieval services by workspace, active database
  generation, canonical database path, and retrieval-configuration fingerprint.
  The fingerprint also binds the effective optional-capability statuses.
  Initialization is serialized. Replaced services are retired immediately and
  closed after their last user; shutdown closes every idle cached service and
  marks active services for close on release.
- `DenseProvider` serializes lazy client/model initialization and closes only
  resources it owns. Externally supplied clients and encoders remain caller
  owned. Runtime-created encoders and clients are closed through
  `RetrievalService.close()`.
- `DenseProjectionBuilder` now follows the same ownership rule. Every bounded
  projection slice and explicit operator rebuild closes its owned Qdrant client
  and document encoder before closing SQLite. A validated zero-row dense
  manifest returns without loading an embedding model.
- Production injects one environment-derived capability-status map into recall,
  committed-write scheduling, startup recovery, and explicit rebuilds. Dense
  assembly requires both `local` and `models-local`; graph assembly requires
  `graph`. Installed but disabled packages are not imported or connected.
- Core lexical retrieval remains dependency-free. Sentence Transformers and
  Qdrant imports remain behind optional lazy paths.
- `DatabaseManager.close()` now quiesces its foreground and scheduled
  projection work, closes registered database-scoped services, disposes the
  engine, and releases the generation lock. SQLite validation helpers use
  explicit `closing()` ownership; transaction context management alone no
  longer leaves Windows database handles open. `MemoryManager` registers its
  cached retrieval and optional provider resources with that lifecycle.
- Linked recall accepts the public maximum of 32 requested workspace IDs. It
  requires a current directional link from the origin, independently resolves
  and authorizes every workspace under the caller identity and briefing scope,
  repeats link/authorization checks before return, holds activation locks in
  workspace-ID order, and runs at most two source recalls concurrently under
  one deadline. The shared candidate budget is divided across every admitted
  source (and rejected as invalid if it cannot grant every source one slot),
  independent of the final result limit. Each source returns canonical
  policy-valid candidates without composing context; those candidates are
  authenticated against their workspace store, fused by normalized source rank
  with workspace-ID tie breaks, and composed once under the global result and
  token budgets. Exact rendered token accounting includes the joined context,
  and fitting reserves room for later globally ranked results.
  Evidence and citation references carry the opaque origin workspace ID.

## Changed surfaces

- `daem0nmcp/retrieval/runtime.py`
- `daem0nmcp/retrieval/jobs.py`
- `daem0nmcp/retrieval/service.py`
- `daem0nmcp/retrieval/providers.py`
- `daem0nmcp/retrieval/dense_projection.py`
- `daem0nmcp/api/v7/runtime_services.py`
- `daem0nmcp/api/v7/federated_retrieval.py`
- `daem0nmcp/api/v7/models.py`
- `daem0nmcp/api/v7/pinned.py`
- `daem0nmcp/api/v7/operations.py`
- `daem0nmcp/api/v7/production.py` (coordinator-owned integration)
- `daem0nmcp/database.py`
- `daem0nmcp/memory.py`
- `daem0nmcp/cli.py`
- `tests/api_v7/process_client.py`
- Focused lifecycle tests in `tests/test_retrieval_jobs.py`,
  `tests/test_retrieval_runtime.py`, `tests/test_retrieval_dense_planner.py`, and
  `tests/api_v7/test_runtime_services.py`, plus federation coverage in
  `tests/api_v7/test_federated_retrieval.py` and
  `tests/api_v7/test_pinned_handlers.py`.

## Verification evidence

Initial focused commands used `.venv` Python 3.10. The final lifecycle,
federation, production, and real-process gate used the all-profile Python 3.12
environment at `.tmp/venv312`.

- `python -m pytest tests/test_retrieval_runtime.py tests/test_retrieval_jobs.py -q`
  — exit 0, 19 passed.
- `python -m pytest tests/api_v7/test_runtime_services.py tests/api_v7/test_production.py -q`
  — exit 0, 29 passed.
- `python -m pytest tests/test_retrieval_dense_planner.py tests/test_retrieval_service.py -q`
  — exit 0, 48 passed and 25 subtests passed.
- Final combined changed-surface run (jobs, runtime, dense, service, v7 runtime
  services, and production) — exit 0, 97 passed and 25 subtests passed.
- All `test_retrieval_*.py` except the two legacy retrieval-router files — exit
  0, 310 passed and 157 subtests passed.
- `python -m compileall -q` for retrieval, runtime services, and production —
  exit 0.
- Ruff changed-surface correctness rules (`E4,E7,E9,F,B023,ASYNC`) — exit 0.
- `git diff --check` — exit 0.
- Final Python 3.12 retrieval/service/federation/pinned/process run — exit 0,
  147 passed and 42 subtests passed.
- Final Python 3.12 database/rules/lifecycle/production/operations run — exit
  0, 82 passed and 30 subtests passed. Windows teardown released every tested
  temporary database without cleanup suppression.
- `tests/test_memory.py` — exit 1, 70 passed and 3 legacy behavior assertions
  failed (`semantic_match`, numeric legacy IDs, and legacy recall-cache hit
  accounting); all prior Windows teardown/locked-file errors are gone and no
  worker-capacity warning remains.
- Python 3.12 real stdio and Streamable HTTP process clients with all extras
  installed and profiles disabled pass. The earlier stdio model import timeout
  is no longer reproducible. The stdio suite now also launches two registered
  workspace stores, proves independent briefing is required, restarts the
  process, retrieves both stores, and verifies origin attribution on evidence
  and citation references.

The broader keyword-selected test collection could not start because the core
environment intentionally lacks legacy/optional packages (`numpy`,
`rank_bm25`, `langgraph`, `networkx`, and `watchdog`). `mypy` is also not
installed in this environment. These are incomplete gates, not passing skips.

## Remaining P3 federation and provider gates

The federation implementation has deterministic unit coverage for shared
budgets, directional link validation, rank-only global fusion, workspace
provenance, and independent target briefing, plus real MCP process coverage
with multiple registered stores. A path-private cross-process read/write guard
now linearizes final link and live authorization validation plus immutable
result construction before link/unlink mutation. The service performs no await
after releasing that read lease and returning the immutable result to its
adapter. This defines publication at response construction and avoids holding a
filesystem lock across unbounded client transport backpressure. A deterministic
contention test proves an unlink writer cannot cross that publication boundary;
multi-process process-level race coverage remains a release integration gate.

The four independent lifecycle review findings were also repaired:

- specialized projection source reads, provenance checks, and digest work run
  before `BEGIN IMMEDIATE`; the publication transaction revalidates the
  append-only event identity and projection generation, and a changed snapshot
  raises retryable `PROJECTION_BUILD_SUPERSEDED`. A real SQLite test pauses
  procedure staging, admits a foreground event-store write, and proves the
  stale candidate cannot activate;
- dense provider retirement tracks admitted worker threads beyond an asyncio
  timeout. Cache rotation and shutdown mark the provider closed but defer owned
  Qdrant/encoder cleanup until the final underlying worker exits, exactly once;
- durable execution carries the authenticated principal and credential-free
  transport session identity. `memory_recall` derives a fresh scope for the
  current origin and each linked workspace and repeats Covenant authorization.
  Legacy rows without a session identity and lost briefing/access state return
  actionable `COMMUNION_REQUIRED`; preflight credentials remain excluded from
  durable arguments;
- the dense worker pool admits four concurrent hybrid requests while retaining
  a hard capacity bound, matching the four-request performance contract. The
  separate Task8 canonical-hydration pool also admits four; deterministic
  barrier coverage catches the former two-slot race, and requests above the
  bound now return retryable `RETRIEVAL_UNAVAILABLE` instead of an internal
  error. When
  the local model profiles are enabled, production also initializes the public
  Sentence Transformers, ONNX, and ONNX Runtime entry points on the main thread
  before any projection/provider pool starts. Disabled/core profiles import no
  model runtime. This prevents the reproduced Windows native-loader deadlock
  between a first NumPy/SciPy import and concurrent worker-thread creation;

Post-review focused verification on Python 3.10:

- dense provider planner — exit 0, 23 passed;
- specialized projections — exit 0, 15 passed;
- durable linked pinned authorization — exit 0, 30 passed and 17 subtests;
- Task8 dense timeout/cache rotation/shutdown regression — exit 0, 1 passed;
- final combined retrieval/lifecycle/federation/durable/production suite under
  the all-profile Python 3.12 environment — exit 0, 175 passed, 11
  environment-dependent task-backend skips, and 45 subtests passed. The
  coordinated migration owner updated the federation schema assertion to
  `CURRENT_SCHEMA_VERSION` 25 before this final run.
- production plus foreground profile configuration — exit 0, 19 passed.
- the diagnostic full-entry preload against an existing active dense generation
  passed real stdio recall and restart; 20 warm requests in groups of four all
  returned dense evidence with observed p95 0.385 seconds. Evidence:
  `.tmp/probe_mcp_dense_preload_full.log`. The earlier NumPy-only preload moved
  the deadlock to SciPy and was rejected as incomplete.
- unmodified production certification then passed on both stdio and Streamable
  HTTP with Qdrant client 1.19.1, ONNX Runtime 1.30, Sentence Transformers 5.7,
  and FastMCP 3.4.7. Stdio recalled an existing dense generation in 2.563
  seconds, completed 20 warm requests in groups of four with zero degradation
  and observed p95 0.50235 seconds, then restarted with canonical recall intact.
  HTTP made a new write dense-visible in 3.234 seconds, completed the same warm
  concurrency gate with zero degradation and observed p95 0.607 seconds, then
  restarted successfully. Evidence: `.tmp/probe_mcp_dense_production_stdio.log`
  and `.tmp/probe_mcp_dense_production_streamable-http.log`.

The configured local ONNX/Qdrant path now has real new-write, existing-generation,
four-way warm concurrency, and restart evidence. Remaining provider acceptance
is remote Qdrant, transient external-provider failure injection, and repeated
startup/shutdown resource-soak evidence. The local fixture is intentionally
small and does not establish large-corpus latency or memory bounds. Durable
task tests requiring an authenticated Redis/Valkey endpoint remain an external
gate in this environment; their non-backend identity/authorization paths and
the coordinator's isolated dispatcher suite pass.

## Million-event retrieval scale repair

The 100,000-memory/1,000,000-event production probe originally abstained with
`POLICY_STATE_UNAVAILABLE`. The lexical provider itself returned 50 candidates
in about 65 ms, but every policy and selected-content read recomputed the full
event root inside the two-second read deadline. The repair keeps the integrity
check and moves its cost out of the warm request path:

- a path-owned repository observes SQLite's persistent revision, pins the
  canonical read transaction between two observer revision reads, and publishes
  an event-root cache entry only when both observations match;
- each workspace has one shielded cold initialization, so cancellation of one
  waiter does not cancel the scan needed by other callers; external commits and
  projection activation invalidate the entry, and repository shutdown waits for
  admitted work before closing its observer;
- an injected connection factory remains conservative and does not use the
  revision cache;
- migration 30 adds the covering event-root index
  `(workspace_id, event_id, event_hash)`, avoiding the observed million random
  table lookups and temporary sort while preserving the ordered full-root
  calculation.

Canonical memory writes now update a compatible active lexical document and FTS
row under a savepoint in the same transaction. The delta covers create, update,
archive, pin, outcome, and delete semantics. An FTS failure rolls back the
canonical append. The active manifest's row count is updated, while its source
event count, root, and digest remain stale until a full generation rebuild
validates them. A durable rebuild is always queued. A successful compatible
delta receives a five-second rebuild grace to coalesce writes and reduce writer
contention; absent or incompatible lexical state queues immediately. Delayed
jobs retain their due time in `background_jobs`, wake autonomously, and resume
after restart.

Migration 30 also adds covering order indexes for the exact briefing warning,
decision, and failed-outcome queries. The queries and eligibility rules are
unchanged. Focused query-plan tests require these indexes and reject temporary
order B-trees. Existing format-7 databases require the separately owned offline
candidate upgrade path before they receive migration 30; the development scale
fixture was upgraded with `DatabaseManager.init_db` only and is not release
upgrade evidence.

Online health no longer runs a full `PRAGMA quick_check` on the event loop. A
bounded owned worker checks read-only open, current schema, and the fixed set of
required tables in one snapshot, with SQLite busy and progress deadlines.
Online health reports this as structural readiness and retains
`not_verified/OFFLINE_VERIFICATION_REQUIRED`; `verify-v7` remains the
authoritative full integrity check. Shutdown drains the inspection pool.

Focused verification on Python 3.12:

- repository, lexical delta, jobs, runtime, and service: exit 0, 135 passed and
  30 subtests passed;
- event store, database, and record operations: exit 0, 34 passed and 11
  subtests passed;
- repository, briefing query plans, and migrations after final migration 30:
  exit 0, 78 passed and 4 subtests passed;
- health plus lexical/jobs/runtime/service: exit 0, 90 passed and 30 subtests
  passed;
- final schema/model/health check: exit 0, 40 passed and 22 subtests passed;
- Ruff on all production and test files in this slice: exit 0;
- targeted mypy for the repository/job queue and runtime health service: exit
  0.

Actual-process evidence records the progression and the remaining open gate:

- `.tmp/scale100k-repository-cache-final.log`: warm four-way recall p95 0.440
  seconds; new-write lexical visibility 18.09 seconds before the delta repair;
- `.tmp/scale100k-lexical-delta.log`: source unchanged, write response 0.413
  seconds, lexical visibility 5.397 seconds, specialized visibility 36.793
  seconds, and warm p95 0.638 seconds on a contended host;
- `.tmp/scale-health-stacks.log` identified the event-loop `quick_check`; after
  its repair, `.tmp/scale-health-probe.log` completed actual MCP briefing and
  bounded health in 4.27 seconds at schema 30;
- `.tmp/scale100k-schema30-final.log` passed briefing, bounded health, and the
  warm recall phase, then its canonical `memory_store` request timed out after
  30 seconds. Cancellation was truthful: the canonical count remained 100,003
  and the marker record was absent.

The final run started with 15.73 GB available memory and 10% CPU, then reached
9.98 GB available and 90% CPU. Nearby host samples reached 414 MB available and
96% CPU due to unrelated user Unity and .NET workloads. No user processes were
terminated. Because the benchmark did not complete and host load changed
materially during it, the current end-to-end 100k performance gate remains
open. The focused correctness, shutdown, and query-plan checks above pass; they
do not replace a stable-host actual MCP rerun or the independent acceptance
review.
