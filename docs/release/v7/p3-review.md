# P3 lifecycle, federation, and local-provider independent re-review

Date: 2026-09-17

## Decision

**ACCEPTED for the bounded P3 lifecycle/federation and configured local
ONNX/Qdrant slice.** No CRITICAL, HIGH, MEDIUM, or LOW correctness finding
remains in the reviewed changes.

This decision does not accept the whole P3 or v7 release. Remote Qdrant,
transient external-provider failure injection, repeated startup/shutdown
resource-soak evidence, multi-process federation race execution, and
large-corpus latency/memory bounds remain release gates.

## Re-review of the four prior findings

### Resolved — link revocation is serialized with final authorization and publication

Affected components: `daem0nmcp/api/v7/federated_retrieval.py`,
`daem0nmcp/api/v7/runtime_services.py`, and
`daem0nmcp/api/v7/federation_operations.py`.

Federated retrieval now takes a path-private shared access guard for the final
directional-link check, both live per-workspace authorization checks, canonical
composition, and unsafe-output validation. Link and unlink take the exclusive
form of the same guard while committing the origin ledger mutation. The guard
combines a writer-preferring process-local read/write condition with the
existing OS file lock, so it supports concurrent readers while serializing
mutations across processes. Activation locks are acquired before this access
guard consistently in retrieval and mutation paths.

The shared guard is released only after the immutable response has been built,
and the function performs no await after release. This gives the operation a
clear publication linearization point without holding a filesystem lock across
transport backpressure. Cancellation while the blocking OS acquisition is in
flight drains the acquisition and releases the guard before propagating.

The deterministic contention regression proves an exclusive unlink cannot
cross a live shared publication guard. A real multi-process race remains a
release integration gate, rather than a defect in the reviewed mechanism.

### Resolved — optional specialized projection work does not hold the writer lock while staging

Affected components: `daem0nmcp/retrieval/specialized_projection.py` and
`tests/test_retrieval_specialized_projection.py`.

Snapshot reads, provenance/digest calculation, and staging now run before the
builder opens its short `BEGIN IMMEDIATE` publication transaction. Publication
rechecks both the append-only event identity and active projection generation.
A changed source raises retryable `PROJECTION_BUILD_SUPERSEDED` and cannot
activate the stale candidate.

The regression pauses real procedure staging, successfully admits a concurrent
foreground EventStore write, and then proves the stale build is superseded.
This exercises actual SQLite contention rather than replacing the drain with a
sleeping mock.

### Resolved — timed-out dense work retains its resources until the worker exits

Affected components: `daem0nmcp/retrieval/providers.py`,
`daem0nmcp/retrieval/service.py`, and
`daem0nmcp/api/v7/runtime_services.py`.

`DenseProvider` now counts admitted underlying operations independently of the
async waiter. Timeout, cache rotation, or shutdown can mark the provider closed,
but owned client and encoder resources detach only after the last admitted
worker exits. An operation that began before close may finish with its existing
resources; later operations fail closed. The cleanup path deduplicates shared
query/document encoders and closes each owned resource once.

The timeout plus cache-retirement and timeout plus shutdown regressions hold a
worker after its async timeout, verify no close occurs while the resource is in
use, release it, and verify exactly one close.

### Resolved — durable federated recall reconstructs and reauthorizes its scope

Affected components: `daem0nmcp/api/v7/tasks.py`,
`daem0nmcp/api/v7/task_dispatcher.py`, and `daem0nmcp/api/v7/pinned.py`.

Durable execution now carries the authenticated principal and credential-free
transport-session identity. `memory_recall` derives a fresh invocation scope
from those values and performs current Covenant authorization for the origin
and every linked workspace during the federation service's before/after
authorization passes. Preflight/briefing credentials are not persisted or
replayed. Legacy rows without a session and state that cannot be re-proved after
restart fail closed with actionable `COMMUNION_REQUIRED`.

The regressions cover successful durable linked recall, loss of briefing/access,
missing restored session identity, and path/credential exclusion.

## Additional reviewed changes

### Bounded four-request capacity

The dense-provider pool and Task8 canonical-hydration pool each have four hard
slots. Admission remains non-queuing: a fifth operation is rejected immediately
and mapped at the Task8 boundary to retryable `RETRIEVAL_UNAVAILABLE`. Capacity
continues to belong to an admitted thread after waiter cancellation until the
underlying operation actually terminates. Barrier tests prove four simultaneous
hydrations enter, while an over-capacity regression proves the stable retryable
error.

### Configured pooled-ONNX adapter and vector validation

`daem0nmcp/retrieval/onnx_encoder.py` uses the public ONNX Runtime session API
for exports that expose `sentence_embedding`, avoiding Optimum's incompatible
`last_hidden_state` assumption. It pins a remote tokenizer to the graph's
downloaded snapshot, disables tokenizer remote code, passes only declared graph
inputs, validates output rank/batch/dimension and finite nonzero values, applies
the configured truncation, and normalizes the result. Token-only exports remain
on the Sentence Transformers backend.

`daem0nmcp/retrieval/vector_validation.py` validates numeric, finite, nonzero,
equal-length vectors by cosine direction using tolerances suitable for Qdrant's
normalized float32 representation. Dense projection verification separately
retains identity, payload, dimension, checksum, and generation validation, so
the cosine comparison does not weaken those contracts. Tests cover the pooled
path, token-only fallback, malformed/zero/non-finite output, close behavior,
real local Qdrant float32 storage, and rejection of malformed vectors.

### Native startup ordering and independent capability profiles

`daem0nmcp/api/v7/production.py` places native runtime preload first in the
production lifecycle. It now imports `qdrant_client` whenever the `local`
profile is ready and imports Sentence Transformers/ONNX entry points whenever
`models-local` is ready; neither profile incorrectly gates the other's startup
initialization. This covers local-only vector export as well as dense retrieval,
while disabled profiles import nothing. An import failure in one optional
entrypoint is contained so the remaining enabled entrypoints still initialize
and the provider boundary can return its stable degraded diagnostic.

## Independent verification

- Focused Python 3.12 lifecycle, job, dense planner/projection, specialized
  projection, retrieval service, ONNX adapter, runtime service, federation,
  durable pinned-handler, production, and foreground-configuration suite:
  **190 passed and 50 subtests passed** in 41.07 seconds.
- Independently inspected the supplied unmodified-process certification logs:
  stdio and Streamable HTTP each reported configured local Qdrant plus pooled
  ONNX dense evidence, 20 warm requests in groups of four with no degradation,
  p95 0.50235/0.60682 seconds on the one-record fixture, and canonical recall
  after restart.
- Source review confirmed the four remediations above, lock-order consistency,
  bounded worker ownership through cancellation, independent native-profile
  gates, and stable error translation at public boundaries.

The small local fixture establishes functional concurrency and restart behavior,
not production-scale performance. The remaining external and soak gates listed
in the decision are still open.
