from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


class ConsolidationRecoveryCliTests(unittest.TestCase):
    def _fixture(self, raw: str) -> Path:
        from daem0nmcp.migrations.schema import MIGRATIONS
        from daem0nmcp.schema_version import CURRENT_SCHEMA_VERSION
        from daem0nmcp.storage_activation import (
            ActiveDatabasePointer,
            write_active_pointer,
        )

        root = Path(raw).resolve()
        storage = root / ".daem0nmcp" / "storage"
        storage.mkdir(parents=True)
        database = storage / "daem0nmcp.db"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute(
                "CREATE TABLE schema_version(version INTEGER PRIMARY KEY)"
            )
            for version, _description, statements in MIGRATIONS:
                if 16 <= version <= CURRENT_SCHEMA_VERSION:
                    for statement in statements:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_version VALUES(?)", (version,)
                    )
            connection.commit()
        write_active_pointer(
            storage, ActiveDatabasePointer(7, 1, database.name, None, None)
        )
        return root

    def _run(self, root: Path, *extra: str) -> subprocess.CompletedProcess[str]:
        repository = Path(__file__).resolve().parents[1]
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "daem0nmcp.cli",
                "--json",
                "recover-consolidation",
                "--project-path",
                str(root),
                *extra,
            ],
            cwd=repository,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )

    def test_empty_recovery_is_successful_and_path_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = self._fixture(raw)
            result = self._run(root)
            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual("recovered", payload["status"])
            self.assertEqual([], payload["runs"])
            self.assertNotIn(str(root), result.stdout)

    def test_unknown_run_is_a_stable_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = self._fixture(raw)
            result = self._run(root, "--run-id", "con_" + "a" * 64)
            self.assertEqual(1, result.returncode, result.stderr)
            self.assertEqual("NOT_FOUND", json.loads(result.stdout)["error"]["code"])


if __name__ == "__main__":
    unittest.main()
