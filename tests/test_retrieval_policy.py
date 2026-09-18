"""Ordered policy gates and provenance-preserving deduplication tests."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

WORKSPACE_ID = "ws_0123456789abcdef01234567"
OTHER_WORKSPACE_ID = "ws_76543210fedcba9876543210"
DEFAULT_HASH = "c" * 64
SNAPSHOT_TIME = datetime(2026, 2, 1, tzinfo=timezone.utc)


def _record_id(digit: str) -> str:
    return "mem_" + digit * 64


def _event_id(digit: str) -> str:
    return "evt_" + digit * 64


def _fused(
    digit: str,
    provider: str = "lexical",
    *,
    score: float = 1.0,
    rank: int = 1,
    content_hash: str = DEFAULT_HASH,
    generation: int | None = 1,
    event_digit: str | None = None,
):
    from daem0nmcp.retrieval.types import EvidenceRef, FusedCandidate

    evidence = EvidenceRef(
        record_id=_record_id(digit),
        event_id=_event_id(event_digit or digit),
        content_hash=content_hash,
        version_id=None,
        provider=provider,
    )
    return FusedCandidate(
        evidence=evidence,
        evidence_refs=(evidence,),
        score=score,
        channels=frozenset({provider}),
        channel_ranks=((provider, rank),),
        manifest_generations=((provider, generation),),
    )


def _record(
    digit: str,
    *,
    channels: tuple[str, ...] = ("lexical",),
    content_hash: str = DEFAULT_HASH,
    projected_hash: str | None = None,
    generation: int = 1,
    **changes,
):
    from daem0nmcp.retrieval.policy import PolicyRecord

    values = {
        "workspace_id": WORKSPACE_ID,
        "record_id": _record_id(digit),
        "version_id": None,
        "content_hash": content_hash,
        "source_event_ids": frozenset({_event_id(digit)}),
        "visibility": "workspace",
        "visibility_allowed": True,
        "archived": False,
        "category": "decision",
        "tags": frozenset({"storage", "migration"}),
        "valid_from": None,
        "valid_to": None,
        "transaction_from": None,
        "transaction_to": None,
        "superseded_by_version_id": None,
        "has_unresolved_contradiction": False,
        "projection_content_hashes": tuple(
            sorted((channel, projected_hash or content_hash) for channel in channels)
        ),
        "active_manifest_generations": tuple(
            sorted((channel, generation) for channel in channels)
        ),
    }
    values.update(changes)
    return PolicyRecord(**values)


def _query(**changes):
    from daem0nmcp.retrieval.types import RetrievalQuery

    values = {"workspace_id": WORKSPACE_ID, "text": "migration"}
    values.update(changes)
    return RetrievalQuery(**values)


def _apply_policy(query, candidates, records, *, snapshot_time=SNAPSHOT_TIME):
    from daem0nmcp.retrieval.policy import apply_retrieval_policy

    return apply_retrieval_policy(
        query,
        candidates,
        records,
        snapshot_time=snapshot_time,
    )


class OrderedPolicyGateTests(unittest.TestCase):
    def test_retrieval_snapshot_is_required_and_timezone_aware(self):
        for invalid_snapshot in (None, datetime(2026, 2, 1)):
            with self.subTest(snapshot=invalid_snapshot), self.assertRaises(ValueError):
                _apply_policy(
                    _query(),
                    (),
                    (),
                    snapshot_time=invalid_snapshot,
                )

    def test_gates_reject_on_the_first_failure_in_the_mandated_order(self):
        candidate = _fused("1", content_hash="d" * 64, generation=2)
        state = _record(
            "1",
            workspace_id=OTHER_WORKSPACE_ID,
            visibility_allowed=False,
            archived=True,
            category="warning",
            tags=frozenset({"other"}),
            has_unresolved_contradiction=True,
            projected_hash="e" * 64,
            generation=1,
        )
        result = _apply_policy(
            _query(
                record_ids=frozenset({_record_id("2")}),
                categories=frozenset({"decision"}),
                tags=frozenset({"migration"}),
            ),
            (candidate,),
            (state,),
        )

        self.assertTrue(result.abstained)
        self.assertEqual("ALL_CANDIDATES_FILTERED", result.reason)
        self.assertEqual(1, len(result.rejections))
        self.assertEqual("scope", result.rejections[0].gate)
        self.assertEqual("WORKSPACE_SCOPE", result.rejections[0].reason)

    def test_scope_visibility_archive_category_and_tag_gates_are_distinct(self):
        candidate = _fused("2")
        cases = (
            (
                _query(record_ids=frozenset({_record_id("3")})),
                _record("2"),
                "RECORD_ID_SCOPE",
            ),
            (_query(), _record("2", visibility_allowed=False), "VISIBILITY_DENIED"),
            (_query(), _record("2", archived=True), "ARCHIVED_EXCLUDED"),
            (
                _query(categories=frozenset({"warning"})),
                _record("2"),
                "CATEGORY_MISMATCH",
            ),
            (
                _query(tags=frozenset({"missing"})),
                _record("2"),
                "TAG_MISMATCH",
            ),
        )
        for query, state, expected in cases:
            with self.subTest(expected=expected):
                result = _apply_policy(query, (candidate,), (state,))
                self.assertEqual(expected, result.rejections[0].reason)

    def test_archive_opt_in_never_bypasses_visibility(self):
        result = _apply_policy(
            _query(include_archived=True),
            (_fused("3"),),
            (_record("3", archived=True, visibility_allowed=False),),
        )
        self.assertEqual("VISIBILITY_DENIED", result.rejections[0].reason)


class BitemporalAndContradictionPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.valid_end = self.start + timedelta(days=10)
        self.transaction_end = self.start + timedelta(days=20)
        self.superseding = "fact_" + "f" * 64

    def _temporal_record(self, **changes):
        values = {
            "valid_from": self.start,
            "valid_to": self.valid_end,
            "transaction_from": self.start,
            "transaction_to": self.transaction_end,
            "superseded_by_version_id": self.superseding,
        }
        values.update(changes)
        return _record("4", **values)

    def test_candidate_valid_at_both_times_is_kept(self):
        result = _apply_policy(
            _query(
                as_of_valid_time=self.start + timedelta(days=5),
                as_of_transaction_time=self.start + timedelta(days=5),
            ),
            (_fused("4"),),
            (self._temporal_record(),),
        )
        self.assertFalse(result.abstained)
        self.assertEqual(1, len(result.candidates))

    def test_implicit_as_of_uses_one_explicit_retrieval_snapshot(self):
        future_start = _apply_policy(
            _query(),
            (_fused("a"),),
            (
                _record(
                    "a",
                    valid_from=SNAPSHOT_TIME + timedelta(seconds=1),
                ),
            ),
            snapshot_time=SNAPSHOT_TIME,
        )
        future_end = _apply_policy(
            _query(),
            (_fused("b"),),
            (
                _record(
                    "b",
                    valid_from=SNAPSHOT_TIME - timedelta(seconds=1),
                    valid_to=SNAPSHOT_TIME + timedelta(seconds=1),
                    transaction_from=SNAPSHOT_TIME - timedelta(seconds=1),
                    transaction_to=SNAPSHOT_TIME + timedelta(seconds=1),
                ),
            ),
            snapshot_time=SNAPSHOT_TIME,
        )
        exact_end = _apply_policy(
            _query(),
            (_fused("c"),),
            (
                _record(
                    "c",
                    valid_from=SNAPSHOT_TIME - timedelta(seconds=1),
                    valid_to=SNAPSHOT_TIME,
                    superseded_by_version_id="fact_" + "d" * 64,
                ),
            ),
            snapshot_time=SNAPSHOT_TIME,
        )
        future_transaction = _apply_policy(
            _query(),
            (_fused("d"),),
            (
                _record(
                    "d",
                    transaction_from=SNAPSHOT_TIME + timedelta(seconds=1),
                ),
            ),
            snapshot_time=SNAPSHOT_TIME,
        )

        self.assertEqual("NOT_YET_VALID", future_start.rejections[0].reason)
        self.assertFalse(future_end.abstained)
        self.assertEqual("INVALIDATED_VERSION", exact_end.rejections[0].reason)
        self.assertEqual("NOT_YET_RECORDED", future_transaction.rejections[0].reason)

    def test_not_yet_valid_and_not_yet_recorded_are_excluded(self):
        not_valid = _apply_policy(
            _query(as_of_valid_time=self.start - timedelta(seconds=1)),
            (_fused("4"),),
            (self._temporal_record(),),
        )
        not_recorded = _apply_policy(
            _query(as_of_transaction_time=self.start - timedelta(seconds=1)),
            (_fused("4"),),
            (self._temporal_record(),),
        )
        self.assertEqual("NOT_YET_VALID", not_valid.rejections[0].reason)
        self.assertEqual("NOT_YET_RECORDED", not_recorded.rejections[0].reason)

    def test_invalidated_version_is_excluded_unless_explicitly_requested(self):
        query_time = self.valid_end + timedelta(seconds=1)
        excluded = _apply_policy(
            _query(as_of_valid_time=query_time),
            (_fused("4"),),
            (self._temporal_record(),),
        )
        included = _apply_policy(
            _query(as_of_valid_time=query_time, include_invalidated=True),
            (_fused("4"),),
            (self._temporal_record(),),
        )

        self.assertEqual("INVALIDATED_VERSION", excluded.rejections[0].reason)
        self.assertFalse(included.abstained)
        self.assertIn(
            f"SUPERSEDED_BY:{self.superseding}",
            included.candidates[0].policy_notes,
        )

    def test_invalidation_without_provenance_fails_closed(self):
        result = _apply_policy(
            _query(include_invalidated=True),
            (_fused("4"),),
            (self._temporal_record(superseded_by_version_id=None),),
        )
        self.assertEqual("INVALIDATION_PROVENANCE_MISSING", result.rejections[0].reason)

    def test_supersession_without_timing_never_leaks_through_an_as_of_query(self):
        result = _apply_policy(
            _query(as_of_valid_time=self.start),
            (_fused("4"),),
            (
                self._temporal_record(
                    valid_from=None,
                    valid_to=None,
                    transaction_from=None,
                    transaction_to=None,
                ),
            ),
        )
        self.assertTrue(result.abstained)
        self.assertEqual("INVALIDATED_VERSION", result.rejections[0].reason)

    def test_unresolved_contradiction_is_never_bypassed_by_invalidation_opt_in(self):
        result = _apply_policy(
            _query(include_invalidated=True),
            (_fused("4"),),
            (
                self._temporal_record(
                    valid_to=None,
                    transaction_to=None,
                    superseded_by_version_id=None,
                    has_unresolved_contradiction=True,
                ),
            ),
        )
        self.assertEqual("UNRESOLVED_CONTRADICTION", result.rejections[0].reason)


class ManifestAndDeduplicationPolicyTests(unittest.TestCase):
    def test_content_and_active_manifest_checks_fail_closed(self):
        stale_evidence = _apply_policy(
            _query(),
            (_fused("5", content_hash="d" * 64),),
            (_record("5"),),
        )
        stale_projection = _apply_policy(
            _query(),
            (_fused("5"),),
            (_record("5", projected_hash="d" * 64),),
        )
        stale_manifest = _apply_policy(
            _query(),
            (_fused("5", generation=2),),
            (_record("5", generation=1),),
        )

        self.assertEqual("CONTENT_HASH_MISMATCH", stale_evidence.rejections[0].reason)
        self.assertEqual("CONTENT_HASH_MISMATCH", stale_projection.rejections[0].reason)
        self.assertEqual("MANIFEST_NOT_ACTIVE", stale_manifest.rejections[0].reason)

    def test_wrong_canonical_source_event_cannot_become_citation_provenance(self):
        result = _apply_policy(
            _query(),
            (_fused("e", event_digit="f"),),
            (_record("e"),),
        )

        self.assertTrue(result.abstained)
        self.assertEqual("SOURCE_EVENT_MISMATCH", result.rejections[0].reason)

    def test_temporal_gate_precedes_a_manifest_mismatch(self):
        now = datetime(2026, 1, 2, tzinfo=timezone.utc)
        result = _apply_policy(
            _query(),
            (_fused("6", content_hash="d" * 64, generation=2),),
            (
                _record(
                    "6",
                    valid_to=now,
                    superseded_by_version_id="fact_" + "e" * 64,
                    generation=1,
                ),
            ),
        )
        self.assertEqual("INVALIDATED_VERSION", result.rejections[0].reason)

    def test_exact_then_near_dedup_merges_all_evidence_and_channels(self):
        lexical = _fused("7", "lexical", score=0.9)
        dense = _fused("7", "dense", score=0.8)
        near = _fused("8", "graph", score=0.7)
        result = _apply_policy(
            _query(),
            (near, dense, lexical),
            (
                _record("7", channels=("dense", "lexical")),
                _record("8", channels=("graph",)),
            ),
        )

        self.assertFalse(result.abstained)
        self.assertEqual(1, len(result.candidates))
        retained = result.candidates[0]
        self.assertEqual(_record_id("7"), retained.record_id)
        self.assertEqual(0.9, retained.score)
        self.assertEqual(frozenset({"lexical", "dense", "graph"}), retained.channels)
        self.assertEqual(
            {_record_id("7"), _record_id("8")},
            {evidence.record_id for evidence in retained.evidence_refs},
        )
        self.assertEqual(("exact", "near"), tuple(item.kind for item in result.merges))

    def test_near_dedup_keeps_retained_primary_evidence_first(self) -> None:
        retained = _fused("f", "lexical", score=0.9, rank=1)
        secondary = _fused("a", "lexical", score=0.8, rank=2)

        result = _apply_policy(
            _query(),
            (retained, secondary),
            (
                _record("f", channels=("lexical",)),
                _record("a", channels=("lexical",)),
            ),
        )

        self.assertEqual(1, len(result.candidates))
        merged = result.candidates[0]
        self.assertEqual(_record_id("f"), merged.record_id)
        self.assertEqual(merged.evidence, merged.evidence_refs[0])
        self.assertEqual(
            (_record_id("f"), _record_id("a")),
            tuple(ref.record_id for ref in merged.evidence_refs),
        )

    def test_temporal_near_duplicate_retains_the_historically_superseded_version(
        self,
    ) -> None:
        from daem0nmcp.retrieval.types import EvidenceRef, FusedCandidate

        current_version = "fact_" + "2" * 64
        historical_version = "fact_" + "1" * 64
        current_event = _event_id("2")
        historical_event = _event_id("1")

        def temporal_candidate(
            version_id: str,
            event_id: str,
            *,
            rank: int,
            score: float,
            policy_notes: tuple[str, ...] = (),
        ) -> FusedCandidate:
            evidence = EvidenceRef(
                record_id=_record_id("8"),
                event_id=event_id,
                content_hash=DEFAULT_HASH,
                version_id=version_id,
                provider="temporal",
            )
            return FusedCandidate(
                evidence=evidence,
                evidence_refs=(evidence,),
                score=score,
                channels=frozenset({"temporal"}),
                channel_ranks=(("temporal", rank),),
                manifest_generations=(("temporal", 1),),
                policy_notes=policy_notes,
            )

        current = temporal_candidate(
            current_version,
            current_event,
            rank=1,
            score=1.0,
        )
        historical = temporal_candidate(
            historical_version,
            historical_event,
            rank=2,
            score=0.5,
            policy_notes=(
                "SUPERSEDED",
                f"SUPERSEDED_BY:{current_version}",
            ),
        )
        result = _apply_policy(
            _query(include_invalidated=True),
            (current, historical),
            (
                _record(
                    "8",
                    channels=("temporal",),
                    version_id=current_version,
                    source_event_ids=frozenset({current_event}),
                    transaction_from=SNAPSHOT_TIME - timedelta(seconds=1),
                ),
                _record(
                    "8",
                    channels=("temporal",),
                    version_id=historical_version,
                    source_event_ids=frozenset({historical_event}),
                    transaction_from=SNAPSHOT_TIME - timedelta(seconds=2),
                    transaction_to=SNAPSHOT_TIME - timedelta(seconds=1),
                    superseded_by_version_id=current_version,
                ),
            ),
        )

        self.assertFalse(result.abstained)
        self.assertEqual(1, len(result.candidates))
        retained = result.candidates[0]
        self.assertEqual(historical_version, retained.version_id)
        self.assertEqual(current.score, retained.score)
        self.assertEqual(
            f"SUPERSEDED_BY:{current_version}",
            next(
                note
                for note in retained.policy_notes
                if note.startswith("SUPERSEDED_BY:")
            ),
        )
        self.assertEqual(
            {current_version, historical_version},
            {evidence.version_id for evidence in retained.evidence_refs},
        )

    def test_empty_and_all_filtered_inputs_have_safe_abstention_reasons(self):
        empty = _apply_policy(_query(), (), ())
        filtered = _apply_policy(
            _query(),
            (_fused("9"),),
            (_record("9", visibility_allowed=False),),
        )

        self.assertEqual("NO_CANDIDATES", empty.reason)
        self.assertEqual("ALL_CANDIDATES_FILTERED", filtered.reason)
        self.assertEqual((("VISIBILITY_DENIED", 1),), filtered.rejection_counts)
        self.assertEqual((), filtered.candidates)


if __name__ == "__main__":
    unittest.main()
