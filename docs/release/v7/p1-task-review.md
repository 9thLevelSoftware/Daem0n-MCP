# P1 durable task dispatcher independent re-review

Date: 2026-09-17

Base reviewed: `cc08b4f696b6226e88cca1f83104ce870dd62ae6`

Status: **accepted for the bounded P1 durable-dispatcher slice**. Live
reconciliation repairs the previously reported post-publication Valkey-loss
defect, graceful shutdown preserves truthful durable state, and the final
claim-to-child-registration race is closed. No material finding remains in the
reviewed dispatcher changes.

This is bounded dispatcher acceptance work. It excludes P5's portable-transfer
contract, cross-platform broker/protected-file evidence, final performance and
soak gates, and whole-P1 or release acceptance.

## Final HIGH re-review

### Resolved — shutdown cannot miss a claimed task before child registration

Affected component: `daem0nmcp/api/v7/task_dispatcher.py:437-447,614-650,774-823`.

`_execute()` now checks `_stop` immediately after registering the child in
`_active` and before reading task state or opening `start_gate`. If shutdown
already crossed its active-child snapshot, execution cancels and drains the
still-unopened child, applies `_settle_shutdown()`, and returns without handler
entry. If shutdown begins after registration, `aclose()` sees and cancels the
child. These two paths cover the entire prior gap; the start gate remains shut
until both lifecycle and persisted cancellation checks pass.

The exact authenticated-Valkey regression pauses `_execute()` after SQLite
claim, begins `aclose()` through its active snapshot, and then releases
execution. For replay-safe work it proves no initial handler entry, queued
settlement, and successful recovery after reopen. It repeats the same window
with a synthetic non-replay-safe row and proves no handler entry plus terminal
failure. Independent execution passed.

## Re-review of the two requested repairs

### Graceful shutdown false cancellation — resolved

`_execute()` now distinguishes dispatcher lifecycle cancellation from explicit
caller cancellation. If shutdown cancels an already registered child and the
child terminates without a result, `_settle_shutdown()` requeues replay-safe
work, fails interrupted non-replay-safe work, and preserves a genuine persisted
`cancel_requested` as cancelled. If a cancellation-resistant handler returns a
late successful result, completion wins. The supplied read and idempotent
mutation close/reopen regression passes.

The final claim-to-registration regression above completes coverage of the
previously open shutdown behavior.

### Post-ack Valkey loss — resolved

Affected component: `daem0nmcp/api/v7/task_dispatcher.py:520-612`.

The live outbox loop now performs bounded one-second reconciliation from the
SQLite authority. It recreates outbox rows for still-queued tasks whose last
publication is absent or stale. Publication uses one atomic Valkey script:
`LPOS` avoids a duplicate list item and `RPUSH` adds the opaque task ID only
when absent. SQLite then updates `last_published_at_us` and deletes the outbox
row transactionally. Duplicate deliveries remain harmless because `_claim`
changes only `queued` rows.

The regression delays workers, waits until the original publication is
acknowledged and its outbox row is gone, deletes the Valkey list, leaves the
dispatcher live, observes reconciliation restore exactly one wake-up, and then
proves the task completes. Independent execution of that regression passed.
The 128-row reconciliation bound preserves database/Valkey work bounds; queued
ordering and worker claims make older rows leave the eligible prefix.

## Earlier findings

The previous re-review conclusions remain unchanged:

- ambiguous `authorizing` rows fail closed and require fresh authorization;
- persisted caller cancellation is not requeued after restart;
- the claim-to-handler caller-cancellation gate prevents handler entry;
- transient SQLite boundary faults are supervised and recover capacity;
- task listing scans stable scoped pages using the complete ordering key;
- queue names are namespaced by a durable per-database authority ID;
- oversized portable export behavior remains owned by P5.

The stale admission-recovery comment identified in the prior report has been
corrected and now describes fail-closed recovery.

## Independent verification

- Authenticated-Valkey execution of the exact claim-to-registration race,
  graceful close/reopen, and live broker-loss regressions: **3 passed** in
  10.74 seconds.
- Source review covered shutdown cancellation/result races, replay-safe and
  non-replay-safe settlement, reconciliation publication/claim races, duplicate
  delivery, batching, and outage recovery.
- The exact claim-to-registration regression passed for both replay policies;
  source review confirmed no lifecycle gap remains between registration,
  `_stop` validation, the active snapshot, and opening the start gate.

The broader author-reported **95 passed and 111 subtests passed** and real stdio
and Streamable HTTP process runs were not repeated in full in this re-review.
