# P4 consolidation repair report

Date: 2026-09-17

## Outcome

The findings in `p4-consolidation-review.md` are repaired on the shared
`v7/completion` worktree. No schema migration was added or changed.

### Authorization and durable admission

- Target and all source grants are rechecked immediately before target commit.
- The target and current source grant are rechecked immediately before every
  source archive commit. A post-target revocation leaves the target receipt in
  `recovery_required` and rolls back the unauthorized source transaction.
- Five-to-32-source operations reconstruct the admitted principal and original
  transport session from credential-free durable execution identity and repeat
  live workspace authorization.
- Real-process testing exposed a separate protected-task adapter defect: the
  dispatcher correctly removed the consumed preflight before persistence, but
  the application router required that field during strict model validation.
  The router now recognizes an exact worker-installed tool/workspace/normalized
  argument digest before supplying a syntax-only validation value. It repeats
  the current workspace ACL check and never forwards or persists that value.
  Wrong tool, workspace, digest, missing legacy session, and client-supplied
  placeholder paths fail closed.

### Idempotency, recovery, and integrity

- Exact replay resumes `target_committed`, `archiving`, and
  `recovery_required` archive runs. Only a verified `completed` run returns a
  final receipt without resuming work.
- A completed receipt remains retrievable after preview expiry; a new
  application still requires a fresh preview.
- Recovery binds the target generation and canonical database path discovered
  with the run. Every later transition requires that same generation/path,
  exactly one run, the expected request hash, a complete progress ledger, and
  successful single-row state changes.
- Recovery verifies every committed target record and source mapping before
  archive continuation. Empty or replacement databases cannot produce a false
  completion.
- Preview child rows are re-derived in ordinal order. Source membership,
  deterministic target IDs, counts, and the immutable selection hash are
  recomputed. Apply and archive also compare current source event, state, and
  content hashes.

### Bounds, cancellation, and projection wake-up

- Source records are read with `fetchmany(64)` in deterministic record order;
  no per-source `fetchall()` occurs before the 10,000-record and 64 MiB limits.
- Target copy and source archive loops observe cancellation between records and
  before authorization/commit. Cancellation after target publication records a
  truthful recoverable boundary while rolling back the in-progress source.
- Projection drains are scheduled after committed database work and after all
  SQLite and activation locks are released. Scheduler failure does not falsify
  the committed receipt.

## Changed files

- `daem0nmcp/api/v7/consolidation_operations.py`
- `daem0nmcp/api/v7/application.py` (narrow protected durable validation seam)
- `tests/api_v7/test_consolidation_operations.py`
- `tests/api_v7/test_application.py`
- `tests/api_v7/test_process_tasks.py`
- `docs/release/v7/p4-consolidation-report.md`

## Verification

- `pytest tests/api_v7/test_consolidation_operations.py tests/test_cli_consolidation.py -q`
  — exit 0, 16 passed.
- Combined consolidation/application/CLI/real-process suite, before the final
  additional archive-cancellation regression — exit 0, 29 passed in 57.68s.
- Real five-source durable consolidation with a held activation lock, shutdown
  before mutation, persisted queued/running state, process restart, renewed
  briefings, and completion:
  - stdio — exit 0, 1 passed in 12.27s.
  - Streamable HTTP — exit 0, 1 passed in 12.02s.
- Real direct consolidation preview/copy/archive over both stdio and Streamable
  HTTP — exit 0, 2 passed in 9.98s.
- `ruff check` on all changed Python modules/tests — exit 0.
- `mypy --follow-imports=skip` on `application.py` and
  `consolidation_operations.py` — exit 0, no issues.
- Python 3.12 `py_compile` on all changed Python files — exit 0.
- Python 3.10.21 `py_compile` on both changed production modules — exit 0.
- Unscoped mypy remains a repository baseline failure: 135 errors in 31
  imported files. None were reported in either changed production module, and
  the scoped no-follow-imports check is clean.

## Review notes

- The durable process test covers the required pre-execution restart boundary
  without test-only production switches: the test owns the target activation
  lock during admission, shuts the process down, proves the task did not reach
  a terminal state, then restarts the real server and completes the same task.
- Schema 27 already makes the preview header immutable. Runtime recomputation
  now closes the mutable-child trust gap without consuming schema 28, which is
  reserved by the derived-state workstream.
- Independent review is still required before P4 consolidation acceptance.
