"""Dependency-free retrieval projection operator CLI coverage."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_migrate_v7 import _create_legacy_database


class RetrievalProjectionCliTests(unittest.TestCase):
    def _run(
        self,
        root: Path,
        command: str,
        *arguments: str,
        environment: dict[str, str] | None = None,
    ):
        repository = Path(__file__).resolve().parents[1]
        process_environment = os.environ.copy()
        if environment:
            process_environment.update(environment)
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "daem0nmcp.cli",
                "--json",
                "--project-path",
                str(root),
                command,
                *arguments,
            ],
            cwd=repository,
            env=process_environment,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )

    def _migrated_fixture(self, raw: str) -> tuple[Path, str]:
        root = Path(raw)
        storage = root / ".daem0nmcp" / "storage"
        storage.mkdir(parents=True)
        connection = _create_legacy_database(storage / "daem0nmcp.db")
        connection.close()
        applied = self._run(root, "migrate-v7", "--apply")
        self.assertEqual(0, applied.returncode, applied.stderr)
        return root, json.loads(applied.stdout)["workspace_id"]

    def test_projection_status_and_lexical_dry_run_are_redacted(self):
        with tempfile.TemporaryDirectory() as raw:
            root, workspace_id = self._migrated_fixture(raw)

            status = self._run(
                root,
                "projection-status",
                "--workspace-id",
                workspace_id,
            )
            self.assertEqual(0, status.returncode, status.stderr)
            status_payload = json.loads(status.stdout)
            self.assertEqual(workspace_id, status_payload["workspace_id"])
            lexical = [
                item
                for item in status_payload["manifests"]
                if item["projection"] == "lexical"
            ]
            self.assertEqual(1, len(lexical))
            self.assertTrue(lexical[0]["active"])
            self.assertNotIn(str(root), status.stdout)

            dry_run = self._run(
                root,
                "rebuild-projection",
                "--projection",
                "lexical",
                "--workspace-id",
                workspace_id,
                "--dry-run",
            )
            self.assertEqual(0, dry_run.returncode, dry_run.stderr)
            dry_payload = json.loads(dry_run.stdout)
            self.assertEqual("dry_run", dry_payload["status"])
            self.assertEqual("ready", dry_payload["capability_status"])
            self.assertEqual(1, dry_payload["active_generation"])
            self.assertNotIn(str(root), dry_run.stdout)

    def test_unknown_projection_is_rejected_by_the_parser(self):
        with tempfile.TemporaryDirectory() as raw:
            root, workspace_id = self._migrated_fixture(raw)
            result = self._run(
                root,
                "rebuild-projection",
                "--projection",
                "unknown",
                "--workspace-id",
                workspace_id,
            )
            self.assertEqual(2, result.returncode)

    def test_procedure_dry_run_uses_the_real_optional_builder(self):
        """The advertised optional command must not stop at its parser facade."""

        with tempfile.TemporaryDirectory() as raw:
            root, workspace_id = self._migrated_fixture(raw)

            result = self._run(
                root,
                "rebuild-projection",
                "--projection",
                "procedure",
                "--workspace-id",
                workspace_id,
                "--dry-run",
            )

            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual("procedure", payload["projection_name"])
            self.assertEqual("dry_run", payload["status"])
            self.assertEqual("ready", payload["capability_status"])

    def test_registered_secondary_workspace_opens_its_own_active_database(self):
        """Authorization of workspace B must never query workspace B inside DB A."""

        with (
            tempfile.TemporaryDirectory() as first_raw,
            tempfile.TemporaryDirectory() as second_raw,
        ):
            first_root, _first_id = self._migrated_fixture(first_raw)
            second_root, second_id = self._migrated_fixture(second_raw)

            result = self._run(
                first_root,
                "projection-status",
                "--workspace-id",
                second_id,
                environment={
                    "DAEM0NMCP_WORKSPACE_ROOTS": json.dumps([str(second_root)])
                },
            )

            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            lexical = [
                manifest
                for manifest in payload["manifests"]
                if manifest["projection"] == "lexical" and manifest["active"]
            ]
            self.assertEqual(1, len(lexical))
            self.assertEqual(second_id, payload["workspace_id"])

    def test_compact_projections_drains_superseded_generations_offline(self):
        """Databases bloated before the GC existed need a one-shot drain."""

        import sqlite3

        from daem0nmcp.retrieval.projections import LexicalProjectionBuilder
        from daem0nmcp.storage_activation import resolve_active_database

        with tempfile.TemporaryDirectory() as raw:
            root, workspace_id = self._migrated_fixture(raw)
            database = resolve_active_database(root / ".daem0nmcp" / "storage").path
            connection = sqlite3.connect(database)
            try:
                connection.execute("PRAGMA foreign_keys=ON")
                with patch(
                    "daem0nmcp.retrieval.projections.collect_superseded_generations",
                    return_value=0,
                ):
                    for _ in range(9):
                        LexicalProjectionBuilder(connection).rebuild(workspace_id)
                superseded = connection.execute(
                    "SELECT COUNT(*) FROM projection_manifests "
                    "WHERE projection_name='lexical' AND status<>'active'"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(9, superseded)

            result = self._run(
                root,
                "compact-projections",
                "--workspace-id",
                workspace_id,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(8, payload["collected"]["lexical"])
            self.assertLess(
                payload["superseded_manifests_after"],
                payload["superseded_manifests_before"],
            )
            self.assertTrue(payload["vacuumed"])
            self.assertNotIn(str(root), result.stdout)
            connection = sqlite3.connect(database)
            try:
                remaining = connection.execute(
                    "SELECT COUNT(*) FROM projection_manifests "
                    "WHERE projection_name='lexical'"
                ).fetchone()[0]
                tables = connection.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                    "AND name GLOB 'retrieval_fts_*_g*' "
                    "AND sql GLOB 'CREATE VIRTUAL TABLE*'"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(2, remaining)
            self.assertEqual(2, tables)


if __name__ == "__main__":
    unittest.main()
