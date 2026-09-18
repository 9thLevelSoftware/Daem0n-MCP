from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

from daem0nmcp.api.v7.application import AdmittedRequest

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
NOW_US = 1_786_276_800_000_000
PREFLIGHT_TOKEN = "preflight-token-0001"


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


class _BlockingStorageResolver:
    def __init__(self, database: Path) -> None:
        self.database = database
        self.entered = threading.Event()
        self.release = threading.Event()

    @contextmanager
    def locked_active(self, _workspace: object):
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("test storage resolver timed out")
        yield SimpleNamespace(path=self.database)


class CodeEntityOperationContractTests(unittest.TestCase):
    def test_registry_is_exact_immutable_and_keyword_only(self) -> None:
        """An extra, mutable, or positional handler bypasses the reviewed seam."""
        from daem0nmcp.api.v7.code_entity_operations import (
            CodeEntityOperationDependencies,
            build_code_entity_operations,
        )

        dependencies = CodeEntityOperationDependencies(operation_secret=b"s" * 32)
        self.addCleanup(dependencies.close)
        operations = build_code_entity_operations(dependencies)

        self.assertEqual(
            {
                "code_impact_analyze",
                "code_todos_scan_and_store",
                "entity_evolution_trace",
            },
            set(operations),
        )
        with self.assertRaises(TypeError):
            operations["code_impact_analyze"] = object()
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

    def test_dependency_secret_is_strong_and_owned_worker_is_closeable(self) -> None:
        """Cursor compatibility cannot rely on a missing or weak HMAC secret."""
        from daem0nmcp.api.v7.code_entity_operations import (
            CodeEntityOperationDependencies,
        )

        with self.assertRaises(ValueError):
            CodeEntityOperationDependencies(operation_secret=b"short")
        dependencies = CodeEntityOperationDependencies(operation_secret=b"s" * 32)
        dependencies.close()


class CodeEntityOperationTests(unittest.IsolatedAsyncioTestCase):
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
        self.dependencies: list[object] = []

    async def asyncTearDown(self) -> None:
        for dependency in reversed(self.dependencies):
            dependency.close()

    def _operations(self, **changes: object):
        from daem0nmcp.api.v7.code_entity_operations import (
            CodeEntityOperationDependencies,
            build_code_entity_operations,
        )

        options: dict[str, object] = {
            "operation_secret": b"s" * 32,
            "clock": lambda: NOW,
        }
        options.update(changes)
        dependencies = CodeEntityOperationDependencies(**options)
        self.dependencies.append(dependencies)
        return build_code_entity_operations(dependencies)

    @staticmethod
    def _record_state(
        content: str,
        *,
        record_type: str = "decision",
        deleted_at_us: int | None = None,
    ) -> dict[str, object]:
        return {
            "record_type": record_type,
            "legacy_type": None,
            "content": content,
            "rationale": None,
            "context": {},
            "tags": [],
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
            "source_client": "code-entity-test",
            "source_model": None,
            "deleted_at_us": deleted_at_us,
        }

    def _append_record(
        self,
        suffix: str,
        content: str,
        *,
        record_type: str = "decision",
        version: int = 1,
        happened_at_us: int = NOW_US - 100,
        deleted_at_us: int | None = None,
    ) -> tuple[str, str]:
        from daem0nmcp.event_store import EventCommand, EventStore

        record_id = "mem_" + suffix * 64
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            event = EventStore(connection).append_and_project(
                EventCommand(
                    workspace_id=self.workspace.workspace_id,
                    stream_id=record_id,
                    stream_kind="memory",
                    event_type=("memory.created" if version == 1 else "memory.updated"),
                    occurred_at_us=happened_at_us,
                    recorded_at_us=happened_at_us,
                    actor_type="client",
                    payload={
                        "record": self._record_state(
                            content,
                            record_type=record_type,
                            deleted_at_us=deleted_at_us,
                        )
                    },
                    expected_stream_version=version,
                )
            )
            connection.commit()
        return record_id, event.event_id

    def _activate_entities(
        self,
        *,
        name: str,
        entity_type: str,
        record_ids: tuple[str, ...],
    ) -> tuple[str, int]:
        from daem0nmcp.discovery_projection import (
            DiscoveryProjectionBuilder,
            EntityProjectionSeed,
            EntityRecordSeed,
        )
        from daem0nmcp.retrieval.specialized_projection import (
            SpecializedProjectionBuilder,
        )

        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            specialized = SpecializedProjectionBuilder(
                connection, clock_us=lambda: NOW_US - 20
            ).rebuild(self.workspace.workspace_id, "graph")
            result = DiscoveryProjectionBuilder(
                connection, clock_us=lambda: NOW_US - 10
            ).populate_graph(
                self.workspace.workspace_id,
                entities=(
                    EntityProjectionSeed(
                        name=name,
                        entity_type=entity_type,
                        records=tuple(
                            EntityRecordSeed(record_id) for record_id in record_ids
                        ),
                    ),
                ),
                communities=(),
            )
            connection.commit()
        self.assertEqual(specialized.generation, result.graph_generation)
        return result.entity_ids[0], int(result.graph_generation)

    async def test_todo_store_matches_scan_and_replays_canonical_events(
        self,
    ) -> None:
        """Wrong scan semantics or a retained write would corrupt TODO evidence."""
        from daem0nmcp.api.v7.utility_operations import (
            UtilityOperationDependencies,
            build_utility_operations,
        )

        source = self.root / "src"
        source.mkdir()
        (source / "app.py").write_text(
            "# TODO: first\n# FIXME second\nprint('ok')\n",
            encoding="utf-8",
        )
        ignored = source / ".git"
        ignored.mkdir()
        (ignored / "ignored.py").write_text("# TODO hidden\n", encoding="utf-8")
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT)"
            )
            connection.commit()
        utility_dependencies = UtilityOperationDependencies(cursor_secret=b"s" * 32)
        self.dependencies.append(utility_dependencies)
        scan = await build_utility_operations(utility_dependencies)["code_todos_scan"](
            workspace=self.workspace,
            request=_request(
                "code_todos_scan",
                workspace_id=self.workspace.workspace_id,
                relative_root="src",
                types={"todo", "fixme"},
                limit=100,
            ),
        )
        operation = self._operations()["code_todos_scan_and_store"]
        request = _request(
            "code_todos_scan_and_store",
            workspace_id=self.workspace.workspace_id,
            relative_root="src",
            types={"todo", "fixme"},
            limit=100,
            idempotency_key="todo-scan-src-0001",
            preflight_token=PREFLIGHT_TOKEN,
        )

        first = await operation(workspace=self.workspace, request=request)
        second = await operation(workspace=self.workspace, request=request)

        self.assertEqual(scan.items, first.findings)
        self.assertEqual(first, second)
        self.assertEqual(2, len(first.stored_records))
        self.assertEqual(2, len(first.event_ids))
        self.assertEqual(
            [
                "TODO at src/app.py:1: # TODO: first",
                "FIXME at src/app.py:2: # FIXME second",
            ],
            [record.excerpt for record in first.stored_records],
        )
        self.assertEqual(
            ["src/app.py", "src/app.py"],
            [record.relative_file_path for record in first.stored_records],
        )
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            events = connection.execute(
                "SELECT event_id,payload_json FROM memory_events "
                "WHERE workspace_id=? ORDER BY event_id",
                (self.workspace.workspace_id,),
            ).fetchall()
            retained = connection.execute("SELECT count(*) FROM memories").fetchone()[0]
        self.assertEqual(2, len(events))
        self.assertEqual(0, retained)
        for row in events:
            payload = json.loads(row["payload_json"])
            self.assertIn(row["event_id"], first.event_ids)
            self.assertEqual("warning", payload["record"]["record_type"])
            self.assertIn("todo_finding", payload)
            self.assertIn("idempotency_request_hash", payload)

    async def test_todo_store_accepts_the_utility_scanner_hmac_cursor(
        self,
    ) -> None:
        """A cursor from the read scanner must select the identical next item."""
        from daem0nmcp.api.v7.utility_operations import (
            UtilityOperationDependencies,
            build_utility_operations,
        )

        (self.root / "todos.py").write_text(
            "# TODO first\n# FIXME second\n# HACK third\n",
            encoding="utf-8",
        )
        utility_dependencies = UtilityOperationDependencies(cursor_secret=b"s" * 32)
        self.dependencies.append(utility_dependencies)
        first_page = await build_utility_operations(utility_dependencies)[
            "code_todos_scan"
        ](
            workspace=self.workspace,
            request=_request(
                "code_todos_scan",
                workspace_id=self.workspace.workspace_id,
                limit=1,
            ),
        )
        self.assertIsNotNone(first_page.next_cursor)

        result = await self._operations()["code_todos_scan_and_store"](
            workspace=self.workspace,
            request=_request(
                "code_todos_scan_and_store",
                workspace_id=self.workspace.workspace_id,
                cursor=first_page.next_cursor,
                limit=1,
                idempotency_key="todo-cursor-page-0001",
                preflight_token=PREFLIGHT_TOKEN,
            ),
        )

        self.assertEqual(1, len(result.findings))
        self.assertEqual("fixme", result.findings[0].todo_type)
        self.assertEqual(2, result.findings[0].line)

    async def test_todo_replay_uses_immutable_event_not_projection_time(
        self,
    ) -> None:
        """A mutable projection cannot rewrite an idempotent response."""

        (self.root / "todo.py").write_text("# TODO bind time\n", encoding="utf-8")
        operation = self._operations()["code_todos_scan_and_store"]
        request = _request(
            "code_todos_scan_and_store",
            workspace_id=self.workspace.workspace_id,
            idempotency_key="todo-time-integrity-0001",
            preflight_token=PREFLIGHT_TOKEN,
        )
        stored = await operation(workspace=self.workspace, request=request)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE memory_records SET created_at_us=created_at_us-1 "
                "WHERE record_id=?",
                (stored.stored_records[0].record_id,),
            )
            connection.commit()

        replay = await operation(workspace=self.workspace, request=request)
        self.assertEqual(stored, replay)

    async def test_todo_replay_survives_legitimate_later_record_update(
        self,
    ) -> None:
        """The immutable v1 batch remains the result after canonical v2."""
        from daem0nmcp.event_store import EventCommand, EventStore

        (self.root / "todo.py").write_text("# TODO archive later\n", encoding="utf-8")
        operation = self._operations()["code_todos_scan_and_store"]
        request = _request(
            "code_todos_scan_and_store",
            workspace_id=self.workspace.workspace_id,
            idempotency_key="todo-later-update-0001",
            preflight_token=PREFLIGHT_TOKEN,
        )
        stored = await operation(workspace=self.workspace, request=request)
        record_id = stored.stored_records[0].record_id
        with closing(sqlite3.connect(self.database)) as connection:
            source = connection.execute(
                "SELECT payload_json FROM memory_events WHERE event_id=?",
                (stored.event_ids[0],),
            ).fetchone()[0]
            state = json.loads(source)["record"]
            state["archived"] = True
            EventStore(connection).append_and_project(
                EventCommand(
                    workspace_id=self.workspace.workspace_id,
                    stream_id=record_id,
                    stream_kind="memory",
                    event_type="memory.updated",
                    occurred_at_us=NOW_US + 1,
                    recorded_at_us=NOW_US + 1,
                    actor_type="client",
                    payload={"record": state},
                    expected_stream_version=2,
                )
            )
            connection.commit()

        replay = await operation(workspace=self.workspace, request=request)
        self.assertEqual(stored, replay)
        with closing(sqlite3.connect(self.database)) as connection:
            version = connection.execute(
                "SELECT stream_version FROM memory_records WHERE record_id=?",
                (record_id,),
            ).fetchone()[0]
        self.assertEqual(2, version)

    async def test_empty_todo_scan_journals_exact_replay(self) -> None:
        """A no-result key cannot acquire findings after the workspace changes."""
        operation = self._operations()["code_todos_scan_and_store"]
        request = _request(
            "code_todos_scan_and_store",
            workspace_id=self.workspace.workspace_id,
            idempotency_key="todo-empty-replay-0001",
            preflight_token=PREFLIGHT_TOKEN,
        )
        first = await operation(workspace=self.workspace, request=request)
        self.assertEqual([], first.findings)
        self.assertEqual(1, len(first.stored_records))
        self.assertEqual(1, len(first.event_ids))
        self.assertEqual("archived", first.stored_records[0].current_status)
        (self.root / "later.py").write_text("# TODO appeared later\n", encoding="utf-8")

        second = await operation(workspace=self.workspace, request=request)

        self.assertEqual(first, second)
        with closing(sqlite3.connect(self.database)) as connection:
            event_count = connection.execute(
                "SELECT count(*) FROM memory_events"
            ).fetchone()[0]
        self.assertEqual(1, event_count)

    async def test_todo_store_binds_key_to_request_and_rolls_back_batch(
        self,
    ) -> None:
        """A changed request conflicts, and a failed second append leaves no batch."""
        from daem0nmcp.api.v7.code_entity_operations import (
            CodeEntityOperationError,
        )

        (self.root / "one.py").write_text("# TODO first\n", encoding="utf-8")
        operation = self._operations()["code_todos_scan_and_store"]
        await operation(
            workspace=self.workspace,
            request=_request(
                "code_todos_scan_and_store",
                workspace_id=self.workspace.workspace_id,
                types={"todo"},
                idempotency_key="todo-conflict-0001",
                preflight_token=PREFLIGHT_TOKEN,
            ),
        )
        with self.assertRaises(CodeEntityOperationError) as conflict:
            await operation(
                workspace=self.workspace,
                request=_request(
                    "code_todos_scan_and_store",
                    workspace_id=self.workspace.workspace_id,
                    types={"fixme"},
                    idempotency_key="todo-conflict-0001",
                    preflight_token=PREFLIGHT_TOKEN,
                ),
            )
        self.assertEqual("IDEMPOTENCY_CONFLICT", conflict.exception.code)

        (self.root / "two.py").write_text(
            "# TODO alpha\n# TODO beta\n", encoding="utf-8"
        )
        with closing(sqlite3.connect(self.database)) as connection:
            before_events = connection.execute(
                "SELECT count(*) FROM memory_events"
            ).fetchone()[0]
            connection.execute(
                "CREATE TRIGGER fail_second_todo_event "
                "BEFORE INSERT ON memory_events "
                "WHEN NEW.correlation_id IS NOT NULL AND EXISTS ("
                "SELECT 1 FROM memory_events AS prior "
                "WHERE prior.correlation_id=NEW.correlation_id) "
                "BEGIN SELECT RAISE(ABORT,'test batch failure'); END"
            )
            connection.commit()
        with self.assertRaises(CodeEntityOperationError) as failed:
            await operation(
                workspace=self.workspace,
                request=_request(
                    "code_todos_scan_and_store",
                    workspace_id=self.workspace.workspace_id,
                    relative_root=".",
                    types={"todo"},
                    idempotency_key="todo-atomic-0001",
                    preflight_token=PREFLIGHT_TOKEN,
                ),
            )
        self.assertEqual("EVENT_STREAM_CONFLICT", failed.exception.code)
        with closing(sqlite3.connect(self.database)) as connection:
            atomic_events = connection.execute(
                "SELECT count(*) FROM memory_events",
            ).fetchone()[0]
        self.assertEqual(before_events, atomic_events)

    async def test_todo_mutation_drain_survives_repeated_cancellation(
        self,
    ) -> None:
        """Repeated cancellation cannot detach a worker that later commits."""
        (self.root / "cancel.py").write_text("# TODO do not commit\n", encoding="utf-8")
        resolver = _BlockingStorageResolver(self.database)
        operation = self._operations(storage_resolver=resolver)[
            "code_todos_scan_and_store"
        ]
        task = asyncio.create_task(
            operation(
                workspace=self.workspace,
                request=_request(
                    "code_todos_scan_and_store",
                    workspace_id=self.workspace.workspace_id,
                    idempotency_key="todo-cancel-0001",
                    preflight_token=PREFLIGHT_TOKEN,
                ),
            )
        )
        entered = await asyncio.to_thread(resolver.entered.wait, 1)
        self.assertTrue(entered)

        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        try:
            self.assertFalse(task.done())
        finally:
            resolver.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with closing(sqlite3.connect(self.database)) as connection:
            event_count = connection.execute(
                "SELECT count(*) FROM memory_events"
            ).fetchone()[0]
        self.assertEqual(0, event_count)

    async def test_entity_evolution_is_chronological_and_event_backed(
        self,
    ) -> None:
        """Projection membership must lead only to verified canonical changes."""
        first_id, first_created = self._append_record(
            "a",
            "Authentication initially used bearer tokens.",
            happened_at_us=NOW_US - 300,
        )
        second_id, second_created = self._append_record(
            "b",
            "Authentication key rotation became mandatory.",
            record_type="warning",
            happened_at_us=NOW_US - 200,
        )
        _same_id, first_updated = self._append_record(
            "a",
            "Authentication now uses signed session cookies.",
            version=2,
            happened_at_us=NOW_US - 100,
        )
        entity_id, generation = self._activate_entities(
            name="Authentication",
            entity_type="concept",
            record_ids=(first_id, second_id),
        )
        operation = self._operations()["entity_evolution_trace"]

        all_versions = await operation(
            workspace=self.workspace,
            request=_request(
                "entity_evolution_trace",
                workspace_id=self.workspace.workspace_id,
                entity_id=entity_id,
                include_invalidated=True,
            ),
        )
        current_versions = await operation(
            workspace=self.workspace,
            request=_request(
                "entity_evolution_trace",
                workspace_id=self.workspace.workspace_id,
                entity_name="authentication",
                entity_type="concept",
                include_invalidated=False,
            ),
        )

        self.assertEqual(entity_id, all_versions.entity.entity_id)
        self.assertEqual("Authentication", all_versions.entity.name)
        self.assertEqual("concept", all_versions.entity.entity_type)
        self.assertEqual(2, all_versions.entity.mention_count)
        self.assertEqual(generation, all_versions.entity.manifest_generation)
        self.assertEqual(
            [first_created, second_created, first_updated],
            [item.event_id for item in all_versions.timeline],
        )
        self.assertEqual(
            [NOW_US - 300, NOW_US - 200, NOW_US - 100],
            [
                int(item.happened_at.timestamp() * 1_000_000)
                for item in all_versions.timeline
            ],
        )
        self.assertIn("bearer tokens", all_versions.timeline[0].summary)
        self.assertIn("signed session cookies", all_versions.timeline[-1].summary)
        self.assertEqual(
            {
                (first_id, first_created),
                (second_id, second_created),
                (first_id, first_updated),
            },
            {
                (reference.record_id, reference.event_id)
                for reference in all_versions.evidence_refs
            },
        )
        self.assertEqual(
            [second_created, first_updated],
            [item.event_id for item in current_versions.timeline],
        )
        self.assertEqual(
            [second_created, first_updated],
            [item.event_id for item in current_versions.evidence_refs],
        )

    async def test_entity_evolution_rejects_stale_generation_id(self) -> None:
        """An entity absent from the active graph cannot select historical rows."""
        from daem0nmcp.api.v7.code_entity_operations import (
            CodeEntityOperationError,
        )
        from daem0nmcp.discovery_projection import (
            DiscoveryProjectionBuilder,
            EntityProjectionSeed,
            EntityRecordSeed,
        )
        from daem0nmcp.retrieval.specialized_projection import (
            SpecializedProjectionBuilder,
        )

        old_record_id, _event_id = self._append_record(
            "a", "Authentication used signed cookies."
        )
        old_entity_id, old_generation = self._activate_entities(
            name="Authentication",
            entity_type="concept",
            record_ids=(old_record_id,),
        )
        new_record_id, _new_event_id = self._append_record(
            "b",
            "Authorization now uses explicit scopes.",
            happened_at_us=NOW_US,
        )
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            rebuilt = SpecializedProjectionBuilder(connection).rebuild(
                self.workspace.workspace_id,
                "graph",
            )
            DiscoveryProjectionBuilder(connection).populate_graph(
                self.workspace.workspace_id,
                entities=(
                    EntityProjectionSeed(
                        name="Authorization",
                        entity_type="concept",
                        records=(EntityRecordSeed(new_record_id),),
                    ),
                ),
                communities=(),
            )
            connection.commit()
        self.assertGreater(rebuilt.generation, old_generation)

        with self.assertRaises(CodeEntityOperationError) as raised:
            await self._operations()["entity_evolution_trace"](
                workspace=self.workspace,
                request=_request(
                    "entity_evolution_trace",
                    workspace_id=self.workspace.workspace_id,
                    entity_id=old_entity_id,
                ),
            )
        self.assertEqual("STALE_PROJECTION_ID", raised.exception.code)

    async def test_entity_evolution_recomputes_partition_digest(self) -> None:
        """A locally changed entity row cannot drive an evidence response."""
        from daem0nmcp.api.v7.code_entity_operations import (
            CodeEntityOperationError,
        )

        record_id, _event_id = self._append_record(
            "a", "Authentication used signed cookies."
        )
        entity_id, generation = self._activate_entities(
            name="Authentication",
            entity_type="concept",
            record_ids=(record_id,),
        )
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("DROP TRIGGER discovery_entities_no_update")
            connection.execute(
                "UPDATE discovery_entities SET name=? "
                "WHERE workspace_id=? AND graph_generation=? AND entity_id=?",
                (
                    "Tampered Authentication",
                    self.workspace.workspace_id,
                    generation,
                    entity_id,
                ),
            )
            connection.commit()

        with self.assertRaises(CodeEntityOperationError) as raised:
            await self._operations()["entity_evolution_trace"](
                workspace=self.workspace,
                request=_request(
                    "entity_evolution_trace",
                    workspace_id=self.workspace.workspace_id,
                    entity_id=entity_id,
                ),
            )
        self.assertEqual("CAPABILITY_DEGRADED", raised.exception.code)

    async def test_entity_evolution_verifies_canonical_event_payload(self) -> None:
        """A stale payload digest cannot be emitted as chronological evidence."""
        from daem0nmcp.api.v7.code_entity_operations import (
            CodeEntityOperationError,
        )

        record_id, event_id = self._append_record(
            "a", "Authentication used signed cookies."
        )
        entity_id, _generation = self._activate_entities(
            name="Authentication",
            entity_type="concept",
            record_ids=(record_id,),
        )
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("DROP TRIGGER memory_events_no_update")
            payload = connection.execute(
                "SELECT payload_json FROM memory_events WHERE event_id=?",
                (event_id,),
            ).fetchone()[0]
            connection.execute(
                "UPDATE memory_events SET payload_json=? WHERE event_id=?",
                (payload + " ", event_id),
            )
            connection.commit()

        with self.assertRaises(CodeEntityOperationError) as raised:
            await self._operations()["entity_evolution_trace"](
                workspace=self.workspace,
                request=_request(
                    "entity_evolution_trace",
                    workspace_id=self.workspace.workspace_id,
                    entity_id=entity_id,
                ),
            )
        self.assertEqual("CAPABILITY_DEGRADED", raised.exception.code)

    async def test_entity_read_drain_survives_repeated_cancellation(self) -> None:
        """Repeated cancellation cannot detach a generation-locked read."""
        record_id, _event_id = self._append_record(
            "a", "Authentication used signed cookies."
        )
        entity_id, _generation = self._activate_entities(
            name="Authentication",
            entity_type="concept",
            record_ids=(record_id,),
        )
        resolver = _BlockingStorageResolver(self.database)
        operation = self._operations(storage_resolver=resolver)[
            "entity_evolution_trace"
        ]
        task = asyncio.create_task(
            operation(
                workspace=self.workspace,
                request=_request(
                    "entity_evolution_trace",
                    workspace_id=self.workspace.workspace_id,
                    entity_id=entity_id,
                ),
            )
        )
        entered = await asyncio.to_thread(resolver.entered.wait, 1)
        self.assertTrue(entered)

        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        try:
            self.assertFalse(task.done())
        finally:
            resolver.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_code_impact_traverses_only_active_generation_dependencies(
        self,
    ) -> None:
        from daem0nmcp.discovery_projection import (
            CodeEdgeProjectionSeed,
            CodeEntityProjectionSeed,
            DiscoveryProjectionBuilder,
        )

        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            DiscoveryProjectionBuilder(connection).rebuild_code(
                self.workspace.workspace_id,
                entities=(
                    CodeEntityProjectionSeed(
                        "target", "function", "core.target", "src/core.py", 1, 2
                    ),
                    CodeEntityProjectionSeed(
                        "middle", "function", "service.middle", "src/service.py", 3, 4
                    ),
                    CodeEntityProjectionSeed(
                        "entry", "function", "api.entry", "src/api.py", 5, 6
                    ),
                ),
                edges=(
                    CodeEdgeProjectionSeed("middle", "target", "call", 3),
                    CodeEdgeProjectionSeed("entry", "middle", "reference", 5),
                ),
            )
            target_id = connection.execute(
                "SELECT code_entity_id FROM discovery_code_entities "
                "WHERE workspace_id=? AND qualified_name='core.target'",
                (self.workspace.workspace_id,),
            ).fetchone()[0]

        operation = self._operations()["code_impact_analyze"]
        one = await operation(
            workspace=self.workspace,
            request=_request(
                "code_impact_analyze",
                workspace_id=self.workspace.workspace_id,
                code_entity_id=target_id,
                max_depth=1,
            ),
        )
        self.assertEqual("core.target", one.subject.qualified_name)
        self.assertEqual(
            ["service.middle"], [item.qualified_name for item in one.affected]
        )
        self.assertEqual(
            [[target_id, one.affected[0].code_entity_id]],
            [path.entities for path in one.paths],
        )

        two = await operation(
            workspace=self.workspace,
            request=_request(
                "code_impact_analyze",
                workspace_id=self.workspace.workspace_id,
                qualified_name="core.target",
                max_depth=2,
            ),
        )
        self.assertEqual(
            ["service.middle", "api.entry"],
            [item.qualified_name for item in two.affected],
        )
        self.assertEqual(3, len(two.paths[-1].entities))

        with self.assertRaises(Exception) as raised:
            await operation(
                workspace=self.workspace,
                request=_request(
                    "code_impact_analyze",
                    workspace_id="ws_0123456789abcdef01234567",
                    qualified_name="core.target",
                ),
            )
        self.assertEqual("UNAUTHORIZED_WORKSPACE", raised.exception.code)

        missing = "code_" + "f" * 64
        with self.assertRaises(Exception) as raised:
            await operation(
                workspace=self.workspace,
                request=_request(
                    "code_impact_analyze",
                    workspace_id=self.workspace.workspace_id,
                    code_entity_id=missing,
                ),
            )
        self.assertEqual("NOT_FOUND", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
