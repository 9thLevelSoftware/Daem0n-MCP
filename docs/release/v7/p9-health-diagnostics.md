# P9 bounded runtime diagnostics

`system_health(workspace_id=..., include_components=true)` includes at most
16 typed runtime diagnostic rows. Global health omits workspace projection
and migration state and all per-workspace task counts. Setting
`include_components=false` omits these diagnostics entirely.

The seven projection rows report enabled/disabled state, the active generation,
projected row count, canonical event lag where comparable, and the coalesced
queue/running/dead-letter state. They report stable error codes only. They do
not include stored error text, record contents, database paths, queued arguments,
credentials, or job IDs. These are projection freshness observations, not a
live network probe of optional providers.

Task readiness requires the owned dispatcher and loops to be running and a
successful broker operation. Workspace task counts require the current
principal's authorization and are filtered by that principal and workspace.
Bridge readiness requires its owned serving thread and, for local IPC, its
worker threads to be alive.

Migration state reports the active generation and `not_verified` with
`OFFLINE_VERIFICATION_REQUIRED`. A bounded health read cannot certify the
authoritative event history; use the explicit offline `verify-v7` command for
that evidence. A readable active pointer is never reported as verification.

Inspection runs on a two-worker bounded pool. Workspace authorization is
checked before inspection and again before returning it. Storage failures and
capacity exhaustion produce bounded stable diagnostics. Shutdown closes the
owned pool.

Development verification (not final-release certification):

- Python 3.12 health and actual stdio/HTTP health reads: 8 passed.
- Python 3.10 health, real authenticated local Valkey task dispatcher, and
  bridge transport: 28 passed, 8 subtests passed.
- Earlier adjacent production/runtime/process checks: 41 passed.

Independent review and final artifact verification remain required. Remote
TLS deployments and live external-provider readiness are separate open gates.
