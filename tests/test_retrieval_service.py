"""End-to-end orchestration tests for the v7 retrieval facade."""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

WORKSPACE_ID = "ws_0123456789abcdef01234567"
SNAPSHOT = datetime(2026, 8, 8, 18, 0, tzinfo=timezone.utc)


def _record_id(digit: str) -> str:
    return "mem_" + digit * 64


def _event_id(digit: str) -> str:
    return "evt_" + digit * 64


def _candidate(
    digit: str,
    provider: str,
    rank: int,
    *,
    raw_score: float | None = None,
):
    from daem0nmcp.retrieval.types import Candidate, EvidenceRef

    return Candidate(
        evidence=EvidenceRef(
            record_id=_record_id(digit),
            event_id=_event_id(digit),
            content_hash=digit * 64,
            version_id=None,
            provider=provider,
        ),
        rank=rank,
        raw_score=raw_score,
        channels=frozenset({provider}),
        highlights=(f"{provider} match {digit}",),
        transaction_time=SNAPSHOT - timedelta(minutes=rank),
    )


def _provider_result(
    provider: str,
    *candidates,
    status: str = "ready",
    reason: str | None = None,
    generation: int = 1,
):
    from daem0nmcp.retrieval.types import ProviderResult

    return ProviderResult(
        provider=provider,
        candidates=tuple(candidates),
        status=status,
        manifest_generation=generation,
        reason=reason,
    )


class StaticProvider:
    def __init__(self, name, result, calls):
        self.name = name
        self._result = result
        self._calls = calls

    async def search(self, query, limit):
        self._calls.append((self.name, limit))
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


class HangingProvider:
    def __init__(self, name, calls):
        self.name = name
        self._calls = calls

    async def search(self, query, limit):
        self._calls.append((self.name, limit))
        await asyncio.Event().wait()


class SeededGraphProvider:
    name = "graph"

    def __init__(self, result):
        self.result = result
        self.query = None
        self.seeds = ()

    async def search(self, query, limit, *, seeds):
        self.query = query
        self.seeds = tuple(seeds)
        return self.result


class FixedClock:
    def __init__(self, value=SNAPSHOT):
        self.value = value
        self.calls = 0

    def now(self):
        self.calls += 1
        return self.value


class WordTokenizer:
    def count_tokens(self, text: str) -> int:
        return len(text.split())


class AsyncComposer:
    """Mirrors the production async boundary and rejects sync service calls."""

    def __init__(self):
        from daem0nmcp.retrieval.composer import EvidenceComposer

        self.delegate = EvidenceComposer(
            tokenizer=WordTokenizer(), max_excerpt_chars=240
        )
        self.async_calls = 0

    def compose(self, selected, *, token_budget):
        raise AssertionError("RetrievalService must not compose on the event loop")

    async def compose_async(self, selected, *, token_budget):
        self.async_calls += 1
        return self.delegate.compose(selected, token_budget=token_budget)


class UnavailableComposer:
    async def compose_async(self, selected, *, token_budget):
        from daem0nmcp.retrieval.composer import CompositionResult
        from daem0nmcp.retrieval.types import ContextPackage

        return CompositionResult(
            items=(),
            context=ContextPackage(
                text="",
                citations=(),
                token_budget=token_budget,
                requested_tokens=0,
                selected_tokens=0,
                rendered_tokens=0,
                dropped_tokens=0,
                drop_reasons=("COMPOSER_UNAVAILABLE",),
            ),
        )


class ForbiddenComposer:
    async def compose_async(self, selected, *, token_budget):
        raise AssertionError("candidate retrieval must not compose context")


class ProvenanceTamperingComposer:
    def __init__(self):
        self.delegate = AsyncComposer()

    async def compose_async(self, selected, *, token_budget):
        from daem0nmcp.retrieval.composer import CompositionResult

        result = await self.delegate.compose_async(selected, token_budget=token_budget)
        return CompositionResult(
            items=(replace(result.items[0], score=999.0),),
            context=result.context,
        )


class OutcomeFlagTamperingComposer:
    def __init__(self):
        self.delegate = AsyncComposer()

    async def compose_async(self, selected, *, token_budget):
        from daem0nmcp.retrieval.composer import CompositionResult

        result = await self.delegate.compose_async(selected, token_budget=token_budget)
        return CompositionResult(
            items=(replace(result.items[0], outcome_failed=True),),
            context=result.context,
        )


class StructuredFieldTamperingComposer:
    def __init__(self):
        self.delegate = AsyncComposer()

    async def compose_async(self, selected, *, token_budget):
        from daem0nmcp.retrieval.composer import CompositionResult

        result = await self.delegate.compose_async(selected, token_budget=token_budget)
        return CompositionResult(
            items=(
                replace(
                    result.items[0],
                    outcome="FABRICATED",
                    procedure_steps=("FABRICATED STEP",),
                ),
            ),
            context=result.context,
        )


class LegacyMetadataTamperingComposer:
    def __init__(self):
        self.delegate = AsyncComposer()

    async def compose_async(self, selected, *, token_budget):
        from daem0nmcp.retrieval.composer import CompositionResult

        result = await self.delegate.compose_async(selected, token_budget=token_budget)
        return CompositionResult(
            items=(
                replace(
                    result.items[0],
                    rationale="FABRICATED RATIONALE",
                    tags=("fabricated",),
                    worked=True,
                ),
            ),
            context=result.context,
        )


class BudgetTamperingComposer:
    def __init__(self):
        self.delegate = AsyncComposer()

    async def compose_async(self, selected, *, token_budget):
        return await self.delegate.compose_async(selected, token_budget=1000)


class ExcerptTamperingComposer:
    def __init__(self):
        self.delegate = AsyncComposer()

    async def compose_async(self, selected, *, token_budget):
        from daem0nmcp.retrieval.composer import CompositionResult

        result = await self.delegate.compose_async(selected, token_budget=token_budget)
        return CompositionResult(
            items=(
                replace(
                    result.items[0],
                    excerpt="FABRICATED EVIDENCE",
                    token_count=999,
                ),
            ),
            context=result.context,
        )


class OrderTamperingComposer:
    def __init__(self):
        self.delegate = AsyncComposer()

    async def compose_async(self, selected, *, token_budget):
        from daem0nmcp.retrieval.composer import CompositionResult

        result = await self.delegate.compose_async(selected, token_budget=token_budget)
        return CompositionResult(
            items=tuple(reversed(result.items)),
            context=replace(
                result.context,
                citations=tuple(reversed(result.context.citations)),
            ),
        )


class CanonicalRepository:
    """Specific in-memory authority fake; provider text never enters it."""

    def __init__(
        self,
        *,
        changes=None,
        contents=None,
        selected_changes=None,
        omit_content=(),
    ):
        self.closed = False
        self.changes = changes or {}
        self.contents = contents or {}
        self.selected_changes = selected_changes or {}
        self.omit_content = frozenset(omit_content)
        self.policy_snapshots = []
        self.content_snapshots = []
        self.policy_candidates = ()
        self.content_candidates = ()

    def close(self):
        self.closed = True

    async def load_policy_records(self, query, candidates, *, snapshot_time):
        from daem0nmcp.retrieval.policy import PolicyRecord

        self.policy_snapshots.append(snapshot_time)
        self.policy_candidates = tuple(candidates)
        records = []
        for candidate in candidates:
            overrides = self.changes.get(candidate.record_id, {})
            channels = tuple(sorted(candidate.channels))
            values = {
                "workspace_id": WORKSPACE_ID,
                "record_id": candidate.record_id,
                "version_id": candidate.version_id,
                "content_hash": candidate.content_hash,
                "source_event_ids": frozenset(
                    evidence.event_id for evidence in candidate.evidence_refs
                ),
                "visibility": "workspace",
                "visibility_allowed": True,
                "archived": False,
                "category": "decision",
                "tags": frozenset({"retrieval"}),
                "valid_from": None,
                "valid_to": None,
                "transaction_from": None,
                "transaction_to": None,
                "superseded_by_version_id": None,
                "has_unresolved_contradiction": False,
                "projection_content_hashes": tuple(
                    (channel, candidate.content_hash) for channel in channels
                ),
                "active_manifest_generations": tuple(
                    (channel, dict(candidate.manifest_generations)[channel])
                    for channel in channels
                ),
            }
            values.update(overrides)
            records.append(PolicyRecord(**values))
        return tuple(records)

    async def load_selected_evidence(self, query, candidates, *, snapshot_time):
        from daem0nmcp.retrieval.composer import SelectedEvidence

        self.content_snapshots.append(snapshot_time)
        self.content_candidates = tuple(candidates)
        selected = []
        for candidate in candidates:
            if candidate.record_id in self.omit_content:
                continue
            overrides = self.changes.get(candidate.record_id, {})
            values = {
                "candidate": candidate,
                "content": self.contents.get(
                    candidate.record_id,
                    f"canonical content {candidate.record_id[-1]}",
                ),
                "category": overrides.get("category", "decision"),
            }
            values.update(self.selected_changes.get(candidate.record_id, {}))
            selected.append(SelectedEvidence(**values))
        return tuple(selected)


class ReversingReranker:
    def __init__(self, result=None):
        self.result = result
        self.received = ()

    async def rerank(self, query, evidence):
        self.received = tuple(evidence)
        if isinstance(self.result, BaseException):
            raise self.result
        if self.result is not None:
            return self.result
        return tuple(source.candidate for source in reversed(evidence))


class HangingReranker:
    async def rerank(self, query, evidence):
        await asyncio.Event().wait()


class FailingPlanner:
    def plan(self, query, *, ready_providers):
        raise RuntimeError("planner secret")


class OversizedPlanner:
    def plan(self, query, *, ready_providers):
        from daem0nmcp.retrieval.planner import ProviderRequest, RetrievalPlan

        return RetrievalPlan((ProviderRequest("lexical", query.candidate_limit + 1),))


def _composer():
    return AsyncComposer()


def _query(**changes):
    from daem0nmcp.retrieval.types import RetrievalQuery

    values = {
        "workspace_id": WORKSPACE_ID,
        "text": "decision result",
        "limit": 10,
        "candidate_limit": 10,
        "token_budget": 400,
    }
    values.update(changes)
    return RetrievalQuery(**values)


def _service(*, providers, repository, **changes):
    from daem0nmcp.retrieval.service import RetrievalService

    values = {
        "providers": providers,
        "repository": repository,
        "composer": _composer(),
        "clock": FixedClock(),
    }
    values.update(changes)
    return RetrievalService(**values)


class RetrievalServiceProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_graph_receives_only_lexical_dense_seeds_at_service_snapshot(self):
        calls = []
        graph = SeededGraphProvider(
            _provider_result("graph", _candidate("3", "graph", 1))
        )
        query = _query(text="why are these records related")

        result = await _service(
            providers={
                "lexical": StaticProvider(
                    "lexical",
                    _provider_result("lexical", _candidate("1", "lexical", 1)),
                    calls,
                ),
                "dense": StaticProvider(
                    "dense",
                    _provider_result("dense", _candidate("2", "dense", 1)),
                    calls,
                ),
                "graph": graph,
            },
            repository=CanonicalRepository(),
        ).retrieve(query)

        self.assertFalse(result.abstained)
        self.assertEqual(
            {_record_id("1"), _record_id("2")},
            {seed.record_id for seed in graph.seeds},
        )
        self.assertTrue(
            all(seed.channels <= {"lexical", "dense"} for seed in graph.seeds)
        )
        self.assertEqual(SNAPSHOT, graph.query.as_of_valid_time)
        self.assertEqual(SNAPSHOT, graph.query.as_of_transaction_time)
        self.assertIsNone(query.as_of_valid_time)
        self.assertIsNone(query.as_of_transaction_time)

    async def test_unexpected_planner_failure_is_a_safe_abstention(self):
        calls = []
        result = await _service(
            providers={
                "lexical": StaticProvider("lexical", _provider_result("lexical"), calls)
            },
            repository=CanonicalRepository(),
            planner=FailingPlanner(),
        ).retrieve(_query())

        self.assertTrue(result.abstained)
        self.assertEqual("PLANNING_FAILED", result.reason)
        self.assertEqual([], calls)
        self.assertNotIn("planner secret", repr(result))

    async def test_planner_cannot_expand_the_validated_candidate_bound(self):
        calls = []
        result = await _service(
            providers={
                "lexical": StaticProvider("lexical", _provider_result("lexical"), calls)
            },
            repository=CanonicalRepository(),
            planner=OversizedPlanner(),
        ).retrieve(_query(candidate_limit=3, limit=2))

        self.assertTrue(result.abstained)
        self.assertEqual("PLANNING_FAILED", result.reason)
        self.assertEqual([], calls)

    async def test_lexical_runs_first_and_rank_only_fusion_is_deterministic(self):
        calls = []
        lexical = StaticProvider(
            "lexical",
            _provider_result(
                "lexical", _candidate("1", "lexical", 1, raw_score=-1e200)
            ),
            calls,
        )
        dense = StaticProvider(
            "dense",
            _provider_result("dense", _candidate("2", "dense", 1, raw_score=1e200)),
            calls,
        )
        outcome = StaticProvider("outcome", _provider_result("outcome"), calls)
        repository = CanonicalRepository()
        service = _service(
            providers={"outcome": outcome, "dense": dense, "lexical": lexical},
            repository=repository,
            weights={"lexical": 2.0, "dense": 1.0, "outcome": 0.5},
        )

        result = await service.retrieve(_query(limit=2))

        self.assertFalse(result.abstained)
        self.assertEqual(["lexical", "dense", "outcome"], [name for name, _ in calls])
        self.assertEqual(
            ("lexical", "dense", "outcome"),
            tuple(item.provider for item in result.providers),
        )
        self.assertEqual(
            (("dense", 1.0), ("lexical", 2.0), ("outcome", 0.5)),
            result.weights,
        )
        self.assertEqual(
            (_record_id("1"), _record_id("2")),
            tuple(item.evidence_refs[0].record_id for item in result.items),
        )
        self.assertEqual(
            {item.citation for item in result.items},
            {citation.marker for citation in result.context.citations},
        )
        self.assertIs(repository.policy_snapshots[0], repository.content_snapshots[0])

    async def test_lexical_unavailable_or_failed_abstains_before_optionals(self):
        cases = (
            (
                _provider_result(
                    "lexical",
                    status="unavailable",
                    reason="LEXICAL_UNAVAILABLE",
                ),
                "LEXICAL_UNAVAILABLE",
            ),
            (
                _provider_result(
                    "lexical",
                    status="failed",
                    reason="LEXICAL_QUERY_FAILED",
                ),
                "LEXICAL_QUERY_FAILED",
            ),
            (RuntimeError("database path and password"), "LEXICAL_PROVIDER_FAILED"),
            (_provider_result("dense"), "LEXICAL_PROVIDER_FAILED"),
        )
        for lexical_result, expected_reason in cases:
            with self.subTest(reason=expected_reason):
                calls = []
                lexical = StaticProvider("lexical", lexical_result, calls)
                dense = StaticProvider(
                    "dense",
                    _provider_result("dense", _candidate("2", "dense", 1)),
                    calls,
                )
                result = await _service(
                    providers={"lexical": lexical, "dense": dense},
                    repository=CanonicalRepository(),
                ).retrieve(_query())

                self.assertTrue(result.abstained)
                self.assertEqual(expected_reason, result.reason)
                self.assertEqual(["lexical"], [name for name, _ in calls])
                self.assertEqual((), result.items)
                self.assertIsNone(result.context)
                self.assertNotIn("password", repr(result))

    async def test_optional_exception_is_sanitized_and_lexical_survives(self):
        calls = []
        lexical = StaticProvider(
            "lexical",
            _provider_result("lexical", _candidate("1", "lexical", 1)),
            calls,
        )
        dense = StaticProvider(
            "dense", RuntimeError("https://secret.invalid?api_key=hunter2"), calls
        )

        result = await _service(
            providers={"lexical": lexical, "dense": dense},
            repository=CanonicalRepository(),
        ).retrieve(_query())

        self.assertFalse(result.abstained)
        self.assertEqual(
            ("lexical", "dense"), tuple(d.provider for d in result.providers)
        )
        self.assertEqual("failed", result.providers[1].status)
        self.assertEqual("DENSE_PROVIDER_FAILED", result.providers[1].reason)
        self.assertNotIn("hunter2", repr(result))
        self.assertIn("canonical content", result.context.text)

    async def test_hung_optional_provider_times_out_without_losing_lexical(self):
        calls = []
        service = _service(
            providers={
                "lexical": StaticProvider(
                    "lexical",
                    _provider_result("lexical", _candidate("1", "lexical", 1)),
                    calls,
                ),
                "dense": HangingProvider("dense", calls),
            },
            repository=CanonicalRepository(),
            provider_timeout_seconds=0.01,
        )

        result = await asyncio.wait_for(service.retrieve(_query()), timeout=0.5)

        self.assertFalse(result.abstained)
        self.assertEqual(["lexical", "dense"], [name for name, _ in calls])
        self.assertEqual("failed", result.providers[1].status)
        self.assertEqual("DENSE_PROVIDER_TIMEOUT", result.providers[1].reason)
        self.assertEqual(_record_id("1"), result.items[0].evidence_refs[0].record_id)

    async def test_dense_only_match_is_allowed_only_after_ready_lexical_runs(self):
        calls = []
        result = await _service(
            providers={
                "lexical": StaticProvider(
                    "lexical", _provider_result("lexical"), calls
                ),
                "dense": StaticProvider(
                    "dense",
                    _provider_result("dense", _candidate("2", "dense", 1)),
                    calls,
                ),
            },
            repository=CanonicalRepository(),
        ).retrieve(_query(text="semantic synonym miss"))

        self.assertFalse(result.abstained)
        self.assertEqual(["lexical", "dense"], [name for name, _ in calls])
        self.assertEqual(_record_id("2"), result.items[0].evidence_refs[0].record_id)


class RetrievalServicePolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_aware_snapshot_drives_repository_and_ordered_policy(self):
        clock = FixedClock()
        calls = []
        candidate = _candidate("1", "lexical", 1)
        repository = CanonicalRepository(
            changes={
                candidate.evidence.record_id: {
                    "valid_from": SNAPSHOT + timedelta(microseconds=1)
                }
            }
        )
        service = _service(
            providers={
                "lexical": StaticProvider(
                    "lexical", _provider_result("lexical", candidate), calls
                )
            },
            repository=repository,
            clock=clock,
        )

        result = await service.retrieve(_query(text="snapshot"))

        self.assertTrue(result.abstained)
        self.assertEqual("ALL_CANDIDATES_FILTERED", result.reason)
        self.assertEqual((("NOT_YET_VALID", 1),), result.policy_rejection_counts)
        self.assertEqual(1, clock.calls)
        self.assertEqual([SNAPSHOT], repository.policy_snapshots)
        self.assertIs(SNAPSHOT, repository.policy_snapshots[0])
        self.assertEqual([], repository.content_snapshots)

    async def test_naive_snapshot_fails_before_any_provider_executes(self):
        calls = []
        service = _service(
            providers={
                "lexical": StaticProvider("lexical", _provider_result("lexical"), calls)
            },
            repository=CanonicalRepository(),
            clock=FixedClock(datetime(2026, 8, 8)),
        )

        with self.assertRaises(ValueError):
            await service.retrieve(_query())
        self.assertEqual([], calls)

    async def test_policy_precedes_limit_and_category_diversity(self):
        calls = []
        candidates = tuple(
            _candidate(str(digit), "lexical", digit) for digit in range(1, 5)
        )
        repository = CanonicalRepository(
            changes={
                _record_id("1"): {"visibility_allowed": False},
                _record_id("2"): {"category": "architecture"},
                _record_id("3"): {"category": "architecture"},
                _record_id("4"): {"category": "warning"},
            },
            contents={
                _record_id("1"): "denied secret",
                _record_id("2"): "first architecture result",
                _record_id("3"): "unselected architecture result",
                _record_id("4"): "independent warning result",
            },
        )
        service = _service(
            providers={
                "lexical": StaticProvider(
                    "lexical", _provider_result("lexical", *candidates), calls
                )
            },
            repository=repository,
        )

        result = await service.retrieve(_query(limit=2))

        self.assertFalse(result.abstained)
        self.assertEqual((("VISIBILITY_DENIED", 1),), result.policy_rejection_counts)
        self.assertEqual(
            (_record_id("2"), _record_id("4")),
            tuple(item.evidence_refs[0].record_id for item in result.items),
        )
        self.assertEqual(
            {_record_id("2"), _record_id("4")},
            {candidate.record_id for candidate in repository.content_candidates},
        )
        self.assertNotIn("denied secret", result.context.text)
        self.assertNotIn("unselected architecture result", result.context.text)

    async def test_all_filtered_returns_exact_counts_without_loading_content(self):
        candidates = (
            _candidate("1", "lexical", 1),
            _candidate("2", "lexical", 2),
        )
        repository = CanonicalRepository(
            changes={
                _record_id("1"): {"visibility_allowed": False},
                _record_id("2"): {"archived": True},
            }
        )
        result = await _service(
            providers={
                "lexical": StaticProvider(
                    "lexical", _provider_result("lexical", *candidates), []
                )
            },
            repository=repository,
        ).retrieve(_query())

        self.assertTrue(result.abstained)
        self.assertEqual("ALL_CANDIDATES_FILTERED", result.reason)
        self.assertEqual(
            (("VISIBILITY_DENIED", 1), ("ARCHIVED_EXCLUDED", 1)),
            result.policy_rejection_counts,
        )
        self.assertEqual([], repository.content_snapshots)


class RetrievalServiceRerankAndCompositionTests(unittest.IsolatedAsyncioTestCase):
    async def test_candidate_retrieval_stops_before_context_composition(self):
        candidate = _candidate("1", "lexical", 1)
        result = await _service(
            providers={
                "lexical": StaticProvider(
                    "lexical", _provider_result("lexical", candidate), []
                )
            },
            repository=CanonicalRepository(),
            composer=ForbiddenComposer(),
        ).retrieve_candidates(_query(limit=1))

        self.assertFalse(result.abstained)
        self.assertEqual(1, len(result.selected))
        self.assertEqual(_record_id("1"), result.selected[0].candidate.record_id)

    async def test_real_composer_async_boundary_produces_cited_evidence(self):
        from daem0nmcp.retrieval.composer import EvidenceComposer

        candidate = _candidate("1", "lexical", 1)
        result = await _service(
            providers={
                "lexical": StaticProvider(
                    "lexical", _provider_result("lexical", candidate), []
                )
            },
            repository=CanonicalRepository(),
            composer=EvidenceComposer(tokenizer=WordTokenizer(), max_excerpt_chars=240),
        ).retrieve(_query())

        self.assertFalse(result.abstained)
        self.assertEqual(1, len(result.items))
        self.assertEqual("[E1]", result.items[0].citation)
        self.assertEqual("[E1]", result.context.citations[0].marker)

    async def test_reranker_reorders_only_policy_accepted_candidates(self):
        candidates = tuple(
            _candidate(str(digit), "lexical", digit) for digit in range(1, 4)
        )
        reranker = ReversingReranker()
        repository = CanonicalRepository(
            contents={
                _record_id("1"): "one accepted",
                _record_id("2"): "two accepted",
                _record_id("3"): "three accepted",
            }
        )
        result = await _service(
            providers={
                "lexical": StaticProvider(
                    "lexical", _provider_result("lexical", *candidates), []
                )
            },
            repository=repository,
            reranker=reranker,
            rerank_enabled=True,
            rerank_candidate_limit=3,
        ).retrieve(_query(limit=2, rerank=True))

        self.assertFalse(result.abstained)
        self.assertEqual(3, len(reranker.received))
        self.assertEqual(
            (_record_id("3"), _record_id("2")),
            tuple(item.evidence_refs[0].record_id for item in result.items),
        )
        self.assertEqual("reranker", result.providers[-1].provider)
        self.assertEqual("ready", result.providers[-1].status)
        for item in result.items:
            citation = next(
                entry
                for entry in result.context.citations
                if entry.marker == item.citation
            )
            self.assertEqual(item.evidence_refs, citation.evidence_refs)

    async def test_invalid_or_failed_reranker_restores_prefailure_order(self):
        candidates = (
            _candidate("1", "lexical", 1),
            _candidate("2", "lexical", 2),
        )
        cases = (
            ReversingReranker(result=(candidates[0],)),
            ReversingReranker(result=RuntimeError("private model endpoint")),
        )
        for reranker in cases:
            with self.subTest(result=reranker.result):
                result = await _service(
                    providers={
                        "lexical": StaticProvider(
                            "lexical",
                            _provider_result("lexical", *candidates),
                            [],
                        )
                    },
                    repository=CanonicalRepository(),
                    reranker=reranker,
                    rerank_enabled=True,
                ).retrieve(_query(limit=2, rerank=True))

                self.assertEqual(
                    (_record_id("1"), _record_id("2")),
                    tuple(item.evidence_refs[0].record_id for item in result.items),
                )
                self.assertEqual("degraded", result.providers[-1].status)
                self.assertEqual("RERANKER_FAILED", result.providers[-1].reason)
                self.assertEqual(0, result.providers[-1].returned_count)
                self.assertNotIn("private model endpoint", repr(result))

    async def test_hung_reranker_times_out_and_restores_prefailure_order(self):
        candidates = (
            _candidate("1", "lexical", 1),
            _candidate("2", "lexical", 2),
        )
        service = _service(
            providers={
                "lexical": StaticProvider(
                    "lexical", _provider_result("lexical", *candidates), []
                )
            },
            repository=CanonicalRepository(),
            reranker=HangingReranker(),
            rerank_enabled=True,
            rerank_timeout_seconds=0.01,
        )

        result = await asyncio.wait_for(
            service.retrieve(_query(limit=2, rerank=True)), timeout=0.5
        )

        self.assertEqual(
            (_record_id("1"), _record_id("2")),
            tuple(item.evidence_refs[0].record_id for item in result.items),
        )
        self.assertEqual("degraded", result.providers[-1].status)
        self.assertEqual("RERANKER_FAILED", result.providers[-1].reason)

    async def test_missing_canonical_content_and_exhausted_budget_abstain(self):
        candidates = (
            _candidate("1", "lexical", 1),
            _candidate("2", "lexical", 2),
        )
        lexical = StaticProvider(
            "lexical", _provider_result("lexical", *candidates), []
        )
        missing = await _service(
            providers={"lexical": lexical},
            repository=CanonicalRepository(omit_content={_record_id("2")}),
        ).retrieve(_query())
        exhausted = await _service(
            providers={"lexical": lexical},
            repository=CanonicalRepository(),
        ).retrieve(_query(token_budget=1))

        self.assertTrue(missing.abstained)
        self.assertEqual("EVIDENCE_CONTENT_UNAVAILABLE", missing.reason)
        self.assertTrue(exhausted.abstained)
        self.assertEqual("TOKEN_BUDGET_EXHAUSTED", exhausted.reason)
        self.assertEqual((), exhausted.items)
        self.assertIsNone(exhausted.context)

    async def test_unavailable_async_composer_has_a_distinct_abstention(self):
        candidate = _candidate("1", "lexical", 1)
        result = await _service(
            providers={
                "lexical": StaticProvider(
                    "lexical", _provider_result("lexical", candidate), []
                )
            },
            repository=CanonicalRepository(),
            composer=UnavailableComposer(),
        ).retrieve(_query())

        self.assertTrue(result.abstained)
        self.assertEqual("COMPOSER_UNAVAILABLE", result.reason)

    async def test_composer_cannot_change_selected_ranking_provenance(self):
        candidate = _candidate("1", "lexical", 1)
        result = await _service(
            providers={
                "lexical": StaticProvider(
                    "lexical", _provider_result("lexical", candidate), []
                )
            },
            repository=CanonicalRepository(),
            composer=ProvenanceTamperingComposer(),
        ).retrieve(_query())

        self.assertTrue(result.abstained)
        self.assertEqual("COMPOSITION_FAILED", result.reason)

    async def test_composer_cannot_flip_a_failed_outcome_signal(self):
        candidate = _candidate("1", "lexical", 1)
        result = await _service(
            providers={
                "lexical": StaticProvider(
                    "lexical", _provider_result("lexical", candidate), []
                )
            },
            repository=CanonicalRepository(
                selected_changes={
                    _record_id("1"): {
                        "outcome": "the original attempt completed",
                        "outcome_failed": False,
                    }
                }
            ),
            composer=OutcomeFlagTamperingComposer(),
        ).retrieve(_query())

        self.assertTrue(result.abstained)
        self.assertEqual("COMPOSITION_FAILED", result.reason)

    async def test_composer_cannot_fabricate_outcome_or_procedure_fields(self):
        candidate = _candidate("1", "lexical", 1)
        result = await _service(
            providers={
                "lexical": StaticProvider(
                    "lexical", _provider_result("lexical", candidate), []
                )
            },
            repository=CanonicalRepository(
                selected_changes={
                    _record_id("1"): {
                        "outcome": "canonical outcome",
                        "procedure_steps": ("canonical step",),
                    }
                }
            ),
            composer=StructuredFieldTamperingComposer(),
        ).retrieve(_query())

        self.assertTrue(result.abstained)
        self.assertEqual("COMPOSITION_FAILED", result.reason)

    async def test_composer_cannot_fabricate_legacy_metadata(self):
        candidate = _candidate("1", "lexical", 1)
        result = await _service(
            providers={
                "lexical": StaticProvider(
                    "lexical", _provider_result("lexical", candidate), []
                )
            },
            repository=CanonicalRepository(
                selected_changes={
                    _record_id("1"): {
                        "rationale": "canonical rationale",
                        "tags": ("canonical",),
                        "worked": False,
                    }
                }
            ),
            composer=LegacyMetadataTamperingComposer(),
        ).retrieve(_query())

        self.assertTrue(result.abstained)
        self.assertEqual("COMPOSITION_FAILED", result.reason)

    async def test_composer_cannot_replace_the_query_token_budget(self):
        candidate = _candidate("1", "lexical", 1)
        result = await _service(
            providers={
                "lexical": StaticProvider(
                    "lexical", _provider_result("lexical", candidate), []
                )
            },
            repository=CanonicalRepository(),
            composer=BudgetTamperingComposer(),
        ).retrieve(_query(token_budget=1))

        self.assertTrue(result.abstained)
        self.assertEqual("COMPOSITION_FAILED", result.reason)

    async def test_composer_cannot_fabricate_the_cited_excerpt(self):
        candidate = _candidate("1", "lexical", 1)
        result = await _service(
            providers={
                "lexical": StaticProvider(
                    "lexical", _provider_result("lexical", candidate), []
                )
            },
            repository=CanonicalRepository(),
            composer=ExcerptTamperingComposer(),
        ).retrieve(_query())

        self.assertTrue(result.abstained)
        self.assertEqual("COMPOSITION_FAILED", result.reason)

    async def test_composer_cannot_reorder_rendered_evidence(self):
        first = _candidate("1", "lexical", 1)
        second = _candidate("2", "lexical", 2)
        result = await _service(
            providers={
                "lexical": StaticProvider(
                    "lexical",
                    _provider_result("lexical", first, second),
                    [],
                )
            },
            repository=CanonicalRepository(),
            composer=OrderTamperingComposer(),
        ).retrieve(_query(limit=2))

        self.assertTrue(result.abstained)
        self.assertEqual("COMPOSITION_FAILED", result.reason)


class RetrievalServiceValidationTests(unittest.TestCase):
    def test_close_releases_the_owned_repository(self):
        repository = CanonicalRepository()
        service = _service(
            providers={
                "lexical": StaticProvider("lexical", _provider_result("lexical"), [])
            },
            repository=repository,
        )

        service.close()

        self.assertTrue(repository.closed)

    def test_injected_boundaries_and_ranking_configuration_fail_closed(self):
        from daem0nmcp.retrieval.service import RetrievalService

        repository = CanonicalRepository()
        lexical = StaticProvider("lexical", _provider_result("lexical"), [])
        invalid = (
            {"providers": {}},
            {"providers": {"wrong": lexical}},
            {
                "providers": {
                    "lexical": lexical,
                    "custom": StaticProvider("custom", _provider_result("custom"), []),
                }
            },
            {"repository": object()},
            {"composer": object()},
            {"planner": object()},
            {"clock": object()},
            {"reranker": object()},
            {"rerank_enabled": 1},
            {
                "providers": {"lexical": lexical},
                "weights": {"lexical": 0.0},
            },
            {
                "providers": {
                    "lexical": lexical,
                    "dense": StaticProvider("dense", _provider_result("dense"), []),
                },
                "weights": {"lexical": 1.0},
            },
            {"providers": {"lexical": lexical}, "rrf_k": 0},
            {"providers": {"lexical": lexical}, "rrf_k": 10**400},
            {
                "providers": {"lexical": lexical},
                "rerank_candidate_limit": 0,
            },
            {
                "providers": {"lexical": lexical},
                "rerank_timeout_seconds": 0.0,
            },
            {
                "providers": {"lexical": lexical},
                "rerank_timeout_seconds": float("nan"),
            },
            {
                "providers": {"lexical": lexical},
                "provider_timeout_seconds": 0.0,
            },
            {
                "providers": {"lexical": lexical},
                "provider_timeout_seconds": 10**400,
            },
            {
                "providers": {"lexical": lexical},
                "rerank_timeout_seconds": 10**400,
            },
        )
        for changes in invalid:
            values = {
                "providers": {"lexical": lexical},
                "repository": repository,
                "composer": _composer(),
                "clock": FixedClock(),
            }
            values.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                RetrievalService(**values)


if __name__ == "__main__":
    unittest.main()
