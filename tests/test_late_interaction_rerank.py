"""Offline contracts for optional token-level late-interaction reranking."""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import threading
import unittest
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch

from daem0nmcp.retrieval.late_interaction import LateInteractionReranker
from daem0nmcp.retrieval.types import RetrievalQuery
from tests.test_retrieval_composer import _source
from tests.test_retrieval_runtime import (
    WORKSPACE_ID,
    _append,
    _apply_retrieval_schema,
    _settings,
)


class TokenModel:
    def __init__(self, documents: list[object]) -> None:
        self.documents = documents
        self.thread_ids: list[int] = []
        self.queries: list[object] = []
        self.contents: list[object] = []

    def query_embed(self, texts: list[str]):
        self.thread_ids.append(threading.get_ident())
        self.queries.append(texts)
        yield [[3.0, 0.0], [0.0, 5.0]]

    def embed(self, contents: list[str]):
        self.thread_ids.append(threading.get_ident())
        self.contents.append(contents)
        yield from self.documents


@unittest.skipUnless(find_spec("numpy"), "late-interaction numpy extra unavailable")
class LateInteractionTests(unittest.IsolatedAsyncioTestCase):
    def query(self) -> RetrievalQuery:
        return RetrievalQuery(
            workspace_id="ws_0123456789abcdef01234567",
            text="query",
            rerank=True,
        )

    async def test_maxsim_normalizes_tokens_preserves_ties_and_loads_once(self):
        model = TokenModel(
            [
                [[1.0, 0.0]],
                [[100.0, 0.0], [0.0, 0.01]],
                [[0.0, 1.0], [1.0, 0.0]],
            ]
        )
        loads: list[tuple[str, int]] = []

        def factory(name: str) -> TokenModel:
            loads.append((name, threading.get_ident()))
            return model

        reranker = LateInteractionReranker(model_name="fake", model_factory=factory)
        original = tuple(
            _source(str(index), f"document {index}", score=1.0) for index in range(1, 4)
        )
        self.assertEqual([], loads)
        self.assertEqual((), await reranker.rerank(self.query(), ()))
        self.assertEqual([], loads)
        loop_thread = threading.get_ident()
        results = await asyncio.gather(
            reranker.rerank(self.query(), original),
            reranker.rerank(self.query(), original),
        )
        expected = tuple(original[index].candidate for index in (1, 2, 0))
        self.assertEqual([expected, expected], results)
        self.assertEqual(1, len(loads))
        self.assertEqual("fake", loads[0][0])
        self.assertNotEqual(loop_thread, loads[0][1])
        self.assertTrue(all(t != loop_thread for t in model.thread_ids))
        self.assertEqual([["query"], ["query"]], model.queries)
        self.assertEqual([[item.content for item in original]] * 2, model.contents)

    async def test_invalid_token_matrices_and_counts_raise(self):
        original = (_source("1", "document", score=1.0),)
        for documents in (
            [[[float("nan"), 0.0]]],
            [[[float("inf"), 0.0]]],
            [[[1.0, 0.0, 0.0]]],
            [[1.0, 0.0]],
            [[]],
            [],
        ):
            with self.subTest(documents=documents):
                model = TokenModel(documents)
                reranker = LateInteractionReranker(
                    model_name="fake", model_factory=lambda _, model=model: model
                )
                with self.assertRaisesRegex(RuntimeError, "RERANKER_VECTOR_INVALID"):
                    await reranker.rerank(self.query(), original)

    async def test_invalid_query_tokens_raise(self):
        class InvalidQueryModel(TokenModel):
            def query_embed(self, texts: list[str]):
                yield [[float("nan"), 0.0]]

        reranker = LateInteractionReranker(
            model_name="fake",
            model_factory=lambda _: InvalidQueryModel([[[1.0, 0.0]]]),
        )
        with self.assertRaisesRegex(RuntimeError, "RERANKER_VECTOR_INVALID"):
            await reranker.rerank(self.query(), (_source("1", "document", score=1.0),))


class LateInteractionFactoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from daem0nmcp.retrieval.projections import LexicalProjectionBuilder

        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "memory.db"
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        _apply_retrieval_schema(connection)
        _append(connection, "1", "query first document", 100)
        _append(connection, "2", "query second document", 200)
        LexicalProjectionBuilder(connection, clock_us=lambda: 300).rebuild(WORKSPACE_ID)
        connection.commit()
        connection.close()
        self.services = []

    def tearDown(self) -> None:
        for service in self.services:
            service.close()
        self.temporary.cleanup()

    async def test_missing_profile_never_falls_back_to_ready_embedding(self):
        from daem0nmcp.retrieval.runtime import create_retrieval_service

        class OfflineEncoder:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def encode(self, text: str) -> list[float]:
                self.calls.append(text)
                return [1.0, 0.0]

        config = _settings()
        config.retrieval_rerank_enabled = True
        config.retrieval_reranker = "late_interaction"
        for status in ("disabled", "degraded", "failed"):
            encoder = OfflineEncoder()
            with (
                self.subTest(status=status),
                patch(
                    "daem0nmcp.retrieval.runtime._embedding_encoder",
                    return_value=encoder,
                ),
            ):
                service = create_retrieval_service(
                    self.path,
                    config=config,
                    capability_statuses={
                        "local": "ready",
                        "models-local": "ready",
                        "graph": "disabled",
                        "late-interaction": status,
                    },
                )
                self.services.append(service)
                result = await service.retrieve(
                    RetrievalQuery(workspace_id=WORKSPACE_ID, text="query", rerank=True)
                )
                self.assertFalse(result.abstained)
                diagnostic = next(
                    item for item in result.providers if item.provider == "reranker"
                )
                self.assertEqual("unavailable", diagnostic.status)
                self.assertEqual("RERANKER_UNAVAILABLE", diagnostic.reason)
                self.assertEqual([], encoder.calls)

    @unittest.skipUnless(
        find_spec("numpy") and find_spec("fastembed"),
        "late-interaction extra unavailable",
    )
    async def test_ready_profile_reranks_real_recall_and_loads_only_when_requested(
        self,
    ):
        from daem0nmcp.retrieval.runtime import create_retrieval_service

        config = _settings()
        config.retrieval_rerank_enabled = True
        config.retrieval_reranker = "late_interaction"
        model = TokenModel([[[1.0, 0.0]], [[1.0, 0.0], [0.0, 1.0]]])
        loads: list[str] = []

        def factory(*, model_name: str) -> TokenModel:
            loads.append(model_name)
            return model

        with patch("fastembed.LateInteractionTextEmbedding", side_effect=factory):
            service = create_retrieval_service(
                self.path,
                config=config,
                capability_statuses={
                    "local": "disabled",
                    "models-local": "disabled",
                    "graph": "disabled",
                    "late-interaction": "ready",
                },
            )
            self.services.append(service)
            baseline = await service.retrieve(
                RetrievalQuery(workspace_id=WORKSPACE_ID, text="query", rerank=False)
            )
            self.assertFalse(baseline.abstained)
            self.assertEqual([], loads)
            result = await service.retrieve(
                RetrievalQuery(workspace_id=WORKSPACE_ID, text="query", rerank=True)
            )
            self.assertFalse(result.abstained)
            self.assertEqual(["colbert-ir/colbertv2.0"], loads)
            self.assertEqual(
                tuple(
                    item.evidence_refs[0].record_id for item in reversed(baseline.items)
                ),
                tuple(item.evidence_refs[0].record_id for item in result.items),
            )
            diagnostic = next(
                item for item in result.providers if item.provider == "reranker"
            )
            self.assertEqual("ready", diagnostic.status)
