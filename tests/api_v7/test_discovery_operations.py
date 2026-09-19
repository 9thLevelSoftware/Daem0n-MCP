from __future__ import annotations

# ruff: noqa: I001 -- preserve the repository's CRLF import block.

import asyncio
import inspect
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import patch

from daem0nmcp.api.v7.application import AdmittedRequest


NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)


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
    return AdmittedRequest(
        tool_name,
        MappingProxyType(model.model_dump(mode="python")),
    )


class DiscoveryOperationTests(unittest.IsolatedAsyncioTestCase):
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
        self.dependencies = None

    async def asyncTearDown(self) -> None:
        if self.dependencies is not None:
            self.dependencies.close()

    def _operations(self, **changes: object):
        from daem0nmcp.api.v7.discovery_operations import (
            DiscoveryOperationDependencies,
            build_discovery_operations,
        )

        options: dict[str, object] = {
            "clock": lambda: NOW,
            "cursor_secret": b"discovery-cursor-secret-32-bytes!",
        }
        options.update(changes)
        self.dependencies = DiscoveryOperationDependencies(**options)
        return build_discovery_operations(self.dependencies)

    def _append_memory(
        self,
        connection: sqlite3.Connection,
        suffix: str,
        content: str,
    ) -> str:
        from daem0nmcp.event_store import EventCommand, EventStore

        record_id = "mem_" + suffix * 64
        EventStore(connection).append_and_project(
            EventCommand(
                workspace_id=self.workspace.workspace_id,
                stream_id=record_id,
                stream_kind="memory",
                event_type="memory.created",
                occurred_at_us=100,
                recorded_at_us=200 + ord(suffix),
                actor_type="system",
                payload={
                    "record": {
                        "record_type": "decision",
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
                        "source_client": "discovery-test",
                        "source_model": None,
                        "deleted_at_us": None,
                    }
                },
            )
        )
        return record_id

    def _append_relationship(
        self,
        connection: sqlite3.Connection,
        source_record_id: str,
        target_record_id: str,
    ) -> None:
        from daem0nmcp.event_store import EventCommand, EventStore

        EventStore(connection).append_and_project(
            EventCommand(
                workspace_id=self.workspace.workspace_id,
                stream_id="rel_" + "d" * 64,
                stream_kind="relationship",
                event_type="relationship.created",
                occurred_at_us=110,
                recorded_at_us=310,
                actor_type="system",
                payload={
                    "relationship": {
                        "source_record_id": source_record_id,
                        "target_record_id": target_record_id,
                        "relationship_type": "depends_on",
                        "legacy_type": None,
                        "description": "canonical dependency",
                        "confidence": 1.0,
                        "metadata": {},
                        "valid_from_us": 100,
                        "valid_to_us": None,
                    }
                },
            )
        )

    def _append_record_ref(
        self,
        connection: sqlite3.Connection,
        source_record_id: str,
        target_record_id: str,
    ) -> None:
        from daem0nmcp.event_store import EventCommand, EventStore

        EventStore(connection).append_and_project(
            EventCommand(
                workspace_id=self.workspace.workspace_id,
                stream_id="fact_" + "e" * 64,
                stream_kind="fact",
                event_type="fact.asserted",
                occurred_at_us=120,
                recorded_at_us=320,
                actor_type="system",
                payload={
                    "fact": {
                        "subject_record_id": source_record_id,
                        "predicate": "uses",
                        "object_kind": "record_ref",
                        "object": target_record_id,
                        "legacy_type": None,
                        "confidence": 1.0,
                        "verification_count": 1,
                        "is_verified": True,
                        "evidence": [],
                        "metadata": {},
                        "valid_from_us": 100,
                        "valid_to_us": None,
                    }
                },
            )
        )

    def _activate_graph(self) -> None:
        from daem0nmcp.retrieval.specialized_projection import (
            SpecializedProjectionBuilder,
        )

        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            first = self._append_memory(connection, "a", "First record.")
            second = self._append_memory(connection, "b", "Second record.")
            third = self._append_memory(connection, "c", "Third record.")
            self._append_relationship(connection, first, second)
            self._append_record_ref(connection, second, third)
            SpecializedProjectionBuilder(connection, clock_us=lambda: 900).rebuild(
                self.workspace.workspace_id, "graph"
            )
            connection.commit()

    def _activate_discovery(self) -> dict[str, object]:
        from daem0nmcp.discovery_projection import (
            CodeEntityProjectionSeed,
            CommunityProjectionSeed,
            DiscoveryProjectionBuilder,
            EntityProjectionSeed,
            EntityRecordSeed,
        )

        self._activate_graph()
        first = "mem_" + "a" * 64
        second = "mem_" + "b" * 64
        third = "mem_" + "c" * 64
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            builder = DiscoveryProjectionBuilder(connection, clock_us=lambda: 1_000)
            graph = builder.populate_graph(
                self.workspace.workspace_id,
                entities=(
                    EntityProjectionSeed(
                        name="Authentication",
                        entity_type="concept",
                        records=(
                            EntityRecordSeed(first, 2),
                            EntityRecordSeed(second, 1),
                        ),
                    ),
                    EntityProjectionSeed(
                        name="Session",
                        entity_type="concept",
                        records=(EntityRecordSeed(third),),
                    ),
                ),
                communities=(
                    CommunityProjectionSeed(
                        source_key="root",
                        label="Architecture",
                        level=1,
                        member_record_ids=(first, second, third),
                    ),
                    CommunityProjectionSeed(
                        source_key="auth",
                        label="Authentication",
                        level=0,
                        parent_source_key="root",
                        member_record_ids=(first, second),
                    ),
                ),
            )
            code = builder.rebuild_code(
                self.workspace.workspace_id,
                entities=(
                    CodeEntityProjectionSeed(
                        source_key="login",
                        kind="function",
                        qualified_name="auth.login",
                        relative_file_path="src/auth.py",
                        start_line=10,
                        end_line=20,
                    ),
                    CodeEntityProjectionSeed(
                        source_key="logout",
                        kind="function",
                        qualified_name="auth.logout",
                        relative_file_path="src/auth.py",
                        start_line=22,
                        end_line=28,
                    ),
                    CodeEntityProjectionSeed(
                        source_key="service",
                        kind="class",
                        qualified_name="auth.Service",
                        relative_file_path="src/service.py",
                        start_line=1,
                        end_line=50,
                    ),
                ),
            )
            connection.commit()
        return {
            "records": (first, second, third),
            "entity_ids": graph.entity_ids,
            "community_ids": graph.community_ids,
            "code_ids": code.code_entity_ids,
        }

    async def test_registry_is_immutable_and_handler_is_keyword_only(self) -> None:
        """A mutable or positional adapter could bypass production admission."""
        operations = self._operations()

        self.assertEqual(
            {
                "code_index",
                "code_search",
                "community_get",
                "community_list",
                "entity_list",
                "knowledge_graph_stats",
                "memory_recall_entity",
            },
            set(operations),
        )
        with self.assertRaises(TypeError):
            operations["code_search"] = object()
        parameters = tuple(
            inspect.signature(operations["knowledge_graph_stats"]).parameters.values()
        )
        self.assertEqual(("workspace", "request"), tuple(p.name for p in parameters))
        self.assertTrue(
            all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in parameters)
        )

    async def test_code_index_builds_the_canonical_code_partition(self) -> None:
        """Indexing writes only relative canonical rows and returns the code manifest."""
        from daem0nmcp.api.v7.tools import CodeIndexData

        source = self.root / "src" / "auth.py"
        source.parent.mkdir()
        source.write_text("def login():\n    return True\n", encoding="utf-8")

        class FakeIndexer:
            available = True

            @staticmethod
            def get_supported_extensions():
                return [".py"]

            @staticmethod
            def index_source_strict(file_path, project_path, source_bytes):
                self.assertEqual(source.resolve(), file_path)
                self.assertEqual(self.root, project_path)
                self.assertEqual(source.read_bytes(), source_bytes)
                yield {
                    "entity_type": "function",
                    "name": "login",
                    "qualified_name": "auth.login",
                    "line_start": 1,
                    "line_end": 2,
                }

        operation = self._operations(code_indexer_factory=lambda: FakeIndexer())[
            "code_index"
        ]
        result = await operation(
            workspace=self.workspace,
            request=_request(
                "code_index",
                workspace_id=self.workspace.workspace_id,
                relative_root="src",
                patterns=["**/*.py"],
            ),
        )
        self.assertIsInstance(result, CodeIndexData)
        self.assertEqual("code", result.manifest.projection)
        self.assertEqual(
            (1, 1, 0), (result.files_seen, result.files_indexed, result.skipped)
        )
        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                "SELECT relative_file_path,qualified_name FROM discovery_code_entities"
            ).fetchone()
            self.assertEqual(("src/auth.py", "auth.login"), row)

    async def test_code_index_filters_unsupported_files_and_counts_them(self) -> None:
        """A broad glob must not report README and binary files as indexed code."""
        source = self.root / "src" / "auth.py"
        readme = self.root / "src" / "README.md"
        source.parent.mkdir()
        source.write_text("def login():\n    return True\n", encoding="utf-8")
        readme.write_text("# not source code\n", encoding="utf-8")
        visited: list[str] = []

        class FakeIndexer:
            available = True

            @staticmethod
            def get_supported_extensions():
                return [".py"]

            @staticmethod
            def index_source_strict(file_path, project_path, source_bytes):
                del project_path, source_bytes
                visited.append(file_path.suffix)
                if file_path.suffix == ".py":
                    yield {
                        "entity_type": "function",
                        "name": "login",
                        "qualified_name": "auth.login",
                        "line_start": 1,
                        "line_end": 2,
                    }

        result = await self._operations(code_indexer_factory=lambda: FakeIndexer())[
            "code_index"
        ](
            workspace=self.workspace,
            request=_request(
                "code_index",
                workspace_id=self.workspace.workspace_id,
                relative_root="src",
                patterns=["**/*"],
            ),
        )

        self.assertEqual([".py"], visited)
        self.assertEqual(
            (2, 1, 1),
            (result.files_seen, result.files_indexed, result.skipped),
        )

    async def test_code_index_strict_producer_failure_preserves_active_index(
        self,
    ) -> None:
        """A swallowed parser failure cannot replace a valid canonical partition."""
        from daem0nmcp.api.v7.discovery_operations import (
            DiscoveryOperationError,
            _StrictTreeSitterProducer,
        )

        self._activate_discovery()
        source = self.root / "src" / "auth.py"
        source.parent.mkdir(exist_ok=True)
        source.write_text("def login():\n    return True\n", encoding="utf-8")
        with closing(sqlite3.connect(self.database)) as connection:
            before = connection.execute(
                "SELECT generation FROM projection_manifests "
                "WHERE workspace_id=? AND projection_name='code' "
                "AND status='active'",
                (self.workspace.workspace_id,),
            ).fetchone()[0]

        class SwallowingTreeSitterDelegate:
            available = True

            @staticmethod
            def index_file(file_path, project_path):
                del file_path, project_path
                return iter(())

            @staticmethod
            def _get_cached_tree(file_path, source_bytes, language_name):
                del file_path, source_bytes, language_name
                raise RuntimeError("parser failed")

            @staticmethod
            def get_parser(language_name):
                del language_name
                return object(), object()

            @staticmethod
            def _extract_entities(*arguments):
                del arguments
                return iter(())

        operation = self._operations(
            code_indexer_factory=lambda: _StrictTreeSitterProducer(
                SwallowingTreeSitterDelegate(), {".py": "python"}
            )
        )["code_index"]
        with self.assertRaises(DiscoveryOperationError) as raised:
            await operation(
                workspace=self.workspace,
                request=_request(
                    "code_index",
                    workspace_id=self.workspace.workspace_id,
                    relative_root="src",
                    patterns=["**/*.py"],
                    force=True,
                ),
            )
        self.assertEqual("CAPABILITY_DEGRADED", raised.exception.code)
        with closing(sqlite3.connect(self.database)) as connection:
            active = connection.execute(
                "SELECT generation,row_count FROM projection_manifests "
                "WHERE workspace_id=? AND projection_name='code' "
                "AND status='active'",
                (self.workspace.workspace_id,),
            ).fetchone()
        self.assertEqual((before, 3), active)

    def test_non_python_strict_producer_contract_is_imports_only(self) -> None:
        """The legacy multi-language parser does not advertise call/reference facts."""
        from daem0nmcp.api.v7.discovery_operations import _StrictTreeSitterProducer

        class Root:
            has_error = False

        class Tree:
            root_node = Root()

        class JavaScriptDelegate:
            available = True

            @staticmethod
            def _get_cached_tree(file_path, source_bytes, language_name):
                del file_path, source_bytes, language_name
                return Tree()

            @staticmethod
            def get_parser(language_name):
                del language_name
                return object(), object()

            @staticmethod
            def _extract_imports(tree, language, language_name, source_bytes):
                del tree, language, language_name, source_bytes
                return ["dep"]

            @staticmethod
            def _extract_entities(*arguments):
                del arguments
                return iter(
                    (
                        {
                            "entity_type": "function",
                            "name": "entry",
                            "qualified_name": "app.entry",
                            "line_start": 1,
                            "line_end": 3,
                        },
                    )
                )

        producer = _StrictTreeSitterProducer(
            JavaScriptDelegate(), {".js": "javascript"}
        )
        entities = tuple(
            producer.index_source_strict(
                self.root / "app.js", self.root, b"import dep from 'dep';"
            )
        )

        self.assertEqual(1, len(entities))
        self.assertEqual([], entities[0]["calls"])
        self.assertEqual([], entities[0]["references"])
        self.assertEqual([{"name": "dep", "line": None}], entities[0]["imports"])

    async def test_code_index_cancellation_rolls_back_before_activation(self) -> None:
        """A cancelled scan cannot publish later from a detached worker."""
        source = self.root / "src" / "auth.py"
        source.parent.mkdir()
        source.write_text("def login():\n    return True\n", encoding="utf-8")
        started = threading.Event()
        release = threading.Event()

        class BlockingIndexer:
            available = True

            @staticmethod
            def get_supported_extensions():
                return [".py"]

            @staticmethod
            def index_source_strict(file_path, project_path, source_bytes):
                del file_path, project_path, source_bytes
                started.set()
                release.wait(timeout=2)
                yield {
                    "entity_type": "function",
                    "name": "login",
                    "qualified_name": "auth.login",
                    "line_start": 1,
                    "line_end": 2,
                }

        operation = self._operations(code_indexer_factory=lambda: BlockingIndexer())[
            "code_index"
        ]
        task = asyncio.create_task(
            operation(
                workspace=self.workspace,
                request=_request(
                    "code_index",
                    workspace_id=self.workspace.workspace_id,
                    relative_root="src",
                    patterns=["**/*.py"],
                ),
            )
        )
        self.assertTrue(await asyncio.to_thread(started.wait, 1))
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT count(*) FROM projection_manifests "
                    "WHERE projection_name='code'"
                ).fetchone()[0],
            )

    async def test_code_index_cancellation_before_commit_rolls_back_generation(
        self,
    ) -> None:
        """Cancellation after row writes but before commit must undo the generation."""
        from daem0nmcp.api.v7.discovery_operations import (
            DiscoveryProjectionBuilder as RealBuilder,
        )

        source = self.root / "src" / "auth.py"
        source.parent.mkdir()
        source.write_text("def login():\n    return True\n", encoding="utf-8")
        before_commit_reached = threading.Event()
        release_commit = threading.Event()

        class FakeIndexer:
            available = True

            @staticmethod
            def get_supported_extensions():
                return [".py"]

            @staticmethod
            def index_source_strict(file_path, project_path, source_bytes):
                del file_path, project_path, source_bytes
                yield {
                    "entity_type": "function",
                    "name": "login",
                    "qualified_name": "auth.login",
                    "line_start": 1,
                    "line_end": 2,
                }

        class BlockingBuilder(RealBuilder):
            def rebuild_code(self, *args, before_commit=None, **kwargs):
                def blocking_checkpoint():
                    before_commit_reached.set()
                    release_commit.wait(timeout=2)
                    if before_commit is not None:
                        before_commit()

                return super().rebuild_code(
                    *args,
                    before_commit=blocking_checkpoint,
                    **kwargs,
                )

        operation = self._operations(code_indexer_factory=lambda: FakeIndexer())[
            "code_index"
        ]
        with patch(
            "daem0nmcp.api.v7.discovery_operations.DiscoveryProjectionBuilder",
            BlockingBuilder,
        ):
            task = asyncio.create_task(
                operation(
                    workspace=self.workspace,
                    request=_request(
                        "code_index",
                        workspace_id=self.workspace.workspace_id,
                        relative_root="src",
                        patterns=["**/*.py"],
                    ),
                )
            )
            self.assertTrue(await asyncio.to_thread(before_commit_reached.wait, 1))
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release_commit.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                (0, 0, 0),
                (
                    connection.execute(
                        "SELECT count(*) FROM projection_manifests "
                        "WHERE projection_name='code'"
                    ).fetchone()[0],
                    connection.execute(
                        "SELECT count(*) FROM discovery_code_entities"
                    ).fetchone()[0],
                    connection.execute(
                        "SELECT count(*) FROM public_object_ids "
                        "WHERE object_kind='code'"
                    ).fetchone()[0],
                ),
            )

    async def test_mutation_boundary_returns_a_result_after_late_cancellation(
        self,
    ) -> None:
        """Once commit wins the race, callers receive its receipt instead of CANCELLED."""
        from daem0nmcp.api.v7.discovery_operations import _run_mutation

        self._operations()
        committed = threading.Event()
        release = threading.Event()

        def operation(cancelled):
            del cancelled
            committed.set()
            release.wait(timeout=2)
            return "committed-receipt"

        task = asyncio.create_task(_run_mutation(self.dependencies, operation))
        self.assertTrue(await asyncio.to_thread(committed.wait, 1))
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        self.assertEqual("committed-receipt", await task)

    async def test_code_search_filters_and_hmac_cursor_bind_to_the_generation(
        self,
    ) -> None:
        """A cursor from another query or code rebuild must never retarget rows."""
        from daem0nmcp.api.v7.discovery_operations import DiscoveryOperationError
        from daem0nmcp.api.v7.models import Page
        from daem0nmcp.api.v7.tools import CodeEntitySummary
        from daem0nmcp.discovery_projection import (
            CodeEntityProjectionSeed,
            DiscoveryProjectionBuilder,
        )

        self._activate_discovery()
        operation = self._operations()["code_search"]
        first = await operation(
            workspace=self.workspace,
            request=_request(
                "code_search",
                workspace_id=self.workspace.workspace_id,
                query="auth.",
                entity_kinds={"function"},
                limit=1,
            ),
        )
        self.assertIsInstance(first, Page)
        self.assertEqual(1, len(first.items))
        self.assertIsInstance(first.items[0], CodeEntitySummary)
        self.assertTrue(first.truncated)
        self.assertIsNotNone(first.next_cursor)

        second = await operation(
            workspace=self.workspace,
            request=_request(
                "code_search",
                workspace_id=self.workspace.workspace_id,
                query="auth.",
                entity_kinds={"function"},
                limit=1,
                cursor=first.next_cursor,
            ),
        )
        self.assertEqual(1, len(second.items))
        self.assertNotEqual(
            first.items[0].code_entity_id,
            second.items[0].code_entity_id,
        )
        with self.assertRaises(DiscoveryOperationError) as raised:
            await operation(
                workspace=self.workspace,
                request=_request(
                    "code_search",
                    workspace_id=self.workspace.workspace_id,
                    query="logout",
                    entity_kinds={"function"},
                    limit=1,
                    cursor=first.next_cursor,
                ),
            )
        self.assertEqual("INVALID_ARGUMENT", raised.exception.code)

        tampered = first.next_cursor[:-1] + (
            "0" if first.next_cursor[-1] != "0" else "1"
        )
        for cursor, kinds in (
            (tampered, {"function"}),
            (first.next_cursor, {"class"}),
        ):
            with self.subTest(cursor=cursor, kinds=kinds):
                with self.assertRaises(DiscoveryOperationError) as raised:
                    await operation(
                        workspace=self.workspace,
                        request=_request(
                            "code_search",
                            workspace_id=self.workspace.workspace_id,
                            query="auth.",
                            entity_kinds=kinds,
                            limit=1,
                            cursor=cursor,
                        ),
                    )
                self.assertEqual("INVALID_ARGUMENT", raised.exception.code)

        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            DiscoveryProjectionBuilder(connection).rebuild_code(
                self.workspace.workspace_id,
                entities=(
                    CodeEntityProjectionSeed(
                        source_key="new",
                        kind="function",
                        qualified_name="auth.new_login",
                        relative_file_path="src/auth.py",
                        start_line=1,
                        end_line=2,
                    ),
                ),
                force=True,
            )
        with self.assertRaises(DiscoveryOperationError) as raised:
            await operation(
                workspace=self.workspace,
                request=_request(
                    "code_search",
                    workspace_id=self.workspace.workspace_id,
                    query="auth.",
                    entity_kinds={"function"},
                    limit=1,
                    cursor=first.next_cursor,
                ),
            )
        self.assertEqual("INVALID_ARGUMENT", raised.exception.code)

    async def test_code_search_rejects_unsafe_rows_even_with_a_matching_digest(
        self,
    ) -> None:
        """A forged partition hash cannot turn an absolute/traversal path into wire data."""
        from daem0nmcp.api.v7.discovery_operations import DiscoveryOperationError
        from daem0nmcp.event_store import sha256_json

        self._activate_discovery()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("DROP TRIGGER discovery_code_entities_no_update")
            connection.execute("DROP TRIGGER discovery_projection_partitions_no_update")
            connection.execute("PRAGMA ignore_check_constraints=ON")
            connection.execute(
                "UPDATE discovery_code_entities SET relative_file_path='../secret.py' "
                "WHERE workspace_id=? AND qualified_name='auth.logout'",
                (self.workspace.workspace_id,),
            )
            connection.execute("PRAGMA ignore_check_constraints=OFF")
            rows = connection.execute(
                "SELECT code_entity_id,end_line,identity_hash,kind,normalized_name,"
                "qualified_name,relative_file_path,start_line "
                "FROM discovery_code_entities WHERE workspace_id=? "
                "AND code_generation=1 ORDER BY identity_hash",
                (self.workspace.workspace_id,),
            ).fetchall()
            digest = sha256_json(
                [
                    {
                        "code_entity_id": row["code_entity_id"],
                        "end_line": row["end_line"],
                        "identity_hash": row["identity_hash"],
                        "kind": row["kind"],
                        "normalized_name": row["normalized_name"],
                        "qualified_name": row["qualified_name"],
                        "relative_file_path": row["relative_file_path"],
                        "start_line": row["start_line"],
                    }
                    for row in rows
                ]
            )
            connection.execute(
                "UPDATE discovery_projection_partitions SET content_hash=? "
                "WHERE workspace_id=? AND projection_name='code' AND generation=1",
                (digest, self.workspace.workspace_id),
            )
            connection.execute(
                "UPDATE projection_manifests SET source_event_root_hash=? "
                "WHERE workspace_id=? AND projection_name='code' AND generation=1",
                (digest, self.workspace.workspace_id),
            )
            connection.commit()

        with self.assertRaises(DiscoveryOperationError) as raised:
            await self._operations()["code_search"](
                workspace=self.workspace,
                request=_request(
                    "code_search",
                    workspace_id=self.workspace.workspace_id,
                    query="login",
                ),
            )
        self.assertEqual("CAPABILITY_DEGRADED", raised.exception.code)
        self.assertNotIn(str(self.root), str(raised.exception))

    async def test_entity_and_community_reads_use_only_active_partitions(self) -> None:
        """Lists and detail membership must agree on one active graph generation."""
        from daem0nmcp.api.v7.models import Page
        from daem0nmcp.api.v7.tools import CommunityDetail, EntitySummary

        fixture = self._activate_discovery()
        operations = self._operations()
        entities = await operations["entity_list"](
            workspace=self.workspace,
            request=_request(
                "entity_list",
                workspace_id=self.workspace.workspace_id,
                entity_type="concept",
                limit=1,
            ),
        )
        self.assertIsInstance(entities, Page)
        self.assertIsInstance(entities.items[0], EntitySummary)
        self.assertTrue(entities.truncated)
        next_entities = await operations["entity_list"](
            workspace=self.workspace,
            request=_request(
                "entity_list",
                workspace_id=self.workspace.workspace_id,
                entity_type="concept",
                limit=1,
                cursor=entities.next_cursor,
            ),
        )
        self.assertNotEqual(
            entities.items[0].entity_id,
            next_entities.items[0].entity_id,
        )

        roots = await operations["community_list"](
            workspace=self.workspace,
            request=_request(
                "community_list",
                workspace_id=self.workspace.workspace_id,
                level=1,
            ),
        )
        self.assertEqual(1, len(roots.items))
        children = await operations["community_list"](
            workspace=self.workspace,
            request=_request(
                "community_list",
                workspace_id=self.workspace.workspace_id,
                parent_community_id=roots.items[0].community_id,
            ),
        )
        self.assertEqual(1, len(children.items))
        detail = await operations["community_get"](
            workspace=self.workspace,
            request=_request(
                "community_get",
                workspace_id=self.workspace.workspace_id,
                community_id=children.items[0].community_id,
                limit=1,
            ),
        )
        self.assertIsInstance(detail, CommunityDetail)
        self.assertEqual(children.items[0], detail.community)
        self.assertEqual(1, len(detail.members.items))
        self.assertTrue(detail.members.truncated)
        self.assertIn(detail.members.items[0].record_id, fixture["records"])

    async def test_stale_community_id_is_distinct_from_not_found(self) -> None:
        """Generation-bound community IDs cannot silently select rebuilt clusters."""
        from daem0nmcp.api.v7.discovery_operations import DiscoveryOperationError
        from daem0nmcp.discovery_projection import (
            CommunityProjectionSeed,
            DiscoveryProjectionBuilder,
            EntityProjectionSeed,
        )

        fixture = self._activate_discovery()
        old_id = fixture["community_ids"][0]
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            self._append_memory(connection, "f", "New graph source")
            from daem0nmcp.retrieval.specialized_projection import (
                SpecializedProjectionBuilder,
            )

            SpecializedProjectionBuilder(connection).rebuild(
                self.workspace.workspace_id, "graph"
            )
            DiscoveryProjectionBuilder(connection).populate_graph(
                self.workspace.workspace_id,
                entities=(
                    EntityProjectionSeed(
                        name="New",
                        entity_type="concept",
                    ),
                ),
                communities=(
                    CommunityProjectionSeed(
                        source_key="new",
                        label="New",
                        level=0,
                    ),
                ),
            )
            connection.commit()
        with self.assertRaises(DiscoveryOperationError) as raised:
            await self._operations()["community_get"](
                workspace=self.workspace,
                request=_request(
                    "community_get",
                    workspace_id=self.workspace.workspace_id,
                    community_id=old_id,
                ),
            )
        self.assertEqual("STALE_PROJECTION_ID", raised.exception.code)

    async def test_memory_recall_entity_hydrates_exact_members_through_task8(
        self,
    ) -> None:
        """Entity membership selects IDs; Task 8 authenticates the returned records."""
        from daem0nmcp.api.v7.models import (
            CitationManifestEntry,
            EvidenceItem,
            EvidenceRef,
            ProviderDiagnostic,
            RecordSummary,
            RetrievalData,
            TokenUsage,
        )

        self._activate_discovery()
        records: dict[str, RecordSummary] = {}
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            for row in connection.execute(
                "SELECT record_id,record_type,content,content_hash,tags_json,"
                "file_path_relative,created_at_us,updated_at_us FROM memory_records "
                "WHERE workspace_id=?",
                (self.workspace.workspace_id,),
            ):
                records[row["record_id"]] = RecordSummary(
                    record_id=row["record_id"],
                    record_type=row["record_type"],
                    excerpt=row["content"],
                    tags=[],
                    relative_file_path=row["file_path_relative"],
                    current_status="current",
                    content_hash=row["content_hash"],
                    created_at=datetime.fromtimestamp(
                        row["created_at_us"] / 1_000_000, timezone.utc
                    ),
                    updated_at=datetime.fromtimestamp(
                        row["updated_at_us"] / 1_000_000, timezone.utc
                    ),
                )

        test_case = self

        class RecallService:
            def __init__(self):
                self.queries = []

            async def retrieve(self, workspace, query, linked_workspace_ids):
                test_case.assertEqual(test_case.workspace, workspace)
                test_case.assertEqual(frozenset(), linked_workspace_ids)
                self.queries.append(query)
                items = []
                manifest = []
                for index, record_id in enumerate(sorted(query.record_ids), 1):
                    record = records[record_id]
                    citation = f"[E{index}]"
                    ref = EvidenceRef(
                        record_id=record_id,
                        event_id="evt_" + ("a" if index == 1 else "b") * 64,
                        content_hash=record.content_hash,
                        provider="lexical",
                    )
                    items.append(
                        EvidenceItem(
                            citation=citation,
                            record=record,
                            bounded_excerpt=record.excerpt,
                            channels=["lexical"],
                            score=1.0,
                            status="current",
                            evidence_refs=[ref],
                        )
                    )
                    manifest.append(
                        CitationManifestEntry(
                            citation=citation,
                            evidence_refs=[ref],
                            channels=["lexical"],
                        )
                    )
                return RetrievalData(
                    items=items,
                    rendered_context="Authenticated entity records",
                    citation_manifest=manifest,
                    provider_diagnostics=[
                        ProviderDiagnostic(
                            provider="lexical",
                            status="ready",
                            manifest_generation=1,
                            elapsed_ms=0.0,
                            returned_count=len(items),
                        )
                    ],
                    abstained=False,
                    token_usage=TokenUsage(
                        budget=query.token_budget,
                        requested=len(items),
                        selected=len(items),
                        rendered=0,
                        dropped=0,
                    ),
                )

        recall = RecallService()
        operation = self._operations(recall_service=recall)["memory_recall_entity"]
        first = await operation(
            workspace=self.workspace,
            request=_request(
                "memory_recall_entity",
                workspace_id=self.workspace.workspace_id,
                entity_name="Authentication",
                limit=1,
            ),
        )
        self.assertEqual(1, len(first.items))
        self.assertTrue(first.truncated)
        self.assertEqual(
            frozenset({first.items[0].record_id}),
            recall.queries[0].record_ids,
        )
        second = await operation(
            workspace=self.workspace,
            request=_request(
                "memory_recall_entity",
                workspace_id=self.workspace.workspace_id,
                entity_name="Authentication",
                limit=1,
                cursor=first.next_cursor,
            ),
        )
        self.assertEqual(1, len(second.items))
        self.assertNotEqual(first.items[0].record_id, second.items[0].record_id)

    async def test_memory_recall_entity_missing_members_retry_only_while_stale(
        self,
    ) -> None:
        """A lexical index still catching up is transient; a current one is not."""
        from daem0nmcp.api.v7.discovery_operations import DiscoveryOperationError
        from daem0nmcp.api.v7.models import (
            ProviderDiagnostic,
            RetrievalData,
            TokenUsage,
        )

        self._activate_discovery()

        class RecallService:
            status = "degraded"

            async def retrieve(self, workspace, query, linked_workspace_ids):
                return RetrievalData(
                    provider_diagnostics=[
                        ProviderDiagnostic(
                            provider="lexical",
                            status=self.status,
                            manifest_generation=1,
                            elapsed_ms=0.0,
                            reason=(
                                None
                                if self.status == "ready"
                                else "LEXICAL_REBUILD_REQUIRED"
                            ),
                            returned_count=0,
                        )
                    ],
                    abstained=True,
                    abstention_reason="NO_EVIDENCE",
                    token_usage=TokenUsage(
                        budget=query.token_budget,
                        requested=0,
                        selected=0,
                        rendered=0,
                        dropped=0,
                    ),
                )

        recall = RecallService()
        operation = self._operations(recall_service=recall)["memory_recall_entity"]
        request = _request(
            "memory_recall_entity",
            workspace_id=self.workspace.workspace_id,
            entity_name="Authentication",
        )
        for status, code in (
            ("degraded", "DATABASE_IN_USE"),
            ("ready", "CAPABILITY_DEGRADED"),
        ):
            recall.status = status
            with self.assertRaises(DiscoveryOperationError) as raised:
                await operation(workspace=self.workspace, request=request)
            self.assertEqual(code, raised.exception.code)

    async def test_stats_read_the_active_canonical_graph_snapshot(self) -> None:
        """Counting retained-v6 graph rows would misreport the v7 projection."""
        from daem0nmcp.api.v7.tools import KnowledgeGraphStatsData

        self._activate_graph()
        result = await self._operations()["knowledge_graph_stats"](
            workspace=self.workspace,
            request=_request(
                "knowledge_graph_stats",
                workspace_id=self.workspace.workspace_id,
            ),
        )

        self.assertIsInstance(result, KnowledgeGraphStatsData)
        self.assertEqual(3, result.node_count)
        self.assertEqual(2, result.edge_count)
        self.assertEqual({"record": 3}, result.type_counts)
        self.assertEqual("graph", result.manifest.projection)
        self.assertEqual(1, result.manifest.generation)
        self.assertEqual(
            datetime(1970, 1, 1, tzinfo=timezone.utc).replace(microsecond=900),
            result.manifest.built_at,
        )

    async def test_stale_graph_manifest_fails_closed_with_a_stable_code(self) -> None:
        """A changed typed projection must never inherit an old active manifest."""
        from daem0nmcp.api.v7.discovery_operations import DiscoveryOperationError

        self._activate_graph()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE projection_manifests SET row_count=row_count+1 "
                "WHERE projection_name='graph' AND status='active'"
            )
            connection.commit()

        with self.assertRaises(DiscoveryOperationError) as raised:
            await self._operations()["knowledge_graph_stats"](
                workspace=self.workspace,
                request=_request(
                    "knowledge_graph_stats",
                    workspace_id=self.workspace.workspace_id,
                ),
            )
        self.assertEqual("CAPABILITY_DEGRADED", raised.exception.code)
        self.assertEqual("CAPABILITY_DEGRADED", str(raised.exception))
        self.assertNotIn(str(self.root), str(raised.exception))

    async def test_cross_workspace_graph_endpoint_fails_closed(self) -> None:
        """A globally valid record FK must not become a cross-workspace node."""
        from daem0nmcp.api.v7.discovery_operations import DiscoveryOperationError
        from daem0nmcp.event_store import EventCommand, EventStore
        from daem0nmcp.retrieval.specialized_projection import (
            SpecializedProjectionBuilder,
        )

        other_workspace_id = "ws_ffffffffffffffffffffffff"
        other_record_id = "mem_" + "f" * 64
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            local_record_id = self._append_memory(connection, "a", "Local record.")
            EventStore(connection).append_and_project(
                EventCommand(
                    workspace_id=other_workspace_id,
                    stream_id=other_record_id,
                    stream_kind="memory",
                    event_type="memory.created",
                    occurred_at_us=100,
                    recorded_at_us=250,
                    actor_type="system",
                    payload={
                        "record": {
                            "record_type": "decision",
                            "legacy_type": None,
                            "content": "Foreign record.",
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
                            "source_client": "discovery-test",
                            "source_model": None,
                            "deleted_at_us": None,
                        }
                    },
                )
            )
            self._append_relationship(connection, local_record_id, other_record_id)
            SpecializedProjectionBuilder(connection, clock_us=lambda: 900).rebuild(
                self.workspace.workspace_id, "graph"
            )
            connection.commit()

        with self.assertRaises(DiscoveryOperationError) as raised:
            await self._operations()["knowledge_graph_stats"](
                workspace=self.workspace,
                request=_request(
                    "knowledge_graph_stats",
                    workspace_id=self.workspace.workspace_id,
                ),
            )
        self.assertEqual("CAPABILITY_DEGRADED", raised.exception.code)

    async def test_workspace_and_tool_mismatches_are_rejected(self) -> None:
        """Direct calls must preserve the router's exact workspace/tool binding."""
        from daem0nmcp.api.v7.discovery_operations import DiscoveryOperationError

        self._activate_graph()
        operation = self._operations()["knowledge_graph_stats"]
        wrong_tool = AdmittedRequest(
            "entity_list",
            MappingProxyType({"workspace_id": self.workspace.workspace_id}),
        )
        with self.assertRaises(DiscoveryOperationError) as raised:
            await operation(workspace=self.workspace, request=wrong_tool)
        self.assertEqual("UNAUTHORIZED_WORKSPACE", raised.exception.code)

        wrong_workspace = _request(
            "knowledge_graph_stats",
            workspace_id="ws_0123456789abcdef01234567",
        )
        with self.assertRaises(DiscoveryOperationError) as raised:
            await operation(workspace=self.workspace, request=wrong_workspace)
        self.assertEqual("UNAUTHORIZED_WORKSPACE", raised.exception.code)

    async def test_cancellation_waits_for_the_blocking_reader_to_finish(self) -> None:
        """Cancellation must not leave a detached SQLite reader holding the lock."""
        self._activate_graph()
        started = threading.Event()
        release = threading.Event()
        database = self.database

        class BlockingResolver:
            @contextmanager
            def locked_active(self, workspace):
                del self
                del workspace
                started.set()
                release.wait(timeout=2)
                yield SimpleNamespace(path=database)

        operation = self._operations(storage_resolver=BlockingResolver())[
            "knowledge_graph_stats"
        ]
        task = asyncio.create_task(
            operation(
                workspace=self.workspace,
                request=_request(
                    "knowledge_graph_stats",
                    workspace_id=self.workspace.workspace_id,
                ),
            )
        )
        self.assertTrue(await asyncio.to_thread(started.wait, 1))
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_schema_below_the_current_floor_is_degraded(self) -> None:
        """A reader must not infer contracts from a superseded schema."""
        from daem0nmcp.api.v7.discovery_operations import DiscoveryOperationError
        from daem0nmcp.schema_version import CURRENT_SCHEMA_VERSION

        self._activate_graph()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "DELETE FROM schema_version WHERE version=?",
                (CURRENT_SCHEMA_VERSION,),
            )
            connection.commit()

        with self.assertRaises(DiscoveryOperationError) as raised:
            await self._operations()["knowledge_graph_stats"](
                workspace=self.workspace,
                request=_request(
                    "knowledge_graph_stats",
                    workspace_id=self.workspace.workspace_id,
                ),
            )
        self.assertEqual("CAPABILITY_DEGRADED", raised.exception.code)

    async def test_code_index_publishes_resolved_generation_edges(self) -> None:
        source = self.root / "src" / "service.py"
        source.parent.mkdir()
        source.write_text(
            "def target(): pass\ndef caller(): target()\n", encoding="utf-8"
        )

        class FakeIndexer:
            available = True

            @staticmethod
            def get_supported_extensions():
                return [".py"]

            @staticmethod
            def index_source_strict(file_path, project_path, source_bytes):
                del file_path, project_path, source_bytes
                yield {
                    "entity_type": "function",
                    "name": "target",
                    "qualified_name": "service.target",
                    "line_start": 1,
                    "line_end": 1,
                }
                yield {
                    "entity_type": "function",
                    "name": "caller",
                    "qualified_name": "service.caller",
                    "line_start": 2,
                    "line_end": 2,
                    "calls": [{"name": "target", "line": 2}],
                    "references": ["target"],
                }

        await self._operations(code_indexer_factory=lambda: FakeIndexer())[
            "code_index"
        ](
            workspace=self.workspace,
            request=_request(
                "code_index",
                workspace_id=self.workspace.workspace_id,
                relative_root="src",
                patterns=["**/*.py"],
            ),
        )
        with closing(sqlite3.connect(self.database)) as connection:
            edges = connection.execute(
                "SELECT source.qualified_name,target.qualified_name,edge.edge_kind "
                "FROM discovery_code_edges AS edge "
                "JOIN discovery_code_entities AS source ON "
                "source.workspace_id=edge.workspace_id AND "
                "source.code_generation=edge.code_generation AND "
                "source.code_entity_id=edge.source_code_entity_id "
                "JOIN discovery_code_entities AS target ON "
                "target.workspace_id=edge.workspace_id AND "
                "target.code_generation=edge.code_generation AND "
                "target.code_entity_id=edge.target_code_entity_id "
                "ORDER BY edge.edge_kind"
            ).fetchall()
        self.assertEqual(
            [
                ("service.caller", "service.target", "call"),
                ("service.caller", "service.target", "reference"),
            ],
            edges,
        )
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("DROP TRIGGER discovery_code_edges_no_update")
            connection.execute(
                "UPDATE discovery_code_edges SET edge_kind='import' "
                "WHERE edge_kind='call'"
            )
            connection.commit()
        from daem0nmcp.api.v7.discovery_operations import DiscoveryOperationError

        with self.assertRaises(DiscoveryOperationError) as raised:
            await self._operations()["code_search"](
                workspace=self.workspace,
                request=_request(
                    "code_search",
                    workspace_id=self.workspace.workspace_id,
                    query="target",
                ),
            )
        self.assertEqual("CAPABILITY_DEGRADED", raised.exception.code)

    def test_code_edge_resolution_skips_ambiguous_symbols_and_module_members(
        self,
    ) -> None:
        from daem0nmcp.api.v7.discovery_operations import _resolve_code_edges
        from daem0nmcp.discovery_projection import CodeEntityProjectionSeed

        def seed(key: str, kind: str, name: str) -> CodeEntityProjectionSeed:
            return CodeEntityProjectionSeed(key, kind, name, f"{key}.py", 1, 1)

        seeds = {
            "a": seed("a", "function", "a.target"),
            "b": seed("b", "function", "b.target"),
            "c": seed("c", "function", "c.C.run"),
            "d": seed("d", "method", "d.D.run"),
            "duplicate-1": seed("duplicate-1", "function", "same.name"),
            "duplicate-2": seed("duplicate-2", "function", "same.name"),
            "pkg": seed("pkg", "module", "pkg"),
            "pkg-one": seed("pkg-one", "function", "pkg.one"),
            "pkg-two": seed("pkg-two", "function", "pkg.two"),
            "caller": seed("caller", "function", "caller.entry"),
        }
        edges = _resolve_code_edges(
            seeds,
            {
                "caller": [
                    ("call", "target", 2),
                    ("reference", "run", 3),
                    ("call", "same.name", 4),
                    ("call", "alias", 5),
                    ("import", "target", 6),
                    ("import", "pkg", 7),
                    ("call", "a.target", 8),
                ]
            },
        )

        self.assertEqual(
            (
                ("caller", "a", "call", 8),
                ("caller", "pkg", "import", 7),
            ),
            tuple(
                (edge.source_key, edge.target_key, edge.kind, edge.source_line)
                for edge in edges
            ),
        )

    def test_code_edges_require_receiver_and_source_scope(self) -> None:
        from daem0nmcp.api.v7.discovery_operations import _resolve_code_edges
        from daem0nmcp.discovery_projection import CodeEntityProjectionSeed

        seeds = {
            "method": CodeEntityProjectionSeed(
                "method", "method", "service.Service.run", "service.py", 1, 2
            ),
            "local": CodeEntityProjectionSeed(
                "local", "function", "client.target", "client.py", 1, 2
            ),
            "remote": CodeEntityProjectionSeed(
                "remote", "function", "other.target", "other.py", 1, 2
            ),
            "caller": CodeEntityProjectionSeed(
                "caller", "function", "client.entry", "client.py", 3, 9
            ),
        }
        edges = _resolve_code_edges(
            seeds,
            {
                "caller": [
                    ("call", "unknown.run", 3),
                    ("call", "other.Service.run", 4),
                    ("call", "run", 5),
                    ("reference", "service.Service.RUN", 6),
                    ("call", "target", 7),
                    ("call", "service.Service.run", 8),
                ]
            },
        )
        self.assertEqual(
            {("local", 7), ("method", 8)},
            {(edge.target_key, edge.source_line) for edge in edges},
        )

    def test_python_import_bindings_relative_levels_and_shadowing(self) -> None:
        from daem0nmcp.api.v7.discovery_operations import (
            _python_relationships,
            _resolve_code_edges,
        )
        from daem0nmcp.discovery_projection import CodeEntityProjectionSeed

        calls, imports, _references = _python_relationships(
            b"from .core import target as chosen\n"
            b"import service as svc\n"
            b"def entry():\n    chosen(); svc.run(); unknown.run(); factory().run()\n",
            "client",
        )
        self.assertIn({"name": ".core.target", "line": 1}, imports)
        names = {value["name"] for value in calls}
        self.assertIn(".core.target", names)
        self.assertIn("service.run", names)
        self.assertNotIn("unknown.run", names)
        self.assertNotIn("run", names)
        seeds = {
            "target": CodeEntityProjectionSeed(
                "target", "function", "core.target", "pkg/core.py", 1, 2
            ),
            "wrong": CodeEntityProjectionSeed(
                "wrong", "function", "core.target", "other/core.py", 1, 2
            ),
            "caller": CodeEntityProjectionSeed(
                "caller", "function", "client.entry", "pkg/client.py", 3, 4
            ),
        }
        edges = _resolve_code_edges(seeds, {"caller": [("call", ".core.target", 4)]})
        self.assertEqual(["target"], [edge.target_key for edge in edges])
        shadowed, _, _ = _python_relationships(
            b"from service import run\ndef entry(run):\n    run()\n", "client"
        )
        self.assertEqual([], shadowed)

    async def test_code_index_rejects_disabled_apps_before_loading_parser(self) -> None:
        from daem0nmcp.api.v7.discovery_operations import DiscoveryOperationError

        loaded = False

        def factory():
            nonlocal loaded
            loaded = True
            raise AssertionError("disabled profile loaded optional parser")

        with self.assertRaises(DiscoveryOperationError) as raised:
            await self._operations(
                code_indexer_factory=factory,
                capability_statuses={"apps": "disabled"},
            )["code_index"](
                workspace=self.workspace,
                request=_request(
                    "code_index",
                    workspace_id=self.workspace.workspace_id,
                    relative_root=".",
                ),
            )
        self.assertFalse(loaded)
        self.assertEqual("CAPABILITY_DISABLED", raised.exception.code)
        self.assertEqual("apps", raised.exception.capability_states[0].name)

    async def test_code_index_source_change_rolls_back_before_publish(self) -> None:
        from daem0nmcp.api.v7.discovery_operations import (
            DiscoveryOperationError,
        )
        from daem0nmcp.api.v7.discovery_operations import (
            DiscoveryProjectionBuilder as RealBuilder,
        )

        source = self.root / "src" / "service.py"
        source.parent.mkdir()
        source.write_text("def target(): pass\n", encoding="utf-8")

        class FakeIndexer:
            available = True

            @staticmethod
            def get_supported_extensions():
                return [".py"]

            @staticmethod
            def index_source_strict(file_path, project_path, source_bytes):
                del file_path, project_path, source_bytes
                yield {
                    "entity_type": "function",
                    "name": "target",
                    "qualified_name": "service.target",
                    "line_start": 1,
                    "line_end": 1,
                }

        class ChangingBuilder(RealBuilder):
            def rebuild_code(self, *args, before_commit=None, **kwargs):
                def change_then_check():
                    source.write_text("def changed(): pass\n", encoding="utf-8")
                    if before_commit is not None:
                        before_commit()

                return super().rebuild_code(
                    *args, before_commit=change_then_check, **kwargs
                )

        with (
            patch(
                "daem0nmcp.api.v7.discovery_operations.DiscoveryProjectionBuilder",
                ChangingBuilder,
            ),
            self.assertRaises(DiscoveryOperationError) as raised,
        ):
            await self._operations(code_indexer_factory=lambda: FakeIndexer())[
                "code_index"
            ](
                workspace=self.workspace,
                request=_request(
                    "code_index",
                    workspace_id=self.workspace.workspace_id,
                    relative_root="src",
                    patterns=["**/*.py"],
                ),
            )
        self.assertEqual("EVENT_STREAM_CONFLICT", raised.exception.code)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT count(*) FROM projection_manifests "
                    "WHERE projection_name='code'"
                ).fetchone()[0],
            )


if __name__ == "__main__":
    unittest.main()
