# P2 bounded independent re-review

Verdict: **ACCEPTED for the verification/recovery and async-cursor slices reviewed here; no material finding remains.** This is not whole-P2 or release acceptance.

The review covers the current `verification_v7.py`/`verify-v7` implementation and tests, plus the previously reviewed async cursor portability changes in `event_store.py`, `retrieval/job_queue.py`, and `test_database_v7.py`, against base `cc08b4f`.

## Remediation verification

| Finding | Result | Independent evidence |
|---|---|---|
| Interrupted repair could activate a stale candidate and lose later data | **Resolved** | Recovery identity includes the logical source inventory and pointer state; publication re-backs up and compares the active source immediately before the atomic pointer write (`verification_v7.py:1260-1267`, `1340-1358`). The retained original and candidate are validated separately. |
| Projection corruption and false manifest metadata could report as verified | **Resolved** | Unsupported names now fail closed, extant lexical/procedure/outcome generations require a matching manifest, active local contents are validated, and candidate repair deletes unsupported manifests plus orphaned local generations/FTS tables before rebuilding (`verification_v7.py:641-780`, `931-972`). The five-name renamed-manifest regression repairs each known local projection and a second verification succeeds. |
| Migration-map keys were not checked against event provenance and runtime claim semantics | **Resolved** | Every map is bound to its migration event, exact source table/ID/hash, target stream/kind, run, and event type. After provenance checks, the verifier calls the production `build_live_compatibility_claim_index` on verified replay state (`verification_v7.py:507-638`). Active, ready, failed, and rolled-back duplicate claims all fail when both identities are live and pass when one identity is retired. |
| Recovery could write through linked/reparse migration parents | **Resolved** | Recovery validates owned regular files and every parent/child containment boundary before exclusive creation and publication. The focused suite retains the Windows junction escape regression. |
| Mid-construction interruptions were permanently non-resumable | **Resolved** | Fsynced partial files are published only after their construction boundary, complete candidates resume, incomplete runs archive, and the candidate is fully reverified before publication (`verification_v7.py:1273-1339`). |
| Explicit repair ignored normally invalidated local projections | **Resolved** | `_verify_manifests` reports the newest local generation as repair-required and the repair no-op condition now requires `local_rebuild_required == 0` (`verification_v7.py:664-681`, `1251-1256`). The ordinary EventStore invalidation probe activates one rebuilt generation; the next repair is a no-op. |

The three original adversarial probes were repeated through their committed parameterized regressions: unsupported/renamed local manifests, live duplicate compatibility claims in every migration-run status with and without a retired identity, and an ordinarily invalidated lexical projection. All 14 parameter combinations passed.

## Async cursor portability slice

**Accepted; no material finding.** `_bounded_cursor_rows` uses bounded `fetchmany` and closes cursors, the synchronous facade operates on SQLAlchemy's adapted aiosqlite connection without introducing a second transaction, and the job queue accepts the structural SQLite surface. The prior dependency-backed integration demonstrated append, compatibility scanning, commit visibility, rollback isolation, and review-probed export/import behavior. This slice was unchanged in the current remediation round and was not rerun.

## Verification evidence and scope

- `.venv/Scripts/python.exe -m pytest` on the three repaired regression groups — **14 passed** in 9.09 seconds.
- `.tmp/verify-root-review-fixes.log` — **46 passed** in 38.92 seconds for `tests/test_verify_v7.py`.
- `git diff --check` for the P2 verification files — passed.
- The coordinator-provided broader focused evidence (**86 passed, 4 skipped, 22 subtests**) was not repeated.

Whole-P2 acceptance remains outside this review. Retained rollback/reactivation, every supported legacy migration fixture, inspection/recovery commands, platform and I/O failure matrices, multi-workspace/WAL/lock-contention evidence, and final-commit release evidence still require their own acceptance.
