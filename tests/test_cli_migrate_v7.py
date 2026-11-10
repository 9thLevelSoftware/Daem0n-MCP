"""Dependency-free CLI coverage for the separate architecture migration."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_migrate_v7 import _create_legacy_database

EXPECTED_KEYS = {
    "status",
    "action",
    "workspace_id",
    "source_format",
    "target_format",
    "migration_run_id",
    "active_generation",
    "inventory",
    "checkpoints",
    "validation",
    "warnings",
    "error",
}


class MigrateV7CliTests(unittest.TestCase):
    def _run(self, root: Path, *arguments: str):
        repository = Path(__file__).resolve().parents[1]
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "daem0nmcp.cli",
                "--json",
                "--project-path",
                str(root),
                "migrate-v7",
                *arguments,
            ],
            cwd=repository,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )

    def _fixture(self, raw: str):
        root = Path(raw)
        storage = root / ".daem0nmcp" / "storage"
        storage.mkdir(parents=True)
        connection = _create_legacy_database(storage / "daem0nmcp.db")
        connection.close()
        return root, storage

    def test_default_dry_run_and_apply_emit_stable_redacted_json(self):
        with tempfile.TemporaryDirectory() as raw:
            root, storage = self._fixture(raw)
            dry = self._run(root)
            self.assertEqual(0, dry.returncode, dry.stderr)
            payload = json.loads(dry.stdout)
            self.assertEqual(EXPECTED_KEYS, set(payload))
            self.assertEqual("dry_run", payload["status"])
            self.assertEqual("migrate", payload["action"])
            self.assertNotIn(str(root), dry.stdout)
            self.assertFalse((storage / "active-db.json").exists())

            applied = self._run(root, "--apply", "--batch-size", "1")
            self.assertEqual(0, applied.returncode, applied.stderr)
            applied_payload = json.loads(applied.stdout)
            self.assertEqual(EXPECTED_KEYS, set(applied_payload))
            self.assertEqual("activated", applied_payload["status"])
            self.assertNotIn("Use SQLite", applied.stdout)
            self.assertNotIn(str(root), applied.stdout)

    def test_option_conflicts_and_batch_bounds_exit_two(self):
        with tempfile.TemporaryDirectory() as raw:
            root, _ = self._fixture(raw)
            conflict = self._run(root, "--apply", "--rollback")
            self.assertEqual(2, conflict.returncode)
            too_small = self._run(root, "--apply", "--batch-size", "0")
            self.assertEqual(2, too_small.returncode)
            too_large = self._run(root, "--apply", "--batch-size", "10001")
            self.assertEqual(2, too_large.returncode)

    def test_unsafe_migration_path_is_a_redacted_exit_two(self):
        with tempfile.TemporaryDirectory() as raw:
            root, storage = self._fixture(raw)
            (storage / "migrations").write_text("untrusted", encoding="utf-8")
            result = self._run(root, "--apply")
            self.assertEqual(2, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual("UNSAFE_MIGRATION_PATH", payload["error"]["code"])
            self.assertEqual("UNSAFE_MIGRATION_PATH", payload["error"]["message"])
            self.assertNotIn(str(root), result.stdout)

    def test_active_runtime_lock_is_a_redacted_exit_two(self):
        from daem0nmcp.storage_activation import DatabaseFileLock

        with tempfile.TemporaryDirectory() as raw:
            root, storage = self._fixture(raw)
            with DatabaseFileLock(storage, "shared"):
                result = self._run(root, "--apply")
            self.assertEqual(2, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual("DATABASE_IN_USE", payload["error"]["code"])
            self.assertNotIn(str(root), result.stdout)

    def test_rollback_latest_is_successful_and_idempotent(self):
        with tempfile.TemporaryDirectory() as raw:
            root, _ = self._fixture(raw)
            self.assertEqual(0, self._run(root, "--apply").returncode)
            rolled = self._run(root, "--rollback")
            self.assertEqual(0, rolled.returncode, rolled.stderr)
            self.assertEqual("rolled_back", json.loads(rolled.stdout)["status"])
            repeated = self._run(root, "--rollback")
            self.assertEqual(0, repeated.returncode, repeated.stderr)
            self.assertEqual(
                "already_rolled_back", json.loads(repeated.stdout)["action"]
            )

    def test_apply_resumes_an_interrupted_candidate_and_exits_zero(self):
        from daem0nmcp.migrations.v7 import MigrationInterrupted, MigrationV7Service
        from daem0nmcp.workspace import WorkspaceRegistry

        with tempfile.TemporaryDirectory() as raw:
            root, _ = self._fixture(raw)

            def interrupt(stage, details):
                if stage == "after_batch" and details["source_table"] == "memories":
                    raise MigrationInterrupted("cli resume fixture")

            registry = WorkspaceRegistry([root], default_root=root)
            with self.assertRaises(MigrationInterrupted):
                MigrationV7Service(registry, fault_injector=interrupt).apply(
                    root, batch_size=1
                )
            resumed = self._run(root, "--apply", "--batch-size", "1")
            self.assertEqual(0, resumed.returncode, resumed.stderr)
            payload = json.loads(resumed.stdout)
            self.assertEqual("activated", payload["status"])
            self.assertEqual("resume", payload["action"])


if __name__ == "__main__":
    unittest.main()
