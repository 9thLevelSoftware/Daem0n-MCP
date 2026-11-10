# P9 runtime-health independent review

Date: 2026-09-17

## Decision

The bounded P9 runtime-health slice is **accepted**. The two prior MEDIUM
findings are repaired. Projection and task counts remain workspace/principal
scoped, global health omits workspace data, diagnostics remain
path/error-text/credential redacted, migration state truthfully requires
offline verification, and the production lifecycle owns and closes the bounded
health worker pool.

This decision covers P9 health diagnostics only; it is not release acceptance.

## Re-review of prior findings

### Resolved: public workspace authorization is rechecked after complete inspection

`PinnedHandlers.system_health` now calls `_scope_for_workspace` again after the
health service has completed and `HealthData` has been validated
(`daem0nmcp/api/v7/pinned.py:1016-1029`). This final check covers storage,
dreaming, and runtime diagnostics together. Revocation at any point during
inspection therefore returns the generic `UNAUTHORIZED_WORKSPACE` envelope
with no health data. The public-handler regression revokes authorization inside
the provider and confirms `data is None`.

`RuntimeHealthDiagnostics.inspect` retains its narrower before/after check too,
so its workspace rows cannot be returned independently after revocation.

### Resolved: readiness is sampled consistently and dispatcher shutdown is not ready

`RuntimeHealthDiagnostics._inspect_sync` captures each provider's `is_ready`
value once and derives both status and stable error code from that snapshot
(`daem0nmcp/api/v7/health_diagnostics.py:80-123`). A transitioning fake is read
exactly once for each component and can no longer produce
`status="ready"` together with an unavailable error.

`DurableTaskDispatcher.is_ready` now requires `not self._stop.is_set()`
(`daem0nmcp/api/v7/task_dispatcher.py:272-280`). The shutdown regression confirms
readiness turns false immediately when stop begins, before worker tasks finish.
The bridge readiness properties are likewise sampled only once by health.

## Accepted behavior

- Workspace projection queries and background-job observations filter by exact
  workspace ID. Task counts reauthorize before and after their query and filter
  by principal plus canonical workspace. Global health returns only task and
  bridge readiness and no per-workspace counts or projection/migration rows.
- Seven projection rows are bounded. Stored error messages, job IDs, queued
  arguments, record contents, credentials, and database paths are not returned;
  only a syntactic stable error code can leave `last_error_json`.
- Migration diagnostics report `not_verified` /
  `OFFLINE_VERIFICATION_REQUIRED`; a readable pointer is not represented as
  authoritative-history verification.
- Task readiness requires started live supervisor loops, a successful broker
  operation, and a non-stopping dispatcher. Broker failures clear queue
  availability. Local bridge readiness requires the serving thread and bounded
  workers; remote bridge readiness requires its serving thread.
- Storage and projection inspection uses a two-worker bounded pool. Capacity
  exhaustion yields a stable degraded diagnostic, and production shutdown
  closes the pool.
- `include_components=false` suppresses capability, dreaming, and runtime
  component rows. Invalid or path-bearing provider output fails closed at the
  pinned response boundary.

## Independent evidence

- `.venv/Scripts/python.exe -m pytest tests/api_v7/test_health_diagnostics.py tests/api_v7/test_pinned_handlers.py::PinnedHandlerTests::test_health_revocation_during_inspection_discards_all_data tests/api_v7/test_consolidation_operations.py -q`
  — **19 passed** in 10.33 seconds. The first four tests plus the pinned-handler
  case directly cover the P9 authorization and readiness repairs; consolidation
  tests were included in the same run for the parallel re-review.
- Earlier independent P9 coverage remains applicable: **23 passed** for health
  diagnostics/runtime services, **29 passed plus 8 subtests** with the real task
  backend, and **2 passed** for production stdio and streamable-HTTP
  health/restart coverage.
- Code inspection found no new cross-workspace disclosure, secret/path leak,
  contradictory readiness row, or lifecycle ownership regression in this
  bounded repair.
