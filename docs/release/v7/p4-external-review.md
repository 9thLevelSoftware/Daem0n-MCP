# P4 external-operations bounded independent review

Verdict: **ACCEPTED for the bounded external URL-ingestion and sandbox implementation slice; no material finding remains.** Live E2B staging and the rest of P4 remain open, so this is not whole-P4 or release acceptance.

## Resolved review findings

- **Dependency/API contract:** `agency-e2b` now pins `e2b-code-interpreter==2.10.0`, and `uv.lock` resolves that exact version. An isolated locked-profile install exposed the expected `lifecycle`, `on_result`, and `on_error` API. The earlier 2.4.1 `lifecycle` failure is resolved (`pyproject.toml:67-69`, `uv.lock:792-793`).
- **Bounded raw and parsed output:** `BoundedExecutionTransport` forces identity encoding, rejects invalid/oversized declared lengths, and caps the entire raw response at 512,000 bytes before HTTPX line decoding or SDK JSON parsing (`e2b_transport.py:14-62`). The pinned SDK subclass creates and returns the bounded subclass and uses the owned client for `run_code`. Actual SDK tests stream one unfinished oversized NDJSON frame with constant producer memory and prove the parser is never called, the response closes, the sandbox is killed, and the client closes. Shared callbacks separately bound cumulative stdout, stderr, rich results, errors, empty frames, and nesting.
- **Cancellation-safe owned-client cleanup:** sandbox kill and client close now share `_finish_e2b_cleanup`, which bounds the operation, defers repeated cancellation until the cleanup task is terminal, and converts timeout/close failures to stable `CAPABILITY_DEGRADED` (`external_operations.py:342-425`). Slow-close cancellation, throwing close, and hanging close regressions pass. The original cancellation is propagated only after owned cleanup finishes or is boundedly cancelled.

## Accepted behavior in this slice

- `document_ingest_url` is wired into production. Its HTTPS-only path validates complete DNS answers, re-resolves and pins the actual TCP peer, preserves the raw query target, rejects redirects, disables environment proxy use, requires identity encoding and reviewed media types, and bounds the response body at 1,000,000 bytes (`external_operations.py:192-236`, `pinned_http.py:350-458`, `480-545`, `577-611`, `638-688`).
- Document chunks commit through one `BEGIN IMMEDIATE` event-store transaction. Namespace/provenance participate in deterministic IDs and idempotency hashing, while the legacy `memory_store_batch` namespace preserves its frozen receipt contract (`record_operations.py:554-723`). Cancellation before commit rolls back; cancellation after commit returns the terminal receipt to the foreground runner.
- HTTPX/pinned-transport imports are lazy inside `_fetch_document`, so importing and assembling the core server does not eagerly require that execution path. Production registers both operations, and application error translation forwards only validated, bounded `CapabilityState` values.
- E2B create/run/kill calls disable sandbox internet access, pass no host environment, use explicit create/request/execution/cleanup timeouts, disable retries, kill the sandbox on success, failure, timeout, and cancellation, and close the owned HTTP client through the same bounded cancellation-deferring cleanup path. Live staging proof remains open.

## Verification evidence and remaining scope

- Repaired wire-bound and cleanup regression groups in `.tmp/p4-wire-bound-verification.log` — **30 passed, 3 subtests passed**.
- Independent isolated `e2b-code-interpreter==2.10.0` run of `test_e2b_transport.py` plus the external-operation suite — **28 passed, 3 subtests passed** in 2.47 seconds.
- Review probe verified real `BoundedSandbox.create(..., debug=True)` returns the dynamically created subclass and installs `BoundedExecutionTransport`.
- The original repeated-cancellation probe was rerun through its committed regression and now waits for the owned delegate close before propagating cancellation.
- `uv lock --check` — passed; isolated `--extra agency-e2b` installed 2.10.0 and exposed the expected signatures.
- `.tmp/onnx-packaging-check.log` — **35 passed** for the packaging/capability/external focus.
- `.tmp/p4-process-acceptance.log` records an actual production stdio `https://example.com/` ingestion, one persisted chunk, byte-stable idempotent replay, both operations registered, and the expected `E2B_API_KEY_MISSING` remediation.
- `.tmp/p4-e2b-staging-gate.log` truthfully records live E2B staging as **NOT_RUN / OPEN** because credentials were unavailable. The required actual isolation, network denial, deadline, output, cleanup, and nonblocking-server staging scenario remains unaccepted.
- Scoped Ruff and `git diff --check` pass for the E2B transport/external-operation files.
- The coordinator-provided broader evidence (**98 passed, 141 subtests**) was not repeated.

Code impact, entity/community generation, consolidation preview/identity/archive, full transport acceptance, task retry policy, platform coverage, and final-commit evidence are outside this report and remain separate P4/release gates.
