from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import MappingProxyType

import pytest

from daem0nmcp.api.v7.application import AdmittedRequest


def _apply_schema(connection: sqlite3.Connection) -> None:
    from daem0nmcp.migrations.schema import MIGRATIONS
    from daem0nmcp.schema_version import CURRENT_SCHEMA_VERSION

    connection.execute("CREATE TABLE schema_version(version INTEGER PRIMARY KEY)")
    for version, _description, statements in MIGRATIONS:
        if 16 <= version <= CURRENT_SCHEMA_VERSION:
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_version(version) VALUES (?)", (version,)
            )
    connection.commit()


def _request(tool: str, **arguments: object) -> AdmittedRequest:
    from daem0nmcp.api.v7.tools import TOOL_INPUT_MODELS

    value = TOOL_INPUT_MODELS[tool].model_validate(arguments)
    return AdmittedRequest(
        tool_name=tool,
        _arguments=MappingProxyType(value.model_dump(mode="python")),
    )


class GraphProjectionBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        from daem0nmcp.workspace import WorkspaceRegistry

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.database = self.root / "graph.db"
        self.connection = sqlite3.connect(self.database)
        self.addCleanup(self.connection.close)
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        _apply_schema(self.connection)
        self.workspace = WorkspaceRegistry([self.root], default_root=self.root).default

    def _append(self, suffix: str, content: str, *, connection=None) -> str:
        from daem0nmcp.event_store import EventCommand, EventStore

        target = self.connection if connection is None else connection
        record_id = "mem_" + suffix * 64
        EventStore(target).append_and_project(
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
                        "source_client": "graph-test",
                        "source_model": None,
                        "deleted_at_us": None,
                    }
                },
            )
        )
        target.commit()
        return record_id

    def _link(self, source: str, target: str, suffix: str = "f") -> None:
        from daem0nmcp.event_store import EventCommand, EventStore

        EventStore(self.connection).append_and_project(
            EventCommand(
                workspace_id=self.workspace.workspace_id,
                stream_id="rel_" + suffix * 64,
                stream_kind="relationship",
                event_type="relationship.created",
                occurred_at_us=110,
                recorded_at_us=400 + ord(suffix),
                actor_type="system",
                payload={
                    "relationship": {
                        "source_record_id": source,
                        "target_record_id": target,
                        "relationship_type": "related_to",
                        "legacy_type": None,
                        "description": None,
                        "confidence": 1.0,
                        "metadata": {},
                        "valid_from_us": 100,
                        "valid_to_us": None,
                    }
                },
            )
        )
        self.connection.commit()

    def test_complete_generation_is_atomic_and_retry_is_idempotent(self) -> None:
        from daem0nmcp.graph_projection import GraphProjectionBuilder

        first = self._append("a", "Use AuthService.login() in auth.py")
        second = self._append("b", "AuthService.login() calls session_store()")
        self._link(first, second)
        builder = GraphProjectionBuilder(self.connection, clock_us=lambda: 1_000)

        built = builder.rebuild(self.workspace.workspace_id)
        retried = builder.rebuild(self.workspace.workspace_id)

        self.assertFalse(built.reused)
        self.assertTrue(retried.reused)
        self.assertEqual(built.generation, retried.generation)
        self.assertGreaterEqual(built.extracted, 2)
        self.assertEqual(1, len(built.communities))
        active = self.connection.execute(
            "SELECT generation,status FROM projection_manifests WHERE workspace_id=? "
            "AND projection_name='graph' AND status='active'",
            (self.workspace.workspace_id,),
        ).fetchall()
        self.assertEqual([(built.generation, "active")], active)
        partitions = self.connection.execute(
            "SELECT partition_name FROM discovery_projection_partitions "
            "WHERE workspace_id=? AND generation=? ORDER BY partition_name",
            (self.workspace.workspace_id, built.generation),
        ).fetchall()
        self.assertEqual([("communities",), ("entities",)], partitions)

    def test_runtime_graph_job_builder_publishes_complete_partitions(self) -> None:
        from daem0nmcp.retrieval.runtime import create_projection_builders

        self._append("a", "Use AuthService.login() in auth.py")
        builders = create_projection_builders(
            self.connection,
            self.database,
            include_optional=True,
            capability_statuses={
                "graph": "ready",
                "local": "disabled",
                "models-local": "disabled",
            },
        )
        try:
            result = builders["graph"](self.workspace.workspace_id)
        finally:
            builders.close()

        partitions = self.connection.execute(
            "SELECT partition_name FROM discovery_projection_partitions "
            "WHERE workspace_id=? AND generation=? ORDER BY partition_name",
            (self.workspace.workspace_id, result.generation),
        ).fetchall()
        self.assertEqual([("communities",), ("entities",)], partitions)

    def test_cancel_before_publish_retains_previous_generation(self) -> None:
        from daem0nmcp.graph_projection import (
            GraphProjectionBuilder,
            GraphProjectionBuildError,
        )

        first = self._append("a", "AuthService.login()")
        second = self._append("b", "AuthService.login()")
        self._link(first, second)
        builder = GraphProjectionBuilder(self.connection, clock_us=lambda: 1_000)
        original = builder.rebuild(self.workspace.workspace_id)
        self._append("c", "NewSession.refresh()")
        cancelled = threading.Event()

        def cancel() -> None:
            rows = self.connection.execute(
                "SELECT generation,status FROM projection_manifests "
                "WHERE workspace_id=? AND projection_name='graph' ORDER BY generation",
                (self.workspace.workspace_id,),
            ).fetchall()
            self.assertEqual("active", rows[0][1])
            self.assertEqual("building", rows[-1][1])
            cancelled.set()

        with self.assertRaises(GraphProjectionBuildError) as raised:
            builder.rebuild(
                self.workspace.workspace_id,
                cancelled=cancelled,
                before_publish=cancel,
            )
        self.assertEqual("CANCELLED", raised.exception.code)
        active = self.connection.execute(
            "SELECT generation FROM projection_manifests WHERE workspace_id=? "
            "AND projection_name='graph' AND status='active'",
            (self.workspace.workspace_id,),
        ).fetchone()
        self.assertEqual(original.generation, active[0])
        self.assertEqual(
            0,
            self.connection.execute(
                "SELECT count(*) FROM projection_manifests WHERE workspace_id=? "
                "AND projection_name='graph' AND status='building'",
                (self.workspace.workspace_id,),
            ).fetchone()[0],
        )

    def test_concurrent_canonical_write_rejects_stale_candidate(self) -> None:
        from daem0nmcp.entity_extractor import EntityExtractor
        from daem0nmcp.graph_projection import (
            GraphProjectionBuilder,
            GraphProjectionBuildError,
        )

        self._append("a", "AuthService.login()")
        owner = self

        class WritingExtractor(EntityExtractor):
            wrote = False

            def extract_all(self, text: str):
                if not self.wrote:
                    self.wrote = True
                    with closing(sqlite3.connect(owner.database)) as concurrent:
                        concurrent.execute("PRAGMA foreign_keys=ON")
                        owner._append("b", "Concurrent.write()", connection=concurrent)
                return super().extract_all(text)

        builder = GraphProjectionBuilder(
            self.connection,
            clock_us=lambda: 1_000,
            extractor_factory=WritingExtractor,
        )
        with self.assertRaises(GraphProjectionBuildError) as raised:
            builder.rebuild(self.workspace.workspace_id)
        self.assertEqual("PROJECTION_BUILD_SUPERSEDED", raised.exception.code)
        self.assertEqual(
            0,
            self.connection.execute(
                "SELECT count(*) FROM projection_manifests WHERE workspace_id=? "
                "AND projection_name='graph' AND status='active'",
                (self.workspace.workspace_id,),
            ).fetchone()[0],
        )

    def test_public_resolution_reaches_real_leiden_configuration(self) -> None:
        from unittest.mock import patch

        from daem0nmcp.graph.leiden import LeidenResult
        from daem0nmcp.graph_projection import GraphProjectionBuilder

        first = self._append("a", "AuthService.login()")
        second = self._append("b", "AuthService.login()")
        self._link(first, second)
        observed: list[float] = []

        def cluster(nodes, edges, config, **kwargs):
            observed.append(config.resolution)
            return LeidenResult(dict.fromkeys(nodes, 0), 0.0)

        with patch("daem0nmcp.graph.leiden.run_leiden_bounded", cluster):
            GraphProjectionBuilder(self.connection, clock_us=lambda: 1_000).rebuild(
                self.workspace.workspace_id,
                resolution=2.75,
            )
        self.assertEqual([2.75], observed)

    def test_community_ids_remain_bound_to_source_keys_on_build_and_reuse(self) -> None:
        from unittest.mock import patch

        from daem0nmcp.graph.leiden import LeidenResult
        from daem0nmcp.graph_projection import GraphProjectionBuilder

        record_ids = [
            self._append(suffix, f"Group{index:02d}Service.run()")
            for index, suffix in enumerate("01234567")
        ]

        def partition(nodes, edges, config, **kwargs):
            return LeidenResult(
                {node: index // 2 for index, node in enumerate(nodes)}, 0.0
            )

        builder = GraphProjectionBuilder(self.connection, clock_us=lambda: 1_000)
        with patch("daem0nmcp.graph.leiden.run_leiden_bounded", partition):
            built = builder.rebuild(self.workspace.workspace_id)
            reused = builder.rebuild(self.workspace.workspace_id)

        self.assertFalse(built.reused)
        self.assertTrue(reused.reused)
        self.assertEqual(
            set(record_ids),
            {item for seed in built.communities for item in seed.member_record_ids},
        )
        for result in (built, reused):
            for seed in result.communities:
                community_id = result.community_ids_by_source_key[seed.source_key]
                row = self.connection.execute(
                    "SELECT label,member_count FROM discovery_communities "
                    "WHERE workspace_id=? AND graph_generation=? AND community_id=?",
                    (self.workspace.workspace_id, result.generation, community_id),
                ).fetchone()
                self.assertEqual((seed.label, len(seed.member_record_ids)), tuple(row))

    def test_maximum_legal_content_skips_oversized_extracted_names(self) -> None:
        from daem0nmcp.graph_projection import GraphProjectionBuilder

        content = "a" * 257 + "()"
        content += " " * (100_000 - len(content))
        self._append("a", content)

        result = GraphProjectionBuilder(
            self.connection, clock_us=lambda: 1_000
        ).rebuild(self.workspace.workspace_id)

        self.assertEqual(1, result.scanned)
        self.assertEqual(0, result.extracted)
        self.assertGreaterEqual(result.skipped, 1)
        self.assertEqual(1, result.generation)

    def test_content_budget_stops_before_extraction_or_partial_generation(self) -> None:
        from unittest.mock import patch

        from daem0nmcp.graph_projection import (
            GraphProjectionBuilder,
            GraphProjectionBuildError,
        )

        class MustNotExtract:
            def extract_all(self, text):
                raise AssertionError("extractor must not run after the content budget")

        self._append("a", "x" * 101)
        with (
            patch("daem0nmcp.graph_projection._MAX_CONTENT_BYTES", 100),
            self.assertRaises(GraphProjectionBuildError) as raised,
        ):
            GraphProjectionBuilder(
                self.connection,
                clock_us=lambda: 1_000,
                extractor_factory=MustNotExtract,
            ).rebuild(self.workspace.workspace_id)
        self.assertEqual("TASK_REQUIRED", raised.exception.code)
        self.assertEqual(
            0,
            self.connection.execute(
                "SELECT count(*) FROM projection_manifests"
            ).fetchone()[0],
        )

    def test_entity_budget_stops_before_native_graph_allocation(self) -> None:
        from unittest.mock import patch

        from daem0nmcp.graph_projection import (
            GraphProjectionBuilder,
            GraphProjectionBuildError,
        )

        class HighCardinalityExtractor:
            def extract_all(self, text):
                return [
                    {"name": f"name_{index}", "type": "function"} for index in range(3)
                ]

        self._append("a", "legal content")
        with (
            patch("daem0nmcp.graph_projection._MAX_ENTITIES", 2),
            patch("daem0nmcp.graph.leiden.run_leiden_bounded") as native,
            self.assertRaises(GraphProjectionBuildError) as raised,
        ):
            GraphProjectionBuilder(
                self.connection,
                clock_us=lambda: 1_000,
                extractor_factory=HighCardinalityExtractor,
            ).rebuild(self.workspace.workspace_id)
        self.assertEqual("TASK_REQUIRED", raised.exception.code)
        native.assert_not_called()

    def test_edge_budget_stops_before_native_graph_allocation(self) -> None:
        from unittest.mock import patch

        from daem0nmcp.graph_projection import (
            GraphProjectionBuilder,
            GraphProjectionBuildError,
        )

        first = self._append("a", "FirstService.run()")
        second = self._append("b", "SecondService.run()")
        third = self._append("c", "ThirdService.run()")
        self._link(first, second, "f")
        self._link(second, third, "e")
        with (
            patch("daem0nmcp.graph_projection._MAX_EDGES", 1),
            patch("daem0nmcp.graph.leiden.run_leiden_bounded") as native,
            self.assertRaises(GraphProjectionBuildError) as raised,
        ):
            GraphProjectionBuilder(self.connection, clock_us=lambda: 1_000).rebuild(
                self.workspace.workspace_id
            )
        self.assertEqual("TASK_REQUIRED", raised.exception.code)
        native.assert_not_called()

    def test_full_100k_record_contract_reaches_bounded_native_phase(self) -> None:
        from unittest.mock import patch

        from daem0nmcp import graph_projection
        from daem0nmcp.graph import leiden
        from daem0nmcp.graph.leiden import LeidenResult

        records = tuple(
            (f"mem_{index:064x}", "representative content") for index in range(100_000)
        )
        observed: list[tuple[int, int]] = []

        def bounded(nodes, edges, config, **kwargs):
            observed.append((len(nodes), len(edges)))
            return LeidenResult({}, 0.0)

        with patch("daem0nmcp.graph.leiden.run_leiden_bounded", bounded):
            communities, modularity = graph_projection._community_seeds(
                records,
                (),
                (),
                min_community_size=2,
                resolution=1.0,
                cancelled=None,
                deadline=time.monotonic() + 5.0,
            )

        self.assertEqual(100_000, graph_projection._MAX_RECORDS)
        self.assertEqual(100_000, leiden._MAX_NATIVE_NODES)
        self.assertEqual([(100_000, 0)], observed)
        self.assertEqual((), communities)
        self.assertEqual(0.0, modularity)

    def test_real_leiden_resolution_changes_partitioning(self) -> None:
        import networkx as nx

        from daem0nmcp.graph.leiden import LeidenConfig, run_leiden_on_networkx

        graph = nx.path_graph(6)
        coarse = run_leiden_on_networkx(graph, LeidenConfig(resolution=0.1))
        fine = run_leiden_on_networkx(graph, LeidenConfig(resolution=3.0))

        self.assertEqual(1, len(set(coarse.values())))
        self.assertGreater(len(set(fine.values())), len(set(coarse.values())))


class GraphOperationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from daem0nmcp.storage_activation import (
            ActiveDatabasePointer,
            write_active_pointer,
        )
        from daem0nmcp.workspace import WorkspaceRegistry

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        storage = self.root / ".daem0nmcp" / "storage"
        storage.mkdir(parents=True)
        self.database = storage / "daem0nmcp.db"
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            _apply_schema(connection)
        write_active_pointer(
            storage,
            ActiveDatabasePointer(7, 1, self.database.name, None, None),
        )
        self.workspace = WorkspaceRegistry([self.root], default_root=self.root).default
        self.dependencies = None

    async def asyncTearDown(self) -> None:
        if self.dependencies is not None:
            self.dependencies.close()

    def _operations(self, status: str = "ready"):
        from daem0nmcp.api.v7.graph_operations import (
            GraphOperationDependencies,
            build_graph_operations,
        )

        self.dependencies = GraphOperationDependencies(
            capability_statuses={"graph": status}
        )
        return build_graph_operations(self.dependencies)

    async def test_disabled_profile_fails_before_builder_construction(self) -> None:
        from daem0nmcp.api.v7.graph_operations import GraphOperationError

        operations = self._operations("disabled")
        request = _request(
            "entity_backfill",
            workspace_id=self.workspace.workspace_id,
            force=False,
            idempotency_key="disabled-graph",
            preflight_token="0123456789abcdef",
        )
        with self.assertRaises(GraphOperationError) as raised:
            await operations["entity_backfill"](
                workspace=self.workspace, request=request
            )
        self.assertEqual("CAPABILITY_DEGRADED", raised.exception.code)

    async def test_transient_snapshot_races_retry_before_publication(self) -> None:
        from daem0nmcp.api.v7.graph_operations import (
            GraphOperationDependencies,
            build_graph_operations,
        )
        from daem0nmcp.graph_projection import (
            GraphProjectionBuilder,
            GraphProjectionBuildError,
        )

        attempts: list[int] = []

        class RacingBuilder:
            def __init__(self, connection):
                self.real = GraphProjectionBuilder(connection, clock_us=lambda: 1_000)

            def rebuild(self, workspace_id, **kwargs):
                attempts.append(len(attempts) + 1)
                if len(attempts) < 3:
                    raise GraphProjectionBuildError("PROJECTION_BUILD_SUPERSEDED")
                return self.real.rebuild(workspace_id, **kwargs)

        self.dependencies = GraphOperationDependencies(
            capability_statuses={"graph": "ready"},
            builder_factory=RacingBuilder,
        )
        operations = build_graph_operations(self.dependencies)
        result = await operations["entity_backfill"](
            workspace=self.workspace,
            request=_request(
                "entity_backfill",
                workspace_id=self.workspace.workspace_id,
                force=False,
                idempotency_key="retry-superseded-build",
                preflight_token="0123456789abcdef",
            ),
        )

        self.assertEqual([1, 2, 3], attempts)
        self.assertEqual("graph", result.manifest.projection)

    async def test_cancellation_terminates_native_child_and_releases_capacity_and_lock(
        self,
    ) -> None:
        from unittest.mock import patch

        from daem0nmcp.api.v7.graph_operations import (
            GraphOperationDependencies,
            build_graph_operations,
        )
        from daem0nmcp.graph.leiden import run_leiden_bounded as real_runner

        entered = threading.Event()

        def blocking_runner(nodes, edges, config, **kwargs):
            entered.set()
            return real_runner(
                nodes,
                edges,
                config,
                cancelled=kwargs["cancelled"],
                deadline=kwargs["deadline"],
                worker_command=(
                    sys.executable,
                    "-c",
                    "import time; time.sleep(300)",
                ),
            )

        self.dependencies = GraphOperationDependencies(
            capability_statuses={"graph": "ready"}
        )
        operations = build_graph_operations(self.dependencies)
        with patch("daem0nmcp.graph.leiden.run_leiden_bounded", blocking_runner):
            task = asyncio.create_task(
                operations["entity_backfill"](
                    workspace=self.workspace,
                    request=_request(
                        "entity_backfill",
                        workspace_id=self.workspace.workspace_id,
                        idempotency_key="cancel-native-graph",
                        preflight_token="0123456789abcdef",
                    ),
                )
            )
            self.assertTrue(await asyncio.to_thread(entered.wait, 3.0))
            lock_started = time.monotonic()
            with self.dependencies.storage_resolver.locked_active(self.workspace):
                pass
            self.assertLess(time.monotonic() - lock_started, 0.5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=3.0)
        self.assertEqual(0, self.dependencies.worker_pool.in_flight)

    async def test_entity_backfill_force_controls_generation(self) -> None:
        from pydantic import ValidationError

        from daem0nmcp.api.v7.graph_operations import GraphOperationError

        operations = self._operations()

        async def rebuild(key: str, force: object = None):
            arguments: dict[str, object] = {
                "workspace_id": self.workspace.workspace_id,
                "idempotency_key": key,
                "preflight_token": "0123456789abcdef",
            }
            if force is not None:
                arguments["force"] = force
            return await operations["entity_backfill"](
                workspace=self.workspace,
                request=_request("entity_backfill", **arguments),
            )

        omitted = await rebuild("force-omitted")
        explicit_false = await rebuild("force-false", False)
        forced = await rebuild("force-true", True)
        replayed = await rebuild("force-true", True)
        forced_again = await rebuild("force-true-new-key", True)

        self.assertEqual(
            omitted.manifest.generation, explicit_false.manifest.generation
        )
        self.assertEqual(omitted.manifest.generation + 1, forced.manifest.generation)
        self.assertEqual(forced.manifest.generation, replayed.manifest.generation)
        self.assertEqual(
            forced.manifest.generation + 1, forced_again.manifest.generation
        )
        with self.assertRaises(GraphOperationError) as conflict:
            await rebuild("force-true", False)
        self.assertEqual("IDEMPOTENCY_CONFLICT", conflict.exception.code)
        with self.assertRaises(ValidationError):
            await rebuild("force-invalid", "definitely-not-a-boolean")

    async def test_receipt_failure_rolls_back_activation_and_receipt_atomically(
        self,
    ) -> None:
        from unittest.mock import patch

        from daem0nmcp.api.v7 import graph_operations
        from daem0nmcp.api.v7.graph_operations import GraphOperationError

        operations = self._operations()
        request = _request(
            "entity_backfill",
            workspace_id=self.workspace.workspace_id,
            force=True,
            idempotency_key="receipt-rollback",
            preflight_token="0123456789abcdef",
        )
        real_insert = graph_operations._insert_receipt

        def insert_then_fail(*args, **kwargs):
            real_insert(*args, **kwargs)
            raise RuntimeError("injected failure after receipt insertion")

        with (
            patch.object(graph_operations, "_insert_receipt", insert_then_fail),
            self.assertRaises(GraphOperationError),
        ):
            await operations["entity_backfill"](
                workspace=self.workspace, request=request
            )
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT count(*) FROM projection_manifests WHERE status='active'"
                ).fetchone()[0],
            )
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT count(*) FROM background_jobs "
                    "WHERE job_type='graph.entity_backfill'"
                ).fetchone()[0],
            )

        recovered = await operations["entity_backfill"](
            workspace=self.workspace, request=request
        )
        self.assertEqual(1, recovered.manifest.generation)

    async def test_authorization_and_enabled_tools_return_models(self) -> None:
        from daem0nmcp.api.v7.discovery_operations import DiscoveryOperationError
        from daem0nmcp.api.v7.tools import CommunityRebuildData, EntityBackfillData
        from daem0nmcp.event_store import EventCommand, EventStore

        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            rows = (("a", "AuthService.login()"), ("b", "AuthService.login()"))
            for suffix, content in rows:
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
                                "source_client": "graph-test",
                                "source_model": None,
                                "deleted_at_us": None,
                            }
                        },
                    )
                )
            connection.commit()
        operations = self._operations()
        wrong = _request(
            "entity_backfill",
            workspace_id="ws_0123456789abcdef01234567",
            force=False,
            idempotency_key="wrong-workspace",
            preflight_token="0123456789abcdef",
        )
        with self.assertRaises(DiscoveryOperationError):
            await operations["entity_backfill"](workspace=self.workspace, request=wrong)
        backfill = await operations["entity_backfill"](
            workspace=self.workspace,
            request=_request(
                "entity_backfill",
                workspace_id=self.workspace.workspace_id,
                force=False,
                idempotency_key="entity-backfill",
                preflight_token="0123456789abcdef",
            ),
        )
        retried = await operations["entity_backfill"](
            workspace=self.workspace,
            request=_request(
                "entity_backfill",
                workspace_id=self.workspace.workspace_id,
                force=False,
                idempotency_key="entity-backfill",
                preflight_token="0123456789abcdef",
            ),
        )
        rebuild = await operations["community_rebuild"](
            workspace=self.workspace,
            request=_request(
                "community_rebuild",
                workspace_id=self.workspace.workspace_id,
                min_community_size=2,
                resolution=1.5,
                idempotency_key="community-rebuild",
                preflight_token="0123456789abcdef",
            ),
        )
        self.assertIsInstance(backfill, EntityBackfillData)
        self.assertEqual(backfill.manifest.generation, retried.manifest.generation)
        self.assertIsInstance(rebuild, CommunityRebuildData)
        self.assertEqual(backfill.manifest.generation, rebuild.manifest.generation)


async def _initialized_process_workspace(tmp_path):
    from daem0nmcp.database import DatabaseManager
    from daem0nmcp.workspace import WorkspaceRegistry

    storage = tmp_path / ".daem0nmcp" / "storage"
    storage.mkdir(parents=True)
    manager = DatabaseManager(str(storage))
    try:
        await manager.init_db()
    finally:
        await manager.close()
    return WorkspaceRegistry([tmp_path], default_root=tmp_path).default


async def _preflight(session, workspace_id: str, tool: str, arguments: dict):
    from tests.api_v7.process_client import succeed

    result = await succeed(
        session,
        "memory_preflight",
        {
            "workspace_id": workspace_id,
            "target_tool": tool,
            "target_arguments": arguments,
        },
    )
    return result["preflight_token"]


@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
async def test_actual_mcp_graph_profile_enabled_and_disabled(tmp_path, transport):
    from tests.api_v7.process_client import call, process_client, succeed

    workspace = await _initialized_process_workspace(tmp_path)
    scope = {"workspace_id": workspace.workspace_id}
    disabled_arguments = {
        "force": False,
        "idempotency_key": "process-graph-disabled",
    }
    # The graph extra is installed in CI, so it is on unless explicitly disabled.
    async with process_client(
        workspace.root,
        transport,
        environment_overrides={"DAEM0NMCP_GRAPH_ENABLED": "false"},
    ) as session:
        await succeed(session, "session_brief", scope)
        token = await _preflight(
            session, workspace.workspace_id, "entity_backfill", disabled_arguments
        )
        disabled = await call(
            session,
            "entity_backfill",
            {**scope, **disabled_arguments, "preflight_token": token},
        )
        assert not disabled["ok"]
        assert disabled["error"]["code"] == "CAPABILITY_DEGRADED"

    environment = {"DAEM0NMCP_GRAPH_ENABLED": "true"}
    async with process_client(
        workspace.root,
        transport,
        environment_overrides=environment,
    ) as session:
        await succeed(session, "session_brief", scope)
        store_arguments = {
            "record_type": "decision",
            "content": "Use AuthService.login() in auth.py",
            "idempotency_key": "process-graph-store",
        }
        token = await _preflight(
            session, workspace.workspace_id, "memory_store", store_arguments
        )
        await succeed(
            session,
            "memory_store",
            {**scope, **store_arguments, "preflight_token": token},
        )
        omitted_arguments = {"idempotency_key": "process-graph-omitted"}
        token = await _preflight(
            session, workspace.workspace_id, "entity_backfill", omitted_arguments
        )
        omitted = await succeed(
            session,
            "entity_backfill",
            {**scope, **omitted_arguments, "preflight_token": token},
        )
        false_arguments = {
            "force": False,
            "idempotency_key": "process-graph-false",
        }
        token = await _preflight(
            session, workspace.workspace_id, "entity_backfill", false_arguments
        )
        explicit_false = await succeed(
            session,
            "entity_backfill",
            {**scope, **false_arguments, "preflight_token": token},
        )
        backfill_arguments = {
            "force": True,
            "idempotency_key": "process-graph-backfill",
        }
        token = await _preflight(
            session, workspace.workspace_id, "entity_backfill", backfill_arguments
        )
        backfill = await succeed(
            session,
            "entity_backfill",
            {**scope, **backfill_arguments, "preflight_token": token},
        )
        token = await _preflight(
            session, workspace.workspace_id, "entity_backfill", backfill_arguments
        )
        replayed = await succeed(
            session,
            "entity_backfill",
            {**scope, **backfill_arguments, "preflight_token": token},
        )
        conflicting_arguments = {
            "force": False,
            "idempotency_key": "process-graph-backfill",
        }
        token = await _preflight(
            session,
            workspace.workspace_id,
            "entity_backfill",
            conflicting_arguments,
        )
        conflict = await call(
            session,
            "entity_backfill",
            {**scope, **conflicting_arguments, "preflight_token": token},
        )
        invalid = await session.call_tool(
            "entity_backfill",
            arguments={
                **scope,
                "force": "definitely-not-a-boolean",
                "idempotency_key": "process-graph-invalid",
                "preflight_token": "0123456789abcdef",
            },
        )
        assert (
            omitted["manifest"]["generation"]
            == explicit_false["manifest"]["generation"]
        )
        assert (
            backfill["manifest"]["generation"] == omitted["manifest"]["generation"] + 1
        )
        assert replayed["manifest"]["generation"] == backfill["manifest"]["generation"]
        assert not conflict["ok"]
        assert conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        assert invalid.isError
        assert backfill["scanned"] == 1
        assert backfill["extracted"] >= 1
        community_arguments = {
            "min_community_size": 2,
            "resolution": 1.75,
            "idempotency_key": "process-graph-community",
        }
        token = await _preflight(
            session,
            workspace.workspace_id,
            "community_rebuild",
            community_arguments,
        )
        rebuilt = await succeed(
            session,
            "community_rebuild",
            {**scope, **community_arguments, "preflight_token": token},
        )
        assert rebuilt["manifest"]["projection"] == "graph"
        assert rebuilt["modularity"] == 0.0


if __name__ == "__main__":
    unittest.main()
