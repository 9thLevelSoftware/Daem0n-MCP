"""Deterministic intent planning for the v7 retrieval providers.

The planner only chooses candidate generators.  It never ranks or filters
evidence, and it cannot produce a vector-only plan: lexical retrieval is the
first request for every query.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from .types import RetrievalQuery

_MAX_INTENT_CHARS = 4096
_TOKEN = re.compile(r"[a-z0-9]+")
_OPTIONAL_ORDER = ("dense", "graph", "temporal", "procedure", "outcome")
_GRAPH_TERMS = frozenset(
    {
        "because",
        "cause",
        "caused",
        "causal",
        "connected",
        "connection",
        "depend",
        "depends",
        "entity",
        "entities",
        "lead",
        "leads",
        "relationship",
        "related",
        "why",
    }
)
_TEMPORAL_TERMS = frozenset(
    {
        "after",
        "before",
        "changed",
        "history",
        "last",
        "month",
        "previous",
        "since",
        "timeline",
        "today",
        "week",
        "when",
        "year",
        "yesterday",
    }
)
_PROCEDURE_TERMS = frozenset(
    {
        "how",
        "instruction",
        "instructions",
        "procedure",
        "step",
        "steps",
        "workflow",
    }
)
_OUTCOME_TERMS = frozenset(
    {
        "choice",
        "chose",
        "decision",
        "fail",
        "failed",
        "failure",
        "outcome",
        "result",
        "results",
        "success",
        "successful",
        "worked",
    }
)


def _positive_integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    """One bounded provider invocation in execution order."""

    provider: str
    limit: int

    def __post_init__(self) -> None:
        if self.provider not in {"lexical", *_OPTIONAL_ORDER}:
            raise ValueError("provider is not a supported retrieval channel")
        _positive_integer(self.limit, "limit")


@dataclass(frozen=True, slots=True)
class RetrievalPlan:
    """Immutable provider plan; lexical is structurally non-optional."""

    requests: tuple[ProviderRequest, ...]

    def __post_init__(self) -> None:
        if not self.requests or self.requests[0].provider != "lexical":
            raise ValueError("a retrieval plan must begin with lexical")
        names = tuple(request.provider for request in self.requests)
        if len(names) != len(set(names)):
            raise ValueError("a provider can be requested only once")

    @property
    def provider_names(self) -> tuple[str, ...]:
        return tuple(request.provider for request in self.requests)


class RetrievalPlanner:
    """Add ready optional providers according to bounded lexical intent."""

    def __init__(self, *, optional_candidate_limit: int = 25) -> None:
        self.optional_candidate_limit = _positive_integer(
            optional_candidate_limit, "optional_candidate_limit"
        )

    def plan(
        self,
        query: RetrievalQuery,
        *,
        ready_providers: Iterable[str] = (),
    ) -> RetrievalPlan:
        if not isinstance(query, RetrievalQuery):
            raise ValueError("query must be a RetrievalQuery")
        ready = {
            provider
            for provider in ready_providers
            if isinstance(provider, str) and provider in _OPTIONAL_ORDER
        }
        intent_text = query.text[:_MAX_INTENT_CHARS].casefold()
        words = frozenset(_TOKEN.findall(intent_text))
        intent = {
            "graph": not words.isdisjoint(_GRAPH_TERMS),
            "temporal": (
                query.as_of_valid_time is not None
                or query.as_of_transaction_time is not None
                or re.search(r"\bas\s+of\b", intent_text) is not None
                or not words.isdisjoint(_TEMPORAL_TERMS)
            ),
            "procedure": not words.isdisjoint(_PROCEDURE_TERMS),
            "outcome": not words.isdisjoint(_OUTCOME_TERMS),
        }
        optional_limit = min(query.candidate_limit, self.optional_candidate_limit)
        requests = [ProviderRequest("lexical", query.candidate_limit)]
        if "dense" in ready:
            requests.append(ProviderRequest("dense", optional_limit))
        for provider in _OPTIONAL_ORDER[1:]:
            if provider in ready and intent[provider]:
                requests.append(ProviderRequest(provider, optional_limit))
        return RetrievalPlan(tuple(requests))


__all__ = ["ProviderRequest", "RetrievalPlan", "RetrievalPlanner"]
