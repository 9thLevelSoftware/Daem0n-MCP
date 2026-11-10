from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

WORKSPACE_A = "ws_0123456789abcdef01234567"
WORKSPACE_B = "ws_89abcdef0123456701234567"
RECORD_A = "mem_" + "a" * 64
ACTIVE_A = "act_" + "b" * 64


def _migration_20() -> tuple[str, list[str]]:
    from daem0nmcp.migrations.schema import MIGRATIONS

    migration = next((item for item in MIGRATIONS if item[0] == 20), None)
    if migration is None:
        raise AssertionError("canonical active-context migration 20 is missing")
    return migration[1], migration[2]


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(
        """
        CREATE TABLE memory_records (
            record_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            CONSTRAINT uq_memory_records_workspace_id
                UNIQUE(workspace_id, record_id)
        );
        """
    )
    for statement in _migration_20()[1]:
        connection.execute(statement)
    return connection


class CanonicalActiveContextMigrationTests(unittest.TestCase):
    def test_canonical_id_is_stable_for_one_workspace_record_binding(self) -> None:
        from daem0nmcp.api.v7.active_context_storage import (
            active_context_id_for_record,
        )

        encoded = json.dumps(
            [
                "daem0nmcp",
                "v7",
                "active-context-entry",
                WORKSPACE_A,
                RECORD_A,
            ],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        expected = "act_" + hashlib.sha256(encoded).hexdigest()
        self.assertEqual(active_context_id_for_record(WORKSPACE_A, RECORD_A), expected)
        self.assertEqual(active_context_id_for_record(WORKSPACE_A, RECORD_A), expected)
        for workspace_id, record_id in (
            ("bad", RECORD_A),
            (WORKSPACE_A, "mem_bad"),
            (WORKSPACE_A, "mem_" + "A" * 64),
        ):
            with (
                self.subTest(workspace_id=workspace_id, record_id=record_id),
                self.assertRaises(ValueError),
            ):
                active_context_id_for_record(workspace_id, record_id)

    def test_resource_repository_imports_in_a_fresh_process(self) -> None:
        # Catches storage activation importing the eager migrations package and
        # cycling back into a partially initialized activation module.
        result = subprocess.run(
            [
                sys.executable,
                "-W",
                "error",
                "-c",
                "import daem0nmcp.api.v7.resource_repository",
            ],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_current_schema_requires_migration_20_without_redefining_format_7(
        self,
    ) -> None:
        from daem0nmcp.migrations.schema import CURRENT_SCHEMA_VERSION, MIGRATIONS
        from daem0nmcp.migrations.v7 import _V7_TABLE_NAMES
        from daem0nmcp.storage_activation import (
            _V7_FORMAT_MIN_SCHEMA_VERSION,
            _V7_FORMAT_TABLES,
        )

        migration_versions = [migration[0] for migration in MIGRATIONS]
        self.assertGreaterEqual(CURRENT_SCHEMA_VERSION, 20)
        self.assertIn(20, migration_versions)
        self.assertEqual(sorted(migration_versions), migration_versions)
        self.assertEqual(CURRENT_SCHEMA_VERSION, migration_versions[-1])
        self.assertLess(migration_versions.index(19), migration_versions.index(20))
        self.assertIn("active_context_entries", _V7_TABLE_NAMES)
        self.assertEqual(18, _V7_FORMAT_MIN_SCHEMA_VERSION)
        self.assertNotIn("active_context_entries", _V7_FORMAT_TABLES)
        self.assertNotIn("public_object_ids", _V7_FORMAT_TABLES)

        database_source = (
            Path(__file__).resolve().parents[2] / "daem0nmcp" / "database.py"
        ).read_text(encoding="utf-8")
        migration_source = (
            Path(__file__).resolve().parents[2] / "daem0nmcp" / "migrations" / "v7.py"
        ).read_text(encoding="utf-8")
        activation_source = (
            Path(__file__).resolve().parents[2] / "daem0nmcp" / "storage_activation.py"
        ).read_text(encoding="utf-8")
        self.assertIn("< CURRENT_SCHEMA_VERSION", database_source)
        self.assertIn("< CURRENT_SCHEMA_VERSION", migration_source)
        self.assertIn("version < _V7_FORMAT_MIN_SCHEMA_VERSION", activation_source)
        self.assertNotIn("version < CURRENT_SCHEMA_VERSION", activation_source)

    def test_public_id_backfill_is_atomic_with_schema_publication(self) -> None:
        from daem0nmcp.migrations.schema import run_migrations

        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "candidate.db"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE schema_version(version INTEGER PRIMARY KEY);
                INSERT INTO schema_version VALUES (18);
                CREATE TABLE memory_records(
                    record_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL,
                    UNIQUE(workspace_id, record_id)
                );
                CREATE TABLE rules(id INTEGER PRIMARY KEY);
                INSERT INTO rules VALUES (0);
                CREATE TABLE active_context(id INTEGER PRIMARY KEY);
                """
            )
            connection.commit()
            connection.close()

            with self.assertRaisesRegex(RuntimeError, "PUBLIC_ID_INTEGRITY_ERROR"):
                run_migrations(str(path), workspace_id=WORKSPACE_A)

            connection = sqlite3.connect(path)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT max(version) FROM schema_version"
                    ).fetchone()[0],
                    18,
                )
                self.assertIsNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='public_object_ids'"
                    ).fetchone()
                )
            finally:
                connection.close()

    def test_retained_public_rows_require_workspace_scope_before_migration_19(
        self,
    ) -> None:
        from daem0nmcp.migrations.schema import run_migrations

        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "candidate.db"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE schema_version(version INTEGER PRIMARY KEY);
                INSERT INTO schema_version VALUES (18);
                CREATE TABLE memory_records(
                    record_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL,
                    UNIQUE(workspace_id, record_id)
                );
                CREATE TABLE rules(id INTEGER PRIMARY KEY);
                INSERT INTO rules VALUES (1);
                CREATE TABLE active_context(id INTEGER PRIMARY KEY);
                """
            )
            connection.commit()
            connection.close()

            with self.assertRaisesRegex(RuntimeError, "WORKSPACE_SCOPE_REQUIRED"):
                run_migrations(str(path))

            connection = sqlite3.connect(path)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT max(version) FROM schema_version"
                    ).fetchone()[0],
                    18,
                )
                self.assertIsNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='public_object_ids'"
                    ).fetchone()
                )
            finally:
                connection.close()

    def test_cli_and_upgrade_entrypoints_supply_workspace_scope(self) -> None:
        root = Path(__file__).resolve().parents[2]
        cli_source = (root / "daem0nmcp" / "cli.py").read_text(encoding="utf-8")
        upgrade_source = (root / "scripts" / "upgrade.py").read_text(encoding="utf-8")
        self.assertIn("db_path, workspace_id=db.workspace_id", cli_source)
        self.assertIn("WorkspaceRegistry", upgrade_source)
        self.assertIn(
            "run_migrations(\n                    fp.db_path, workspace_id=workspace_id",
            upgrade_source,
        )

    def test_migration_declares_canonical_without_rowid_table_and_guards(self) -> None:
        description, _statements = _migration_20()
        self.assertIn("active context", description.lower())
        connection = _connection()
        try:
            table_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='active_context_entries'"
            ).fetchone()[0]
            columns = {
                row[1]: (row[2], row[3], row[5])
                for row in connection.execute(
                    "PRAGMA table_info(active_context_entries)"
                )
            }
            triggers = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                )
            }
            indexes = {
                row[1]
                for row in connection.execute(
                    "PRAGMA index_list(active_context_entries)"
                )
            }
            foreign_keys = list(
                connection.execute("PRAGMA foreign_key_list(active_context_entries)")
            )

            self.assertIn("WITHOUT ROWID", table_sql.upper())
            self.assertEqual(
                set(columns),
                {
                    "active_context_id",
                    "workspace_id",
                    "record_id",
                    "priority",
                    "reason",
                    "added_at_us",
                    "expires_at_us",
                    "removed_at_us",
                },
            )
            self.assertEqual(columns["active_context_id"][2], 1)
            self.assertEqual(
                {(row[3], row[4], row[6], row[5]) for row in foreign_keys},
                {
                    ("workspace_id", "workspace_id", "RESTRICT", "RESTRICT"),
                    ("record_id", "record_id", "RESTRICT", "RESTRICT"),
                },
            )
            self.assertTrue(
                {
                    "active_context_entries_no_identity_update",
                    "active_context_entries_no_delete",
                    "active_context_entries_no_replace",
                }
                <= triggers
            )
            self.assertTrue(
                {
                    "idx_active_context_entries_current",
                    "idx_active_context_entries_expiry",
                }
                <= indexes
            )
        finally:
            connection.close()

    def test_constraints_bind_identity_to_one_workspace_record(self) -> None:
        connection = _connection()
        try:
            connection.execute(
                "INSERT INTO memory_records VALUES (?,?)", (RECORD_A, WORKSPACE_A)
            )
            connection.execute(
                "INSERT INTO active_context_entries VALUES (?,?,?,?,?,?,?,?)",
                (ACTIVE_A, WORKSPACE_A, RECORD_A, 5, "current focus", 10, 20, None),
            )

            invalid_rows = (
                ("act_bad", WORKSPACE_A, RECORD_A, 0, None, 10, None, None),
                ("act_" + "c" * 64, WORKSPACE_B, RECORD_A, 0, None, 10, None, None),
                ("act_" + "d" * 64, WORKSPACE_A, RECORD_A, 101, None, 10, None, None),
                ("act_" + "e" * 64, WORKSPACE_A, RECORD_A, 0, "", 10, None, None),
                ("act_" + "f" * 64, WORKSPACE_A, RECORD_A, 0, None, -1, None, None),
            )
            for row in invalid_rows:
                with self.subTest(row=row), self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO active_context_entries VALUES (?,?,?,?,?,?,?,?)",
                        row,
                    )

            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO active_context_entries VALUES (?,?,?,?,?,?,?,?)",
                    (
                        "act_" + "1" * 64,
                        WORKSPACE_A,
                        RECORD_A,
                        0,
                        None,
                        10,
                        None,
                        None,
                    ),
                )
        finally:
            connection.close()

    def test_identity_is_immutable_delete_is_soft_and_operational_state_is_mutable(
        self,
    ) -> None:
        connection = _connection()
        try:
            connection.execute(
                "INSERT INTO memory_records VALUES (?,?)", (RECORD_A, WORKSPACE_A)
            )
            connection.execute(
                "INSERT INTO active_context_entries VALUES (?,?,?,?,?,?,?,?)",
                (ACTIVE_A, WORKSPACE_A, RECORD_A, 0, None, 10, None, None),
            )
            connection.execute(
                "UPDATE active_context_entries SET priority=7,reason='needed',"
                "added_at_us=11,expires_at_us=30,removed_at_us=20 "
                "WHERE active_context_id=?",
                (ACTIVE_A,),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT priority,reason,added_at_us,expires_at_us,removed_at_us "
                    "FROM active_context_entries"
                ).fetchone(),
                (7, "needed", 11, 30, 20),
            )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "IMMUTABLE_ACTIVE_CONTEXT_IDENTITY"
            ):
                connection.execute(
                    "UPDATE active_context_entries SET active_context_id=?",
                    ("act_" + "2" * 64,),
                )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "SOFT_REMOVE_ACTIVE_CONTEXT"
            ):
                connection.execute("DELETE FROM active_context_entries")
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "IMMUTABLE_ACTIVE_CONTEXT_IDENTITY"
            ):
                connection.execute(
                    "INSERT OR REPLACE INTO active_context_entries "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (ACTIVE_A, WORKSPACE_A, RECORD_A, 1, None, 12, None, None),
                )
        finally:
            connection.close()

    def test_orm_metadata_declares_canonical_table_parity(self) -> None:
        models_path = Path(__file__).resolve().parents[2] / "daem0nmcp" / "models.py"
        tree = ast.parse(models_path.read_text(encoding="utf-8"))
        table_to_class: dict[str, str] = {}
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
                    table_to_class[str(statement.value.value)] = node.name
        self.assertEqual(
            table_to_class.get("active_context_entries"), "ActiveContextEntry"
        )


if __name__ == "__main__":
    unittest.main()
