from __future__ import annotations

import asyncio
import inspect
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import patch

from daem0nmcp.api.v7.application import AdmittedRequest

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
PREFLIGHT_TOKEN = "t" * 32
CURSOR_SECRET = b"record-operation-cursor-secret-32!"


def _apply_v7_schema(connection: sqlite3.Connection) -> None:
    from daem0nmcp.migrations.schema import MIGRATIONS
    from daem0nmcp.schema_version import CURRENT_SCHEMA_VERSION

    connection.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
    for version in range(16, CURRENT_SCHEMA_VERSION + 1):
        migration = next(item for item in MIGRATIONS if item[0] == version)
        for statement in migration[2]:
            connection.execute(statement)
        connection.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))
    connection.commit()


def _request(tool_name: str, **arguments: object) -> AdmittedRequest:
    from daem0nmcp.api.v7.tools import TOOL_INPUT_MODELS

    model = TOOL_INPUT_MODELS[tool_name].model_validate(arguments)
    effective = model.model_dump(mode="python")
    effective.pop("preflight_token", None)
    return AdmittedRequest(tool_name, MappingProxyType(effective))


class RecordOperationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from daem0nmcp.storage_activation import (
            ActiveDatabasePointer,
            write_active_pointer,
        )
        from daem0nmcp.workspace import WorkspaceRegistry

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.storage = self.root / ".daem0nmcp" / "storage"
        self.storage.mkdir(parents=True)
        self.database = self.storage / "daem0nmcp.db"
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            _apply_v7_schema(connection)
        write_active_pointer(
            self.storage,
            ActiveDatabasePointer(7, 1, self.database.name, None, None),
        )
        self.workspace = WorkspaceRegistry([self.root], default_root=self.root).default
        self.scheduler_observations: list[tuple[Path, int]] = []

    def _schedule(self, path: Path) -> None:
        with closing(sqlite3.connect(path)) as connection:
            count = int(
                connection.execute("SELECT count(*) FROM memory_events").fetchone()[0]
            )
        self.scheduler_observations.append((Path(path), count))

    def _operations(self, **changes: object):
        from daem0nmcp.api.v7.record_operations import (
            RecordOperationDependencies,
            build_record_operations,
        )

        options = {
            "clock": lambda: NOW,
            "projection_scheduler": self._schedule,
            "poll_interval_seconds": 0.01,
        }
        options.update(changes)
        return build_record_operations(RecordOperationDependencies(**options))

    async def test_canonical_batch_namespace_keeps_legacy_replays_and_separates_new_receipts(
        self,
    ) -> None:
        """The extension must not invalidate pre-extension batch receipts."""
        from daem0nmcp.api.v7.record_operations import (
            CanonicalBatchStoreRequest,
            RecordOperationDependencies,
            store_canonical_batch,
        )

        legacy = self._batch_request()
        operation = self._operations()["memory_store_batch"]
        first = await operation(workspace=self.workspace, request=legacy)
        replay = await operation(workspace=self.workspace, request=legacy)
        self.assertTrue(replay.idempotent_replay)
        self.assertEqual(first.event_ids, replay.event_ids)

        dependencies = RecordOperationDependencies(
            clock=lambda: NOW,
            projection_scheduler=self._schedule,
        )
        request = CanonicalBatchStoreRequest(
            records=({"record_type": "learning", "content": "Pinned provenance."},),
            idempotency_key="document-ingest-0001",
            semantic_namespace="document-ingest-url",
            provenance={"url": "https://example.test/a", "content_hash": "a" * 64},
        )
        namespaced = await store_canonical_batch(
            dependencies, workspace=self.workspace, request=request
        )
        namespaced_replay = await store_canonical_batch(
            dependencies, workspace=self.workspace, request=request
        )
        self.assertFalse(namespaced.idempotent_replay)
        self.assertTrue(namespaced_replay.idempotent_replay)
        self.assertEqual(namespaced.event_ids, namespaced_replay.event_ids)

    async def test_extracted_batch_seam_replays_frozen_legacy_receipt_byte_for_byte(
        self,
    ) -> None:
        """The public seam must preserve the cc08b4f legacy receipt contract."""
        from daem0nmcp.api.v7.pinned import IdempotencyConflict
        from daem0nmcp.api.v7.record_operations import (
            CanonicalBatchStoreRequest,
            RecordOperationDependencies,
            store_canonical_batch,
        )
        from daem0nmcp.workspace import Workspace

        workspace = Workspace("ws_" + "a" * 24, self.root)
        records = (
            {
                "record_type": "learning",
                "content": "Frozen legacy receipt.",
                "tags": ["legacy"],
            },
        )
        admitted = _request(
            "memory_store_batch",
            workspace_id=workspace.workspace_id,
            records=list(records),
            idempotency_key="record-batch-golden-0001",
            preflight_token=PREFLIGHT_TOKEN,
        )
        legacy_operation = self._operations()["memory_store_batch"]
        with (
            patch(
                "daem0nmcp.api.v7.record_operations.WorkspaceRegistry",
                return_value=SimpleNamespace(default=workspace),
            ),
            patch(
                "daem0nmcp.api.v7.runtime_services.WorkspaceRegistry",
                return_value=SimpleNamespace(default=workspace),
            ),
        ):
            legacy = await legacy_operation(workspace=workspace, request=admitted)

        request = CanonicalBatchStoreRequest(
            records=records,
            idempotency_key="record-batch-golden-0001",
        )
        with patch(
            "daem0nmcp.api.v7.runtime_services.WorkspaceRegistry",
            return_value=SimpleNamespace(default=workspace),
        ):
            replay = await store_canonical_batch(
                RecordOperationDependencies(clock=lambda: NOW),
                workspace=workspace,
                request=request,
            )

        self.assertTrue(replay.idempotent_replay)
        self.assertEqual(
            legacy.event_ids,
            ["evt_57525b6768d9b92cf07720951c7e9107dcb76f787daf298e1b657f9dddb122b7"],
        )
        self.assertEqual(replay.event_ids, legacy.event_ids)
        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                "SELECT stream_id,correlation_id,payload_json,payload_hash,event_hash "
                "FROM memory_events"
            ).fetchone()
        self.assertEqual(
            row,
            (
                "mem_c14193dfb685cdbaeaacf03b7b5e2300fd0d2beb3d58e17b0f1b6693548617ca",
                "job_8ff19a5f50a9c1a59e9c15d2d6d84db5993661b82ee36579da3f8c5d897caf12",
                '{"batch_index":0,"batch_size":1,"idempotency_request_hash":"c0a0d841029c7cdb331d21a7cd2f85a51245ad668eeaddd4bd1658fc8e455e2a","record":{"archived":false,"content":"Frozen legacy receipt.","context":{},"deleted_at_us":null,"file_path":null,"file_path_relative":null,"importance_score":null,"is_permanent":false,"keywords":null,"legacy_type":null,"outcome":null,"pinned":false,"rationale":null,"recall_count":0,"record_type":"learning","source_client":null,"source_model":null,"surprise_score":null,"tags":["legacy"],"worked":null}}',
                "be5b02d99c68aabef0a668d9978e75871af5a6df0abfa8d86ce063d009e5db55",
                "57525b6768d9b92cf07720951c7e9107dcb76f787daf298e1b657f9dddb122b7",
            ),
        )

        conflicting = CanonicalBatchStoreRequest(
            records=(
                {
                    "record_type": "learning",
                    "content": "Rebound legacy receipt.",
                },
            ),
            idempotency_key="record-batch-golden-0001",
        )
        with (
            patch(
                "daem0nmcp.api.v7.runtime_services.WorkspaceRegistry",
                return_value=SimpleNamespace(default=workspace),
            ),
            self.assertRaises(IdempotencyConflict),
        ):
            await store_canonical_batch(
                RecordOperationDependencies(clock=lambda: NOW),
                workspace=workspace,
                request=conflicting,
            )
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                connection.execute("SELECT count(*) FROM memory_events").fetchone()[0],
                1,
            )

    def test_dependencies_require_a_32_byte_cursor_secret(self) -> None:
        """Weak or ambiguously typed cursor keys must fail during composition."""
        from daem0nmcp.api.v7.record_operations import RecordOperationDependencies

        with self.assertRaisesRegex(ValueError, "at least 32 bytes"):
            RecordOperationDependencies(cursor_secret=b"x" * 31)
        with self.assertRaisesRegex(ValueError, "at least 32 bytes"):
            RecordOperationDependencies(
                cursor_secret="x" * 32  # type: ignore[arg-type]
            )

        dependencies = RecordOperationDependencies(cursor_secret=b"x" * 32)
        self.assertEqual(b"x" * 32, dependencies.cursor_secret)

    def _workspace_with_cloned_record(self, event_id: str):
        """Build an authorized second workspace containing the cursor row."""
        from daem0nmcp.storage_activation import (
            ActiveDatabasePointer,
            write_active_pointer,
        )
        from daem0nmcp.workspace import WorkspaceRegistry

        other_root = self.root / "other-workspace"
        other_storage = other_root / ".daem0nmcp" / "storage"
        other_storage.mkdir(parents=True)
        other_database = other_storage / "daem0nmcp.db"
        other_workspace = WorkspaceRegistry(
            [other_root], default_root=other_root
        ).default
        with (
            closing(sqlite3.connect(self.database)) as source,
            closing(sqlite3.connect(other_database)) as destination,
        ):
            destination.execute("PRAGMA foreign_keys=ON")
            _apply_v7_schema(destination)
            for table, predicate in (
                ("memory_events", "event_id=?"),
                ("memory_records", "source_event_id=?"),
            ):
                query = source.execute(
                    f"SELECT * FROM {table} WHERE {predicate}",
                    (event_id,),
                )
                columns = [str(item[0]) for item in query.description]
                values = list(query.fetchone())
                values[columns.index("workspace_id")] = other_workspace.workspace_id
                placeholders = ",".join("?" for _ in columns)
                destination.execute(
                    f"INSERT INTO {table} ({','.join(columns)}) "
                    f"VALUES ({placeholders})",
                    values,
                )
            destination.commit()
        write_active_pointer(
            other_storage,
            ActiveDatabasePointer(7, 1, other_database.name, None, None),
        )
        return other_workspace

    def _batch_request(
        self,
        *,
        key: str = "record-batch-0001",
        records: list[dict[str, object]] | None = None,
    ) -> AdmittedRequest:
        if records is None:
            records = [
                {
                    "record_type": "decision",
                    "content": "Use one canonical event stream.",
                    "rationale": "Replay-safe mutations are easier to operate.",
                    "context": {"component": "runtime"},
                    "tags": ["v7", "events"],
                    "relative_file_path": "daem0nmcp/runtime.py",
                    "happened_at": NOW,
                },
                {
                    "record_type": "warning",
                    "content": "Never bypass the active generation lock.",
                    "context": {},
                    "tags": ["v7"],
                    "relative_file_path": "daem0nmcp/runtime.py",
                    "happened_at": NOW + timedelta(seconds=1),
                },
            ]
        return _request(
            "memory_store_batch",
            workspace_id=self.workspace.workspace_id,
            records=records,
            idempotency_key=key,
            preflight_token=PREFLIGHT_TOKEN,
        )

    async def _store_default_batch(self):
        return await self._operations()["memory_store_batch"](
            workspace=self.workspace,
            request=self._batch_request(),
        )

    async def test_registry_is_exact_immutable_and_keyword_only(self) -> None:
        """A positional or mutable adapter registry could bypass root wiring."""
        operations = self._operations()

        self.assertEqual(
            {
                "session_updates_get",
                "memory_recall_file",
                "memory_search_text",
                "memory_store_batch",
                "memory_pin_set",
                "memory_archive_set",
            },
            set(operations),
        )
        with self.assertRaises(TypeError):
            operations["extra"] = object()
        for operation in operations.values():
            parameters = tuple(inspect.signature(operation).parameters.values())
            self.assertEqual(
                ("workspace", "request"), tuple(p.name for p in parameters)
            )
            self.assertTrue(
                all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in parameters)
            )

    async def test_batch_is_atomic_replay_safe_and_scheduled_after_commit(self) -> None:
        """Partial batch commits or key rebinding would corrupt canonical history."""
        from daem0nmcp.api.v7.pinned import IdempotencyConflict
        from daem0nmcp.api.v7.tools import MemoryStoreBatchData

        operation = self._operations()["memory_store_batch"]
        first = await operation(
            workspace=self.workspace,
            request=self._batch_request(),
        )

        self.assertIsInstance(first, MemoryStoreBatchData)
        self.assertFalse(first.idempotent_replay)
        self.assertEqual(2, len(first.records))
        self.assertEqual(2, len(first.event_ids))
        self.assertEqual([(self.database, 2)], self.scheduler_observations)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                (2, 2),
                (
                    connection.execute("SELECT count(*) FROM memory_events").fetchone()[
                        0
                    ],
                    connection.execute(
                        "SELECT count(*) FROM memory_records"
                    ).fetchone()[0],
                ),
            )

        replay = await operation(
            workspace=self.workspace,
            request=self._batch_request(),
        )
        self.assertTrue(replay.idempotent_replay)
        self.assertEqual(first.event_ids, replay.event_ids)
        self.assertEqual(
            [item.record_id for item in first.records],
            [item.record_id for item in replay.records],
        )
        self.assertEqual([(self.database, 2)], self.scheduler_observations)

        rebound = self._batch_request(
            records=[
                {
                    "record_type": "decision",
                    "content": "The same key now names different content.",
                }
            ]
        )
        with self.assertRaises(IdempotencyConflict):
            await operation(workspace=self.workspace, request=rebound)

        invalid = self._batch_request(
            key="record-batch-invalid-0002",
            records=[
                {
                    "record_type": "learning",
                    "content": "This first append must be rolled back.",
                },
                {
                    "record_type": "procedure",
                    "content": "Conflicting procedure context.",
                    "context": {"steps": ["different"]},
                    "procedure_steps": ["expected"],
                },
            ],
        )
        with self.assertRaisesRegex(Exception, "INVALID_ARGUMENT"):
            await operation(workspace=self.workspace, request=invalid)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                (2, 2),
                (
                    connection.execute("SELECT count(*) FROM memory_events").fetchone()[
                        0
                    ],
                    connection.execute(
                        "SELECT count(*) FROM memory_records"
                    ).fetchone()[0],
                ),
            )

    async def test_batch_cancellation_before_commit_leaves_no_events(self) -> None:
        """Cancelling a queued mutation must not commit after its caller leaves."""
        clock_started = threading.Event()
        release_clock = threading.Event()

        def blocking_clock() -> datetime:
            clock_started.set()
            release_clock.wait(timeout=2)
            return NOW

        operation = self._operations(clock=blocking_clock)["memory_store_batch"]
        task = asyncio.create_task(
            operation(workspace=self.workspace, request=self._batch_request())
        )
        started = await asyncio.to_thread(clock_started.wait, 2)
        self.assertTrue(started)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release_clock.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                0,
                connection.execute("SELECT count(*) FROM memory_events").fetchone()[0],
            )
        self.assertEqual([], self.scheduler_observations)

    async def test_read_cancellation_joins_the_shared_lock_worker(self) -> None:
        """A cancelled read must not detach a worker that still owns generation I/O."""
        from daem0nmcp.api.v7.runtime_services import WorkspaceStorageResolver

        entered = threading.Event()
        release = threading.Event()

        class BlockingResolver(WorkspaceStorageResolver):
            @contextmanager
            def locked_active(self, workspace):
                entered.set()
                release.wait(timeout=2)
                with super().locked_active(workspace) as active:
                    yield active

        operation = self._operations(storage_resolver=BlockingResolver())[
            "memory_recall_file"
        ]
        task = asyncio.create_task(
            operation(
                workspace=self.workspace,
                request=_request(
                    "memory_recall_file",
                    workspace_id=self.workspace.workspace_id,
                    relative_file_path="src/runtime.py",
                ),
            )
        )
        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0.02)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_pin_and_archive_append_authoritative_idempotent_events(self) -> None:
        """Direct record UPDATEs or duplicate state events would violate authority."""
        stored = await self._store_default_batch()
        record_id = stored.records[0].record_id
        operations = self._operations()

        pin_request = _request(
            "memory_pin_set",
            workspace_id=self.workspace.workspace_id,
            record_id=record_id,
            pinned=True,
            preflight_token=PREFLIGHT_TOKEN,
        )
        pinned = await operations["memory_pin_set"](
            workspace=self.workspace,
            request=pin_request,
        )
        replay = await operations["memory_pin_set"](
            workspace=self.workspace,
            request=pin_request,
        )
        self.assertFalse(pinned.idempotent_replay)
        self.assertTrue(replay.idempotent_replay)
        self.assertEqual([record_id], pinned.affected_ids)
        self.assertEqual(pinned.event_ids, replay.event_ids)

        archive_request = _request(
            "memory_archive_set",
            workspace_id=self.workspace.workspace_id,
            record_id=record_id,
            archived=True,
            preflight_token=PREFLIGHT_TOKEN,
        )
        archived = await operations["memory_archive_set"](
            workspace=self.workspace,
            request=archive_request,
        )
        archive_replay = await operations["memory_archive_set"](
            workspace=self.workspace,
            request=archive_request,
        )
        self.assertFalse(archived.idempotent_replay)
        self.assertTrue(archive_replay.idempotent_replay)
        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                "SELECT pinned,archived FROM memory_records WHERE record_id=?",
                (record_id,),
            ).fetchone()
            event_types = [
                item[0]
                for item in connection.execute(
                    "SELECT event_type FROM memory_events "
                    "WHERE stream_id=? ORDER BY rowid",
                    (record_id,),
                )
            ]
        self.assertEqual((1, 1), row)
        self.assertEqual(
            ["memory.created", "memory.pin_set", "memory.archive_set"],
            event_types,
        )

    async def test_mutation_not_found_is_stable_and_path_free(self) -> None:
        """Missing-record failures must not reveal the registered root."""
        from daem0nmcp.api.v7.record_operations import RecordOperationError

        request = _request(
            "memory_pin_set",
            workspace_id=self.workspace.workspace_id,
            record_id="mem_" + "f" * 64,
            pinned=True,
            preflight_token=PREFLIGHT_TOKEN,
        )
        with self.assertRaises(RecordOperationError) as caught:
            await self._operations()["memory_pin_set"](
                workspace=self.workspace,
                request=request,
            )
        self.assertEqual("NOT_FOUND", caught.exception.code)
        self.assertNotIn(str(self.root), str(caught.exception))

    async def test_file_recall_uses_versioned_authenticated_pagination(self) -> None:
        """File cursors must be keyed, tamper evident, and selector bound."""
        await self._operations()["memory_store_batch"](
            workspace=self.workspace,
            request=self._batch_request(
                records=[
                    {
                        "record_type": "decision",
                        "content": f"Runtime decision {index}",
                        "relative_file_path": "src/runtime.py",
                        "happened_at": NOW + timedelta(seconds=index),
                    }
                    for index in range(3)
                ]
                + [
                    {
                        "record_type": "warning",
                        "content": "A different file",
                        "relative_file_path": "src/other.py",
                    }
                ]
            ),
        )
        operation = self._operations()["memory_recall_file"]
        first = await operation(
            workspace=self.workspace,
            request=_request(
                "memory_recall_file",
                workspace_id=self.workspace.workspace_id,
                relative_file_path="src/runtime.py",
                limit=2,
            ),
        )
        self.assertEqual(2, len(first.items))
        self.assertTrue(first.truncated)
        self.assertIsNotNone(first.next_cursor)
        self.assertRegex(
            first.next_cursor,
            r"^cur_v1_[0-9a-f]{64}_[0-9a-f]{64}$",
        )
        self.assertTrue(
            all(item.relative_file_path == "src/runtime.py" for item in first.items)
        )

        cursor = first.next_cursor
        assert cursor is not None
        tampered = cursor[:-1] + ("0" if cursor[-1] != "0" else "1")
        from daem0nmcp.api.v7.record_operations import RecordOperationError

        for rejected_cursor in (
            tampered,
            cursor.replace("cur_v1_", "cur_v2_", 1),
            cursor + "0",
        ):
            with self.assertRaises(RecordOperationError) as caught:
                await operation(
                    workspace=self.workspace,
                    request=_request(
                        "memory_recall_file",
                        workspace_id=self.workspace.workspace_id,
                        relative_file_path="src/runtime.py",
                        cursor=rejected_cursor,
                        limit=2,
                    ),
                )
            self.assertEqual("INVALID_ARGUMENT", caught.exception.code)

        rotated_operation = self._operations(
            cursor_secret=b"rotated-record-cursor-secret-32!"
        )["memory_recall_file"]
        with self.assertRaises(RecordOperationError) as caught:
            await rotated_operation(
                workspace=self.workspace,
                request=_request(
                    "memory_recall_file",
                    workspace_id=self.workspace.workspace_id,
                    relative_file_path="src/runtime.py",
                    cursor=cursor,
                    limit=2,
                ),
            )
        self.assertEqual("INVALID_ARGUMENT", caught.exception.code)

        second = await operation(
            workspace=self.workspace,
            request=_request(
                "memory_recall_file",
                workspace_id=self.workspace.workspace_id,
                relative_file_path="src/runtime.py",
                cursor=cursor,
                limit=2,
            ),
        )
        self.assertEqual(1, len(second.items))
        self.assertFalse(second.truncated)
        self.assertIsNone(second.next_cursor)
        self.assertEqual(
            3,
            len({item.record_id for item in first.items + second.items}),
        )

        with self.assertRaises(RecordOperationError) as caught:
            await operation(
                workspace=self.workspace,
                request=_request(
                    "memory_recall_file",
                    workspace_id=self.workspace.workspace_id,
                    relative_file_path="src/other.py",
                    cursor=first.next_cursor,
                    limit=2,
                ),
            )
        self.assertEqual("INVALID_ARGUMENT", caught.exception.code)

    async def test_file_cursor_cannot_cross_an_authorized_workspace(self) -> None:
        """A matching event row in another workspace must not validate the cursor."""
        await self._operations(cursor_secret=CURSOR_SECRET)["memory_store_batch"](
            workspace=self.workspace,
            request=self._batch_request(
                records=[
                    {
                        "record_type": "decision",
                        "content": f"Cross-scope decision {index}",
                        "relative_file_path": "src/scope.py",
                        "happened_at": NOW + timedelta(seconds=index),
                    }
                    for index in range(2)
                ]
            ),
        )
        operation = self._operations(cursor_secret=CURSOR_SECRET)["memory_recall_file"]
        first = await operation(
            workspace=self.workspace,
            request=_request(
                "memory_recall_file",
                workspace_id=self.workspace.workspace_id,
                relative_file_path="src/scope.py",
                limit=1,
            ),
        )
        cursor = first.next_cursor
        assert cursor is not None
        event_id = "evt_" + cursor.split("_", 3)[2]
        other_workspace = self._workspace_with_cloned_record(event_id)

        from daem0nmcp.api.v7.record_operations import RecordOperationError

        with self.assertRaises(RecordOperationError) as caught:
            await operation(
                workspace=other_workspace,
                request=_request(
                    "memory_recall_file",
                    workspace_id=other_workspace.workspace_id,
                    relative_file_path="src/scope.py",
                    cursor=cursor,
                    limit=1,
                ),
            )
        self.assertEqual("INVALID_ARGUMENT", caught.exception.code)

    async def test_text_search_uses_task8_lexical_results_and_offset_highlights(
        self,
    ) -> None:
        """Falling back to v6 TF-IDF would lose Task 8 provenance and ranking."""
        await self._operations()["memory_store_batch"](
            workspace=self.workspace,
            request=self._batch_request(
                records=[
                    {
                        "record_type": "decision",
                        "content": "Signed cookie verification protects sessions.",
                        "tags": ["authentication"],
                        "relative_file_path": "src/auth.py",
                    },
                    {
                        "record_type": "pattern",
                        "content": "Signed cookie rotation limits exposure.",
                        "tags": ["authentication"],
                        "relative_file_path": "src/cookies.py",
                    },
                    {
                        "record_type": "learning",
                        "content": "Unrelated indexing note.",
                    },
                ]
            ),
        )
        from daem0nmcp.retrieval.projections import LexicalProjectionBuilder

        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            LexicalProjectionBuilder(connection, clock_us=lambda: 500).rebuild(
                self.workspace.workspace_id
            )
            connection.commit()

        operation = self._operations()["memory_search_text"]
        first = await operation(
            workspace=self.workspace,
            request=_request(
                "memory_search_text",
                workspace_id=self.workspace.workspace_id,
                query="signed cookie",
                limit=1,
                include_metadata=False,
                highlight=True,
            ),
        )
        self.assertEqual(1, len(first.items))
        self.assertTrue(first.truncated)
        self.assertRegex(
            first.next_cursor,
            r"^cur_v1_[0-9a-f]{64}_[0-9a-f]{64}$",
        )
        hit = first.items[0]
        self.assertEqual([], hit.record.tags)
        self.assertIsNone(hit.record.relative_file_path)
        self.assertEqual("lexical", hit.evidence_refs[0].provider)
        self.assertTrue(hit.highlights)
        for span in hit.highlights:
            highlighted = hit.bounded_excerpt[span.start : span.end].casefold()
            self.assertIn(highlighted, {"signed", "cookie"})

        cursor = first.next_cursor
        assert cursor is not None
        from daem0nmcp.api.v7.record_operations import RecordOperationError

        rotated_operation = self._operations(
            cursor_secret=b"rotated-record-cursor-secret-32!"
        )["memory_search_text"]
        with self.assertRaises(RecordOperationError) as caught:
            await rotated_operation(
                workspace=self.workspace,
                request=_request(
                    "memory_search_text",
                    workspace_id=self.workspace.workspace_id,
                    query="signed cookie",
                    cursor=cursor,
                    limit=1,
                    include_metadata=False,
                    highlight=True,
                ),
            )
        self.assertEqual("INVALID_ARGUMENT", caught.exception.code)

        tampered = cursor[:-1] + ("0" if cursor[-1] != "0" else "1")
        with self.assertRaises(RecordOperationError) as caught:
            await operation(
                workspace=self.workspace,
                request=_request(
                    "memory_search_text",
                    workspace_id=self.workspace.workspace_id,
                    query="signed cookie",
                    cursor=tampered,
                    limit=1,
                    include_metadata=False,
                    highlight=True,
                ),
            )
        self.assertEqual("INVALID_ARGUMENT", caught.exception.code)

        second = await operation(
            workspace=self.workspace,
            request=_request(
                "memory_search_text",
                workspace_id=self.workspace.workspace_id,
                query="signed cookie",
                cursor=cursor,
                limit=1,
                include_metadata=False,
                highlight=True,
            ),
        )
        self.assertEqual(1, len(second.items))
        self.assertNotEqual(
            first.items[0].record.record_id,
            second.items[0].record.record_id,
        )
        with self.assertRaises(RecordOperationError) as caught:
            await operation(
                workspace=self.workspace,
                request=_request(
                    "memory_search_text",
                    workspace_id=self.workspace.workspace_id,
                    query="different query",
                    cursor=first.next_cursor,
                    limit=1,
                ),
            )
        self.assertEqual("INVALID_ARGUMENT", caught.exception.code)

    async def test_text_search_fails_closed_without_lexical_projection(self) -> None:
        """Missing lexical authority must not silently select a legacy search path."""
        from daem0nmcp.api.v7.record_operations import RecordOperationError

        await self._store_default_batch()
        with self.assertRaises(RecordOperationError) as caught:
            await self._operations()["memory_search_text"](
                workspace=self.workspace,
                request=_request(
                    "memory_search_text",
                    workspace_id=self.workspace.workspace_id,
                    query="canonical",
                ),
            )
        self.assertEqual("LEXICAL_UNAVAILABLE", caught.exception.code)

    async def test_session_updates_cursor_advances_without_timestamp_loss(self) -> None:
        """Equal timestamps must not make a newly appended update disappear."""
        operations = self._operations()
        stored = await operations["memory_store_batch"](
            workspace=self.workspace,
            request=self._batch_request(
                records=[
                    {
                        "record_type": "decision",
                        "content": "First session update.",
                    }
                ]
            ),
        )
        first = await operations["session_updates_get"](
            workspace=self.workspace,
            request=_request(
                "session_updates_get",
                workspace_id=self.workspace.workspace_id,
            ),
        )
        self.assertTrue(first.changed)
        self.assertEqual(stored.event_ids, [item.event_id for item in first.events])
        self.assertEqual("record", first.events[0].kind)
        self.assertEqual(stored.records[0].record_id, first.events[0].object_id)
        self.assertRegex(
            first.cursor,
            r"^cur_v1_[0-9a-f]{64}_[0-9a-f]{64}$",
        )

        from daem0nmcp.api.v7.record_operations import RecordOperationError

        rotated = self._operations(cursor_secret=b"rotated-record-cursor-secret-32!")[
            "session_updates_get"
        ]
        with self.assertRaises(RecordOperationError) as caught:
            await rotated(
                workspace=self.workspace,
                request=_request(
                    "session_updates_get",
                    workspace_id=self.workspace.workspace_id,
                    after_cursor=first.cursor,
                ),
            )
        self.assertEqual("INVALID_ARGUMENT", caught.exception.code)

        tampered = first.cursor[:-1] + ("0" if first.cursor[-1] != "0" else "1")
        with self.assertRaises(RecordOperationError) as caught:
            await operations["session_updates_get"](
                workspace=self.workspace,
                request=_request(
                    "session_updates_get",
                    workspace_id=self.workspace.workspace_id,
                    after_cursor=tampered,
                ),
            )
        self.assertEqual("INVALID_ARGUMENT", caught.exception.code)

        with self.assertRaises(RecordOperationError) as caught:
            await operations["session_updates_get"](
                workspace=self.workspace,
                request=_request(
                    "session_updates_get",
                    workspace_id=self.workspace.workspace_id,
                    after_cursor=first.cursor,
                    since=NOW - timedelta(seconds=1),
                ),
            )
        self.assertEqual("INVALID_ARGUMENT", caught.exception.code)

        unchanged = await operations["session_updates_get"](
            workspace=self.workspace,
            request=_request(
                "session_updates_get",
                workspace_id=self.workspace.workspace_id,
                after_cursor=first.cursor,
            ),
        )
        self.assertFalse(unchanged.changed)
        self.assertEqual(first.cursor, unchanged.cursor)

        later = await operations["memory_store_batch"](
            workspace=self.workspace,
            request=self._batch_request(
                key="record-batch-later-0002",
                records=[
                    {
                        "record_type": "warning",
                        "content": "Second update at the same recorded timestamp.",
                    }
                ],
            ),
        )
        changed = await operations["session_updates_get"](
            workspace=self.workspace,
            request=_request(
                "session_updates_get",
                workspace_id=self.workspace.workspace_id,
                after_cursor=first.cursor,
            ),
        )
        self.assertTrue(changed.changed)
        self.assertEqual(later.event_ids, [item.event_id for item in changed.events])
        self.assertNotEqual(first.cursor, changed.cursor)

    async def test_empty_session_cursor_is_authenticated(self) -> None:
        """The origin cursor is a capability too, even before the first event."""
        operation = self._operations(cursor_secret=CURSOR_SECRET)["session_updates_get"]
        first = await operation(
            workspace=self.workspace,
            request=_request(
                "session_updates_get",
                workspace_id=self.workspace.workspace_id,
            ),
        )
        self.assertFalse(first.changed)
        self.assertRegex(first.cursor, r"^cur_v1_origin_[0-9a-f]{64}$")

        rotated = self._operations(cursor_secret=b"rotated-record-cursor-secret-32!")[
            "session_updates_get"
        ]
        from daem0nmcp.api.v7.record_operations import RecordOperationError

        with self.assertRaises(RecordOperationError) as caught:
            await rotated(
                workspace=self.workspace,
                request=_request(
                    "session_updates_get",
                    workspace_id=self.workspace.workspace_id,
                    after_cursor=first.cursor,
                ),
            )
        self.assertEqual("INVALID_ARGUMENT", caught.exception.code)

    async def test_session_updates_exclude_fact_and_relationship_streams(self) -> None:
        """The record feed must not emit object IDs outside its public union."""
        with closing(sqlite3.connect(self.database)) as connection:
            event_hash = "f" * 64
            connection.execute(
                "INSERT INTO memory_events "
                "(event_id,workspace_id,stream_id,stream_kind,stream_version,"
                "event_type,event_schema_version,occurred_at_us,recorded_at_us,"
                "actor_type,payload_json,payload_hash,event_hash) "
                "VALUES (?,?,?,?,1,'fact.asserted',1,1,1,'system','{}',?,?)",
                (
                    "evt_" + event_hash,
                    self.workspace.workspace_id,
                    "fact_" + "a" * 64,
                    "fact",
                    "b" * 64,
                    event_hash,
                ),
            )
            connection.commit()

        result = await self._operations()["session_updates_get"](
            workspace=self.workspace,
            request=_request(
                "session_updates_get",
                workspace_id=self.workspace.workspace_id,
            ),
        )

        self.assertFalse(result.changed)
        self.assertEqual([], result.events)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT count(*) FROM session_update_sequence"
                ).fetchone()[0],
            )

    async def test_handlers_reject_a_mismatched_workspace_object(self) -> None:
        """Trusting only the request selector would admit a forged root object."""
        from daem0nmcp.api.v7.record_operations import RecordOperationError
        from daem0nmcp.workspace import Workspace

        forged = Workspace(self.workspace.workspace_id, self.root / "other")
        request = _request(
            "memory_recall_file",
            workspace_id=self.workspace.workspace_id,
            relative_file_path="src/runtime.py",
        )
        with self.assertRaises(RecordOperationError) as caught:
            await self._operations()["memory_recall_file"](
                workspace=forged,
                request=request,
            )
        self.assertEqual("UNAUTHORIZED_WORKSPACE", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
