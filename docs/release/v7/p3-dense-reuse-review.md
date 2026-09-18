# P3 dense reuse / migration 31 independent review

Date: 2026-09-17  
Reviewer: independent Sol review (`/root/p5_final_review`)  
Verdict: **ACCEPTED (bounded dense reuse / migration 31 scope)**

## Scope and limits

Reviewed migration 31 and the dense reuse trust chain across:

- `daem0nmcp/migrations/schema.py`, `daem0nmcp/schema_version.py`, and `daem0nmcp/models.py`
- `daem0nmcp/retrieval/dense_projection.py`
- `daem0nmcp/retrieval/onnx_encoder.py`
- `daem0nmcp/retrieval/providers.py`
- `daem0nmcp/retrieval/vector_validation.py`
- `daem0nmcp/retrieval/runtime.py` and `daem0nmcp/retrieval/jobs.py`
- `daem0nmcp/api/v7/portable_projections.py`
- the benchmark adapter and focused tests

No unresolved material finding remains in this bounded scope. Generation garbage collection and the actual 100k-record performance gate remain open by design and are not accepted by this report.

## Accepted behavior

- An unchanged, attested generation performs zero new encodes. Adding one record creates a complete new generation while encoding only that record.
- Reuse is bound to workspace, provider, immutable artifact fingerprint/vector-space hash, record ID, content hash, source event, exact payload, point ID, dimension, and canonical finite float32 provider-vector checksum.
- Missing, duplicate, unexpected, corrupt, or unattested prior vectors do not seed reuse.
- Provider operations and inference are bounded to 128 and 32 items respectively, with a global exact-count check.
- The builder performs a bounded final sweep of every staged point and durable attestation before activation. It rechecks exact reference state, IDs, payloads, vector dimensions and finite values, recomputed provider-vector digests, and count. Late mutation of an earlier batch rejects and cleans the candidate while retaining the prior active generation.
- Provider outage, validation failure, stale source, and cooperative cancellation clean staging while preserving the previous active generation.
- Migration 30 to 31 adds nullable attestation columns with canonical checks. The ORM mirrors both columns and checks.
- Legacy/imported refs remain unattested (`NULL`/`NULL`) and cannot seed reuse.
- Portable vector transfer emits or derives the canonical vector format and vector-space hash, validates supplied values, activates them in the manifest, and remains compatible with legacy bundles that omit them. Imported ref attestations remain null. Both legacy/no-fingerprint and current fingerprint-bound imports remain query compatible.
- Query/build contracts reject a changed artifact under the same model name. ONNX fingerprinting hashes stable resolved snapshot bytes and checks the fingerprint before and after model construction.

## Cancellation and durable job ownership

- A direct `drain_projection_jobs` call owns its cancellation event and sets it when its asyncio waiter is cancelled. The generic worker pool continues to own admitted thread capacity, while the dense builder sees the operation-owned event and aborts cooperatively.
- A scheduled drain passes its scheduler-owned token into the direct drain. Cancellation of an observing scheduled task does not convert into an unintended build cancellation. `await_projection_job_drains` is the owner that marks scheduler shutdown and sets that token.
- The builder checks cancellation before and after blocking provider calls, between batches, throughout the final staged sweep, and inside `BEGIN IMMEDIATE` immediately before active-manifest demotion. That final in-transaction check is the cancellation linearization point; after it, the two manifest updates and commit contain no blocking external operation.
- `ProjectionJobRunner` detects cancellation before and after each builder. A cancelled claimed job is returned to `queued`, has its lease cleared, and has the claim-time attempt increment reversed. Lease-token and unexpired-lease CAS checks prevent an obsolete worker from rewriting a successor-owned job.

Independent production-path probe:

```text
> .tmp\venv312\Scripts\python.exe .tmp\p3_dense_reuse_cancellation_probe.py
{'cancelled_direct_drain': True, 'statuses_after_worker_finished': [(1, 'active')], 'collections': ['cancel-probe-ws_0123456789abcdef01234567-local-g1-0726149a4a39']}
exit 0
```

This probe uses public `drain_projection_jobs`, the actual global `BoundedWorkerPool`, and the operation-owned cancellation token. The earlier bare `pool.run(builder.rebuild)` probe was invalid for production cancellation because it bypassed token ownership.

## Other independent probes

Late provider mutation is rejected before activation:

```text
> .tmp\venv312\Scripts\python.exe .tmp\p3_dense_reuse_late_corruption_probe.py
{'first_generation': 1, 'failure_code': 'PROJECTION_VALIDATION_FAILED', 'statuses': [(1, 'active')], 'active_is_current': False}
exit 0
```

`active_is_current` is false because the probe deliberately advanced canonical source state after generation 1; the relevant invariant is that the corrupt generation was not activated.

Portable query-contract repair:

```text
> .tmp\venv312\Scripts\python.exe .tmp\p3_dense_reuse_portable_probe.py
{'normal_matches': True, 'legacy_portable_matches': False, 'repaired_portable_matches': True, 'legacy_missing': ['vector_format', 'vector_space_hash']}
exit 0
```

## Verification evidence

- Final dense projection, durable job, runtime, portable import, and ORM suites: **99 passed, 30 subtests**, exit 0.
- Earlier broad focused dense, ONNX, query-contract, runtime, portable, benchmark, migration, schema-upgrade, and ORM suite: **155 passed, 39 subtests**, exit 0.
- Final scoped Ruff across production and focused tests: exit 0.
- Final scoped `git diff --check`: exit 0; Git emitted only line-ending conversion warnings.
- The functional benchmark compatibility suite passed. Direct Ruff invocation on the benchmark adapter still reports two pre-existing style findings outside the dense repair: import order and an explicit UTF-8 argument to `encode`.

No production files were modified by this review.
