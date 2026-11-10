# P5 repair re-review

Status: **needs work**. Coordinator review of the repaired implementation found
two remaining release-blocking resource and concurrency defects. This does not
invalidate the ordinary enabled Qdrant roundtrip; it blocks acceptance of the
failure/recovery contract.

## HIGH: expired finalization leases lack ownership fencing

`claim_import_finalization` records only `status='finalizing'` and a timestamp.
It returns no owner token, has no renewal, and permits another claimant after
15 minutes. Import preparation and provider I/O can outlive that interval;
background execution permits up to an hour and preparation does not check the
caller cancellation flag during validation/provider work.

Both claimants use the same `validated-events.db`, `legacy.jsonl`, and
`vectors.jsonl`. `prepare_import` deletes these paths when starting. The second
claimant can therefore remove/rewrite the first claimant's live validation data.
`release_import_finalization` then updates any `finalizing` row without checking
ownership, so an old failed worker releases the new worker's lease. Canonical
commit also does not fence against a lost claim.

Deterministic probe `.tmp/p5_lease_probe.py` claims at time T, claims again at
T+16 minutes, then performs the first worker's cleanup:

```text
second_claim_after_expiry=finalizing
old_owner_release_after_new_claim=staging
```

Required repair: a durable random claim owner/fencing token, renewal or a hard
enforced lifetime, compare-and-set ownership for renew/release/publication,
attempt-private validation paths, and cancellation checks during preparation and
provider I/O. Test expiry/takeover while the original worker is still active and
prove stale cleanup cannot release/delete/commit the winning attempt.

## HIGH: export backup exceeds its physical allowance before the first check

`create_export_session` reserves the remaining workspace allowance, but executes
`connection.backup(snapshot)` without a progress callback or pre-backup size
check. Its first cancellation and physical-size checkpoint runs only after the
entire database has been copied. A source larger than the allowance can fill the
disk or spend unbounded time copying before returning `TASK_REQUIRED`.

Required repair: reject a known oversized source before creating its snapshot,
enforce the remaining allowance and cancellation during SQLite backup, and
cleanly release the building reservation/partial files on interruption. Bound
validation staging similarly rather than checking physical size only after
expensive writes.

## Verified repairs and evidence

Inspection confirms explicit local-profile gating, compact Merkle page proofs,
unique vector candidate collection names, canonical-root/record coverage checks,
and expanded vector compatibility validation. Export publication returns its
committed resumable receipt after late cancellation.

Independent focused P5/operations run: **34 passed, 5 subtests**, exit 0, log
`.tmp/p5-root-rereview-tests.log`. The existing concurrent-finalizer regression
checks only an unexpired lease, so it does not exercise the takeover defect.

Real multi-gigabyte interruption, supported platforms, and final-commit evidence
remain open in addition to these findings.
