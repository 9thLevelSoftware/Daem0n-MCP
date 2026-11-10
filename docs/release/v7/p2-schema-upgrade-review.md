# Existing-format-7 schema upgrade independent review

Date: 2026-09-17

Status: **BOUNDED ACCEPTANCE at schema 32**. The three original findings, the
schema-ledger contract gap, and the fresh-store ORM/ledger divergence are
repaired. The review found no remaining defect in the existing-format-7
upgrade boundary.

## Findings

### RESOLVED HIGH — Ledger-complete databases could omit required physical schema

Affected paths:

- `daem0nmcp/migrations/v7_schema_upgrade.py:74-115`
- `daem0nmcp/migrations/v7_schema_upgrade.py:191-212`
- `daem0nmcp/migrations/v7_schema_upgrade.py:633-659`
- `daem0nmcp/migrations/v7_schema_upgrade.py:784-821`

`_schema_versions` proves only that version rows 16 through the maximum are
present. `_verify_published_candidate` delegates to the whole-store verifier,
whose schema check likewise proves required table names and ledger rows, but it
does not attest migration-defined indexes or columns. Resume and pointer
recovery therefore accept a published candidate whose migration ledger claims
versions 30 and 31 after required DDL has been removed. The ordinary
already-current path has the same behavior.

Two independent probes interrupted after candidate publication, altered only
the candidate schema, and retried `migrate-v7 --apply`:

```text
result activated resume missing_index_activated True
altered True result activated resume missing_column_activated True
```

The first dropped `idx_memory_records_briefing_type`. The second dropped
`dense_projection_refs.vector_sha256`. A third probe removed both objects from
an active schema-31 store; apply returned `already_active`. Integrity and event
replay still pass because these objects contain no authority rows, but runtime
queries can lose their certified query plan or fail when they use the missing
column.

Add a read-only physical schema contract check to current-store inspection,
post-migration candidate validation, resume, pointer recovery, and
reactivation. For migration 30 it should validate the exact indexed table,
ordered columns, and direction through `PRAGMA index_xinfo`, rather than the
index name alone. For migration 31 it should validate both columns and their
declared constraints. Regressions should alter a published candidate and a
ledger-current source and prove both fail closed without changing the pointer.

### RESOLVED MEDIUM — Dry-run bypassed the storage-generation lock

Affected path:

- `daem0nmcp/migrations/v7.py:1318-1367`

Apply and rollback hold `DatabaseFileLock(storage, "exclusive")`, but dry-run
resolves the pointer and performs schema/inventory reads without even a shared
generation lock. Those reads use the platform no-lock SQLite VFS to preserve
database bytes. A probe held the storage's exclusive lock and still obtained:

```text
dry_run_bypassed_exclusive_lock upgrade
```

This permits inspection to race pointer publication or a cooperating database
writer, so its schema, identity, and disk estimate need not describe one active
generation. Hold the shared generation lock from pointer resolution through
inspection. Add a regression proving an exclusive holder rejects or blocks the
dry-run and that pointer resolution occurs inside the acquired lock.

### RESOLVED LOW — The reviewed module was not mypy-clean

`_rolled_back_candidate` derives `expected` as `str | None` and then calls
`expected.split` after a condition that mypy does not narrow. The scoped check
reports:

```text
daem0nmcp/migrations/v7_schema_upgrade.py:891: error: Item "None" of
"str | None" has no attribute "split"  [union-attr]
```

The preceding runtime condition makes the current execution safe. Construct
`expected` only after returning for a missing run ID, which also leaves the
branch easier to audit.

## Verified behavior

- Python 3.12 focused suite: `18 passed` in 14.90 seconds, exit 0.
- Scoped Ruff: exit 0.
- CLI help advertises `--apply` as an offline format or schema upgrade, exit 0.
- An actual `python -m daem0nmcp.cli --json ... migrate-v7 --apply` against a
  schema-29 store returned `activated`, `source_format=7`, `schema_from=29`,
  `schema_to=31`, and generation 2. The source database bytes were unchanged.
  The active candidate contained all three migration-30 indexes, both
  migration-31 vector attestation columns, and ledger versions 16 through 31.
- The focused suite covers every declared durable construction/publication
  interruption, rollback-pointer interruption, candidate reactivation,
  earliest supported schema 16, future and incomplete ledgers, source
  authority corruption, disk refusal before candidate creation, exclusive
  apply locking, malformed candidate refusal, and current-schema database and
  pointer byte invariance.
- Inspection confirms final cutover re-resolves the active pointer and compares
  a full logical source identity while the exclusive generation lock is held.
  Rollback publishes format 7, retains both databases, and reactivation checks
  the rolled-back source identity before reuse.

Temporary reproduction scripts are retained under `.tmp/p2_schema*_probe.py`.

## Schema-31 repair re-review

### RESOLVED MEDIUM — `schema_version` was wholly exempt from the physical contract

Affected component:

- `daem0nmcp/migrations/v7_schema_upgrade.py:467-472`

The new semantic contract validates every expected application table, column,
foreign key, check, explicit and automatic index, trigger, and view, but skips
the complete `schema_version` table contract. Ledger row coverage is checked
separately, yet its storage types, primary key, and default are not. A probe
rebuilt a ledger-current database as:

```sql
CREATE TABLE schema_version(version TEXT, applied_at BLOB)
```

It copied versions 16 through the current target as text, removed the primary
key and timestamp default, and dry-run still returned:

```text
weakened_schema_ledger_accepted already_active
```

This previously permitted duplicate or non-integral future ledger writes and
removed the declared application-time behavior from the table that controls
which schema migrations are trusted. The repair now validates this table with
the same semantic contract as other tables. Current-store and published-candidate
regressions rebuild it with text/blob columns, no primary key, and no default;
both fail closed without pointer mutation. The authentic D967 schema-29 table,
`version INTEGER PRIMARY KEY, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP`,
remains compatible with the generated reference.

### Verified repairs at the schema-32 boundary

- The physical contract derives the expected schema by applying the supported
  migration ledger to an isolated database. It compares SQLite affinity,
  nullability, primary-key position, generated-column state, defaults, foreign
  keys, checks, table flags, exact index keys/collations/direction/uniqueness,
  partial predicates, automatic unique indexes, triggers, and views.
- Current stores and published candidates fail closed after a required index or
  column is removed. Additional probes confirmed published candidates are also
  rejected after weakening the migration-31 vector checksum check, removing a
  nonhistorical default, reversing a migration-30 index direction, or changing
  an active-manifest partial predicate.
- The default-omission allowlist has 24 entries. An independent comparison with
  `Base.metadata.create_all` found exactly the same 24 non-nullable columns,
  with no allowlist extras and no uncovered historical omissions. Missing these
  SQL defaults makes direct omitted-value writes fail rather than selecting a
  weaker value. Nonhistorical defaults remain mandatory.
- Affinity normalization matches SQLite's documented coercion classes; exact
  column names, ordering, nullability, primary-key position, generated state,
  checks, and defaults remain independently enforced. Numeric default
  normalization is limited to numeric-affinity columns and finite equal values.
- Dry-run now holds a shared generation lock before pointer resolution and
  through inspection. Its exclusive-lock conflict and resolve-inside-lock
  regressions pass.
- The prior Optional narrowing error is repaired. Scoped mypy reports no issues.

### Interim re-review evidence

- Python 3.12 focused schema-upgrade suite: **31 passed**, exit 0.
- Python 3.10 focused schema-upgrade suite: **31 passed**, exit 0.
- Scoped Ruff and mypy: exit 0.
- Authenticated artifact provenance was checked: the installed wheel SHA-256 is
  `D9673A5650A738D40C53EFA5F2BD0711AEBD12332F85D2B4AF6A3B2CF1658D6C`,
  and its isolated installed package reports schema 29.
- A fresh store created by that installed artifact passed current-code dry-run
  and apply. Both exited 0, reported `source_format=7`, retained the source
  database byte-for-byte, applied migrations 30 through 32, and activated
  generation 2 at target schema 32.
- Detailed authentic-artifact output is retained in
  `.tmp/p2-d967-schema29-result.json`; semantic and allowlist probes are retained
  under `.tmp/p2_*probe.py`.

## Final target-32 finding

### RESOLVED HIGH — Fresh schema-32 ORM stores failed the exact physical contract

Affected components:

- `daem0nmcp/models.py` dense projection, lease, and GC-job models
- `daem0nmcp/migrations/schema.py` migration 32
- `daem0nmcp/migrations/v7_schema_upgrade.py:52-77`

Migration 32 declares SQL defaults for four non-null columns:

- `dense_generation_read_leases.projection_name = 'dense'`
- `dense_generation_gc_jobs.projection_name = 'dense'`
- `dense_generation_gc_jobs.attempts = 0`
- `dense_generation_gc_jobs.max_attempts = 3`

The corresponding SQLAlchemy columns use Python-side defaults, so
`Base.metadata.create_all` omits these clauses. An independent comparison of a
fresh ORM schema with the migration-derived reference found 28 missing SQL
defaults: the 24 evidenced historical omissions plus exactly these four new
schema-32 omissions. The historical allowlist remains at 24, correctly causing
the physical validator to reject this new divergence.

The full table-contract comparison found two additional incompatibilities:

- `dense_projection_refs` places the migration-31 `vector_format` and
  `vector_sha256` columns before `updated_at_us`, while migration 31 appends
  them after it. Exact column ordering therefore differs.
- The ORM forms of `dense_generation_read_leases` and
  `dense_generation_gc_jobs` retain only 4 CHECK constraints each. The
  migration-derived tables have 9 and 11 respectively, including bounded IDs,
  integer type/range checks, lease timing, collection/error bounds, and the
  owner/token/expiry state invariant.

A real current-code startup probe then exercised the production constructor:

```text
DatabaseManager.init_db: exit 0
migrate-v7 dry-run: exit 1, V7_PHYSICAL_SCHEMA_INVALID
```

The database and pointer are published as schema 32 before the discrepancy is
reported. This affects every fresh schema-32 store and is distinct from the
successful D967 schema-29 upgrade, where migration 32 creates the tables with
the declared defaults.

Fresh construction now reproduces the migration-ledger contract: the four
columns have matching SQL `server_default` clauses, the migration-31 columns
retain their append order after `updated_at_us`, and both migration-32 tables
declare the complete CHECK set. The 24-entry historical default allowlist was
not widened. A production-path regression creates the database through
`DatabaseManager.init_db` and immediately validates it as current.

Reproduction output is retained at `.tmp/p2-fresh-schema32-result.json` and the
exact ORM/reference comparisons at `.tmp/p2_orm_allowlist_probe.py` and
`.tmp/p2_fresh_contract_detail.py`.

## Final schema-32 evidence

- Python 3.12 focused suite: **32 passed** in 33.95 seconds, exit 0.
- Python 3.10 focused suite: **32 passed** in 34.39 seconds, exit 0.
- The adversarial current/candidate, schema-ledger, interruption, and rollback
  subset: **18 passed** in 26.03 seconds, exit 0.
- A new production `DatabaseManager.init_db` store had no migration-derived
  table-contract mismatches. Actual CLI dry-run and apply both exited 0 with
  `already_active`; database and pointer SHA-256 values were unchanged.
- The ORM/reference default comparison again found exactly the 24 evidenced
  historical omissions, with no missing or extra allowlist entries.
- Published candidates with a weakened CHECK, missing nonhistorical default,
  reversed index direction, or changed partial predicate were independently
  rejected with `ACTIVE_V7_INVALID`.
- A new store created by the isolated D967 wheel (SHA-256
  `D9673A5650A738D40C53EFA5F2BD0711AEBD12332F85D2B4AF6A3B2CF1658D6C`)
  upgraded from schema 29 to 32 through the actual CLI. Dry-run and apply exited
  0, apply activated generation 2, the original source bytes were unchanged,
  and the candidate contained ledger versions 16 through 32, the migration-30
  indexes, and migration-31 columns. Evidence is retained in
  `.tmp/p2-d967-schema29-final-result.json`.
- Scoped Ruff, mypy, and `git diff --check` passed. The latter emitted only
  existing line-ending normalization warnings for two shared files.
