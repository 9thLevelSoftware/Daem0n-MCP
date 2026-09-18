# P4 graph independent-review repair

Date: 2026-09-17

Status: implementation complete; independent re-review still required.

## Repaired findings

- Community responses now resolve public IDs by each seed's `source_key`. Fresh
  and reused generations no longer depend on unrelated tuple ordering.
- Extracted names are normalized and checked against the discovery projection
  contract before retention. Unsupported long/control/non-UTF-8 names are
  skipped and counted, so a valid 100,000-character memory cannot poison every
  rebuild.
- Canonical records and edges are read in pages. Builds stop with
  `TASK_REQUIRED` before retaining more than 32 MiB of UTF-8 content, 100,000
  records/nodes, 100,000 entities, 250,000 entity memberships, or 200,000
  edges. Native request/result bodies are capped at 64/16 MiB. A representative
  100,000-node bounded subprocess probe completed in 16.3 seconds under the
  30-second deadline and returned all node assignments.
- NetworkX, igraph, and Leiden run in an owned subprocess with a 30-second hard
  build deadline and a 1 GiB process-memory ceiling. Cancellation terminates
  only that child handle. The workspace activation lock is released during
  snapshot/extraction/native work and reacquired with exact pointer validation
  for publication.
- `entity_backfill.force` now reaches the projection builder. Omitted/false
  reuses an exact current generation; true publishes a fresh validated
  generation. A background-job receipt binds workspace, operation arguments,
  and idempotency key. Receipt and activation commit in the same SQLite
  transaction, conflicting reuses return `IDEMPOTENCY_CONFLICT`, and exact
  retries return the recorded response without rebuilding.

## Verification

- `python -m pytest tests/api_v7/test_graph_operations.py -q`:
  `20 passed` (stdio and streamable-HTTP enabled/disabled, force branches, and
  the 100,000-record contract).
- Wider graph/discovery/runtime selection from the independent review:
  `104 passed, 2 subtests passed`.
- Scoped Ruff: passed.
- Scoped mypy for the three graph production modules with ignored optional
  imports and skipped dependency bodies: passed.
- `py_compile` and `git diff --check` for the repair scope: passed.

Focused regressions cover multi-community association on fresh and reused
generations, maximum legal content with an oversized extracted identifier,
content/entity/edge budget failures before native allocation, cancellation of
a never-returning child with prompt lock and worker-capacity recovery, force
omitted/false/true/invalid behavior, durable replay/conflict behavior, and an
injected failure after receipt insertion proving activation and receipt roll
back together. The 100,000-record regression also keeps the canonical and
native node ceilings aligned and passes the complete eligible node set into
the bounded phase.
