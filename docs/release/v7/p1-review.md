# P1 independent review

Date: 2026-09-16

## Scope and disposition

Reviewed the bounded foreground slice against `cc08b4f`, including the
FastMCP 3.4.7 adapter and packaging pin, foreground policy table,
cancellation/deadline runner, root deadline configuration, strict HTTP replay
fix, Git briefing timeout, subprocess transport harness, and focused tests.
Concurrent P3 lifecycle and P8 static-resource work was excluded except where
it shared the reviewed adapter or composition root.

No CRITICAL findings were identified. Four HIGH findings were reproduced in
the initial review. Targeted corrections were subsequently applied and
independently rechecked; all four findings are resolved in the current
worktree. The durable admitted-ID dispatcher remains a separate, unaccepted P1
gate and is changing concurrently, so this review does not treat P1 or the
package as complete.

## Findings

### RESOLVED HIGH — Policy-rejected requests bypassed the configured deadline

Affected component: `daem0nmcp/api/v7/fastmcp.py:189-203`.

When an optional tool request exceeds its foreground policy, the adapter sets
the admission-only context and awaits `execute()` directly. This path does not
use `run_sync_fallback`, so workspace resolution, Covenant authorization, and
the admission-aware handler are outside the configured 1-60 second deadline.
It contradicts the report's claim that all foreground calls use the configured
deadline and provides a slow-admission denial-of-service path.

Evidence: with `sync_timeout_seconds=1`, a policy-rejected request, and an
admission-aware handler that waited 1.2 seconds before returning
`TASKS_UNAVAILABLE`, the adapter returned `TASK_REQUIRED` after 1.219 seconds
instead of enforcing the deadline. Existing adapter tests verify authorization
and lack of mutation, but do not exercise a slow admission path.

Remediation: execute the admission-only handler through the same
cancellation-safe bounded runner, while setting the context before child task
creation so it propagates. Preserve authorization before `TASK_REQUIRED`, do
not consume a one-use capability, and add a regression that proves slow
admission returns `DEADLINE_EXCEEDED` only after owned work is terminal.

Resolution: the admission-only path now uses `run_sync_fallback` with the
configured deadline. The new slow-admission regression observes
`DEADLINE_EXCEEDED` and verifies cleanup completed.

### RESOLVED HIGH — Deadline cleanup could report failure after a durable commit

Affected component: `daem0nmcp/api/v7/tasks.py:313-319` and the mutation
contract documented in `daem0nmcp/api/v7/operations.py:290-292`.

On timeout, `run_sync_fallback` cancels and drains its child but discards a
successful terminal result and always raises `DEADLINE_EXCEEDED`. Operation
layers intentionally return a committed receipt when cancellation arrives too
late to prevent a commit. Discarding that receipt tells the caller the mutation
failed even though durable state changed, breaking truthful cancellation and
safe retry behavior.

Evidence: a focused operation waited, caught cancellation, completed cleanup,
and returned `"committed-receipt"`; `run_sync_fallback` nevertheless reported
`DEADLINE_EXCEEDED`. The current timeout test covers a child that propagates
cancellation, not a child that reaches a successful committed terminal state.

Remediation: after timeout cancellation, drain the child and return its result
if it completed successfully. Translate terminal cancellation/failure to the
deadline result as appropriate. Keep explicit caller cancellation propagating
as cancellation. Add a committed-receipt-after-timeout regression.

Resolution: timeout cleanup now returns a successful terminal child result,
while terminal cancellation still becomes `DEADLINE_EXCEEDED` and explicit
caller cancellation still propagates. The committed-receipt regression passes
on Python 3.10 and 3.12.

### RESOLVED HIGH — Workspace export checked its bound after materialization

Affected components: `daem0nmcp/api/v7/tasks.py:205`,
`daem0nmcp/api/v7/operations.py:382-392,928-946`, and
`daem0nmcp/event_store.py:1251-1276`.

The foreground policy admits every `workspace_export` request based only on its
small arguments. The operation then selects, fetches, parses, stores, and hashes
the entire workspace event stream. Only after that work completes does
`_validate_bundle` reject more than 10,000 events with `TASK_REQUIRED`. A large
workspace can therefore consume unbounded time and memory before admission is
rejected. Because the blocking worker cannot be interrupted and is drained on
cancellation, the outer deadline does not bound this work.

Remediation: perform a cheap bounded cardinality check before calling
`export_event_bundle` (for example, a count or `LIMIT max+1`) and reject with
`TASK_REQUIRED` before fetching or parsing the stream. Add a regression with
more than 10,000 events that proves the exporter/materializer is never invoked,
and audit the other unconditional state-dependent policies for the same order
of operations.

Resolution: `_export_sync` now checks workspace event cardinality before the
exporter runs. The regression supplies 10,001 events and verifies
`export_event_bundle` is not called.

### RESOLVED HIGH — Git timeout did not safely own the process tree

Affected components: `daem0nmcp/api/v7/resource_repository.py:519-550` and
`tests/api_v7/test_git_briefing_deadline.py:19-46`.

The original temporary-file workaround returned after two seconds while a
spawned descendant remained alive. A focused Windows probe mirroring the test
found the descendant launcher and interpreter still running after the method
returned. Repeated briefing calls can accumulate processes and temporary-file
handles.

The subsequently proposed Job Object revision is also unsafe in its reviewed
form: Win32 ctypes functions lack pointer-sized `argtypes`/`restype`, the job is
assigned after `Popen` starts so a fast descendant can escape, and Job Object
setup failure returns without killing or reaping the already-started process.
The current test delays descendant creation by 0.5 seconds, masking the
assignment race.

Remediation: on Windows, create Git suspended, configure a
`KILL_ON_JOB_CLOSE` Job Object using fully typed Win32 declarations, assign the
suspended process, then resume its primary thread through documented APIs.
Every setup failure must kill and reap the process. On POSIX, start a new
session and terminate/wait the process group with exit-race handling. Retain the
temporary output file and disable optional repository-controlled helpers as
defense in depth. Tests must use a no-delay grandchild, assert the descendant is
gone after timeout, and cover Job assignment failure cleanup as well as normal
Git output parsing.

Resolution: Windows now creates Git suspended, assigns it to a typed
kill-on-close Job Object, and resumes the primary thread only after assignment;
all setup failures kill and reap the suspended process. POSIX uses a new
session and process-group termination. The no-delay grandchild and injected
Job setup failure regressions pass on Windows, and a real Git status read still
returns bounded relative entries.

## Reviewed behavior without a finding

- FastMCP is pinned exactly to 3.4.7 in both the base and `tasks` profiles, and
  the lock resolves that version.
- Server construction fails closed if upstream raw FastMCP task queuing is
  requested.
- All 35 task-optional tools have immutable explicit foreground policies; a
  focused schema check found no misspelled collection, numeric, or deadline
  field paths.
- The production setting retains the 15 second default and validates the public
  1-60 second finite, non-boolean range before reaching the adapter.
- The HTTP replay change correctly waits for a real ASGI disconnect after the
  buffered body instead of fabricating one; real Streamable HTTP ritual and
  restart coverage passed.

## Verification performed

- Python 3.10 focused unit/conformance suite: `46 passed, 105 subtests passed`.
- Python 3.12 task and adapter suite: `23 passed, 83 subtests passed`.
- Real subprocess stdio and Streamable HTTP ritual/restart: `2 passed`.
- Normal Git status parsing through the temporary-file path returned 50 bounded
  relative entries in the active worktree.
- Focused probes reproduced the admission deadline bypass, discarded committed
  receipt, and surviving Windows descendant tree described above.
- Post-fix Python 3.10 task/adapter/operation/Git regression suite:
  `46 passed, 88 subtests passed`.
- Post-fix Python 3.12 task and adapter suite: `25 passed, 83 subtests passed`.
