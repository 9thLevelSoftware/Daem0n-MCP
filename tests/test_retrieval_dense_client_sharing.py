"""One embedded Qdrant client per storage folder, shared and released once."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import math
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

WORKSPACE_ID = "ws_0123456789abcdef01234567"
MODEL_ID = "shared-client-test-model"
STATUSES = {"local": "ready", "models-local": "ready"}


class _EmbeddedFolders:
    """Embedded-Qdrant double: persisted per folder, one live client per folder."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.collections: dict[str, dict[str, dict[str, dict[str, object]]]] = {}
        self.live: dict[str, _EmbeddedClient] = {}
        self.opened: list[_EmbeddedClient] = []
        self.foreign: set[str] = set()
        self.attempts = 0
        self.call_seconds = 0.0
        self.before_count: Callable[[], None] | None = None

    def open(self, **kwargs: object) -> _EmbeddedClient:
        if set(kwargs) != {"path"}:
            raise AssertionError(f"unexpected local client options: {sorted(kwargs)}")
        path = os.path.normcase(os.path.realpath(str(kwargs["path"])))
        with self._lock:
            self.attempts += 1
            if path in self.live or path in self.foreign:
                raise RuntimeError(
                    f"Storage folder {path} is already accessed by another "
                    "instance of Qdrant client. If you require concurrent access, "
                    "use Qdrant server instead."
                )
            client = _EmbeddedClient(self, path)
            self.live[path] = client
            self.opened.append(client)
            return client


class _EmbeddedClient:
    """Thread-affine and non-reentrant, like the embedded SQLite persistence."""

    def __init__(self, folders: _EmbeddedFolders, path: str) -> None:
        self._folders = folders
        self._guard = threading.Lock()
        self._active = 0
        self.path = path
        self.thread = threading.get_ident()
        self.close_count = 0
        self.overlapped = False
        self.foreign_thread_calls = 0
        self.closed_while_active = False
        self.mutations = 0
        self.block_queries: threading.Event | None = None
        self.query_started = threading.Event()

    @contextmanager
    def _call(self):
        with self._guard:
            if self.close_count:
                raise RuntimeError("embedded client is closed")
            if threading.get_ident() != self.thread:
                self.foreign_thread_calls += 1
            self._active += 1
            if self._active > 1:
                self.overlapped = True
        try:
            if self._folders.call_seconds:
                time.sleep(self._folders.call_seconds)
            yield self._folders.collections.setdefault(self.path, {})
        finally:
            with self._guard:
                self._active -= 1

    def collection_exists(self, collection_name: str) -> bool:
        with self._call() as collections:
            return collection_name in collections

    def create_collection(
        self, *, collection_name: str, vectors_config: object
    ) -> None:
        del vectors_config
        with self._call() as collections:
            self.mutations += 1
            collections[collection_name] = {}

    def delete_collection(self, collection_name: str) -> None:
        with self._call() as collections:
            self.mutations += 1
            collections.pop(collection_name, None)

    def upsert(
        self, *, collection_name: str, points: list[dict[str, object]], wait: bool
    ) -> None:
        del wait
        with self._call() as collections:
            self.mutations += 1
            for point in points:
                collections[collection_name][str(point["id"])] = {
                    "id": point["id"],
                    "payload": dict(point["payload"]),  # type: ignore[call-overload]
                    "vector": list(point["vector"]),  # type: ignore[call-overload]
                }

    def retrieve(
        self,
        *,
        collection_name: str,
        ids: list[str],
        with_payload: bool,
        with_vectors: bool,
    ) -> list[dict[str, object]]:
        del with_payload, with_vectors
        with self._call() as collections:
            stored = collections[collection_name]
            return [copy.deepcopy(stored[key]) for key in ids if key in stored]

    def count(self, *, collection_name: str, exact: bool) -> dict[str, int]:
        del exact
        with self._call() as collections:
            if self._folders.before_count is not None:
                self._folders.before_count()
            return {"count": len(collections[collection_name])}

    def query_points(
        self,
        *,
        collection_name: str,
        query: list[float],
        limit: int,
        with_payload: bool,
        with_vectors: bool,
    ) -> SimpleNamespace:
        del with_payload, with_vectors
        with self._call() as collections:
            self.query_started.set()
            if self.block_queries is not None:
                self.block_queries.wait(timeout=10)
            norm = math.hypot(*query) or 1.0
            ranked = []
            for point in collections[collection_name].values():
                vector = point["vector"]
                assert isinstance(vector, list)
                size = math.hypot(*vector) or 1.0
                score = sum(a * b for a, b in zip(query, vector, strict=True))
                ranked.append(
                    {
                        "id": point["id"],
                        "payload": copy.deepcopy(point["payload"]),
                        "score": score / (norm * size),
                    }
                )
            ranked.sort(key=lambda item: (-item["score"], str(item["id"])))
            return SimpleNamespace(points=ranked[:limit])

    def close(self) -> None:
        with self._guard:
            if threading.get_ident() != self.thread:
                self.foreign_thread_calls += 1
            if self._active:
                self.closed_while_active = True
            self.close_count += 1
        with self._folders._lock:
            if self._folders.live.get(self.path) is self:
                del self._folders.live[self.path]


def _open(folders: _EmbeddedFolders, path: object, **options: object) -> object:
    from daem0nmcp.retrieval.providers import create_qdrant_client

    return create_qdrant_client(
        qdrant_url=None,
        qdrant_api_key=None,
        qdrant_path=path,  # type: ignore[arg-type]
        timeout_seconds=5.0,
        client_factory=options.get("client_factory", folders.open),  # type: ignore[arg-type]
    )


class SharedLocalClientTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name) / "qdrant"
        self.folders = _EmbeddedFolders()

    def test_local_folder_is_opened_once_and_closed_with_its_last_user(self):
        first = _open(self.folders, self.folder)
        second = _open(self.folders, self.folder)
        self.assertEqual(1, len(self.folders.opened))
        client = self.folders.opened[0]

        first.create_collection(collection_name="generation-1", vectors_config=None)
        self.assertTrue(second.collection_exists("generation-1"))

        first.close()
        first.close()
        self.assertEqual(0, client.close_count)
        self.assertEqual(
            {"count": 0}, second.count(collection_name="generation-1", exact=True)
        )
        with self.assertRaises(RuntimeError):
            first.collection_exists("generation-1")

        second.close()
        self.assertEqual(1, client.close_count)

        reopened = _open(self.folders, self.folder)
        self.addCleanup(reopened.close)
        self.assertEqual(2, len(self.folders.opened))
        self.assertTrue(reopened.collection_exists("generation-1"))
        reopened.close()
        self.assertEqual([1, 1], [item.close_count for item in self.folders.opened])
        self.assertEqual(
            [0, 0], [item.foreign_thread_calls for item in self.folders.opened]
        )

    def test_equivalent_folder_spellings_share_and_other_folders_do_not(self):
        first = _open(self.folders, self.folder)
        self.addCleanup(first.close)
        same = _open(self.folders, os.path.join(str(self.folder), ""))
        self.addCleanup(same.close)
        nested = _open(self.folders, self.folder / "child" / os.pardir)
        self.addCleanup(nested.close)
        other = _open(self.folders, self.folder.with_name("other-qdrant"))
        self.addCleanup(other.close)

        self.assertEqual(2, len(self.folders.opened))
        first.create_collection(collection_name="shared", vectors_config=None)
        self.assertTrue(same.collection_exists("shared"))
        self.assertTrue(nested.collection_exists("shared"))
        self.assertFalse(other.collection_exists("shared"))

    def test_remote_clients_are_created_per_caller_and_never_shared(self):
        from daem0nmcp.retrieval.providers import create_qdrant_client

        created: list[dict[str, object]] = []

        def factory(**kwargs: object) -> dict[str, object]:
            created.append(kwargs)
            return created[-1]

        clients = [
            create_qdrant_client(
                qdrant_url="https://qdrant.example.test",
                qdrant_api_key=None,
                qdrant_path=self.folder,
                timeout_seconds=2.0,
                client_factory=factory,
            )
            for _ in range(2)
        ]

        self.assertEqual(2, len(created))
        self.assertIs(created[0], clients[0])
        self.assertIs(created[1], clients[1])
        self.assertEqual(0, self.folders.attempts)

    def test_concurrent_users_share_one_client_and_never_overlap_calls(self):
        anchor = _open(self.folders, self.folder)
        anchor.create_collection(collection_name="shared", vectors_config=None)
        self.folders.call_seconds = 0.002
        start = threading.Barrier(8)
        failures: list[BaseException] = []

        def use(index: int) -> None:
            try:
                start.wait(timeout=30)
                handle = _open(self.folders, self.folder)
                try:
                    for step in range(5):
                        handle.upsert(
                            collection_name="shared",
                            points=[
                                {
                                    "id": f"{index}-{step}",
                                    "payload": {},
                                    "vector": [1.0, float(index), float(step)],
                                }
                            ],
                            wait=True,
                        )
                        handle.query_points(
                            collection_name="shared",
                            query=[1.0, 0.0, 0.0],
                            limit=3,
                            with_payload=True,
                            with_vectors=False,
                        )
                finally:
                    handle.close()
            except BaseException as exc:
                failures.append(exc)

        threads = [threading.Thread(target=use, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.folders.call_seconds = 0.0

        self.assertEqual([], failures)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(1, len(self.folders.opened))
        client = self.folders.opened[0]
        self.assertEqual(
            {"count": 40}, anchor.count(collection_name="shared", exact=True)
        )
        self.assertFalse(client.overlapped)
        self.assertEqual(0, client.foreign_thread_calls)
        self.assertEqual(0, client.close_count)
        anchor.close()
        self.assertEqual(1, client.close_count)
        self.assertEqual(0, client.foreign_thread_calls)

    def test_last_release_waits_for_a_query_still_running_on_another_thread(self):
        handle = _open(self.folders, self.folder)
        other = _open(self.folders, self.folder)
        handle.create_collection(collection_name="shared", vectors_config=None)
        client = self.folders.opened[0]
        client.block_queries = threading.Event()
        results: list[object] = []

        def query() -> None:
            results.append(
                handle.query_points(
                    collection_name="shared",
                    query=[1.0, 0.0, 0.0],
                    limit=1,
                    with_payload=True,
                    with_vectors=False,
                )
            )

        querying = threading.Thread(target=query)
        querying.start()
        self.assertTrue(client.query_started.wait(timeout=30))

        other.close()
        self.assertEqual(0, client.close_count)
        closing = threading.Thread(target=handle.close)
        closing.start()
        closing.join(timeout=0.2)
        self.assertTrue(closing.is_alive())
        self.assertEqual(0, client.close_count)

        client.block_queries.set()
        querying.join(timeout=30)
        closing.join(timeout=30)
        self.assertFalse(querying.is_alive() or closing.is_alive())
        self.assertEqual(1, len(results))
        self.assertEqual(1, client.close_count)
        self.assertFalse(client.closed_while_active)

    def test_folder_held_elsewhere_is_reported_and_probed_with_backoff(self):
        from daem0nmcp.retrieval import providers

        clock = [1_000.0]
        self.folders.foreign.add(os.path.normcase(os.path.realpath(self.folder)))
        with patch.object(providers, "_monotonic", lambda: clock[0]):
            with self.assertRaises(providers.QdrantStorageLockedError) as raised:
                _open(self.folders, self.folder)
            self.assertEqual("DENSE_STORAGE_LOCKED", raised.exception.code)
            self.assertEqual(1, self.folders.attempts)

            with self.assertRaises(providers.QdrantStorageLockedError):
                _open(self.folders, self.folder)
            self.assertEqual(1, self.folders.attempts)

            clock[0] += 1.5
            with self.assertRaises(providers.QdrantStorageLockedError):
                _open(self.folders, self.folder)
            self.assertEqual(2, self.folders.attempts)
            clock[0] += 1.5
            with self.assertRaises(providers.QdrantStorageLockedError):
                _open(self.folders, self.folder)
            self.assertEqual(2, self.folders.attempts)

            self.folders.foreign.clear()
            clock[0] += 3_600.0
            handle = _open(self.folders, self.folder)
            self.assertEqual(3, self.folders.attempts)
            handle.close()

            self.folders.foreign.add(os.path.normcase(os.path.realpath(self.folder)))
            with self.assertRaises(providers.QdrantStorageLockedError):
                _open(self.folders, self.folder)
            clock[0] += 1.5
            self.folders.foreign.clear()
            _open(self.folders, self.folder).close()
        self.assertEqual([1, 1], [item.close_count for item in self.folders.opened])

    def test_unrelated_open_failures_keep_their_own_error(self):
        calls: list[dict[str, object]] = []

        def broken(**kwargs: object) -> object:
            calls.append(kwargs)
            raise ModuleNotFoundError("optional qdrant_client is absent")

        for _ in range(2):
            with self.assertRaises(ModuleNotFoundError):
                _open(self.folders, self.folder, client_factory=broken)
        self.assertEqual([{"path": str(self.folder)}] * 2, calls)
        handle = _open(self.folders, self.folder)
        self.addCleanup(handle.close)
        self.assertFalse(handle.collection_exists("absent"))
        self.assertEqual(1, len(self.folders.opened))


def _apply_schema(connection: sqlite3.Connection, versions: tuple[int, ...]) -> None:
    from daem0nmcp.migrations.schema import MIGRATIONS

    connection.execute(
        "CREATE TABLE schema_version(version INTEGER PRIMARY KEY, applied_at TEXT)"
    )
    for version, _description, statements in MIGRATIONS:
        if version not in versions:
            continue
        for statement in statements:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_version(version,applied_at) VALUES (?,'now')",
            (version,),
        )
    connection.commit()


def _record(content: str) -> dict[str, object]:
    return {
        "record_type": "decision",
        "legacy_type": None,
        "content": content,
        "rationale": "shared client evidence",
        "context": {},
        "tags": ["runtime"],
        "file_path": None,
        "file_path_relative": None,
        "keywords": None,
        "is_permanent": False,
        "pinned": False,
        "archived": False,
        "outcome": None,
        "worked": None,
        "recall_count": 0,
        "surprise_score": None,
        "importance_score": None,
        "source_client": "test",
        "source_model": None,
        "deleted_at_us": None,
    }


class _Encoder:
    """Deterministic encoder that carries the configured embedding contract."""

    encoded: list[tuple[str, str]] = []

    def __init__(self, prefix: str) -> None:
        self.model_id = MODEL_ID
        self.dimension = 3
        self.prefix = prefix
        self.backend = "test-backend"
        self.artifact_fingerprint = hashlib.sha256(b"shared-client-test").hexdigest()

    def prepare_artifact_identity(self) -> str:
        return self.artifact_fingerprint

    def encode(self, text: str) -> list[float]:
        self.encoded.append((self.prefix, text))
        return [
            float(len(text)),
            float(sum(text.encode("utf-8")) % 997) + 1.0,
            float(text.count(" ") + 1),
        ]


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        embedding_backend="test-backend",
        embedding_dimension=3,
        embedding_document_prefix="search_document: ",
        embedding_model=MODEL_ID,
        embedding_query_prefix="search_query: ",
        qdrant_api_key=None,
        qdrant_collection_prefix="daem0nmcp",
        qdrant_path=None,
        qdrant_timeout_seconds=30.0,
        qdrant_url=None,
        retrieval_candidate_limit=50,
        retrieval_graph_max_branching=25,
        retrieval_graph_max_depth=2,
        retrieval_rerank_candidate_limit=25,
        retrieval_rerank_enabled=False,
        retrieval_reranker="embedding",
        retrieval_late_interaction_model="colbert-ir/colbertv2.0",
        retrieval_utility_mode="shadow",
        retrieval_utility_weight=0.1,
        retrieval_utility_credit="trace",
        retrieval_utility_candidate_limit=25,
        memory_validity_mode="shadow",
        retrieval_retention_mode="shadow",
        retrieval_rrf_weights={
            "lexical": 1.0,
            "dense": 1.0,
            "graph": 0.7,
            "temporal": 0.85,
            "procedure": 0.8,
            "outcome": 0.9,
        },
        rrf_k=60,
    )


_FIND_SPEC = importlib.util.find_spec


def _qdrant_dependency_probe(name: str, package: str | None = None) -> object:
    """Answer the builder's optional-dependency probe without the local extra."""

    if name == "qdrant_client":
        return importlib.util.spec_from_loader(name, loader=None)
    return _FIND_SPEC(name, package)


class _DenseRuntimeCase(unittest.IsolatedAsyncioTestCase):
    """A v7 database driven through the production retrieval assembly."""

    schema_versions: tuple[int, ...] = (16, 17, 18, 31, 32, 33)

    def setUp(self) -> None:
        from daem0nmcp.retrieval import runtime

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "runtime.db"
        self.folder = self.path.parent / "qdrant"
        self.connection = sqlite3.connect(self.path)
        self.addCleanup(self.connection.close)
        self.connection.execute("PRAGMA foreign_keys=ON")
        _apply_schema(self.connection, self.schema_versions)
        _Encoder.encoded = []
        self.config = _settings()
        self._clock = 1_000
        self.monotonic = [1_000.0]
        for patcher in (
            patch.object(
                runtime,
                "_embedding_encoder",
                lambda config, purpose: _Encoder(
                    config.embedding_query_prefix
                    if purpose == "query"
                    else config.embedding_document_prefix
                ),
            ),
            patch(
                "daem0nmcp.retrieval.providers._monotonic",
                lambda: self.monotonic[0],
                create=True,
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _append(self, suffix: str, content: str) -> str:
        from daem0nmcp.event_store import EventCommand, EventStore

        self._clock += 100
        record_id = "mem_" + suffix * 64
        EventStore(self.connection).append_and_project(
            EventCommand(
                workspace_id=WORKSPACE_ID,
                stream_id=record_id,
                stream_kind="memory",
                event_type="memory.created",
                occurred_at_us=self._clock,
                recorded_at_us=self._clock,
                actor_type="system",
                payload={"record": _record(content)},
            )
        )
        self.connection.commit()
        return record_id

    def _enqueue(self, *projections: str) -> None:
        from daem0nmcp.retrieval.job_queue import enqueue_projection_rebuild

        for projection in projections:
            enqueue_projection_rebuild(
                self.connection,
                workspace_id=WORKSPACE_ID,
                projection_name=projection,
                source_event_id=None,
                recorded_at_us=self._clock,
            )
        self.connection.commit()

    async def _drain(self, max_jobs: int = 5) -> dict[str, tuple[str, str | None]]:
        from daem0nmcp.retrieval.runtime import drain_projection_jobs

        self.connection.execute(
            "UPDATE background_jobs SET available_at_us=0 WHERE status='queued'"
        )
        self.connection.execute(
            "UPDATE dense_generation_gc_jobs SET available_at_us=0 "
            "WHERE status='queued'"
        )
        self.connection.commit()
        self.monotonic[0] += 3_600.0
        runs = await drain_projection_jobs(
            self.path,
            config=self.config,
            max_jobs=max_jobs,
            include_optional=True,
            capability_statuses=STATUSES,
        )
        return {run.projections[0]: (run.status, run.reason) for run in runs}

    def _job(self, projection: str) -> tuple[str, int, str | None]:
        row = self.connection.execute(
            "SELECT status,attempts,json_extract(last_error_json,'$.code') "
            "FROM background_jobs WHERE idempotency_key=?",
            (f"active-projection:{projection}",),
        ).fetchone()
        return str(row[0]), int(row[1]), row[2]

    def _generations(self) -> list[tuple[int, str]]:
        return [
            (int(row[0]), str(row[1]))
            for row in self.connection.execute(
                "SELECT generation,status FROM projection_manifests "
                "WHERE projection_name='dense' ORDER BY generation"
            )
        ]

    def _service(self):
        from daem0nmcp.retrieval.runtime import create_retrieval_service

        service = create_retrieval_service(
            self.path, config=self.config, capability_statuses=STATUSES
        )
        self.addCleanup(service.close)
        return service

    async def _dense(self, service, text: str = "durable dense evidence"):
        from daem0nmcp.retrieval.types import RetrievalQuery

        result = await service.retrieve(
            RetrievalQuery(workspace_id=WORKSPACE_ID, text=text, token_budget=400)
        )
        diagnostic = next(item for item in result.providers if item.provider == "dense")
        return diagnostic.status, diagnostic.reason, diagnostic.returned_count


class _EmbeddedFolderRuntimeCase(_DenseRuntimeCase):
    """The runtime assembly over the folder-exclusive Qdrant double."""

    def setUp(self) -> None:
        super().setUp()
        self.folders = _EmbeddedFolders()
        for patcher in (
            patch(
                "daem0nmcp.retrieval.providers._default_qdrant_client_factory",
                self.folders.open,
            ),
            patch(
                "daem0nmcp.retrieval.dense_projection.importlib.util.find_spec",
                _qdrant_dependency_probe,
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.folder_key = os.path.normcase(os.path.realpath(self.folder))


class SharedLocalClientRuntimeTests(_EmbeddedFolderRuntimeCase):
    async def test_rebuild_and_gc_reuse_the_client_held_by_dense_recall(self):
        self._append("1", "durable dense evidence one")
        self._enqueue("lexical", "dense")
        self.assertEqual(
            {"lexical": ("succeeded", None), "dense": ("succeeded", None)},
            await self._drain(),
        )
        self.assertEqual([1], [item.close_count for item in self.folders.opened])

        service = self._service()
        self.assertEqual(("ready", None, 1), await self._dense(service))
        self.assertEqual(2, len(self.folders.opened))
        recall_client = self.folders.opened[1]

        self._append("2", "durable dense evidence two")
        self.assertEqual(("succeeded", None), (await self._drain())["dense"])
        self.assertEqual(("succeeded", 1, None), self._job("dense"))
        self.assertEqual(("ready", None, 2), await self._dense(service))

        self._append("3", "durable dense evidence three")
        self.assertEqual(("succeeded", None), (await self._drain())["dense"])
        self.assertEqual([(2, "ready"), (3, "active")], self._generations())
        self.assertEqual(
            0,
            self.connection.execute(
                "SELECT COUNT(*) FROM dense_generation_gc_jobs"
            ).fetchone()[0],
        )
        self.assertEqual(2, len(self.folders.collections[self.folder_key]))
        self.assertEqual(("ready", None, 3), await self._dense(service))

        self.assertEqual(2, len(self.folders.opened))
        self.assertEqual(0, recall_client.close_count)
        service.close()
        self.assertEqual(1, recall_client.close_count)
        self.assertFalse(recall_client.overlapped)
        self.assertFalse(recall_client.closed_while_active)
        self.assertEqual(
            [0, 0], [item.foreign_thread_calls for item in self.folders.opened]
        )
        self.assertEqual({}, self.folders.live)

    async def test_folder_held_by_another_process_defers_dense_rebuilds(self):
        self._append("1", "durable dense evidence one")
        self._enqueue("lexical", "dense")
        await self._drain()
        self.folders.foreign.add(self.folder_key)

        service = self._service()
        self.assertEqual(
            ("degraded", "DENSE_STORAGE_LOCKED", 0), await self._dense(service)
        )
        self._append("2", "durable dense evidence two")
        for _ in range(5):
            runs = await self._drain()
            self.assertEqual(("queued", "DENSE_STORAGE_LOCKED"), runs["dense"])
            self.assertEqual(("queued", 0, "DENSE_STORAGE_LOCKED"), self._job("dense"))
            self.assertGreater(
                self.connection.execute(
                    "SELECT available_at_us FROM background_jobs "
                    "WHERE idempotency_key='active-projection:dense'"
                ).fetchone()[0],
                time.time_ns() // 1_000,
            )
        self.assertEqual([(1, "active")], self._generations())
        self.assertEqual(
            0,
            self.connection.execute(
                "SELECT COUNT(*) FROM dense_projection_refs "
                "WHERE projection_generation<>1"
            ).fetchone()[0],
        )

        self.folders.foreign.clear()
        self.assertEqual(("succeeded", None), (await self._drain())["dense"])
        self.assertEqual(("succeeded", 1, None), self._job("dense"))
        self.assertEqual(("ready", None, 2), await self._dense(service))

    async def test_manual_rebuild_reports_a_folder_held_by_another_process(self):
        from daem0nmcp.retrieval.operations import (
            ProjectionOperationError,
            rebuild_projection,
        )
        from daem0nmcp.retrieval.runtime import create_projection_builders

        self._append("1", "durable dense evidence one")
        self._enqueue("lexical", "dense")
        await self._drain()
        self._append("2", "durable dense evidence two")
        self.folders.foreign.add(self.folder_key)

        builders = create_projection_builders(
            self.connection,
            self.path,
            config=self.config,
            capability_statuses=STATUSES,
        )
        self.addCleanup(builders.close)
        with self.assertRaises(ProjectionOperationError) as raised:
            rebuild_projection(
                self.connection,
                workspace_id=WORKSPACE_ID,
                projection="dense",
                builders=builders,
            )
        self.assertEqual("DENSE_STORAGE_LOCKED", raised.exception.code)
        self.assertEqual([(1, "active")], self._generations())

        self.folders.foreign.clear()
        self.monotonic[0] += 3_600.0
        result = rebuild_projection(
            self.connection,
            workspace_id=WORKSPACE_ID,
            projection="dense",
            builders=builders,
        )
        self.assertEqual((2, "active"), (result["generation"], result["status"]))

    async def test_folder_held_by_another_process_defers_generation_gc(self):
        from daem0nmcp.event_store import canonical_json_bytes
        from daem0nmcp.retrieval.dense_generation_gc import (
            enqueue_inactive_generation_gc,
        )
        from daem0nmcp.retrieval.providers import dense_manifest_details

        self._append("1", "durable dense evidence one")
        self._enqueue("lexical", "dense")
        await self._drain()
        details = dense_manifest_details(
            workspace_id=WORKSPACE_ID,
            provider_key="qdrant",
            generation=7,
            model_id=MODEL_ID,
            dimension=3,
            collection_prefix="daem0nmcp",
        )
        self.connection.execute(
            "INSERT INTO projection_manifests(manifest_id,workspace_id,"
            "projection_name,generation,projection_version,status,"
            "source_event_count,source_event_root_hash,cursor_recorded_at_us,"
            "cursor_event_id,row_count,builder_version,details_json,started_at_us,"
            "completed_at_us,activated_at_us) VALUES (?,?,'dense',7,1,'ready',0,?,"
            "NULL,NULL,0,'runtime',?,1,1,NULL)",
            (
                "prj_" + "a" * 64,
                WORKSPACE_ID,
                "0" * 64,
                canonical_json_bytes(details).decode("utf-8"),
            ),
        )
        enqueue_inactive_generation_gc(
            self.connection,
            workspace_id=WORKSPACE_ID,
            provider_key="qdrant",
            now_us=1,
            retain_inactive=0,
        )
        self.connection.commit()
        self.folders.foreign.add(self.folder_key)

        self.assertEqual({}, await self._drain())
        row = self.connection.execute(
            "SELECT status,attempts,last_error_code,available_at_us "
            "FROM dense_generation_gc_jobs"
        ).fetchone()
        self.assertEqual(
            ("queued", 0, "DENSE_GENERATION_GC_STORAGE_LOCKED"), tuple(row[:3])
        )
        self.assertGreater(row[3], time.time_ns() // 1_000)

        self.folders.foreign.clear()
        await self._drain()
        self.assertEqual(
            0,
            self.connection.execute(
                "SELECT COUNT(*) FROM dense_generation_gc_jobs"
            ).fetchone()[0],
        )
        self.assertEqual([(1, "active")], self._generations())


class EmbeddedQdrantTests(_DenseRuntimeCase):
    """The same sequences against the real embedded Qdrant client."""

    def setUp(self) -> None:
        pytest.importorskip("qdrant_client")
        from qdrant_client.local.persistence import CollectionPersistence

        super().setUp()
        # SQLite builds that are not fully serialized bind every collection's
        # persistence to the thread that opened it.
        patcher = patch.object(CollectionPersistence, "CHECK_SAME_THREAD", True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _collection_directories(self) -> list[str]:
        root = self.folder / "collection"
        return sorted(item.name for item in root.iterdir()) if root.is_dir() else []

    async def test_recall_rebuild_and_gc_run_on_one_embedded_client(self):
        from qdrant_client import QdrantClient

        self._append("1", "durable dense evidence one")
        self._enqueue("lexical", "dense")
        self.assertEqual(("succeeded", None), (await self._drain())["dense"])
        service = self._service()
        self.assertEqual(("ready", None, 1), await self._dense(service))

        self._append("2", "durable dense evidence two")
        self.assertEqual(("succeeded", None), (await self._drain())["dense"])
        self.assertEqual(("succeeded", 1, None), self._job("dense"))
        self.assertEqual(("ready", None, 2), await self._dense(service))

        self._append("3", "durable dense evidence three")
        self.assertEqual(("succeeded", None), (await self._drain())["dense"])
        self.assertEqual([(2, "ready"), (3, "active")], self._generations())
        self.assertEqual(2, len(self._collection_directories()))
        self.assertEqual(("ready", None, 3), await self._dense(service))

        service.close()
        released = QdrantClient(path=str(self.folder))
        try:
            self.assertEqual(2, len(released.get_collections().collections))
        finally:
            released.close()
        shutil.rmtree(self.folder)
        self.assertFalse(self.folder.exists())

    async def test_folder_locked_by_a_foreign_client_is_retried_not_dead_lettered(
        self,
    ):
        from qdrant_client import QdrantClient

        self._append("1", "durable dense evidence one")
        self._enqueue("lexical", "dense")
        await self._drain()
        foreign = QdrantClient(path=str(self.folder))
        self.addCleanup(foreign.close)

        service = self._service()
        self.assertEqual(
            ("degraded", "DENSE_STORAGE_LOCKED", 0), await self._dense(service)
        )
        self._append("2", "durable dense evidence two")
        for _ in range(4):
            self.assertEqual(
                ("queued", "DENSE_STORAGE_LOCKED"), (await self._drain())["dense"]
            )
            self.assertEqual(("queued", 0, "DENSE_STORAGE_LOCKED"), self._job("dense"))

        foreign.close()
        self.assertEqual(("succeeded", None), (await self._drain())["dense"])
        self.assertEqual(("ready", None, 2), await self._dense(service))

    def test_shared_handle_deletion_closes_collection_persistence_first(self):
        from qdrant_client import models
        from qdrant_client.local.local_collection import LocalCollection

        from daem0nmcp.retrieval.providers import (
            create_qdrant_client,
            qdrant_delete_collection,
        )

        handles = [
            create_qdrant_client(
                qdrant_url=None,
                qdrant_api_key=None,
                qdrant_path=self.folder,
                timeout_seconds=5.0,
            )
            for _ in range(2)
        ]
        closed: list[str] = []
        original_close = LocalCollection.close

        def recording_close(collection: LocalCollection) -> None:
            closed.append(Path(str(collection.storage.location)).parent.name)
            original_close(collection)

        try:
            handles[0].create_collection(
                collection_name="retired",
                vectors_config=models.VectorParams(
                    size=3, distance=models.Distance.COSINE
                ),
            )
            handles[0].upsert(
                collection_name="retired",
                points=[models.PointStruct(id=1, vector=[1.0, 0.0, 0.0])],
                wait=True,
            )
            with patch.object(LocalCollection, "close", recording_close):
                qdrant_delete_collection(handles[1], "retired")
            self.assertEqual(["retired"], closed)
            self.assertFalse(handles[0].collection_exists("retired"))
            self.assertEqual([], self._collection_directories())
        finally:
            for handle in handles:
                handle.close()
        shutil.rmtree(self.folder)
        self.assertFalse(self.folder.exists())


if __name__ == "__main__":
    unittest.main()
