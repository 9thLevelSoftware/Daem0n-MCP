# P5 portable export/import report

## Delivered protocol

Workspace export format 2 is a durable, frozen snapshot protocol. The first call
creates a SQLite backup and, when requested, captures the complete active Qdrant
generation before publishing any page. Pages are canonical JSON, individually
hashed, limited to 4,096 items and at most 850,000 encoded bytes, and bound to a
manifest hash. Export cursors are origin-local HMAC capabilities; portable import
trust comes from page hashes, section hashes, the authoritative event root, and
full event-envelope replay validation.

The manifest is a compact session header. It carries a Merkle root for the page
table rather than the full table; each response carries only its current page
descriptor and a logarithmic proof. A deterministic 65,536-page probe produces
a sub-kilobyte manifest and a 16-hash proof. The implementation also rejects a
serialized page response above 1 MiB, leaving room inside both 2 MiB MCP
transport limits.

Import stages pages under a deterministic import session without changing
canonical state. Finalization requires every declared page, reconstructs a
bounded local event index, validates event stream and causation continuity, and
replays the complete authoritative history in one SQLite transaction. A failure
or cancellation rolls back canonical state. Format-1 event-only bundles remain
accepted.

Migration compatibility identities are reconstructed for both format-1 and
format-2 imports from authoritative `actor_type=migration` events. Memory, fact,
relationship, and orphan-placeholder mappings retain the original correlation
ID, exact lossless `legacy` payload hash, target stream, and imported event ID.
Portable-derived run metadata uses source format 7 and status `ready`; it does
not claim a retained v6 source database. Optional legacy rows must exactly match
the canonical replay and its event-derived compatibility identity.

Optional dense vectors include the schema, provider, model, dimension, distance,
generation, projection and builder versions, storage/build hashes, full encoder
contract, event root, row count, deterministic point IDs, canonical safe
payloads, and exact transport checksums. Import rejects malformed contracts and
returns `VECTOR_REBUILD_REQUIRED` for unsupported semantics. It builds a unique,
attempt-owned candidate collection and enumerates it with bounded scrolls. Candidate vectors
are compared with cosine-normalized float32 values using numeric tolerance.
Activation repeats candidate validation and binds every point to a live canonical
record. It also requires exact target event count/root and complete live-record
coverage. Merge into a canonical superset therefore commits the authoritative
events, leaves the imported vectors inactive, and reports
`VECTOR_REBUILD_REQUIRED`. Provider access requires the `local` capability to be
`ready`; disabled export never constructs a client and disabled import performs
no external write.

Migration 24 adds durable transfer session and page metadata. Sessions have a
24-hour expiry, a four-session workspace quota, and a 4 GiB aggregate quota.
An export reserves quota and a `building` row under `BEGIN IMMEDIATE` before
creating files. The source page inventory must fit before a backup file is
created; SQLite backup runs with 64 KiB progress checkpoints that enforce quota
and cancellation and remove the partial file and reservation on interruption.
Publication deletes the source backup and records the complete physical
session-directory size. Reconciliation removes orphan directories and expired
or abandoned builders. Import accounting includes staging, validation, and
successful retained sessions, and validation refuses insufficient physical
capacity before creating its derived event database.
The finalizer reserves the remaining workspace allowance in the session row,
serializing concurrent validation builders; release and successful cleanup
restore `total_bytes` to the measured retained files.

Migration 29 adds a separate owner-fenced finalization lease. Claims use random
64-hex owner tokens, renew by compare-and-set, and keep all derived validation
files under `attempts/<owner-token>/`. Release and final publication require the
same owner; the final expiry/owner check and lease deletion occur inside the
canonical commit transaction. An expired worker can no longer reset the new
owner's session, delete its files, or commit canonical events. Cancellation is
checked while validating pages and events and on both sides of bounded provider
calls. Partial imports remain staging-only; complete imports journal an
idempotent receipt. Expired-owner recovery removes only the fenced owner's
private attempt and commits exact physical accounting before attempting a new
reservation. A capacity refusal therefore cannot roll the expired lease and its
stale reservation back into service. Temporary Qdrant collection identities are
derived from the lease owner and registered durably before creation. A successor
deletes registered prior-owner collections only after confirming that no active
dense manifest references them.

Offline `verify-v7 --repair-projections` treats these leases as transient
process state. Its separate candidate deletes lease rows and resets finalizing
imports to staging while retaining every staged page. Only after atomic pointer
publication does it remove orphan attempt directories and replace the temporary
reservation with measured physical bytes; interruption before publication
leaves the original database and its staging state untouched.

## Verification evidence

- Portable component and operations suites cover frozen multi-page snapshots,
  tamper rejection without canonical mutation, legacy replay comparison,
  format-1 compatibility, identity preservation, all migration mapping kinds,
  same-timestamp relationship dependencies, vector candidate activation, and a
  real v6 migration roundtrip whose imported store passes `verify_v7`, compact
  maximum-page manifests, physical quota accounting and orphan recovery,
  pre-backup quota refusal, backup interruption cleanup, bounded validation
  staging, committed export cancellation, disabled-provider non-use, structural
  provider validation, provider cancellation cleanup, full vector contract
  rejection, merge coverage, expired-lease takeover fencing, and concurrent
  finalizer exclusion, crash-sized attempt reclamation without restaging, and
  inactive prior-owner provider artifact reclamation. A repair regression also proves lease scrubbing, staging
  page retention, foreign-key integrity, and post-activation byte accounting.
  The focused portable/operations selection passes 40 tests and 5 subtests; the
  combined portable, operations, migration, and verifier selection passes 101
  tests and 5 subtests.
- Event-bundle regression coverage confirms deterministic topological replay
  with explicit stream/causation dependencies plus semantic memory dependencies
  for facts and relationships.
- An authenticated local Qdrant roundtrip through the actual stdio MCP server
  exported two pages, reset the same workspace identity to a fresh store, and
  imported one event/vector successfully against `127.0.0.1:16333` after the
  review repairs. Credentials
  were loaded from the certification environment and were not logged. The test
  `local` profile was explicitly enabled. Evidence is retained at
  `.tmp/p5-mcp-qdrant-final-repair.log`.

## Operational boundary

The transfer RPC stays below the 2 MiB transport ceiling through an 850,000-byte
content budget and a 1 MiB complete-response limit. Histories of 100k or 1m
events use the same bounded paging path; no response carries the full page table
or buffers the whole event history. Snapshot and staged files remain within the
reserved workspace quota until expiry cleanup. Qdrant and SQLite cannot share
one physical transaction, so attempt-owned candidates remain isolated and
unreferenced until SQLite activation; cleanup rechecks active-manifest ownership
before deleting a collection.
