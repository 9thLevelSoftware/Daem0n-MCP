# P4 code-impact independent final re-review

Date: 2026-09-17

## Decision

**ACCEPTED for the bounded P4 code-impact slice.** The two original HIGH
findings and the follow-up dotted-receiver finding are repaired. No material
finding remains in the reviewed migration, immutable code projection,
resolver, verifier, or real MCP code-index/search/impact flow.

This does not accept the full P4 phase or v7 release. External performance,
enabled optional-platform coverage beyond the reviewed parser profile, soak,
and final-commit evidence remain separate gates.

## Final HIGH re-review — resolved

Affected component: `daem0nmcp/api/v7/discovery_operations.py`.

Python relationship extraction now preserves explicit import aliases and
relative import levels. It rejects computed receivers and conservatively drops
assigned or parameter-shadowed bindings. Resolution is case-sensitive, never
falls back from a dotted expression to a global leaf, and allows a bare-name
fallback only to declarations in the caller's source file. Relative names are
resolved against the source package path. File-level imports are attached only
to an entity that uses the binding or contains the import, avoiding the former
all-functions dependency inflation.

The previous adversarial probe now returns no target for `run`, `unknown.run`,
or `other.Service.run`; only exact `service.Service.run` selects the indexed
method. Focused regressions also prove alias and relative-import resolution,
parameter shadowing, computed-receiver rejection, local bare-name scope, and
case sensitivity. The real stdio and Streamable HTTP fixture includes a
relative aliased import plus unrelated parameter and dynamic receivers, and
reverse impact includes only the true two-hop callers.

## Earlier HIGH re-review — resolved

- Ambiguous exact, leaf, and module candidate sets fail closed. Module imports
  bind only an actual `module` or `file` entity; duplicate symbols do not create
  authenticated guessed edges.
- Runtime reads and `verify_v7` share `verify_code_projection()`. It recomputes
  entity identities and public IDs, bindings, edge endpoints and identities,
  entity/edge digests, and the combined manifest root under explicit bounds.
- `verify_v7` gives code manifests code-specific root semantics and requires
  the entity, edge, partition, and public-ID tables. It accepts a pristine code
  generation and rejects missing edge schema, inserted/deleted/modified edges,
  corrupt bindings, and partition or manifest digest changes.

## Reviewed behavior without additional material findings

- Migration 26 keeps entity and edge endpoints within one workspace and
  generation, restricts edge kinds, rejects self-edges, indexes reverse
  traversal, and protects persisted rows from update/delete.
- Publication inserts IDs, entities, edges, partition metadata, and manifest
  activation in one transaction. A repeated bounded source selection and hash
  pass detects additions, removals, selection changes, and content changes
  before commit. Cancellation is joined and a late committed receipt wins.
- Runtime search and impact verify the complete generation before returning.
  Traversal and output cardinalities are bounded and over-limit work maps to
  `TASK_REQUIRED`; stale generations and format-6 projection tables are not
  accepted.
- Python supplies call/import/reference facts. JavaScript and TypeScript retain
  their file-level import contract; other advertised legacy languages provide
  entities only and do not claim call/reference impact.
- Apps capability checks happen before optional parser loading. Workspace
  authorization and exact typed handler admission remain in the production
  path.

## Independent verification

- Targeted resolver/language regressions: **4 passed** with 78 unrelated tests
  deselected in 1.42 seconds.
- Real production stdio/Streamable HTTP code-impact process tests plus the full
  verify-v7 integrity suite: **57 passed** in 54.12 seconds.
- Direct replay of the prior dotted-name probe produced:

```text
run -> []
unknown.run -> []
other.Service.run -> []
service.Service.run -> ['wanted']
```

The independent checks and source review support bounded P4 code-impact
acceptance. They make no claim about the remaining full-release gates.
