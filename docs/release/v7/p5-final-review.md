# P5 lease/quota final independent review

Date: 2026-09-17

Status: **ACCEPTED (bounded P5 scope)**. The expired-owner quota failure and
the provider-artifact cleanup failure from the prior reviews are resolved. No
material P5 finding remains in the reviewed lease, quota, cancellation, vector
canonicality, or verifier-repair paths. Whole-release acceptance remains
**0/571**, and no final release commit is accepted by this review.

## Final repair verification

- Expired-owner retirement removes only the fenced attempt directory,
  recomputes physical usage, deletes the expired lease by owner and expiry, and
  commits staging state and exact accounting before the successor capacity
  check. A later capacity refusal cannot restore the old lease or reservation.
- A delayed release or commit from the expired owner cannot affect a successor.
  Renew, release, and final publication use owner compare-and-set checks, and
  the final ownership/expiry check and lease deletion occur inside the
  canonical commit transaction.
- Migration 29 durably registers the owner-scoped temporary Qdrant collection
  before provider creation. Candidate names and validation files are private to
  the lease owner.
- Failed candidate preparation now uses one cleanup path for both stable
  `PortableTransferError` values and unexpected provider failures. Cleanup and
  close failures do not replace the original stable error.
- The cleanup path removes its durable artifact row only after a provider
  existence check proves the collection absent. If deletion fails or absence
  cannot be confirmed, the row remains for a later finalization owner.
- Provider deletion checks active dense manifests across every workspace in the
  store because collection names are provider-global. The same guard is used by
  failed preparation, candidate discard, and expired-owner reclamation.
- A successor can reclaim a retained prior-owner collection and clear its exact
  row. An active manifest in another workspace blocks that deletion until the
  manifest is no longer active.
- Candidate activation and canonical import run inside the final SQLite
  transaction. Registry mutations therefore roll back with later activation or
  owner-fence failures, while provider cleanup retains retry state when an
  external delete fails.
- Export still reserves workspace capacity before file creation, rejects a
  known oversized SQLite source before snapshot creation, and checks
  cancellation, logical source growth, and retained allocation during bounded
  SQLite backup progress steps.
- Offline verifier repair uses the exclusive generation lock, deletes
  process-bound leases only in its separate candidate, resets finalizing
  sessions to staging, retains staged pages and provider-artifact rows, and
  removes orphan attempt files only after pointer publication.

## Independent probes and checks

- The original double-failure probe was replayed with collection creation
  succeeding, exact-count validation raising unexpectedly, and provider
  deletion raising. It now returns:

  ```text
  result=CAPABILITY_DEGRADED
  provider_collection_created=True
  provider_delete_failed=True
  durable_registry_retained=True
  client_closed=True
  ```

- The new four-case recovery matrix covers unexpected validation failure versus
  `CANCELLED`, each with and without an active other-workspace manifest. With
  the lease fencing regression, the focused run passed **5 tests** in 1.85
  seconds.
- The complete Python 3.12 portable projection suite passed **25 tests** in
  15.28 seconds.
- Focused Ruff for the implementation and portable tests reported **all checks
  passed**.
- The earlier combined portable, operations, migration, and verifier selection
  passed **101 tests and 5 subtests** before this final narrow cleanup repair.
  The independently rerun portable suite covers the files changed by that
  repair; release-scale integration remains with the parent review.
- `.tmp/p5-mcp-qdrant-final-repair.log` records an authenticated production
  stdio roundtrip with `pages=2 imported=1`. Inspection found no credential
  values. This evidence covers the ordinary enabled-provider path; deterministic
  fault probes cover the repaired deletion-failure path.
