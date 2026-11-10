"""Dependency-free executable schema contract for additive SQL migration 16."""

from __future__ import annotations

import ast
import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path


def _load_schema_module():
    schema_path = (
        Path(__file__).resolve().parents[1] / "daem0nmcp" / "migrations" / "schema.py"
    )
    spec = importlib.util.spec_from_file_location(
        "task7_schema_under_test", schema_path
    )
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load the real migration schema module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SCHEMA_MODULE = _load_schema_module()
MIGRATIONS = SCHEMA_MODULE.MIGRATIONS


V7_TABLES = {
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


class Migration16SchemaTests(unittest.TestCase):
    """Catch an absent or incomplete additive v7 storage schema."""

    def test_migration_16_creates_exact_v7_tables_and_immutable_event_triggers(
        self,
    ) -> None:
        """Dropping a required table or event immutability trigger must fail."""
        migration = next((item for item in MIGRATIONS if item[0] == 16), None)
        self.assertIsNotNone(migration, "additive SQL migration 16 is missing")
        assert migration is not None

        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            for statement in migration[2]:
                connection.execute(statement)
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            triggers = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                )
            }
            self.assertEqual(V7_TABLES, tables)
            self.assertTrue(
                {"memory_events_no_update", "memory_events_no_delete"} <= triggers
            )
        finally:
            connection.close()

    def test_models_declare_all_ten_typed_v7_tables(self) -> None:
        """ORM metadata must mirror the executable SQLite migration."""
        models_path = Path(__file__).resolve().parents[1] / "daem0nmcp" / "models.py"
        tree = ast.parse(models_path.read_text(encoding="utf-8"))
        declared = {}
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            for statement in node.body:
                if (
                    isinstance(statement, ast.Assign)
                    and any(
                        isinstance(target, ast.Name) and target.id == "__tablename__"
                        for target in statement.targets
                    )
                    and isinstance(statement.value, ast.Constant)
                ):
                    declared[statement.value.value] = node.name
        expected = {
            "memory_events": "MemoryEvent",
            "memory_records": "MemoryRecord",
            "memory_fact_versions": "MemoryFactVersion",
            "memory_relationship_versions": "MemoryRelationshipVersion",
            "projection_manifests": "ProjectionManifest",
            "enrichment_decisions": "EnrichmentDecision",
            "background_jobs": "BackgroundJob",
            "v7_migration_runs": "V7MigrationRun",
            "v7_migration_checkpoints": "V7MigrationCheckpoint",
            "legacy_id_map": "LegacyIdMap",
        }
        self.assertEqual({name: declared.get(name) for name in expected}, expected)

    def test_dense_projection_ref_declares_nullable_vector_attestations(self) -> None:
        """ORM metadata must expose the nullable TEXT columns added by migration 31."""
        from sqlalchemy.dialects import sqlite
        from sqlalchemy.schema import CreateTable

        from daem0nmcp.models import DenseProjectionRef

        columns = DenseProjectionRef.__table__.columns
        for name in ("vector_format", "vector_sha256"):
            with self.subTest(name=name):
                self.assertEqual(columns[name].type.__class__.__name__, "Text")
                self.assertTrue(columns[name].nullable)
        ddl = str(
            CreateTable(DenseProjectionRef.__table__).compile(dialect=sqlite.dialect())
        )
        self.assertIn(
            "CONSTRAINT ck_dense_refs_vector_format CHECK "
            "(vector_format IS NULL OR vector_format='qdrant-cosine-f32-le-v1')",
            ddl,
        )
        self.assertIn(
            "CONSTRAINT ck_dense_refs_vector_sha256 CHECK "
            "(vector_sha256 IS NULL OR (length(vector_sha256)=64 AND "
            "vector_sha256 NOT GLOB '*[^0-9a-f]*'))",
            ddl,
        )

    def test_orm_declares_exact_id_and_relationship_constraints_from_migration_16(self):
        """Fresh create_all metadata must not be weaker than upgraded databases."""
        models_path = Path(__file__).resolve().parents[1] / "daem0nmcp" / "models.py"
        source = models_path.read_text(encoding="utf-8")
        for expression in (
            "length(fact_version_id)=69",
            "length(fact_id)=69",
            "length(relationship_version_id)=68",
            "length(relationship_id)=68",
            "length(manifest_id)=68",
            "length(decision_id)=68",
            "length(job_id)=68",
            "length(migration_run_id)=68",
            "'evidence_for','derived_from','invalidates'",
        ):
            with self.subTest(expression=expression):
                self.assertIn(expression, source)
        for name in (
            "ck_memory_events_payload_json",
            "uq_memory_records_workspace_id",
            "ck_fact_versions_id",
            "ck_fact_versions_object_json",
            "ck_relationship_versions_id",
        ):
            with self.subTest(name=name):
                self.assertIn(f'name="{name}"', source)

    def test_sync_migrations_enable_foreign_keys_and_upgrade_script_uses_current_schema(
        self,
    ) -> None:
        """Offline candidate/schema paths cannot silently run with FK checks off."""
        root = Path(__file__).resolve().parents[1]
        schema_source = (root / "daem0nmcp" / "migrations" / "schema.py").read_text(
            encoding="utf-8"
        )
        upgrade_source = (root / "scripts" / "upgrade.py").read_text(encoding="utf-8")
        self.assertIn('conn.execute("PRAGMA foreign_keys=ON")', schema_source)
        self.assertIn(
            "from daem0nmcp.schema_version import CURRENT_SCHEMA_VERSION",
            upgrade_source,
        )
        self.assertIn('"active-db.json"', upgrade_source)
        self.assertIn('pointer.get("format_version")', upgrade_source)

    def test_memory_events_are_immutable_and_projection_constraints_apply(self) -> None:
        """SQLite, not merely application code, enforces canonical integrity."""
        migration = next((item for item in MIGRATIONS if item[0] == 16), None)
        self.assertIsNotNone(migration, "additive SQL migration 16 is missing")
        assert migration is not None
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            for statement in migration[2]:
                connection.execute(statement)
            event_hash = "b" * 64
            event_id = "evt_" + event_hash
            connection.execute(
                """
                INSERT INTO memory_events (
                    event_id, workspace_id, stream_id, stream_kind, stream_version,
                    event_type, event_schema_version, occurred_at_us, recorded_at_us,
                    actor_type, payload_json, payload_hash, event_hash
                ) VALUES (?, ?, ?, 'memory', 1, 'memory.created', 1, 1, 1,
                          'system', '{}', ?, ?)
                """,
                (event_id, "ws_fixture", "mem_" + "c" * 64, "a" * 64, event_hash),
            )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "IMMUTABLE_MEMORY_EVENT"
            ):
                connection.execute(
                    "UPDATE memory_events SET event_type='memory.changed' WHERE event_id=?",
                    (event_id,),
                )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "IMMUTABLE_MEMORY_EVENT"
            ):
                connection.execute(
                    "DELETE FROM memory_events WHERE event_id=?", (event_id,)
                )
            connection.execute("PRAGMA recursive_triggers=OFF")
            original = connection.execute(
                "SELECT * FROM memory_events WHERE event_id=?", (event_id,)
            ).fetchone()
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "IMMUTABLE_MEMORY_EVENT"
            ):
                connection.execute(
                    """
                    INSERT OR REPLACE INTO memory_events (
                        event_id, workspace_id, stream_id, stream_kind,
                        stream_version, event_type, event_schema_version,
                        occurred_at_us, recorded_at_us, actor_type, payload_json,
                        payload_hash, event_hash
                    ) VALUES (?, 'ws_fixture', ?, 'memory', 1,
                              'memory.replaced', 1, 2, 2, 'system', '{}', ?, ?)
                    """,
                    (event_id, "mem_" + "c" * 64, "1" * 64, "2" * 64),
                )
            self.assertEqual(
                original,
                connection.execute(
                    "SELECT * FROM memory_events WHERE event_id=?", (event_id,)
                ).fetchone(),
            )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "IMMUTABLE_MEMORY_EVENT"
            ):
                connection.execute(
                    """
                    INSERT OR REPLACE INTO memory_events (
                        event_id, workspace_id, stream_id, stream_kind,
                        stream_version, event_type, event_schema_version,
                        occurred_at_us, recorded_at_us, actor_type, payload_json,
                        payload_hash, event_hash
                    ) VALUES (?, 'ws_fixture', ?, 'memory', 1,
                              'memory.replaced', 1, 2, 2, 'system', '{}', ?, ?)
                    """,
                    (
                        "evt_" + "3" * 64,
                        "mem_" + "c" * 64,
                        "4" * 64,
                        "3" * 64,
                    ),
                )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "IMMUTABLE_MEMORY_EVENT"
            ):
                connection.execute(
                    """
                    INSERT OR REPLACE INTO memory_events (
                        event_id, workspace_id, stream_id, stream_kind,
                        stream_version, event_type, event_schema_version,
                        occurred_at_us, recorded_at_us, actor_type, payload_json,
                        payload_hash, event_hash
                    ) VALUES (?, 'ws_fixture', ?, 'memory', 1,
                              'memory.replaced', 1, 2, 2, 'system', '{}', ?, ?)
                    """,
                    (
                        "evt_" + "5" * 64,
                        "mem_" + "d" * 64,
                        "6" * 64,
                        event_hash,
                    ),
                )
            original_rowid = connection.execute(
                "SELECT rowid FROM memory_events WHERE event_id=?", (event_id,)
            ).fetchone()[0]
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "IMMUTABLE_MEMORY_EVENT"
            ):
                connection.execute(
                    """
                    INSERT OR REPLACE INTO memory_events (
                        rowid, event_id, workspace_id, stream_id, stream_kind,
                        stream_version, event_type, event_schema_version,
                        occurred_at_us, recorded_at_us, actor_type, payload_json,
                        payload_hash, event_hash
                    ) VALUES (?, ?, 'ws_fixture', ?, 'memory', 1,
                              'memory.replaced', 1, 2, 2, 'system', '{}', ?, ?)
                    """,
                    (
                        original_rowid,
                        "evt_" + "7" * 64,
                        "mem_" + "8" * 64,
                        "9" * 64,
                        "7" * 64,
                    ),
                )
            self.assertEqual(
                1,
                connection.execute("SELECT count(*) FROM memory_events").fetchone()[0],
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO memory_records (
                        record_id, workspace_id, record_type, legacy_type, content,
                        content_hash, context_json, tags_json, source_event_id,
                        stream_version, created_at_us, updated_at_us, state_hash
                    ) VALUES (?, 'ws_fixture', 'decision', 'not-allowed', 'x', ?,
                              '{}', '[]', ?, 1, 1, 1, ?)
                    """,
                    ("mem_" + "d" * 64, "e" * 64, event_id, "f" * 64),
                )
        finally:
            connection.close()

    def test_migration_17_upgrades_a_pre_fix_version_16_event_table(self) -> None:
        """Already-v16 databases must receive rowid-safe replacement protection."""
        migration_16 = next(item for item in MIGRATIONS if item[0] == 16)
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "pre-fix-v16.db"
            connection = sqlite3.connect(path)
            connection.execute(
                "CREATE TABLE schema_version(version INTEGER PRIMARY KEY, applied_at TEXT)"
            )
            connection.execute("INSERT INTO schema_version(version) VALUES (16)")
            for statement in migration_16[2]:
                if "memory_events_no_replace" not in statement:
                    connection.execute(statement)
            connection.commit()
            connection.close()

            count, _applied = SCHEMA_MODULE.run_migrations(str(path))

            self.assertEqual(SCHEMA_MODULE.CURRENT_SCHEMA_VERSION - 16, count)
            connection = sqlite3.connect(path)
            try:
                self.assertEqual(
                    SCHEMA_MODULE.CURRENT_SCHEMA_VERSION,
                    connection.execute(
                        "SELECT max(version) FROM schema_version"
                    ).fetchone()[0],
                )
                trigger = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' "
                    "AND name='memory_events_no_replace'"
                ).fetchone()
                self.assertIsNotNone(trigger)
                event_hash = "b" * 64
                event_id = "evt_" + event_hash
                connection.execute(
                    """
                    INSERT INTO memory_events (
                        rowid, event_id, workspace_id, stream_id, stream_kind,
                        stream_version, event_type, event_schema_version,
                        occurred_at_us, recorded_at_us, actor_type, payload_json,
                        payload_hash, event_hash
                    ) VALUES (41, ?, 'ws_fixture', ?, 'memory', 1,
                              'memory.created', 1, 1, 1, 'system', '{}', ?, ?)
                    """,
                    (event_id, "mem_" + "c" * 64, "a" * 64, event_hash),
                )
                connection.execute("PRAGMA recursive_triggers=OFF")
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError, "IMMUTABLE_MEMORY_EVENT"
                ):
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO memory_events (
                            rowid, event_id, workspace_id, stream_id, stream_kind,
                            stream_version, event_type, event_schema_version,
                            occurred_at_us, recorded_at_us, actor_type,
                            payload_json, payload_hash, event_hash
                        ) VALUES (41, ?, 'ws_fixture', ?, 'memory', 1,
                                  'memory.replaced', 1, 2, 2, 'system', '{}', ?, ?)
                        """,
                        (
                            "evt_" + "d" * 64,
                            "mem_" + "e" * 64,
                            "f" * 64,
                            "d" * 64,
                        ),
                    )
                self.assertEqual(
                    event_id,
                    connection.execute(
                        "SELECT event_id FROM memory_events WHERE rowid=41"
                    ).fetchone()[0],
                )
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
