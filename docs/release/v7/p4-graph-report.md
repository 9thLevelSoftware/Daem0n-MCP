# P4 graph/entity implementation report

Date: 2026-09-17

## Implemented slice

- `entity_backfill` and `community_rebuild` are production operations backed by
  the canonical v7 event store and rebuildable projection tables. They require
  the graph capability, authorize the workspace before resolving storage, run
  in a bounded owned worker pool, and expose only stable path-free failures.
- Graph rebuilds read one canonical SQLite snapshot, validate every live record
  against its source event, extract entities with the existing pure extractor,
  and cluster the canonical relationship/fact graph with the caller's public
  Leiden resolution. Resolution-aware modularity uses
  `RBConfigurationVertexPartition`; the former modularity partition silently
  ignored resolution.
- Entity and community rows are populated into the same isolated `building`
  graph generation as the specialized graph projection. The builder validates
  the event snapshot and active-generation identity under a short writer
  transaction, validates both discovery partitions, and atomically activates
  the complete generation. Cancellation or stale canonical input rolls back
  the candidate and preserves the prior active generation.
- Expensive canonical reads, extraction, clustering, and digest construction
  happen before acquiring SQLite's writer transaction. A concurrent canonical
  write returns `PROJECTION_BUILD_SUPERSEDED`; foreground operations retry that
  race three times and then return a stable conflict.
- Identical canonical/configured results reuse the active complete generation,
  making retries content-idempotent. Durable graph projection jobs now call the
  complete graph builder, so canonical writes cannot publish a specialized
  graph generation without the entity and community partitions.
- Core assembly remains lazy. Disabled graph profiles do not import NumPy,
  NetworkX, igraph, or leidenalg. For enabled profiles, production initializes
  NumPy and the graph native entry points on the main thread before worker
  pools serve requests. This prevents the Windows native-loader/thread-start
  deadlock reproduced when `igraph.Graph.from_networkx` first imported NumPy
  inside a graph worker.

## Changed surfaces

- `daem0nmcp/graph_projection.py`
- `daem0nmcp/api/v7/graph_operations.py`
- `daem0nmcp/discovery_projection.py`
- `daem0nmcp/retrieval/specialized_projection.py`
- `daem0nmcp/retrieval/runtime.py`
- `daem0nmcp/graph/leiden.py`
- `daem0nmcp/api/v7/production.py` and its composition tests
- `tests/api_v7/test_graph_operations.py` and focused projection/runtime tests

No schema migration was required; the existing immutable graph-generation,
discovery-partition, and public-object-ID tables provide the required storage
contract.

## Verification

- Focused graph, discovery, specialized projection, runtime, and durable-job
  suite: **59 passed** in 27.12 seconds.
- Production composition suite excluding one independently owned stale P1
  operation-list expectation: **12 passed, 1 deselected** in 3.41 seconds.
- Actual production MCP calls passed with the graph profile disabled and
  enabled over both stdio and streamable HTTP. Enabled calls persisted one
  canonical record, completed entity backfill and community rebuild, and
  returned the active graph manifest.
- Concurrency regressions cover stale canonical writes during extraction,
  cancellation immediately before publication, retained active generation,
  rollback of building candidates, transient-race retries, and idempotent
  replay.
- Real Leiden tests prove two public resolution values produce different
  partitions. Runtime-builder coverage proves graph jobs publish both
  `entities` and `communities` partitions.
- Scoped Ruff, `py_compile`, and `git diff --check` passed. A clean core import
  reported none of `numpy`, `networkx`, `igraph`, or `leidenalg` in
  `sys.modules`.

The bounded graph/entity/community slice is ready for independent review. Code
impact, consolidation, live external sandbox staging, cross-platform release
coverage, and whole-P4 acceptance remain separate gates.
