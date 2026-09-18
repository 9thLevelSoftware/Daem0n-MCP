# P9 authorization, quota, identity, and HTTP-boundary independent review

Date: 2026-09-17

## Decision

**ACCEPTED for the bounded P9 authorization and quota scope.** Workspace grants,
exact JWT-subject identity, live revocation, durable task ownership, shared task
lifecycle admission, protected state files, Host/origin policy, trusted-proxy
configuration, and strict JSON parsing are implemented coherently and pass the
focused security suite. No material P9 finding remains in the reviewed scope.

This report reviews the bounded P9 implementation. It is not certification of
an external HTTPS proxy, multi-host deployment, performance/soak behavior, or
the complete v7 release.

## Resolved HIGH — task lifecycle requests now share application inflight quotas

Affected components:
`daem0nmcp/api/v7/fastmcp.py:347-497`,
`daem0nmcp/api/v7/middleware.py:197-245`, and
`daem0nmcp/api/v7/task_dispatcher.py:1345-1408`.

The initial review found that owned `tasks/get`, `tasks/result`, `tasks/cancel`,
and `tasks/list` re-entered `server._run_middleware()` without entering the
application's global, principal, or workspace inflight counters. This allowed
unbounded lifecycle calls, most seriously long-lived `tasks/result` pollers.

`V7InvocationMiddleware.on_request` now recognizes exactly the four owned task
methods. It authenticates first and acquires the same global and per-principal
counters used by tools. For get/result/cancel, it resolves the workspace only
through `DurableTaskDispatcher.task_scope`, which loads the durable row and
checks exact principal ownership, current workspace authorization, and current
session briefing before entering the workspace counter. `tasks/list` correctly
holds global/principal capacity because one listing can span multiple authorized
workspaces. Unknown or unauthorized task IDs still consume global/principal
capacity and remain indistinguishable.

The callback, including the 50 ms result polling loop and cancellation-settle
loop, remains inside the admission scope. Nested `finally` blocks release the
workspace and shared global/principal counters on success, dispatcher errors,
capacity errors, coroutine cancellation, and long-wait cancellation. Task
submission remains counted once through `on_call_tool`; generic `on_request`
passes non-lifecycle methods through and therefore does not double count it.

The deterministic regression holds an ordinary tool slot and verifies each of
the four task methods is rejected at the global/principal boundary; it then
holds each task method and verifies both another tool and another lifecycle call
are rejected. The workspace variant covers get/result/cancel, and an unknown-ID
probe confirms the global/principal slot is acquired and released before lookup.
All counter maps return to empty after cancellation.

## Reviewed behavior without additional material findings

- Remote identity uses only the verified JWT `sub` and MCP session. The subject
  is preserved exactly rather than trimmed or case-normalized, while missing,
  empty, oversized, or control-bearing subjects fail closed. Client metadata,
  headers other than the verified bearer context, and IP information do not
  become principals.
- `WorkspaceAccessPolicy` maps canonical configured roots to opaque workspace
  IDs and rereads one strictly parsed, size-bounded, owner-only policy for every
  authorization. Unknown fields, duplicate JSON keys, malformed principals,
  wildcard/path grants, unsafe permissions, and missing policy state deny
  remote access. Briefing state cannot create a grant.
- The router, pinned handlers, resources, federation/link operations,
  consolidation, edit bridge, and queued task execution all consult current
  workspace access. Federated retrieval authorizes every source before work and
  again under the link guard before composing the response. Revoked task
  status/result/list/cancel requests are hidden; a revoked queued task fails
  before handler entry.
- Durable task admissions are scoped by principal, workspace, tool, and exact
  idempotency binding. Queue limits are counted transactionally in SQLite;
  exact retries reuse an existing admission, changed arguments conflict, and
  Valkey carries only the opaque task ID. Lifecycle reads require the original
  principal plus a current-session briefing and repeat authorization while
  waiting for a result.
- The task database, policy file, parent directories, and SQLite sidecars use
  the protected-file boundary. The Windows implementation applies and verifies
  a protected DACL for the current user and SYSTEM; POSIX requires exact owner
  and 0600/0700 modes. Link/reparse ancestry is rejected.
- HTTP admits one exact normalized Host, rejects duplicate Host headers before
  reading the body, ignores forwarded headers by default, and accepts only
  explicitly configured proxy IP literals. Non-loopback binds require the
  pinned production JWT verifier. Origin allowlists are independent of Host and
  workspace grants.
- HTTP and stdio JSON parsing reject duplicate keys, non-finite numbers,
  excessive nesting, malformed UTF-8, and bodies above 2 MiB before SDK model
  dispatch. The HTTP body replay waits for a real disconnect after delivering
  the validated body, preserving streaming responses.

## Independent verification

- Python 3.12 security-focused workspace access, protected files, Host/proxy,
  strict JSON, middleware, admission, launcher, dispatcher, real task process,
  real JWT workspace process, federation, and pinned-handler suite:
  **100 passed, 2 skipped, and 33 subtests passed** in 50.63 seconds.
- The repaired task-lifecycle quota suite reported **62 passed and 94 subtests
  passed**. Independent focused rerun of `tests/api_v7/test_admission_limits.py`
  reported **16 passed**.
- Independent authenticated real-process rerun of
  `tests/api_v7/test_process_tasks.py` reported **2 passed**, covering stdio and
  Streamable HTTP task lifecycle and restart with the protected Valkey test
  configuration.
- The two skips were platform-inapplicable/unavailable protected-path cases
  (POSIX modes on Windows and symlink creation); the Windows DACL round trip and
  unsafe-DACL rejection passed.
- The authenticated Valkey configuration was loaded from the protected test
  environment without printing it. Real stdio/HTTP task lifecycle and live JWT
  workspace grant/revocation tests passed.
- The earlier direct bypass probe is now represented by the integration
  regressions and no longer enters the task callback when the configured global,
  principal, or applicable workspace limit is occupied.

The focused suite supports the authorization, revocation, identity, shared
quota, file-protection, and remote parsing claims. External proxy deployment,
multi-host behavior, and performance/soak certification remain release-level
gates outside this bounded P9 code review.
