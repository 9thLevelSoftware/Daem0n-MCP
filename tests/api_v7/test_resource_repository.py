from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from daem0nmcp.api.v7.resources import ResourceReadRequest, ResourceRow
from daem0nmcp.schema_version import CURRENT_SCHEMA_VERSION
from daem0nmcp.storage_activation import ResolvedActiveDatabase
from daem0nmcp.workspace import Workspace

WORKSPACE_ID = "ws_0123456789abcdef01234567"
OTHER_WORKSPACE_ID = "ws_89abcdef0123456701234567"
NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
MIGRATION_RUN_ID = "mig_" + "9" * 64


def _public_id(kind: str, prefix: str, source_key: int) -> str:
    value = [
        "daem0nmcp",
        "v7",
        "public-object-id",
        WORKSPACE_ID,
        kind,
        f"i:{source_key}",
        0,
    ]
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(encoded).hexdigest()}"


def _microseconds(value: datetime) -> int:
    return int(value.timestamp() * 1_000_000)


class _DatabaseFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.workspace_root = base / "private-workspace"
        self.storage = self.workspace_root / ".daem0nmcp" / "storage"
        self.storage.mkdir(parents=True)
        self.database_path = self.storage / "active.db"
        self.connection = sqlite3.connect(self.database_path)
        self._create_schema()

    def close(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    @property
    def workspace(self) -> Workspace:
        return Workspace(WORKSPACE_ID, self.workspace_root)

    @property
    def resolved(self) -> ResolvedActiveDatabase:
        return ResolvedActiveDatabase(
            storage_path=self.storage,
            path=self.database_path,
            relative_path="active.db",
            format_version=7,
            generation=3,
            previous_db=None,
            migration_run_id=MIGRATION_RUN_ID,
            pointer=None,
            pointer_bytes=None,
        )

    def _create_schema(self) -> None:
        self.connection.executescript(
            f"""
            CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
            INSERT INTO schema_version VALUES ({CURRENT_SCHEMA_VERSION});

            CREATE TABLE memory_events (
                event_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                stream_kind TEXT NOT NULL,
                event_hash TEXT NOT NULL
            );

            CREATE TABLE memory_records (
                record_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                record_type TEXT NOT NULL,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                tags_json TEXT NOT NULL,
                file_path TEXT,
                file_path_relative TEXT,
                archived INTEGER NOT NULL,
                worked INTEGER,
                created_at_us INTEGER NOT NULL,
                updated_at_us INTEGER NOT NULL,
                deleted_at_us INTEGER
            );

            CREATE TABLE rules (
                id INTEGER PRIMARY KEY,
                trigger TEXT NOT NULL,
                must_do TEXT NOT NULL,
                must_not TEXT NOT NULL,
                ask_first TEXT NOT NULL,
                warnings TEXT NOT NULL,
                priority INTEGER NOT NULL,
                enabled INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE active_context (
                id INTEGER PRIMARY KEY,
                project_path TEXT NOT NULL,
                memory_id INTEGER NOT NULL,
                priority INTEGER NOT NULL,
                reason TEXT,
                added_at TEXT NOT NULL,
                expires_at TEXT
            );

            CREATE TABLE v7_migration_runs (
                migration_run_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                status TEXT NOT NULL
            );

            CREATE TABLE legacy_id_map (
                migration_run_id TEXT NOT NULL,
                source_table TEXT NOT NULL,
                legacy_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                target_kind TEXT NOT NULL,
                target_id TEXT NOT NULL
            );

            CREATE TABLE projection_manifests (
                manifest_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                projection_name TEXT NOT NULL,
                generation INTEGER NOT NULL,
                status TEXT NOT NULL,
                source_event_root_hash TEXT NOT NULL,
                details_json TEXT NOT NULL,
                started_at_us INTEGER NOT NULL,
                completed_at_us INTEGER,
                activated_at_us INTEGER
            );
            """
        )
        from daem0nmcp.migrations.schema import MIGRATIONS

        for version in (19, 20, 21, 30):
            migration = next(item for item in MIGRATIONS if item[0] == version)
            for statement in migration[2]:
                self.connection.execute(statement)
        self.connection.execute(
            "INSERT INTO v7_migration_runs VALUES (?,?,?)",
            (MIGRATION_RUN_ID, WORKSPACE_ID, "active"),
        )
        self.connection.commit()

    def add_record(
        self,
        index: int,
        *,
        workspace_id: str = WORKSPACE_ID,
        record_type: str = "warning",
        content: str | None = None,
        tags_json: str = '["resource"]',
        relative_path: str | None = "src/example.py",
        archived: int = 0,
        worked: int | None = None,
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
        deleted_at: datetime | None = None,
    ) -> str:
        record_id = f"mem_{index:064x}"
        created = created_at or NOW - timedelta(days=1)
        updated = updated_at or NOW + timedelta(minutes=index)
        self.connection.execute(
            "INSERT INTO memory_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                record_id,
                workspace_id,
                record_type,
                content or f"record {index}",
                f"{index + 1000:064x}",
                tags_json,
                str(self.workspace_root / "secret.py"),
                relative_path,
                archived,
                worked,
                _microseconds(created),
                _microseconds(updated),
                None if deleted_at is None else _microseconds(deleted_at),
            ),
        )
        self.connection.commit()
        return record_id

    def add_public_mapping(self, kind: str, prefix: str, source_key: int) -> str:
        public_id = _public_id(kind, prefix, source_key)
        self.connection.execute(
            "INSERT INTO public_object_ids VALUES (?,?,?,?,?,?)",
            (
                WORKSPACE_ID,
                kind,
                f"i:{source_key}",
                0,
                public_id,
                _microseconds(NOW),
            ),
        )
        self.connection.commit()
        return public_id

    def add_rule(
        self,
        rule_id: int,
        *,
        priority: int,
        enabled: int = 1,
        must_do: str = '["test it"]',
    ) -> str:
        self.connection.execute(
            "INSERT INTO rules VALUES (?,?,?,?,?,?,?,?,?)",
            (
                rule_id,
                f"rule trigger {rule_id}",
                must_do,
                "[]",
                "[]",
                "[]",
                priority,
                enabled,
                (NOW + timedelta(minutes=rule_id))
                .replace(tzinfo=None)
                .isoformat(sep=" "),
            ),
        )
        public_id = self.add_public_mapping("rule", "rule", rule_id)
        from daem0nmcp.event_store import (
            GovernanceEventCommand,
            GovernanceEventStore,
        )

        happened_at = _microseconds(NOW + timedelta(minutes=rule_id))
        GovernanceEventStore(self.connection).append_and_project(
            GovernanceEventCommand(
                workspace_id=WORKSPACE_ID,
                stream_id=public_id,
                stream_kind="rule",
                event_type="rule.created",
                occurred_at_us=happened_at,
                recorded_at_us=happened_at,
                actor_type="migration",
                expected_stream_version=1,
                payload={
                    "rule_id": public_id,
                    "trigger": f"rule trigger {rule_id}",
                    "must_do": json.loads(must_do),
                    "must_not": [],
                    "ask_first": [],
                    "warnings": [],
                    "priority": priority,
                    "enabled": bool(enabled),
                    "created_at_us": happened_at,
                    "updated_at_us": happened_at,
                },
            )
        )
        self.connection.commit()
        return public_id

    def add_active_context(
        self,
        active_id: int,
        legacy_memory_id: int,
        record_id: str,
        *,
        priority: int,
        project_path: Path | None = None,
        expires_at: datetime | None = None,
        map_public_id: bool = True,
    ) -> str:
        self.connection.execute(
            "INSERT INTO active_context VALUES (?,?,?,?,?,?,?)",
            (
                active_id,
                str(project_path or self.workspace_root),
                legacy_memory_id,
                priority,
                f"reason {active_id}",
                NOW.replace(tzinfo=None).isoformat(sep=" "),
                None if expires_at is None else expires_at.isoformat(),
            ),
        )
        self.connection.execute(
            "INSERT INTO legacy_id_map VALUES (?,?,?,?,?,?)",
            (
                MIGRATION_RUN_ID,
                "memories",
                str(legacy_memory_id),
                WORKSPACE_ID,
                "memory",
                record_id,
            ),
        )
        public_id = _public_id("active_context", "act", active_id)
        if map_public_id:
            self.add_public_mapping("active_context", "act", active_id)
        self.connection.commit()
        return public_id

    def add_canonical_active_context(
        self,
        active_id: str,
        record_id: str,
        *,
        priority: int,
        added_at: datetime = NOW,
        expires_at: datetime | None = None,
        removed_at: datetime | None = None,
    ) -> str:
        self.connection.execute(
            "INSERT INTO active_context_entries VALUES (?,?,?,?,?,?,?,?)",
            (
                active_id,
                WORKSPACE_ID,
                record_id,
                priority,
                f"canonical {priority}",
                _microseconds(added_at),
                None if expires_at is None else _microseconds(expires_at),
                None if removed_at is None else _microseconds(removed_at),
            ),
        )
        self.connection.commit()
        return active_id


class SQLiteResourceRepositoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.fixture = _DatabaseFixture()
        self.resolver_threads: list[int] = []
        self.resolver_workspaces: list[Workspace] = []

    def tearDown(self) -> None:
        self.fixture.close()

    def _repository(self):
        from daem0nmcp.api.v7.resource_repository import SQLiteResourceRepository

        def resolve(workspace: Workspace) -> ResolvedActiveDatabase:
            self.resolver_threads.append(threading.get_ident())
            self.resolver_workspaces.append(workspace)
            return self.fixture.resolved

        return SQLiteResourceRepository(resolve, clock=lambda: NOW)

    def test_briefing_record_queries_use_bounded_order_indexes(self) -> None:
        workspace_id = self.fixture.workspace.workspace_id
        warning_plan = self.fixture.connection.execute(
            "EXPLAIN QUERY PLAN SELECT record_id FROM memory_records "
            "WHERE workspace_id=? AND record_type='warning' AND archived=0 "
            "AND deleted_at_us IS NULL "
            "ORDER BY updated_at_us DESC,record_id DESC LIMIT 50",
            (workspace_id,),
        ).fetchall()
        failure_plan = self.fixture.connection.execute(
            "EXPLAIN QUERY PLAN SELECT record_id FROM memory_records "
            "WHERE workspace_id=? AND worked=0 AND archived=0 "
            "AND deleted_at_us IS NULL "
            "ORDER BY updated_at_us DESC,record_id DESC LIMIT 50",
            (workspace_id,),
        ).fetchall()

        self.assertIn(
            "idx_memory_records_briefing_type",
            " ".join(str(row[3]) for row in warning_plan),
        )
        self.assertIn(
            "idx_memory_records_briefing_outcome",
            " ".join(str(row[3]) for row in failure_plan),
        )
        self.assertNotIn(
            "USE TEMP B-TREE",
            " ".join(str(row[3]) for row in (*warning_plan, *failure_plan)),
        )

    async def test_warning_and_failure_reads_are_scoped_bounded_and_path_safe(
        self,
    ) -> None:
        # Catches cross-workspace rows, post-limit filtering, raw paths, and wrong order.
        newest = self.fixture.add_record(1, content="x" * 5000)
        second = self.fixture.add_record(2, updated_at=NOW + timedelta(hours=2))
        self.fixture.add_record(3, archived=1, updated_at=NOW + timedelta(hours=3))
        self.fixture.add_record(
            4,
            deleted_at=NOW,
            updated_at=NOW + timedelta(hours=4),
        )
        self.fixture.add_record(5, workspace_id=OTHER_WORKSPACE_ID)
        failed = self.fixture.add_record(
            6,
            record_type="decision",
            worked=0,
            updated_at=NOW + timedelta(hours=6),
        )
        self.fixture.add_record(7, record_type="decision", worked=1)
        repository = self._repository()

        warnings = await repository.read_warnings(
            self.fixture.workspace,
            ResourceReadRequest(kind="warnings", limit=2, order_by="updated_at_desc"),
        )
        failures = await repository.read_failures(
            self.fixture.workspace,
            ResourceReadRequest(kind="failures", limit=2, order_by="updated_at_desc"),
        )

        self.assertEqual(
            [row.item.record_id for row in warnings],
            [second, newest],
        )
        self.assertTrue(all(isinstance(row, ResourceRow) for row in warnings))
        self.assertEqual(len(warnings[1].item.excerpt), 4000)
        self.assertEqual(warnings[0].item.relative_file_path, "src/example.py")
        self.assertEqual(warnings[0].item.current_status, "current")
        self.assertEqual([row.item.record_id for row in failures], [failed])
        rendered = json.dumps(
            [row.item.model_dump(mode="json") for row in [*warnings, *failures]]
        )
        self.assertNotIn(str(self.fixture.workspace_root), rendered)
        self.assertNotRegex(rendered, r'"record_id"\s*:\s*\d')

    async def test_memory_flags_are_honored_without_losing_row_state(self) -> None:
        # Catches hard-coded active-only predicates that ignore an explicit request.
        archived = self.fixture.add_record(10, archived=1)
        deleted = self.fixture.add_record(11, deleted_at=NOW)
        repository = self._repository()

        rows = await repository.read_warnings(
            self.fixture.workspace,
            ResourceReadRequest(
                kind="warnings",
                limit=10,
                order_by="updated_at_desc",
                include_archived=True,
                include_deleted=True,
            ),
        )

        by_id = {row.item.record_id: row for row in rows}
        self.assertEqual(by_id[archived].item.current_status, "archived")
        self.assertFalse(by_id[archived].deleted)
        self.assertEqual(by_id[deleted].item.current_status, "invalidated")
        self.assertTrue(by_id[deleted].deleted)

    async def test_canonical_root_embedded_in_public_text_fails_closed(self) -> None:
        # Catches free-text fields bypassing the path-safe structured field policy.
        self.fixture.add_record(
            12,
            content=f"do not expose {self.fixture.workspace_root / 'secret.py'}",
        )
        repository = self._repository()
        from daem0nmcp.api.v7.resource_repository import ResourceRepositoryError

        with self.assertRaises(ResourceRepositoryError):
            await repository.read_warnings(
                self.fixture.workspace,
                ResourceReadRequest(
                    kind="warnings", limit=1, order_by="updated_at_desc"
                ),
            )

    async def test_rules_use_canonical_ids_and_highest_priority_enabled_order(
        self,
    ) -> None:
        # Catches integer IDs, mapping bypass, disabled rows, and ascending priority.
        low = self.fixture.add_rule(21, priority=2)
        high = self.fixture.add_rule(22, priority=9)
        self.fixture.add_rule(23, priority=100, enabled=0)
        repository = self._repository()

        rows = await repository.read_rules(
            self.fixture.workspace,
            ResourceReadRequest(
                kind="rules",
                limit=10,
                order_by="priority_desc",
                enabled_only=True,
            ),
        )

        self.assertEqual([row.rule_id for row in rows], [high, low])
        self.assertEqual([row.priority for row in rows], [9, 2])
        self.assertEqual(rows[0].must_do, ["test it"])
        self.assertTrue(all(type(row.rule_id) is str for row in rows))

        self.fixture.connection.execute(
            "UPDATE rules SET trigger='tampered retained rule',must_do='[]'"
        )
        self.fixture.connection.commit()
        repeated = await repository.read_rules(
            self.fixture.workspace,
            ResourceReadRequest(
                kind="rules",
                limit=10,
                order_by="priority_desc",
                enabled_only=True,
            ),
        )
        self.assertEqual([row.rule_id for row in repeated], [high, low])
        self.assertTrue(all(row.trigger.startswith("rule trigger") for row in repeated))
        self.assertEqual(rows[0].created_at.tzinfo, timezone.utc)

    async def test_active_context_is_scoped_unexpired_and_maps_legacy_memory(
        self,
    ) -> None:
        # Catches project-path bleed, raw legacy memory IDs, expiry, and bad ordering.
        low_record = self.fixture.add_record(31, record_type="decision")
        high_record = self.fixture.add_record(32, record_type="pattern")
        expired_record = self.fixture.add_record(33, record_type="learning")
        foreign_record = self.fixture.add_record(34, record_type="warning")
        low = self.fixture.add_active_context(41, 301, low_record, priority=1)
        high = self.fixture.add_active_context(42, 302, high_record, priority=8)
        self.fixture.add_active_context(
            43,
            303,
            expired_record,
            priority=100,
            expires_at=NOW,
        )
        self.fixture.add_active_context(
            44,
            304,
            foreign_record,
            priority=200,
            project_path=self.fixture.workspace_root.parent / "other",
        )
        repository = self._repository()

        rows = await repository.read_active_context(
            self.fixture.workspace,
            ResourceReadRequest(
                kind="active_context", limit=10, order_by="priority_desc"
            ),
        )

        self.assertEqual(
            [row.item.active_context_id for row in rows],
            [high, low],
        )
        self.assertEqual(
            [row.item.record.record_id for row in rows],
            [high_record, low_record],
        )
        self.assertEqual([row.item.priority for row in rows], [8, 1])
        self.assertTrue(all(row.item.expires_at is None for row in rows))
        rendered_items = [row.item.model_dump(mode="json") for row in rows]
        self.assertTrue(all("memory_id" not in item for item in rendered_items))
        self.assertTrue(all("id" not in item for item in rendered_items))
        self.assertTrue(
            all(
                isinstance(item["active_context_id"], str)
                and item["active_context_id"].startswith("act_")
                for item in rendered_items
            )
        )

    async def test_active_context_merges_canonical_and_legacy_with_canonical_shadowing(
        self,
    ) -> None:
        # Catches duplicate records, public-ID mapping requirements for fresh
        # rows, and removed canonical rows accidentally reviving legacy state.
        legacy_only_record = self.fixture.add_record(81, record_type="warning")
        fresh_record = self.fixture.add_record(82, record_type="decision")
        shadowed_record = self.fixture.add_record(83, record_type="pattern")
        removed_record = self.fixture.add_record(84, record_type="learning")
        legacy_only = self.fixture.add_active_context(
            91, 901, legacy_only_record, priority=3
        )
        self.fixture.add_active_context(92, 902, shadowed_record, priority=100)
        self.fixture.add_active_context(93, 903, removed_record, priority=100)
        fresh = self.fixture.add_canonical_active_context(
            "act_" + "a" * 64,
            fresh_record,
            priority=8,
            added_at=NOW + timedelta(minutes=1),
        )
        shadow = self.fixture.add_canonical_active_context(
            "act_" + "b" * 64,
            shadowed_record,
            priority=1,
        )
        self.fixture.add_canonical_active_context(
            "act_" + "c" * 64,
            removed_record,
            priority=50,
            removed_at=NOW + timedelta(minutes=2),
        )
        repository = self._repository()

        rows = await repository.read_active_context(
            self.fixture.workspace,
            ResourceReadRequest(
                kind="active_context", limit=10, order_by="priority_desc"
            ),
        )

        self.assertEqual(
            [row.item.active_context_id for row in rows],
            [fresh, legacy_only, shadow],
        )
        self.assertEqual(
            [row.item.record.record_id for row in rows],
            [fresh_record, legacy_only_record, shadowed_record],
        )
        self.assertEqual(len({row.item.record.record_id for row in rows}), 3)
        self.assertNotIn(removed_record, {row.item.record.record_id for row in rows})

    async def test_shared_storage_lock_covers_resolution_and_row_materialization(
        self,
    ) -> None:
        # Catches pointer selection and SQLite reads occurring in different
        # storage-generation lock windows.
        self.fixture.add_record(94)
        from daem0nmcp.api.v7.resource_repository import SQLiteResourceRepository
        from daem0nmcp.storage_activation import DatabaseFileLock, DatabaseInUseError

        resolution_observations: list[str] = []

        def assert_exclusive_blocked() -> None:
            candidate = DatabaseFileLock(self.fixture.storage, "exclusive")
            try:
                candidate.acquire()
            except DatabaseInUseError:
                return
            finally:
                if candidate.acquired:
                    candidate.release()
            self.fail("exclusive storage lock was available during a resource read")

        def resolve(_workspace: Workspace) -> ResolvedActiveDatabase:
            assert_exclusive_blocked()
            resolution_observations.append("locked")
            return self.fixture.resolved

        repository = SQLiteResourceRepository(resolve, clock=lambda: NOW)
        original = repository._record_summary

        def materialize(row, workspace):
            assert_exclusive_blocked()
            resolution_observations.append("materialized")
            return original(row, workspace)

        repository._record_summary = materialize
        rows = await repository.read_warnings(
            self.fixture.workspace,
            ResourceReadRequest(kind="warnings", limit=1, order_by="updated_at_desc"),
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(resolution_observations, ["locked", "materialized"])
        with DatabaseFileLock(self.fixture.storage, "exclusive") as lock:
            self.assertTrue(lock.acquired)

    async def test_timeout_does_not_detach_worker_or_shared_storage_lock(self) -> None:
        # Catches a timeout response racing a still-running resolver/SQLite
        # worker that continues to hold the storage generation lock.
        from daem0nmcp.api.v7.resource_repository import (
            ResourceRepositoryError,
            SQLiteResourceRepository,
        )
        from daem0nmcp.bounded_workers import BoundedWorkerPool

        entered = threading.Event()
        release = threading.Event()
        pool = BoundedWorkerPool(max_workers=1, thread_name_prefix="resource-timeout")

        def resolve(_workspace: Workspace) -> ResolvedActiveDatabase:
            entered.set()
            release.wait(timeout=5)
            return self.fixture.resolved

        repository = SQLiteResourceRepository(
            resolve,
            clock=lambda: NOW,
            timeout_seconds=0.05,
            worker_pool=pool,
        )
        read = asyncio.create_task(
            repository.read_warnings(
                self.fixture.workspace,
                ResourceReadRequest(
                    kind="warnings", limit=1, order_by="updated_at_desc"
                ),
            )
        )
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            await asyncio.sleep(0.1)
            self.assertFalse(read.done())
            self.assertEqual(pool.in_flight, 1)
            release.set()
            with self.assertRaises(ResourceRepositoryError) as caught:
                await read
            # An overrun read that did not itself fail is retryable contention.
            self.assertEqual(caught.exception.code, "DATABASE_IN_USE")
            self.assertEqual(pool.in_flight, 0)
        finally:
            release.set()
            if not read.done():
                with self.assertRaises(ResourceRepositoryError):
                    await read
            pool.shutdown()

    async def test_cancellation_joins_worker_before_propagating(self) -> None:
        # Catches caller cancellation escaping while a non-cancellable resolver
        # remains in flight under the shared storage-generation lock.
        from daem0nmcp.api.v7.resource_repository import SQLiteResourceRepository
        from daem0nmcp.bounded_workers import BoundedWorkerPool

        entered = threading.Event()
        release = threading.Event()
        pool = BoundedWorkerPool(max_workers=1, thread_name_prefix="resource-cancel")

        def resolve(_workspace: Workspace) -> ResolvedActiveDatabase:
            entered.set()
            release.wait(timeout=5)
            return self.fixture.resolved

        repository = SQLiteResourceRepository(
            resolve,
            clock=lambda: NOW,
            timeout_seconds=2,
            worker_pool=pool,
        )
        read = asyncio.create_task(
            repository.read_warnings(
                self.fixture.workspace,
                ResourceReadRequest(
                    kind="warnings", limit=1, order_by="updated_at_desc"
                ),
            )
        )
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            read.cancel()
            await asyncio.sleep(0.05)
            self.assertFalse(read.done())
            self.assertEqual(pool.in_flight, 1)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await read
            self.assertEqual(pool.in_flight, 0)
        finally:
            release.set()
            if not read.done():
                read.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await read
            pool.shutdown()

    async def test_briefing_snapshot_holds_one_generation_lock_across_sections(
        self,
    ) -> None:
        # Catches four individually safe reads mixing pointer generations in
        # the gaps between warning/failure/rule/active-context sections.
        warning = self.fixture.add_record(101, record_type="warning")
        failure = self.fixture.add_record(102, record_type="decision", worked=0)
        rule = self.fixture.add_rule(103, priority=4)
        active_record = self.fixture.add_record(104, record_type="pattern")
        active = self.fixture.add_canonical_active_context(
            "act_" + "d" * 64, active_record, priority=2
        )
        other_active_record = self.fixture.add_record(105, record_type="pattern")
        self.fixture.add_canonical_active_context(
            "act_" + "e" * 64,
            other_active_record,
            priority=1,
        )
        from daem0nmcp.api.v7.resource_repository import SQLiteResourceRepository
        from daem0nmcp.storage_activation import DatabaseFileLock, DatabaseInUseError

        repository = SQLiteResourceRepository(
            lambda _workspace: self.fixture.resolved,
            clock=lambda: NOW,
        )
        original = repository._read_records_sync
        between_sections: list[str] = []
        calls = 0

        def read_records(*args, **kwargs):
            nonlocal calls
            result = original(*args, **kwargs)
            calls += 1
            if calls == 1:
                candidate = DatabaseFileLock(self.fixture.storage, "exclusive")
                try:
                    candidate.acquire()
                except DatabaseInUseError:
                    between_sections.append("blocked")
                else:
                    between_sections.append("switched")
                finally:
                    candidate.release()
            return result

        repository._read_records_sync = read_records
        snapshot = await repository.read_briefing_snapshot(
            self.fixture.workspace,
            warning_limit=1,
            failure_limit=1,
            rule_limit=1,
            active_context_limit=1,
        )

        self.assertEqual(between_sections, ["blocked"])
        self.assertEqual([row.item.record_id for row in snapshot.warnings], [warning])
        self.assertEqual([row.item.record_id for row in snapshot.failures], [failure])
        self.assertEqual([row.rule_id for row in snapshot.rules], [rule])
        self.assertEqual(
            [row.item.active_context_id for row in snapshot.active_context], [active]
        )
        self.assertEqual(snapshot.workspace_statistics["active_context"], 2)

    async def test_snapshot_skips_git_subprocesses_when_changes_are_unused(
        self,
    ) -> None:
        # Preflight guidance paid two git subprocesses per call for data it
        # discarded; under load that pushed reads past their deadline.
        from daem0nmcp.api.v7.resource_repository import SQLiteResourceRepository

        repository = SQLiteResourceRepository(
            lambda _workspace: self.fixture.resolved,
            clock=lambda: NOW,
        )
        git_reads: list[object] = []

        def read_git_changes(workspace):
            git_reads.append(workspace)
            return []

        repository._read_git_changes_sync = read_git_changes
        limits = {"warning_limit": 1, "failure_limit": 1, "active_context_limit": 1}
        skipped = await repository.read_briefing_snapshot(
            self.fixture.workspace, include_git_changes=False, **limits
        )
        self.assertEqual((skipped.git_changes, git_reads), ([], []))
        await repository.read_briefing_snapshot(self.fixture.workspace, **limits)
        self.assertEqual(len(git_reads), 1)

    async def test_briefing_sections_share_one_sqlite_read_snapshot(self) -> None:
        # A canonical writer may commit while holding the same shared generation
        # lock. Every section of one brief must still observe one SQLite snapshot.
        self.fixture.connection.execute("PRAGMA journal_mode=WAL")
        warning = self.fixture.add_record(111, record_type="warning")
        self.fixture.connection.commit()
        from daem0nmcp.api.v7.resource_repository import SQLiteResourceRepository

        repository = SQLiteResourceRepository(
            lambda _workspace: self.fixture.resolved,
            clock=lambda: NOW,
        )
        original = repository._read_records_sync
        calls = 0

        def read_records(*args, **kwargs):
            nonlocal calls
            result = original(*args, **kwargs)
            calls += 1
            if calls == 1:
                with closing(sqlite3.connect(self.fixture.database_path)) as writer:
                    writer.execute(
                        "INSERT INTO memory_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            f"mem_{112:064x}",
                            WORKSPACE_ID,
                            "decision",
                            "record 112",
                            f"{1112:064x}",
                            "[]",
                            str(self.fixture.workspace_root / "secret.py"),
                            "src/example.py",
                            0,
                            0,
                            _microseconds(NOW - timedelta(days=1)),
                            _microseconds(NOW + timedelta(minutes=112)),
                            None,
                        ),
                    )
                    writer.commit()
            return result

        repository._read_records_sync = read_records
        snapshot = await repository.read_briefing_snapshot(
            self.fixture.workspace,
            warning_limit=1,
            failure_limit=1,
            rule_limit=1,
            active_context_limit=1,
        )

        self.assertEqual([row.item.record_id for row in snapshot.warnings], [warning])
        self.assertEqual(snapshot.failures, [])

    async def test_retained_unmapped_rule_is_ignored_and_active_context_fails_closed(
        self,
    ) -> None:
        # Catches silent row drops or freshly invented IDs during a read-only call.
        self.fixture.connection.execute(
            "INSERT INTO rules VALUES (?,?,?,?,?,?,?,?,?)",
            (51, "unmapped", "[]", "[]", "[]", "[]", 1, 1, NOW.isoformat()),
        )
        record_id = self.fixture.add_record(52, record_type="decision")
        self.fixture.add_active_context(
            53,
            501,
            record_id,
            priority=1,
            map_public_id=False,
        )
        self.fixture.connection.commit()
        repository = self._repository()
        from daem0nmcp.api.v7.resource_repository import ResourceRepositoryError

        rules = await repository.read_rules(
            self.fixture.workspace,
            ResourceReadRequest(kind="rules", limit=10, order_by="priority_desc"),
        )
        self.assertEqual(rules, [])

        with self.assertRaises(ResourceRepositoryError) as caught:
            await repository.read_active_context(
                self.fixture.workspace,
                ResourceReadRequest(
                    kind="active_context", limit=10, order_by="priority_desc"
                ),
            )
        self.assertEqual(caught.exception.code, "RESOURCE_REPOSITORY_UNAVAILABLE")
        self.assertEqual(
            0,
            self.fixture.connection.execute(
                "SELECT count(*) FROM public_object_ids"
            ).fetchone()[0],
        )

    async def test_malformed_json_and_wrong_schema_are_sanitized(self) -> None:
        # Catches permissive JSON fallback and exceptions that expose SQL or paths.
        rule_id = self.fixture.add_rule(61, priority=1)
        self.fixture.connection.execute("PRAGMA ignore_check_constraints=ON")
        self.fixture.connection.execute(
            "UPDATE governance_rules SET must_do_json='[\"ok\", NaN]' WHERE rule_id=?",
            (rule_id,),
        )
        self.fixture.connection.execute("PRAGMA ignore_check_constraints=OFF")
        self.fixture.connection.commit()
        repository = self._repository()
        from daem0nmcp.api.v7.resource_repository import ResourceRepositoryError

        with self.assertRaises(ResourceRepositoryError) as malformed:
            await repository.read_rules(
                self.fixture.workspace,
                ResourceReadRequest(kind="rules", limit=10, order_by="priority_desc"),
            )

        self.fixture.connection.execute("UPDATE schema_version SET version=18")
        self.fixture.connection.commit()
        with self.assertRaises(ResourceRepositoryError) as old_schema:
            await repository.read_warnings(
                self.fixture.workspace,
                ResourceReadRequest(
                    kind="warnings", limit=10, order_by="updated_at_desc"
                ),
            )

        errors = (malformed.exception, old_schema.exception)
        self.assertEqual(
            {(error.code, error.args) for error in errors},
            {("RESOURCE_REPOSITORY_UNAVAILABLE", ("RESOURCE_REPOSITORY_UNAVAILABLE",))},
        )
        rendered = " ".join(str(error) + repr(error) for error in errors).lower()
        for secret in ("private-workspace", "sqlite", "select", "schema_version"):
            self.assertNotIn(secret, rendered)

    async def test_oversized_json_is_rejected_before_becoming_a_resource(self) -> None:
        # Catches syntactically valid padding bypassing the repository JSON bound.
        self.fixture.add_record(
            65,
            tags_json=" " * 65_536 + '["resource"]',
        )
        repository = self._repository()
        from daem0nmcp.api.v7.resource_repository import ResourceRepositoryError

        with self.assertRaises(ResourceRepositoryError) as caught:
            await repository.read_warnings(
                self.fixture.workspace,
                ResourceReadRequest(
                    kind="warnings", limit=10, order_by="updated_at_desc"
                ),
            )
        self.assertEqual(caught.exception.code, "RESOURCE_REPOSITORY_UNAVAILABLE")

    async def test_resolver_and_sqlite_work_run_off_loop_and_factory_matches_dependencies(
        self,
    ) -> None:
        # Catches direct event-loop SQLite use and a factory wired to wrong methods.
        self.fixture.add_record(71)
        loop_thread = threading.get_ident()
        repository = self._repository()
        rows = await repository.read_warnings(
            self.fixture.workspace,
            ResourceReadRequest(kind="warnings", limit=1, order_by="updated_at_desc"),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.resolver_workspaces, [self.fixture.workspace])
        self.assertEqual(len(self.resolver_threads), 1)
        self.assertNotEqual(self.resolver_threads[0], loop_thread)

        from daem0nmcp.api.v7.resource_repository import (
            build_sqlite_resource_readers,
        )

        readers = build_sqlite_resource_readers(
            lambda workspace: self.fixture.resolved,
            clock=lambda: NOW,
        )
        self.assertTrue(asyncio.iscoroutinefunction(readers.warning_reader))
        self.assertTrue(asyncio.iscoroutinefunction(readers.failure_reader))
        self.assertTrue(asyncio.iscoroutinefunction(readers.rule_reader))
        self.assertTrue(asyncio.iscoroutinefunction(readers.active_context_reader))

    async def test_invalid_request_is_rejected_before_database_resolution(self) -> None:
        # Catches reader mix-ups and unbounded limits reaching SQLite.
        repository = self._repository()
        invalid = (
            ResourceReadRequest(kind="failures", limit=1, order_by="updated_at_desc"),
            ResourceReadRequest(kind="warnings", limit=0, order_by="updated_at_desc"),
            ResourceReadRequest(kind="warnings", limit=52, order_by="updated_at_desc"),
            ResourceReadRequest(kind="warnings", limit=1, order_by="priority_desc"),
        )
        for request in invalid:
            with self.subTest(request=request), self.assertRaises(ValueError):
                await repository.read_warnings(self.fixture.workspace, request)
        self.assertEqual(self.resolver_threads, [])


if __name__ == "__main__":
    unittest.main()
