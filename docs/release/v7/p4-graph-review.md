# P4 graph/entity independent review

Date: 2026-09-17

## Decision

The bounded P4 graph slice is **not accepted**. Canonical snapshot validation,
generation publication, stale-snapshot rejection, capability gating, workspace
authorization, and resolution-aware Leiden behavior are substantially sound.
Production stdio and streamable-HTTP calls also work when the graph extra is
installed. The findings below leave response integrity, valid-input handling,
resource bounds, cancellation, and one public argument incomplete.

This is a review of the graph slice only. It is not whole-P4 or release
acceptance, and it excludes code-impact work and P5 portable projections.

## Findings

### HIGH — `community_rebuild` can attach a community ID to another community's label and member count

Affected components: `daem0nmcp/graph_projection.py:239-276,393-413`,
`daem0nmcp/discovery_projection.py:444-460,814-821`, and
`daem0nmcp/api/v7/graph_operations.py:211-221`.

`_community_seeds` orders communities by member tuple. `populate_graph` then
orders `_CommunityRow` values by `source_key` and returns IDs in that different
order. `GraphProjectionBuilder` returns the original seed tuple alongside that
reordered ID tuple. `community_rebuild` zips the two tuples positionally. The
reuse path is also unsafe because it reads IDs ordered by `identity_hash`.

The public result can therefore claim that `com_X` has label/member count A
while `community_get(com_X)` returns community B. A focused probe created eight
two-record communities with deterministic partitions and compared each zipped
pair to the activated database row; **5 of 8 pairs were wrong**. For example,
the third returned seed was `Group04Service, Group05Service`, while its returned
ID belonged to `Group12Service, Group13Service`. Existing tests only exercise a
single community, so ordering cannot diverge.

Remediation: preserve the association by source key rather than position. Have
the projection result return a mapping or records containing both seed and
public ID, and build `CommunityRebuildData` by explicit `source_key` lookup.
Add multi-community tests for both a fresh build and the reused-generation
path, asserting each returned ID against `community_get`/the activated row.

### HIGH — one valid memory can permanently poison graph rebuilding

Affected components: `daem0nmcp/entity_extractor.py:50-86`,
`daem0nmcp/graph_projection.py:176-210,363-371`, and
`daem0nmcp/discovery_projection.py:140-164,334-385`.

The public memory input permits content up to 100,000 characters, and the
extractor places no length limit on function, file, module, or variable names.
The discovery projection rejects names longer than 256 characters. Thus legal
canonical content such as `"a" * 257 + "()"` is extracted successfully and
then rejected as `INVALID_DISCOVERY_SEED`. Every rebuild continues to encounter
the same record, so no graph generation can be activated until authoritative
memory is changed or removed. The graph builder also lets the discovery-specific
exception escape instead of normalizing it to `GraphProjectionBuildError`.

Concrete probe: append one normal `memory.created` event whose content is the
257-character identifier followed by `()`, then call
`GraphProjectionBuilder(...).rebuild(workspace_id)`. It raises
`DiscoveryProjectionBuildError INVALID_DISCOVERY_SEED`, and the active graph
manifest count remains zero.

Remediation: enforce the projection contract at extraction time. Deterministically
skip unsupported extracted tokens (or use an explicitly specified collision-safe
canonicalization policy), record them in skipped/diagnostic counts, and never let
one legal memory invalidate the whole projection. Translate all projection-layer
failures at the graph builder boundary. Add valid maximum-content tests containing
oversized identifiers and paths.

### HIGH — foreground graph work is not safely bounded by memory

Affected component: `daem0nmcp/graph_projection.py:31-33,95-165,176-284`.

The row-count ceilings do not provide a safe process-memory bound. The snapshot
uses `fetchall()` for as many as 100,000 records, each of which can contain
100,000 characters, before checking any cumulative byte budget. That permits
roughly 10 GB of ASCII content alone, excluding SQLite/Python object overhead.
Entity extraction then eagerly accumulates all names and memberships before the
downstream discovery limits run. Up to one million edges are materialized as
rows, a Python set, a NetworkX graph, an undirected graph copy, and an igraph
copy. A protected but otherwise valid workspace can exhaust the MCP process
instead of returning `TASK_REQUIRED`.

Remediation: define conservative cumulative UTF-8 content, extracted entity,
mention/membership, node, and edge budgets based on production memory limits.
Read canonical rows in pages, charge budgets before retaining data, and stop
with `TASK_REQUIRED` before constructing large Python/native graphs. Test
adversarial maximum-size legal records and high-cardinality unique entities;
assert bounded peak allocation and no partial generation.

### HIGH — cancellation cannot interrupt or time-limit graph construction/Leiden

Affected components: `daem0nmcp/graph_projection.py:213-252` and
`daem0nmcp/api/v7/graph_operations.py:101-160`.

Cancellation is checked before native graph construction and after Leiden, but
`run_leiden_on_networkx` has no cancellation or deadline boundary. NetworkX
materialization and the igraph/Leiden call can therefore run indefinitely from
the request's perspective. `_run_mutation` catches cancellation and waits until
the worker terminates, while `_rebuild_sync` holds the active-storage lock for
the complete computation. Two cancelled or stuck builds can occupy the entire
two-worker pool and keep storage activation locked.

A focused probe replaced only `run_leiden_on_networkx` with a blocking call,
started a real `GraphProjectionBuilder`, then set its cancellation event. After
200 ms the build future was still unfinished; it returned `CANCELLED` only after
the native-call stand-in was released. The existing cancellation test fires
immediately before publication and does not exercise this interval.

Remediation: execute the non-cooperative native phase behind a killable,
deadline- and memory-bounded process boundary, terminate it on cancellation,
and keep the storage lock out of the expensive compute phase where possible.
Add cancellation tests while graph conversion and Leiden are in progress,
including a never-returning stand-in, and prove worker capacity and the storage
lock are recovered within a fixed deadline.

### MEDIUM — the public `entity_backfill.force` argument is ignored

Affected components: `daem0nmcp/api/v7/tools.py:1471-1475` and
`daem0nmcp/api/v7/graph_operations.py:176-190`.

The accepted request exposes `force`, but the handler never reads
`request.force`, `_rebuild_sync` has no force parameter, and
`GraphProjectionBuilder.rebuild` always reuses a matching generation. Thus
`force=false`, omitted `force`, and `force=true` have identical behavior. This
does not satisfy the requirement to exercise and assert the present/omitted
optional branch, and callers cannot request a fresh extraction after extractor
or native-runtime repair when canonical events are unchanged.

Remediation: carry `force` through the operation and builder, with a new
idempotency key forcing a new validated generation while replay of the same
idempotency key remains idempotent at the application boundary. Add enabled
production MCP tests for omitted, explicit false, explicit true, and invalid
values, asserting the documented generation/side effect.

## Independent evidence

- `uv run --extra graph python -m pytest tests/api_v7/test_graph_operations.py tests/api_v7/test_discovery_projection.py tests/api_v7/test_discovery_operations.py tests/test_retrieval_specialized_projection.py tests/test_retrieval_runtime.py tests/test_retrieval_jobs.py tests/api_v7/test_production.py -q`
  — **93 passed, 2 subtests passed** in 33.82 seconds. This includes the actual
  production stdio and streamable-HTTP enabled/disabled graph-profile test.
- The same selection under the repository `.venv` without the graph extra
  produced 10 expected graph-capability failures and 83 passes. Running through
  the declared `graph` extra installed the optional distributions and passed;
  this was an environment limitation, not counted as a product finding.
- Targeted probes independently reproduced the multi-community response
  mismatch, the legal oversized-identifier poison case, and cancellation being
  deferred until the native phase returns.

The passing tests support the canonical event snapshot, atomic generation,
stale-write retry, active-generation preservation, capability gating,
authorization, resolution propagation, lazy disabled-profile import, and both
MCP transports. They do not mitigate the findings above.
