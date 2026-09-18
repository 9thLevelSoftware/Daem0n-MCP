# P1 durable task dispatcher report

Date: 2026-09-17

## Outcome

P1 now has a dedicated durable task dispatcher that does not use FastMCP's
Docket argument/context queue. The production MCP server implements the
standard experimental task methods while keeping FastMCP task configuration
for every tool set to `forbidden`; task-capable tools advertise optional task
support through the protocol adapter and are dispatched only by Daem0n-MCP.

Admission completes typed normalization, workspace resolution, exact identity
scope, fresh briefing, Covenant authorization, deadline validation, and replay
safety before a durable job becomes runnable. An interrupted `authorizing`
reservation is failed closed because it cannot prove that the process-local
Covenant nonce was consumed. A retry must present a fresh independently valid
grant and completes admission from scratch.

SQLite stores the normalized credential-free request under a protected
managed-storage database. Valkey receives only an opaque `tsk_` identifier.
The database and outbox form the authority, so a queue outage or process
interruption after acknowledged admission does not lose accepted work.

The dispatcher accepts only an authenticated loopback `redis://` or `rediss://`
endpoint. It runs one to eight embedded workers, reconnects after Valkey
failure, claims duplicate deliveries once, and recovers replay-safe interrupted
work. A mutation needs an operation idempotency key; read-only tasks use their
canonical normalized-argument digest. An exact retry of an accepted task
returns the existing task; a changed request under the same idempotency key is
rejected.

Task status, result, list, and cancel require the original principal and a new
briefing for the task's workspace in the current transport session. Queue
records contain no JWT, preflight token, headers, request context, Redis secret,
or raw transport session. Cancellation reports `cancelled` only before an
operation starts or after its child has actually cancelled. A late committed
result wins cancellation and is stored as completed. Persisted cancellation is
resolved as cancelled after restart and cannot be requeued. A start gate closes
the claim-to-handler race, so cancellation accepted before handler entry stops
the operation. Graceful dispatcher shutdown is a separate lifecycle signal:
active replay-safe work returns to the durable queue for restart, while an
interrupted non-replay-safe execution fails rather than being falsely reported
as caller-cancelled. `_execute` repeats the stop check after registering its
child and before opening the handler start gate, closing the window where a
task was already claimed but absent from `aclose()`'s active-child snapshot.
Terminal records are pruned according to their MCP TTL.

Outbox and worker loops are supervised with bounded backoff and publish health
state. One-shot SQLite failures at pruning, outbox selection/deletion, claim,
and terminal-result storage recover without permanently losing worker capacity
or an opaque wake-up. Bounded periodic reconciliation republishes authoritative
queued rows whose acknowledged Valkey list item was lost. Atomic `LPOS`/`RPUSH`
publication keeps at most one queued copy during repeated reconciliation. Task
listing scans stable database pages until it collects authorized rows, so newer
tasks from unbriefed workspaces cannot hide older authorized tasks. Its opaque
cursor carries the full ordering key. Production queue names use a durable
random authority identifier stored in the task database; independent databases
sharing Valkey cannot steal each other's IDs.

## Interfaces and composition

- `DurableTaskDispatcher.submit(tool_name, arguments, scope, task_metadata)`
  performs durable admission and returns a `TaskView`.
- `get_task`, `get_result`, `list_tasks`, and `cancel` require `principal_id`
  and the current `transport_session_id`; workspace access is rechecked through
  the Covenant briefing state.
- `start()` recovers the database and starts outbox/workers; `aclose()` drains
  owned children and durably requeues their replay-safe work without inventing
  caller cancellation. Production registers it last so it closes first.
- `durable_task_execution_var` carries only the exact admitted task identity,
  credential-free principal/session identity, workspace, tool, and normalized
  argument digest into the existing typed tool handler. The handler bypasses
  only the already-consumed duplicate Covenant gate when the admitted values
  match; operation validation and live per-workspace authorization remain.
- `DAEM0NMCP_TASK_REDIS_URL` is the explicit production enablement switch.
  Without it, the core profile remains available and task capability is absent.
  The database path is `<managed storage>/v7-task-dispatcher.sqlite3`.
- Local stdio and unauthenticated-loopback owners use a stable principal derived
  from the managed-storage authority. A restarted process therefore finds the
  same owner's task, while the new MCP session must brief the workspace again.

## P1-owned changes

- `daem0nmcp/api/v7/task_dispatcher.py`: protected SQLite authority/outbox,
  loopback Valkey delivery, admission, recovery, supervision, worker execution,
  isolation, pagination, truthful cancellation, and TTL cleanup.
- `daem0nmcp/api/v7/fastmcp.py`: standard MCP task protocol handlers and
  capability advertisement, routed through the existing FastMCP middleware;
  upstream FastMCP/Docket task submission remains disabled.
- `daem0nmcp/api/v7/tasks.py`: exact durable execution context plus the reviewed
  foreground cancellation and committed-result correction.
- `daem0nmcp/api/v7/application.py` and `pinned.py`: exact admitted-execution
  reconciliation at the existing typed handler boundary.
- `daem0nmcp/api/v7/middleware.py`: public identity resolution using the same
  authenticated stdio/HTTP context used by normal tool invocation.
- `daem0nmcp/api/v7/production.py`: dispatcher configuration, stable local
  authority principal, managed database, service lifecycle, and adapter wiring.
  Concurrent P3/P4 composition is preserved.
- `tests/api_v7/test_task_dispatcher.py`: real Valkey recovery, duplicate
  delivery, fail-closed admission crash points, persisted cancellation,
  caller-cancel and shutdown claim/start races, transient SQLite faults, deep
  scoped pagination, authority namespace isolation, graceful close/reopen,
  live post-ack broker loss, and lifecycle access controls.
- `tests/api_v7/test_process_tasks.py` and `process_client.py`: standard task
  submit/status/result/list/cancel through real stdio and Streamable HTTP
  subprocesses, followed by process restart and renewed briefing.

## Verification

- Authenticated real Valkey P1 dispatcher/adapter/task plus production,
  composition, foreground, pinned and factory conformance:
  `95 passed, 111 subtests passed` in 46.57 seconds, exit 0.
- Real subprocess task lifecycle and restart on stdio and Streamable HTTP:
  included above; the focused dispatcher plus process run passed
  `16 passed, 8 subtests passed` in 40.44 seconds, exit 0.
- Existing production ritual and restart on both transports with task
  configuration absent: `2 passed` in 19.19 seconds, exit 0.
- Ruff on the dispatcher, adapter/task modules, process helper, and focused
  tests: exit 0.
- Python 3.10.21 `compileall` for the v7 API and focused tests: exit 0.
- Focused mypy with imported modules skipped reported no issues in
  `task_dispatcher.py`, `tasks.py`, or `fastmcp.py`, exit 0.

## Remaining P1 gates and concerns

Independent re-review of the repaired dispatcher remains open. The P5-owned
multi-page export contract is being reconciled with its operation tests; an
intermediate combined run reported ten export/import failures outside the P1
dispatcher surface. Cross-platform Redis/Valkey and protected-file evidence
remains a release gate beyond the local Windows certification service. No
FastMCP upstream task queuing should be enabled unless its persistence model
changes to exclude arguments and authenticated request context.
