"""Optional token-level MaxSim reranking of policy-approved evidence."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from ..bounded_workers import BoundedWorkerPool
from .composer import SelectedEvidence
from .types import FusedCandidate, RetrievalQuery

_LATE_INTERACTION_WORKERS = BoundedWorkerPool(
    max_workers=2,
    thread_name_prefix="daem0nmcp-late-interaction",
)


class LateInteractionReranker:
    """Lazily load one late-interaction model and run inference off-loop."""

    def __init__(
        self,
        *,
        model_name: str,
        model_factory: Callable[[str], object] | None = None,
        worker_pool: BoundedWorkerPool | None = None,
    ) -> None:
        if not isinstance(model_name, str) or not model_name.strip():
            raise ValueError("model_name must be a non-empty string")
        if model_factory is not None and not callable(model_factory):
            raise ValueError("model_factory must be callable")
        if worker_pool is not None and not isinstance(worker_pool, BoundedWorkerPool):
            raise ValueError("worker_pool must be a BoundedWorkerPool")
        self._model_name = model_name
        self._model_factory = model_factory
        self._model: Any = None
        self._model_lock = threading.Lock()
        self._worker_pool = worker_pool or _LATE_INTERACTION_WORKERS

    def _load_model(self) -> Any:
        with self._model_lock:
            if self._model is None:
                if self._model_factory is None:
                    from fastembed import LateInteractionTextEmbedding

                    self._model = LateInteractionTextEmbedding(
                        model_name=self._model_name,
                    )
                else:
                    self._model = self._model_factory(self._model_name)
            return self._model

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
        text: str,
        candidates: tuple[SelectedEvidence, ...],
    ) -> tuple[FusedCandidate, ...]:
        import numpy as np

        model = self._load_model()
        queries = list(model.query_embed([text]))
        documents = list(model.embed([candidate.content for candidate in candidates]))
        if len(queries) != 1 or len(documents) != len(candidates):
            raise RuntimeError("RERANKER_VECTOR_INVALID")
        query_tokens = _normalized_tokens(queries[0])
        scored: list[tuple[float, int]] = []
        for index, document in enumerate(documents):
            document_tokens = _normalized_tokens(document)
            if query_tokens.shape[1] != document_tokens.shape[1]:
                raise RuntimeError("RERANKER_VECTOR_INVALID")
            score = float((query_tokens @ document_tokens.T).max(axis=1).sum())
            if not np.isfinite(score):
                raise RuntimeError("RERANKER_VECTOR_INVALID")
            scored.append((score, index))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return tuple(candidates[index].candidate for _, index in scored)


def _normalized_tokens(value: object) -> Any:
    import numpy as np

    try:
        tokens = np.asarray(value, dtype=np.float64)
    except (OverflowError, TypeError, ValueError) as exc:
        raise RuntimeError("RERANKER_VECTOR_INVALID") from exc
    if tokens.ndim != 2 or 0 in tokens.shape or not np.isfinite(tokens).all():
        raise RuntimeError("RERANKER_VECTOR_INVALID")
    scales = np.max(np.abs(tokens), axis=1, keepdims=True)
    tokens = np.divide(tokens, scales, out=np.zeros_like(tokens), where=scales > 0)
    norms = np.linalg.norm(tokens, axis=1, keepdims=True)
    return np.divide(tokens, norms, out=tokens, where=norms > 0)


__all__ = ["LateInteractionReranker"]
