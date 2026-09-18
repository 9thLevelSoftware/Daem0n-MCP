# P4 code impact implementation report

## Delivered behavior

- Schema migration 26 adds `discovery_code_edges`, an immutable table keyed by
  workspace, code generation, source entity, target entity, and edge kind.
  Only `call`, `import`, and `reference` edges are accepted, and both endpoints
  must belong to the same retained code generation.
- `code_index` extracts bounded Python call/import/reference facts, accepts the
  same reviewed relationship shape from other strict index producers, resolves
  only workspace-local entities, and publishes entities and edges in the same
  SQLite transaction. The active manifest hash covers both partitions.
- Before commit, `code_index` repeats the bounded file selection and hashes every
  selected source again. Cancellation or a changed/added/removed source rolls
  the generation back instead of activating stale data.
- `code_search` verifies entity rows, edge rows, public ID bindings, partition
  hashes, and the active manifest hash before returning results.
- `code_impact_analyze` selects one active-generation entity by public ID or
  qualified name and follows reverse dependency edges. Traversal is bounded to
  the requested depth (schema maximum 10), 5,000 inspected edges, 500 affected
  entities, and 500 returned paths. An exceeded bound returns `TASK_REQUIRED`.
- The apps capability gates parser loading. A disabled profile returns
  `CAPABILITY_DISABLED` with a concrete `DAEM0NMCP_APPS_ENABLED=true`
  remediation; existing indexed search and impact reads remain available.
- Production composition registers the completed impact handler. No format-6
  table is read or mutated by the implementation.

## Changed files

- `daem0nmcp/migrations/schema.py`, `daem0nmcp/schema_version.py`
- `daem0nmcp/database.py`, `daem0nmcp/migrations/v7.py`
- `daem0nmcp/discovery_projection.py`
- `daem0nmcp/api/v7/discovery_operations.py`
- `daem0nmcp/api/v7/code_entity_operations.py`
- `daem0nmcp/api/v7/production.py` (one dependency-plumbing keyword)
- `tests/api_v7/test_discovery_operations.py`
- `tests/api_v7/test_code_entity_operations.py`
- `tests/api_v7/test_process_code_impact.py`
- `tests/api_v7/test_production.py`
- `tests/test_database_v7.py`, `tests/test_migrations.py`

## Verification

- `pytest -q tests/api_v7/test_discovery_projection.py tests/api_v7/test_discovery_operations.py tests/api_v7/test_code_entity_operations.py tests/api_v7/test_production.py tests/api_v7/test_process_code_impact.py tests/test_migrations.py tests/test_database_v7.py`
  - exit 0: 80 passed, 17 subtests passed
- `pytest -q tests/api_v7/test_process_code_impact.py`
  - exit 0: 2 passed using a real `python -m daem0nmcp.server` stdio process
- `pytest -q tests/test_verify_v7.py tests/api_v7/test_portable_projections.py`
  - exit 0: 61 passed
- `pytest -q tests/api_v7/test_models.py tests/api_v7/test_tool_manifest.py tests/api_v7/test_factory.py`
  - exit 0: 28 passed, 275 subtests passed
- `pytest -q tests/test_migrations.py tests/test_database_v7.py`
  - exit 0: 21 passed, 14 subtests passed
- `uv run --python 3.10 --no-project python -m py_compile ...`
  - exit 0 for all changed production modules
- `git diff --check -- <changed files>`
  - exit 0
- Scoped mypy with `--ignore-missing-imports --follow-imports=skip` on
  `discovery_projection.py`, both API operation modules, and
  `verification_v7.py`: exit 0. A normal dependency-following run reports no
  errors in those four owned modules; 137 errors remain in imported modules
  outside this slice.
- Ruff on the four production modules and three focused test modules: exit 0.

## Post-review repairs

- Ambiguous exact, module, and leaf resolution now fails closed. A relationship
  is persisted only when the selected resolution tier has exactly one target;
  module imports bind only an actual `module` or `file` entity. Duplicate leaf
  names, methods, qualified names, unresolved aliases, and member-only module
  prefixes no longer manufacture authenticated dependency edges.
- Runtime reads and offline verification now share
  `verify_code_projection()`. It recomputes entity identities, public-ID
  bindings, edge endpoints and identities, both partition digests, and the
  combined manifest root under explicit row bounds. `verify_v7` treats the code
  root as a code-generation digest rather than a memory-event root and requires
  the code entity, edge, partition, and public-ID tables.
- Verifier regressions cover a pristine active generation, a missing edge
  table, deleted/inserted/modified edges, a corrupt public-ID binding, and
  partition and manifest hash corruption.
- The enabled production process flow now runs through both stdio and
  Streamable HTTP.
- Post-review focused unit suite: 100 passed and 2 subtests passed. Real process
  suite: 3 passed. Python 3.10 compilation, scoped mypy, and Ruff all exit 0.

## Review notes

- Upgraded databases with an older active code generation intentionally fail
  code partition verification until `code_index` rebuilds it under schema 26;
  accepting an old generation would claim edge completeness that was never
  recorded.
- The established multi-language parser contract is narrower than Python:
  JavaScript and TypeScript contribute their existing file-level import facts,
  while the other advertised languages contribute entities only. The legacy
  producer initializes `calls` as empty for every language and does not extract
  references, so v7 does not claim call/reference impact for those languages.
  A focused contract regression fixes that boundary until a language-specific,
  scope-aware relationship extractor is implemented and reviewed.
- Dependency-following mypy still exposes 137 repository-wide errors in
  imported storage, retrieval, graph, optional-provider, and legacy modules.
  The four owned code-impact/verifier modules contribute none of those errors.
