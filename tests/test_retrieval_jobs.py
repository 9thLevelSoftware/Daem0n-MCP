"""Durable, coalesced retrieval projection job lifecycle contracts."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from tests.test_retrieval_lexical import WORKSPACE_ID, _apply_migration


class RetrievalProjectionJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self._database_directory = tempfile.TemporaryDirectory()
        self.database_path = (
            Path(self._database_directory.name) / "retrieval-jobs.sqlite3"
        )
        self.connection = sqlite3.connect(self.database_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        for version in (16, 17, 18):
            _apply_migration(self.connection, version)
        self.connection.commit()
        self._clock = 1_000_000

    def tearDown(self) -> None:
        self.connection.close()
        self._database_directory.cleanup()

    def _append_record(self, suffix: str, content: str) -> str:
        from daem0nmcp.event_store import EventCommand, EventStore

        record_id = "mem_" + suffix * 64
        EventStore(self.connection).append_and_project(
            EventCommand(
                workspace_id=WORKSPACE_ID,
                stream_id=record_id,
                stream_kind="memory",
                event_type="memory.created",
                occurred_at_us=self._clock,
                recorded_at_us=self._clock,
                actor_type="system",
                payload={
                    "record": {
                        "record_type": "decision",
                        "legacy_type": None,
                        "content": content,
                        "rationale": "job fixture",
                        "context": {},
                        "tags": ["jobs"],
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
        self._clock += 1
        return record_id

    def _queue_job(self) -> None:
        from daem0nmcp.retrieval.projections import LexicalProjectionBuilder

        self._append_record("a", "baseline record")
        LexicalProjectionBuilder(self.connection, clock_us=lambda: self._clock).rebuild(
            WORKSPACE_ID
        )
        self._append_record("b", "later canonical record")
        # Job-runner tests exercise claiming and leases independently from the
        # write-side rebuild coalescing grace.
        self.connection.execute(
            "UPDATE background_jobs SET available_at_us=?", (self._clock,)
        )
        self.connection.commit()

    def _runner(self, builders, **changes):
        from daem0nmcp.retrieval.jobs import ProjectionJobRunner

        return ProjectionJobRunner(
            self.connection,
            builders=builders,
            clock_us=lambda: self._clock,
            lease_owner="test-worker",
            token_factory=lambda: "lease-token",
            retry_delay_us=1,
            **changes,
        )

    def test_claim_executes_and_completes_one_coalesced_job(self):
        self._queue_job()
        calls = []

        result = self._runner(
            {"lexical": lambda workspace_id: calls.append(workspace_id)}
        ).run_once()

        self.assertIsNotNone(result)
        self.assertEqual("succeeded", result.status)
        self.assertEqual(("lexical",), result.projections)
        self.assertEqual([WORKSPACE_ID], calls)
        row = self.connection.execute(
            "SELECT status,attempts,lease_owner,lease_token,result_json "
            "FROM background_jobs"
        ).fetchone()
        self.assertEqual(("succeeded", 1, None, None), tuple(row[:4]))
        self.assertEqual(
            {"projection_names": ["lexical"], "status": "succeeded"},
            json.loads(row[4]),
        )

    def test_explicit_availability_defers_claim_until_due(self):
        from daem0nmcp.retrieval.job_queue import enqueue_projection_rebuild

        self._append_record("d", "deferred availability fixture")
        source_event_id = self.connection.execute(
            "SELECT source_event_id FROM memory_records WHERE workspace_id=?",
            (WORKSPACE_ID,),
        ).fetchone()[0]
        enqueue_projection_rebuild(
            self.connection,
            workspace_id=WORKSPACE_ID,
            projection_name="lexical",
            source_event_id=source_event_id,
            recorded_at_us=self._clock,
            available_at_us=self._clock + 50,
        )
        self.connection.commit()
        calls: list[str] = []
        runner = self._runner(
            {"lexical": lambda workspace_id: calls.append(workspace_id)}
        )

        self.assertIsNone(runner.run_once())
        self.assertEqual([], calls)
        self._clock += 50
        result = runner.run_once()

        self.assertIsNotNone(result)
        self.assertEqual("succeeded", result.status)
        self.assertEqual([WORKSPACE_ID], calls)

    def test_availability_must_not_predate_recording(self):
        from daem0nmcp.retrieval.job_queue import enqueue_projection_rebuild

        with self.assertRaisesRegex(ValueError, "available_at_us is invalid"):
            enqueue_projection_rebuild(
                self.connection,
                workspace_id=WORKSPACE_ID,
                projection_name="lexical",
                source_event_id=None,
                recorded_at_us=self._clock,
                available_at_us=self._clock - 1,
            )

    def test_failure_retries_then_dead_letters_without_exception_text(self):
        self._queue_job()
        self.connection.execute("UPDATE background_jobs SET max_attempts=2")
        self.connection.commit()

        def fail(_workspace_id):
            raise RuntimeError("secret path D:/private/workspace")

        first = self._runner({"lexical": fail}).run_once()
        self.assertEqual("queued", first.status)
        self._clock += 1
        second = self._runner({"lexical": fail}).run_once()
        self.assertEqual("dead_letter", second.status)
        row = self.connection.execute(
            "SELECT status,attempts,last_error_json,lease_owner FROM background_jobs"
        ).fetchone()
        self.assertEqual(("dead_letter", 2, None), (row[0], row[1], row[3]))
        self.assertEqual({"code": "PROJECTION_REBUILD_FAILED"}, json.loads(row[2]))
        self.assertNotIn("private", row[2])

    def test_shutdown_cancellation_requeues_without_consuming_retry_attempt(self):
        self._queue_job()
        calls: list[str] = []

        result = self._runner(
            {"lexical": lambda workspace_id: calls.append(workspace_id)},
            cancelled=lambda: True,
        ).run_once()

        self.assertEqual("queued", result.status)
        self.assertEqual("PROJECTION_CANCELLED", result.reason)
        self.assertEqual([], calls)
        row = self.connection.execute(
            "SELECT status,attempts,lease_owner,lease_token,last_error_json "
            "FROM background_jobs"
        ).fetchone()
        self.assertEqual(("queued", 0, None, None), tuple(row[:4]))
        self.assertEqual({"code": "PROJECTION_CANCELLED"}, json.loads(row[4]))

    def test_unavailable_optional_projection_dead_letters_without_retrying(self):
        from daem0nmcp.retrieval.dense_projection import DenseProjectionBuildError

        self._queue_job()
        payload = {
            "projection_names": ["dense"],
            "source_event_id": self.connection.execute(
                "SELECT source_event_id FROM background_jobs"
            ).fetchone()[0],
            "workspace_id": WORKSPACE_ID,
        }
        from daem0nmcp.event_store import canonical_json_bytes

        payload_json = canonical_json_bytes(payload).decode("utf-8")
        import hashlib

        self.connection.execute(
            "UPDATE background_jobs SET payload_json=?,payload_hash=?",
            (payload_json, hashlib.sha256(payload_json.encode()).hexdigest()),
        )
        self.connection.commit()

        def unavailable(_workspace_id):
            raise DenseProjectionBuildError(
                "DENSE_UNAVAILABLE", "must not expose provider detail"
            )

        result = self._runner({"dense": unavailable}).run_once()

        self.assertEqual("dead_letter", result.status)
        self.assertEqual("DENSE_UNAVAILABLE", result.reason)
        row = self.connection.execute(
            "SELECT status,attempts,last_error_json FROM background_jobs"
        ).fetchone()
        self.assertEqual(("dead_letter", 1), tuple(row[:2]))
        self.assertEqual({"code": "DENSE_UNAVAILABLE"}, json.loads(row[2]))
        self.assertNotIn("provider detail", row[2])

    def test_expired_lease_is_reclaimed(self):
        self._queue_job()
        self.connection.execute(
            "UPDATE background_jobs SET status='running', attempts=1, "
            "lease_owner='dead-worker', lease_token='dead-token', "
            "lease_expires_at_us=?",
            (self._clock - 1,),
        )
        self.connection.commit()

        result = self._runner({"lexical": lambda _workspace_id: None}).run_once()

        self.assertEqual("succeeded", result.status)
        self.assertEqual(
            ("succeeded", 2, None),
            tuple(
                self.connection.execute(
                    "SELECT status,attempts,lease_owner FROM background_jobs"
                ).fetchone()
            ),
        )

    def test_running_build_renews_lease_before_competing_reclaim(self):
        from daem0nmcp.retrieval.jobs import ProjectionJobRunner

        self._queue_job()
        claimed_at = self._clock
        lease_duration_us = 100_000
        original_expiry = claimed_at + lease_duration_us
        competing_runs = []

        def long_build(_workspace_id):
            self._clock = claimed_at + lease_duration_us // 2
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                renewed = self.connection.execute(
                    "SELECT lease_expires_at_us FROM background_jobs"
                ).fetchone()[0]
                if int(renewed) > original_expiry:
                    break
                time.sleep(0.01)
            else:
                self.fail("projection job lease was not renewed")
            self._clock = original_expiry + 1
            competitor_connection = sqlite3.connect(self.database_path)
            competitor_connection.row_factory = sqlite3.Row
            try:
                competitor = ProjectionJobRunner(
                    competitor_connection,
                    builders={
                        "lexical": lambda _workspace: competing_runs.append("reclaimed")
                    },
                    clock_us=lambda: self._clock,
                    lease_owner="competing-worker",
                    token_factory=lambda: "competing-token",
                    lease_duration_us=lease_duration_us,
                    heartbeat_interval_us=10_000,
                    retry_delay_us=1,
                )
                competing_runs.append(competitor.run_once())
            finally:
                competitor_connection.close()

        result = self._runner(
            {"lexical": long_build},
            lease_duration_us=lease_duration_us,
            heartbeat_interval_us=10_000,
        ).run_once()

        self.assertEqual("succeeded", result.status)
        self.assertEqual([None], competing_runs)
        self.assertEqual(
            "succeeded",
            self.connection.execute("SELECT status FROM background_jobs").fetchone()[0],
        )

    def test_write_locked_builder_renews_from_fresh_post_lock_time(self):
        from daem0nmcp.retrieval.jobs import ProjectionJobRunner

        self._queue_job()
        competing_results = []

        def write_locked_build(_workspace_id):
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                "UPDATE background_jobs SET result_json=result_json"
            )
            time.sleep(0.25)
            self.connection.commit()
            time.sleep(0.08)
            competitor_connection = sqlite3.connect(self.database_path)
            competitor_connection.row_factory = sqlite3.Row
            try:
                competitor = ProjectionJobRunner(
                    competitor_connection,
                    builders={"lexical": lambda _workspace: None},
                    clock_us=lambda: time.time_ns() // 1_000,
                    lease_owner="competing-worker",
                    token_factory=lambda: "competing-token",
                    lease_duration_us=100_000,
                    heartbeat_interval_us=20_000,
                    retry_delay_us=1,
                )
                competing_results.append(competitor.run_once())
            finally:
                competitor_connection.close()

        runner = ProjectionJobRunner(
            self.connection,
            builders={"lexical": write_locked_build},
            clock_us=lambda: time.time_ns() // 1_000,
            lease_owner="test-worker",
            token_factory=lambda: "lease-token",
            lease_duration_us=100_000,
            heartbeat_interval_us=20_000,
            retry_delay_us=1,
        )

        result = runner.run_once()

        self.assertEqual("succeeded", result.status)
        self.assertEqual([None], competing_results)
        self.assertEqual(
            ("succeeded", 1),
            tuple(
                self.connection.execute(
                    "SELECT status,attempts FROM background_jobs"
                ).fetchone()
            ),
        )

    def test_write_locked_builder_outlasts_heartbeat_busy_timeout(self):
        from daem0nmcp.retrieval.jobs import ProjectionJobRunner

        self._queue_job()

        def write_locked_build(_workspace_id):
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                "UPDATE background_jobs SET result_json=result_json"
            )
            time.sleep(1.25)
            self.connection.commit()

        runner = ProjectionJobRunner(
            self.connection,
            builders={"lexical": write_locked_build},
            clock_us=lambda: time.time_ns() // 1_000,
            lease_owner="test-worker",
            token_factory=lambda: "lease-token",
            lease_duration_us=100_000,
            heartbeat_interval_us=20_000,
            retry_delay_us=1,
        )

        result = runner.run_once()

        self.assertEqual("succeeded", result.status)
        self.assertEqual(
            ("succeeded", 1),
            tuple(
                self.connection.execute(
                    "SELECT status,attempts FROM background_jobs"
                ).fetchone()
            ),
        )

    def test_write_during_running_build_requeues_latest_snapshot(self):
        self._queue_job()

        def append_during_build(_workspace_id):
            self._append_record("c", "arrived during rebuild")
            self.connection.commit()

        result = self._runner({"lexical": append_during_build}).run_once()

        self.assertEqual("queued", result.status)
        row = self.connection.execute(
            "SELECT status,attempts,lease_owner,payload_json,source_event_id "
            "FROM background_jobs"
        ).fetchone()
        self.assertEqual(("queued", 1, None), tuple(row[:3]))
        self.assertEqual(row[4], json.loads(row[3])["source_event_id"])
        self.assertEqual(
            1,
            self.connection.execute("SELECT COUNT(*) FROM background_jobs").fetchone()[
                0
            ],
        )

    def test_default_runner_rebuilds_stale_lexical_generation(self):
        from daem0nmcp.retrieval.jobs import create_projection_job_runner
        from daem0nmcp.retrieval.providers import LexicalProvider
        from daem0nmcp.retrieval.types import RetrievalQuery

        self._queue_job()
        result = create_projection_job_runner(
            self.connection,
            clock_us=lambda: self._clock,
            lease_owner="test-worker",
            token_factory=lambda: "lease-token",
        ).run_once()

        provider = asyncio.run(
            LexicalProvider(self.connection).search(
                RetrievalQuery(
                    workspace_id=WORKSPACE_ID,
                    text="later canonical",
                ),
                10,
            )
        )
        self.assertEqual("succeeded", result.status)
        self.assertEqual("ready", provider.status)
        self.assertEqual(1, len(provider.candidates))


if __name__ == "__main__":
    unittest.main()
