"""Bounded optional reranking over already policy-approved evidence."""

from __future__ import annotations

import math
from collections.abc import Sequence

from ..bounded_workers import BoundedWorkerPool
from .composer import SelectedEvidence
from .types import FusedCandidate, RetrievalQuery

_RERANK_WORKERS = BoundedWorkerPool(
    max_workers=2,
    thread_name_prefix="daem0nmcp-retrieval-rerank",
)


class EmbeddingSimilarityReranker:
    """Reorder an exact evidence tuple using isolated embedding similarity."""

    def __init__(
        self,
        *,
        encoder: object | None = None,
        query_encoder: object | None = None,
        document_encoder: object | None = None,
        worker_pool: BoundedWorkerPool | None = None,
    ) -> None:
        if encoder is not None:
            if query_encoder is not None or document_encoder is not None:
                raise ValueError(
                    "encoder cannot be combined with query/document encoders"
                )
            query_encoder = encoder
            document_encoder = encoder
        for encoder, field_name in (
            (query_encoder, "query_encoder"),
            (document_encoder, "document_encoder"),
        ):
            if not callable(getattr(encoder, "encode", None)):
                raise ValueError(f"{field_name} must provide encode")
        if worker_pool is not None and not isinstance(worker_pool, BoundedWorkerPool):
            raise ValueError("worker_pool must be a BoundedWorkerPool")
        self._query_encoder = query_encoder
        self._document_encoder = document_encoder
        self._worker_pool = worker_pool or _RERANK_WORKERS

    async def rerank(
        self,
        query: RetrievalQuery,
        candidates: tuple[SelectedEvidence, ...],
    ) -> tuple[FusedCandidate, ...]:
        if not isinstance(query, RetrievalQuery):
            raise ValueError("query must be a RetrievalQuery")
        if not isinstance(candidates, tuple) or not all(
            isinstance(candidate, SelectedEvidence) for candidate in candidates
        ):
            raise ValueError("candidates must contain SelectedEvidence")
        if not candidates:
            return ()
        return await self._worker_pool.run(
            lambda: self._rerank_sync(query.text, candidates)
        )

    def _rerank_sync(
        self,
        query_text: str,
        candidates: tuple[SelectedEvidence, ...],
    ) -> tuple[FusedCandidate, ...]:
        query_vector = _vector(self._query_encoder.encode(query_text))
        scored: list[tuple[float, int, SelectedEvidence]] = []
        for index, candidate in enumerate(candidates):
            document_vector = _vector(self._document_encoder.encode(candidate.content))
            if len(document_vector) != len(query_vector):
                raise RuntimeError("RERANKER_VECTOR_INVALID")
            score = _cosine_similarity(query_vector, document_vector)
            scored.append((score, index, candidate))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return tuple(item[2].candidate for item in scored)


def _vector(value: object) -> tuple[float, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise RuntimeError("RERANKER_VECTOR_INVALID")
    try:
        vector = tuple(float(component) for component in value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise RuntimeError("RERANKER_VECTOR_INVALID") from exc
    if not vector or not all(math.isfinite(component) for component in vector):
        raise RuntimeError("RERANKER_VECTOR_INVALID")
    return vector


def _cosine_similarity(
    left: tuple[float, ...],
    right: tuple[float, ...],
) -> float:
    left_norm = math.sqrt(sum(component * component for component in left))
    right_norm = math.sqrt(sum(component * component for component in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return -1.0
    score = sum(a * b for a, b in zip(left, right, strict=True)) / (
        left_norm * right_norm
    )
    if not math.isfinite(score):
        raise RuntimeError("RERANKER_VECTOR_INVALID")
    return score


__all__ = ["EmbeddingSimilarityReranker"]
