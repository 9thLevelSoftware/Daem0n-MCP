"""Production v7 granular operation adapters at the canonical storage boundary."""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from unittest.mock import MagicMock, patch

from daem0nmcp.api.v7.application import AdmittedRequest
from daem0nmcp.workspace import Workspace

WORKSPACE_ID = "ws_0123456789abcdef01234567"
OTHER_WORKSPACE_ID = "ws_999999999999999999999999"
NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def _record(
    content: str,
    *,
    archived: bool = False,
    raw_path: str | None = None,
    context_note: str | None = None,
):
    return {
        "record_type": "decision",
        "legacy_type": None,
        "content": content,
        "rationale": "tested",
        "context": {} if context_note is None else {"note": context_note},
        "tags": ["v7"],
        "file_path": raw_path,
        "file_path_relative": "docs/decision.md",
        "keywords": None,
        "is_permanent": False,
        "pinned": False,
        "archived": archived,
        "outcome": None,
        "worked": None,
        "recall_count": 0,
        "surprise_score": None,
        "importance_score": None,
        "source_client": "test",
        "source_model": None,
        "deleted_at_us": None,
    }


class _Fixture:
    def __init__(self, temporary: Path) -> None:
        from daem0nmcp.migrations.schema import MIGRATIONS
        from daem0nmcp.schema_version import CURRENT_SCHEMA_VERSION
        from daem0nmcp.storage_activation import (
            ActiveDatabasePointer,
            write_active_pointer,
        )

        self.root = temporary / "workspace"
        self.root.mkdir()
        self.storage = self.root / ".daem0nmcp" / "storage"
        self.storage.mkdir(parents=True)
        self.database = self.storage / "daem0nmcp.db"
        connection = sqlite3.connect(self.database)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
        for version, _description, statements in MIGRATIONS:
            if not 16 <= version <= CURRENT_SCHEMA_VERSION:
                continue
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_version(version) VALUES (?)", (version,)
            )
        connection.commit()
        connection.close()
        write_active_pointer(
            self.storage,
            ActiveDatabasePointer(
                format_version=7,
                generation=1,
                active_db=self.database.name,
                previous_db=None,
                migration_run_id=None,
            ),
        )
        self.workspace = Workspace(WORKSPACE_ID, self.root)

    def append(
        self,
        content: str,
        *,
        occurred_at_us: int,
        recorded_at_us: int,
        raw_path: str | None = None,
        context_note: str | None = None,
    ) -> str:
        from daem0nmcp.event_store import EventCommand, EventStore

        connection = sqlite3.connect(self.database)
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            result = EventStore(connection).append_and_project(
                EventCommand(
                    workspace_id=WORKSPACE_ID,
                    stream_id="mem_" + "a" * 64,
                    stream_kind="memory",
                    event_type=(
                        "memory.created"
                        if connection.execute(
                            "SELECT COUNT(*) FROM memory_events"
                        ).fetchone()[0]
                        == 0
                        else "memory.updated"
                    ),
                    occurred_at_us=occurred_at_us,
                    recorded_at_us=recorded_at_us,
                    actor_type="client",
                    payload={
                        "record": _record(
                            content,
                            raw_path=raw_path,
                            context_note=context_note,
                        )
                    },
                )
            )
            connection.commit()
            return result.event_id
        finally:
            connection.close()


def _request(tool_name: str, **arguments: object) -> AdmittedRequest:
    from daem0nmcp.api.v7.tools import TOOL_INPUT_MODELS

    validated = TOOL_INPUT_MODELS[tool_name].model_validate(arguments)
    effective = validated.model_dump(mode="python")
    effective.pop("preflight_token", None)
    return AdmittedRequest(tool_name, MappingProxyType(effective))


class CoreOperationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from daem0nmcp.api.v7.operations import CoreOperationDependencies
        from daem0nmcp.covenant import (
            CapabilityAuthority,
            CovenantGate,
            CovenantStateStore,
            InvocationScope,
        )

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = _Fixture(Path(self.temporary.name))
        self.scope = InvocationScope("principal", "session", self.fixture.root)
        self.gate = CovenantGate(
            state_store=CovenantStateStore(clock=lambda: 1_767_323_045),
            authority=CapabilityAuthority(
                secret=b"x" * 32,
                kid="test",
                clock=lambda: 1_767_323_045,
                ttl_seconds=300,
            ),
        )
        self.dependencies = CoreOperationDependencies(
            covenant_gate=self.gate,
            scope_provider=lambda: self.scope,
            storage_path_resolver=lambda _workspace: self.fixture.storage,
            clock=lambda: NOW,
        )

    def _operations(self):
        from daem0nmcp.api.v7.operations import build_core_operations

        return build_core_operations(self.dependencies)

    async def test_registry_is_exact_and_immutable(self) -> None:
        operations = self._operations()

        self.assertEqual(
            {
                "covenant_status",
                "memory_at_time_get",
                "memory_versions_list",
                "projection_rebuild",
                "workspace_export",
                "workspace_import",
            },
            set(operations),
        )
        with self.assertRaises(TypeError):
            operations["extra"] = lambda: None  # type: ignore[index]

    async def test_covenant_status_is_recovery_safe_and_never_exposes_grants(
        self,
    ) -> None:
        operation = self._operations()["covenant_status"]
        request = _request("covenant_status", workspace_id=WORKSPACE_ID)

        before = await operation(workspace=self.fixture.workspace, request=request)
        self.gate.record_briefing(self.scope)
        after = await operation(workspace=self.fixture.workspace, request=request)

        self.assertFalse(before.briefed)
        self.assertEqual("session_brief", before.next_step.tool)
        self.assertEqual(300, before.token_ttl_seconds)
        self.assertTrue(after.briefed)
        self.assertEqual(
            datetime.fromtimestamp(1_767_323_045, tz=timezone.utc),
            after.briefed_at,
        )
        self.assertIsNone(after.next_step)
        self.assertNotIn("capabil", str(after.model_dump()).casefold())

    async def test_versions_are_hash_bound_path_redacted_and_cursor_bounded(
        self,
    ) -> None:
        first = self.fixture.append(
            "first decision",
            occurred_at_us=1_700_000_000_000_000,
            recorded_at_us=1_700_000_000_000_100,
        )
        second = self.fixture.append(
            "second decision",
            occurred_at_us=1_700_000_100_000_000,
            recorded_at_us=1_700_000_100_000_100,
        )
        operation = self._operations()["memory_versions_list"]
        first_page = await operation(
            workspace=self.fixture.workspace,
            request=_request(
                "memory_versions_list",
                workspace_id=WORKSPACE_ID,
                record_id="mem_" + "a" * 64,
                limit=1,
            ),
        )
        second_page = await operation(
            workspace=self.fixture.workspace,
            request=_request(
                "memory_versions_list",
                workspace_id=WORKSPACE_ID,
                record_id="mem_" + "a" * 64,
                cursor=first_page.next_cursor,
                limit=1,
            ),
        )

        self.assertEqual([first], [item.event_id for item in first_page.items])
        self.assertEqual([second], [item.event_id for item in second_page.items])
        self.assertTrue(first_page.truncated)
        self.assertIsNotNone(first_page.next_cursor)
        assert first_page.next_cursor is not None
        self.assertNotIn(first[4:], first_page.next_cursor)
        from daem0nmcp.api.v7.operations import build_core_operations

        alternate = build_core_operations(
            replace(self.dependencies, cursor_secret=b"y" * 32)
        )
        alternate_page = await alternate["memory_versions_list"](
            workspace=self.fixture.workspace,
            request=_request(
                "memory_versions_list",
                workspace_id=WORKSPACE_ID,
                record_id="mem_" + "a" * 64,
                limit=1,
            ),
        )
        self.assertNotEqual(first_page.next_cursor, alternate_page.next_cursor)
        self.assertFalse(second_page.truncated)
        self.assertRegex(first_page.items[0].version_id, r"^ver_[0-9a-f]{64}$")
        rendered = str(first_page.model_dump()) + str(second_page.model_dump())
        self.assertNotIn(str(self.fixture.root), rendered)
        self.assertNotIn(str(self.fixture.root), rendered)

    async def test_at_time_honors_valid_and_transaction_time_with_evidence(
        self,
    ) -> None:
        first = self.fixture.append(
            "old truth",
            occurred_at_us=1_700_000_000_000_000,
            recorded_at_us=1_700_000_000_000_100,
        )
        self.fixture.append(
            "new truth",
            occurred_at_us=1_700_000_100_000_000,
            recorded_at_us=1_700_000_100_000_100,
        )
        result = await self._operations()["memory_at_time_get"](
            workspace=self.fixture.workspace,
            request=_request(
                "memory_at_time_get",
                workspace_id=WORKSPACE_ID,
                record_id="mem_" + "a" * 64,
                valid_time="2023-11-14T22:13:50Z",
                transaction_time="2023-11-14T22:13:50Z",
            ),
        )

        self.assertEqual("old truth", result.record.excerpt)
        self.assertEqual(first, result.evidence_refs[0].event_id)
        self.assertEqual(result.version_id, result.evidence_refs[0].version_id)
        self.assertEqual("temporal", result.evidence_refs[0].provider)

    async def test_tampered_event_history_fails_closed(self) -> None:
        self.fixture.append(
            "trusted",
            occurred_at_us=1_700_000_000_000_000,
            recorded_at_us=1_700_000_000_000_100,
        )
        connection = sqlite3.connect(self.fixture.database)
        connection.execute("DROP TRIGGER memory_events_no_update")
        connection.execute(
            "UPDATE memory_events SET payload_json='{}' WHERE workspace_id=?",
            (WORKSPACE_ID,),
        )
        connection.commit()
        connection.close()

        with self.assertRaises(Exception) as caught:
            await self._operations()["memory_versions_list"](
                workspace=self.fixture.workspace,
                request=_request(
                    "memory_versions_list",
                    workspace_id=WORKSPACE_ID,
                    record_id="mem_" + "a" * 64,
                ),
            )
        self.assertEqual("IMPORT_INVALID", caught.exception.code)

    async def test_projection_rebuild_returns_typed_manifest_counts(self) -> None:
        self.fixture.append(
            "projection source",
            occurred_at_us=1_700_000_000_000_000,
            recorded_at_us=1_700_000_000_000_100,
        )
        result = await self._operations()["projection_rebuild"](
            workspace=self.fixture.workspace,
            request=_request(
                "projection_rebuild",
                workspace_id=WORKSPACE_ID,
                projection="lexical",
            ),
        )

        self.assertEqual("lexical", result.manifest.projection)
        self.assertEqual(1, result.manifest.generation)
        self.assertEqual(1, result.counts["rows"])
        self.assertEqual(1, result.counts["source_events"])
        self.assertNotIn(str(self.fixture.root), str(result.model_dump()))

    async def test_projection_retry_reuses_current_unless_force_is_true(self) -> None:
        self.fixture.append(
            "projection source",
            occurred_at_us=1_700_000_000_000_000,
            recorded_at_us=1_700_000_000_000_100,
        )
        operation = self._operations()["projection_rebuild"]
        arguments = {
            "workspace_id": WORKSPACE_ID,
            "projection": "lexical",
        }

        first = await operation(
            workspace=self.fixture.workspace,
            request=_request("projection_rebuild", **arguments),
        )
        replay = await operation(
            workspace=self.fixture.workspace,
            request=_request("projection_rebuild", **arguments),
        )
        forced = await operation(
            workspace=self.fixture.workspace,
            request=_request("projection_rebuild", **arguments, force=True),
        )

        self.assertEqual(1, first.manifest.generation)
        self.assertEqual(1, replay.manifest.generation)
        self.assertEqual("PROJECTION_CURRENT", replay.diagnostics[0].code)
        self.assertEqual(2, forced.manifest.generation)

    async def test_late_cancelled_projection_rebuild_returns_committed_result(
        self,
    ) -> None:
        from unittest.mock import patch

        from daem0nmcp.api.v7 import operations as operations_module

        self.fixture.append(
            "projection cancellation source",
            occurred_at_us=1_700_000_000_000_000,
            recorded_at_us=1_700_000_000_000_100,
        )
        original_projection = operations_module._projection_sync
        committed = threading.Event()
        release = threading.Event()

        def blocking_after_commit(*args, **kwargs):
            result = original_projection(*args, **kwargs)
            committed.set()
            release.wait(2)
            return result

        with patch.object(
            operations_module,
            "_projection_sync",
            side_effect=blocking_after_commit,
        ):
            task = asyncio.create_task(
                self._operations()["projection_rebuild"](
                    workspace=self.fixture.workspace,
                    request=_request(
                        "projection_rebuild",
                        workspace_id=WORKSPACE_ID,
                        projection="lexical",
                    ),
                )
            )
            self.assertTrue(await asyncio.to_thread(committed.wait, 2))
            task.cancel()
            await asyncio.sleep(0.02)
            self.assertFalse(task.done())
            release.set()
            result = await task

        self.assertEqual(result.manifest.projection, "lexical")
        self.assertEqual(result.manifest.generation, 1)
        connection = sqlite3.connect(self.fixture.database)
        try:
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT COUNT(*) FROM projection_manifests "
                    "WHERE workspace_id=? AND projection_name='lexical' "
                    "AND status='active'",
                    (WORKSPACE_ID,),
                ).fetchone()[0],
            )
        finally:
            connection.close()

    async def test_export_round_trip_is_hash_exact_and_import_is_replay_safe(
        self,
    ) -> None:
        event_id = self.fixture.append(
            "portable",
            occurred_at_us=1_700_000_000_000_000,
            recorded_at_us=1_700_000_000_000_100,
        )
        operations = self._operations()
        bundle = await operations["workspace_export"](
            workspace=self.fixture.workspace,
            request=_request(
                "workspace_export",
                workspace_id=WORKSPACE_ID,
                include_legacy_projection=False,
                include_vectors=False,
            ),
        )
        self.assertEqual("7", bundle.api_version)
        self.assertEqual([event_id], [event.event_id for event in bundle.events])
        self.assertFalse(bundle.legacy_projection_included)
        self.assertFalse(bundle.vectors_included)

        target_temp = tempfile.TemporaryDirectory()
        self.addCleanup(target_temp.cleanup)
        target = _Fixture(Path(target_temp.name))
        target_dependencies = type(self.dependencies)(
            covenant_gate=self.gate,
            scope_provider=lambda: self.scope,
            storage_path_resolver=lambda _workspace: target.storage,
            clock=lambda: NOW,
        )
        from daem0nmcp.api.v7.operations import build_core_operations

        importer = build_core_operations(target_dependencies)["workspace_import"]
        arguments = {
            "workspace_id": WORKSPACE_ID,
            "bundle": bundle.model_dump(mode="python"),
            "merge": True,
            "idempotency_key": "import-portable-0001",
            "preflight_token": "token_value_for_test",
        }
        first = await importer(
            workspace=target.workspace,
            request=_request("workspace_import", **arguments),
        )
        replay = await importer(
            workspace=target.workspace,
            request=_request("workspace_import", **arguments),
        )

        self.assertEqual((1, 0), (first.imported, first.skipped))
        self.assertEqual((0, 1), (replay.imported, replay.skipped))
        self.assertEqual([event_id], first.event_ids)

        from daem0nmcp.api.v7.operations import CoreOperationError

        with self.assertRaises(CoreOperationError) as caught:
            await importer(
                workspace=target.workspace,
                request=_request(
                    "workspace_import",
                    **{**arguments, "merge": False},
                ),
            )
        self.assertEqual("IDEMPOTENCY_CONFLICT", caught.exception.code)

    async def test_export_import_preserves_signed_microseconds_exactly(self) -> None:
        self.fixture.append(
            "epoch precision",
            occurred_at_us=249,
            recorded_at_us=300,
        )
        bundle = await self._operations()["workspace_export"](
            workspace=self.fixture.workspace,
            request=_request(
                "workspace_export",
                workspace_id=WORKSPACE_ID,
                include_legacy_projection=False,
            ),
        )
        target_temp = tempfile.TemporaryDirectory()
        self.addCleanup(target_temp.cleanup)
        target = _Fixture(Path(target_temp.name))
        dependencies = type(self.dependencies)(
            covenant_gate=self.gate,
            scope_provider=lambda: self.scope,
            storage_path_resolver=lambda _workspace: target.storage,
            clock=lambda: NOW,
        )
        from daem0nmcp.api.v7.operations import build_core_operations

        result = await build_core_operations(dependencies)["workspace_import"](
            workspace=target.workspace,
            request=_request(
                "workspace_import",
                workspace_id=WORKSPACE_ID,
                bundle=bundle.model_dump(mode="python"),
                idempotency_key="epoch-precision-0001",
                preflight_token="token_value_for_test",
            ),
        )

        self.assertEqual(1, result.imported)

    async def test_import_tamper_and_cross_workspace_roll_back_every_write(
        self,
    ) -> None:
        self.fixture.append(
            "portable",
            occurred_at_us=1_700_000_000_000_000,
            recorded_at_us=1_700_000_000_000_100,
        )
        operations = self._operations()
        bundle = await operations["workspace_export"](
            workspace=self.fixture.workspace,
            request=_request(
                "workspace_export",
                workspace_id=WORKSPACE_ID,
                include_legacy_projection=False,
            ),
        )
        bad = bundle.model_dump(mode="python")
        bad["events"][0]["payload"]["data"]["record"]["content"] = "tampered"
        bad_path = bundle.model_dump(mode="python")
        bad_path["events"][0]["payload"]["data"]["record"]["file_path"] = {}

        for candidate, code in (
            (bad, "IMPORT_INVALID"),
            (bad_path, "IMPORT_INVALID"),
            (
                {
                    **bundle.model_dump(mode="python"),
                    "workspace_id": OTHER_WORKSPACE_ID,
                },
                "CROSS_WORKSPACE_IMPORT_UNSUPPORTED",
            ),
        ):
            with self.subTest(code=code):
                with self.assertRaises(Exception) as caught:
                    await operations["workspace_import"](
                        workspace=self.fixture.workspace,
                        request=_request(
                            "workspace_import",
                            workspace_id=WORKSPACE_ID,
                            bundle=candidate,
                            merge=True,
                            idempotency_key="tamper-case-0001-" + code.casefold(),
                            preflight_token="token_value_for_test",
                        ),
                    )
                self.assertEqual(code, caught.exception.code)
                connection = sqlite3.connect(self.fixture.database)
                try:
                    self.assertEqual(
                        0,
                        connection.execute(
                            "SELECT COUNT(*) FROM background_jobs "
                            "WHERE job_type='v7.workspace_import'"
                        ).fetchone()[0],
                    )
                finally:
                    connection.close()

    async def test_import_stream_fork_is_owned_and_rolls_back_journal(self) -> None:
        from daem0nmcp.api.v7.operations import (
            CoreOperationError,
            build_core_operations,
        )

        self.fixture.append(
            "source branch",
            occurred_at_us=1_700_000_000_000_000,
            recorded_at_us=1_700_000_000_000_100,
        )
        bundle = await self._operations()["workspace_export"](
            workspace=self.fixture.workspace,
            request=_request(
                "workspace_export",
                workspace_id=WORKSPACE_ID,
                include_legacy_projection=False,
            ),
        )
        target_temp = tempfile.TemporaryDirectory()
        self.addCleanup(target_temp.cleanup)
        target = _Fixture(Path(target_temp.name))
        target.append(
            "target branch",
            occurred_at_us=1_700_000_000_000_000,
            recorded_at_us=1_700_000_000_000_100,
        )
        dependencies = type(self.dependencies)(
            covenant_gate=self.gate,
            scope_provider=lambda: self.scope,
            storage_path_resolver=lambda _workspace: target.storage,
            clock=lambda: NOW,
        )

        with self.assertRaises(CoreOperationError) as caught:
            await build_core_operations(dependencies)["workspace_import"](
                workspace=target.workspace,
                request=_request(
                    "workspace_import",
                    workspace_id=WORKSPACE_ID,
                    bundle=bundle.model_dump(mode="python"),
                    idempotency_key="stream-fork-0001",
                    preflight_token="token_value_for_test",
                ),
            )
        self.assertEqual("EVENT_STREAM_CONFLICT", caught.exception.code)
        connection = sqlite3.connect(target.database)
        try:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM background_jobs "
                    "WHERE job_type='v7.workspace_import'"
                ).fetchone()[0],
            )
        finally:
            connection.close()

    async def test_export_rejects_vectors_and_raw_paths_without_disclosure(
        self,
    ) -> None:
        secret = str(self.fixture.root / "private" / "secret.txt")
        self.fixture.append(
            "path source",
            occurred_at_us=1_700_000_000_000_000,
            recorded_at_us=1_700_000_000_000_100,
            raw_path=secret,
        )
        operation = self._operations()["workspace_export"]

        cases = ((True, "WORKSPACE_PATH_ESCAPE"), (False, "WORKSPACE_PATH_ESCAPE"))
        for include_vectors, code in cases:
            with self.subTest(include_vectors=include_vectors):
                with self.assertRaises(Exception) as caught:
                    await operation(
                        workspace=self.fixture.workspace,
                        request=_request(
                            "workspace_export",
                            workspace_id=WORKSPACE_ID,
                            include_vectors=include_vectors,
                        ),
                    )
                self.assertEqual(code, caught.exception.code)
                self.assertNotIn(secret, str(caught.exception))

    async def test_oversized_export_uses_paged_snapshot_builder(self) -> None:
        from contextlib import contextmanager

        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = (10_001,)

        @contextmanager
        def active_connection(*_args, **_kwargs):
            yield connection

        with (
            patch(
                "daem0nmcp.api.v7.operations._active_connection",
                side_effect=active_connection,
            ),
            patch(
                "daem0nmcp.api.v7.operations.create_export_session",
                return_value={
                    "export_session_id": "xpt_" + "1" * 64,
                    "manifest_hash": "2" * 64,
                    "manifest": {
                        "bundle_version": 2,
                        "workspace_id": WORKSPACE_ID,
                        "event_root_hash": "3" * 64,
                        "event_count": 10_001,
                        "legacy_projection": None,
                        "vectors": None,
                        "pages": [
                            {
                                "page_index": 0,
                                "page_kind": "events",
                                "item_count": 0,
                                "byte_count": 28,
                                "page_hash": "4" * 64,
                            }
                        ],
                    },
                    "page_index": 0,
                    "page_count": 1,
                    "page_kind": "events",
                    "page_hash": "4" * 64,
                    "next_cursor": None,
                    "complete": True,
                    "content": {"kind": "events", "items": []},
                },
            ) as snapshot_builder,
        ):
            bundle = await self._operations()["workspace_export"](
                workspace=self.fixture.workspace,
                request=_request(
                    "workspace_export",
                    workspace_id=WORKSPACE_ID,
                    include_legacy_projection=False,
                ),
            )

        self.assertEqual(2, bundle.bundle_version)
        self.assertEqual(10_001, bundle.manifest["event_count"])
        snapshot_builder.assert_called_once()

    async def test_export_rejects_nested_absolute_path_strings(self) -> None:
        secret = str(self.fixture.root / "private" / "nested-secret.txt")
        self.fixture.append(
            "nested path source",
            occurred_at_us=1_700_000_000_000_000,
            recorded_at_us=1_700_000_000_000_100,
            context_note=secret,
        )

        with self.assertRaises(Exception) as caught:
            await self._operations()["workspace_export"](
                workspace=self.fixture.workspace,
                request=_request("workspace_export", workspace_id=WORKSPACE_ID),
            )

        self.assertEqual("WORKSPACE_PATH_ESCAPE", caught.exception.code)
        self.assertNotIn(secret, str(caught.exception))

    async def test_cancelled_sqlite_work_propagates_and_finishes_privately(
        self,
    ) -> None:
        started = threading.Event()
        released = threading.Event()
        finished = threading.Event()

        def slow_storage(_workspace: Workspace) -> Path:
            started.set()
            released.wait(2)
            finished.set()
            return self.fixture.storage

        dependencies = type(self.dependencies)(
            covenant_gate=self.gate,
            scope_provider=lambda: self.scope,
            storage_path_resolver=slow_storage,
            clock=lambda: NOW,
        )
        from daem0nmcp.api.v7.operations import build_core_operations

        operation = build_core_operations(dependencies)["workspace_export"]
        task = asyncio.create_task(
            operation(
                workspace=self.fixture.workspace,
                request=_request("workspace_export", workspace_id=WORKSPACE_ID),
            )
        )
        await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0.02)
        self.assertFalse(
            task.done(),
            "cancellation returned while SQLite work was still detached",
        )
        released.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(finished.is_set())

        def wait_for_worker_lock_release() -> bool:
            from daem0nmcp.storage_activation import (
                DatabaseFileLock,
                DatabaseInUseError,
            )

            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                lock = DatabaseFileLock(self.fixture.storage, "exclusive")
                try:
                    lock.acquire()
                except DatabaseInUseError:
                    time.sleep(0.01)
                    continue
                lock.release()
                return True
            return False

        self.assertTrue(await asyncio.to_thread(wait_for_worker_lock_release))

    async def test_cancelled_workspace_import_rolls_back_before_commit(self) -> None:
        from unittest.mock import patch

        from daem0nmcp.api.v7 import operations as operations_module
        from daem0nmcp.api.v7.operations import build_core_operations

        self.fixture.append(
            "source branch",
            occurred_at_us=1_700_000_000_000_000,
            recorded_at_us=1_700_000_000_000_100,
        )
        bundle = await self._operations()["workspace_export"](
            workspace=self.fixture.workspace,
            request=_request(
                "workspace_export",
                workspace_id=WORKSPACE_ID,
                include_legacy_projection=False,
            ),
        )
        target_temp = tempfile.TemporaryDirectory()
        self.addCleanup(target_temp.cleanup)
        target = _Fixture(Path(target_temp.name))
        dependencies = type(self.dependencies)(
            covenant_gate=self.gate,
            scope_provider=lambda: self.scope,
            storage_path_resolver=lambda _workspace: target.storage,
            clock=lambda: NOW,
        )
        original_append = operations_module.EventStore.append_and_project
        staged = threading.Event()
        release = threading.Event()

        def blocking_append(*args, **kwargs):
            result = original_append(*args, **kwargs)
            staged.set()
            release.wait(2)
            return result

        with patch.object(
            operations_module.EventStore,
            "append_and_project",
            autospec=True,
            side_effect=blocking_append,
        ):
            task = asyncio.create_task(
                build_core_operations(dependencies)["workspace_import"](
                    workspace=target.workspace,
                    request=_request(
                        "workspace_import",
                        workspace_id=WORKSPACE_ID,
                        bundle=bundle.model_dump(mode="python"),
                        idempotency_key="cancel-import-0001",
                        preflight_token="token_value_for_test",
                    ),
                )
            )
            self.assertTrue(await asyncio.to_thread(staged.wait, 2))
            task.cancel()
            await asyncio.sleep(0.02)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

        connection = sqlite3.connect(target.database)
        try:
            self.assertEqual(
                0,
                connection.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0],
            )
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM background_jobs "
                    "WHERE job_type='v7.workspace_import'"
                ).fetchone()[0],
            )
        finally:
            connection.close()

    async def test_late_cancelled_workspace_import_returns_committed_receipt(
        self,
    ) -> None:
        from unittest.mock import patch

        from daem0nmcp.api.v7 import operations as operations_module
        from daem0nmcp.api.v7.operations import build_core_operations

        self.fixture.append(
            "committed source branch",
            occurred_at_us=1_700_000_000_000_000,
            recorded_at_us=1_700_000_000_000_100,
        )
        bundle = await self._operations()["workspace_export"](
            workspace=self.fixture.workspace,
            request=_request(
                "workspace_export",
                workspace_id=WORKSPACE_ID,
                include_legacy_projection=False,
            ),
        )
        target_temp = tempfile.TemporaryDirectory()
        self.addCleanup(target_temp.cleanup)
        target = _Fixture(Path(target_temp.name))
        dependencies = type(self.dependencies)(
            covenant_gate=self.gate,
            scope_provider=lambda: self.scope,
            storage_path_resolver=lambda _workspace: target.storage,
            clock=lambda: NOW,
        )
        original_import = operations_module._import_sync
        committed = threading.Event()
        release = threading.Event()

        def blocking_after_commit(*args, **kwargs):
            result = original_import(*args, **kwargs)
            committed.set()
            release.wait(2)
            return result

        with patch.object(
            operations_module,
            "_import_sync",
            side_effect=blocking_after_commit,
        ):
            task = asyncio.create_task(
                build_core_operations(dependencies)["workspace_import"](
                    workspace=target.workspace,
                    request=_request(
                        "workspace_import",
                        workspace_id=WORKSPACE_ID,
                        bundle=bundle.model_dump(mode="python"),
                        idempotency_key="late-cancel-import-0001",
                        preflight_token="token_value_for_test",
                    ),
                )
            )
            self.assertTrue(await asyncio.to_thread(committed.wait, 2))
            task.cancel()
            await asyncio.sleep(0.02)
            self.assertFalse(task.done())
            release.set()
            receipt = await task

        self.assertEqual(receipt.imported, 1)
        connection = sqlite3.connect(target.database)
        try:
            self.assertEqual(
                1,
                connection.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0],
            )
        finally:
            connection.close()

    async def test_direct_cross_workspace_request_is_rejected_before_io(self) -> None:
        with self.assertRaises(Exception) as caught:
            await self._operations()["workspace_export"](
                workspace=self.fixture.workspace,
                request=_request("workspace_export", workspace_id=OTHER_WORKSPACE_ID),
            )
        self.assertEqual("UNAUTHORIZED_WORKSPACE", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
