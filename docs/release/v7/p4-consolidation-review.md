# P4 consolidation independent re-review

Date: 2026-09-17

## Decision

The bounded consolidation slice is **accepted**. All prior HIGH findings, the
cancellation and immutable-selection gaps, the protected durable-validation
seam, and the remaining projection-scheduler finding are repaired.

This is consolidation-slice acceptance, not whole-P4 or release acceptance.

## Re-review of projection scheduling

The previous MEDIUM finding is resolved.

`_apply_sync` no longer invokes the projection scheduler from its worker
thread. Instead it adds the target database path immediately after target
commit and, for archive operations, all participating source paths
(`daem0nmcp/api/v7/consolidation_operations.py:1078-1082`). Replay/resume paths
collect the same paths before archive continuation
(`daem0nmcp/api/v7/consolidation_operations.py:869-873`). The set has one worker
writer; the event loop reads it only after that worker terminates.

`_run_mutation` shields the worker and, on cancellation, signals cooperative
cancellation and awaits its true terminal state. Its `finally` block then calls
`_schedule_projections` on the owning event-loop thread
(`daem0nmcp/api/v7/consolidation_operations.py:1345-1368`). Consequently:

- successful mutations call production's asyncio scheduler on a running loop;
- target commit followed by source revocation or archive failure still wakes
  the target and every source that might have committed;
- cancellation after target publication waits for rollback/recovery state and
  then wakes the durable projection queues;
- scheduling occurs only after database locks and the worker operation are
  terminal, avoiding a drain racing an uncommitted mutation.

The focused regression requires `asyncio.get_running_loop()` inside the
scheduler callback and passed. The source-revocation regression observed all
three target/source database paths despite the expected
`UNAUTHORIZED_WORKSPACE` failure. The archive-cancellation regression observed
the target wake and a truthful `recovery_required` run with no source archive
committed.

## Previously resolved findings reconfirmed

- **Commit-time authorization:** target and source grants are rechecked before
  each durable boundary. A pre-target revocation rolls back all work; a
  post-target source revocation leaves a truthful recoverable ledger.
- **Replay, expiry, and recovery:** incomplete archive states resume; only a
  verified completed run returns a final receipt. Recovery binds the exact
  target generation/path, request, immutable selection, target records,
  mappings, and progress rows.
- **Bounded reads:** source records use bounded `fetchmany` and enforce record
  and byte budgets before retention.
- **Durable identity:** queued execution restores the admitted principal and
  transport session from an exact tool/workspace/argument digest, then rechecks
  all live ACLs. The five-source real-process task survives restart.
- **Cancellation and idempotency:** target-copy and archive loops check
  cancellation before authorization/commit. Pre-publication cancellation rolls
  back; post-publication cancellation preserves a recoverable target. Exact
  replays remain idempotent.
- **Immutable selection:** apply/recovery re-derive membership, target IDs,
  counts and hashes, including source event/state/content identity.
- **Protected durable validation:** the syntax-only internal placeholder is
  available only under an exact worker-installed durable execution context,
  is removed before `AdmittedRequest`, and neither it nor the client preflight
  credential is persisted.

## Independent evidence

- `.venv/Scripts/python.exe -m pytest tests/api_v7/test_health_diagnostics.py tests/api_v7/test_pinned_handlers.py::PinnedHandlerTests::test_health_revocation_during_inspection_discards_all_data tests/api_v7/test_consolidation_operations.py -q`
  — **19 passed** in 10.33 seconds; all 14 consolidation component tests passed.
- Earlier independent consolidation coverage remains applicable: **17 passed**
  across operations, CLI recovery, and protected durable validation; **2 passed**
  for the five-source authenticated durable task over stdio and streamable HTTP;
  and the focused dispatcher credential-persistence regression passed.
- Inspection of success, replay/resume, source-revocation, and cancellation
  paths found no scheduler call remaining inside the worker and no committed
  path omitted from the eventual event-loop wake set.
