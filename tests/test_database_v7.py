"""DatabaseManager activation tests; dependency-backed cases skip honestly."""

from __future__ import annotations

import ast
import importlib.util
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from daem0nmcp.schema_version import CURRENT_SCHEMA_VERSION

_V7_CORE_TABLES = {
    "memory_events",
    "memory_records",
    "memory_fact_versions",
    "memory_relationship_versions",
    "projection_manifests",
    "enrichment_decisions",
    "background_jobs",
    "v7_migration_runs",
    "v7_migration_checkpoints",
    "legacy_id_map",
}
_V7_RETRIEVAL_TABLES = {
    "retrieval_documents",
    "record_procedures",
    "record_outcome_view",
    "dense_projection_refs",
}
_V7_LOCAL_STATE_TABLES = {
    "public_object_ids",
    "active_context_entries",
    "governance_events",
    "governance_rules",
    "governance_context_triggers",
    "session_update_sequence",
}
_V7_DISCOVERY_TABLES = {
    "discovery_projection_partitions",
    "discovery_entities",
    "discovery_entity_records",
    "discovery_communities",
    "discovery_community_members",
    "discovery_code_entities",
    "discovery_code_edges",
}


def _load_database_manager_without_optional_dependencies():
    """Load the real manager while replacing only unavailable import boundaries."""

    def listens_for(*_args, **_kwargs):
        return lambda function: function

    def async_sessionmaker(**_kwargs):
        return None

    def create_async_engine(*_args, **_kwargs):
        return None

    sqlalchemy = types.ModuleType("sqlalchemy")
    sqlalchemy.event = types.SimpleNamespace(listens_for=listens_for)
    sqlalchemy_ext = types.ModuleType("sqlalchemy.ext")
    sqlalchemy_async = types.ModuleType("sqlalchemy.ext.asyncio")
    sqlalchemy_async.AsyncSession = type("AsyncSession", (), {})
    sqlalchemy_async.async_sessionmaker = async_sessionmaker
    sqlalchemy_async.create_async_engine = create_async_engine
    sqlalchemy_pool = types.ModuleType("sqlalchemy.pool")
    sqlalchemy_pool.NullPool = type("NullPool", (), {})
    models = types.ModuleType("daem0nmcp.models")
    models.Base = type("Base", (), {})
    models.MemoryVersion = type("MemoryVersion", (), {})
    replacements = {
        "sqlalchemy": sqlalchemy,
        "sqlalchemy.ext": sqlalchemy_ext,
        "sqlalchemy.ext.asyncio": sqlalchemy_async,
        "sqlalchemy.pool": sqlalchemy_pool,
        "daem0nmcp.models": models,
    }
    source_path = Path(__file__).resolve().parents[1] / "daem0nmcp" / "database.py"
    spec = importlib.util.spec_from_file_location(
        "daem0nmcp._database_validation_subject", source_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("database validation subject could not be loaded")
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, replacements):
        spec.loader.exec_module(module)
    return module.DatabaseManager


def _create_v7_validation_database(
    path: Path,
    *,
    schema_version: int = CURRENT_SCHEMA_VERSION,
) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE schema_version(version INTEGER PRIMARY KEY, applied_at TEXT)"
        )
        connection.execute(
            "INSERT INTO schema_version(version, applied_at) VALUES (?, 'now')",
            (schema_version,),
        )
        for table in sorted(
            _V7_CORE_TABLES
            | _V7_RETRIEVAL_TABLES
            | _V7_LOCAL_STATE_TABLES
            | _V7_DISCOVERY_TABLES
        ):
            connection.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY)')
        connection.commit()
    finally:
        connection.close()


def _create_real_empty_v7_database(path: Path) -> None:
    from daem0nmcp.migrations.schema import MIGRATIONS

    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE schema_version(version INTEGER PRIMARY KEY, applied_at TEXT)"
        )
        for version, _description, statements in MIGRATIONS:
            if version < 16 or version > CURRENT_SCHEMA_VERSION:
                continue
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_version(version, applied_at) VALUES (?, 'now')",
                (version,),
            )
        connection.commit()
    finally:
        connection.close()


class DatabaseManagerSourceContractTests(unittest.TestCase):
    def test_manager_resolves_pointer_holds_lock_and_never_swallows_migrations(self):
        """Static contract remains runnable when SQLAlchemy is unavailable."""
        source_path = Path(__file__).resolve().parents[1] / "daem0nmcp" / "database.py"
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertIn("resolve_active_database", calls)
        self.assertIn("DatabaseFileLock", calls)
        self.assertIn("write_active_pointer", calls)
        self.assertNotIn("Migration check failed", source)

    def test_additive_schema_16_does_not_reclassify_populated_v6_storage(self):
        """Architecture format comes only from the pointer, never SQL version 16."""
        source_path = Path(__file__).resolve().parents[1] / "daem0nmcp" / "database.py"
        source = source_path.read_text(encoding="utf-8")
        self.assertNotIn("V7_POINTER_RECOVERY_REQUIRED", source)

    def test_fast_hook_and_embedding_maintenance_follow_active_pointer(self):
        """Raw sqlite consumers may not keep reading the retained v6 source."""
        from daem0nmcp.claude_hooks.session_start import _fast_briefing
        from daem0nmcp.migrations.migrate_embedding_model import _resolve_db_path
        from daem0nmcp.storage_activation import (
            ActiveDatabasePointer,
            write_active_pointer,
        )

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            storage = root / ".daem0nmcp" / "storage"
            run_id = "mig_" + "1" * 64
            run_dir = storage / "migrations" / "v7" / run_id
            run_dir.mkdir(parents=True)
            source = storage / "daem0nmcp.db"
            active = run_dir / "candidate.db"
            for path, count in ((source, 1), (active, 2)):
                connection = sqlite3.connect(path)
                connection.executescript(
                    "CREATE TABLE memories(id INTEGER PRIMARY KEY, category TEXT, worked INTEGER);"
                    "CREATE TABLE session_state(session_id TEXT PRIMARY KEY, project_path TEXT, "
                    "briefed INTEGER, context_checks TEXT, pending_decisions TEXT, "
                    "last_activity TEXT, created_at TEXT);"
                )
                connection.executemany(
                    "INSERT INTO memories(id,category,worked) VALUES (?, 'decision', NULL)",
                    [(index,) for index in range(1, count + 1)],
                )
                connection.commit()
                connection.close()
            write_active_pointer(
                storage,
                ActiveDatabasePointer(
                    7,
                    1,
                    f"migrations/v7/{run_id}/candidate.db",
                    "daem0nmcp.db",
                    run_id,
                ),
            )

            self.assertEqual(str(active.resolve()), _resolve_db_path(str(root)))
            self.assertIn("2 memories", _fast_briefing(str(root)))

    def test_cli_additive_migration_uses_manager_selected_database(self):
        source = (
            Path(__file__).resolve().parents[1] / "daem0nmcp" / "cli.py"
        ).read_text(encoding="utf-8")
        migrate = source[source.index('elif args.command == "migrate"') :]
        migrate = migrate[: migrate.index('elif args.command == "status"')]
        self.assertIn("db.db_path", migrate)
        self.assertNotIn('Path(storage_path) / "daem0nmcp.db"', migrate)


class DatabaseManagerActivationValidationTests(unittest.TestCase):
    def _manager_for(self, path: Path):
        manager_type = _load_database_manager_without_optional_dependencies()
        manager = object.__new__(manager_type)
        manager.db_path = path
        manager.migration_run_id = None
        return manager

    def test_format_seven_rejects_schema_version_nineteen(self):
        """A v7 pointer may not activate before active-context migration 20."""

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            path = Path(raw) / "candidate.db"
            _create_v7_validation_database(path, schema_version=19)

            with self.assertRaisesRegex(RuntimeError, "SCHEMA_MIGRATION_INCOMPLETE"):
                self._manager_for(path)._validate_database(format_version=7)

    def test_format_seven_rejects_each_missing_retrieval_table(self):
        """Every durable retrieval projection table is part of the active schema."""

        for missing in sorted(_V7_RETRIEVAL_TABLES):
            with (
                self.subTest(missing=missing),
                tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw,
            ):
                path = Path(raw) / "candidate.db"
                _create_v7_validation_database(path)
                connection = sqlite3.connect(path)
                try:
                    connection.execute(f'DROP TABLE "{missing}"')
                    connection.commit()
                finally:
                    connection.close()

                with self.assertRaisesRegex(RuntimeError, "V7_SCHEMA_INCOMPLETE"):
                    self._manager_for(path)._validate_database(format_version=7)

    def test_format_seven_rejects_missing_public_object_id_table(self):
        """Opaque public IDs are required before a v7 database can activate."""

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            path = Path(raw) / "candidate.db"
            _create_v7_validation_database(path)
            connection = sqlite3.connect(path)
            try:
                connection.execute("DROP TABLE public_object_ids")
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(RuntimeError, "V7_SCHEMA_INCOMPLETE"):
                self._manager_for(path)._validate_database(format_version=7)

    def test_format_seven_rejects_missing_canonical_active_context_table(self):
        """Fresh v7 active-context state is required before activation."""

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            path = Path(raw) / "candidate.db"
            _create_v7_validation_database(path)
            connection = sqlite3.connect(path)
            try:
                connection.execute("DROP TABLE active_context_entries")
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(RuntimeError, "V7_SCHEMA_INCOMPLETE"):
                self._manager_for(path)._validate_database(format_version=7)

    def test_format_seven_rejects_each_missing_governance_table(self):
        """The append log, projections, and shared update order are required."""

        governance_tables = {
            "governance_events",
            "governance_rules",
            "governance_context_triggers",
            "session_update_sequence",
        }
        for missing in sorted(governance_tables):
            with (
                self.subTest(missing=missing),
                tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw,
            ):
                path = Path(raw) / "candidate.db"
                _create_v7_validation_database(path)
                connection = sqlite3.connect(path)
                try:
                    connection.execute(f'DROP TABLE "{missing}"')
                    connection.commit()
                finally:
                    connection.close()
                with self.assertRaisesRegex(RuntimeError, "V7_SCHEMA_INCOMPLETE"):
                    self._manager_for(path)._validate_database(format_version=7)

    def test_format_seven_rejects_each_missing_discovery_table(self):
        """An active v7 database cannot fall back to path-keyed discovery rows."""

        for missing in sorted(_V7_DISCOVERY_TABLES):
            with (
                self.subTest(missing=missing),
                tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw,
            ):
                path = Path(raw) / "candidate.db"
                _create_v7_validation_database(path)
                connection = sqlite3.connect(path)
                try:
                    connection.execute(f'DROP TABLE "{missing}"')
                    connection.commit()
                finally:
                    connection.close()
                with self.assertRaisesRegex(RuntimeError, "V7_SCHEMA_INCOMPLETE"):
                    self._manager_for(path)._validate_database(format_version=7)

    def test_fresh_v7_bootstrap_is_idempotent_and_activates_empty_lexical_fts(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            path = Path(raw) / "candidate.db"
            _create_real_empty_v7_database(path)
            manager = self._manager_for(path)
            manager.workspace_id = "ws_0123456789abcdef01234567"
            bootstrap = getattr(manager, "_bootstrap_lexical_projection", None)
            self.assertTrue(callable(bootstrap))

            bootstrap()
            bootstrap()

            connection = sqlite3.connect(path)
            try:
                manifests = connection.execute(
                    "SELECT generation,status,row_count,details_json "
                    "FROM projection_manifests WHERE workspace_id=? "
                    "AND projection_name='lexical' ORDER BY generation",
                    (manager.workspace_id,),
                ).fetchall()
                self.assertEqual(1, len(manifests))
                self.assertEqual((1, "active", 0), tuple(manifests[0][:3]))
                details = __import__("json").loads(manifests[0][3])
                self.assertEqual(
                    0,
                    connection.execute(
                        f'SELECT count(*) FROM "{details["fts_table"]}"'
                    ).fetchone()[0],
                )
            finally:
                connection.close()

    def test_fresh_v7_bootstrap_seeds_every_optional_projection_as_stale(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            path = Path(raw) / "candidate.db"
            _create_real_empty_v7_database(path)
            manager = self._manager_for(path)
            manager.workspace_id = "ws_0123456789abcdef01234567"

            manager._bootstrap_lexical_projection()
            manager._bootstrap_lexical_projection()

            with sqlite3.connect(path) as connection:
                rows = connection.execute(
                    "SELECT projection_name,status,row_count "
                    "FROM projection_manifests WHERE workspace_id=? "
                    "AND projection_name IN "
                    "('dense','graph','temporal','procedure','outcome') "
                    "ORDER BY projection_name",
                    (manager.workspace_id,),
                ).fetchall()
                queued = [
                    __import__("json").loads(row[0])["projection_names"]
                    for row in connection.execute(
                        "SELECT payload_json FROM background_jobs "
                        "WHERE workspace_id=? AND "
                        "job_type='retrieval.projection_rebuild' "
                        "ORDER BY idempotency_key",
                        (manager.workspace_id,),
                    ).fetchall()
                ]
            self.assertEqual(
                [
                    ("dense", "rebuild_required", 0),
                    ("graph", "rebuild_required", 0),
                    ("outcome", "rebuild_required", 0),
                    ("procedure", "rebuild_required", 0),
                    ("temporal", "rebuild_required", 0),
                ],
                rows,
            )
            self.assertEqual(
                [["dense"], ["graph"], ["outcome"], ["procedure"], ["temporal"]],
                queued,
            )


@unittest.skipUnless(
    importlib.util.find_spec("sqlalchemy") is not None
    and importlib.util.find_spec("aiosqlite") is not None,
    "SQLAlchemy/aiosqlite unavailable in dependency-free environment",
)
class DatabaseManagerDependencyIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_init_writes_format7_pointer_and_second_init_is_noop(self):
        from daem0nmcp.database import DatabaseManager
        from daem0nmcp.storage_activation import resolve_active_database

        with tempfile.TemporaryDirectory() as raw:
            manager = DatabaseManager(raw)
            await manager.init_db()
            pointer_before = (Path(raw) / "active-db.json").read_bytes()
            await manager.init_db()
            resolved = resolve_active_database(raw)
            self.assertEqual(7, resolved.format_version)
            self.assertEqual(1, resolved.generation)
            self.assertEqual("daem0nmcp.db", resolved.relative_path)
            self.assertEqual(
                pointer_before, (Path(raw) / "active-db.json").read_bytes()
            )
            await manager.close()

    async def test_invalid_pointer_fails_before_engine_construction(self):
        from daem0nmcp.database import DatabaseManager
        from daem0nmcp.storage_activation import PointerValidationError

        with tempfile.TemporaryDirectory() as raw:
            storage = Path(raw)
            (storage / "daem0nmcp.db").write_bytes(b"not-used")
            (storage / "active-db.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(PointerValidationError):
                DatabaseManager(raw)

    @mock.patch("daem0nmcp.retrieval.runtime.schedule_projection_job_drain")
    async def test_async_compatibility_facade_scans_adapted_cursor_in_batches(
        self, _schedule_projection_job_drain
    ):
        from daem0nmcp.database import DatabaseManager
        from daem0nmcp.event_store import (
            EventCommand,
            append_and_project_async,
            resolve_compatibility_stream_async,
        )

        with tempfile.TemporaryDirectory() as raw:
            manager = DatabaseManager(raw)
            await manager.init_db()
            record_id = "mem_" + "f" * 64
            record = {
                "record_type": "decision",
                "legacy_type": None,
                "content": "async compatibility",
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
                "source_client": "test",
                "source_model": None,
                "deleted_at_us": None,
            }
            try:
                async with manager.get_session() as session:
                    await append_and_project_async(
                        session,
                        EventCommand(
                            workspace_id=manager.workspace_id,
                            stream_id=record_id,
                            stream_kind="memory",
                            event_type="memory.created",
                            occurred_at_us=1,
                            recorded_at_us=1,
                            actor_type="system",
                            payload={
                                "record": record,
                                "compatibility": {"legacy_memory_id": 42},
                            },
                        ),
                    )
                    self.assertEqual(
                        record_id,
                        await resolve_compatibility_stream_async(
                            session,
                            manager.workspace_id,
                            "memory",
                            "memories",
                            42,
                        ),
                    )
                    rolled_back_id = "mem_" + "e" * 64
                with self.assertRaisesRegex(RuntimeError, "rollback boundary"):
                    async with manager.get_session() as session:
                        await append_and_project_async(
                            session,
                            EventCommand(
                                workspace_id=manager.workspace_id,
                                stream_id=rolled_back_id,
                                stream_kind="memory",
                                event_type="memory.created",
                                occurred_at_us=2,
                                recorded_at_us=2,
                                actor_type="system",
                                payload={
                                    "record": {**record, "content": "roll back"},
                                    "compatibility": {"legacy_memory_id": 43},
                                },
                            ),
                        )
                        raise RuntimeError("rollback boundary")
                async with manager.get_session() as session:
                    self.assertEqual(
                        record_id,
                        await resolve_compatibility_stream_async(
                            session,
                            manager.workspace_id,
                            "memory",
                            "memories",
                            42,
                        ),
                    )
                    self.assertIsNone(
                        await resolve_compatibility_stream_async(
                            session,
                            manager.workspace_id,
                            "memory",
                            "memories",
                            43,
                        )
                    )
            finally:
                await manager.close()


if __name__ == "__main__":
    unittest.main()
