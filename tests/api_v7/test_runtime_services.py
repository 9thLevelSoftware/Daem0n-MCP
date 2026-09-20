"""Dependency-free production-service contracts for the pinned v7 handlers."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

WORKED_AT = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def _apply_v7_schema(
    connection: sqlite3.Connection,
    *,
    through_version: int | None = None,
) -> None:
    from daem0nmcp.migrations.schema import MIGRATIONS
    from daem0nmcp.schema_version import CURRENT_SCHEMA_VERSION

    connection.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
    target = CURRENT_SCHEMA_VERSION if through_version is None else through_version
    for version in range(16, target + 1):
        migration = next(item for item in MIGRATIONS if item[0] == version)
        for statement in migration[2]:
            connection.execute(statement)
        connection.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))
    connection.commit()


class _RuntimeServiceFixtures:
    def setUp(self) -> None:
        from daem0nmcp.storage_activation import (
            ActiveDatabasePointer,
            write_active_pointer,
        )
        from daem0nmcp.workspace import WorkspaceRegistry

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.storage = self.root / ".daem0nmcp" / "storage"
        self.storage.mkdir(parents=True)
        self.database = self.storage / "daem0nmcp.db"
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            _apply_v7_schema(connection)
        finally:
            connection.close()
        write_active_pointer(
            self.storage,
            ActiveDatabasePointer(7, 1, "daem0nmcp.db", None, None),
        )
        self.workspace = WorkspaceRegistry([self.root], default_root=self.root).default
        self.writers: list[object] = []
        self.health_services: list[object] = []

    def tearDown(self) -> None:
        for writer in self.writers:
            writer.close()
        for service in self.health_services:
            service.close()
        self.temporary.cleanup()

    def _writer(self, **changes):
        from daem0nmcp.api.v7.runtime_services import SQLiteMemoryEventWriter

        options = {
            "clock": lambda: WORKED_AT,
            "projection_scheduler": lambda _path: None,
            "max_workers": 1,
        }
        options.update(changes)
        writer = SQLiteMemoryEventWriter(**options)
        self.writers.append(writer)
        return writer

    @staticmethod
    def _store_command(**changes):
        from daem0nmcp.api.v7.pinned import MemoryStoreCommand

        values = {
            "record_type": "decision",
            "content": "Use one canonical event stream.",
            "rationale": "Replay-safe writes are easier to operate.",
            "context": {"component": "runtime"},
            "tags": ("v7", "sqlite"),
            "relative_file_path": "daem0nmcp/runtime.py",
            "happened_at": WORKED_AT,
            "procedure_steps": (),
            "idempotency_key": "runtime-store-0001",
        }
        values.update(changes)
        return MemoryStoreCommand(**values)


class WriterServiceTests(
    _RuntimeServiceFixtures,
    unittest.IsolatedAsyncioTestCase,
):
    async def test_safe_resolver_requires_a_format_7_active_pointer(self) -> None:
        """Accepting pointerless format 6 storage would write the wrong authority."""
        from daem0nmcp.api.v7.runtime_services import (
            RuntimeServiceError,
            WorkspaceStorageResolver,
            resolve_workspace_storage,
        )
        from daem0nmcp.workspace import Workspace, WorkspaceRegistry

        self.assertEqual(self.storage, resolve_workspace_storage(self.workspace))
        resolver = WorkspaceStorageResolver()
        with resolver.locked_active(self.workspace) as active:
            self.assertEqual(self.database, active.path)
            self.assertEqual(7, active.format_version)

        other = self.root / "pointerless"
        other_storage = other / ".daem0nmcp" / "storage"
        other_storage.mkdir(parents=True)
        (other_storage / "daem0nmcp.db").touch()
        pointerless = WorkspaceRegistry([other], default_root=other).default
        with (
            self.assertRaisesRegex(RuntimeServiceError, "MIGRATION_REQUIRED"),
            resolver.locked_active(pointerless),
        ):
            self.fail("pointerless storage was admitted")
        with self.assertRaisesRegex(RuntimeServiceError, "INVALID_WORKSPACE"):
            resolve_workspace_storage(Workspace(self.workspace.workspace_id, other))

    async def test_store_commits_canonical_event_then_schedules_projection(
        self,
    ) -> None:
        """EventStore authority and post-commit scheduling must remain intact."""
        scheduler_observations: list[tuple[Path, int]] = []

        def schedule(path: Path) -> None:
            with closing(sqlite3.connect(path)) as connection:
                count = connection.execute(
                    "SELECT count(*) FROM memory_events"
                ).fetchone()[0]
            scheduler_observations.append((Path(path), int(count)))

        writer = self._writer(projection_scheduler=schedule)
        stored = await writer.store(self.workspace, self._store_command())

        self.assertFalse(stored.idempotent_replay)
        self.assertTrue(stored.record.record_id.startswith("mem_"))
        self.assertEqual("daem0nmcp/runtime.py", stored.record.relative_file_path)
        self.assertEqual([(self.database, 1)], scheduler_observations)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            event = connection.execute(
                "SELECT event_type,correlation_id,payload_json FROM memory_events"
            ).fetchone()
            record = connection.execute(
                "SELECT file_path,file_path_relative,source_event_id "
                "FROM memory_records"
            ).fetchone()
            legacy_vectors = connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' "
                "AND name='memories'"
            ).fetchone()[0]
        self.assertEqual("memory.created", event["event_type"])
        self.assertTrue(str(event["correlation_id"]).startswith("job_"))
        self.assertIn("idempotency_request_hash", json.loads(event["payload_json"]))
        self.assertIsNone(record["file_path"])
        self.assertEqual("daem0nmcp/runtime.py", record["file_path_relative"])
        self.assertEqual(stored.event.event_id, record["source_event_id"])
        self.assertEqual(0, legacy_vectors)

    async def test_store_replays_exact_request_and_rejects_key_rebinding(self) -> None:
        """Appending twice or accepting a changed payload under one key is a bug."""
        from daem0nmcp.api.v7.pinned import IdempotencyConflict

        writer = self._writer()
        first = await writer.store(self.workspace, self._store_command())
        replay = await writer.store(self.workspace, self._store_command())

        self.assertTrue(replay.idempotent_replay)
        self.assertEqual(first.event, replay.event)
        self.assertEqual(first.record.record_id, replay.record.record_id)
        with self.assertRaises(IdempotencyConflict):
            await writer.store(
                self.workspace,
                self._store_command(content="A changed request must conflict."),
            )
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                1,
                connection.execute("SELECT count(*) FROM memory_events").fetchone()[0],
            )

    async def test_store_projects_procedure_steps_in_canonical_context(self) -> None:
        """Keeping steps outside context would make Task 8's projection miss them."""
        writer = self._writer()
        await writer.store(
            self.workspace,
            self._store_command(
                record_type="procedure",
                context={"component": "release"},
                procedure_steps=("Run tests", "Publish artifacts"),
            ),
        )
        with closing(sqlite3.connect(self.database)) as connection:
            context_text, payload_text = connection.execute(
                "SELECT record.context_json,event.payload_json "
                "FROM memory_records AS record JOIN memory_events AS event "
                "ON event.event_id=record.source_event_id"
            ).fetchone()
        self.assertEqual(
            {"component": "release", "steps": ["Run tests", "Publish artifacts"]},
            json.loads(context_text),
        )
        self.assertEqual(
            json.loads(context_text), json.loads(payload_text)["record"]["context"]
        )

    async def test_store_rolls_back_event_and_projection_on_invalid_state(self) -> None:
        """An EventStore projection failure must never leave an orphan event."""
        from daem0nmcp.api.v7.runtime_services import RuntimeServiceError

        writer = self._writer()
        with self.assertRaisesRegex(
            RuntimeServiceError,
            "MEMORY_STORE_FAILED",
        ) as caught:
            await writer.store(
                self.workspace,
                self._store_command(record_type="unsupported"),
            )
        self.assertNotIn(str(self.root), str(caught.exception))
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                (0, 0),
                (
                    connection.execute("SELECT count(*) FROM memory_events").fetchone()[
                        0
                    ],
                    connection.execute(
                        "SELECT count(*) FROM memory_records"
                    ).fetchone()[0],
                ),
            )

    async def test_outcome_updates_existing_workspace_record_and_replays(
        self,
    ) -> None:
        """A missing or foreign record ID must not create an outcome stream."""
        from daem0nmcp.api.v7.pinned import (
            IdempotencyConflict,
            MemoryOutcomeCommand,
        )
        from daem0nmcp.api.v7.runtime_services import RuntimeServiceError
        from daem0nmcp.workspace import WorkspaceRegistry

        writer = self._writer()
        stored = await writer.store(self.workspace, self._store_command())
        command = MemoryOutcomeCommand(
            record_id=stored.record.record_id,
            outcome_text="The release stayed healthy.",
            worked=True,
            happened_at=WORKED_AT,
            idempotency_key="runtime-outcome-0001",
        )
        first = await writer.record_outcome(self.workspace, command)
        replay = await writer.record_outcome(self.workspace, command)
        self.assertFalse(first.idempotent_replay)
        self.assertTrue(replay.idempotent_replay)
        self.assertEqual(first.event, replay.event)
        self.assertEqual(2, first.event.stream_version)
        with closing(sqlite3.connect(self.database)) as connection:
            outcome, worked, source_event = connection.execute(
                "SELECT outcome,worked,source_event_id FROM memory_records "
                "WHERE record_id=?",
                (stored.record.record_id,),
            ).fetchone()
        self.assertEqual("The release stayed healthy.", outcome)
        self.assertEqual(1, worked)
        self.assertEqual(first.event.event_id, source_event)

        with self.assertRaises(IdempotencyConflict):
            await writer.record_outcome(
                self.workspace,
                MemoryOutcomeCommand(
                    record_id=stored.record.record_id,
                    outcome_text="Changed outcome.",
                    worked=False,
                    happened_at=WORKED_AT,
                    idempotency_key="runtime-outcome-0001",
                ),
            )
        missing = "mem_" + "f" * 64
        with self.assertRaisesRegex(RuntimeServiceError, "NOT_FOUND"):
            await writer.record_outcome(
                self.workspace,
                MemoryOutcomeCommand(
                    missing,
                    "Must not exist.",
                    False,
                    WORKED_AT,
                    "runtime-outcome-missing",
                ),
            )
        other_root = self.root / "other"
        other_root.mkdir()
        other_storage = other_root / ".daem0nmcp" / "storage"
        other_storage.mkdir(parents=True)
        other_database = other_storage / "daem0nmcp.db"
        other_connection = sqlite3.connect(other_database)
        try:
            other_connection.execute("PRAGMA foreign_keys=ON")
            _apply_v7_schema(other_connection)
        finally:
            other_connection.close()
        from daem0nmcp.storage_activation import (
            ActiveDatabasePointer,
            write_active_pointer,
        )

        write_active_pointer(
            other_storage,
            ActiveDatabasePointer(7, 1, "daem0nmcp.db", None, None),
        )
        other = WorkspaceRegistry([other_root], default_root=other_root).default
        with self.assertRaisesRegex(RuntimeServiceError, "NOT_FOUND"):
            await writer.record_outcome(other, command)

    async def test_cancelled_worker_retains_capacity_and_leaves_no_write(self) -> None:
        """Cancelling an asyncio waiter must not over-admit or commit later."""
        from daem0nmcp.bounded_workers import BoundedWorkerBusyError

        clock_entered = threading.Event()
        release_clock = threading.Event()

        def blocked_clock() -> datetime:
            clock_entered.set()
            release_clock.wait(timeout=2.0)
            return WORKED_AT

        writer = self._writer(clock=blocked_clock)
        first = asyncio.create_task(writer.store(self.workspace, self._store_command()))
        await asyncio.to_thread(clock_entered.wait, 1.0)
        first.cancel()
        await asyncio.sleep(0)
        with self.assertRaises(BoundedWorkerBusyError):
            await writer.store(
                self.workspace,
                self._store_command(idempotency_key="runtime-store-overflow"),
            )
        release_clock.set()
        with self.assertRaises(asyncio.CancelledError):
            await first
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                0,
                connection.execute("SELECT count(*) FROM memory_events").fetchone()[0],
            )


class RecallAdapterTests(
    _RuntimeServiceFixtures,
    unittest.IsolatedAsyncioTestCase,
):
    @staticmethod
    def _retrieval_result(stored, *, content_hash=None, provider="lexical"):
        from daem0nmcp.retrieval import (
            CitationEntry,
            ContextPackage,
            EvidenceItem,
            EvidenceRef,
            ProviderDiagnostic,
            RetrievalResult,
        )

        ref = EvidenceRef(
            record_id=stored.record.record_id,
            event_id=stored.event.event_id,
            content_hash=content_hash or stored.record.content_hash,
            version_id=None,
            provider=provider,
        )
        item = EvidenceItem(
            citation="[E1]",
            excerpt="Use one canonical event stream.",
            category="decision",
            status="current",
            score=1.25,
            channels=frozenset({"lexical"}),
            token_count=7,
            evidence_refs=(ref,),
            rationale="Replay-safe writes are easier to operate.",
            tags=("v7", "sqlite"),
        )
        text = "[E1] Use one canonical event stream."
        start = text.index("Use")
        context = ContextPackage(
            text=text,
            citations=(
                CitationEntry(
                    marker="[E1]",
                    evidence_refs=(ref,),
                    channels=frozenset({"lexical"}),
                    excerpt_start=start,
                    excerpt_end=len(text),
                ),
            ),
            token_budget=2400,
            requested_tokens=7,
            selected_tokens=7,
            rendered_tokens=8,
            dropped_tokens=0,
        )
        return RetrievalResult(
            items=(item,),
            context=context,
            providers=(
                ProviderDiagnostic(
                    provider="lexical",
                    status="ready",
                    manifest_generation=1,
                    elapsed_ms=1.5,
                    reason=None,
                    returned_count=1,
                ),
            ),
        )

    @staticmethod
    def _query(workspace_id: str):
        from daem0nmcp.retrieval import RetrievalQuery

        return RetrievalQuery(workspace_id=workspace_id, text="canonical")

    async def test_recall_maps_task8_after_canonical_reauthentication(
        self,
    ) -> None:
        """Trusting provider metadata without row/event/hash binding must fail."""
        from daem0nmcp.api.v7.runtime_services import Task8RecallService

        writer = self._writer()
        stored = await writer.store(self.workspace, self._store_command())
        expected = self._retrieval_result(stored)

        class Service:
            @staticmethod
            async def retrieve(query):
                self.assertEqual(self.workspace.workspace_id, query.workspace_id)
                return expected

        opened: list[Path] = []

        def factory(path: Path):
            opened.append(Path(path))
            return Service()

        service = Task8RecallService(service_factory=factory, max_workers=1)
        try:
            result = await service.retrieve(
                self.workspace,
                self._query(self.workspace.workspace_id),
                frozenset(),
            )
        finally:
            service.close()

        self.assertEqual([self.database], opened)
        self.assertFalse(result.abstained)
        self.assertEqual(
            "daem0nmcp/runtime.py",
            result.items[0].record.relative_file_path,
        )
        self.assertEqual(WORKED_AT, result.items[0].record.created_at)
        self.assertEqual("lexical", result.items[0].evidence_refs[0].provider)
        self.assertEqual(
            "[E1] Use one canonical event stream.",
            result.rendered_context,
        )
        self.assertEqual(8, result.token_usage.rendered)
        self.assertNotIn(str(self.root), result.model_dump_json())

    async def test_recall_cache_rotates_by_generation_and_configuration(self) -> None:
        from daem0nmcp.api.v7.runtime_services import Task8RecallService

        writer = self._writer()
        stored = await writer.store(self.workspace, self._store_command())
        expected = self._retrieval_result(stored)
        testcase = self

        class Resolver:
            generation = 1

            @contextmanager
            def locked_active(self, _workspace):
                yield SimpleNamespace(
                    path=testcase.database, generation=self.generation
                )

        class Service:
            def __init__(self):
                self.closed = 0

            async def retrieve(self, _query):
                return expected

            def close(self):
                self.closed += 1

        resolver = Resolver()
        config = SimpleNamespace(profile="first")
        created: list[Service] = []

        def factory(_path):
            service = Service()
            created.append(service)
            return service

        adapter = Task8RecallService(
            storage_resolver=resolver,
            service_factory=factory,
            config=config,
            max_workers=1,
        )
        query = self._query(self.workspace.workspace_id)
        try:
            await adapter.retrieve(self.workspace, query, frozenset())
            await adapter.retrieve(self.workspace, query, frozenset())
            self.assertEqual(1, len(created))

            resolver.generation = 2
            await adapter.retrieve(self.workspace, query, frozenset())
            self.assertEqual(2, len(created))
            self.assertEqual(1, created[0].closed)

            config.profile = "second"
            await adapter.retrieve(self.workspace, query, frozenset())
            self.assertEqual(3, len(created))
            self.assertEqual(1, created[1].closed)
        finally:
            adapter.close()

        self.assertEqual([1, 1, 1], [service.closed for service in created])

    async def test_default_hydration_capacity_admits_four_concurrent_recalls(
        self,
    ) -> None:
        from daem0nmcp.api.v7.runtime_services import Task8RecallService

        writer = self._writer()
        stored = await writer.store(self.workspace, self._store_command())
        expected = self._retrieval_result(stored)

        class Service:
            async def retrieve(self, _query):
                return expected

        adapter = Task8RecallService(service_factory=lambda _path: Service())
        original_hydrate = adapter._hydrate
        admitted = threading.Barrier(4)

        def synchronized_hydrate(*args):
            admitted.wait(timeout=2)
            return original_hydrate(*args)

        adapter._hydrate = synchronized_hydrate
        try:
            results = await asyncio.gather(
                *(
                    adapter.retrieve(
                        self.workspace,
                        self._query(self.workspace.workspace_id),
                        frozenset(),
                    )
                    for _ in range(4)
                )
            )
        finally:
            adapter.close()

        self.assertEqual(4, len(results))
        self.assertTrue(all(not result.abstained for result in results))

    async def test_hydration_over_capacity_is_retryable_unavailable(self) -> None:
        from daem0nmcp.api.v7.runtime_services import (
            RuntimeServiceError,
            Task8RecallService,
        )

        writer = self._writer()
        stored = await writer.store(self.workspace, self._store_command())
        expected = self._retrieval_result(stored)

        class Service:
            async def retrieve(self, _query):
                return expected

        adapter = Task8RecallService(
            service_factory=lambda _path: Service(), max_workers=1
        )
        original_hydrate = adapter._hydrate
        started = threading.Event()
        release = threading.Event()

        def blocked_hydrate(*args):
            started.set()
            release.wait(timeout=2)
            return original_hydrate(*args)

        adapter._hydrate = blocked_hydrate
        first = asyncio.create_task(
            adapter.retrieve(
                self.workspace,
                self._query(self.workspace.workspace_id),
                frozenset(),
            )
        )
        self.assertTrue(await asyncio.to_thread(started.wait, 2))
        try:
            with self.assertRaisesRegex(RuntimeServiceError, "RETRIEVAL_UNAVAILABLE"):
                await adapter.retrieve(
                    self.workspace,
                    self._query(self.workspace.workspace_id),
                    frozenset(),
                )
        finally:
            release.set()
            await first
            adapter.close()

    async def test_retired_recall_service_closes_after_in_flight_user(self) -> None:
        from daem0nmcp.api.v7.runtime_services import Task8RecallService

        writer = self._writer()
        stored = await writer.store(self.workspace, self._store_command())
        expected = self._retrieval_result(stored)
        testcase = self

        class Resolver:
            generation = 1

            @contextmanager
            def locked_active(self, _workspace):
                yield SimpleNamespace(
                    path=testcase.database, generation=self.generation
                )

        started = asyncio.Event()
        release = asyncio.Event()

        class Service:
            def __init__(self, generation):
                self.generation = generation
                self.closed = 0

            async def retrieve(self, _query):
                if self.generation == 1:
                    started.set()
                    await release.wait()
                return expected

            def close(self):
                self.closed += 1

        resolver = Resolver()
        created: list[Service] = []

        def factory(_path):
            service = Service(resolver.generation)
            created.append(service)
            return service

        adapter = Task8RecallService(
            storage_resolver=resolver,
            service_factory=factory,
            max_workers=2,
        )
        query = self._query(self.workspace.workspace_id)
        first = asyncio.create_task(
            adapter.retrieve(self.workspace, query, frozenset())
        )
        await started.wait()
        resolver.generation = 2
        await adapter.retrieve(self.workspace, query, frozenset())
        self.assertEqual(0, created[0].closed)
        release.set()
        await first
        self.assertEqual(1, created[0].closed)
        adapter.close()
        self.assertEqual(1, created[1].closed)

    async def test_timed_out_dense_worker_outlives_cache_rotation_and_shutdown(
        self,
    ) -> None:
        from daem0nmcp.api.v7.runtime_services import Task8RecallService
        from daem0nmcp.retrieval.providers import DenseProvider

        writer = self._writer()
        stored = await writer.store(self.workspace, self._store_command())
        expected = self._retrieval_result(stored)
        testcase = self

        class Resolver:
            generation = 1

            @contextmanager
            def locked_active(self, _workspace):
                yield SimpleNamespace(
                    path=testcase.database, generation=self.generation
                )

        class OwnedClient:
            def __init__(self):
                self.started = threading.Event()
                self.release = threading.Event()
                self.closed_event = threading.Event()
                self.in_use = False
                self.closed = 0
                self.closed_while_in_use = False

            def close(self):
                self.closed_while_in_use = self.in_use
                self.closed += 1
                self.closed_event.set()

        def make_provider(client):
            provider = DenseProvider(
                connection_factory=lambda: sqlite3.connect(testcase.database),
                provider_key="qdrant",
                model_id="test-model",
                dimension=3,
                encoder=lambda _text: [0.0, 0.0, 0.0],
                qdrant_path="unused-local-path",
                client_factory=lambda **_kwargs: client,
                timeout_seconds=0.02,
            )

            def blocking_search(_query, _limit, started_ns):
                owned = provider._get_client()
                owned.in_use = True
                owned.started.set()
                owned.release.wait(timeout=2)
                owned.in_use = False
                return provider._result(started_ns, status="ready")

            provider._search_sync = blocking_search
            return provider

        class Service:
            def __init__(self, provider):
                self.provider = provider

            async def retrieve(self, _query):
                return expected

            def close(self):
                self.provider.close()

        clients = {1: OwnedClient(), 2: OwnedClient()}
        providers = {
            generation: make_provider(client) for generation, client in clients.items()
        }
        resolver = Resolver()
        adapter = Task8RecallService(
            storage_resolver=resolver,
            service_factory=lambda _path: Service(providers[resolver.generation]),
            max_workers=1,
        )
        query = self._query(self.workspace.workspace_id)
        await adapter.retrieve(self.workspace, query, frozenset())

        timed_out = await providers[1].search(query, 1)
        self.assertEqual("DENSE_PROVIDER_TIMEOUT", timed_out.reason)
        resolver.generation = 2
        await adapter.retrieve(self.workspace, query, frozenset())
        self.assertEqual(0, clients[1].closed)
        clients[1].release.set()
        self.assertTrue(await asyncio.to_thread(clients[1].closed_event.wait, 2))
        self.assertEqual(1, clients[1].closed)
        self.assertFalse(clients[1].closed_while_in_use)

        timed_out = await providers[2].search(query, 1)
        self.assertEqual("DENSE_PROVIDER_TIMEOUT", timed_out.reason)
        adapter.close()
        self.assertEqual(0, clients[2].closed)
        clients[2].release.set()
        self.assertTrue(await asyncio.to_thread(clients[2].closed_event.wait, 2))
        self.assertEqual(1, clients[2].closed)
        self.assertFalse(clients[2].closed_while_in_use)

    async def test_recall_rejects_tamper_blank_provider_and_raw_path(
        self,
    ) -> None:
        """Fabricating provenance or leaking a canonical raw path must be impossible."""
        from daem0nmcp.api.v7.runtime_services import (
            RuntimeServiceError,
            Task8RecallService,
        )

        writer = self._writer()
        stored = await writer.store(self.workspace, self._store_command())

        async def run(result):
            class Service:
                async def retrieve(self, _query):
                    return result

            adapter = Task8RecallService(
                service_factory=lambda _path: Service(), max_workers=1
            )
            try:
                return await adapter.retrieve(
                    self.workspace,
                    self._query(self.workspace.workspace_id),
                    frozenset(),
                )
            finally:
                adapter.close()

        with self.assertRaisesRegex(
            RuntimeServiceError, "EVIDENCE_AUTHENTICATION_FAILED"
        ) as tamper:
            await run(self._retrieval_result(stored, content_hash="f" * 64))
        self.assertNotIn(str(self.root), str(tamper.exception))
        with self.assertRaisesRegex(
            RuntimeServiceError, "EVIDENCE_AUTHENTICATION_FAILED"
        ):
            await run(self._retrieval_result(stored, provider=""))

        # A row migrated from v6 keeps the host-absolute ``file_path`` v6
        # wrote.  It is legacy provenance, so it must not deny the recall, and
        # it must never leave the server.
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE memory_records SET file_path=? WHERE record_id=?",
                (str(self.root / "private.txt"), stored.record.record_id),
            )
            connection.commit()
        legacy = await run(self._retrieval_result(stored))
        self.assertNotIn("private.txt", legacy.model_dump_json())

    async def test_recall_rejects_federation_and_preserves_abstention(
        self,
    ) -> None:
        """Task 11 federation cannot be guessed, and abstention cannot gain evidence."""
        from daem0nmcp.api.v7.runtime_services import (
            RuntimeServiceError,
            Task8RecallService,
        )
        from daem0nmcp.retrieval import ProviderDiagnostic, RetrievalResult

        class Service:
            async def retrieve(self, _query):
                return RetrievalResult(
                    providers=(
                        ProviderDiagnostic(
                            "lexical", "failed", None, 0.5, "LEXICAL_FAILED", 0
                        ),
                    ),
                    abstained=True,
                    reason="LEXICAL_FAILED",
                )

        adapter = Task8RecallService(
            service_factory=lambda _path: Service(), max_workers=1
        )
        try:
            with self.assertRaisesRegex(RuntimeServiceError, "FEDERATION_UNAVAILABLE"):
                await adapter.retrieve(
                    self.workspace,
                    self._query(self.workspace.workspace_id),
                    frozenset({"ws_" + "f" * 24}),
                )
            result = await adapter.retrieve(
                self.workspace,
                self._query(self.workspace.workspace_id),
                frozenset(),
            )
        finally:
            adapter.close()
        self.assertTrue(result.abstained)
        self.assertEqual("LEXICAL_FAILED", result.abstention_reason)
        self.assertEqual([], result.items)
        self.assertIsNone(result.rendered_context)
        self.assertEqual(0, result.token_usage.rendered)


class BasicServiceTests(
    _RuntimeServiceFixtures,
    unittest.IsolatedAsyncioTestCase,
):
    async def test_health_is_exact_and_contains_no_storage_path(self) -> None:
        """A healthy workspace must report its real active storage versions."""
        from daem0nmcp.api.v7.models import CapabilityState
        from daem0nmcp.api.v7.runtime_services import BasicHealthService

        tasks = CapabilityState(
            name="tasks",
            status="disabled",
            reason_code="TASKS_UNAVAILABLE",
            remediation="Install the reviewed tasks profile.",
        )
        lexical = CapabilityState(name="lexical", status="ready")
        service = BasicHealthService(
            auth_mode="process",
            task_support=tasks,
            capability_states=(lexical,),
            package_version="7.0.0.dev0",
        )
        self.health_services.append(service)
        full = await service.inspect(self.workspace, True)
        compact = await service.inspect(None, False)
        with closing(sqlite3.connect(self.database)) as connection:
            actual_schema = connection.execute(
                "SELECT MAX(version) FROM schema_version"
            ).fetchone()[0]
        self.assertEqual("7", full.api_version)
        self.assertEqual("2025-11-25", full.protocol_version)
        self.assertEqual(
            (7, actual_schema),
            (full.storage_format_version, full.storage_schema_version),
        )
        self.assertEqual({"stdio", "streamable-http"}, full.supported_transports)
        self.assertEqual(
            [lexical, CapabilityState(name="storage", status="ready")],
            full.capability_states,
        )
        self.assertEqual(
            (7, actual_schema),
            (compact.storage_format_version, compact.storage_schema_version),
        )
        self.assertEqual([], compact.capability_states)
        self.assertNotIn(str(self.root), full.model_dump_json())

    async def test_online_health_never_runs_an_unbounded_database_scan(self) -> None:
        from unittest.mock import patch

        import daem0nmcp.api.v7.runtime_services as runtime_services

        original_connect = sqlite3.connect

        def guarded_connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs)

            def authorize(action, argument, _second, _database, _trigger):
                if action == sqlite3.SQLITE_PRAGMA and argument == "quick_check":
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            connection.set_authorizer(authorize)
            return connection

        with patch.object(
            runtime_services.sqlite3, "connect", side_effect=guarded_connect
        ):
            schema_version, tables = runtime_services._inspect_database_health(
                self.database
            )

        self.assertIsNotNone(schema_version)
        self.assertTrue(tables >= runtime_services._REQUIRED_V7_TABLES)

    async def test_blocked_health_probe_does_not_stall_loop_and_shutdown_drains(
        self,
    ) -> None:
        from unittest.mock import patch

        import daem0nmcp.api.v7.runtime_services as runtime_services
        from daem0nmcp.api.v7.models import CapabilityState

        started = threading.Event()
        release = threading.Event()
        service = runtime_services.BasicHealthService(
            auth_mode="process",
            task_support=CapabilityState(name="tasks", status="ready"),
        )
        self.health_services.append(service)

        def blocked_capability(_resolver, _workspace):
            started.set()
            release.wait(timeout=2)
            return (
                7,
                runtime_services._SCHEMA_VERSION,
                CapabilityState(name="storage", status="ready"),
            )

        with patch.object(
            runtime_services,
            "_storage_capability",
            side_effect=blocked_capability,
        ):
            inspection = asyncio.create_task(service.inspect(self.workspace, True))
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.1)
            inspection.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await inspection
            self.assertEqual(1, service._workers.in_flight)

            closing = asyncio.create_task(asyncio.to_thread(service.close))
            await asyncio.sleep(0.01)
            self.assertFalse(closing.done())
            release.set()
            await asyncio.wait_for(closing, timeout=1)

        self.health_services.remove(service)
        self.assertEqual(0, service._workers.in_flight)

    async def test_health_reports_readable_stale_storage_as_degraded(self) -> None:
        """Replacing the real schema version with the server target is a bug."""
        from daem0nmcp.api.v7.models import CapabilityState
        from daem0nmcp.api.v7.runtime_services import BasicHealthService
        from daem0nmcp.storage_activation import (
            ActiveDatabasePointer,
            write_active_pointer,
        )
        from daem0nmcp.workspace import WorkspaceRegistry

        stale_root = self.root / "stale"
        stale_storage = stale_root / ".daem0nmcp" / "storage"
        stale_storage.mkdir(parents=True)
        stale_database = stale_storage / "daem0nmcp.db"
        with closing(sqlite3.connect(stale_database)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            _apply_v7_schema(connection, through_version=19)
        write_active_pointer(
            stale_storage,
            ActiveDatabasePointer(7, 1, "daem0nmcp.db", None, None),
        )
        stale_workspace = WorkspaceRegistry(
            [stale_root], default_root=stale_root
        ).default
        service = BasicHealthService(
            auth_mode="process",
            task_support=CapabilityState(name="tasks", status="ready"),
        )
        self.health_services.append(service)

        result = await service.inspect(stale_workspace, True)

        self.assertEqual(
            (7, 19),
            (
                result.storage_format_version,
                result.storage_schema_version,
            ),
        )
        self.assertEqual(
            CapabilityState(
                name="storage",
                status="degraded",
                reason_code="STORAGE_SCHEMA_OUTDATED",
                remediation="Migrate the workspace storage to the current schema.",
            ),
            result.capability_states[-1],
        )
        self.assertNotIn(str(stale_root), result.model_dump_json())

    async def test_health_reports_absent_storage_without_claiming_versions(
        self,
    ) -> None:
        """An uninitialized workspace must not masquerade as current storage."""
        from daem0nmcp.api.v7.models import CapabilityState
        from daem0nmcp.api.v7.runtime_services import BasicHealthService
        from daem0nmcp.workspace import WorkspaceRegistry

        empty_root = self.root / "empty"
        empty_root.mkdir()
        empty_workspace = WorkspaceRegistry(
            [empty_root], default_root=empty_root
        ).default
        service = BasicHealthService(
            auth_mode="process",
            task_support=CapabilityState(name="tasks", status="ready"),
        )
        self.health_services.append(service)

        result = await service.inspect(empty_workspace, True)

        self.assertIsNone(result.storage_format_version)
        self.assertIsNone(result.storage_schema_version)
        self.assertEqual(
            CapabilityState(
                name="storage",
                status="failed",
                reason_code="STORAGE_UNAVAILABLE",
                remediation="Initialize or repair the workspace storage.",
            ),
            result.capability_states[-1],
        )
        self.assertNotIn(str(empty_root), result.model_dump_json())

    async def test_health_rejects_versions_from_broken_active_storage(
        self,
    ) -> None:
        """A schema marker alone must not authenticate incomplete v7 storage."""
        from daem0nmcp.api.v7.models import CapabilityState
        from daem0nmcp.api.v7.runtime_services import BasicHealthService

        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("DROP TABLE active_context_entries")
            connection.commit()
        service = BasicHealthService(
            auth_mode="process",
            task_support=CapabilityState(name="tasks", status="ready"),
        )
        self.health_services.append(service)

        result = await service.inspect(self.workspace, True)

        self.assertIsNone(result.storage_format_version)
        self.assertIsNone(result.storage_schema_version)
        self.assertEqual(
            CapabilityState(
                name="storage",
                status="failed",
                reason_code="STORAGE_INVALID",
                remediation="Repair or recreate the workspace storage.",
            ),
            result.capability_states[-1],
        )
        self.assertNotIn(str(self.root), result.model_dump_json())

    async def test_health_fails_closed_for_a_corrupt_database_header(self) -> None:
        from daem0nmcp.api.v7.models import CapabilityState
        from daem0nmcp.api.v7.runtime_services import BasicHealthService

        self.database.write_bytes(b"not a sqlite database")
        service = BasicHealthService(
            auth_mode="process",
            task_support=CapabilityState(name="tasks", status="ready"),
        )
        self.health_services.append(service)

        result = await service.inspect(self.workspace, True)

        self.assertIsNone(result.storage_format_version)
        self.assertIsNone(result.storage_schema_version)
        self.assertEqual("failed", result.capability_states[-1].status)
        self.assertEqual(
            "STORAGE_UNAVAILABLE", result.capability_states[-1].reason_code
        )

    async def test_health_dreaming_is_workspace_scoped_and_component_suppressed(
        self,
    ) -> None:
        from daem0nmcp.api.v7.models import CapabilityState
        from daem0nmcp.api.v7.runtime_services import BasicHealthService

        seen: list[str | None] = []

        async def dreaming_provider(workspace):
            seen.append(None if workspace is None else workspace.workspace_id)
            return {
                "enabled": True,
                "running": False,
                "yielded": False,
                "strategies": [
                    {
                        "strategy": "failed_decision",
                        "status": "idle",
                        "pending_count": 1,
                        "last_success_at": WORKED_AT,
                        "stable_error_code": None,
                        "yielded": False,
                    }
                ],
            }

        service = BasicHealthService(
            auth_mode="process",
            task_support=CapabilityState(name="tasks", status="ready"),
            dreaming_provider=dreaming_provider,
        )
        self.health_services.append(service)
        global_result = await service.inspect(None, True)
        suppressed = await service.inspect(self.workspace, False)
        scoped = await service.inspect(self.workspace, True)
        self.assertIsNone(global_result.dreaming)
        self.assertIsNone(suppressed.dreaming)
        self.assertEqual(1, scoped.dreaming.strategies[0].pending_count)
        self.assertEqual([self.workspace.workspace_id], seen)

    async def test_briefing_and_preflight_use_injected_readers(
        self,
    ) -> None:
        """Readers stay injected, path-free, and separate from Covenant state."""
        from daem0nmcp.api.v7.runtime_services import (
            BasicBriefingService,
            BasicPreflightService,
            RuntimeServiceError,
        )
        from daem0nmcp.api.v7.tools import SessionBriefInput

        observed: list[object] = []

        async def briefing_reader(workspace, request):
            observed.append((workspace.workspace_id, request.warning_limit))
            return {
                "workspace_id": workspace.workspace_id,
                "briefed_at": WORKED_AT,
                "workspace_statistics": {"records": 0},
            }

        def guidance_reader(workspace, target_tool, arguments, description):
            observed.append(
                (workspace.workspace_id, target_tool, dict(arguments), description)
            )
            return {"must_do": ["Reuse the same idempotency key."]}

        brief = await BasicBriefingService(
            reader=briefing_reader, clock=lambda: WORKED_AT
        ).assemble(
            self.workspace,
            SessionBriefInput(workspace_id=self.workspace.workspace_id),
        )
        guidance = await BasicPreflightService(reader=guidance_reader).guidance(
            self.workspace,
            "memory_store",
            {"idempotency_key": "stable-key"},
            "Store one decision",
        )
        self.assertEqual(self.workspace.workspace_id, brief.workspace_id)
        self.assertEqual(["Reuse the same idempotency key."], guidance.must_do)
        self.assertEqual(2, len(observed))

        async def leaky_reader(_workspace, _request):
            return {
                "workspace_id": self.workspace.workspace_id,
                "briefed_at": WORKED_AT,
                "workspace_statistics": {},
                "covenant_next_steps": [
                    {"tool": "memory_preflight", "reason": str(self.root)}
                ],
            }

        with self.assertRaisesRegex(
            RuntimeServiceError,
            "BRIEFING_FAILED",
        ) as leak:
            await BasicBriefingService(reader=leaky_reader).assemble(
                self.workspace,
                SessionBriefInput(workspace_id=self.workspace.workspace_id),
            )
        self.assertNotIn(str(self.root), str(leak.exception))


if __name__ == "__main__":
    unittest.main()
