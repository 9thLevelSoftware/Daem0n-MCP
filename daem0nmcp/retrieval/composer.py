"""Deterministic evidence composition with exact citation provenance.

The composer is deliberately storage- and model-agnostic.  Its input is the
small set of evidence already accepted by the retrieval policy pipeline, and
all token accounting is performed by one caller-supplied tokenizer.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Literal, Protocol

from ..bounded_workers import BoundedWorkerBusyError, BoundedWorkerPool
from .types import (
    _VERSION_ID,
    MAX_TOKEN_BUDGET,
    CitationEntry,
    ContextPackage,
    EvidenceItem,
    FusedCandidate,
    _legacy_metadata,
    _opaque,
)

_CITATION_PATTERN = re.compile(r"\[E[1-9][0-9]*\]")
_COMPOSER_WORKERS = BoundedWorkerPool(
    max_workers=2,
    thread_name_prefix="daem0nmcp-evidence-composer",
)
_COMPOSER_FALLBACK_WORKERS = BoundedWorkerPool(
    max_workers=2,
    thread_name_prefix="daem0nmcp-evidence-fallback",
)
_DETERMINISTIC_COMPOSE_TIMEOUT_SECONDS = 2.0


def _validated_token_budget(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or value > MAX_TOKEN_BUDGET
    ):
        raise ValueError(f"token_budget must be between 1 and {MAX_TOKEN_BUDGET}")
    return value


def _normalize_evidence_text(value: str) -> str:
    cleaned = " ".join(value.split())
    return _CITATION_PATTERN.sub(
        lambda match: "［" + match.group(0)[1:-1] + "］",
        cleaned,
    )


class Tokenizer(Protocol):
    """The single exact token counter used for one composition."""

    def count_tokens(self, text: str) -> int:
        """Return the non-negative token count for *text*."""


class ItemCompressor(Protocol):
    """Optional best-effort compressor for one evidence excerpt only."""

    def compress(
        self,
        text: str,
        *,
        max_tokens: int,
        protected_tokens: tuple[str, ...],
    ) -> str:
        """Return a shorter excerpt without changing protected tokens."""


@dataclass(frozen=True, slots=True)
class SelectedEvidence:
    """Canonical content for one post-policy fused candidate."""

    candidate: FusedCandidate
    content: str
    category: str
    rationale: str | None = None
    tags: tuple[str, ...] = ()
    worked: bool | None = None
    status: Literal["current", "superseded"] = "current"
    superseded_by_version_id: str | None = None
    outcome: str | None = None
    outcome_failed: bool = False
    procedure_steps: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, FusedCandidate):
            raise ValueError("candidate must be a policy-valid FusedCandidate")
        for field_name in ("content", "category"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        _legacy_metadata(self.rationale, self.tags, self.worked)
        if self.status not in {"current", "superseded"}:
            raise ValueError("status is invalid")
        if (self.status == "superseded") != (self.superseded_by_version_id is not None):
            raise ValueError("superseded evidence requires its invalidating version")
        if self.superseded_by_version_id is not None:
            _opaque(
                self.superseded_by_version_id,
                _VERSION_ID,
                "superseded_by_version_id",
            )
        if self.outcome is not None and (
            not isinstance(self.outcome, str) or not self.outcome.strip()
        ):
            raise ValueError("outcome must be non-empty when supplied")
        if not isinstance(self.outcome_failed, bool):
            raise ValueError("outcome_failed must be boolean")
        if self.outcome_failed and self.outcome is None:
            raise ValueError("a failed outcome requires outcome text")
        if not isinstance(self.procedure_steps, tuple) or not all(
            isinstance(step, str) and step.strip() for step in self.procedure_steps
        ):
            raise ValueError("procedure_steps must contain non-empty strings")

    @property
    def priority(self) -> bool:
        return self.category.casefold() == "warning" or self.outcome_failed


@dataclass(frozen=True, slots=True)
class CompositionResult:
    """Selected evidence items and their exact rendered context."""

    items: tuple[EvidenceItem, ...]
    context: ContextPackage

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple) or not all(
            isinstance(item, EvidenceItem) for item in self.items
        ):
            raise ValueError("items must contain EvidenceItem values")
        if not isinstance(self.context, ContextPackage):
            raise ValueError("context must be a ContextPackage")
        if tuple(item.citation for item in self.items) != tuple(
            citation.marker for citation in self.context.citations
        ):
            raise ValueError("items and citation manifest must match in exact order")
        for item, citation in zip(self.items, self.context.citations, strict=True):
            if (
                item.evidence_refs != citation.evidence_refs
                or item.channels != citation.channels
                or self.context.text[citation.excerpt_start : citation.excerpt_end]
                != item.excerpt
            ):
                raise ValueError(
                    "citation manifest must bind each rendered evidence item"
                )


@dataclass(frozen=True, slots=True)
class _PreparedItem:
    source: SelectedEvidence
    marker: str
    excerpt: str
    block: str
    token_count: int
    truncated: bool


class EvidenceComposer:
    """Pack selected evidence under an exact token and provenance budget."""

    def __init__(
        self,
        *,
        tokenizer: Tokenizer,
        compressor: ItemCompressor | None = None,
        compressor_timeout_seconds: float = 0.25,
        max_excerpt_chars: int = 1200,
        worker_pool: BoundedWorkerPool | None = None,
    ) -> None:
        counter = getattr(tokenizer, "count_tokens", None)
        if not callable(counter):
            raise ValueError("tokenizer must provide count_tokens(text)")
        if compressor is not None and not callable(
            getattr(compressor, "compress", None)
        ):
            raise ValueError("compressor must provide compress(text, ...)")
        if (
            isinstance(max_excerpt_chars, bool)
            or not isinstance(max_excerpt_chars, int)
            or max_excerpt_chars < 32
            or max_excerpt_chars > 16384
        ):
            raise ValueError("max_excerpt_chars must be between 32 and 16384")
        try:
            timeout = float(compressor_timeout_seconds)
        except (OverflowError, TypeError, ValueError):
            timeout = math.nan
        if (
            isinstance(compressor_timeout_seconds, bool)
            or not isinstance(compressor_timeout_seconds, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
            or timeout > 30
        ):
            raise ValueError("compressor_timeout_seconds must be between 0 and 30")
        if worker_pool is not None and not isinstance(worker_pool, BoundedWorkerPool):
            raise ValueError("worker_pool must be a BoundedWorkerPool")
        self._tokenizer = tokenizer
        self._compressor = compressor
        self._compressor_timeout_seconds = timeout
        self._max_excerpt_chars = max_excerpt_chars
        self._worker_pool = worker_pool or _COMPOSER_WORKERS

    async def compose_async(
        self,
        selected: Iterable[SelectedEvidence],
        *,
        token_budget: int,
    ) -> CompositionResult:
        """Compose without letting an optional compressor block the event loop."""

        _validated_token_budget(token_budget)
        sources = tuple(selected)
        primary_timeout = (
            self._compressor_timeout_seconds
            if self._compressor is not None
            else _DETERMINISTIC_COMPOSE_TIMEOUT_SECONDS
        )
        try:
            return await asyncio.wait_for(
                self._worker_pool.run(
                    lambda: self.compose(sources, token_budget=token_budget)
                ),
                timeout=primary_timeout,
            )
        except (asyncio.TimeoutError, BoundedWorkerBusyError):
            if self._compressor is None:
                return self._unavailable_composition(token_budget)
            fallback = EvidenceComposer(
                tokenizer=self._tokenizer,
                max_excerpt_chars=self._max_excerpt_chars,
            )
            try:
                fallback_result = await asyncio.wait_for(
                    _COMPOSER_FALLBACK_WORKERS.run(
                        lambda: fallback.compose(
                            sources,
                            token_budget=token_budget,
                        )
                    ),
                    timeout=_DETERMINISTIC_COMPOSE_TIMEOUT_SECONDS,
                )
            except (asyncio.TimeoutError, BoundedWorkerBusyError):
                return self._unavailable_composition(token_budget)
            reasons = list(fallback_result.context.drop_reasons)
            self._append_reason(reasons, "COMPRESSOR_DEGRADED")
            return CompositionResult(
                items=fallback_result.items,
                context=replace(
                    fallback_result.context,
                    drop_reasons=tuple(reasons),
                ),
            )

    @staticmethod
    def _unavailable_composition(token_budget: int) -> CompositionResult:
        _validated_token_budget(token_budget)
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

    def compose(
        self,
        selected: Iterable[SelectedEvidence],
        *,
        token_budget: int,
    ) -> CompositionResult:
        """Render only supplied post-policy evidence under *token_budget*."""

        _validated_token_budget(token_budget)
        sources = tuple(selected)
        if not all(isinstance(source, SelectedEvidence) for source in sources):
            raise ValueError("selected must contain SelectedEvidence values")
        if not sources:
            context = ContextPackage(
                text="",
                citations=(),
                token_budget=token_budget,
                requested_tokens=0,
                selected_tokens=0,
                rendered_tokens=0,
                dropped_tokens=0,
                drop_reasons=(),
            )
            return CompositionResult(items=(), context=context)

        # The service has already applied policy, diversity, and any optional
        # reranker.  Preserve that authoritative order in the rendered
        # context.  Priority evidence is packed first only to reserve capacity;
        # selected items are resequenced before rendering below.
        ordered = sources
        requested_text = "\n\n".join(
            self._render_block(
                f"[E{index}]",
                source,
                self._full_excerpt(source),
            )
            for index, source in enumerate(ordered, 1)
        )
        requested_tokens = self._count(requested_text)
        priority = [source for source in ordered if source.priority]
        ordinary = [source for source in ordered if not source.priority]
        priority_reserve = min(
            token_budget,
            max(128, math.ceil(token_budget * 0.15)),
        )

        prepared: list[_PreparedItem] = []
        drop_reasons: list[str] = []
        deferred_priority: list[SelectedEvidence] = []
        for source in priority:
            item = self._fit(source, prepared, priority_reserve, drop_reasons)
            if item is None:
                deferred_priority.append(source)
            else:
                prepared.append(item)

        # The reservation is a floor, not a ceiling: unused total budget may
        # hold additional warnings before ordinary evidence is considered.
        for source in deferred_priority:
            item = self._fit(source, prepared, token_budget, drop_reasons)
            if item is not None:
                prepared.append(item)
            else:
                self._append_reason(drop_reasons, "TOKEN_BUDGET")
        for source in ordinary:
            item = self._fit(source, prepared, token_budget, drop_reasons)
            if item is not None:
                prepared.append(item)
            else:
                self._append_reason(drop_reasons, "TOKEN_BUDGET")

        source_order = {id(source): index for index, source in enumerate(ordered)}
        prepared.sort(key=lambda item: source_order[id(item.source)])
        prepared = self._resequence(prepared)

        text = "\n\n".join(item.block for item in prepared)
        rendered_tokens = self._count(text)
        items: list[EvidenceItem] = []
        citations: list[CitationEntry] = []
        cursor = 0
        for index, item in enumerate(prepared):
            if index:
                cursor += 2
            excerpt_start = cursor + item.block.index("\n") + 1
            excerpt_end = excerpt_start + len(item.excerpt)
            source = item.source
            relation_paths = self._relation_paths(source.candidate)
            evidence_item = EvidenceItem(
                citation=item.marker,
                excerpt=item.excerpt,
                category=self._clean(source.category),
                status=source.status,
                score=source.candidate.score,
                channels=source.candidate.channels,
                token_count=item.token_count,
                evidence_refs=source.candidate.evidence_refs,
                rationale=source.rationale,
                tags=source.tags,
                worked=source.worked,
                superseded_by_version_id=source.superseded_by_version_id,
                outcome=(
                    self._bounded(source.outcome)
                    if source.outcome is not None
                    else None
                ),
                outcome_failed=source.outcome_failed,
                procedure_steps=tuple(
                    self._bounded(step) for step in source.procedure_steps
                ),
                relation_path=relation_paths[0] if relation_paths else (),
                relation_paths=relation_paths,
            )
            items.append(evidence_item)
            citations.append(
                CitationEntry(
                    marker=item.marker,
                    evidence_refs=evidence_item.evidence_refs,
                    channels=evidence_item.channels,
                    excerpt_start=excerpt_start,
                    excerpt_end=excerpt_end,
                )
            )
            cursor += len(item.block)

        selected_tokens = rendered_tokens
        context = ContextPackage(
            text=text,
            citations=tuple(citations),
            token_budget=token_budget,
            requested_tokens=requested_tokens,
            selected_tokens=selected_tokens,
            rendered_tokens=rendered_tokens,
            dropped_tokens=max(0, requested_tokens - selected_tokens),
            drop_reasons=tuple(drop_reasons),
        )
        return CompositionResult(items=tuple(items), context=context)

    def _resequence(self, prepared: list[_PreparedItem]) -> list[_PreparedItem]:
        """Assign contiguous markers after restoring caller-selected order."""

        resequenced: list[_PreparedItem] = []
        for index, item in enumerate(prepared, 1):
            marker = f"[E{index}]"
            block = self._render_block(marker, item.source, item.excerpt)
            resequenced.append(
                replace(
                    item,
                    marker=marker,
                    block=block,
                    token_count=self._count(block),
                )
            )
        return resequenced

    def _fit(
        self,
        source: SelectedEvidence,
        prepared: list[_PreparedItem],
        budget: int,
        drop_reasons: list[str],
    ) -> _PreparedItem | None:
        marker = f"[E{len(prepared) + 1}]"
        full_excerpt = self._full_excerpt(source)
        excerpt = self._bounded(full_excerpt)
        truncated = excerpt != full_excerpt
        block = self._render_block(marker, source, excerpt)
        if self._combined_count(prepared, block) > budget:
            compressed = self._compress_excerpt(
                source,
                marker,
                excerpt,
                max_tokens=max(1, budget - self._combined_count(prepared, "")),
                drop_reasons=drop_reasons,
            )
            if compressed is not None:
                compressed_block = self._render_block(marker, source, compressed)
                if self._combined_count(prepared, compressed_block) <= budget:
                    excerpt = compressed
                    block = compressed_block
                    truncated = True
        if self._combined_count(prepared, block) > budget:
            excerpt = self._largest_fitting_excerpt(
                source,
                marker,
                excerpt,
                prepared,
                budget,
            )
            if excerpt is None:
                return None
            block = self._render_block(marker, source, excerpt)
            truncated = True
        if truncated:
            self._append_reason(drop_reasons, "ITEM_TRUNCATED")
        return _PreparedItem(
            source=source,
            marker=marker,
            excerpt=excerpt,
            block=block,
            token_count=self._count(block),
            truncated=truncated,
        )

    def _compress_excerpt(
        self,
        source: SelectedEvidence,
        marker: str,
        excerpt: str,
        *,
        max_tokens: int,
        drop_reasons: list[str],
    ) -> str | None:
        if self._compressor is None:
            return None
        protected = (marker,) + tuple(
            value
            for value in (
                source.outcome,
                *source.procedure_steps,
                *(
                    relation_id
                    for path in self._relation_paths(source.candidate)
                    for relation_id in path
                ),
            )
            if value
        )
        try:
            compressed = self._compressor.compress(
                excerpt,
                max_tokens=max_tokens,
                protected_tokens=protected,
            )
        except Exception:
            self._append_reason(drop_reasons, "COMPRESSOR_DEGRADED")
            return None
        if (
            not isinstance(compressed, str)
            or not compressed.strip()
            or _CITATION_PATTERN.search(compressed)
        ):
            self._append_reason(drop_reasons, "COMPRESSOR_DEGRADED")
            return None
        bounded = self._bounded(compressed)
        if bounded not in excerpt or any(
            token in excerpt and token not in bounded for token in protected
        ):
            self._append_reason(drop_reasons, "COMPRESSOR_DEGRADED")
            return None
        return bounded

    def _largest_fitting_excerpt(
        self,
        source: SelectedEvidence,
        marker: str,
        excerpt: str,
        prepared: list[_PreparedItem],
        budget: int,
    ) -> str | None:
        low = 1
        high = len(excerpt)
        best: str | None = None
        while low <= high:
            midpoint = (low + high) // 2
            candidate = excerpt[:midpoint].rstrip()
            if not candidate:
                candidate = excerpt[:1]
            block = self._render_block(marker, source, candidate)
            if self._combined_count(prepared, block) <= budget:
                best = candidate
                low = midpoint + 1
            else:
                high = midpoint - 1
        return best

    def _full_excerpt(self, source: SelectedEvidence) -> str:
        highlights = " ".join(
            self._clean(value) for value in source.candidate.highlights
        )
        content = self._clean(source.content)
        return f"{highlights} {content}".strip() if highlights else content

    def _render_block(
        self,
        marker: str,
        source: SelectedEvidence,
        excerpt: str,
    ) -> str:
        channels = ",".join(sorted(source.candidate.channels))
        lines = [
            (
                f"{marker} {self._clean(source.category)} {source.status} "
                f"channels={channels} score={source.candidate.score:.6f}"
            ),
            excerpt,
        ]
        if source.outcome is not None:
            outcome_label = "Failed outcome" if source.outcome_failed else "Outcome"
            lines.append(f"{outcome_label}: {self._bounded(source.outcome)}")
        if source.procedure_steps:
            lines.append(
                "Steps: "
                + " | ".join(self._bounded(step) for step in source.procedure_steps)
            )
        relation_paths = self._relation_paths(source.candidate)
        if relation_paths:
            lines.append(
                "Relations: " + " ; ".join(" > ".join(path) for path in relation_paths)
            )
        return "\n".join(lines)

    @staticmethod
    def _relation_paths(candidate: FusedCandidate) -> tuple[tuple[str, ...], ...]:
        return tuple(
            sorted(
                {
                    evidence.relation_path
                    for evidence in candidate.evidence_refs
                    if evidence.relation_path
                }
            )
        )

    def _combined_count(
        self,
        prepared: list[_PreparedItem],
        candidate_block: str,
    ) -> int:
        blocks = [item.block for item in prepared]
        if candidate_block:
            blocks.append(candidate_block)
        return self._count("\n\n".join(blocks))

    def _count(self, text: str) -> int:
        try:
            value = self._tokenizer.count_tokens(text)
        except Exception as exc:
            raise ValueError("tokenizer failed to count text") from exc
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("tokenizer must return a non-negative integer")
        return value

    def _bounded(self, value: str) -> str:
        cleaned = self._clean(value)
        if len(cleaned) <= self._max_excerpt_chars:
            return cleaned
        return cleaned[: self._max_excerpt_chars].rstrip()

    @staticmethod
    def _clean(value: str) -> str:
        return _normalize_evidence_text(value)

    @staticmethod
    def _append_reason(reasons: list[str], reason: str) -> None:
        if reason not in reasons:
            reasons.append(reason)


__all__ = [
    "CompositionResult",
    "EvidenceComposer",
    "ItemCompressor",
    "SelectedEvidence",
    "Tokenizer",
]
