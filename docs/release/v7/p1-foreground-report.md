# P1 foreground execution report

Date: 2026-09-16

## Outcome

The first bounded P1 slice is implemented. FastMCP is pinned exactly to 3.4.7
for both the base dependency and the `tasks` extra, while upstream FastMCP task
queuing remains disabled. The production adapter now uses explicit immutable
foreground policies for every task-optional v7 tool instead of inferring safety
from read-only annotations or handler attributes.

All foreground tool calls use a configurable deadline with a 15 second default
and the existing 1-60 second validation bounds. Ordinary bounded calls,
including `memory_recall`, run without the tasks extra. Requests outside a
tool's foreground policy pass through typed workspace and Covenant admission in
admission-only mode, do not execute the operation, and return actionable
`TASK_REQUIRED`. Operation-level `TASK_REQUIRED` responses receive the same
actionable message.

Caller cancellation remains cancellation rather than a business error. Owned
children are cancelled and joined before the caller observes cancellation or a
deadline failure. A Python 3.12 scheduling race that could cancel a newly
created child before its coroutine entered its `try/finally` body was fixed by
shielding the child and waiting for an explicit start handshake before
cancellation and drain.

## P1-owned changes

- `daem0nmcp/api/v7/fastmcp.py`
  - exact FastMCP 3.4.7 compatibility gate;
  - Python 3.10-compatible dynamic `Annotated` construction;
  - fail-closed foreground policy and admission-aware-handler checks;
  - bounded execution for task-forbidden and task-optional tools;
  - pre-side-effect oversized-request rejection after authorization;
  - actionable `TASK_REQUIRED` normalization;
  - explicit refusal to enable upstream task queuing.
- `daem0nmcp/api/v7/tasks.py`
  - immutable per-tool foreground execution policy table covering all 35
    task-optional tools;
  - request byte, collection, numeric, and deadline-field admission bounds;
  - cancellation-safe started-child handshake and terminal drain;
  - retained 15 second default and 1-60 second public bounds.
- `pyproject.toml` and `uv.lock`
  - exact `fastmcp==3.4.7` and `fastmcp[tasks]==3.4.7` pins and resolved lock.
- Conformance tests
  - `tests/api_v7/test_fastmcp_adapter.py`
  - `tests/api_v7/test_tasks.py`
  - `tests/api_v7/test_packaging_contract.py`
  - `tests/test_fastmcp3_compat.py`
  - FastMCP version references in `tests/api_v7/test_composition.py`

No P1 changes were made to `middleware.py`, `production.py`, or `tools.py`.
The coordinator separately wired `Settings.sync_timeout_seconds` through the
production composition root and owns its configuration tests. Concurrent P8
dashboard work now also changes the adapter/registry/resource surface; those
changes are outside this report.

## Verification

- `uv lock`: exit 0.
- Python 3.10.21 scoped conformance:
  `40 passed, 83 subtests passed` (exit 0), covering the adapter, task policy,
  registry, packaging, foreground configuration, and real installed FastMCP
  API compatibility tests.
- Python 3.12.14 task/cancellation conformance:
  `9 passed, 4 subtests passed` (exit 0).
- Python 3.10.21 adapter plus cancellation tests after the scheduling-race fix:
  `23 passed, 83 subtests passed` (exit 0).
- Ruff on the changed adapter/task/test surface: exit 0.
- `python -m compileall -q daem0nmcp` on Python 3.10: exit 0.
- Clean Python 3.10 base-profile editable install without the `tasks` extra:
  exit 0; Redis was absent and `create_v7_server("stdio")` constructed the real
  FastMCP 3.4.7 server.
- The coordinator reported both real subprocess MCP transports passing after
  independent harness fixes: `2 passed in 13.20s` for stdio and Streamable
  HTTP ritual/restart coverage.

## Remaining P1 scope

The dedicated durable admitted-ID dispatcher remains intentionally separate.
It must persist only admitted opaque IDs, keep credentials and raw request
context out of the queue, scope worker lifecycle to the production server,
recover the outbox, provide replay-safe result lookup, and preserve truthful
cancellation. Upstream FastMCP task execution must stay disabled because its
3.4.7 task path persists request context and raw arguments.

Foreground policy thresholds are deliberately conservative. Workspace-size
dependent tools also rely on their existing operation-layer scan/cardinality
guards before mutation. Threshold tuning should use the P10 benchmark corpus;
it must not silently broaden admission. Deadline cancellation drains owned work
to a terminal state, so a call can take longer than its nominal deadline when
cleanup or an already-committing mutation must finish to report a truthful
outcome.

## Review corrections

The independent foreground review found three high-severity gaps. They are now
corrected and covered by regressions:

- oversized admission-only execution uses the same cancellation-safe bounded
  runner as every other foreground call;
- a child that suppresses deadline cancellation because its mutation committed
  returns the committed receipt instead of a false `DEADLINE_EXCEEDED` error;
- `workspace_export` performs a bounded event-count precheck and returns
  `TASK_REQUIRED` before bundle materialization above 10,000 events.

The dedicated dispatcher described above is now implemented. Its separate
implementation and verification report is `p1-task-dispatcher-report.md`.
