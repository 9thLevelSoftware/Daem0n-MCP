from __future__ import annotations

import asyncio
import inspect
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType

from daem0nmcp.api.v7.application import AdmittedRequest

NOW = datetime(2026, 8, 9, 18, 0, tzinfo=timezone.utc)


def _apply_v7_schema(connection: sqlite3.Connection) -> None:
    from daem0nmcp.migrations.schema import MIGRATIONS
    from daem0nmcp.schema_version import CURRENT_SCHEMA_VERSION

    connection.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
    for version, _description, statements in MIGRATIONS:
        if 16 <= version <= CURRENT_SCHEMA_VERSION:
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_version(version) VALUES (?)", (version,)
            )
    connection.commit()


def _request(tool_name: str, **arguments: object) -> AdmittedRequest:
    from daem0nmcp.api.v7.tools import TOOL_INPUT_MODELS

    model = TOOL_INPUT_MODELS[tool_name].model_validate(arguments)
    effective = model.model_dump(mode="python")
    effective.pop("preflight_token", None)
    return AdmittedRequest(tool_name, MappingProxyType(effective))


class FederationOperationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from daem0nmcp.storage_activation import (
            ActiveDatabasePointer,
            write_active_pointer,
        )
        from daem0nmcp.workspace import WorkspaceRegistry

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name).resolve()
        self.roots = [base / name for name in ("target", "source-a", "source-b")]
        for root in self.roots:
            storage = root / ".daem0nmcp" / "storage"
            storage.mkdir(parents=True)
            database = storage / "daem0nmcp.db"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("PRAGMA foreign_keys=ON")
                _apply_v7_schema(connection)
            write_active_pointer(
                storage,
                ActiveDatabasePointer(7, 1, database.name, None, None),
            )
        self.registry = WorkspaceRegistry(self.roots[1:], default_root=self.roots[0])
        self.target = self.registry.resolve(str(self.roots[0]))
        self.source_a = self.registry.resolve(str(self.roots[1]))
        self.source_b = self.registry.resolve(str(self.roots[2]))
        self.dependencies = None

    async def asyncTearDown(self) -> None:
        if self.dependencies is not None:
            self.dependencies.close()

    def _operations(self, **changes: object):
        from daem0nmcp.api.v7.federation_operations import (
            FederationOperationDependencies,
            build_federation_operations,
        )

        options: dict[str, object] = {
            "workspace_resolver": self.registry,
            "clock": lambda: NOW,
            "cursor_secret": b"federation-cursor-secret-32-bytes!",
        }
        options.update(changes)
        self.dependencies = FederationOperationDependencies(**options)
        return build_federation_operations(self.dependencies)

    def _database(self, root: Path | None = None) -> Path:
        selected = self.roots[0] if root is None else root
        return selected / ".daem0nmcp" / "storage" / "daem0nmcp.db"

    def test_schema_ledger_adds_an_immutable_workspace_id_only_link_log(self) -> None:
        from daem0nmcp.migrations.schema import MIGRATIONS
        from daem0nmcp.schema_version import CURRENT_SCHEMA_VERSION

        self.assertEqual(CURRENT_SCHEMA_VERSION, MIGRATIONS[-1][0])
        with closing(sqlite3.connect(self._database())) as connection:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(workspace_link_events)"
                )
            }
            self.assertIn("workspace_id", columns)
            self.assertIn("linked_workspace_id", columns)
            self.assertNotIn("source_path", columns)
            self.assertNotIn("linked_path", columns)
            self.assertNotIn("project_path", columns)

    def test_factory_exposes_only_safe_link_lifecycle_handlers(self) -> None:
        operations = self._operations()
        self.assertEqual(
            {"workspace_link", "workspace_unlink", "workspace_links_list"},
            set(operations),
        )
        for handler in operations.values():
            parameters = inspect.signature(handler).parameters
            self.assertEqual(["workspace", "request"], list(parameters))
            self.assertTrue(
                all(item.kind is item.KEYWORD_ONLY for item in parameters.values())
            )

    async def test_link_list_update_unlink_and_replays_are_append_only(self) -> None:
        operations = self._operations()
        linked = await operations["workspace_link"](
            workspace=self.target,
            request=_request(
                "workspace_link",
                workspace_id=self.target.workspace_id,
                linked_workspace_id=self.source_a.workspace_id,
                relationship="related",
                label="first label",
                preflight_token="t" * 32,
            ),
        )
        self.assertEqual(self.target.workspace_id, linked.workspace_id)
        self.assertEqual(self.source_a.workspace_id, linked.linked_workspace_id)
        replay = await operations["workspace_link"](
            workspace=self.target,
            request=_request(
                "workspace_link",
                workspace_id=self.target.workspace_id,
                linked_workspace_id=self.source_a.workspace_id,
                relationship="related",
                label="first label",
                preflight_token="t" * 32,
            ),
        )
        self.assertEqual(linked, replay)
        updated = await operations["workspace_link"](
            workspace=self.target,
            request=_request(
                "workspace_link",
                workspace_id=self.target.workspace_id,
                linked_workspace_id=self.source_a.workspace_id,
                relationship="related",
                label="updated label",
                preflight_token="t" * 32,
            ),
        )
        self.assertEqual("updated label", updated.label)

        page = await operations["workspace_links_list"](
            workspace=self.target,
            request=_request(
                "workspace_links_list",
                workspace_id=self.target.workspace_id,
                limit=50,
            ),
        )
        self.assertEqual([updated], page.items)
        self.assertFalse(page.truncated)

        removed = await operations["workspace_unlink"](
            workspace=self.target,
            request=_request(
                "workspace_unlink",
                workspace_id=self.target.workspace_id,
                linked_workspace_id=self.source_a.workspace_id,
                preflight_token="t" * 32,
            ),
        )
        self.assertFalse(removed.idempotent_replay)
        self.assertEqual([self.source_a.workspace_id], removed.affected_ids)
        self.assertEqual(1, removed.counts["unlinked"])
        self.assertEqual(1, len(removed.event_ids))
        removed_replay = await operations["workspace_unlink"](
            workspace=self.target,
            request=_request(
                "workspace_unlink",
                workspace_id=self.target.workspace_id,
                linked_workspace_id=self.source_a.workspace_id,
                preflight_token="t" * 32,
            ),
        )
        self.assertTrue(removed_replay.idempotent_replay)
        self.assertEqual(removed.event_ids, removed_replay.event_ids)
        self.assertEqual(0, removed_replay.counts["unlinked"])

        empty = await operations["workspace_links_list"](
            workspace=self.target,
            request=_request(
                "workspace_links_list",
                workspace_id=self.target.workspace_id,
                limit=50,
            ),
        )
        self.assertEqual([], empty.items)
        with closing(sqlite3.connect(self._database())) as connection:
            rows = connection.execute(
                "SELECT event_type,stream_version,workspace_id,"
                "linked_workspace_id FROM workspace_link_events "
                "ORDER BY stream_version"
            ).fetchall()
            self.assertEqual(
                ["workspace.linked", "workspace.linked", "workspace.unlinked"],
                [row[0] for row in rows],
            )
            self.assertEqual([1, 2, 3], [row[1] for row in rows])
            self.assertTrue(
                all(
                    row[2:] == (self.target.workspace_id, self.source_a.workspace_id)
                    for row in rows
                )
            )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "IMMUTABLE"):
                connection.execute("UPDATE workspace_link_events SET label='changed'")

    async def test_list_cursor_is_hmac_bound_to_workspace_and_anchor(self) -> None:
        operations = self._operations()
        for source in (self.source_a, self.source_b):
            await operations["workspace_link"](
                workspace=self.target,
                request=_request(
                    "workspace_link",
                    workspace_id=self.target.workspace_id,
                    linked_workspace_id=source.workspace_id,
                    relationship="related",
                    label=None,
                    preflight_token="t" * 32,
                ),
            )
        first = await operations["workspace_links_list"](
            workspace=self.target,
            request=_request(
                "workspace_links_list",
                workspace_id=self.target.workspace_id,
                limit=1,
            ),
        )
        self.assertTrue(first.truncated)
        self.assertIsNotNone(first.next_cursor)
        second = await operations["workspace_links_list"](
            workspace=self.target,
            request=_request(
                "workspace_links_list",
                workspace_id=self.target.workspace_id,
                cursor=first.next_cursor,
                limit=1,
            ),
        )
        self.assertEqual(1, len(second.items))
        self.assertNotEqual(first.items, second.items)

        from daem0nmcp.api.v7.federation_operations import FederationOperationError

        tampered = str(first.next_cursor)[:-1] + (
            "0" if str(first.next_cursor)[-1] != "0" else "1"
        )
        with self.assertRaisesRegex(FederationOperationError, "INVALID_ARGUMENT"):
            await operations["workspace_links_list"](
                workspace=self.target,
                request=_request(
                    "workspace_links_list",
                    workspace_id=self.target.workspace_id,
                    cursor=tampered,
                    limit=1,
                ),
            )

    async def test_link_ledger_hash_and_chain_are_verified_before_read(self) -> None:
        from daem0nmcp.api.v7.federation_operations import FederationOperationError

        operations = self._operations()
        await operations["workspace_link"](
            workspace=self.target,
            request=_request(
                "workspace_link",
                workspace_id=self.target.workspace_id,
                linked_workspace_id=self.source_a.workspace_id,
                relationship="related",
                label="verified",
                preflight_token="t" * 32,
            ),
        )
        with closing(sqlite3.connect(self._database())) as connection:
            connection.execute("DROP TRIGGER workspace_link_events_no_update")
            connection.execute("UPDATE workspace_link_events SET label='tampered'")
            connection.commit()
        with self.assertRaisesRegex(FederationOperationError, "CAPABILITY_DEGRADED"):
            await operations["workspace_links_list"](
                workspace=self.target,
                request=_request(
                    "workspace_links_list",
                    workspace_id=self.target.workspace_id,
                    limit=50,
                ),
            )

    async def test_linked_workspace_must_resolve_to_exact_registered_identity(
        self,
    ) -> None:
        from daem0nmcp.api.v7.federation_operations import FederationOperationError
        from daem0nmcp.workspace import WorkspaceRegistry

        isolated = WorkspaceRegistry([self.roots[0]], default_root=self.roots[0])
        operations = self._operations(workspace_resolver=isolated)
        with self.assertRaisesRegex(FederationOperationError, "UNAUTHORIZED_WORKSPACE"):
            await operations["workspace_link"](
                workspace=self.target,
                request=_request(
                    "workspace_link",
                    workspace_id=self.target.workspace_id,
                    linked_workspace_id=self.source_a.workspace_id,
                    relationship="related",
                    label=None,
                    preflight_token="t" * 32,
                ),
            )
        with closing(sqlite3.connect(self._database())) as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT count(*) FROM workspace_link_events"
                ).fetchone()[0],
            )

    async def test_link_acquires_both_activation_locks_in_workspace_id_order(
        self,
    ) -> None:
        from daem0nmcp.api.v7.runtime_services import WorkspaceStorageResolver

        observed: list[tuple[str, str]] = []

        class RecordingResolver:
            @staticmethod
            @contextmanager
            def locked_active(workspace):
                observed.append(("enter", workspace.workspace_id))
                with WorkspaceStorageResolver().locked_active(workspace) as active:
                    try:
                        yield active
                    finally:
                        observed.append(("exit", workspace.workspace_id))

        operations = self._operations(storage_resolver=RecordingResolver())
        await operations["workspace_link"](
            workspace=self.target,
            request=_request(
                "workspace_link",
                workspace_id=self.target.workspace_id,
                linked_workspace_id=self.source_a.workspace_id,
                relationship="related",
                label=None,
                preflight_token="t" * 32,
            ),
        )
        ordered = sorted([self.target.workspace_id, self.source_a.workspace_id])
        self.assertEqual(
            [("enter", value) for value in ordered]
            + [("exit", value) for value in reversed(ordered)],
            observed,
        )

    async def test_cancellation_before_commit_rolls_back_and_repeated_cancel_drains(
        self,
    ) -> None:
        from daem0nmcp.api.v7.runtime_services import WorkspaceStorageResolver

        entered = threading.Event()
        release = threading.Event()

        class BlockingResolver:
            @staticmethod
            @contextmanager
            def locked_active(workspace):
                entered.set()
                release.wait(5)
                with WorkspaceStorageResolver().locked_active(workspace) as active:
                    yield active

        operations = self._operations(storage_resolver=BlockingResolver())
        task = asyncio.create_task(
            operations["workspace_link"](
                workspace=self.target,
                request=_request(
                    "workspace_link",
                    workspace_id=self.target.workspace_id,
                    linked_workspace_id=self.source_a.workspace_id,
                    relationship="related",
                    label=None,
                    preflight_token="t" * 32,
                ),
            )
        )
        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
        task.cancel()
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with closing(sqlite3.connect(self._database())) as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT count(*) FROM workspace_link_events"
                ).fetchone()[0],
            )


if __name__ == "__main__":
    unittest.main()
