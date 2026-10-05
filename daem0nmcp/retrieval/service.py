"""Policy-first orchestration for the v7 retrieval pipeline.

The service coordinates projection providers, rank-only fusion, canonical
policy state, optional reranking, diversity selection, and asynchronous
evidence composition.  Provider candidate text is never part of this facade:
content enters only from the authoritative repository after policy acceptance.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter_ns
from types import MappingProxyType
from typing import Literal, Protocol, runtime_checkable

from ..bounded_workers import BoundedWorkerPool
from ..code_bindings import BindingEvaluator
from .composer import (
    CompositionResult,
    RetentionPolicy,
    SelectedEvidence,
    _normalize_evidence_text,
    query_identifiers,
)
from .fusion import (
    DEFAULT_RRF_K,
    DEFAULT_RRF_WEIGHTS,
    MAX_RRF_K,
    weighted_reciprocal_rank_fusion,
)
from .planner import RetrievalPlan, RetrievalPlanner
from .policy import PolicyRecord, apply_retrieval_policy
from .types import (
    EvidenceItem,
    FusedCandidate,
    ProviderDiagnostic,
    ProviderResult,
    RetrievalProvider,
    RetrievalQuery,
    RetrievalResult,
    _aware_datetime,
)
from .utility import UtilityContribution, UtilityEstimate, fold_utility, utility_signal

_VALIDITY_WORKERS = BoundedWorkerPool(
    max_workers=2, thread_name_prefix="daem0nmcp-validity"
)

_PROVIDER_ORDER = (
    "lexical",
    "dense",
    "graph",
    "temporal",
    "procedure",
    "outcome",
)
_SUPPORTED_PROVIDERS = frozenset(_PROVIDER_ORDER)


@runtime_checkable
class UtilityContributionRepository(Protocol):
    """Optional outcome-learning reads supported by canonical repositories."""

    async def load_utility_contributions(
        self,
        workspace_id: str,
        record_ids: tuple[str, ...],
        transaction_at_us: int,
    ) -> Mapping[str, tuple[UtilityContribution, ...]]: ...


class RetrievalRepository(Protocol):
    """Canonical metadata and content reads for one retrieval snapshot."""

    async def load_policy_records(
        self,
        query: RetrievalQuery,
        candidates: tuple[FusedCandidate, ...],
        *,
        snapshot_time: datetime,
    ) -> Iterable[PolicyRecord]:
        """Return authoritative policy state without candidate content."""
        ...

    async def load_selected_evidence(
        self,
        query: RetrievalQuery,
        candidates: tuple[FusedCandidate, ...],
        *,
        snapshot_time: datetime,
    ) -> Iterable[SelectedEvidence]:
        """Return canonical content only for already accepted candidates."""
        ...


class RetrievalReranker(Protocol):
    """Optional rank adapter; its output must be an exact permutation."""

    async def rerank(
        self,
        query: RetrievalQuery,
        evidence: tuple[SelectedEvidence, ...],
    ) -> Iterable[FusedCandidate]:
        """Return reordered candidates without adding or changing evidence."""
        ...


class RetrievalClock(Protocol):
    """Clock seam used to capture exactly one retrieval snapshot."""

    def now(self) -> datetime:
        """Return a timezone-aware timestamp."""
        ...


class AsyncEvidenceComposer(Protocol):
    """Non-blocking composition boundary used by the async service."""

    async def compose_async(
        self,
        selected: Iterable[SelectedEvidence],
        *,
        token_budget: int,
        retention: RetentionPolicy | None = None,
        label_applicability: bool = False,
    ) -> CompositionResult:
        """Compose under a total bounded-worker/timeout policy."""
        ...


class _SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


def _positive_integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _positive_finite_number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be positive and finite")
    try:
        numeric = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{field_name} must be positive and finite") from exc
    if not math.isfinite(numeric) or numeric <= 0:
        raise ValueError(f"{field_name} must be positive and finite")
    return numeric


def _validated_weights(weights: Mapping[str, float]) -> dict[str, float]:
    if not isinstance(weights, Mapping) or not weights:
        raise ValueError("weights must be a non-empty mapping")
    validated: dict[str, float] = {}
    for provider, weight in weights.items():
        if provider not in _SUPPORTED_PROVIDERS:
            raise ValueError("weights contain an unsupported provider")
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            raise ValueError("weights must be positive finite numbers")
        try:
            numeric = float(weight)
        except (OverflowError, ValueError) as exc:
            raise ValueError("weights must be positive finite numbers") from exc
        if not math.isfinite(numeric) or numeric <= 0:
            raise ValueError("weights must be positive finite numbers")
        validated[provider] = numeric
    return validated


def _identity(candidate: FusedCandidate) -> tuple[str, str | None]:
    return candidate.record_id, candidate.version_id


@dataclass(frozen=True, slots=True)
class RetrievalCandidateResult:
    """Policy-valid canonical evidence before any context composition."""

    selected: tuple[SelectedEvidence, ...] = ()
    providers: tuple[ProviderDiagnostic, ...] = ()
    weights: tuple[tuple[str, float], ...] = ()
    policy_rejection_counts: tuple[tuple[str, int], ...] = ()
    abstained: bool = False
    reason: str | None = None


class RetrievalService:
    """Execute the v7 retrieval contract without bypassing policy."""

    def __init__(
        self,
        *,
        providers: Mapping[str, RetrievalProvider],
        repository: RetrievalRepository,
        composer: AsyncEvidenceComposer,
        planner: RetrievalPlanner | None = None,
        clock: RetrievalClock | None = None,
        reranker: RetrievalReranker | None = None,
        rerank_enabled: bool = False,
        rerank_candidate_limit: int = 25,
        rerank_timeout_seconds: float = 2.0,
        provider_timeout_seconds: float = 10.0,
        weights: Mapping[str, float] = DEFAULT_RRF_WEIGHTS,
        rrf_k: int = DEFAULT_RRF_K,
        utility_mode: Literal["off", "shadow", "apply"] = "off",
        utility_weight: float = 0.1,
        utility_credit: Literal["single_step", "trace"] = "trace",
        utility_candidate_limit: int = 25,
        validity_mode: Literal["off", "shadow", "apply"] = "off",
        retention_mode: Literal["off", "shadow", "apply"] = "off",
    ) -> None:
        self._providers = self._validate_providers(providers)
        if not callable(
            getattr(repository, "load_policy_records", None)
        ) or not callable(getattr(repository, "load_selected_evidence", None)):
            raise ValueError("repository must provide canonical retrieval reads")
        if not callable(getattr(composer, "compose_async", None)):
            raise ValueError("composer must provide compose_async")
        selected_planner = planner or RetrievalPlanner()
        if not callable(getattr(selected_planner, "plan", None)):
            raise ValueError("planner must provide plan")
        selected_clock = clock or _SystemClock()
        if not callable(getattr(selected_clock, "now", None)):
            raise ValueError("clock must provide now")
        if reranker is not None and not callable(getattr(reranker, "rerank", None)):
            raise ValueError("reranker must provide rerank")
        if not isinstance(rerank_enabled, bool):
            raise ValueError("rerank_enabled must be boolean")
        if utility_mode not in {"off", "shadow", "apply"}:
            raise ValueError("utility_mode is invalid")
        if utility_credit not in {"single_step", "trace"}:
            raise ValueError("utility_credit is invalid")
        if (
            isinstance(utility_weight, bool)
            or not isinstance(utility_weight, (int, float))
            or not math.isfinite(utility_weight)
            or not 0 <= utility_weight <= 1
        ):
            raise ValueError("utility_weight must be finite and between 0 and 1")
        utility_candidate_limit = _positive_integer(
            utility_candidate_limit, "utility_candidate_limit"
        )
        if utility_candidate_limit > 200:
            raise ValueError("utility_candidate_limit must not exceed 200")
        if utility_mode != "off" and not callable(
            getattr(repository, "load_utility_contributions", None)
        ):
            raise ValueError("repository must provide utility contribution reads")
        self._utility_mode = utility_mode
        self._utility_weight = float(utility_weight)
        self._utility_credit = utility_credit
        self._utility_candidate_limit = utility_candidate_limit
        if validity_mode not in {"off", "shadow", "apply"}:
            raise ValueError("validity_mode is invalid")
        self._validity_mode = validity_mode
        self._binding_evaluator = BindingEvaluator()
        if retention_mode not in {"off", "shadow", "apply"}:
            raise ValueError("retention_mode is invalid")
        self._retention_mode = retention_mode

        validated_weights = _validated_weights(weights)
        missing_weights = set(self._providers).difference(validated_weights)
        if missing_weights:
            raise ValueError("every registered provider requires an RRF weight")

        self._repository = repository
        self._composer = composer
        self._planner = selected_planner
        self._clock = selected_clock
        self._reranker = reranker
        self._rerank_enabled = rerank_enabled
        self._rerank_candidate_limit = _positive_integer(
            rerank_candidate_limit, "rerank_candidate_limit"
        )
        self._rerank_timeout_seconds = _positive_finite_number(
            rerank_timeout_seconds, "rerank_timeout_seconds"
        )
        self._provider_timeout_seconds = _positive_finite_number(
            provider_timeout_seconds, "provider_timeout_seconds"
        )
        self._weights = MappingProxyType(validated_weights)
        self._reported_weights = tuple(sorted(validated_weights.items()))
        self._rrf_k = _positive_integer(rrf_k, "rrf_k")
        if self._rrf_k > MAX_RRF_K:
            raise ValueError(f"rrf_k must not exceed {MAX_RRF_K}")

    def close(self) -> None:
        """Release resources owned by registered providers and repository."""

        first_error: Exception | None = None
        for resource in (*self._providers.values(), self._repository):
            close = getattr(resource, "close", None)
            if not callable(close):
                continue
            try:
                close()
            except Exception as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error

    @staticmethod
    def _validate_providers(
        providers: Mapping[str, RetrievalProvider],
    ) -> dict[str, RetrievalProvider]:
        if not isinstance(providers, Mapping) or "lexical" not in providers:
            raise ValueError("a lexical provider is required")
        validated: dict[str, RetrievalProvider] = {}
        for name, provider in providers.items():
            if name not in _SUPPORTED_PROVIDERS:
                raise ValueError("provider registry contains an unsupported name")
            if getattr(provider, "name", None) != name or not callable(
                getattr(provider, "search", None)
            ):
                raise ValueError("provider registry keys must match provider names")
            validated[name] = provider
        return validated

    async def retrieve(
        self, query: RetrievalQuery, *, workspace_root: Path | None = None
    ) -> RetrievalResult:
        """Retrieve, authorize, select, and compose evidence for *query*."""

        candidates = await self.retrieve_candidates(
            query, workspace_root=workspace_root
        )
        if candidates.abstained:
            return RetrievalResult(
                providers=candidates.providers,
                weights=candidates.weights,
                policy_rejection_counts=candidates.policy_rejection_counts,
                abstained=True,
                reason=candidates.reason,
            )
        diagnostics = list(candidates.providers)
        retention = (
            RetentionPolicy(query.intent, query_identifiers(query.text))
            if query.intent is not None and self._retention_mode != "off"
            else None
        )
        try:
            started = perf_counter_ns()
            composition = await self._composer.compose_async(
                candidates.selected,
                token_budget=query.token_budget,
                retention=retention if self._retention_mode == "apply" else None,
                label_applicability=self._validity_mode == "apply",
            )
            if not isinstance(composition, CompositionResult):
                raise ValueError("composer returned an invalid result")
            if (
                composition.context.token_budget != query.token_budget
                or composition.context.rendered_tokens > query.token_budget
            ):
                raise ValueError("composer changed the validated token budget")
            items = self._validated_composed_items(composition, candidates.selected)
            if retention is not None:
                retained = composition
                retention_failed = False
                if self._retention_mode == "shadow" and retention.intent != "explore":
                    try:
                        retained = await self._composer.compose_async(
                            candidates.selected,
                            token_budget=query.token_budget,
                            retention=retention,
                            label_applicability=self._validity_mode == "apply",
                        )
                        if not isinstance(retained, CompositionResult):
                            raise ValueError("composer returned an invalid result")
                    except Exception:
                        retention_failed = True
                diagnostics.append(
                    ProviderDiagnostic(
                        provider="retention",
                        status="degraded" if retention_failed else "ready",
                        manifest_generation=None,
                        elapsed_ms=(perf_counter_ns() - started) / 1_000_000,
                        reason=(
                            "RETENTION_FAILED"
                            if retention_failed
                            else "RETENTION_APPLIED"
                            if self._retention_mode == "apply"
                            else "RETENTION_SHADOW"
                        ),
                        returned_count=0 if retention_failed else len(retained.items),
                    )
                )
        except Exception:
            return self._abstention(
                "COMPOSITION_FAILED",
                diagnostics=diagnostics,
                rejection_counts=candidates.policy_rejection_counts,
            )
        if not items:
            reason = (
                "COMPOSER_UNAVAILABLE"
                if "COMPOSER_UNAVAILABLE" in composition.context.drop_reasons
                else "TOKEN_BUDGET_EXHAUSTED"
            )
            return self._abstention(
                reason,
                diagnostics=diagnostics,
                rejection_counts=candidates.policy_rejection_counts,
            )
        return RetrievalResult(
            items=items,
            context=composition.context,
            providers=tuple(diagnostics),
            weights=candidates.weights,
            policy_rejection_counts=candidates.policy_rejection_counts,
        )

    async def retrieve_candidates(
        self, query: RetrievalQuery, *, workspace_root: Path | None = None
    ) -> RetrievalCandidateResult:
        """Retrieve authenticated policy-valid evidence without composing it."""

        if not isinstance(query, RetrievalQuery):
            raise ValueError("query must be a RetrievalQuery")
        snapshot_time = self._clock.now()
        _aware_datetime(snapshot_time, "snapshot_time")
        provider_query = replace(
            query,
            as_of_valid_time=query.as_of_valid_time or snapshot_time,
            as_of_transaction_time=(query.as_of_transaction_time or snapshot_time),
        )

        try:
            plan = self._planner.plan(
                query,
                ready_providers=tuple(
                    name for name in _PROVIDER_ORDER[1:] if name in self._providers
                ),
            )
        except Exception:
            return self._candidate_abstention("PLANNING_FAILED")
        if not isinstance(plan, RetrievalPlan) or any(
            request.limit > query.candidate_limit for request in plan.requests
        ):
            return self._candidate_abstention("PLANNING_FAILED")

        results: list[ProviderResult] = []
        lexical_request = plan.requests[0]
        lexical = await self._invoke_provider(
            lexical_request.provider,
            provider_query,
            lexical_request.limit,
        )
        if lexical.status in {"unavailable", "failed"}:
            return self._candidate_abstention(
                lexical.reason or "LEXICAL_PROVIDER_FAILED",
                diagnostics=(ProviderDiagnostic.from_result(lexical),),
            )

        provider_results = {"lexical": lexical}
        graph_request = None
        for request in plan.requests[1:]:
            if request.provider == "graph":
                graph_request = request
                continue
            result = await self._invoke_provider(
                request.provider, provider_query, request.limit
            )
            provider_results[request.provider] = result

        if graph_request is not None:
            seed_results = tuple(
                provider_results[name]
                for name in ("lexical", "dense")
                if name in provider_results
            )
            graph_seeds = weighted_reciprocal_rank_fusion(
                seed_results,
                weights=self._weights,
                k=self._rrf_k,
            )
            provider_results["graph"] = await self._invoke_provider(
                "graph",
                provider_query,
                graph_request.limit,
                seeds=graph_seeds,
            )

        results = [provider_results[request.provider] for request in plan.requests]
        diagnostics = [ProviderDiagnostic.from_result(result) for result in results]

        fused = weighted_reciprocal_rank_fusion(
            results,
            weights=self._weights,
            k=self._rrf_k,
        )
        try:
            policy_records = tuple(
                await self._repository.load_policy_records(
                    query,
                    fused,
                    snapshot_time=snapshot_time,
                )
            )
            policy_result = apply_retrieval_policy(
                query,
                fused,
                policy_records,
                snapshot_time=snapshot_time,
            )
        except Exception:
            return self._candidate_abstention(
                "POLICY_STATE_UNAVAILABLE", diagnostics=diagnostics
            )

        if policy_result.abstained:
            return self._candidate_abstention(
                policy_result.reason or "ALL_CANDIDATES_FILTERED",
                diagnostics=diagnostics,
                rejection_counts=policy_result.rejection_counts,
            )

        ordered_candidates = policy_result.candidates
        content_cache: dict[tuple[str, str | None], SelectedEvidence] = {}
        if query.rerank:
            rerank_sources: tuple[SelectedEvidence, ...] = ()
            if self._rerank_enabled and self._reranker is not None:
                head_size = min(len(ordered_candidates), self._rerank_candidate_limit)
                rerank_candidates = ordered_candidates[:head_size]
                started = perf_counter_ns()
                try:
                    rerank_sources = await self._load_selected_evidence(
                        query,
                        rerank_candidates,
                        policy_records,
                        snapshot_time,
                    )
                    content_cache.update(
                        (_identity(source.candidate), source)
                        for source in rerank_sources
                    )
                except Exception:
                    reranker_diagnostic = ProviderDiagnostic(
                        provider="reranker",
                        status="degraded",
                        manifest_generation=None,
                        elapsed_ms=(perf_counter_ns() - started) / 1_000_000,
                        reason="RERANKER_FAILED",
                        returned_count=0,
                    )
                else:
                    ordered_candidates, reranker_diagnostic = await self._rerank(
                        query, ordered_candidates, rerank_sources
                    )
            else:
                ordered_candidates, reranker_diagnostic = await self._rerank(
                    query, ordered_candidates, rerank_sources
                )
            diagnostics.append(reranker_diagnostic)

        utility_estimates: dict[str, UtilityEstimate] = {}
        if self._utility_mode != "off":
            (
                ordered_candidates,
                utility_estimates,
                diagnostic,
            ) = await self._apply_utility(query, ordered_candidates, snapshot_time)
            diagnostics.append(diagnostic)

        diverse_candidates = self._select_diverse_candidates(
            ordered_candidates,
            policy_records,
            query.limit,
        )
        missing_candidates = tuple(
            candidate
            for candidate in diverse_candidates
            if _identity(candidate) not in content_cache
        )
        try:
            if missing_candidates:
                loaded = await self._load_selected_evidence(
                    query,
                    missing_candidates,
                    policy_records,
                    snapshot_time,
                )
                content_cache.update(
                    (_identity(source.candidate), source) for source in loaded
                )
            diverse = tuple(
                content_cache[_identity(candidate)] for candidate in diverse_candidates
            )
            diverse = tuple(
                replace(
                    source,
                    utility=(
                        estimate.q_single
                        if self._utility_credit == "single_step"
                        else estimate.q_trace
                    ),
                )
                if (estimate := utility_estimates.get(source.candidate.record_id))
                is not None
                and estimate.evidence_count > 0
                else source
                for source in diverse
            )
        except Exception:
            return self._candidate_abstention(
                "EVIDENCE_CONTENT_UNAVAILABLE",
                diagnostics=diagnostics,
                rejection_counts=policy_result.rejection_counts,
            )

        if (
            self._validity_mode != "off"
            and workspace_root is not None
            and any(source.code_bindings for source in diverse)
        ):
            diverse, diagnostic = await self._evaluate_bindings(workspace_root, diverse)
            diagnostics.append(diagnostic)

        return RetrievalCandidateResult(
            selected=diverse,
            providers=tuple(diagnostics),
            weights=self._reported_weights,
            policy_rejection_counts=policy_result.rejection_counts,
        )

    async def _evaluate_bindings(
        self, root: Path, selected: tuple[SelectedEvidence, ...]
    ) -> tuple[tuple[SelectedEvidence, ...], ProviderDiagnostic]:
        started = perf_counter_ns()

        def evaluate() -> tuple[SelectedEvidence, ...]:
            evaluated = []
            with self._binding_evaluator.read_batch():
                for source in selected:
                    evaluation = (
                        self._binding_evaluator.evaluate(root, source.binding_context)
                        if source.code_bindings
                        else None
                    )
                    evaluated.append(
                        replace(
                            source,
                            applicability=evaluation.applicability,
                            changed_bindings=evaluation.changed,
                        )
                        if evaluation is not None
                        else source
                    )
            return tuple(evaluated)

        try:
            evaluated = await _VALIDITY_WORKERS.run(evaluate)
        except Exception:
            return selected, ProviderDiagnostic(
                provider="validity",
                status="degraded",
                manifest_generation=None,
                elapsed_ms=(perf_counter_ns() - started) / 1_000_000,
                reason="VALIDITY_FAILED",
                returned_count=0,
            )
        return evaluated, ProviderDiagnostic(
            provider="validity",
            status="ready",
            manifest_generation=None,
            elapsed_ms=(perf_counter_ns() - started) / 1_000_000,
            reason="VALIDITY_APPLIED"
            if self._validity_mode == "apply"
            else "VALIDITY_SHADOW",
            returned_count=sum(
                source.applicability == "needs_revalidation" for source in evaluated
            ),
        )

    async def _apply_utility(
        self,
        query: RetrievalQuery,
        ordered: tuple[FusedCandidate, ...],
        snapshot_time: datetime,
    ) -> tuple[
        tuple[FusedCandidate, ...], dict[str, UtilityEstimate], ProviderDiagnostic
    ]:
        started = perf_counter_ns()
        try:
            head = ordered[: self._utility_candidate_limit]
            transaction_time = query.as_of_transaction_time or snapshot_time
            delta = transaction_time.astimezone(timezone.utc) - datetime(
                1970, 1, 1, tzinfo=timezone.utc
            )
            transaction_at_us = (
                delta.days * 86_400 + delta.seconds
            ) * 1_000_000 + delta.microseconds
            if not isinstance(self._repository, UtilityContributionRepository):
                raise TypeError("repository must provide utility contribution reads")
            contributions = await self._repository.load_utility_contributions(
                query.workspace_id,
                tuple(candidate.record_id for candidate in head),
                transaction_at_us,
            )
            estimates = {
                candidate.record_id: fold_utility(
                    contributions.get(candidate.record_id, ())
                )
                for candidate in head
            }
            ranked = sorted(
                enumerate(head),
                key=lambda pair: (
                    -(
                        61 / (61 + pair[0])
                        + self._utility_weight
                        * utility_signal(
                            estimates[pair[1].record_id], self._utility_credit
                        )
                    ),
                    pair[0],
                ),
            )
            sorted_head = tuple(candidate for _, candidate in ranked)
            if self._utility_mode == "apply":
                result = sorted_head + ordered[len(head) :]
                reason = "UTILITY_APPLIED"
            else:
                result = ordered
                reason = (
                    "UTILITY_SHADOW_REORDER"
                    if sorted_head != head
                    else "UTILITY_SHADOW_SAME"
                )
            status = "ready"
            returned_count = sum(
                estimates[candidate.record_id].evidence_count > 0 for candidate in head
            )
        except Exception:
            result = ordered
            estimates = {}
            status = "degraded"
            reason = "UTILITY_FAILED"
            returned_count = 0
        return (
            result,
            estimates,
            ProviderDiagnostic(
                provider="utility",
                status=status,
                manifest_generation=None,
                elapsed_ms=(perf_counter_ns() - started) / 1_000_000,
                reason=reason,
                returned_count=returned_count,
            ),
        )

    async def _invoke_provider(
        self,
        name: str,
        query: RetrievalQuery,
        limit: int,
        *,
        seeds: tuple[FusedCandidate, ...] | None = None,
    ) -> ProviderResult:
        started = perf_counter_ns()
        provider = self._providers.get(name)
        if provider is None:
            return ProviderResult(
                provider=name,
                status="unavailable",
                reason=f"{name.upper()}_UNAVAILABLE",
                elapsed_ms=(perf_counter_ns() - started) / 1_000_000,
            )
        try:
            invocation = (
                provider.search(query, limit, seeds=seeds)
                if name == "graph"
                else provider.search(query, limit)
            )
            result = await asyncio.wait_for(
                invocation,
                timeout=self._provider_timeout_seconds,
            )
            if not isinstance(result, ProviderResult) or result.provider != name:
                raise ValueError("provider returned a mismatched result")
            return result
        except asyncio.TimeoutError:
            return ProviderResult(
                provider=name,
                status="failed",
                reason=f"{name.upper()}_PROVIDER_TIMEOUT",
                elapsed_ms=(perf_counter_ns() - started) / 1_000_000,
            )
        except Exception:
            return ProviderResult(
                provider=name,
                status="failed",
                reason=f"{name.upper()}_PROVIDER_FAILED",
                elapsed_ms=(perf_counter_ns() - started) / 1_000_000,
            )

    @staticmethod
    def _validate_selected_evidence(
        candidates: tuple[FusedCandidate, ...],
        records: tuple[PolicyRecord, ...],
        selected: tuple[SelectedEvidence, ...],
    ) -> tuple[SelectedEvidence, ...]:
        if not all(isinstance(source, SelectedEvidence) for source in selected):
            raise ValueError("repository returned invalid evidence")
        expected = {_identity(candidate): candidate for candidate in candidates}
        if len(expected) != len(candidates):
            raise ValueError("accepted candidates must have unique identities")
        returned: dict[tuple[str, str | None], SelectedEvidence] = {}
        for source in selected:
            identity = _identity(source.candidate)
            if (
                identity in returned
                or identity not in expected
                or source.candidate != expected[identity]
            ):
                raise ValueError("repository changed selected evidence")
            returned[identity] = source
        if set(returned) != set(expected):
            raise ValueError("repository omitted selected evidence")

        record_by_identity = {record.identity: record for record in records}
        for identity, source in returned.items():
            state = record_by_identity.get(identity)
            if state is None or source.category != state.category:
                raise ValueError("repository content disagrees with policy state")
            supersession = tuple(
                note.removeprefix("SUPERSEDED_BY:")
                for note in source.candidate.policy_notes
                if note.startswith("SUPERSEDED_BY:")
            )
            if supersession:
                if (
                    len(supersession) != 1
                    or source.status != "superseded"
                    or source.superseded_by_version_id != supersession[0]
                ):
                    raise ValueError("superseded content lost policy provenance")
            elif source.status != "current":
                raise ValueError("current policy evidence was relabeled")
        return tuple(returned[_identity(candidate)] for candidate in candidates)

    async def _load_selected_evidence(
        self,
        query: RetrievalQuery,
        candidates: tuple[FusedCandidate, ...],
        records: tuple[PolicyRecord, ...],
        snapshot_time: datetime,
    ) -> tuple[SelectedEvidence, ...]:
        selected = tuple(
            await self._repository.load_selected_evidence(
                query,
                candidates,
                snapshot_time=snapshot_time,
            )
        )
        return self._validate_selected_evidence(candidates, records, selected)

    async def _rerank(
        self,
        query: RetrievalQuery,
        candidates: tuple[FusedCandidate, ...],
        selected: tuple[SelectedEvidence, ...],
    ) -> tuple[tuple[FusedCandidate, ...], ProviderDiagnostic]:
        if not self._rerank_enabled or self._reranker is None:
            return candidates, ProviderDiagnostic(
                provider="reranker",
                status="unavailable",
                manifest_generation=None,
                elapsed_ms=0.0,
                reason="RERANKER_UNAVAILABLE",
                returned_count=0,
            )

        head_size = min(len(candidates), self._rerank_candidate_limit)
        head = candidates[:head_size]
        selected_by_identity = {
            _identity(source.candidate): source for source in selected
        }
        rerank_input = tuple(
            selected_by_identity[_identity(candidate)] for candidate in head
        )
        started = perf_counter_ns()
        try:
            reranked = tuple(
                await asyncio.wait_for(
                    self._reranker.rerank(query, rerank_input),
                    timeout=self._rerank_timeout_seconds,
                )
            )
            originals = {_identity(candidate): candidate for candidate in head}
            identities = tuple(_identity(candidate) for candidate in reranked)
            if (
                len(reranked) != len(head)
                or len(set(identities)) != len(identities)
                or set(identities) != set(originals)
                or any(
                    not isinstance(candidate, FusedCandidate)
                    or candidate != originals[_identity(candidate)]
                    for candidate in reranked
                )
            ):
                raise ValueError("reranker did not return an exact permutation")
        except Exception:
            return candidates, ProviderDiagnostic(
                provider="reranker",
                status="degraded",
                manifest_generation=None,
                elapsed_ms=(perf_counter_ns() - started) / 1_000_000,
                reason="RERANKER_FAILED",
                returned_count=0,
            )
        return reranked + candidates[head_size:], ProviderDiagnostic(
            provider="reranker",
            status="ready",
            manifest_generation=None,
            elapsed_ms=(perf_counter_ns() - started) / 1_000_000,
            reason=None,
            returned_count=len(head),
        )

    @staticmethod
    def _select_diverse_candidates(
        candidates: tuple[FusedCandidate, ...],
        records: tuple[PolicyRecord, ...],
        limit: int,
    ) -> tuple[FusedCandidate, ...]:
        record_by_identity = {record.identity: record for record in records}
        chosen: list[FusedCandidate] = []
        chosen_ids: set[tuple[str, str | None]] = set()
        seen_categories: set[str] = set()
        for candidate in candidates:
            category = record_by_identity[_identity(candidate)].category.casefold()
            if category in seen_categories:
                continue
            chosen.append(candidate)
            chosen_ids.add(_identity(candidate))
            seen_categories.add(category)
            if len(chosen) == limit:
                return tuple(chosen)
        for candidate in candidates:
            identity = _identity(candidate)
            if identity in chosen_ids:
                continue
            chosen.append(candidate)
            if len(chosen) == limit:
                break
        return tuple(chosen)

    @staticmethod
    def _validated_composed_items(
        composition: CompositionResult,
        selected: tuple[SelectedEvidence, ...],
    ) -> tuple[EvidenceItem, ...]:
        allowed = {
            frozenset(source.candidate.evidence_refs): (index, source)
            for index, source in enumerate(selected)
        }
        item_keys = [frozenset(item.evidence_refs) for item in composition.items]
        if (
            len(item_keys) != len(set(item_keys))
            or any(key not in allowed for key in item_keys)
            or [allowed[key][0] for key in item_keys]
            != sorted(allowed[key][0] for key in item_keys)
        ):
            raise ValueError("composer emitted unselected evidence")
        for item, key in zip(composition.items, item_keys, strict=True):
            _, source = allowed[key]
            candidate = source.candidate
            normalized_outcome = (
                _normalize_evidence_text(source.outcome)
                if source.outcome is not None
                else None
            )
            outcome_matches = (
                item.outcome is None
                if normalized_outcome is None
                else item.outcome is not None
                and normalized_outcome.startswith(item.outcome)
            )
            normalized_steps = tuple(
                _normalize_evidence_text(step) for step in source.procedure_steps
            )
            relation_paths = tuple(
                sorted(
                    {
                        evidence.relation_path
                        for evidence in candidate.evidence_refs
                        if evidence.relation_path
                    }
                )
            )
            steps_match = len(item.procedure_steps) == len(normalized_steps) and all(
                expected.startswith(actual)
                for actual, expected in zip(
                    item.procedure_steps, normalized_steps, strict=True
                )
            )
            if (
                item.evidence_refs != candidate.evidence_refs
                or item.channels != candidate.channels
                or item.score != candidate.score
                or item.category != _normalize_evidence_text(source.category)
                or item.rationale != source.rationale
                or item.tags != source.tags
                or item.worked != source.worked
                or item.utility != source.utility
                or item.applicability != source.applicability
                or item.changed_bindings != source.changed_bindings
                or item.status != source.status
                or item.superseded_by_version_id != source.superseded_by_version_id
                or not outcome_matches
                or item.outcome_failed != source.outcome_failed
                or not steps_match
                or item.relation_paths != relation_paths
                or item.relation_path != (relation_paths[0] if relation_paths else ())
            ):
                raise ValueError("composer changed selected provenance")
        return composition.items

    def _abstention(
        self,
        reason: str,
        *,
        diagnostics: Iterable[ProviderDiagnostic] = (),
        rejection_counts: tuple[tuple[str, int], ...] = (),
    ) -> RetrievalResult:
        return RetrievalResult(
            providers=tuple(diagnostics),
            weights=self._reported_weights,
            policy_rejection_counts=rejection_counts,
            abstained=True,
            reason=reason,
        )

    def _candidate_abstention(
        self,
        reason: str,
        *,
        diagnostics: Iterable[ProviderDiagnostic] = (),
        rejection_counts: tuple[tuple[str, int], ...] = (),
    ) -> RetrievalCandidateResult:
        return RetrievalCandidateResult(
            providers=tuple(diagnostics),
            weights=self._reported_weights,
            policy_rejection_counts=rejection_counts,
            abstained=True,
            reason=reason,
        )


__all__ = [
    "AsyncEvidenceComposer",
    "RetrievalClock",
    "RetrievalRepository",
    "RetrievalReranker",
    "RetrievalCandidateResult",
    "RetrievalService",
]
