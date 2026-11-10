# P7 production dreaming implementation report

Status: implementation checkpoint, awaiting independent acceptance review.

## Production behavior

- `V7DreamingCoordinator` is a format-7-only runtime service. It does not import or call the legacy dreaming scheduler, strategies, memory manager, or mutable relationship manager.
- Production owns one coordinator and a bounded worker pool. Startup initializes four durable strategy rows for each registered active workspace; repeated startup is idempotent. Shutdown signals every cooperative cancellation event, cancels idle loops, waits for admitted worker functions to end, then closes the owned pool before retrieval and storage services close.
- Idle clocks and active foreground-call counts are workspace-local. The MCP middleware signals begin and end only after resolving a registered workspace and independently confirming `CovenantGate.workspace_authorized`. The callback retains only the registered `Workspace`, never a principal, session, token, capability, or request body.
- Each workspace has at most one analysis pass. A global configurable semaphore and worker pool bound analyses across workspaces. Foreground activity sets the running workspace's cancellation event; graph rebuilding receives that event directly.
- Background admission yields once to queued foreground callbacks, snapshots a per-workspace activity epoch, and rechecks both the epoch and active foreground count after acquiring capacity and again after clearing stale cancellation. A foreground request therefore cannot be erased by a background waiter. The synchronous middleware callback never waits for a worker-owned lock; it publishes the epoch, active count, and cancellation event immediately on the event-loop thread.
- Missing graph capability durably disables `connection_discovery` and `community_refresh` with `GRAPH_CAPABILITY_UNAVAILABLE`. No graph provider import or retry occurs in that state.

## Strategy and write behavior

- Failed-decision and pending-outcome review use an oldest-first durable `(updated_at_us, record_id)` cursor. Cooldown exclusion occurs in SQL before the per-session limit, so a recently reviewed source cannot starve later eligible decisions. The cursor advances only after every admitted write finishes and survives process restart; selection no longer has a latest-1,000-record ceiling.
- Source and evidence reads select bounded content prefixes and retain the authoritative full byte length. Each slice has explicit source-row, evidence-row, byte, graph-membership, graph-pair, and wall-time limits, with cooperative cancellation throughout ranking. Truncated or incomplete evidence is never classified as unanimous for automatic outcome publication.
- Pending-outcome analysis distinguishes insufficient, mixed, unanimously positive, and unanimously negative evidence. The default remains `dream_pending_dry_run=True`; all default results are review candidates that state no outcome was committed.
- When `dream_pending_dry_run=False`, only unanimous classifications can append `memory.outcome_recorded`. The transaction revalidates the source and every evidence event, uses `actor_type=system`, writes deterministic request and correlation hashes, and is replay safe. The resulting candidate explicitly says whether the canonical outcome committed.
- Connection discovery traverses a bounded outer source record and a bounded page of peer memberships from the active graph generation. The peer query matches source entity memberships across the full generation, so qualifying records can straddle membership pages. A durable generation plus compressed outer/inner record cursor resumes the exact two-dimensional traversal across restart without allocating all record pairs. Active canonical relationships remain excluded and the operation only produces review candidates.
- Community refresh compares the active graph manifest's source event count with the canonical event count and calls `GraphProjectionBuilder` only at the configured threshold. The builder owns bounded extraction, cooperative cancellation, snapshot validation, and atomic generation activation.
- Candidate idempotency derives from the strategy, workspace, source event/state/content hashes, relevant evidence, and graph generation where applicable. `CaptureCandidateStore.stage_validated` rechecks exact authoritative source event IDs inside the same `BEGIN IMMEDIATE` transaction that inserts the candidate. Candidate and automatic-outcome workers serialize only their final publication boundary: cancellation observed before the boundary rolls back, while a commit that already passed the boundary may finish truthfully. Foreground admission never waits for this worker lock.

## Persistence, recovery, and health

- Migration 28 adds `dreaming_strategy_state`, constrained to exactly the four strategy identities and bounded status, pending, cursor, cooldown, success, error, and yielded fields. Migration 29 was appended independently afterward; the current schema remains the shared ledger tip.
- Startup converts interrupted `running` rows to `idle`, preserves degraded stable failures, and allows a newly enabled graph profile to recover previously disabled rows. Deterministic candidates and canonical outcome events remain the semantic idempotency authorities.
- Candidate publications admitted by a coordinator are shielded from cancellation and tracked independently of the module-global capture pool. Shutdown cancels the workspace, then waits only for that coordinator's admitted publications to reach commit or rollback before returning; unrelated capture work does not delay it.
- Offline verification requires and validates the state table. Projection recovery explicitly clears this derived metadata so a recovered generation resumes from authoritative records and candidates instead of claiming historical in-memory success.
- Workspace-scoped `system_health` reports enabled/running/yielded plus at most four bounded strategy rows. Each row exposes `work_remaining` from durable bounded-slice state. Global health and `include_components=false` omit dreaming state, preventing cross-workspace disclosure.

## Files changed for P7

- `daem0nmcp/dreaming/v7_runtime.py`
- `daem0nmcp/capture_candidates.py` (atomic source-version validation seam)
- `daem0nmcp/config.py`
- `daem0nmcp/migrations/schema.py` (migration 28 only)
- `daem0nmcp/schema_version.py` (shared schema tip is now 29)
- `daem0nmcp/verification_v7.py`
- `daem0nmcp/api/v7/{production,composition,middleware,runtime_services,tools}.py`
- `tests/test_v7_dreaming_runtime.py`
- `tests/api_v7/test_process_dreaming.py`
- Narrow activity and health cases in existing middleware/runtime-service tests.

## Verification performed

- Second independent-review repairs: lock probe reports `record_activity_seconds: 0.0`; shutdown probe reports zero capture workers and zero publication at return; the reduced-page graph probe finds its only cross-page shared pair. The original fairness/foreground probe remains `3/3` candidates and zero strategies started during foreground activity.
- Python 3.12 repaired P7 component suite: `16 passed` (exit 0), including an unrelated event-loop heartbeat during a stalled worker publication, coordinator-owned real capture-worker drain and rollback, and the only qualifying pair beyond the production 4,096-membership boundary.
- Python 3.10 repaired P7 component suite: `16 passed` (exit 0).
- Python 3.12 real-process and adjacent health/schema/candidate suite after the second repair: `49 passed, 234 subtests passed` (exit 0).
- Scoped Ruff and isolated mypy after the second repair: clean (exit 0).

- Independent root reproduction after repair: three eligible failed decisions over three one-item passes produced three candidates; a queued background pass with foreground activity started zero strategies (exit 0).
- Python 3.12 repaired P7 component suite: `13 passed` (exit 0). It covers cooldown-before-limit fairness, restart traversal of 1,002 authoritative decisions, source and graph cancellation, large-content conservative outcome handling, a cancellation/commit publication race, and bounded shutdown waiting for its worker.
- Python 3.12 real-process and adjacent schema/health/candidate suite: `49 passed, 234 subtests passed` (exit 0), including ordinary stdio and Streamable HTTP startup, restart idempotency, disabled graph behavior, and real enabled graph generation.
- Python 3.10 repaired P7 component suite: `13 passed` (exit 0).
- Ruff on the repaired coordinator, candidate store, and focused tests: clean (exit 0).
- Isolated mypy with imported modules skipped on the new coordinator and candidate store: clean (exit 0).

- Python 3.12 component and adjacent integration suite: `92 passed, 18 subtests passed` (exit 0).
- Python 3.12 offline whole-store verification suite: `54 passed` (exit 0).
- Python 3.12 P7 component suite, including real graph builder: `7 passed` (exit 0).
- Python 3.12 ordinary production MCP subprocess suite: `4 passed` across stdio and Streamable HTTP (exit 0). It observed candidates and durable health in core mode, restarted without duplicate proposals, and built an active graph generation with the real graph profile enabled.
- Python 3.10 core P7 component subset: `6 passed, 1 graph-profile test deselected` (exit 0).
- Python 3.10 ordinary stdio process restart/candidate test: `1 passed` (exit 0). This caught and fixed the pre-3.11 `asyncio.TimeoutError` distinction in the idle loop.
- Production composition and graph-native admission: `14 passed` (exit 0).
- Configuration/capability/foreground configuration: `23 passed, 24 subtests passed` (exit 0).
- Ruff on all P7-owned production and test files: clean (exit 0).
- Isolated mypy on the new coordinator and capture candidate store: clean (exit 0).
- Python 3.10 import probe confirmed the v7 dreaming runtime imports neither `igraph` nor `sentence_transformers` when optional profiles are disabled.

The repository-wide suite was not rerun at this checkpoint because other release slices are still changing shared files. P7 requires independent re-review of these acceptance repairs and the final quiescent release suite. Real graph process coverage used installed graph dependencies; it did not exercise every optional dense-provider combination, which is outside the dreaming write path.
