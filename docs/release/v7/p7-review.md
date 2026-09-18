# P7 independent repair review

Date: 2026-09-17

## Decision

The bounded P7 dreaming slice is **accepted**. The three original HIGH
findings and the three subsequent MEDIUM lifecycle/completeness findings are
repaired. No new consequential issue was found in the repaired scope.

This is P7 dreaming-slice acceptance, not whole-release certification.

## Re-review of the three MEDIUM findings

### Resolved: foreground activity no longer blocks the event loop

`record_activity` updates the event-loop-owned activity epoch/count, publishes
the cancellation event, and wakes the workspace without acquiring the
worker-owned publication lock
(`daem0nmcp/dreaming/v7_runtime.py:357-372`). `run_once` snapshots the epoch,
rechecks it and the foreground count after semaphore admission, clears stale
cancellation only in an event-loop-atomic section, and immediately rechecks the
epoch/count before starting work (`:413-439`).

The independent probe held `publication_lock` for 350 ms. `record_activity`
returned in 0.0 seconds, and the focused regression confirmed an unrelated
50-ms heartbeat completed while the worker still held that lock. Foreground
middleware therefore no longer inherits SQLite publication latency.

Candidate and outcome workers retain their final publication lock and
commit-time cancellation check. Cancellation observed before that boundary
rolls back; a worker that already passed the boundary can complete its truthful
commit without blocking foreground admission.

### Resolved: shutdown drains coordinator-owned candidate publications

The coordinator tracks every candidate publication task in
`_publication_tasks`. `_stage_validated` shields an admitted publication,
awaits its true terminal state when the workspace task is cancelled, and
removes it only after completion
(`daem0nmcp/dreaming/v7_runtime.py:1068-1100`). `aclose` first cancels the owned
workspace loops, then awaits any remaining tracked publication tasks before
closing the coordinator worker pool (`:374-396`). It waits only for work
admitted by this coordinator; unrelated users of the module-global capture pool
do not delay shutdown.

The independent real-store probe stalled a candidate at the publication lock.
Shutdown remained pending until the lock was released, then returned with the
global capture pool at zero in-flight workers and no candidate published:

```text
{'close_seconds': 0.11, 'close_waited_for_worker': True,
 'capture_worker_in_flight_after_close': 0, 'published_after_release': 0}
```

This establishes a clean coordinator quiescence boundary and preserves the
pre-commit rollback guarantee.

### Resolved: graph traversal examines qualifying peers across page boundaries and restart

Connection discovery now uses a durable two-dimensional cursor containing the
active graph generation, outer source record, and inner peer record. For each
bounded outer record, the peer query matches its entity memberships across the
full active generation and pages peers after the inner cursor
(`daem0nmcp/dreaming/v7_runtime.py:824-1039`). It groups complete shared-entity
evidence for each admitted peer, retries a split peer that has not yet reached
the configured minimum, and advances only through pairs actually inspected.
Generation replacement invalidates the old cursor.

An independent production-size probe used minimum shared entities of two. One
source owned entities A+B; 4,096 intervening peers owned only A; the sole
qualifying target beyond the boundary owned A+B. The first pass produced no
proposal and persisted `pending=1`. A newly constructed coordinator, given
only that durable cursor, found the pair on its next pass:

```text
{'production_page': 4096, 'first_proposals': 0, 'first_pending': 1,
 'restart_proposals': 1,
 'pair': ['mem_...0001', 'mem_...1002']}
```

The traversal remains bounded by membership rows, inspected pairs, proposal
count, wall time, and cooperative cancellation. Active canonical relationships
are excluded and discovery still produces review candidates rather than
authoritative relationships.

## Reconfirmed behavior

- Failed-decision and pending-outcome selection applies cooldown exclusion
  before its per-session limit. The durable `(updated_at_us, record_id)` cursor
  reaches records beyond the former 1,000-row ceiling across restart.
- Original repair probe: three one-item passes reviewed all three eligible
  failed decisions; a queued foreground request started zero strategies.
- Source/evidence prefixes, row and byte admission, graph row/pair/deadline
  budgets, and SQLite progress cancellation remain present. Incomplete or
  truncated evidence cannot be treated as unanimous for automatic outcomes.
- Candidate and outcome transactions revalidate exact canonical source event
  identities and check cancellation before commit. Outcome publication remains
  deterministic and replay-safe.
- Runtime state, SQL predicates, candidate provenance, and health reads remain
  tied to the exact registered workspace. No cross-workspace read/write path or
  principal/session retention was found in this slice.
- Missing graph capability remains durably disabled without importing or
  retrying the optional graph provider.

## Independent evidence

- `.venv/Scripts/python.exe -m pytest tests/test_v7_dreaming_runtime.py -q`
  — **16 passed** in 5.91 seconds.
- `.tmp/p7_review_probe.py` — **3/3** eligible decisions reviewed and **0**
  strategies admitted during foreground activity.
- `.tmp/p7_lock_probe.py` — `record_activity_seconds: 0.0` while another thread
  retained the publication lock.
- `.tmp/p7_candidate_shutdown_probe.py` — shutdown waited for the real capture
  worker, returned with zero workers, and published zero candidates.
- `.tmp/p7_graph_restart_probe.py` — the sole qualifying pair beyond the
  production 4,096-membership page was found by a fresh coordinator from the
  first pass's cursor.

The repository-wide suite and large synthetic release-performance fixtures are
outside this bounded review and remain separate release gates.
