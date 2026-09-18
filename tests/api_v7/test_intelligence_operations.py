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
from types import MappingProxyType, SimpleNamespace

from daem0nmcp.api.v7.application import AdmittedRequest

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
NOW_US = 1_786_276_800_000_000
PREFLIGHT_TOKEN = "preflight-token-0001"


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


class IntelligenceOperationTests(unittest.IsolatedAsyncioTestCase):
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
        from daem0nmcp.api.v7.intelligence_operations import (
            IntelligenceOperationDependencies,
            build_intelligence_operations,
        )

        options: dict[str, object] = {"clock": lambda: NOW}
        options.update(changes)
        self.dependencies = IntelligenceOperationDependencies(**options)
        return build_intelligence_operations(self.dependencies)

    @staticmethod
    def _record_state(
        content: str,
        *,
        record_type: str = "decision",
        worked: bool | None = None,
        archived: bool = False,
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
            "archived": archived,
            "outcome": None,
            "worked": worked,
            "recall_count": 0,
            "surprise_score": None,
            "importance_score": None,
            "source_client": "intelligence-test",
            "source_model": None,
            "deleted_at_us": deleted_at_us,
        }

    def _append_record(
        self,
        suffix: str,
        content: str,
        *,
        record_type: str = "decision",
        worked: bool | None = None,
        recorded_at_us: int = NOW_US - 100,
        occurred_at_us: int | None = None,
        expected_stream_version: int = 1,
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
                    event_type=(
                        "memory.created"
                        if expected_stream_version == 1
                        else "memory.updated"
                    ),
                    occurred_at_us=(
                        recorded_at_us if occurred_at_us is None else occurred_at_us
                    ),
                    recorded_at_us=recorded_at_us,
                    actor_type="system",
                    payload={
                        "record": self._record_state(
                            content,
                            record_type=record_type,
                            worked=worked,
                        )
                    },
                    expected_stream_version=expected_stream_version,
                )
            )
            connection.commit()
        return record_id, event.event_id

    def _append_rule(
        self,
        source_id: int,
        trigger: str,
        *,
        priority: int = 10,
        enabled: bool = True,
    ) -> tuple[str, str]:
        from daem0nmcp.api.v7.public_ids import PublicObjectIdRepository
        from daem0nmcp.event_store import (
            GovernanceEventCommand,
            GovernanceEventStore,
        )

        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            rule_id = PublicObjectIdRepository(
                connection, clock_us=lambda: NOW_US - 500
            ).get_or_create(
                self.workspace.workspace_id,
                "rule",
                source_id,
            )
            event = GovernanceEventStore(connection).append_and_project(
                GovernanceEventCommand(
                    workspace_id=self.workspace.workspace_id,
                    stream_id=rule_id,
                    stream_kind="rule",
                    event_type="rule.created",
                    occurred_at_us=NOW_US - 500,
                    recorded_at_us=NOW_US - 500,
                    actor_type="system",
                    payload={
                        "rule_id": rule_id,
                        "trigger": trigger,
                        "must_do": ["Run focused tests"],
                        "must_not": ["Skip evidence"],
                        "ask_first": [],
                        "warnings": ["Review failures"],
                        "priority": priority,
                        "enabled": enabled,
                        "created_at_us": NOW_US - 500,
                        "updated_at_us": NOW_US - 500,
                    },
                    expected_stream_version=1,
                )
            )
            connection.commit()
        return rule_id, event.event_id

    def _activate_communities(
        self,
        authentication_records: tuple[str, str],
        unrelated_record: str,
    ) -> None:
        from daem0nmcp.discovery_projection import (
            CommunityProjectionSeed,
            DiscoveryProjectionBuilder,
        )
        from daem0nmcp.retrieval.specialized_projection import (
            SpecializedProjectionBuilder,
        )

        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            SpecializedProjectionBuilder(
                connection, clock_us=lambda: NOW_US - 20
            ).rebuild(self.workspace.workspace_id, "graph")
            DiscoveryProjectionBuilder(
                connection, clock_us=lambda: NOW_US - 10
            ).populate_graph(
                self.workspace.workspace_id,
                entities=(),
                communities=(
                    CommunityProjectionSeed(
                        source_key="architecture",
                        label="Architecture",
                        level=1,
                        member_record_ids=(
                            *authentication_records,
                            unrelated_record,
                        ),
                    ),
                    CommunityProjectionSeed(
                        source_key="authentication",
                        label="Authentication",
                        level=0,
                        parent_source_key="architecture",
                        member_record_ids=authentication_records,
                    ),
                ),
            )
            connection.commit()

    async def test_registry_is_exact_immutable_and_keyword_only(self) -> None:
        """A mutable or positional registry could bypass admitted requests."""
        operations = self._operations()

        self.assertEqual(
            {
                "decision_debate",
                "decision_simulate",
                "memory_recall_hierarchical",
                "memory_verify",
                "rule_evolution_analyze",
            },
            set(operations),
        )
        with self.assertRaises(TypeError):
            operations["memory_verify"] = object()
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

    async def test_memory_verify_is_bounded_and_evidence_backed(self) -> None:
        """Fabricated verdicts or untraceable evidence would mislead callers."""
        supported_id, supported_event = self._append_record(
            "a", "The migration completed successfully."
        )
        contradicted_id, contradicted_event = self._append_record(
            "b", "The deployment must not skip backups."
        )
        operation = self._operations()["memory_verify"]

        result = await operation(
            workspace=self.workspace,
            request=_request(
                "memory_verify",
                workspace_id=self.workspace.workspace_id,
                text=(
                    "The migration completed successfully. "
                    "The deployment must skip backups. "
                    "Quantum mode is enabled."
                ),
            ),
        )

        self.assertEqual(
            ["supported", "contradicted", "unknown"],
            [claim.status for claim in result.claims],
        )
        self.assertEqual("mixed", result.overall_status)
        self.assertEqual(
            {(supported_id, supported_event), (contradicted_id, contradicted_event)},
            {(ref.record_id, ref.event_id) for ref in result.evidence_refs},
        )
        self.assertEqual(1, len(result.contradictions))
        self.assertEqual(
            "The deployment must skip backups.",
            result.contradictions[0].claim,
        )
        self.assertEqual(
            {contradicted_id},
            {ref.record_id for ref in result.contradictions[0].evidence_refs},
        )

    async def test_memory_verify_honors_category_and_transaction_time(self) -> None:
        """Ignoring filters or future events would corrupt point-in-time truth."""
        self._append_record(
            "c",
            "The feature flag is enabled.",
            record_type="warning",
            recorded_at_us=NOW_US - 100,
        )
        operation = self._operations()["memory_verify"]

        category_filtered = await operation(
            workspace=self.workspace,
            request=_request(
                "memory_verify",
                workspace_id=self.workspace.workspace_id,
                text="The feature flag is enabled.",
                categories=["decision"],
            ),
        )
        historical = await operation(
            workspace=self.workspace,
            request=_request(
                "memory_verify",
                workspace_id=self.workspace.workspace_id,
                text="The feature flag is enabled.",
                categories=["warning"],
                as_of_transaction_time="2026-08-09T11:59:59.999850Z",
            ),
        )

        self.assertEqual("unknown", category_filtered.overall_status)
        self.assertEqual("unknown", historical.overall_status)

    async def test_decision_simulate_compares_inscription_with_current_evidence(
        self,
    ) -> None:
        """Using only current rows would erase the decision's historical context."""
        historical_id, historical_event = self._append_record(
            "d",
            "Signed session cookies preserve authentication state.",
            record_type="pattern",
            recorded_at_us=NOW_US - 400,
        )
        decision_id, decision_event = self._append_record(
            "e",
            "Use signed session cookies for authentication.",
            recorded_at_us=NOW_US - 300,
        )
        current_id, current_event = self._append_record(
            "f",
            "Session cookies require authentication key rotation.",
            record_type="warning",
            recorded_at_us=NOW_US - 100,
        )
        self._append_record(
            "9",
            "The build cache is isolated.",
            record_type="observation",
            recorded_at_us=NOW_US - 50,
        )

        result = await self._operations()["decision_simulate"](
            workspace=self.workspace,
            request=_request(
                "decision_simulate",
                workspace_id=self.workspace.workspace_id,
                record_id=decision_id,
            ),
        )

        self.assertEqual(decision_id, result.decision.record_id)
        self.assertEqual(
            [historical_id],
            [record.record_id for record in result.then_context],
        )
        self.assertEqual(
            [historical_id, current_id],
            [record.record_id for record in result.current_context],
        )
        self.assertEqual(
            [f"New evidence after comparison point: {current_id}."],
            result.differences,
        )
        self.assertEqual(
            {
                (decision_id, decision_event),
                (historical_id, historical_event),
                (current_id, current_event),
            },
            {(ref.record_id, ref.event_id) for ref in result.evidence_refs},
        )

    async def test_rule_evolution_reports_canonical_revisions_and_outcomes(
        self,
    ) -> None:
        """A rule report must be grounded in governance and memory authority."""
        rule_id, _rule_event = self._append_rule(1, "database migration deployment")
        worked_id, worked_event = self._append_record(
            "1",
            "The database migration deployment completed after backup.",
            worked=True,
        )
        failed_id, failed_event = self._append_record(
            "2",
            "The database migration deployment failed before backup.",
            worked=False,
        )

        result = await self._operations()["rule_evolution_analyze"](
            workspace=self.workspace,
            request=_request(
                "rule_evolution_analyze",
                workspace_id=self.workspace.workspace_id,
                rule_id=rule_id,
            ),
        )

        self.assertEqual(1, result.analyzed)
        self.assertEqual([rule_id], [report.rule.rule_id for report in result.reports])
        self.assertIn("1 worked", result.reports[0].summary)
        self.assertIn("1 failed", result.reports[0].summary)
        self.assertIn("1 canonical revision", result.reports[0].summary)
        self.assertEqual(
            {(worked_id, worked_event), (failed_id, failed_event)},
            {(ref.record_id, ref.event_id) for ref in result.reports[0].evidence_refs},
        )
        self.assertEqual(
            result.reports[0].evidence_refs,
            result.evidence_refs,
        )
        with closing(sqlite3.connect(self.database)) as connection:
            event_count = connection.execute(
                "SELECT count(*) FROM governance_events WHERE workspace_id=? "
                "AND stream_id=?",
                (self.workspace.workspace_id, rule_id),
            ).fetchone()[0]
        self.assertEqual(1, event_count)

    async def test_hierarchical_recall_uses_active_generation_and_members(
        self,
    ) -> None:
        """Mixing generations or inventing community members breaks provenance."""
        first_id, first_event = self._append_record(
            "3", "Authentication uses signed session cookies."
        )
        second_id, second_event = self._append_record(
            "4", "Authentication key rotation is mandatory.", record_type="warning"
        )
        unrelated_id, _ = self._append_record(
            "5", "Build artifacts are reproducible.", record_type="pattern"
        )
        self._activate_communities((first_id, second_id), unrelated_id)

        result = await self._operations()["memory_recall_hierarchical"](
            workspace=self.workspace,
            request=_request(
                "memory_recall_hierarchical",
                workspace_id=self.workspace.workspace_id,
                query="authentication",
                include_members=True,
                limit=10,
            ),
        )

        self.assertEqual(
            ["Authentication"],
            [item.label for item in result.communities],
        )
        self.assertEqual([0], [layer.level for layer in result.layers])
        self.assertEqual(
            [first_id, second_id],
            [record.record_id for record in result.layers[0].records],
        )
        generation = result.communities[0].manifest_generation
        self.assertGreaterEqual(generation, 1)
        self.assertEqual(
            {(first_id, first_event), (second_id, second_event)},
            {(ref.record_id, ref.event_id) for ref in result.evidence_refs},
        )

    async def test_hierarchical_recall_rejects_tampered_partition_payload(
        self,
    ) -> None:
        first_id, _ = self._append_record("8", "Authentication uses OAuth.")
        second_id, _ = self._append_record("9", "Authentication verifies JWTs.")
        unrelated_id, _ = self._append_record("0", "Unrelated deployment note.")
        self._activate_communities((first_id, second_id), unrelated_id)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("DROP TRIGGER discovery_communities_no_update")
            connection.execute(
                "UPDATE discovery_communities SET label='Tampered label' "
                "WHERE workspace_id=? AND label='Authentication'",
                (self.workspace.workspace_id,),
            )
            connection.commit()

        operation = self._operations()["memory_recall_hierarchical"]
        with self.assertRaisesRegex(Exception, "CAPABILITY_DEGRADED"):
            await operation(
                workspace=self.workspace,
                request=_request(
                    "memory_recall_hierarchical",
                    workspace_id=self.workspace.workspace_id,
                    query="tampered label",
                ),
            )

    async def test_decision_debate_is_deterministic_canonical_and_idempotent(
        self,
    ) -> None:
        """A retry must not append or synthesize a different consensus."""
        advocate_id, advocate_event = self._append_record(
            "6",
            "Postgres durable transactions support relational storage.",
            worked=True,
        )
        challenger_id, challenger_event = self._append_record(
            "7",
            "SQLite local storage avoids a network dependency.",
            worked=True,
        )
        operation = self._operations()["decision_debate"]
        request = _request(
            "decision_debate",
            workspace_id=self.workspace.workspace_id,
            topic="Storage",
            advocate_position="Use Postgres durable relational storage",
            challenger_position="Use SQLite local storage",
            max_rounds=3,
            idempotency_key="debate-storage-0001",
            preflight_token=PREFLIGHT_TOKEN,
        )

        first = await operation(workspace=self.workspace, request=request)
        second = await operation(workspace=self.workspace, request=request)

        self.assertEqual(first, second)
        self.assertEqual(
            [1, 2, 3],
            [item.round_number for item in first.rounds],
        )
        self.assertIn("advocate", first.synthesis.casefold())
        self.assertEqual(1, len(first.event_ids))
        self.assertEqual(
            {(advocate_id, advocate_event), (challenger_id, challenger_event)},
            {(ref.record_id, ref.event_id) for ref in first.evidence_refs},
        )
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            events = connection.execute(
                "SELECT event_id,payload_json FROM memory_events "
                "WHERE workspace_id=? AND stream_id=?",
                (self.workspace.workspace_id, first.consensus_record_id),
            ).fetchall()
            record = connection.execute(
                "SELECT record_type,content,source_event_id FROM memory_records "
                "WHERE workspace_id=? AND record_id=?",
                (self.workspace.workspace_id, first.consensus_record_id),
            ).fetchone()
        self.assertEqual(1, len(events))
        self.assertEqual(first.event_ids[0], events[0]["event_id"])
        self.assertEqual("decision", record["record_type"])
        self.assertEqual(first.synthesis, record["content"])
        self.assertEqual(first.event_ids[0], record["source_event_id"])

    async def test_read_worker_drain_survives_repeated_cancellation(self) -> None:
        """A second cancellation must not detach a live snapshot reader."""
        resolver = _BlockingStorageResolver(self.database)
        operation = self._operations(storage_resolver=resolver)["memory_verify"]
        task = asyncio.create_task(
            operation(
                workspace=self.workspace,
                request=_request(
                    "memory_verify",
                    workspace_id=self.workspace.workspace_id,
                    text="No detached readers.",
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

    async def test_mutation_drain_survives_repeated_cancellation(self) -> None:
        """A detached cancelled debate could commit after cancellation is reported."""
        resolver = _BlockingStorageResolver(self.database)
        operation = self._operations(storage_resolver=resolver)["decision_debate"]
        task = asyncio.create_task(
            operation(
                workspace=self.workspace,
                request=_request(
                    "decision_debate",
                    workspace_id=self.workspace.workspace_id,
                    topic="Cancellation",
                    advocate_position="Wait for the worker",
                    challenger_position="Detach the worker",
                    idempotency_key="debate-cancellation-0001",
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
            count = connection.execute(
                "SELECT count(*) FROM memory_events WHERE workspace_id=? "
                "AND correlation_id LIKE 'job_%' "
                "AND event_type='memory.created'",
                (self.workspace.workspace_id,),
            ).fetchone()[0]
        self.assertEqual(0, count)


if __name__ == "__main__":
    unittest.main()
