"""Dependency-free end-to-end coverage for the v7 retrieval runtime."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

WORKSPACE_ID = "ws_0123456789abcdef01234567"


def _apply_retrieval_schema(connection: sqlite3.Connection) -> None:
    from daem0nmcp.migrations.schema import MIGRATIONS

    connection.execute(
        "CREATE TABLE schema_version(version INTEGER PRIMARY KEY, applied_at TEXT)"
    )
    for version, _description, statements in MIGRATIONS:
        if version < 16 or version > 18:
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
        "rationale": "runtime integration evidence",
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


def _append(connection: sqlite3.Connection, suffix: str, content: str, at: int) -> str:
    from daem0nmcp.event_store import EventCommand, EventStore

    record_id = "mem_" + suffix * 64
    EventStore(connection).append_and_project(
        EventCommand(
            workspace_id=WORKSPACE_ID,
            stream_id=record_id,
            stream_kind="memory",
            event_type="memory.created",
            occurred_at_us=at,
            recorded_at_us=at,
            actor_type="system",
            payload={"record": _record(content)},
        )
    )
    return record_id


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        embedding_backend="torch",
        embedding_dimension=256,
        embedding_document_prefix="search_document: ",
        embedding_model="nomic-ai/modernbert-embed-base",
        embedding_query_prefix="search_query: ",
        qdrant_api_key=None,
        qdrant_collection_prefix="daem0nmcp",
        qdrant_path=None,
        qdrant_timeout_seconds=1.0,
        qdrant_url=None,
        retrieval_candidate_limit=50,
        retrieval_graph_max_branching=25,
        retrieval_graph_max_depth=2,
        retrieval_rerank_candidate_limit=25,
        retrieval_rerank_enabled=False,
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


class RetrievalRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.services = []
        self.path = Path(self.temporary.name) / "runtime.db"
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        _apply_retrieval_schema(connection)
        self.first_id = _append(connection, "1", "durable runtime baseline", 100)
        from daem0nmcp.retrieval.projections import LexicalProjectionBuilder

        LexicalProjectionBuilder(connection, clock_us=lambda: 200).rebuild(WORKSPACE_ID)
        # The fixture begins at a fully built checkpoint.  Cold-workspace queue
        # behavior is covered separately; scheduler tests add their own work.
        connection.execute("DELETE FROM background_jobs")
        connection.commit()
        connection.close()

    def tearDown(self) -> None:
        for service in self.services:
            service.close()
        self.temporary.cleanup()

    async def test_factory_runs_real_lexical_policy_repository_and_composer(self):
        from daem0nmcp.retrieval.runtime import create_retrieval_service
        from daem0nmcp.retrieval.types import RetrievalQuery

        service = create_retrieval_service(
            self.path,
            config=_settings(),
        )
        self.services.append(service)
        result = await service.retrieve(
            RetrievalQuery(
                workspace_id=WORKSPACE_ID,
                text="durable runtime",
                token_budget=200,
            )
        )

        self.assertFalse(result.abstained)
        self.assertEqual(self.first_id, result.items[0].evidence_refs[0].record_id)
        self.assertEqual("lexical", result.providers[0].provider)
        self.assertIn("durable runtime baseline", result.context.text)

    async def test_new_write_is_recalled_from_stale_active_lexical_delta(self):
        from daem0nmcp.retrieval.runtime import create_retrieval_service
        from daem0nmcp.retrieval.types import RetrievalQuery

        service = create_retrieval_service(self.path, config=_settings())
        self.services.append(service)
        baseline = RetrievalQuery(
            workspace_id=WORKSPACE_ID,
            text="durable runtime",
            token_budget=200,
        )
        self.assertFalse((await service.retrieve(baseline)).abstained)

        connection = sqlite3.connect(self.path)
        new_id = _append(connection, "2", "immediate lexical delta marker", 300)
        connection.commit()
        manifest = connection.execute(
            "SELECT source_event_count,row_count,details_json "
            "FROM projection_manifests WHERE workspace_id=? "
            "AND projection_name='lexical' AND status='active'",
            (WORKSPACE_ID,),
        ).fetchone()
        connection.close()

        result = await service.retrieve(
            RetrievalQuery(
                workspace_id=WORKSPACE_ID,
                text="immediate lexical delta marker",
                token_budget=200,
            )
        )

        self.assertFalse(result.abstained, result.reason)
        self.assertEqual(new_id, result.items[0].evidence_refs[0].record_id)
        self.assertEqual("degraded", result.providers[0].status)
        self.assertEqual((1, 2), tuple(manifest[:2]))
        self.assertIn("rebuild_required_event_id", json.loads(str(manifest[2])))

    async def test_factory_does_not_assemble_disabled_optional_profiles(self):
        from unittest.mock import patch

        from daem0nmcp.retrieval.runtime import create_retrieval_service

        with patch(
            "daem0nmcp.retrieval.runtime._embedding_encoder"
        ) as embedding_encoder:
            service = create_retrieval_service(
                self.path,
                config=_settings(),
                capability_statuses={
                    "local": "disabled",
                    "models-local": "disabled",
                    "graph": "disabled",
                },
            )
            self.services.append(service)

        self.assertNotIn("dense", service._providers)
        self.assertNotIn("graph", service._providers)
        embedding_encoder.assert_not_called()

    async def test_durable_lexical_job_is_drained_off_loop_and_refreshes_search(self):
        connection = sqlite3.connect(self.path)
        second_id = _append(connection, "2", "novel queued projection term", 300)
        connection.commit()
        connection.close()

        from daem0nmcp.retrieval.providers import LexicalProvider
        from daem0nmcp.retrieval.repository import sqlite_read_connection_factory
        from daem0nmcp.retrieval.runtime import drain_projection_jobs
        from daem0nmcp.retrieval.types import RetrievalQuery

        runs = await drain_projection_jobs(
            self.path,
            config=_settings(),
            max_jobs=1,
            include_optional=False,
        )
        self.assertEqual(1, len(runs))
        self.assertEqual("succeeded", runs[0].status)
        self.assertEqual(("lexical",), runs[0].projections)

        result = await LexicalProvider(
            connection_factory=sqlite_read_connection_factory(self.path)
        ).search(
            RetrievalQuery(workspace_id=WORKSPACE_ID, text="novel queued"),
            10,
        )
        self.assertEqual("ready", result.status)
        self.assertEqual(
            [second_id],
            [candidate.evidence.record_id for candidate in result.candidates],
        )

    async def test_owned_runtime_drain_resumes_due_dense_gc_after_restart(self):
        from unittest.mock import patch

        from daem0nmcp.event_store import canonical_json_bytes
        from daem0nmcp.migrations.schema import MIGRATIONS
        from daem0nmcp.retrieval.dense_generation_gc import (
            enqueue_inactive_generation_gc,
        )
        from daem0nmcp.retrieval.providers import dense_manifest_details
        from daem0nmcp.retrieval.runtime import (
            _ProjectionBuilderRegistry,
            drain_projection_jobs,
        )

        connection = sqlite3.connect(self.path)
        for version, _description, statements in MIGRATIONS:
            if version not in (31, 32):
                continue
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_version(version,applied_at) VALUES (?,'now')",
                (version,),
            )
        details = dense_manifest_details(
            workspace_id=WORKSPACE_ID,
            provider_key="qdrant",
            generation=1,
            model_id="runtime-model",
            dimension=3,
            collection_prefix="runtime",
        )
        collection_name = str(details["collection_name"])
        connection.execute(
            "INSERT INTO projection_manifests(manifest_id,workspace_id,projection_name,"
            "generation,projection_version,status,source_event_count,"
            "source_event_root_hash,cursor_recorded_at_us,cursor_event_id,row_count,"
            "builder_version,details_json,started_at_us,completed_at_us,activated_at_us) "
            "VALUES (?,?, 'dense',1,1,'ready',0,?,NULL,NULL,0,'runtime',?,1,1,NULL)",
            (
                "prj_" + "a" * 64,
                WORKSPACE_ID,
                "0" * 64,
                canonical_json_bytes(details).decode("utf-8"),
            ),
        )
        enqueue_inactive_generation_gc(
            connection,
            workspace_id=WORKSPACE_ID,
            provider_key="qdrant",
            now_us=1,
            retain_inactive=0,
        )
        connection.commit()
        connection.close()

        class Client:
            collections = {collection_name}
            collection_exists = None

            def get_collections(self):
                return SimpleNamespace(
                    collections=[
                        SimpleNamespace(name=name) for name in self.collections
                    ]
                )

            def delete_collection(self, name):
                self.collections.discard(name)

        class Dense:
            def _get_client(self):
                return client

        client = Client()
        builders = _ProjectionBuilderRegistry()
        builders["lexical"] = lambda _workspace_id: None
        builders.dense_builder = Dense()
        with patch(
            "daem0nmcp.retrieval.runtime.create_projection_builders",
            return_value=builders,
        ):
            await drain_projection_jobs(
                self.path,
                config=_settings(),
                max_jobs=1,
                include_optional=True,
            )

        connection = sqlite3.connect(self.path)
        try:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM dense_generation_gc_jobs"
                ).fetchone()[0],
            )
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM projection_manifests "
                    "WHERE projection_name='dense'"
                ).fetchone()[0],
            )
        finally:
            connection.close()
        self.assertNotIn(collection_name, client.collections)

    async def test_optional_drain_capacity_cannot_starve_core_lexical_refresh(self):
        from unittest.mock import patch

        from daem0nmcp.retrieval.runtime import drain_projection_jobs

        optional_started = threading.Event()
        release_optional = threading.Event()

        def fake_drain(
            _path,
            _config,
            _max_jobs,
            include_optional,
            _statuses,
            _cancellation_event,
        ):
            if include_optional:
                optional_started.set()
                release_optional.wait(timeout=2)
            return ()

        with patch(
            "daem0nmcp.retrieval.runtime._drain_projection_jobs_sync",
            side_effect=fake_drain,
        ):
            optional = asyncio.create_task(
                drain_projection_jobs(
                    self.path,
                    config=_settings(),
                    include_optional=True,
                )
            )
            await asyncio.to_thread(optional_started.wait, 2)
            try:
                core = await asyncio.wait_for(
                    drain_projection_jobs(
                        self.path,
                        config=_settings(),
                        include_optional=False,
                    ),
                    timeout=0.5,
                )
                self.assertEqual((), core)
            finally:
                release_optional.set()
                await optional

    async def test_legacy_date_filter_resolves_only_canonical_opaque_ids(self):
        connection = sqlite3.connect(self.path)
        second_id = _append(connection, "2", "newer filter target", 300)
        connection.commit()
        connection.close()

        from daem0nmcp.retrieval.runtime import resolve_legacy_record_filter

        selected = await resolve_legacy_record_filter(
            self.path,
            workspace_id=WORKSPACE_ID,
            since=datetime.fromtimestamp(0, timezone.utc).replace(microsecond=250),
        )
        self.assertEqual(frozenset({second_id}), selected)

    def test_include_warnings_does_not_narrow_an_unfiltered_legacy_recall(self):
        from daem0nmcp.retrieval.runtime import normalize_legacy_category_filter

        self.assertIsNone(normalize_legacy_category_filter(None, True))
        self.assertIsNone(normalize_legacy_category_filter([], True))
        self.assertEqual(
            frozenset({"decision", "warning"}),
            normalize_legacy_category_filter(["decision"], True),
        )
        self.assertEqual(
            frozenset({"decision"}),
            normalize_legacy_category_filter(["decision"], False),
        )
        with self.assertRaises(ValueError):
            normalize_legacy_category_filter(1, True)

    def test_configured_embedding_backend_never_silently_changes_contract(self):
        from unittest.mock import patch

        from daem0nmcp.retrieval.runtime import ConfiguredEmbeddingEncoder

        calls: list[dict[str, object]] = []
        fake = types.ModuleType("sentence_transformers")

        def sentence_transformer(_model_id, **kwargs):
            calls.append(kwargs)
            if "backend" in kwargs:
                raise RuntimeError("configured backend unavailable")
            return object()

        fake.SentenceTransformer = sentence_transformer
        previous = sys.modules.get("sentence_transformers")
        sys.modules["sentence_transformers"] = fake
        try:
            with tempfile.TemporaryDirectory() as temporary:
                Path(temporary, "model.bin").write_bytes(b"model")
                with (
                    patch(
                        "daem0nmcp.retrieval.onnx_encoder.load_pooled_onnx_model",
                        return_value=None,
                    ) as pooled_loader,
                    patch(
                        "huggingface_hub.snapshot_download",
                        return_value=temporary,
                    ),
                ):
                    encoder = ConfiguredEmbeddingEncoder(
                        model_id="test/model",
                        dimension=2,
                        prefix="query: ",
                        backend="onnx",
                    )
                    with self.assertRaisesRegex(
                        RuntimeError, "DENSE_ENCODER_UNAVAILABLE"
                    ):
                        encoder.encode("text")
                    pooled_loader.assert_called_once_with("test/model", 2)
        finally:
            if previous is None:
                sys.modules.pop("sentence_transformers", None)
            else:
                sys.modules["sentence_transformers"] = previous

        self.assertEqual(1, len(calls))
        self.assertEqual("onnx", calls[0]["backend"])

    def test_configured_onnx_encoder_uses_the_pooled_loader_and_closes_it(self):
        from unittest.mock import Mock, patch

        from daem0nmcp.retrieval.runtime import ConfiguredEmbeddingEncoder

        pooled_model = Mock()
        pooled_model.artifact_fingerprint = "a" * 64
        pooled_model.encode_many.return_value = [[3.0, 4.0]]
        with patch(
            "daem0nmcp.retrieval.onnx_encoder.load_pooled_onnx_model",
            return_value=pooled_model,
        ) as pooled_loader:
            encoder = ConfiguredEmbeddingEncoder(
                model_id="test/model",
                dimension=2,
                prefix="query: ",
                backend="onnx",
            )
            self.assertEqual([3.0, 4.0], encoder.encode("text"))
            encoder.close()

        pooled_loader.assert_called_once_with("test/model", 2)
        pooled_model.encode_many.assert_called_once_with(
            ["query: text"], convert_to_numpy=True
        )
        pooled_model.close.assert_called_once_with()

    def test_configured_batch_encoder_rejects_wrong_count_shape_and_nonfinite(self):
        from unittest.mock import Mock, patch

        from daem0nmcp.retrieval.runtime import ConfiguredEmbeddingEncoder

        for raw in ([[1.0, 2.0]], [[1.0], [2.0]], [[1.0, 2.0], [3.0, float("nan")]]):
            with self.subTest(raw=raw):
                pooled_model = Mock()
                pooled_model.artifact_fingerprint = "b" * 64
                pooled_model.encode_many.return_value = raw
                with patch(
                    "daem0nmcp.retrieval.onnx_encoder.load_pooled_onnx_model",
                    return_value=pooled_model,
                ):
                    encoder = ConfiguredEmbeddingEncoder(
                        model_id="test/model",
                        dimension=2,
                        prefix="document: ",
                        backend="onnx",
                    )
                    with self.assertRaisesRegex(RuntimeError, "DENSE_ENCODER_INVALID"):
                        encoder.encode_many(["first", "second"])

    def test_runtime_builds_distinct_query_and_document_encoder_contracts(self):
        from daem0nmcp.retrieval.runtime import _embedding_encoder

        query_encoder = _embedding_encoder(_settings(), "query")
        document_encoder = _embedding_encoder(_settings(), "document")

        self.assertEqual("search_query: ", query_encoder.prefix)
        self.assertEqual("search_document: ", document_encoder.prefix)
        self.assertEqual(query_encoder.model_id, document_encoder.model_id)
        self.assertEqual(query_encoder.dimension, document_encoder.dimension)

    def test_v6_hybrid_weight_adapter_emits_a_deprecation_warning(self):
        from daem0nmcp.retrieval.runtime import warn_legacy_hybrid_weight

        with self.assertWarnsRegex(
            DeprecationWarning,
            "hybrid_vector_weight is a v6-only compatibility setting",
        ):
            warn_legacy_hybrid_weight()

    async def test_optional_projection_drain_scheduler_coalesces_by_database(self):
        from unittest.mock import patch

        from daem0nmcp.retrieval.runtime import schedule_projection_job_drain

        started = __import__("asyncio").Event()
        release = __import__("asyncio").Event()
        calls: list[dict[str, object]] = []

        async def fake_drain(database_path, **kwargs):
            calls.append({"database_path": database_path, **kwargs})
            started.set()
            await release.wait()
            return ()

        with patch(
            "daem0nmcp.retrieval.runtime.drain_projection_jobs",
            new=fake_drain,
        ):
            first = schedule_projection_job_drain(
                self.path,
                config=_settings(),
                max_jobs=5,
            )
            await started.wait()
            second = schedule_projection_job_drain(
                self.path,
                config=_settings(),
                max_jobs=5,
            )
            self.assertIs(first, second)
            release.set()
            await first

            third = schedule_projection_job_drain(
                self.path,
                config=_settings(),
                max_jobs=5,
            )
            await third

        self.assertEqual(3, len(calls))
        self.assertTrue(all(call["include_optional"] for call in calls))
        self.assertTrue(all(call["max_jobs"] == 5 for call in calls))

    async def test_scheduler_cancellation_waits_for_projection_thread_cleanup(self):
        from unittest.mock import patch

        from daem0nmcp.retrieval.runtime import schedule_projection_job_drain

        started = threading.Event()
        cleanup_release = threading.Event()
        cleanup_finished = threading.Event()

        def blocking_drain(
            _path,
            _config,
            _max_jobs,
            _include_optional,
            _capability_statuses,
            cancellation_event,
        ):
            started.set()
            cancellation_event.wait(1.0)
            cleanup_release.wait(1.0)
            cleanup_finished.set()
            return ()

        with patch(
            "daem0nmcp.retrieval.runtime._drain_projection_jobs_sync",
            side_effect=blocking_drain,
        ):
            scheduled = schedule_projection_job_drain(
                self.path,
                config=_settings(),
                max_jobs=5,
            )
            self.assertTrue(await asyncio.to_thread(started.wait, 1.0))
            children = [
                task
                for task in asyncio.all_tasks()
                if task.get_name() == "daem0nmcp-projection-drain-slice"
            ]
            self.assertEqual(1, len(children))

            scheduled.cancel()
            children[0].cancel()
            for _ in range(20):
                await asyncio.sleep(0)
                if scheduled.done():
                    break
            scheduled_done_before_cleanup = scheduled.done()
            cleanup_done_before_release = cleanup_finished.is_set()
            cleanup_release.set()

            with self.assertRaises(asyncio.CancelledError):
                await scheduled
            self.assertFalse(scheduled_done_before_cleanup)
            self.assertFalse(cleanup_done_before_release)

        self.assertTrue(cleanup_finished.is_set())

    async def test_scheduler_cancellation_waits_for_deadline_check_cleanup(self):
        from unittest.mock import patch

        from daem0nmcp.retrieval.runtime import schedule_projection_job_drain

        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        async def empty_drain(_database_path, **_kwargs):
            return ()

        def blocking_deadline(_path):
            started.set()
            release.wait(1.0)
            finished.set()
            return None

        with (
            patch(
                "daem0nmcp.retrieval.runtime.drain_projection_jobs",
                new=empty_drain,
            ),
            patch(
                "daem0nmcp.retrieval.runtime._next_projection_job_deadline_sync",
                side_effect=blocking_deadline,
            ),
        ):
            scheduled = schedule_projection_job_drain(
                self.path,
                config=_settings(),
                max_jobs=5,
            )
            self.assertTrue(await asyncio.to_thread(started.wait, 1.0))
            scheduled.cancel()
            for _ in range(20):
                await asyncio.sleep(0)
                if scheduled.done():
                    break
            scheduled_done_before_cleanup = scheduled.done()
            cleanup_done_before_release = finished.is_set()
            release.set()

            with self.assertRaises(asyncio.CancelledError):
                await scheduled
            self.assertFalse(scheduled_done_before_cleanup)
            self.assertFalse(cleanup_done_before_release)

        self.assertTrue(finished.is_set())

    async def test_database_close_defers_repeated_cancellation_until_drain_cleanup(self):
        from unittest.mock import patch

        from daem0nmcp.database import DatabaseManager
        from daem0nmcp.retrieval.runtime import schedule_projection_job_drain

        started = asyncio.Event()
        cleanup_release = asyncio.Event()
        cleanup_finished = asyncio.Event()
        lock_released = asyncio.Event()

        async def blocking_drain(_database_path, **_kwargs):
            started.set()
            await cleanup_release.wait()
            cleanup_finished.set()
            return ()

        class RecordingLock:
            def release(self):
                lock_released.set()

        manager = object.__new__(DatabaseManager)
        manager._close_callbacks = []
        manager._foreground_projection_tasks = set()
        manager._defer_fresh_activation = False
        manager._engine = None
        manager._database_lock = RecordingLock()
        manager.format_version = 7
        manager.db_path = self.path

        with patch(
            "daem0nmcp.retrieval.runtime.drain_projection_jobs",
            new=blocking_drain,
        ):
            schedule_projection_job_drain(
                self.path,
                config=_settings(),
                max_jobs=5,
            )
            await started.wait()

            close_task = asyncio.create_task(manager.close())
            await asyncio.sleep(0)
            close_task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(lock_released.is_set())

            close_task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(lock_released.is_set())

            cleanup_release.set()
            with self.assertRaises(asyncio.CancelledError):
                await close_task

        self.assertTrue(cleanup_finished.is_set())
        self.assertTrue(lock_released.is_set())

    async def test_scheduled_drain_runs_additional_bounded_slices_until_empty(self):
        from unittest.mock import patch

        from daem0nmcp.retrieval.jobs import ProjectionJobRun
        from daem0nmcp.retrieval.runtime import schedule_projection_job_drain

        calls = 0

        async def fake_drain(_database_path, **_kwargs):
            nonlocal calls
            calls += 1
            count = 5 if calls == 1 else 1
            return tuple(
                ProjectionJobRun(
                    f"job_{calls}_{index}", WORKSPACE_ID, ("lexical",), "succeeded"
                )
                for index in range(count)
            )

        with (
            patch(
                "daem0nmcp.retrieval.runtime.drain_projection_jobs",
                new=fake_drain,
            ),
            patch(
                "daem0nmcp.retrieval.runtime._next_projection_job_deadline_sync",
                return_value=None,
            ),
        ):
            await schedule_projection_job_drain(
                self.path, config=_settings(), max_jobs=5
            )

        self.assertEqual(2, calls)

    async def test_continuous_owned_drain_polls_for_work_admitted_after_startup(self):
        from unittest.mock import patch

        from daem0nmcp.retrieval.runtime import (
            await_projection_job_drains,
            schedule_projection_job_drain,
        )

        calls = 0
        polled = asyncio.Event()

        async def fake_drain(_database_path, **_kwargs):
            nonlocal calls
            calls += 1
            if calls >= 2:
                polled.set()
            return ()

        with (
            patch(
                "daem0nmcp.retrieval.runtime.drain_projection_jobs",
                new=fake_drain,
            ),
            patch(
                "daem0nmcp.retrieval.runtime._next_projection_job_deadline_sync",
                return_value=None,
            ),
            patch(
                "daem0nmcp.retrieval.runtime._PROJECTION_IDLE_POLL_SECONDS",
                0.01,
            ),
        ):
            task = schedule_projection_job_drain(
                self.path,
                config=_settings(),
                max_jobs=5,
                continuous=True,
            )
            await asyncio.wait_for(polled.wait(), timeout=0.5)
            self.assertFalse(task.done())
            await await_projection_job_drains((self.path,))

        self.assertGreaterEqual(calls, 2)
        self.assertTrue(task.done())

    async def test_scheduled_drain_retries_transient_failure_with_capped_backoff(self):
        from unittest.mock import patch

        from daem0nmcp.retrieval.runtime import schedule_projection_job_drain

        calls = 0
        delays: list[float] = []

        async def fake_drain(_database_path, **_kwargs):
            nonlocal calls
            calls += 1
            if calls < 4:
                raise sqlite3.OperationalError("database is locked")
            return ()

        async def fake_wait(_state, delay):
            delays.append(delay)

        with (
            patch(
                "daem0nmcp.retrieval.runtime.drain_projection_jobs",
                new=fake_drain,
            ),
            patch(
                "daem0nmcp.retrieval.runtime._wait_for_projection_retry",
                new=fake_wait,
            ),
            patch(
                "daem0nmcp.retrieval.runtime._next_projection_job_deadline_sync",
                return_value=None,
            ),
        ):
            await schedule_projection_job_drain(
                self.path, config=_settings(), max_jobs=5
            )

        self.assertEqual(4, calls)
        self.assertEqual([0.05, 0.1, 0.2], delays)

    async def test_shutdown_interrupts_projection_retry_wait(self):
        from unittest.mock import patch

        from daem0nmcp.retrieval.runtime import (
            await_projection_job_drains,
            schedule_projection_job_drain,
        )

        failed = asyncio.Event()

        async def fake_drain(_database_path, **_kwargs):
            failed.set()
            raise OSError("temporary storage failure")

        with patch(
            "daem0nmcp.retrieval.runtime.drain_projection_jobs",
            new=fake_drain,
        ):
            task = schedule_projection_job_drain(
                self.path, config=_settings(), max_jobs=5
            )
            await failed.wait()
            await asyncio.wait_for(
                await_projection_job_drains((self.path,)), timeout=0.5
            )

        self.assertTrue(task.done())

    async def test_scheduled_drain_wakes_for_delayed_retry_without_another_write(self):
        from unittest.mock import patch

        from daem0nmcp.retrieval.jobs import ProjectionJobRun
        from daem0nmcp.retrieval.runtime import schedule_projection_job_drain

        calls = 0

        async def fake_drain(_database_path, **_kwargs):
            nonlocal calls
            calls += 1
            status = "queued" if calls == 1 else "succeeded"
            return (
                ProjectionJobRun("job_delayed", WORKSPACE_ID, ("lexical",), status),
            )

        deadlines = iter((time.time_ns() // 1_000 + 20_000, None))
        with (
            patch(
                "daem0nmcp.retrieval.runtime.drain_projection_jobs",
                new=fake_drain,
            ),
            patch(
                "daem0nmcp.retrieval.runtime._next_projection_job_deadline_sync",
                side_effect=lambda _path: next(deadlines),
            ),
        ):
            await schedule_projection_job_drain(
                self.path, config=_settings(), max_jobs=5
            )

        self.assertEqual(2, calls)

    async def test_isolated_write_delayed_rebuild_wakes_without_another_write(self):
        from unittest.mock import patch

        from daem0nmcp.retrieval.runtime import schedule_projection_job_drain

        recorded_at_us = time.time_ns() // 1_000
        connection = sqlite3.connect(self.path)
        try:
            with patch("daem0nmcp.event_store._PROJECTION_REBUILD_GRACE_US", 100_000):
                second_id = _append(
                    connection,
                    "3",
                    "isolated delayed wake target",
                    recorded_at_us,
                )
            connection.commit()
            queued = connection.execute(
                "SELECT status,available_at_us,created_at_us,updated_at_us "
                "FROM background_jobs "
                "WHERE workspace_id=? AND idempotency_key="
                "'active-projection:lexical'",
                (WORKSPACE_ID,),
            ).fetchone()
        finally:
            connection.close()

        self.assertEqual("queued", queued[0])
        self.assertEqual(recorded_at_us + 100_000, queued[1])
        self.assertEqual(recorded_at_us, queued[2])
        self.assertEqual(recorded_at_us, queued[3])

        task = schedule_projection_job_drain(
            self.path,
            config=_settings(),
            max_jobs=5,
            capability_statuses={
                "local": "disabled",
                "models-local": "disabled",
                "graph": "disabled",
            },
        )
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        await asyncio.wait_for(task, timeout=2)

        connection = sqlite3.connect(self.path)
        try:
            final = connection.execute(
                "SELECT status FROM background_jobs WHERE workspace_id=? "
                "AND idempotency_key='active-projection:lexical'",
                (WORKSPACE_ID,),
            ).fetchone()
            projected = connection.execute(
                "SELECT 1 FROM retrieval_documents WHERE workspace_id=? "
                "AND record_id=? AND projection_generation=(SELECT generation FROM "
                "projection_manifests WHERE workspace_id=? "
                "AND projection_name='lexical' AND status='active')",
                (WORKSPACE_ID, second_id, WORKSPACE_ID),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual("succeeded", final[0])
        self.assertIsNotNone(projected)

    async def test_shutdown_quiesces_delayed_work_and_startup_resumes_it(self):
        from unittest.mock import patch

        from daem0nmcp.retrieval.jobs import ProjectionJobRun
        from daem0nmcp.retrieval.runtime import (
            await_projection_job_drains,
            schedule_projection_job_drain,
        )

        calls = 0
        deadline_checked = threading.Event()

        async def fake_drain(_database_path, **_kwargs):
            nonlocal calls
            calls += 1
            status = "queued" if calls == 1 else "succeeded"
            return (
                ProjectionJobRun("job_restart", WORKSPACE_ID, ("lexical",), status),
            )

        def next_deadline(_path):
            deadline_checked.set()
            return None if calls > 1 else time.time_ns() // 1_000 + 10_000_000

        with (
            patch(
                "daem0nmcp.retrieval.runtime.drain_projection_jobs",
                new=fake_drain,
            ),
            patch(
                "daem0nmcp.retrieval.runtime._next_projection_job_deadline_sync",
                side_effect=next_deadline,
            ),
        ):
            first = schedule_projection_job_drain(
                self.path, config=_settings(), max_jobs=5
            )
            await asyncio.to_thread(deadline_checked.wait, 2)
            await await_projection_job_drains((self.path,))
            self.assertTrue(first.done())
            self.assertEqual(1, calls)

            second = schedule_projection_job_drain(
                self.path, config=_settings(), max_jobs=5
            )
            await second

        self.assertEqual(2, calls)


if __name__ == "__main__":
    unittest.main()
