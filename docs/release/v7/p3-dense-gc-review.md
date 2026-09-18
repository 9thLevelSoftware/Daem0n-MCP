# P3 dense generation lease and GC independent review

## Verdict

**ACCEPTED (bounded).** The dense generation lease and garbage-collection
lifecycle meets the reviewed correctness, recovery, compatibility, and
bounded-storage criteria. This is not a whole-P3 or whole-release acceptance;
generation GC scale and actual 100k certification remain governed by their
separate release gates.

## Resolved findings

### Qdrant lifecycle compatibility and ambiguous lookup handling

- The first implementation passed the raw provider client to GC and therefore
  rejected the supported Qdrant 1.7 shape, which has `get_collections` rather
  than `collection_exists`.
- The initial compatibility helper then treated malformed list responses as an
  empty collection list, allowing false SQLite finalization while a provider
  collection remained.
- The final shared helper supports modern `collection_exists`, Qdrant 1.7
  `get_collections`, and `get_collection`. It requires an actual boolean from
  the modern API, explicit and structurally valid collection lists, valid
  bounded string names for every entry, and a non-null direct lookup response.
  Only an explicit numeric 404 status/code proves absence. Malformed responses,
  transport failures, and non-404 provider errors propagate so GC requeues and
  preserves refs/manifests.

The former false-finalization probe now returns the following on Python 3.10
and 3.12 while preserving the manifest and provider collection:

```text
{'result': [('queued', 'DENSE_GENERATION_GC_PROVIDER_UNAVAILABLE')], 'manifest_one': <row>, 'provider_collection_still_exists': True}
```

`.tmp/p3_dense_gc_qdrant17_probe.py` successfully deletes and finalizes the old
generation on Python 3.10 and 3.12. `.tmp/p3_qdrant_lookup_semantics_probe.py`
also confirms modern booleans, transport propagation, explicit 404 absence, and
503 propagation on both versions.

## Accepted behavior

- Lease acquisition snapshots the exact active manifest and recomputes the
  canonical collection identity inside `BEGIN IMMEDIATE`. Renewal and release
  are fenced by owner, token, and live expiry, so an expired holder cannot
  revive or release a successor lease.
- Portable vector export owns a dedicated lease connection. SQLite backup
  progress and provider scroll checkpoints renew the lease, snapshot validation
  binds generation and manifest ID, lease loss aborts publication, and `finally`
  releases only the held fence.
- Activation keeps the active generation plus one inactive generation. Older
  ready generations are durably enrolled without provider I/O in the activation
  transaction. Live leased generations are skipped atomically, then admitted on
  fenced release or bounded restart reconciliation.
- GC claim and finalization use durable claim tokens and expiries. Collection
  names are recomputed from canonical manifest identity. Provider deletion and
  confirmed absence precede SQLite ref/manifest deletion. Finalization
  revalidates the claim, ready status, collection identity, and absence of a
  live read lease.
- Provider ambiguity, lost delete acknowledgement, claim expiry, and restart
  preserve replay-safe durable work. Queued reactivation can explicitly cancel
  a job; running/dead-letter jobs block activation. Active manifests cannot be
  claimed or finalized.
- Cancellation is checked after each provider call, immediately after the final
  absence check, and again inside the fenced finalization transaction before DB
  deletion. Cancellation requeues without consuming an attempt and preserves
  refs/manifests.
- Reconciliation now filters for workspaces that actually have eligible old
  generations before its 16-workspace limit. This removes completed prefixes
  and converges across restarts. The runtime checks schema availability before
  querying schema-32 tables, preventing old fixtures from spinning.
- Production starts a continuous, owned drain and shutdown sets its owned
  cancellation token. Durable deadlines and a one-second idle poll discover
  later admissions without foreground provider deletion or a tight loop.
- Migration 32, `CURRENT_SCHEMA_VERSION = 32`, and ORM table declarations agree
  on the two new tables, composite keys/FKs, checks, indexes, and WITHOUT ROWID
  layout. Upgrade tests exercised the 31-to-32 path.

## Independent verification

- Python 3.12 focused suite:
  `python -m pytest -q tests/test_retrieval_dense_projection.py
  tests/test_retrieval_runtime.py tests/api_v7/test_portable_projections.py
  tests/test_v7_models.py tests/test_migrations.py
  tests/test_v7_schema_upgrade.py` — **133 passed, 30 subtests passed**.
- Python 3.10 GC-focused selection — **77 passed, 57 deselected, 16 subtests
  passed**.
- The broader Python 3.10 selection reached **131 passed, 1 skipped, 30
  subtests passed** with one unrelated environment failure: the core 3.10 venv
  does not install `huggingface_hub`, so an embedding-backend mock could not be
  imported.
- Ruff over the reviewed production and test files — **clean**.
- Post-repair Qdrant compatibility/error-classification selection on each
  Python version — **4 passed, 5 subtests passed**. It covers the 1.7 adapter,
  malformed provider responses, lost delete acknowledgement, and production
  runtime GC drain.
- Mypy was not a clean signal for this slice: a direct invocation traversed the
  existing application dependency graph and reported 176 pre-existing errors;
  none originated in `dense_generation_gc.py`.
- `.tmp/p3_dense_gc_lease_renewal_probe.py` on both Python versions: a leased
  generation survives two activations, receives no premature GC job, and
  renews.
- `.tmp/p3_dense_gc_cancellation_probe.py` on both versions: cancellation while
  the final absence check is blocked requeues with attempts `0` and preserves
  all three manifests.
- `.tmp/p3_dense_gc_reconcile_fairness_probe.py` on both versions: bounded
  admissions converge as `[16, 1, 0, 0]` with no missing workspace.
- `.tmp/p3_dense_gc_qdrant17_probe.py` on both versions: one queued generation
  is deleted and finalized through a 1.7-shaped `get_collections` client.
- `.tmp/p3_dense_gc_lookup_error_probe.py` on both versions: malformed lookup
  requeues `DENSE_GENERATION_GC_PROVIDER_UNAVAILABLE`, retains the manifest,
  and leaves provider state untouched.
- `.tmp/p3_qdrant_lookup_semantics_probe.py` on both versions: modern boolean
  results work, transport/503 errors propagate, and explicit numeric 404 alone
  reports absence.
