from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

from daem0nmcp.api.v7.application import AdmittedRequest
from daem0nmcp.covenant import InvocationScope
from daem0nmcp.event_store import EventCommand, EventStore

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
NOW_US = 1_789_646_400_000_000


def _schema(connection: sqlite3.Connection) -> None:
    from daem0nmcp.migrations.schema import MIGRATIONS
    from daem0nmcp.schema_version import CURRENT_SCHEMA_VERSION

    connection.execute("CREATE TABLE schema_version(version INTEGER PRIMARY KEY)")
    for version, _description, statements in MIGRATIONS:
        if 16 <= version <= CURRENT_SCHEMA_VERSION:
            for statement in statements:
                connection.execute(statement)
            connection.execute("INSERT INTO schema_version VALUES(?)", (version,))
    connection.commit()


def _request(name: str, **arguments: object) -> AdmittedRequest:
    from daem0nmcp.api.v7.tools import TOOL_INPUT_MODELS

    model = TOOL_INPUT_MODELS[name].model_validate(arguments)
    values = model.model_dump(mode="python")
    values.pop("preflight_token", None)
    return AdmittedRequest(name, MappingProxyType(values))


class _Gate:
    def __init__(self) -> None:
        self.denied: set[str] = set()

    def workspace_authorized(self, scope: InvocationScope) -> bool:
        return scope.canonical_workspace not in self.denied


class ConsolidationOperationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from daem0nmcp.storage_activation import (
            ActiveDatabasePointer,
            write_active_pointer,
        )
        from daem0nmcp.workspace import WorkspaceRegistry

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.roots = [base / name for name in ("target", "source-a", "source-b")]
        for root in self.roots:
            database = root / ".daem0nmcp" / "storage" / "daem0nmcp.db"
            database.parent.mkdir(parents=True)
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("PRAGMA foreign_keys=ON")
                _schema(connection)
            write_active_pointer(
                database.parent, ActiveDatabasePointer(7, 1, database.name, None, None)
            )
        self.registry = WorkspaceRegistry(self.roots[1:], default_root=self.roots[0])
        self.target = self.registry.resolve(str(self.roots[0]))
        self.sources = [self.registry.resolve(str(root)) for root in self.roots[1:]]
        self.gate = _Gate()
        self.dependencies = None

    async def asyncTearDown(self) -> None:
        if self.dependencies is not None:
            self.dependencies.close()

    def _db(self, workspace) -> Path:
        return workspace.root / ".daem0nmcp" / "storage" / "daem0nmcp.db"

    def _seed(self, workspace, content: str) -> str:
        from daem0nmcp.event_store import deterministic_id

        record_id = deterministic_id("mem", "test", workspace.workspace_id, content)
        state = {
            "record_type": "learning",
            "legacy_type": None,
            "content": content,
            "rationale": "why",
            "context": {"source": content},
            "tags": ["test"],
            "file_path": None,
            "file_path_relative": "docs/a.md",
            "keywords": None,
            "is_permanent": False,
            "pinned": False,
            "archived": False,
            "outcome": "worked",
            "worked": True,
            "recall_count": 2,
            "surprise_score": 0.25,
            "importance_score": 0.75,
            "source_client": "test",
            "source_model": None,
            "deleted_at_us": None,
        }
        with closing(sqlite3.connect(self._db(workspace))) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            EventStore(connection).append_and_project(
                EventCommand(
                    workspace_id=workspace.workspace_id,
                    stream_id=record_id,
                    stream_kind="memory",
                    event_type="memory.created",
                    occurred_at_us=NOW_US,
                    recorded_at_us=NOW_US,
                    actor_type="user",
                    payload={"record": state},
                    expected_stream_version=1,
                )
            )
            connection.commit()
        return record_id

    async def _link(self) -> None:
        from daem0nmcp.api.v7.federation_operations import (
            FederationOperationDependencies,
            build_federation_operations,
        )

        deps = FederationOperationDependencies(
            workspace_resolver=self.registry, clock=lambda: NOW, cursor_secret=b"x" * 32
        )
        try:
            operation = build_federation_operations(deps)["workspace_link"]
            for source in self.sources:
                await operation(
                    workspace=self.target,
                    request=_request(
                        "workspace_link",
                        workspace_id=self.target.workspace_id,
                        linked_workspace_id=source.workspace_id,
                        relationship="related",
                        preflight_token="p" * 32,
                    ),
                )
        finally:
            deps.close()

    def _operations(self, **changes):
        from daem0nmcp.api.v7.consolidation_operations import (
            ConsolidationOperationDependencies,
            build_consolidation_operations,
        )

        options = {
            "workspace_resolver": self.registry,
            "covenant_gate": self.gate,
            "scope_provider": lambda: InvocationScope(
                "principal", "session", str(self.target.root)
            ),
            "signing_key": b"consolidation-test-signing-key-32!",
            "clock": lambda: NOW,
        }
        options.update(changes)
        self.dependencies = ConsolidationOperationDependencies(**options)
        return build_consolidation_operations(self.dependencies)

    async def _preview(self, operations):
        return await operations["workspace_consolidation_preview"](
            workspace=self.target,
            request=_request(
                "workspace_consolidation_preview",
                workspace_id=self.target.workspace_id,
                source_workspace_ids={source.workspace_id for source in self.sources},
            ),
        )

    async def test_preview_apply_replay_preserves_state_and_provenance(self) -> None:
        await self._link()
        source_ids = [
            self._seed(source, f"from {index}")
            for index, source in enumerate(self.sources)
        ]
        operations = self._operations()
        preview = await self._preview(operations)
        self.assertEqual(2, preview.selected)
        request = _request(
            "workspace_consolidate",
            workspace_id=self.target.workspace_id,
            source_workspace_ids={source.workspace_id for source in self.sources},
            idempotency_key="consolidate-0001",
            selection_token=preview.selection_token,
            preflight_token="p" * 32,
        )
        result = await operations["workspace_consolidate"](
            workspace=self.target, request=request
        )
        replay = await operations["workspace_consolidate"](
            workspace=self.target, request=request
        )
        self.assertEqual(2, result.imported)
        self.assertEqual(result, replay)
        with closing(sqlite3.connect(self._db(self.target))) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT content,context_json,outcome,worked,file_path,file_path_relative FROM memory_records WHERE workspace_id=? ORDER BY content",
                (self.target.workspace_id,),
            ).fetchall()
            self.assertEqual(["from 0", "from 1"], [row["content"] for row in rows])
            self.assertTrue(all(row["file_path"] is None for row in rows))
            self.assertTrue(
                all(row["file_path_relative"] == "docs/a.md" for row in rows)
            )
            provenance = [
                json.loads(row[0])["provenance"]
                for row in connection.execute(
                    "SELECT payload_json FROM memory_events WHERE workspace_id=? ORDER BY event_id",
                    (self.target.workspace_id,),
                )
            ]
            self.assertEqual(
                set(source_ids), {item["source_record_id"] for item in provenance}
            )

    async def test_concurrent_same_key_commits_one_target_history(self) -> None:
        await self._link()
        for index, source in enumerate(self.sources):
            self._seed(source, f"concurrent {index}")
        operations = self._operations()
        preview = await self._preview(operations)
        request = _request(
            "workspace_consolidate",
            workspace_id=self.target.workspace_id,
            source_workspace_ids={source.workspace_id for source in self.sources},
            idempotency_key="concurrent-0001",
            selection_token=preview.selection_token,
            preflight_token="p" * 32,
        )
        first, second = await asyncio.gather(
            *[
                operations["workspace_consolidate"](
                    workspace=self.target, request=request
                )
                for _ in range(2)
            ]
        )
        self.assertEqual(first, second)
        with closing(sqlite3.connect(self._db(self.target))) as connection:
            self.assertEqual(
                2,
                connection.execute(
                    "SELECT count(*) FROM memory_events WHERE workspace_id=?",
                    (self.target.workspace_id,),
                ).fetchone()[0],
            )
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT count(*) FROM consolidation_runs"
                ).fetchone()[0],
            )

    async def test_preview_token_survives_service_restart_without_credentials(
        self,
    ) -> None:
        await self._link()
        for index, source in enumerate(self.sources):
            self._seed(source, f"restart {index}")
        operations = self._operations()
        preview = await self._preview(operations)
        self.dependencies.close()
        self.dependencies = None
        from daem0nmcp.api.v7.consolidation_operations import (
            ConsolidationOperationDependencies,
            build_consolidation_operations,
        )

        self.dependencies = ConsolidationOperationDependencies(
            workspace_resolver=self.registry,
            covenant_gate=self.gate,
            scope_provider=lambda: InvocationScope(
                "principal", "session", str(self.target.root)
            ),
            signing_key=b"a-different-post-restart-key-32!",
            clock=lambda: NOW,
        )
        restarted = build_consolidation_operations(self.dependencies)
        result = await restarted["workspace_consolidate"](
            workspace=self.target,
            request=_request(
                "workspace_consolidate",
                workspace_id=self.target.workspace_id,
                source_workspace_ids={source.workspace_id for source in self.sources},
                idempotency_key="restart-0001",
                selection_token=preview.selection_token,
                preflight_token="p" * 32,
            ),
        )
        self.assertEqual(2, result.imported)

    async def test_cancel_before_publication_rolls_back_target(self) -> None:
        await self._link()
        for index, source in enumerate(self.sources):
            self._seed(source, f"cancel {index}")
        operations = self._operations()
        preview = await self._preview(operations)
        request = _request(
            "workspace_consolidate",
            workspace_id=self.target.workspace_id,
            source_workspace_ids={source.workspace_id for source in self.sources},
            idempotency_key="cancel-0001",
            selection_token=preview.selection_token,
            preflight_token="p" * 32,
        )
        entered = threading.Event()
        release = threading.Event()
        from daem0nmcp.api.v7 import consolidation_operations as module

        original = module._event_root

        def blocked_root(connection, workspace_id):
            entered.set()
            release.wait(5)
            return original(connection, workspace_id)

        with patch.object(module, "_event_root", side_effect=blocked_root):
            task = asyncio.create_task(
                operations["workspace_consolidate"](
                    workspace=self.target, request=request
                )
            )
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            task.cancel()
            # Let the task observe its cancellation before the worker resumes.
            await asyncio.sleep(0)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        with closing(sqlite3.connect(self._db(self.target))) as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT count(*) FROM memory_events WHERE workspace_id=?",
                    (self.target.workspace_id,),
                ).fetchone()[0],
            )
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT count(*) FROM consolidation_runs"
                ).fetchone()[0],
            )

    async def test_stale_snapshot_and_denied_source_fail_before_target_events(
        self,
    ) -> None:
        await self._link()
        self._seed(self.sources[0], "first")
        self._seed(self.sources[1], "second")
        operations = self._operations()
        preview = await self._preview(operations)
        self._seed(self.sources[0], "changed after preview")
        with self.assertRaisesRegex(Exception, "CONFLICT"):
            await operations["workspace_consolidate"](
                workspace=self.target,
                request=_request(
                    "workspace_consolidate",
                    workspace_id=self.target.workspace_id,
                    source_workspace_ids={
                        source.workspace_id for source in self.sources
                    },
                    idempotency_key="consolidate-0002",
                    selection_token=preview.selection_token,
                    preflight_token="p" * 32,
                ),
            )
        with closing(sqlite3.connect(self._db(self.target))) as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT count(*) FROM memory_events WHERE workspace_id=?",
                    (self.target.workspace_id,),
                ).fetchone()[0],
            )
        self.gate.denied.add(os.path.normcase(str(self.sources[0].root)))
        with self.assertRaisesRegex(Exception, "UNAUTHORIZED_WORKSPACE"):
            await self._preview(operations)

    async def test_archive_is_canonical_and_keeps_source_storage(self) -> None:
        await self._link()
        for index, source in enumerate(self.sources):
            self._seed(source, f"archive {index}")
        operations = self._operations()
        preview = await self._preview(operations)
        result = await operations["workspace_consolidate_and_archive_sources"](
            workspace=self.target,
            request=_request(
                "workspace_consolidate_and_archive_sources",
                workspace_id=self.target.workspace_id,
                source_workspace_ids={source.workspace_id for source in self.sources},
                idempotency_key="archive-0001",
                selection_token=preview.selection_token,
                preflight_token="p" * 32,
            ),
        )
        self.assertEqual(2, result.archived)
        for source in self.sources:
            self.assertTrue(source.root.is_dir())
            with closing(sqlite3.connect(self._db(source))) as connection:
                self.assertEqual(
                    (1, "memory.archive_set"),
                    connection.execute(
                        "SELECT archived,event_type FROM memory_records JOIN memory_events ON source_event_id=event_id WHERE memory_records.workspace_id=?",
                        (source.workspace_id,),
                    ).fetchone(),
                )

    async def test_target_access_revocation_rolls_back_before_commit(self) -> None:
        await self._link()
        for index, source in enumerate(self.sources):
            self._seed(source, f"revoke target {index}")
        operations = self._operations()
        preview = await self._preview(operations)
        target_root = os.path.normcase(str(self.target.root))
        original = EventStore.append_and_project
        appended = 0

        def revoke_after_first(store, command):
            nonlocal appended
            result = original(store, command)
            if command.workspace_id == self.target.workspace_id:
                appended += 1
                if appended == 1:
                    self.gate.denied.add(target_root)
            return result

        with (
            patch.object(EventStore, "append_and_project", revoke_after_first),
            self.assertRaisesRegex(Exception, "UNAUTHORIZED_WORKSPACE"),
        ):
            await operations["workspace_consolidate"](
                workspace=self.target,
                request=_request(
                    "workspace_consolidate",
                    workspace_id=self.target.workspace_id,
                    source_workspace_ids={item.workspace_id for item in self.sources},
                    idempotency_key="revoke-target-0001",
                    selection_token=preview.selection_token,
                    preflight_token="p" * 32,
                ),
            )
        with closing(sqlite3.connect(self._db(self.target))) as connection:
            self.assertEqual(
                0,
                connection.execute("SELECT count(*) FROM memory_events").fetchone()[0],
            )
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT count(*) FROM consolidation_runs"
                ).fetchone()[0],
            )

    async def test_incomplete_archive_replay_resumes_and_expired_receipt_replays(
        self,
    ) -> None:
        await self._link()
        for index, source in enumerate(self.sources):
            self._seed(source, f"resume archive {index}")
        now = [NOW]
        scheduled: list[Path] = []
        loop = asyncio.get_running_loop()

        def schedule(path: Path) -> None:
            # Match production's requirement for a running owner event loop.
            self.assertIs(loop, asyncio.get_running_loop())
            scheduled.append(path)

        operations = self._operations(
            clock=lambda: now[0], projection_scheduler=schedule
        )
        preview = await self._preview(operations)
        request = _request(
            "workspace_consolidate_and_archive_sources",
            workspace_id=self.target.workspace_id,
            source_workspace_ids={item.workspace_id for item in self.sources},
            idempotency_key="resume-archive-0001",
            selection_token=preview.selection_token,
            preflight_token="p" * 32,
        )
        original = EventStore.append_and_project
        revoked: str | None = None

        def revoke_source_before_commit(store, command):
            nonlocal revoked
            result = original(store, command)
            if command.event_type == "memory.archive_set" and revoked is None:
                source = next(
                    item
                    for item in self.sources
                    if item.workspace_id == command.workspace_id
                )
                revoked = os.path.normcase(str(source.root))
                self.gate.denied.add(revoked)
            return result

        with (
            patch.object(EventStore, "append_and_project", revoke_source_before_commit),
            self.assertRaisesRegex(Exception, "UNAUTHORIZED_WORKSPACE"),
        ):
            await operations["workspace_consolidate_and_archive_sources"](
                workspace=self.target, request=request
            )
        with closing(sqlite3.connect(self._db(self.target))) as connection:
            self.assertEqual(
                "recovery_required",
                connection.execute("SELECT status FROM consolidation_runs").fetchone()[
                    0
                ],
            )
        assert revoked is not None
        self.assertEqual(
            {self._db(self.target), *(self._db(source) for source in self.sources)},
            set(scheduled),
        )
        scheduled.clear()
        self.gate.denied.remove(revoked)
        resumed = await operations["workspace_consolidate_and_archive_sources"](
            workspace=self.target, request=request
        )
        self.assertEqual(2, resumed.archived)
        self.assertEqual(3, len(set(scheduled)))
        now[0] = NOW + timedelta(minutes=20)
        replay = await operations["workspace_consolidate_and_archive_sources"](
            workspace=self.target, request=request
        )
        self.assertEqual(resumed, replay)

    async def test_cancel_during_source_archive_keeps_recoverable_target(self) -> None:
        await self._link()
        for index, source in enumerate(self.sources):
            self._seed(source, f"cancel archive {index}")
        scheduled: list[Path] = []
        operations = self._operations(projection_scheduler=scheduled.append)
        preview = await self._preview(operations)
        request = _request(
            "workspace_consolidate_and_archive_sources",
            workspace_id=self.target.workspace_id,
            source_workspace_ids={item.workspace_id for item in self.sources},
            idempotency_key="cancel-archive-0001",
            selection_token=preview.selection_token,
            preflight_token="p" * 32,
        )
        entered = threading.Event()
        release = threading.Event()
        original = EventStore.append_and_project

        def block_first_archive(store, command):
            result = original(store, command)
            if command.event_type == "memory.archive_set" and not entered.is_set():
                entered.set()
                release.wait(5)
            return result

        with patch.object(EventStore, "append_and_project", block_first_archive):
            task = asyncio.create_task(
                operations["workspace_consolidate_and_archive_sources"](
                    workspace=self.target, request=request
                )
            )
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            task.cancel()
            # Let the task observe its cancellation before the worker resumes.
            await asyncio.sleep(0)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertIn(self._db(self.target), scheduled)

        with closing(sqlite3.connect(self._db(self.target))) as connection:
            self.assertEqual(
                ("recovery_required", 2, 0),
                connection.execute(
                    "SELECT status,imported_count,archived_count "
                    "FROM consolidation_runs"
                ).fetchone(),
            )
        for source in self.sources:
            with closing(sqlite3.connect(self._db(source))) as connection:
                self.assertEqual(
                    0,
                    connection.execute(
                        "SELECT COUNT(*) FROM memory_records WHERE archived=1"
                    ).fetchone()[0],
                )

    async def test_preview_child_and_source_content_hash_are_reverified(self) -> None:
        await self._link()
        for index, source in enumerate(self.sources):
            self._seed(source, f"integrity {index}")
        now = [NOW]
        operations = self._operations(clock=lambda: now[0])
        preview = await self._preview(operations)
        with closing(sqlite3.connect(self._db(self.target))) as connection:
            connection.execute(
                "UPDATE consolidation_preview_records SET target_record_id=? "
                "WHERE preview_id=? AND ordinal=0",
                ("mem_" + "0" * 64, preview.preview_id),
            )
            connection.commit()
        with self.assertRaisesRegex(Exception, "CAPABILITY_DEGRADED"):
            await operations["workspace_consolidate"](
                workspace=self.target,
                request=_request(
                    "workspace_consolidate",
                    workspace_id=self.target.workspace_id,
                    source_workspace_ids={item.workspace_id for item in self.sources},
                    idempotency_key="tampered-child-0001",
                    selection_token=preview.selection_token,
                    preflight_token="p" * 32,
                ),
            )

        now[0] += timedelta(seconds=1)
        fresh = await self._preview(operations)
        with closing(sqlite3.connect(self._db(self.sources[0]))) as connection:
            connection.execute(
                "UPDATE memory_records SET content_hash=? WHERE workspace_id=?",
                ("f" * 64, self.sources[0].workspace_id),
            )
            connection.commit()
        with self.assertRaisesRegex(Exception, "CONFLICT"):
            await operations["workspace_consolidate"](
                workspace=self.target,
                request=_request(
                    "workspace_consolidate",
                    workspace_id=self.target.workspace_id,
                    source_workspace_ids={item.workspace_id for item in self.sources},
                    idempotency_key="tampered-content-0001",
                    selection_token=fresh.selection_token,
                    preflight_token="p" * 32,
                ),
            )

    async def test_durable_execution_scope_supports_five_sources_after_restart(
        self,
    ) -> None:
        from daem0nmcp.api.v7.tasks import (
            DurableTaskExecution,
            durable_task_execution_var,
        )
        from daem0nmcp.storage_activation import (
            ActiveDatabasePointer,
            write_active_pointer,
        )
        from daem0nmcp.workspace import WorkspaceRegistry

        for index in range(3):
            root = self.roots[0].parent / f"source-extra-{index}"
            database = root / ".daem0nmcp" / "storage" / "daem0nmcp.db"
            database.parent.mkdir(parents=True)
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("PRAGMA foreign_keys=ON")
                _schema(connection)
            write_active_pointer(
                database.parent,
                ActiveDatabasePointer(7, 1, database.name, None, None),
            )
            self.roots.append(root)
        self.registry = WorkspaceRegistry(self.roots[1:], default_root=self.roots[0])
        self.target = self.registry.resolve(str(self.roots[0]))
        self.sources = [self.registry.resolve(str(root)) for root in self.roots[1:]]
        await self._link()
        for index, source in enumerate(self.sources):
            self._seed(source, f"durable {index}")
        operations = self._operations(scope_provider=lambda: None)
        execution = DurableTaskExecution(
            task_id="tsk_" + "1" * 64,
            tool_name="workspace_consolidation_preview",
            workspace_id=self.target.workspace_id,
            arguments_sha256="2" * 64,
            principal_id="principal",
            transport_session_id="session",
        )
        token = durable_task_execution_var.set(execution)
        try:
            preview = await self._preview(operations)
        finally:
            durable_task_execution_var.reset(token)
        self.assertEqual(5, preview.selected)

        denied = os.path.normcase(str(self.sources[-1].root))
        self.gate.denied.add(denied)
        apply_execution = DurableTaskExecution(
            task_id="tsk_" + "3" * 64,
            tool_name="workspace_consolidate",
            workspace_id=self.target.workspace_id,
            arguments_sha256="4" * 64,
            principal_id="principal",
            transport_session_id="session",
        )
        token = durable_task_execution_var.set(apply_execution)
        try:
            with self.assertRaisesRegex(Exception, "UNAUTHORIZED_WORKSPACE"):
                await operations["workspace_consolidate"](
                    workspace=self.target,
                    request=_request(
                        "workspace_consolidate",
                        workspace_id=self.target.workspace_id,
                        source_workspace_ids={
                            item.workspace_id for item in self.sources
                        },
                        idempotency_key="durable-five-0001",
                        selection_token=preview.selection_token,
                        preflight_token="p" * 32,
                    ),
                )
        finally:
            durable_task_execution_var.reset(token)
        self.gate.denied.remove(denied)
        token = durable_task_execution_var.set(apply_execution)
        try:
            result = await operations["workspace_consolidate"](
                workspace=self.target,
                request=_request(
                    "workspace_consolidate",
                    workspace_id=self.target.workspace_id,
                    source_workspace_ids={item.workspace_id for item in self.sources},
                    idempotency_key="durable-five-0001",
                    selection_token=preview.selection_token,
                    preflight_token="p" * 32,
                ),
            )
        finally:
            durable_task_execution_var.reset(token)
        self.assertEqual(5, result.imported)

    def test_source_rows_are_streamed_without_fetchall(self) -> None:
        from daem0nmcp.api.v7.consolidation_operations import _rows

        class Cursor:
            def __init__(self) -> None:
                self.remaining = 10_001

            def fetchmany(self, size):
                count = min(size, self.remaining)
                self.remaining -= count
                return [object()] * count

            def fetchall(self):
                raise AssertionError("source rows must not be materialized")

        class Connection:
            def execute(self, query, parameters):
                del query, parameters
                return Cursor()

        with self.assertRaisesRegex(Exception, "TASK_REQUIRED"):
            tuple(_rows(Connection(), self.sources[0].workspace_id))

    async def test_recovery_recognizes_committed_archive_after_progress_interruption(
        self,
    ) -> None:
        from daem0nmcp.api.v7.consolidation_operations import recover_consolidation

        await self._link()
        for index, source in enumerate(self.sources):
            self._seed(source, f"recover {index}")
        operations = self._operations()
        preview = await self._preview(operations)
        await operations["workspace_consolidate_and_archive_sources"](
            workspace=self.target,
            request=_request(
                "workspace_consolidate_and_archive_sources",
                workspace_id=self.target.workspace_id,
                source_workspace_ids={source.workspace_id for source in self.sources},
                idempotency_key="archive-recover-0001",
                selection_token=preview.selection_token,
                preflight_token="p" * 32,
            ),
        )
        with closing(sqlite3.connect(self._db(self.target))) as connection:
            run_id = connection.execute(
                "SELECT run_id FROM consolidation_runs"
            ).fetchone()[0]
            connection.execute(
                "UPDATE consolidation_archive_progress SET status='pending',archive_event_id=NULL"
            )
            connection.execute(
                "UPDATE consolidation_runs SET status='recovery_required',archived_count=0"
            )
            connection.commit()
        recovered = recover_consolidation(
            self.registry, self.target, run_id=run_id, clock=lambda: NOW
        )
        self.assertEqual("completed", recovered[0]["status"])
        self.assertEqual(2, recovered[0]["archived"])
        for source in self.sources:
            with closing(sqlite3.connect(self._db(source))) as connection:
                self.assertEqual(
                    2,
                    connection.execute(
                        "SELECT count(*) FROM memory_events WHERE workspace_id=?",
                        (source.workspace_id,),
                    ).fetchone()[0],
                )

    async def test_recovery_rejects_target_generation_swap(self) -> None:
        from daem0nmcp.api.v7.consolidation_operations import recover_consolidation
        from daem0nmcp.api.v7.runtime_services import WorkspaceStorageResolver
        from daem0nmcp.storage_activation import ResolvedActiveDatabase

        await self._link()
        for index, source in enumerate(self.sources):
            self._seed(source, f"swap recover {index}")
        operations = self._operations()
        preview = await self._preview(operations)
        await operations["workspace_consolidate_and_archive_sources"](
            workspace=self.target,
            request=_request(
                "workspace_consolidate_and_archive_sources",
                workspace_id=self.target.workspace_id,
                source_workspace_ids={source.workspace_id for source in self.sources},
                idempotency_key="swap-recover-0001",
                selection_token=preview.selection_token,
                preflight_token="p" * 32,
            ),
        )
        with closing(sqlite3.connect(self._db(self.target))) as connection:
            run_id = connection.execute(
                "SELECT run_id FROM consolidation_runs"
            ).fetchone()[0]
            connection.execute(
                "UPDATE consolidation_archive_progress SET status='pending',archive_event_id=NULL"
            )
            connection.execute(
                "UPDATE consolidation_runs SET status='recovery_required',archived_count=0"
            )
            connection.commit()
        replacement = self._db(self.target).with_name("replacement.db")
        with closing(sqlite3.connect(replacement)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            _schema(connection)
        delegate = WorkspaceStorageResolver()

        class SwappingResolver:
            def __init__(self) -> None:
                self.target_calls = 0

            @contextmanager
            def locked_active(self, workspace):
                if workspace.workspace_id != self_target.workspace_id:
                    with delegate.locked_active(workspace) as active:
                        yield active
                    return
                self.target_calls += 1
                if self.target_calls == 1:
                    with delegate.locked_active(workspace) as active:
                        yield active
                    return
                yield ResolvedActiveDatabase(
                    storage_path=replacement.parent,
                    path=replacement,
                    relative_path=replacement.name,
                    format_version=7,
                    generation=2,
                    previous_db=self._db_name,
                    migration_run_id=None,
                    pointer=None,
                    pointer_bytes=None,
                )

            _db_name = self._db(self.target).name

        self_target = self.target
        with self.assertRaisesRegex(Exception, "CONFLICT"):
            recover_consolidation(
                self.registry,
                self.target,
                run_id=run_id,
                storage_resolver=SwappingResolver(),
                clock=lambda: NOW,
            )
        with closing(sqlite3.connect(self._db(self.target))) as connection:
            self.assertEqual(
                "recovery_required",
                connection.execute("SELECT status FROM consolidation_runs").fetchone()[
                    0
                ],
            )


if __name__ == "__main__":
    unittest.main()
