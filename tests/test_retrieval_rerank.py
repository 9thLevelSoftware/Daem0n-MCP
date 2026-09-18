"""Isolated production reranker contracts for v7 retrieval."""

from __future__ import annotations

import asyncio
import threading
import unittest

from tests.test_retrieval_composer import _source


class DeterministicEncoder:
    def __init__(self) -> None:
        self.thread_ids: list[int] = []

    def encode(self, text: str) -> list[float]:
        self.thread_ids.append(threading.get_ident())
        if text == "query":
            return [1.0, 0.0]
        if "best" in text:
            return [1.0, 0.0]
        return [0.0, 1.0]


class RetrievalRerankerTests(unittest.IsolatedAsyncioTestCase):
    async def test_embedding_reranker_runs_off_loop_and_returns_exact_permutation(self):
        from daem0nmcp.retrieval.rerank import EmbeddingSimilarityReranker
        from daem0nmcp.retrieval.types import RetrievalQuery

        encoder = DeterministicEncoder()
        original = (
            _source("1", "unrelated evidence", score=2.0),
            _source("2", "best semantic evidence", score=1.0),
        )
        event_loop_thread = threading.get_ident()

        result = await EmbeddingSimilarityReranker(encoder=encoder).rerank(
            RetrievalQuery(
                workspace_id="ws_0123456789abcdef01234567",
                text="query",
                rerank=True,
            ),
            original,
        )

        self.assertEqual(
            (original[1].candidate, original[0].candidate),
            result,
        )
        self.assertEqual(3, len(encoder.thread_ids))
        self.assertTrue(
            all(thread_id != event_loop_thread for thread_id in encoder.thread_ids)
        )

    async def test_cancelled_rerank_retains_worker_capacity_until_encoder_finishes(
        self,
    ):
        from daem0nmcp.bounded_workers import BoundedWorkerBusyError, BoundedWorkerPool
        from daem0nmcp.retrieval.rerank import EmbeddingSimilarityReranker
        from daem0nmcp.retrieval.types import RetrievalQuery

        started = threading.Event()
        release = threading.Event()

        class BlockingEncoder:
            def encode(self, _text: str) -> list[float]:
                started.set()
                release.wait(timeout=2)
                return [1.0]

        pool = BoundedWorkerPool(max_workers=1, thread_name_prefix="rerank-test")
        reranker = EmbeddingSimilarityReranker(
            encoder=BlockingEncoder(), worker_pool=pool
        )
        query = RetrievalQuery(
            workspace_id="ws_0123456789abcdef01234567",
            text="query",
            rerank=True,
        )
        task = asyncio.create_task(
            reranker.rerank(query, (_source("1", "evidence", score=1.0),))
        )
        for _ in range(1_000):
            if started.is_set():
                break
            await asyncio.sleep(0.001)
        self.assertTrue(started.is_set())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with self.assertRaises(BoundedWorkerBusyError):
            await reranker.rerank(query, (_source("2", "second evidence", score=1.0),))
        release.set()
        for _ in range(100):
            if pool.in_flight == 0:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(0, pool.in_flight)
        await asyncio.to_thread(pool.shutdown)

    async def test_real_service_accepts_adapter_permutation_and_reports_ready(self):
        from daem0nmcp.retrieval.rerank import EmbeddingSimilarityReranker
        from tests.test_retrieval_service import (
            CanonicalRepository,
            StaticProvider,
            _candidate,
            _provider_result,
            _query,
            _record_id,
            _service,
        )

        candidates = (
            _candidate("1", "lexical", 1),
            _candidate("2", "lexical", 2),
        )
        service = _service(
            providers={
                "lexical": StaticProvider(
                    "lexical",
                    _provider_result("lexical", *candidates),
                    [],
                )
            },
            repository=CanonicalRepository(
                contents={
                    _record_id("1"): "unrelated evidence",
                    _record_id("2"): "best semantic evidence",
                }
            ),
            reranker=EmbeddingSimilarityReranker(encoder=DeterministicEncoder()),
            rerank_enabled=True,
            rerank_candidate_limit=2,
        )

        result = await service.retrieve(
            _query(text="query", limit=2, candidate_limit=2, rerank=True)
        )

        self.assertFalse(result.abstained)
        self.assertEqual(
            (_record_id("2"), _record_id("1")),
            tuple(item.evidence_refs[0].record_id for item in result.items),
        )
        reranker = result.providers[-1]
        self.assertEqual("reranker", reranker.provider)
        self.assertEqual("ready", reranker.status)


if __name__ == "__main__":
    unittest.main()
