# P3 scale repair bounded review

Review date: 2026-09-17

## Decision

**ACCEPTED for the bounded cache, transactional lexical-delta, durable scheduling,
and dense-provider batching changes reviewed here.** I found no material
correctness, concurrency, integrity, or cleanup defect in this slice.

This is not acceptance of the P3 scale gate or of the release. The health and
session-brief performance repairs were still changing when this review closed
and are explicitly outside this decision. They need a separate final review and
a stable-source scale run.

## Reviewed behavior

- `daem0nmcp/retrieval/repository.py`: revision-observed event-root caching,
  snapshot pinning, per-workspace cold single-flight, cancellation isolation,
  timeout behavior, fail-closed cache misses, and repository closure.
- `daem0nmcp/event_store.py` and
  `daem0nmcp/retrieval/projections.py`: same-transaction lexical delta,
  stale-manifest marking, authoritative document reuse, rollback, and full
  rebuild convergence.
- `daem0nmcp/retrieval/job_queue.py`, `daem0nmcp/retrieval/jobs.py`, and
  `daem0nmcp/retrieval/runtime.py`: durable five-second coalescing grace,
  immediate cold/incompatible fallback, delayed autonomous wake, superseded
  build requeue, lease renewal/reclaim, and shutdown/restart behavior.
- `daem0nmcp/retrieval/service.py`: owned repository/provider cleanup and error
  propagation.
- `daem0nmcp/retrieval/dense_projection.py`: 128-point provider batches without
  weakening whole-collection validation or staging cleanup.

## Findings

No material findings in the bounded reviewed implementation.

The repository cache binds a cached root to the observer's SQLite
`data_version`. A read transaction is pinned between two observer reads, and a
revision race rejects the cache entry rather than mixing snapshots. External
commits invalidate the cache. Cold initialization is shared per workspace and
shielded from waiter cancellation; the review probe cancelled one waiter while
a second waiter completed from the same single scan, after which a warm read
reused the result. `close()` rejects newly admitted work and waits for admitted
worker operations before closing the observer.

The lexical delta runs under a savepoint inside the canonical event write. It
updates or removes the authoritative retrieval document and its FTS row, checks
the materialized values, then updates the active manifest row count. Any delta
failure rolls back the canonical append. The event store still marks the active
generation stale and durably queues a full rebuild, so the delta does not claim
a new complete event root. The five-second grace is used only after a compatible
active lexical delta succeeds; cold or incompatible lexical state queues an
immediate rebuild. Stale lexical candidates are accepted only through the
bounded stale path and selected evidence is checked against canonical state.

Delayed work remains represented in `background_jobs`. The scheduler derives
its next wake from queued availability or running lease expiry, can be awakened
by a newer write, and resumes durable work after process restart. A write that
supersedes an in-flight build causes the old completion to requeue the latest
source rather than declaring it current. Lease claims and terminal transitions
are token- and expiry-guarded.

Dense batching preserves the previous validation boundary. Upsert and retrieve
calls are capped at 128 points, while exact provider count and the global ID set
are still compared after all batches. Duplicate, missing, and unexpected IDs
are rejected; payloads remain exact and vectors use the existing finite cosine
comparison. A failure after a partial provider upload removes the staging
collection and manifest while retaining the prior active generation.

## Independent verification

- `pytest tests/test_retrieval_repository.py tests/test_retrieval_lexical.py
  tests/test_retrieval_jobs.py tests/test_retrieval_runtime.py
  tests/test_retrieval_service.py -q`: **135 passed, 30 subtests passed**.
- `pytest tests/test_retrieval_dense_projection.py -q`: **25 passed, 11
  subtests passed**.
- Ruff on the reviewed production and test files: **passed**.
- `.tmp/p3-review-probe.py`: **passed**; cancellation of one cold-cache waiter
  did not cancel the shared initialization, the surviving waiter completed, and
  the warm evidence read reused one root calculation.

## Performance boundary left open

The million-event root calculation remains deliberately outside warm-request
latency and can outlive the 15-second waiter deadline. On the shared development
machine, a direct cold SQL iteration took 28.42 seconds and a complete Python
root validation took 72.51 seconds during this review. The single-flight task
continues after a waiter times out, so this is bounded and fail-closed, but it is
not evidence that cold availability or scale acceptance passes.

The latest pre-review stable-source measurement also remained below acceptance:
p95 recall was 0.638 seconds and lexical visibility was 5.397 seconds. A later
run timed out in health diagnostics; health and session-brief repairs were in
progress when this report closed. Those measurements are recorded only to bound
this decision. A new stable-source run and separate review of those repairs are
required before P3 or the release can be accepted.

## Final health and index extension re-review

The completed schema-30 index and online-health extension is **ACCEPTED for
bounded correctness** with no material finding. This extends the earlier
decision; it does not accept the scale or release gate.

Migration 30 adds a covering `(workspace_id,event_id,event_hash)` index for the
ordered event-root scan and order-compatible warning and failed-outcome indexes.
The reviewed query plans select all three indexes and do not create temporary
order B-trees. On the actual schema-30 million-event fixture, the event-root
query selected `idx_memory_events_root_covering`; one cold SQL iteration took
9.03 seconds and an immediately repeated complete Python root validation took
1.14 seconds. The earlier 28.42/72.51-second observation in this report was
against the pre-index schema and is retained as the before-repair measurement.

Online health now performs read-only structural inspection in an owned
two-worker pool. SQLite busy and progress deadlines bound the database work,
and no `quick_check` or other full-page integrity scan occurs. The production
lifecycle calls `aclose`, which drains worker shutdown off the event loop;
cancelling a waiter retains worker capacity until the admitted operation really
ends. Structural readiness remains distinct from authoritative integrity:
runtime diagnostics continue to report
`not_verified/OFFLINE_VERIFICATION_REQUIRED`, and offline verification still
owns full integrity checks.

Independent extension checks:

- The two health-boundary tests plus briefing and event-root query-plan tests:
  **4 passed**, exit 0.
- Ruff on the health, production lifecycle, migration, repository, and related
  test files: **passed**, exit 0.
- Direct schema-30 plans selected
  `idx_memory_events_root_covering`, `idx_memory_records_briefing_type`, and
  `idx_memory_records_briefing_outcome`, with no temporary sort.

The final actual MCP run passed indexed briefing, bounded health, and the warm
recall phase, then `memory_store` timed out while unrelated host load rose from
about 10% to 90% CPU. The cancellation left the canonical count and marker
unchanged. This run is inconclusive rather than a performance failure or pass;
the end-to-end 100k gate remains open for a stable-host rerun.
