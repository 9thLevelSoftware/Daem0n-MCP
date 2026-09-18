# P1 legacy MemoryManager v7 compatibility re-review

Date: 2026-09-17

## Decision

The bounded legacy compatibility repair is **accepted**. The earlier stale cache
publication, unauthenticated event-bound alias, and 4+16 admission mismatch are
fixed under their original adversarial inputs. This is not whole-P1 or release
acceptance.

## Re-reviewed repairs

- `daem0nmcp/memory.py:1309-1338,1536-1540` now samples the same canonical
  revision before and after retrieval and caches only when the values are equal.
  With revisions `(1, 2, 2, 2)`, the first result was returned but not cached;
  the second call invoked retrieval again and returned the new snapshot.
- `daem0nmcp/retrieval/legacy_compat.py:84-158` now loads the complete immutable
  event envelope, canonicalizes and hashes the payload, recomputes the event hash
  and event ID, and omits alias/context metadata when any check fails. Corrupting
  only `compatibility.legacy_memory_id` while retaining the original hashes now
  returns the opaque record ID and no compatibility metadata.
- `daem0nmcp/memory.py:1387-1416` now admits 20 in-flight requests, allowing four
  service owners and sixteen queued callers, and returns
  `V7_RETRIEVAL_BUSY` to the 21st caller. The counter is decremented in `finally`.

The previously accepted behavior remains intact: opaque canonical IDs and
evidence provenance are preserved; historical context is exact-event-bound,
bounded, allowlisted, path-safe, and detached; linked retrieval remains uncached
and subject to caller resolution plus final link/target ACL checks; owned managers
and retrieval resources remain explicitly closed.

## Independent evidence

- Focused repair regressions:
  `.venv/Scripts/python.exe -m pytest tests/test_memory.py::TestRecallCaching::test_revision_change_during_recall_is_not_cached tests/test_memory.py::TestRecallCaching::test_v7_admits_four_active_plus_sixteen_waiting tests/test_retrieval_legacy_compat.py::LegacyRecallCompatibilityTests::test_corrupt_event_envelope_omits_legacy_metadata -q`
  — **3 passed** in 1.79 seconds.
- `.tmp/p1_legacy_cache_race_probe.py`:
  `service_calls=2`, old snapshot first, new snapshot second, and no stale cache
  publication.
- `.tmp/p1_legacy_event_hash_probe.py`:
  the tampered alias is `None` and the loader fails closed.
- `.tmp/p1_legacy_waiter_probe.py`:
  four active, one busy, and 20 successful calls at the 21-call boundary.
- The earlier broader compatibility run remains **94 passed** for
  `tests/test_memory.py`, `tests/test_index_freshness.py`, and
  `tests/test_linked_projects.py`.

