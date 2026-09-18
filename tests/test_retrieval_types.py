"""Dependency-free tests for immutable v7 retrieval contracts."""

from __future__ import annotations

import math
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

WORKSPACE_ID = "ws_0123456789abcdef01234567"
RECORD_ID = "mem_" + "1" * 64
EVENT_ID = "evt_" + "2" * 64
VERSION_ID = "fact_" + "3" * 64
RELATION_ID = "rel_" + "4" * 64
CONTENT_HASH = "5" * 64


class RetrievalQueryContractTests(unittest.TestCase):
    def test_query_is_immutable_and_keeps_task_7_opaque_scope(self):
        from daem0nmcp.retrieval.types import RetrievalQuery

        query = RetrievalQuery(
            workspace_id=WORKSPACE_ID,
            text="why did the migration fail?",
            categories=frozenset({"warning", "decision"}),
            tags=frozenset({"migration"}),
            record_ids=frozenset({RECORD_ID}),
            as_of_valid_time=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )

        self.assertEqual(10, query.limit)
        self.assertEqual(50, query.candidate_limit)
        self.assertEqual(frozenset({RECORD_ID}), query.record_ids)
        with self.assertRaises(FrozenInstanceError):
            query.limit = 20  # type: ignore[misc]

    def test_query_rejects_non_opaque_scope_and_unsafe_bounds(self):
        from daem0nmcp.retrieval.types import RetrievalQuery

        cases = (
            {"workspace_id": "C:/source", "text": "x"},
            {"workspace_id": WORKSPACE_ID, "text": "x", "limit": 0},
            {
                "workspace_id": WORKSPACE_ID,
                "text": "x",
                "limit": 5,
                "candidate_limit": 4,
            },
            {
                "workspace_id": WORKSPACE_ID,
                "text": "x",
                "token_budget": 0,
            },
            {
                "workspace_id": WORKSPACE_ID,
                "text": "x",
                "record_ids": frozenset({"17"}),
            },
            {
                "workspace_id": WORKSPACE_ID,
                "text": "x",
                "as_of_transaction_time": datetime(2026, 1, 2),
            },
            {
                "workspace_id": WORKSPACE_ID,
                "text": "x",
                "limit": 10**400,
                "candidate_limit": 10**400,
            },
            {
                "workspace_id": WORKSPACE_ID,
                "text": "x",
                "token_budget": 10**400,
            },
            {"workspace_id": WORKSPACE_ID, "text": "x" * 16385},
            {
                "workspace_id": WORKSPACE_ID,
                "text": "x",
                "tags": frozenset(f"tag-{index}" for index in range(257)),
            },
            {
                "workspace_id": WORKSPACE_ID,
                "text": "x",
                "categories": frozenset({"x" * 257}),
            },
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                RetrievalQuery(**kwargs)


class EvidenceAndCandidateContractTests(unittest.TestCase):
    def _evidence(self, **changes):
        from daem0nmcp.retrieval.types import EvidenceRef

        values = {
            "record_id": RECORD_ID,
            "event_id": EVENT_ID,
            "content_hash": CONTENT_HASH,
            "version_id": VERSION_ID,
            "relation_path": (RELATION_ID,),
            "provider": "lexical",
        }
        values.update(changes)
        return EvidenceRef(**values)

    def test_evidence_accepts_only_task_7_ids_and_hashes(self):
        evidence = self._evidence()

        self.assertEqual(RECORD_ID, evidence.record_id)
        self.assertEqual((RELATION_ID,), evidence.relation_path)
        fact_path = self._evidence(relation_path=("fact_" + "d" * 64,))
        self.assertEqual("fact_" + "d" * 64, fact_path.relation_path[0])
        for changes in (
            {"record_id": "mem_7"},
            {"event_id": "event-2"},
            {"content_hash": "sha256:5"},
            {"version_id": "17"},
            {"relation_path": ("related_to",)},
            {"provider": "dense provider"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self._evidence(**changes)

    def test_candidate_is_frozen_and_rejects_nonfinite_diagnostics(self):
        from daem0nmcp.retrieval.types import Candidate

        candidate = Candidate(
            evidence=self._evidence(),
            rank=1,
            raw_score=-3.25,
            channels=frozenset({"lexical"}),
            highlights=("durable write",),
            transaction_time=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )

        self.assertEqual(-3.25, candidate.raw_score)
        with self.assertRaises(FrozenInstanceError):
            candidate.rank = 2  # type: ignore[misc]
        for changes in (
            {"rank": True},
            {"rank": 0},
            {"rank": 10**400},
            {"raw_score": math.nan},
            {"raw_score": 10**400},
            {"channels": frozenset()},
            {"channels": frozenset({"dense"})},
            {"transaction_time": datetime(2026, 1, 2)},
        ):
            values = {
                "evidence": self._evidence(),
                "rank": 1,
                "raw_score": None,
                "channels": frozenset({"lexical"}),
            }
            values.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                Candidate(**values)

    def test_fused_candidate_requires_unique_evidence_references(self):
        from daem0nmcp.retrieval.types import FusedCandidate

        evidence = self._evidence()
        with self.assertRaises(ValueError):
            FusedCandidate(
                evidence=evidence,
                evidence_refs=(evidence, evidence),
                score=1.0,
                channels=frozenset({"lexical"}),
                channel_ranks=(("lexical", 1),),
                manifest_generations=(("lexical", 1),),
            )

    def test_fused_candidate_requires_primary_evidence_first(self) -> None:
        from daem0nmcp.retrieval.types import FusedCandidate

        primary = self._evidence()
        secondary = self._evidence(
            record_id="mem_" + "a" * 64,
            event_id="evt_" + "b" * 64,
        )

        with self.assertRaises(ValueError):
            FusedCandidate(
                evidence=primary,
                evidence_refs=(secondary, primary),
                score=1.0,
                channels=frozenset({"lexical"}),
                channel_ranks=(("lexical", 1),),
                manifest_generations=(("lexical", 1),),
            )

    def test_fused_candidate_requires_every_named_origin_channel(self):
        from daem0nmcp.retrieval.types import FusedCandidate

        lexical = self._evidence(provider="lexical")
        dense = self._evidence(provider="dense")
        for primary, evidence_refs in (
            (dense, (dense,)),
            (lexical, (lexical, dense)),
        ):
            with self.subTest(primary=primary.provider), self.assertRaises(ValueError):
                FusedCandidate(
                    evidence=primary,
                    evidence_refs=evidence_refs,
                    score=1.0,
                    channels=frozenset({"lexical"}),
                    channel_ranks=(("lexical", 1),),
                    manifest_generations=(("lexical", 1),),
                )

        legacy_blank = self._evidence(provider="")
        fused = FusedCandidate(
            evidence=legacy_blank,
            evidence_refs=(legacy_blank,),
            score=1.0,
            channels=frozenset({"lexical"}),
            channel_ranks=(("lexical", 1),),
            manifest_generations=(("lexical", 1),),
        )
        self.assertEqual("", fused.evidence.provider)


class ProviderAndRetrievalResultContractTests(unittest.TestCase):
    def test_public_facade_exports_lexical_projection_baseline(self):
        from daem0nmcp import retrieval

        for name in (
            "LexicalProjectionBuilder",
            "ProjectionBuildError",
            "ProjectionBuildResult",
            "LexicalProvider",
        ):
            with self.subTest(name=name):
                self.assertTrue(hasattr(retrieval, name))

    def _candidate(self):
        from daem0nmcp.retrieval.types import Candidate, EvidenceRef

        return Candidate(
            evidence=EvidenceRef(
                record_id=RECORD_ID,
                event_id=EVENT_ID,
                content_hash=CONTENT_HASH,
                version_id=None,
                provider="lexical",
            ),
            rank=1,
            raw_score=0.9,
            channels=frozenset({"lexical"}),
        )

    def test_provider_result_exposes_sanitized_candidate_free_diagnostic(self):
        from daem0nmcp.retrieval.types import ProviderDiagnostic, ProviderResult

        result = ProviderResult(
            provider="lexical",
            candidates=(self._candidate(),),
            status="ready",
            manifest_generation=7,
            elapsed_ms=2.5,
        )
        diagnostic = ProviderDiagnostic.from_result(result)

        self.assertEqual("lexical", diagnostic.provider)
        self.assertEqual(1, diagnostic.returned_count)
        self.assertFalse(hasattr(diagnostic, "candidates"))

    def test_provider_protocol_is_dependency_free_and_structural(self):
        from daem0nmcp.retrieval.types import (
            ProviderResult,
            RetrievalProvider,
            RetrievalQuery,
        )

        class Provider:
            name = "lexical"

            async def search(self, query: RetrievalQuery, limit: int) -> ProviderResult:
                return ProviderResult(provider=self.name)

        self.assertIsInstance(Provider(), RetrievalProvider)

    def test_provider_failure_requires_a_safe_code_and_no_candidates(self):
        from daem0nmcp.retrieval.types import ProviderResult

        with self.assertRaises(ValueError):
            ProviderResult(
                provider="lexical",
                candidates=(self._candidate(),),
                status="unavailable",
                reason="database exception: C:/private/project",
            )
        with self.assertRaises(ValueError):
            ProviderResult(
                provider="lexical",
                status="failed",
                reason=None,
            )
        unavailable = ProviderResult(
            provider="lexical",
            status="unavailable",
            reason="LEXICAL_UNAVAILABLE",
        )
        self.assertEqual((), unavailable.candidates)

    def test_provider_result_rejects_spoofed_evidence_origin(self):
        from daem0nmcp.retrieval.types import Candidate, EvidenceRef, ProviderResult

        spoofed = Candidate(
            evidence=EvidenceRef(
                record_id=RECORD_ID,
                event_id=EVENT_ID,
                content_hash=CONTENT_HASH,
                version_id=None,
                provider="dense",
            ),
            rank=1,
            raw_score=None,
            channels=frozenset({"dense", "lexical"}),
        )
        with self.assertRaises(ValueError):
            ProviderResult(provider="lexical", candidates=(spoofed,))

        legacy_blank = Candidate(
            evidence=EvidenceRef(
                record_id=RECORD_ID,
                event_id=EVENT_ID,
                content_hash=CONTENT_HASH,
                version_id=None,
                provider="",
            ),
            rank=1,
            raw_score=None,
            channels=frozenset({"lexical"}),
        )
        self.assertEqual(
            "",
            ProviderResult(provider="lexical", candidates=(legacy_blank,))
            .candidates[0]
            .evidence.provider,
        )

    def test_provider_diagnostic_cannot_hide_an_unsanitized_failure(self):
        from daem0nmcp.retrieval.types import ProviderDiagnostic

        with self.assertRaises(ValueError):
            ProviderDiagnostic(
                provider="dense",
                status="failed",
                manifest_generation=None,
                elapsed_ms=1.0,
                reason=None,
                returned_count=0,
            )
        with self.assertRaises(ValueError):
            ProviderDiagnostic(
                provider="dense",
                status="unavailable",
                manifest_generation=None,
                elapsed_ms=1.0,
                reason="DENSE_UNAVAILABLE",
                returned_count=1,
            )

    def test_retrieval_abstention_is_empty_and_uses_a_safe_reason_code(self):
        from daem0nmcp.retrieval.types import RetrievalResult

        result = RetrievalResult(
            abstained=True,
            reason="ALL_CANDIDATES_FILTERED",
            policy_rejection_counts=(("VISIBILITY_DENIED", 2),),
        )
        self.assertEqual((), result.items)
        self.assertIsNone(result.context)
        self.assertEqual((("VISIBILITY_DENIED", 2),), result.policy_rejection_counts)
        with self.assertRaises(ValueError):
            RetrievalResult(
                abstained=True,
                reason="ALL_CANDIDATES_FILTERED",
            )
        with self.assertRaises(ValueError):
            RetrievalResult(
                abstained=True,
                reason="filtered by /private/workspace",
            )

    def test_non_abstaining_result_has_typed_citation_complete_context(self):
        from daem0nmcp.retrieval.types import (
            CitationEntry,
            ContextPackage,
            EvidenceItem,
            RetrievalResult,
        )

        evidence = self._candidate().evidence
        item = EvidenceItem(
            citation="[E1]",
            excerpt="SQLite WAL transactions are durable.",
            category="decision",
            status="current",
            score=0.25,
            channels=frozenset({"lexical"}),
            token_count=7,
            evidence_refs=(evidence,),
        )
        citation = CitationEntry(
            marker="[E1]",
            evidence_refs=(evidence,),
            channels=frozenset({"lexical"}),
            excerpt_start=5,
            excerpt_end=41,
        )
        context = ContextPackage(
            text="[E1] SQLite WAL transactions are durable.",
            citations=(citation,),
            token_budget=100,
            requested_tokens=7,
            selected_tokens=7,
            rendered_tokens=8,
            dropped_tokens=0,
        )
        result = RetrievalResult(items=(item,), context=context)

        self.assertEqual("[E1]", result.items[0].citation)
        with self.assertRaises(FrozenInstanceError):
            context.rendered_tokens = 9  # type: ignore[misc]
        with self.assertRaises(ValueError):
            RetrievalResult(
                items=(item,),
                context=ContextPackage(
                    text="[E2] unrelated",
                    citations=(
                        CitationEntry(
                            marker="[E2]",
                            evidence_refs=(evidence,),
                            channels=frozenset({"lexical"}),
                            excerpt_start=5,
                            excerpt_end=14,
                        ),
                    ),
                    token_budget=100,
                    requested_tokens=2,
                    selected_tokens=2,
                    rendered_tokens=2,
                    dropped_tokens=0,
                ),
            )
        with self.assertRaises(ValueError):
            ContextPackage(
                text="[E1] SQLite WAL transactions are durable.",
                citations=(citation,),
                token_budget=1,
                requested_tokens=2,
                selected_tokens=2,
                rendered_tokens=2,
                dropped_tokens=0,
            )

    def test_evidence_item_legacy_metadata_is_output_bounded(self):
        from daem0nmcp.retrieval.types import EvidenceItem

        values = {
            "citation": "[E1]",
            "excerpt": "selected",
            "category": "decision",
            "status": "current",
            "score": 1.0,
            "channels": frozenset({"lexical"}),
            "token_count": 1,
            "evidence_refs": (self._candidate().evidence,),
        }
        invalid = (
            {"rationale": "r" * 4097},
            {"tags": tuple(f"tag-{index}" for index in range(33))},
            {"tags": ("t" * 129,)},
        )
        for changes in invalid:
            with self.subTest(changes=tuple(changes)), self.assertRaises(ValueError):
                EvidenceItem(**values, **changes)

    def test_citation_manifest_cannot_change_selected_provenance(self):
        from daem0nmcp.retrieval.types import (
            CitationEntry,
            ContextPackage,
            EvidenceItem,
            EvidenceRef,
            RetrievalResult,
        )

        selected = self._candidate().evidence
        changed = EvidenceRef(
            record_id=selected.record_id,
            event_id="evt_" + "9" * 64,
            content_hash=selected.content_hash,
            version_id=selected.version_id,
            provider=selected.provider,
        )
        item = EvidenceItem(
            citation="[E1]",
            excerpt="selected",
            category="decision",
            status="current",
            score=1.0,
            channels=frozenset({"lexical"}),
            token_count=1,
            evidence_refs=(selected,),
        )
        context = ContextPackage(
            text="[E1] selected",
            citations=(
                CitationEntry(
                    marker="[E1]",
                    evidence_refs=(changed,),
                    channels=frozenset({"lexical"}),
                    excerpt_start=5,
                    excerpt_end=13,
                ),
            ),
            token_budget=10,
            requested_tokens=1,
            selected_tokens=1,
            rendered_tokens=2,
            dropped_tokens=0,
        )
        with self.assertRaises(ValueError):
            RetrievalResult(items=(item,), context=context)

    def test_context_rejects_unmapped_citation_shaped_markers(self):
        from daem0nmcp.retrieval.types import CitationEntry, ContextPackage

        evidence = self._candidate().evidence
        with self.assertRaises(ValueError):
            ContextPackage(
                text="[E1] selected [E999] unmapped",
                citations=(
                    CitationEntry(
                        marker="[E1]",
                        evidence_refs=(evidence,),
                        channels=frozenset({"lexical"}),
                        excerpt_start=5,
                        excerpt_end=13,
                    ),
                ),
                token_budget=20,
                requested_tokens=4,
                selected_tokens=4,
                rendered_tokens=6,
                dropped_tokens=0,
            )

    def test_result_rejects_duplicate_evidence_item_citations(self):
        from daem0nmcp.retrieval.types import (
            CitationEntry,
            ContextPackage,
            EvidenceItem,
            RetrievalResult,
        )

        evidence = self._candidate().evidence
        item = EvidenceItem(
            citation="[E1]",
            excerpt="selected",
            category="decision",
            status="current",
            score=1.0,
            channels=frozenset({"lexical"}),
            token_count=1,
            evidence_refs=(evidence,),
        )
        context = ContextPackage(
            text="[E1] selected",
            citations=(
                CitationEntry(
                    marker="[E1]",
                    evidence_refs=(evidence,),
                    channels=frozenset({"lexical"}),
                    excerpt_start=5,
                    excerpt_end=13,
                ),
            ),
            token_budget=10,
            requested_tokens=2,
            selected_tokens=2,
            rendered_tokens=2,
            dropped_tokens=0,
        )
        with self.assertRaises(ValueError):
            RetrievalResult(items=(item, item), context=context)

    def test_context_rejects_excerpt_offsets_outside_rendered_text(self):
        from daem0nmcp.retrieval.types import CitationEntry, ContextPackage

        evidence = self._candidate().evidence
        text = "[E1] selected"
        citation = CitationEntry(
            marker="[E1]",
            evidence_refs=(evidence,),
            channels=frozenset({"lexical"}),
            excerpt_start=5,
            excerpt_end=len(text) + 1,
        )

        with self.assertRaises(ValueError):
            ContextPackage(
                text=text,
                citations=(citation,),
                token_budget=10,
                requested_tokens=2,
                selected_tokens=2,
                rendered_tokens=2,
                dropped_tokens=0,
            )

    def test_context_requires_each_marker_to_precede_its_excerpt(self):
        from daem0nmcp.retrieval.types import CitationEntry, ContextPackage

        evidence = self._candidate().evidence
        with self.assertRaises(ValueError):
            ContextPackage(
                text="selected [E1]",
                citations=(
                    CitationEntry(
                        marker="[E1]",
                        evidence_refs=(evidence,),
                        channels=frozenset({"lexical"}),
                        excerpt_start=0,
                        excerpt_end=8,
                    ),
                ),
                token_budget=10,
                requested_tokens=2,
                selected_tokens=2,
                rendered_tokens=2,
                dropped_tokens=0,
            )

    def test_context_rejects_duplicate_rendered_marker_occurrences(self):
        from daem0nmcp.retrieval.types import CitationEntry, ContextPackage

        evidence = self._candidate().evidence
        text = "[E1] duplicate [E1]\nselected"
        with self.assertRaises(ValueError):
            ContextPackage(
                text=text,
                citations=(
                    CitationEntry(
                        marker="[E1]",
                        evidence_refs=(evidence,),
                        channels=frozenset({"lexical"}),
                        excerpt_start=text.index("selected"),
                        excerpt_end=len(text),
                    ),
                ),
                token_budget=10,
                requested_tokens=3,
                selected_tokens=3,
                rendered_tokens=3,
                dropped_tokens=0,
            )

    def test_types_expose_the_query_token_budget_ceiling(self):
        from daem0nmcp.retrieval import types

        self.assertTrue(hasattr(types, "MAX_TOKEN_BUDGET"))

    def test_context_uses_the_query_token_budget_ceiling(self):
        from daem0nmcp.retrieval.types import ContextPackage

        with self.assertRaises(ValueError):
            ContextPackage(
                text="",
                citations=(),
                token_budget=131_073,
                requested_tokens=0,
                selected_tokens=0,
                rendered_tokens=0,
                dropped_tokens=0,
            )

    def test_superseded_item_requires_the_invalidating_opaque_version(self):
        from daem0nmcp.retrieval.types import EvidenceItem

        with self.assertRaises(ValueError):
            EvidenceItem(
                citation="[E1]",
                excerpt="old fact",
                category="warning",
                status="superseded",
                score=1.0,
                channels=frozenset({"lexical"}),
                token_count=2,
                evidence_refs=(self._candidate().evidence,),
            )


if __name__ == "__main__":
    unittest.main()
