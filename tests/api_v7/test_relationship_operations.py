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
from types import MappingProxyType

from daem0nmcp.api.v7.application import AdmittedRequest

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
NOW_US = 1_786_276_800_000_000
TOKEN = "preflight-token-0001"


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


class RelationshipOperationTests(unittest.IsolatedAsyncioTestCase):
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
        from daem0nmcp.api.v7.relationship_operations import (
            RelationshipOperationDependencies,
            build_relationship_operations,
        )

        options: dict[str, object] = {"clock": lambda: NOW}
        options.update(changes)
        self.dependencies = RelationshipOperationDependencies(**options)
        return build_relationship_operations(self.dependencies)

    def _append_record(
        self,
        suffix: str,
        content: str,
        *,
        record_type: str = "decision",
        tags: list[str] | None = None,
    ) -> str:
        from daem0nmcp.event_store import EventCommand, EventStore

        record_id = "mem_" + suffix * 64
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            EventStore(connection).append_and_project(
                EventCommand(
                    workspace_id=self.workspace.workspace_id,
                    stream_id=record_id,
                    stream_kind="memory",
                    event_type="memory.created",
                    occurred_at_us=NOW_US - 20,
                    recorded_at_us=NOW_US - 10,
                    actor_type="system",
                    payload={
                        "record": {
                            "record_type": record_type,
                            "legacy_type": None,
                            "content": content,
                            "rationale": None,
                            "context": {},
                            "tags": [] if tags is None else tags,
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
                            "source_client": "relationship-test",
                            "source_model": None,
                            "deleted_at_us": None,
                        }
                    },
                )
            )
            connection.commit()
        return record_id

    async def _link(
        self,
        operations: object,
        source: str,
        target: str,
        relationship_type: str,
        key: str,
        *,
        description: str | None = None,
    ) -> str:
        receipt = await operations["memory_link"](
            workspace=self.workspace,
            request=_request(
                "memory_link",
                workspace_id=self.workspace.workspace_id,
                source_record_id=source,
                target_record_id=target,
                relationship_type=relationship_type,
                description=description,
                idempotency_key=key,
                preflight_token=TOKEN,
            ),
        )
        return next(item for item in receipt.affected_ids if item.startswith("rel_"))

    def _activate_graph(self) -> None:
        from daem0nmcp.retrieval.specialized_projection import (
            SpecializedProjectionBuilder,
        )

        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            SpecializedProjectionBuilder(
                connection, clock_us=lambda: NOW_US + 100
            ).rebuild(self.workspace.workspace_id, "graph")
            connection.commit()

    async def test_registry_is_exact_immutable_and_keyword_only(self) -> None:
        """A mutable or positional handler registry could bypass admission."""
        operations = self._operations()

        self.assertEqual(
            {
                "knowledge_graph_get",
                "knowledge_graph_render",
                "memory_chain_trace",
                "memory_link",
                "memory_related",
                "memory_unlink",
            },
            set(operations),
        )
        with self.assertRaises(TypeError):
            operations["memory_link"] = object()
        for operation in operations.values():
            parameters = tuple(inspect.signature(operation).parameters.values())
            self.assertEqual(
                ("workspace", "request"), tuple(item.name for item in parameters)
            )
            self.assertTrue(
                all(item.kind is inspect.Parameter.KEYWORD_ONLY for item in parameters)
            )

    async def test_memory_link_appends_one_canonical_idempotent_event(self) -> None:
        """Direct projection writes or unstable retries would corrupt authority."""
        from daem0nmcp.api.v7.models import MutationReceipt

        source = self._append_record("a", "Choose the durable queue.")
        target = self._append_record("b", "Implement the worker.")
        request = _request(
            "memory_link",
            workspace_id=self.workspace.workspace_id,
            source_record_id=source,
            target_record_id=target,
            relationship_type="led_to",
            description="Decision led to implementation.",
            confidence=0.75,
            idempotency_key="link-decision-worker-0001",
            preflight_token=TOKEN,
        )
        operation = self._operations()["memory_link"]

        first = await operation(workspace=self.workspace, request=request)
        second = await operation(workspace=self.workspace, request=request)

        self.assertIsInstance(first, MutationReceipt)
        self.assertFalse(first.idempotent_replay)
        self.assertTrue(second.idempotent_replay)
        self.assertEqual(first.operation_id, second.operation_id)
        self.assertEqual(first.affected_ids, second.affected_ids)
        self.assertEqual(first.event_ids, second.event_ids)
        relationship_id = next(
            item for item in first.affected_ids if item.startswith("rel_")
        )
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            events = connection.execute(
                "SELECT event_type,payload_json,correlation_id FROM memory_events "
                "WHERE workspace_id=? AND stream_id=? ORDER BY stream_version",
                (self.workspace.workspace_id, relationship_id),
            ).fetchall()
            projection = connection.execute(
                "SELECT source_record_id,target_record_id,relationship_type,"
                "description,confidence,valid_to_us,asserted_by_event_id "
                "FROM memory_relationship_versions WHERE workspace_id=? "
                "AND relationship_id=?",
                (self.workspace.workspace_id, relationship_id),
            ).fetchone()
        self.assertEqual(1, len(events))
        self.assertEqual("relationship.created", events[0]["event_type"])
        payload = json.loads(events[0]["payload_json"])
        self.assertEqual(
            {
                "source_record_id": source,
                "target_record_id": target,
                "relationship_type": "led_to",
                "legacy_type": None,
                "description": "Decision led to implementation.",
                "confidence": 0.75,
                "metadata": {},
                "valid_from_us": NOW_US,
                "valid_to_us": None,
            },
            payload["relationship"],
        )
        self.assertEqual(
            (
                source,
                target,
                "led_to",
                "Decision led to implementation.",
                0.75,
                None,
                first.event_ids[0],
            ),
            tuple(projection),
        )
        self.assertNotIn(str(self.root), first.model_dump_json())

    async def test_memory_link_rejects_key_reuse_and_duplicate_live_edge(self) -> None:
        """A reused key or duplicate typed edge must not append ambiguous state."""
        from daem0nmcp.api.v7.relationship_operations import (
            RelationshipOperationError,
        )

        source = self._append_record("a", "Source.")
        target = self._append_record("b", "Target.")
        operation = self._operations()["memory_link"]
        base = {
            "workspace_id": self.workspace.workspace_id,
            "source_record_id": source,
            "target_record_id": target,
            "relationship_type": "depends_on",
            "idempotency_key": "link-same-key-0001",
            "preflight_token": TOKEN,
        }
        await operation(
            workspace=self.workspace,
            request=_request("memory_link", **base),
        )

        with self.assertRaises(RelationshipOperationError) as reused:
            await operation(
                workspace=self.workspace,
                request=_request(
                    "memory_link",
                    **{**base, "relationship_type": "related_to"},
                ),
            )
        self.assertEqual("IDEMPOTENCY_CONFLICT", reused.exception.code)
        with self.assertRaises(RelationshipOperationError) as duplicate:
            await operation(
                workspace=self.workspace,
                request=_request(
                    "memory_link",
                    **{**base, "idempotency_key": "link-other-key-0002"},
                ),
            )
        self.assertEqual("CONFLICT", duplicate.exception.code)
        with closing(sqlite3.connect(self.database)) as connection:
            count = connection.execute(
                "SELECT count(*) FROM memory_events "
                "WHERE workspace_id=? AND stream_kind='relationship'",
                (self.workspace.workspace_id,),
            ).fetchone()[0]
        self.assertEqual(1, count)

    async def test_memory_link_rejects_missing_or_deleted_endpoints(self) -> None:
        """Foreign or invalidated endpoint IDs must never enter the graph."""
        from daem0nmcp.api.v7.relationship_operations import (
            RelationshipOperationError,
        )

        source = self._append_record("a", "Source.")
        missing = "mem_" + "f" * 64
        operation = self._operations()["memory_link"]
        with self.assertRaises(RelationshipOperationError) as not_found:
            await operation(
                workspace=self.workspace,
                request=_request(
                    "memory_link",
                    workspace_id=self.workspace.workspace_id,
                    source_record_id=source,
                    target_record_id=missing,
                    relationship_type="related_to",
                    idempotency_key="link-missing-target-0001",
                    preflight_token=TOKEN,
                ),
            )
        self.assertEqual("NOT_FOUND", not_found.exception.code)

    async def test_memory_unlink_appends_one_tombstone_and_replays_it(self) -> None:
        """Unlink must preserve history and never mutate or delete prior versions."""
        source = self._append_record("a", "Source.")
        target = self._append_record("b", "Target.")
        operations = self._operations()
        linked = await operations["memory_link"](
            workspace=self.workspace,
            request=_request(
                "memory_link",
                workspace_id=self.workspace.workspace_id,
                source_record_id=source,
                target_record_id=target,
                relationship_type="invalidates",
                idempotency_key="link-before-unlink-0001",
                preflight_token=TOKEN,
            ),
        )
        relationship_id = next(
            item for item in linked.affected_ids if item.startswith("rel_")
        )
        request = _request(
            "memory_unlink",
            workspace_id=self.workspace.workspace_id,
            relationship_id=relationship_id,
            preflight_token=TOKEN,
        )

        first = await operations["memory_unlink"](
            workspace=self.workspace, request=request
        )
        second = await operations["memory_unlink"](
            workspace=self.workspace, request=request
        )

        self.assertFalse(first.idempotent_replay)
        self.assertTrue(second.idempotent_replay)
        self.assertEqual(first.event_ids, second.event_ids)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            events = connection.execute(
                "SELECT event_type,payload_json FROM memory_events "
                "WHERE workspace_id=? AND stream_id=? ORDER BY stream_version",
                (self.workspace.workspace_id, relationship_id),
            ).fetchall()
            versions = connection.execute(
                "SELECT version,valid_to_us,transaction_to_us,"
                "retracted_by_event_id FROM memory_relationship_versions "
                "WHERE workspace_id=? AND relationship_id=? ORDER BY version",
                (self.workspace.workspace_id, relationship_id),
            ).fetchall()
        self.assertEqual(
            ["relationship.created", "relationship.removed"],
            [row["event_type"] for row in events],
        )
        self.assertEqual(2, len(versions))
        self.assertEqual(first.event_ids[0], versions[0]["retracted_by_event_id"])
        self.assertIsNotNone(versions[0]["transaction_to_us"])
        self.assertEqual(NOW_US + 1, versions[1]["valid_to_us"])
        self.assertIsNone(versions[1]["transaction_to_us"])
        self.assertEqual(first.event_ids[0], versions[1]["retracted_by_event_id"])

    async def test_memory_related_walks_a_filtered_directional_snapshot(self) -> None:
        """Traversal must honor direction, type, depth, cycles, and stable order."""
        from daem0nmcp.api.v7.tools import MemoryRelatedData

        a = self._append_record("a", "Root decision.", tags=["root"])
        b = self._append_record("b", "Worker implementation.")
        c = self._append_record("c", "Incoming prerequisite.")
        d = self._append_record("d", "Downstream test.")
        operations = self._operations()
        ab = await self._link(operations, a, b, "led_to", "related-edge-ab-0001")
        ca = await self._link(operations, c, a, "depends_on", "related-edge-ca-0002")
        bd = await self._link(operations, b, d, "depends_on", "related-edge-bd-0003")
        await self._link(operations, d, a, "conflicts_with", "related-cycle-da-0004")
        request = _request(
            "memory_related",
            workspace_id=self.workspace.workspace_id,
            record_id=a,
            relationship_types={"led_to", "depends_on"},
            direction="both",
            max_depth=2,
        )

        first = await operations["memory_related"](
            workspace=self.workspace, request=request
        )
        second = await operations["memory_related"](
            workspace=self.workspace, request=request
        )

        self.assertIsInstance(first, MemoryRelatedData)
        self.assertEqual(a, first.root.record_id)
        self.assertEqual({b, c, d}, {item.record_id for item in first.records})
        self.assertEqual(
            {ab, ca, bd},
            {item.relationship_id for item in first.relationships},
        )
        by_target = {path.record_ids[-1]: path for path in first.paths}
        self.assertEqual([a, b], by_target[b].record_ids)
        self.assertEqual([a, c], by_target[c].record_ids)
        self.assertEqual([a, b, d], by_target[d].record_ids)
        self.assertEqual(first.model_dump_json(), second.model_dump_json())

    async def test_memory_chain_trace_returns_bounded_directed_simple_paths(
        self,
    ) -> None:
        """Chain tracing must not reverse edges or loop forever through cycles."""
        from daem0nmcp.api.v7.tools import MemoryChainTraceData

        a = self._append_record("a", "Start.")
        b = self._append_record("b", "Left branch.")
        c = self._append_record("c", "Right branch.")
        d = self._append_record("d", "End.")
        operations = self._operations()
        ad = await self._link(operations, a, d, "led_to", "chain-ad-0001")
        ab = await self._link(operations, a, b, "led_to", "chain-ab-0002")
        bd = await self._link(operations, b, d, "depends_on", "chain-bd-0003")
        ac = await self._link(operations, a, c, "related_to", "chain-ac-0004")
        cd = await self._link(operations, c, d, "led_to", "chain-cd-0005")
        await self._link(operations, d, a, "conflicts_with", "chain-cycle-da-0006")

        result = await operations["memory_chain_trace"](
            workspace=self.workspace,
            request=_request(
                "memory_chain_trace",
                workspace_id=self.workspace.workspace_id,
                start_record_id=a,
                end_record_id=d,
                max_depth=3,
            ),
        )

        self.assertIsInstance(result, MemoryChainTraceData)
        self.assertEqual(
            [[a, d], [a, b, d], [a, c, d]],
            [path.record_ids for path in result.paths],
        )
        self.assertEqual(
            [[ad], [ab, bd], [ac, cd]],
            [path.relationship_ids for path in result.paths],
        )
        self.assertEqual(
            {ad, ab, bd, ac, cd},
            {
                relation_id
                for evidence in result.evidence_refs
                for relation_id in evidence.relation_path
            },
        )

    async def test_knowledge_graph_get_uses_current_manifest_and_selectors(
        self,
    ) -> None:
        """Graph JSON must be a bounded active-generation snapshot, not v6 state."""
        from daem0nmcp.api.v7.tools import KnowledgeGraphData

        a = self._append_record("a", "Queue architecture.")
        b = self._append_record("b", "Worker implementation.")
        c = self._append_record("c", "Worker test plan.")
        orphan = self._append_record("d", "Unlinked note.")
        operations = self._operations()
        ab = await self._link(operations, a, b, "led_to", "graph-ab-0001")
        bc = await self._link(operations, b, c, "evidence_for", "graph-bc-0002")
        self._activate_graph()

        selected = await operations["knowledge_graph_get"](
            workspace=self.workspace,
            request=_request(
                "knowledge_graph_get",
                workspace_id=self.workspace.workspace_id,
                record_ids={a, b},
                max_nodes=10,
            ),
        )
        with_orphans = await operations["knowledge_graph_get"](
            workspace=self.workspace,
            request=_request(
                "knowledge_graph_get",
                workspace_id=self.workspace.workspace_id,
                include_orphans=True,
                max_nodes=10,
            ),
        )
        queried = await operations["knowledge_graph_get"](
            workspace=self.workspace,
            request=_request(
                "knowledge_graph_get",
                workspace_id=self.workspace.workspace_id,
                query="Worker test",
                max_nodes=10,
            ),
        )

        self.assertIsInstance(selected, KnowledgeGraphData)
        self.assertEqual([a, b], [node.record.record_id for node in selected.nodes])
        self.assertEqual(
            [ab],
            [edge.relationship.relationship_id for edge in selected.edges],
        )
        self.assertEqual("graph", selected.manifest.projection)
        self.assertEqual(1, selected.manifest.generation)
        self.assertEqual(
            {a, b, c, orphan},
            {node.record.record_id for node in with_orphans.nodes},
        )
        self.assertEqual([c], [node.record.record_id for node in queried.nodes])
        self.assertEqual([], queried.edges)
        self.assertNotIn(
            bc,
            [edge.relationship.relationship_id for edge in selected.edges],
        )

    async def test_knowledge_graph_get_refuses_a_stale_active_manifest(self) -> None:
        """Serving new rows with an old manifest would mix graph generations."""
        from daem0nmcp.api.v7.relationship_operations import (
            RelationshipOperationError,
        )

        a = self._append_record("a", "First.")
        b = self._append_record("b", "Second.")
        c = self._append_record("c", "Third.")
        operations = self._operations()
        await self._link(operations, a, b, "related_to", "stale-ab-0001")
        self._activate_graph()
        current = await operations["knowledge_graph_get"](
            workspace=self.workspace,
            request=_request(
                "knowledge_graph_get",
                workspace_id=self.workspace.workspace_id,
            ),
        )
        self.assertEqual({a, b}, {node.record.record_id for node in current.nodes})
        await self._link(operations, b, c, "related_to", "stale-bc-0002")

        with self.assertRaises(RelationshipOperationError) as raised:
            await operations["knowledge_graph_get"](
                workspace=self.workspace,
                request=_request(
                    "knowledge_graph_get",
                    workspace_id=self.workspace.workspace_id,
                ),
            )
        self.assertEqual("CAPABILITY_DEGRADED", raised.exception.code)

    async def test_knowledge_graph_render_escapes_mermaid_labels(self) -> None:
        """Stored text must never break out of a Mermaid node label."""
        from daem0nmcp.api.v7.tools import KnowledgeGraphRenderData

        a = self._append_record("a", 'Unsafe "]\nclick evil callback')
        b = self._append_record("b", "Safe target.")
        operations = self._operations()
        await self._link(
            operations,
            a,
            b,
            "related_to",
            "render-ab-0001",
            description='Edge "]\nclick attack callback',
        )
        self._activate_graph()

        result = await operations["knowledge_graph_render"](
            workspace=self.workspace,
            request=_request(
                "knowledge_graph_render",
                workspace_id=self.workspace.workspace_id,
                format="mermaid",
            ),
        )

        self.assertIsInstance(result, KnowledgeGraphRenderData)
        self.assertEqual("mermaid", result.format)
        lines = result.text.splitlines()
        self.assertEqual("flowchart TD", lines[0])
        self.assertEqual(4, len(lines))
        self.assertIn("&#x22;&#x5D;", result.text)
        self.assertNotIn('Unsafe "]', result.text)
        self.assertNotIn("\nclick ", result.text)

    async def test_graph_record_selector_is_capped_without_false_not_found(
        self,
    ) -> None:
        """max_nodes must bound output without misclassifying valid selected IDs."""
        record_ids = {
            self._append_record(character, f"Record {character}.")
            for character in ("a", "b", "c", "d")
        }
        self._activate_graph()
        operations = self._operations()

        result = await operations["knowledge_graph_get"](
            workspace=self.workspace,
            request=_request(
                "knowledge_graph_get",
                workspace_id=self.workspace.workspace_id,
                record_ids=record_ids,
                max_nodes=2,
            ),
        )

        self.assertEqual(
            sorted(record_ids)[:2],
            [node.record.record_id for node in result.nodes],
        )

    async def test_cancelled_link_rolls_back_before_canonical_append(self) -> None:
        """Cancellation observed before append must leave no event or projection."""
        from daem0nmcp.api.v7.runtime_services import WorkspaceStorageResolver

        source = self._append_record("a", "Source.")
        target = self._append_record("b", "Target.")
        entered = threading.Event()
        release = threading.Event()
        delegate = WorkspaceStorageResolver()

        class BlockingResolver:
            @contextmanager
            def locked_active(self, workspace):
                entered.set()
                if not release.wait(timeout=5):
                    raise RuntimeError("test resolver timed out")
                with delegate.locked_active(workspace) as active:
                    yield active

        operation = self._operations(storage_resolver=BlockingResolver())["memory_link"]
        task = asyncio.create_task(
            operation(
                workspace=self.workspace,
                request=_request(
                    "memory_link",
                    workspace_id=self.workspace.workspace_id,
                    source_record_id=source,
                    target_record_id=target,
                    relationship_type="related_to",
                    idempotency_key="cancelled-link-0001",
                    preflight_token=TOKEN,
                ),
            )
        )
        self.assertTrue(await asyncio.to_thread(entered.wait, 2))

        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task

        with closing(sqlite3.connect(self.database)) as connection:
            event_count = connection.execute(
                "SELECT count(*) FROM memory_events "
                "WHERE workspace_id=? AND stream_kind='relationship'",
                (self.workspace.workspace_id,),
            ).fetchone()[0]
            projection_count = connection.execute(
                "SELECT count(*) FROM memory_relationship_versions "
                "WHERE workspace_id=?",
                (self.workspace.workspace_id,),
            ).fetchone()[0]
        self.assertEqual((0, 0), (event_count, projection_count))

    async def test_related_refuses_projection_state_that_differs_from_event(
        self,
    ) -> None:
        """A mutable projection row must never outrank its append-only event."""
        from daem0nmcp.api.v7.relationship_operations import (
            RelationshipOperationError,
        )

        source = self._append_record("a", "Source.")
        target = self._append_record("b", "Target.")
        operations = self._operations()
        relationship_id = await self._link(
            operations,
            source,
            target,
            "related_to",
            "tamper-related-edge-0001",
            description="Canonical description.",
        )
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE memory_relationship_versions SET description=? "
                "WHERE relationship_id=?",
                ("Tampered description.", relationship_id),
            )
            connection.commit()

        with self.assertRaises(RelationshipOperationError) as raised:
            await operations["memory_related"](
                workspace=self.workspace,
                request=_request(
                    "memory_related",
                    workspace_id=self.workspace.workspace_id,
                    record_id=source,
                ),
            )
        self.assertEqual("CAPABILITY_DEGRADED", raised.exception.code)

    async def test_late_cancelled_link_returns_the_committed_receipt(self) -> None:
        """A cancellation after commit must not hide a durable relationship."""
        from daem0nmcp.api.v7.runtime_services import WorkspaceStorageResolver

        source = self._append_record("a", "Source.")
        target = self._append_record("b", "Target.")
        committed = threading.Event()
        release = threading.Event()
        delegate = WorkspaceStorageResolver()

        class ExitBlockingResolver:
            @contextmanager
            def locked_active(self, workspace):
                with delegate.locked_active(workspace) as active:
                    yield active
                    committed.set()
                    if not release.wait(timeout=5):
                        raise RuntimeError("test resolver timed out")

        operation = self._operations(storage_resolver=ExitBlockingResolver())[
            "memory_link"
        ]
        task = asyncio.create_task(
            operation(
                workspace=self.workspace,
                request=_request(
                    "memory_link",
                    workspace_id=self.workspace.workspace_id,
                    source_record_id=source,
                    target_record_id=target,
                    relationship_type="related_to",
                    idempotency_key="late-cancel-link-0001",
                    preflight_token=TOKEN,
                ),
            )
        )
        self.assertTrue(await asyncio.to_thread(committed.wait, 2))

        task.cancel()
        release.set()
        receipt = await task

        self.assertFalse(receipt.idempotent_replay)
        self.assertEqual(1, receipt.counts["changed"])
        with closing(sqlite3.connect(self.database)) as connection:
            event_count = connection.execute(
                "SELECT count(*) FROM memory_events "
                "WHERE workspace_id=? AND stream_kind='relationship'",
                (self.workspace.workspace_id,),
            ).fetchone()[0]
        self.assertEqual(1, event_count)


if __name__ == "__main__":
    unittest.main()
