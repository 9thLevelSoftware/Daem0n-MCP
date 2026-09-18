from __future__ import annotations

import asyncio
import inspect
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

from daem0nmcp.api.v7.application import AdmittedRequest

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
PREFLIGHT_TOKEN = "t" * 32


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


class _Fixture:
    def __init__(self, temporary: Path) -> None:
        from daem0nmcp.storage_activation import (
            ActiveDatabasePointer,
            write_active_pointer,
        )
        from daem0nmcp.workspace import WorkspaceRegistry

        self.root = (temporary / "workspace").resolve()
        self.root.mkdir()
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

    def add_record(
        self,
        index: int,
        *,
        archived: bool = False,
        deleted_at_us: int | None = None,
    ) -> str:
        from daem0nmcp.event_store import EventCommand, EventStore

        record_id = f"mem_{index:064x}"
        timestamp = int(NOW.timestamp() * 1_000_000) + index
        state = {
            "record_type": "decision",
            "legacy_type": None,
            "content": f"canonical record {index}",
            "rationale": "local-state test",
            "context": {},
            "tags": ["active"],
            "file_path": None,
            "file_path_relative": "src/context.py",
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
            "deleted_at_us": deleted_at_us,
        }
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            EventStore(connection).append_and_project(
                EventCommand(
                    workspace_id=self.workspace.workspace_id,
                    stream_id=record_id,
                    stream_kind="memory",
                    event_type="memory.created",
                    occurred_at_us=timestamp,
                    recorded_at_us=timestamp,
                    actor_type="client",
                    payload={"record": state},
                )
            )
            connection.commit()
        return record_id


class LocalStateOperationContractTests(unittest.TestCase):
    def test_registry_is_exact_immutable_and_keyword_only(self) -> None:
        """A mutable or positional registry could bypass v7 routing policy."""
        from daem0nmcp.api.v7.local_state_operations import (
            LocalStateOperationDependencies,
            build_local_state_operations,
        )

        operations = build_local_state_operations(
            LocalStateOperationDependencies(token_secret=b"s" * 32)
        )

        self.assertEqual(
            {
                "active_context_add",
                "active_context_clear",
                "active_context_list",
                "active_context_remove",
            },
            set(operations),
        )
        with self.assertRaises(TypeError):
            operations["extra"] = object()  # type: ignore[index]
        for operation in operations.values():
            parameters = tuple(inspect.signature(operation).parameters.values())
            self.assertEqual(
                ("workspace", "request"),
                tuple(parameter.name for parameter in parameters),
            )
            self.assertTrue(
                all(
                    parameter.kind is inspect.Parameter.KEYWORD_ONLY
                    for parameter in parameters
                )
            )


class ActiveContextOperationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = _Fixture(Path(self.temporary.name))

    def _operations(self, **changes: object):
        from daem0nmcp.api.v7.local_state_operations import (
            LocalStateOperationDependencies,
            build_local_state_operations,
        )

        options: dict[str, object] = {
            "clock": lambda: NOW,
            "selection_ttl_seconds": 300,
            "token_secret": b"active-context-operation-secret-01",
        }
        options.update(changes)
        return build_local_state_operations(LocalStateOperationDependencies(**options))

    def _add_request(
        self,
        record_id: str,
        *,
        reason: str | None = "Current focus",
        priority: int = 0,
        expires_at: datetime | None = None,
    ) -> AdmittedRequest:
        return _request(
            "active_context_add",
            workspace_id=self.fixture.workspace.workspace_id,
            record_id=record_id,
            reason=reason,
            priority=priority,
            expires_at=expires_at,
            preflight_token=PREFLIGHT_TOKEN,
        )

    async def test_add_uses_fresh_canonical_record_and_is_set_idempotent(self) -> None:
        """Fresh Task7 records must activate without a retained integer row."""
        from daem0nmcp.api.v7.active_context_storage import (
            active_context_id_for_record,
        )
        from daem0nmcp.api.v7.resources import ActiveContextItem

        record_id = self.fixture.add_record(1)
        operation = self._operations()["active_context_add"]
        request = self._add_request(record_id, priority=4)

        created = await operation(
            workspace=self.fixture.workspace,
            request=request,
        )
        replay = await operation(
            workspace=self.fixture.workspace,
            request=request,
        )
        changed = await operation(
            workspace=self.fixture.workspace,
            request=self._add_request(
                record_id,
                reason="Changed focus",
                priority=9,
            ),
        )

        self.assertIsInstance(created, ActiveContextItem)
        self.assertEqual(created, replay)
        self.assertEqual(
            active_context_id_for_record(
                self.fixture.workspace.workspace_id,
                record_id,
            ),
            created.active_context_id,
        )
        self.assertEqual(record_id, created.record.record_id)
        self.assertEqual(9, changed.priority)
        self.assertEqual("Changed focus", changed.reason)
        self.assertNotIn(str(self.fixture.root), changed.model_dump_json())
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            self.assertEqual(
                (1, 0),
                connection.execute(
                    "SELECT count(*),COALESCE(sum(removed_at_us IS NOT NULL),0) "
                    "FROM active_context_entries"
                ).fetchone(),
            )
            legacy_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='active_context'"
            ).fetchone()
            if legacy_table is not None:
                self.assertEqual(
                    0,
                    connection.execute(
                        "SELECT count(*) FROM active_context"
                    ).fetchone()[0],
                )

    async def test_mutations_append_canonical_observable_events(self) -> None:
        """State changes must be discoverable without polling projection tables."""
        operations = self._operations()
        record_id = self.fixture.add_record(1)
        created = await operations["active_context_add"](
            workspace=self.fixture.workspace,
            request=self._add_request(record_id, priority=4),
        )
        await operations["active_context_add"](
            workspace=self.fixture.workspace,
            request=self._add_request(record_id, priority=9),
        )
        await operations["active_context_add"](
            workspace=self.fixture.workspace,
            request=self._add_request(record_id, priority=9),
        )
        removed = await operations["active_context_remove"](
            workspace=self.fixture.workspace,
            request=_request(
                "active_context_remove",
                workspace_id=self.fixture.workspace.workspace_id,
                active_context_id=created.active_context_id,
                preflight_token=PREFLIGHT_TOKEN,
            ),
        )

        with closing(sqlite3.connect(self.fixture.database)) as connection:
            rows = connection.execute(
                "SELECT event_id,stream_id,stream_version,event_type,payload_json "
                "FROM governance_events ORDER BY stream_version"
            ).fetchall()
        self.assertEqual(
            [
                "active_context.added",
                "active_context.updated",
                "active_context.removed",
            ],
            [row[3] for row in rows],
        )
        self.assertEqual([1, 2, 3], [row[2] for row in rows])
        self.assertTrue(all(row[1] == created.active_context_id for row in rows))
        self.assertEqual([rows[-1][0]], removed.event_ids)
        self.assertNotIn(str(self.fixture.root), "".join(row[4] for row in rows))

    async def test_active_event_appears_in_session_updates(self) -> None:
        """Session polling must merge governance and memory event timelines."""
        from daem0nmcp.api.v7.record_operations import (
            RecordOperationDependencies,
            build_record_operations,
        )

        record_id = self.fixture.add_record(1)
        session_updates = build_record_operations(
            RecordOperationDependencies(clock=lambda: NOW)
        )["session_updates_get"]
        before = await session_updates(
            workspace=self.fixture.workspace,
            request=_request(
                "session_updates_get",
                workspace_id=self.fixture.workspace.workspace_id,
            ),
        )
        added = await self._operations()["active_context_add"](
            workspace=self.fixture.workspace,
            request=self._add_request(record_id),
        )
        after = await session_updates(
            workspace=self.fixture.workspace,
            request=_request(
                "session_updates_get",
                workspace_id=self.fixture.workspace.workspace_id,
                after_cursor=before.cursor,
            ),
        )

        self.assertTrue(after.changed)
        self.assertEqual(1, len(after.events))
        self.assertEqual("active_context", after.events[0].kind)
        self.assertEqual(added.active_context_id, after.events[0].object_id)

    async def test_list_has_signed_pagination_and_one_snapshot_token(self) -> None:
        """Pagination and destructive selection must bind one deterministic snapshot."""
        from daem0nmcp.api.v7.local_state_operations import (
            LocalStateOperationError,
        )
        from daem0nmcp.api.v7.tools import ActiveContextPage

        operations = self._operations()
        added = []
        for index, priority in enumerate((30, 20, 10), start=1):
            record_id = self.fixture.add_record(index)
            added.append(
                await operations["active_context_add"](
                    workspace=self.fixture.workspace,
                    request=self._add_request(record_id, priority=priority),
                )
            )

        list_times_end = NOW + timedelta(seconds=1)
        list_times = [NOW, list_times_end]
        list_operations = self._operations(
            clock=lambda: list_times.pop(0) if list_times else list_times_end
        )

        first = await list_operations["active_context_list"](
            workspace=self.fixture.workspace,
            request=_request(
                "active_context_list",
                workspace_id=self.fixture.workspace.workspace_id,
                limit=1,
            ),
        )
        second = await list_operations["active_context_list"](
            workspace=self.fixture.workspace,
            request=_request(
                "active_context_list",
                workspace_id=self.fixture.workspace.workspace_id,
                cursor=first.next_cursor,
                limit=2,
            ),
        )

        self.assertIsInstance(first, ActiveContextPage)
        self.assertEqual([added[0]], first.items)
        self.assertEqual(added[1:], second.items)
        self.assertTrue(first.truncated)
        self.assertFalse(second.truncated)
        self.assertEqual(first.selection_token, second.selection_token)
        self.assertRegex(
            first.selection_token,
            r"^sel_[0-9a-f]{64}_[0-9a-f]{1,16}_[0-9a-f]{64}$",
        )

        assert first.next_cursor is not None
        tampered = first.next_cursor[:-1] + (
            "0" if first.next_cursor[-1] != "0" else "1"
        )
        with self.assertRaises(LocalStateOperationError) as caught:
            await list_operations["active_context_list"](
                workspace=self.fixture.workspace,
                request=_request(
                    "active_context_list",
                    workspace_id=self.fixture.workspace.workspace_id,
                    cursor=tampered,
                    limit=1,
                ),
            )
        self.assertEqual("INVALID_ARGUMENT", caught.exception.code)

    async def test_remove_is_soft_replay_safe_and_reactivation_keeps_identity(
        self,
    ) -> None:
        """Removal must retain durable identity and a repeated request must be safe."""
        from daem0nmcp.api.v7.models import MutationReceipt

        operations = self._operations()
        record_id = self.fixture.add_record(1)
        added = await operations["active_context_add"](
            workspace=self.fixture.workspace,
            request=self._add_request(record_id),
        )
        request = _request(
            "active_context_remove",
            workspace_id=self.fixture.workspace.workspace_id,
            active_context_id=added.active_context_id,
            preflight_token=PREFLIGHT_TOKEN,
        )

        removed = await operations["active_context_remove"](
            workspace=self.fixture.workspace,
            request=request,
        )
        replay = await operations["active_context_remove"](
            workspace=self.fixture.workspace,
            request=request,
        )
        reactivated = await operations["active_context_add"](
            workspace=self.fixture.workspace,
            request=self._add_request(record_id, reason="Back in focus"),
        )

        self.assertIsInstance(removed, MutationReceipt)
        self.assertEqual([added.active_context_id], removed.affected_ids)
        self.assertEqual({"removed": 1}, removed.counts)
        self.assertFalse(removed.idempotent_replay)
        self.assertEqual({"removed": 0}, replay.counts)
        self.assertTrue(replay.idempotent_replay)
        self.assertEqual(added.active_context_id, reactivated.active_context_id)
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            self.assertEqual(
                (1, None),
                connection.execute(
                    "SELECT count(*),max(removed_at_us) FROM active_context_entries"
                ).fetchone(),
            )

    async def test_clear_rejects_stale_snapshot_and_replays_without_new_deletes(
        self,
    ) -> None:
        """An old clear token must never remove context added after its listing."""
        from daem0nmcp.api.v7.local_state_operations import (
            LocalStateOperationError,
        )
        from daem0nmcp.api.v7.models import DestructiveMutationReceipt

        operations = self._operations()
        first_ids = []
        for index in (1, 2):
            record_id = self.fixture.add_record(index)
            first_ids.append(
                (
                    await operations["active_context_add"](
                        workspace=self.fixture.workspace,
                        request=self._add_request(record_id, priority=index),
                    )
                ).active_context_id
            )
        stale_page = await operations["active_context_list"](
            workspace=self.fixture.workspace,
            request=_request(
                "active_context_list",
                workspace_id=self.fixture.workspace.workspace_id,
                limit=100,
            ),
        )
        third_record = self.fixture.add_record(3)
        third = await operations["active_context_add"](
            workspace=self.fixture.workspace,
            request=self._add_request(third_record, priority=3),
        )

        with self.assertRaises(LocalStateOperationError) as caught:
            await operations["active_context_clear"](
                workspace=self.fixture.workspace,
                request=_request(
                    "active_context_clear",
                    workspace_id=self.fixture.workspace.workspace_id,
                    selection_token=stale_page.selection_token,
                    preflight_token=PREFLIGHT_TOKEN,
                ),
            )
        self.assertEqual("CONFLICT", caught.exception.code)

        current_page = await operations["active_context_list"](
            workspace=self.fixture.workspace,
            request=_request(
                "active_context_list",
                workspace_id=self.fixture.workspace.workspace_id,
                limit=100,
            ),
        )
        clear_request = _request(
            "active_context_clear",
            workspace_id=self.fixture.workspace.workspace_id,
            selection_token=current_page.selection_token,
            preflight_token=PREFLIGHT_TOKEN,
        )
        cleared = await operations["active_context_clear"](
            workspace=self.fixture.workspace,
            request=clear_request,
        )
        replay = await operations["active_context_clear"](
            workspace=self.fixture.workspace,
            request=clear_request,
        )
        reactivated = await operations["active_context_add"](
            workspace=self.fixture.workspace,
            request=self._add_request(third_record, reason="New incarnation"),
        )
        replay_after_reactivation = await operations["active_context_clear"](
            workspace=self.fixture.workspace,
            request=clear_request,
        )

        self.assertIsInstance(cleared, DestructiveMutationReceipt)
        self.assertEqual(
            set(first_ids + [third.active_context_id]),
            set(cleared.affected_ids),
        )
        self.assertEqual(
            (3, 3, 0),
            (
                cleared.selected_count,
                cleared.changed_count,
                cleared.skipped_count,
            ),
        )
        self.assertFalse(cleared.idempotent_replay)
        self.assertTrue(replay.idempotent_replay)
        self.assertTrue(replay_after_reactivation.idempotent_replay)
        self.assertEqual(reactivated.active_context_id, third.active_context_id)
        self.assertEqual(3, len(cleared.event_ids))
        self.assertEqual(cleared.event_ids, replay.event_ids)
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT removed_at_us FROM active_context_entries "
                    "WHERE active_context_id=?",
                    (third.active_context_id,),
                ).fetchone()[0]
            )

    async def test_expired_add_and_tampered_selection_fail_closed(self) -> None:
        """Invisible additions and unauthenticated destructive tokens are rejected."""
        from daem0nmcp.api.v7.local_state_operations import (
            LocalStateOperationError,
        )

        operations = self._operations()
        record_id = self.fixture.add_record(1)
        with self.assertRaises(LocalStateOperationError) as caught:
            await operations["active_context_add"](
                workspace=self.fixture.workspace,
                request=self._add_request(
                    record_id,
                    expires_at=NOW - timedelta(seconds=1),
                ),
            )
        self.assertEqual("INVALID_ARGUMENT", caught.exception.code)

        page = await operations["active_context_list"](
            workspace=self.fixture.workspace,
            request=_request(
                "active_context_list",
                workspace_id=self.fixture.workspace.workspace_id,
            ),
        )
        tampered = page.selection_token[:-1] + (
            "0" if page.selection_token[-1] != "0" else "1"
        )
        with self.assertRaises(LocalStateOperationError) as caught:
            await operations["active_context_clear"](
                workspace=self.fixture.workspace,
                request=_request(
                    "active_context_clear",
                    workspace_id=self.fixture.workspace.workspace_id,
                    selection_token=tampered,
                    preflight_token=PREFLIGHT_TOKEN,
                ),
            )
        self.assertEqual("TOKEN_TAMPERED", caught.exception.code)

    async def test_selection_token_expires_at_the_configured_bound(self) -> None:
        """A signed snapshot must not remain destructively usable forever."""
        from daem0nmcp.api.v7.local_state_operations import (
            LocalStateOperationError,
        )

        record_id = self.fixture.add_record(1)
        operations = self._operations()
        await operations["active_context_add"](
            workspace=self.fixture.workspace,
            request=self._add_request(record_id),
        )
        page = await operations["active_context_list"](
            workspace=self.fixture.workspace,
            request=_request(
                "active_context_list",
                workspace_id=self.fixture.workspace.workspace_id,
            ),
        )
        expired_operations = self._operations(
            clock=lambda: NOW + timedelta(seconds=301)
        )
        with self.assertRaises(LocalStateOperationError) as caught:
            await expired_operations["active_context_clear"](
                workspace=self.fixture.workspace,
                request=_request(
                    "active_context_clear",
                    workspace_id=self.fixture.workspace.workspace_id,
                    selection_token=page.selection_token,
                    preflight_token=PREFLIGHT_TOKEN,
                ),
            )
        self.assertEqual("TOKEN_EXPIRED", caught.exception.code)

    async def test_expired_rows_do_not_exhaust_active_capacity(self) -> None:
        """Expired context must not consume the bounded current-entry quota."""
        first_record = self.fixture.add_record(1)
        second_record = self.fixture.add_record(2)
        with patch(
            "daem0nmcp.api.v7.local_state_operations._MAX_ACTIVE_ENTRIES",
            1,
        ):
            first_operations = self._operations()
            await first_operations["active_context_add"](
                workspace=self.fixture.workspace,
                request=self._add_request(
                    first_record,
                    expires_at=NOW + timedelta(seconds=1),
                ),
            )
            later_operations = self._operations(
                clock=lambda: NOW + timedelta(seconds=2)
            )
            added = await later_operations["active_context_add"](
                workspace=self.fixture.workspace,
                request=self._add_request(second_record),
            )
        self.assertEqual(second_record, added.record.record_id)

    async def test_reactivation_cannot_bypass_active_capacity(self) -> None:
        """A retained inactive row still needs capacity before reactivation."""
        from daem0nmcp.api.v7.local_state_operations import (
            LocalStateOperationError,
        )

        first_record = self.fixture.add_record(1)
        second_record = self.fixture.add_record(2)
        operations = self._operations()
        with patch(
            "daem0nmcp.api.v7.local_state_operations._MAX_ACTIVE_ENTRIES",
            1,
        ):
            first = await operations["active_context_add"](
                workspace=self.fixture.workspace,
                request=self._add_request(first_record),
            )
            await operations["active_context_remove"](
                workspace=self.fixture.workspace,
                request=_request(
                    "active_context_remove",
                    workspace_id=self.fixture.workspace.workspace_id,
                    active_context_id=first.active_context_id,
                    preflight_token=PREFLIGHT_TOKEN,
                ),
            )
            await operations["active_context_add"](
                workspace=self.fixture.workspace,
                request=self._add_request(second_record),
            )

            with self.assertRaises(LocalStateOperationError) as caught:
                await operations["active_context_add"](
                    workspace=self.fixture.workspace,
                    request=self._add_request(first_record),
                )

        self.assertEqual("CAPABILITY_DEGRADED", caught.exception.code)
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT count(*) FROM active_context_entries "
                    "WHERE removed_at_us IS NULL"
                ).fetchone()[0],
            )

    async def test_late_cancellation_returns_the_committed_result(self) -> None:
        """A mutation committed during cancellation must return its receipt."""
        from daem0nmcp.api.v7.local_state_operations import _run_mutation

        committed = threading.Event()
        release = threading.Event()

        def operation(_cancelled: threading.Event) -> str:
            committed.set()
            release.wait(timeout=2)
            return "committed-receipt"

        task = asyncio.create_task(_run_mutation(operation))
        self.assertTrue(await asyncio.to_thread(committed.wait, 2))
        task.cancel()
        await asyncio.sleep(0.02)
        self.assertFalse(task.done())
        release.set()
        self.assertEqual("committed-receipt", await task)

    async def test_cancelled_add_rolls_back_and_joins_worker(self) -> None:
        """Cancellation must not leave a detached SQLite commit behind."""
        started = threading.Event()
        release = threading.Event()

        def blocking_clock() -> datetime:
            started.set()
            release.wait(timeout=2)
            return NOW

        record_id = self.fixture.add_record(1)
        operation = self._operations(clock=blocking_clock)["active_context_add"]
        task = asyncio.create_task(
            operation(
                workspace=self.fixture.workspace,
                request=self._add_request(record_id),
            )
        )
        self.assertTrue(await asyncio.to_thread(started.wait, 2))
        task.cancel()
        await asyncio.sleep(0.02)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT count(*) FROM active_context_entries"
                ).fetchone()[0],
            )
            self.assertEqual(
                0,
                connection.execute("SELECT count(*) FROM governance_events").fetchone()[
                    0
                ],
            )


if __name__ == "__main__":
    unittest.main()
