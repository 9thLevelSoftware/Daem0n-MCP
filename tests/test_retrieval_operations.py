"""Operator-facing projection rebuild and status contracts."""

from __future__ import annotations

import sqlite3
import unittest

from tests.test_retrieval_lexical import WORKSPACE_ID, _apply_migration


class RetrievalProjectionOperationTests(unittest.TestCase):
    def setUp(self) -> None:
        from daem0nmcp.event_store import EventCommand, EventStore

        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        for version in (16, 17, 18):
            _apply_migration(self.connection, version)
        EventStore(self.connection).append_and_project(
            EventCommand(
                workspace_id=WORKSPACE_ID,
                stream_id="mem_" + "f" * 64,
                stream_kind="memory",
                event_type="memory.created",
                occurred_at_us=100,
                recorded_at_us=100,
                actor_type="system",
                payload={
                    "record": {
                        "record_type": "decision",
                        "legacy_type": None,
                        "content": "operator projection fixture",
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
                },
            )
        )
        self.connection.commit()

    def tearDown(self) -> None:
        self.connection.close()

    def test_dry_run_reports_delta_and_writes_nothing(self):
        from daem0nmcp.retrieval.operations import rebuild_projection

        before = self.connection.total_changes
        result = rebuild_projection(
            self.connection,
            workspace_id=WORKSPACE_ID,
            projection="lexical",
            dry_run=True,
        )

        self.assertEqual("dry_run", result["status"])
        self.assertEqual("ready", result["capability_status"])
        self.assertEqual(1, result["row_count_delta"])
        self.assertIsNotNone(result["source_high_water_event_id"])
        self.assertEqual(100, result["source_high_water_recorded_at_us"])
        self.assertIsNone(result["active_generation"])
        self.assertEqual(before, self.connection.total_changes)
        self.assertEqual(
            0,
            self.connection.execute(
                "SELECT COUNT(*) FROM projection_manifests"
            ).fetchone()[0],
        )

    def test_rebuild_and_status_return_redacted_projection_state(self):
        from daem0nmcp.retrieval.operations import (
            projection_status,
            rebuild_projection,
        )

        rebuilt = rebuild_projection(
            self.connection,
            workspace_id=WORKSPACE_ID,
            projection="lexical",
        )
        status = projection_status(self.connection, WORKSPACE_ID)

        self.assertEqual("active", rebuilt["status"])
        self.assertEqual(1, rebuilt["generation"])
        self.assertEqual(WORKSPACE_ID, status["workspace_id"])
        self.assertEqual(
            [
                {
                    "active": True,
                    "build_config_hash": rebuilt["build_config_hash"],
                    "generation": 1,
                    "projection": "lexical",
                    "rebuild_required": False,
                    "row_count": 1,
                    "status": "active",
                }
            ],
            status["manifests"],
        )
        self.assertEqual(
            [
                {
                    "attempts": 0,
                    "max_attempts": 3,
                    "projection_names": ["lexical"],
                    "status": "queued",
                }
            ],
            status["jobs"],
        )
        self.assertNotIn("path", str(status).casefold())

    def test_unimplemented_optional_builder_fails_with_owned_code(self):
        from daem0nmcp.retrieval.operations import (
            ProjectionOperationError,
            rebuild_projection,
        )

        with self.assertRaisesRegex(
            ProjectionOperationError, "PROJECTION_BUILDER_UNAVAILABLE"
        ):
            rebuild_projection(
                self.connection,
                workspace_id=WORKSPACE_ID,
                projection="dense",
            )


if __name__ == "__main__":
    unittest.main()
