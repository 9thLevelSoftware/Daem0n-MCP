# P2 whole-store verification and projection recovery

This slice adds `python -m daem0nmcp.cli verify-v7 --workspace-id <id>` and the
explicit offline `--repair-projections` mode.

Verification takes a consistent SQLite backup while holding the storage
generation lock. It checks the complete schema ledger, SQLite integrity and
foreign keys, canonical memory-event bundles, governance event hashes and
stream dependencies, federation link-event chains, session update coverage,
legacy migration-map provenance, activation-pointer/run/snapshot metadata, and
projection manifests. Active local projection generations are checked against
their SQLite rows, builder digests/configuration, event cursor, row count, and
source root; external or seed-dependent generations must either have consistent
local references or be explicitly marked `rebuild_required`. Memory and
governance authority are replayed into a new
format-7 database and the six canonical derived tables are compared by exact
ordered row digest.

Repair refuses to run if any authority, mapping, sequence, pointer, SQLite, or
schema check fails. For repairable derived-state divergence it retains a
physical source snapshot, creates a separate candidate from the active store,
replaces only replay-derived canonical tables, rebuilds SQLite-local lexical,
graph, temporal, procedure, and outcome generations, and validates the full
candidate before atomically replacing `active-db.json`. The prior database is
preserved in `previous_db`. Dense and seed-dependent code, community, and
entity generations are marked `rebuild_required`; repair never calls an
external vector provider or invents missing discovery seeds.

Each repair candidate is bound to the complete logical inventory of its source
backup and to the active pointer bytes and generation. A retry cannot activate
a candidate after any table changes in the selected source. Recovery path
components are checked with `lstat`, Windows reparse metadata, and resolved
containment before exclusive partial-file creation. Snapshot and candidate
files are fsynced and atomically published. Incomplete runs are preserved with
an `.incomplete-N` suffix and rebuilt; a fully validated pre-pointer candidate
is resumable.

Fault tests interrupt after run-directory creation, each snapshot/candidate
copy boundary, canonical replay, manifest rebuild, run-row commit, candidate
validation, candidate fsync, and immediately before pointer publication. The
original generation remains selected at every boundary. A stale-candidate
regression writes a queued background job after a pre-pointer interruption and
proves the new candidate is rebuilt from and preserves that later state.

Migration verification requires the map key to equal the canonical legacy
claim carried by its imported event, validates expected event type/actor/run,
rejects duplicate active claims, detects unmapped migration events, recomputes
retained source-row hashes, and checks run inventory/map/checkpoint counts in
both directions.

Focused evidence:

```text
.venv/Scripts/python.exe -m pytest tests/test_verify_v7.py -q
# 33 passed

.tmp/venv312/Scripts/python.exe -m ruff check \
  daem0nmcp/verification_v7.py tests/test_verify_v7.py
# All checks passed
```

This slice does not claim the rest of P2. Existing migration rollback coverage
is unchanged, and final release evidence still needs final-commit platform and
failure-matrix execution. Provider-backed dense rebuilding and discovery
seed regeneration remain explicit follow-up operations after local recovery.
