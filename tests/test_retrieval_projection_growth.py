"""Superseded projection generations are garbage-collected (KD-5, F-001).

Every write rebuilds the local projections into a new generation. Without GC
each rebuild left a full-corpus FTS5 table, its document rows and a manifest
behind, so storage grew quadratically with the number of memories.
"""

from __future__ import annotations

import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

WORKSPACE_ID = "ws_0123456789abcdef01234567"
_NO_OPTIONAL_PROFILES = {
    "local": "disabled",
    "models-local": "disabled",
    "graph": "disabled",
}
_LOCAL = ("lexical", "procedure", "outcome", "temporal")
# Spelled out here on purpose: importing the production mapping would make a
# shrunken GENERATION_TABLES pass this file.
_GENERATION_TABLES = {
    "lexical": "retrieval_documents",
    "outcome": "record_outcome_view",
    "procedure": "record_procedures",
}


def _record(content: str, *, procedure: bool = False) -> dict[str, object]:
    return {
        "record_type": "procedure" if procedure else "decision",
        "legacy_type": None,
        "content": content,
        "rationale": None,
        "context": {"steps": ["Check state.", "Apply change."]} if procedure else {},
        "tags": ["growth"],
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
        "source_client": "growth-test",
        "source_model": None,
        "deleted_at_us": None,
    }


class ProjectionGenerationGrowthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from daem0nmcp.migrations.schema import MIGRATIONS

        self._directory = tempfile.TemporaryDirectory()
        self.path = Path(self._directory.name) / "growth.sqlite3"
        self.connection = sqlite3.connect(self.path)
        self.connection.execute("PRAGMA foreign_keys=ON")
        # Pre-PR-8 databases on POSIX still use the rollback journal, where
        # readers block a committing writer; GC must stay safe there.
        self.connection.execute("PRAGMA journal_mode=DELETE")
        # Durability is irrelevant here; skipping fsync keeps the hundreds of
        # seeding rebuilds fast on slow CI disks.
        self.connection.execute("PRAGMA synchronous=OFF")
        for version, _description, statements in MIGRATIONS:
            if 16 <= version <= 18:
                for statement in statements:
                    self.connection.execute(statement)
        self.connection.commit()
        self._sequence = 0

    def tearDown(self) -> None:
        self.connection.close()
        self._directory.cleanup()

    def _record_outcome(self, record_id: str) -> None:
        from daem0nmcp.event_store import EventCommand, EventStore

        self._sequence += 1
        EventStore(self.connection).append_and_project(
            EventCommand(
                workspace_id=WORKSPACE_ID,
                stream_id=record_id,
                stream_kind="memory",
                event_type="memory.outcome_recorded",
                occurred_at_us=100 + self._sequence,
                recorded_at_us=100 + self._sequence,
                actor_type="system",
                payload={
                    "record": {
                        **_record("growth memory number 1", procedure=True),
                        "outcome": "the generation was collected",
                        "worked": True,
                    }
                },
            )
        )
        self.connection.commit()

    def _append(self, *, procedure: bool = False) -> str:
        from daem0nmcp.event_store import EventCommand, EventStore

        self._sequence += 1
        record_id = f"mem_{self._sequence:064x}"
        EventStore(self.connection).append_and_project(
            EventCommand(
                workspace_id=WORKSPACE_ID,
                stream_id=record_id,
                stream_kind="memory",
                event_type="memory.created",
                occurred_at_us=100 + self._sequence,
                recorded_at_us=100 + self._sequence,
                actor_type="system",
                payload={
                    "record": _record(
                        f"growth memory number {self._sequence}", procedure=procedure
                    )
                },
            )
        )
        self.connection.commit()
        return record_id

    def _rebuild(self, projection: str) -> None:
        from daem0nmcp.retrieval.projections import LexicalProjectionBuilder
        from daem0nmcp.retrieval.specialized_projection import (
            SpecializedProjectionBuilder,
        )

        if projection == "lexical":
            LexicalProjectionBuilder(self.connection).rebuild(WORKSPACE_ID)
        else:
            SpecializedProjectionBuilder(self.connection).rebuild(
                WORKSPACE_ID, projection, force=True
            )

    def _manifests(self, projection: str) -> dict[int, str]:
        return {
            int(row[0]): str(row[1])
            for row in self.connection.execute(
                "SELECT generation,status FROM projection_manifests "
                "WHERE workspace_id=? AND projection_name=?",
                (WORKSPACE_ID, projection),
            )
        }

    def _fts_generations(self, projection: str) -> set[int]:
        prefix = (
            "retrieval_procedure_fts_"
            if projection == "procedure"
            else ("retrieval_fts_")
        )
        pattern = re.compile(re.escape(prefix + WORKSPACE_ID[3:]) + r"_g(\d+)")
        return {
            int(match[1])
            for (name,) in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            if (match := pattern.fullmatch(str(name))) is not None
        }

    def _active(self, projection: str) -> int:
        [generation] = [
            generation
            for generation, status in self._manifests(projection).items()
            if status == "active"
        ]
        return generation

    async def test_spaced_writes_keep_generations_bounded(self) -> None:
        from daem0nmcp.retrieval.runtime import drain_projection_jobs

        first = self._append(procedure=True)
        self._record_outcome(first)
        for projection in _LOCAL:
            self._rebuild(projection)
        writes = 20
        for index in range(writes):
            self._append(procedure=index % 5 == 0)
            # Every job is due immediately: this is the interactive case of
            # writes spaced further apart than the coalescing grace.
            self.connection.execute("UPDATE background_jobs SET available_at_us=0")
            self.connection.commit()
            runs = await drain_projection_jobs(
                self.path,
                config=SimpleNamespace(),
                max_jobs=8,
                # The real drain: every local projection rebuilds, so the
                # specialized GC call site runs on the production path.
                include_optional=True,
                capability_statuses=_NO_OPTIONAL_PROFILES,
            )
            self.assertEqual(
                {"succeeded"},
                {run.status for run in runs},
                [run.reason for run in runs],
            )
            self.assertEqual(
                set(_LOCAL),
                {name for run in runs for name in run.projections},
            )

        for projection in _LOCAL:
            manifests = self._manifests(projection)
            self.assertLessEqual(len(manifests), 2, (projection, manifests))
            self.assertEqual(1, list(manifests.values()).count("active"), projection)
        for projection in ("lexical", "procedure"):
            tables = self._fts_generations(projection)
            self.assertLessEqual(len(tables), 2, (projection, tables))
            self.assertEqual(tables, set(self._manifests(projection)), projection)
        live = self.connection.execute(
            "SELECT COUNT(*) FROM memory_records WHERE workspace_id=?",
            (WORKSPACE_ID,),
        ).fetchone()[0]
        self.assertEqual(writes + 1, live)
        documents = self.connection.execute(
            "SELECT COUNT(*) FROM retrieval_documents"
        ).fetchone()[0]
        self.assertLessEqual(documents, 2 * live)
        self._assert_generation_rows_collected()
        # The stale marker every write sets clears once the rebuild runs, and
        # GC never leaves it stuck on an active manifest.
        self.assertEqual(
            [],
            self.connection.execute(
                "SELECT projection_name FROM projection_manifests "
                "WHERE workspace_id=? AND status='active' AND ("
                "json_extract(details_json,'$.rebuild_required_event_id') IS NOT NULL "
                "OR json_extract(details_json,'$.rebuild_required_at_us') IS NOT NULL)",
                (WORKSPACE_ID,),
            ).fetchall(),
        )

    def _assert_generation_rows_collected(self) -> None:
        """Every generation table keeps rows only for surviving manifests."""

        for projection, table in _GENERATION_TABLES.items():
            rows = {
                int(row[0])
                for row in self.connection.execute(
                    f'SELECT DISTINCT projection_generation FROM "{table}" '
                    "WHERE workspace_id=?",
                    (WORKSPACE_ID,),
                )
            }
            self.assertTrue(rows, f"{table} has no rows to prove collection")
            self.assertLessEqual(rows, set(self._manifests(projection)), table)

    def test_stale_backlog_shrinks_five_generations_per_activation(self) -> None:
        self._append(procedure=True)
        for projection in ("lexical", "procedure"):
            with self.subTest(projection=projection):
                # Seed 120 superseded generations the way a pre-GC database
                # accumulated them.
                with (
                    patch(
                        "daem0nmcp.retrieval.projections.collect_superseded_generations",
                        return_value=0,
                    ),
                    patch(
                        "daem0nmcp.retrieval.specialized_projection."
                        "collect_superseded_generations",
                        return_value=0,
                    ),
                ):
                    for _ in range(121):
                        self._rebuild(projection)
                self.assertEqual(121, len(self._manifests(projection)))
                self.assertEqual(121, len(self._fts_generations(projection)))

                previous = 120
                while previous > 1:
                    self._rebuild(projection)
                    stale = sum(
                        status != "active"
                        for status in self._manifests(projection).values()
                    )
                    self.assertEqual(max(1, previous + 1 - 5), stale)
                    previous = stale
                manifests = self._manifests(projection)
                self.assertEqual(2, len(manifests))
                self.assertEqual(set(manifests), self._fts_generations(projection))
                active = self._active(projection)
                self.assertEqual({active - 1, active}, set(manifests))

    def test_failed_or_unactivated_generation_never_evicts_last_active(self) -> None:
        from daem0nmcp.event_store import deterministic_id

        self._append()
        self._rebuild("lexical")
        self._rebuild("lexical")
        previous_active = self._active("lexical")
        template = self.connection.execute(
            "SELECT source_event_count,source_event_root_hash,row_count,"
            "builder_version FROM projection_manifests WHERE workspace_id=? "
            "AND projection_name='lexical' AND generation=?",
            (WORKSPACE_ID, previous_active),
        ).fetchone()
        # Generations just below the next one that never became active: an
        # arithmetic "keep the one below the new generation" rule would keep
        # these and evict the generation readers were actually using.
        for offset, status in ((1, "failed"), (2, "ready")):
            generation = previous_active + offset
            self.connection.execute(
                "INSERT INTO projection_manifests (manifest_id,workspace_id,"
                "projection_name,generation,projection_version,status,"
                "source_event_count,source_event_root_hash,row_count,"
                "builder_version,details_json,started_at_us) "
                "VALUES (?,?,'lexical',?,1,?,?,?,?,?,'{}',1)",
                (
                    deterministic_id("prj", "growth-test", generation),
                    WORKSPACE_ID,
                    generation,
                    status,
                    *template,
                ),
            )
        self.connection.commit()

        self._rebuild("lexical")

        manifests = self._manifests("lexical")
        active = self._active("lexical")
        self.assertEqual(previous_active + 3, active)
        self.assertEqual({previous_active: "ready", active: "active"}, manifests)
        self.assertEqual({previous_active, active}, self._fts_generations("lexical"))

    def test_gc_blocked_by_an_open_reader_does_not_fail_the_rebuild(self) -> None:
        from daem0nmcp.retrieval import projections
        from daem0nmcp.retrieval.lexical_config import lexical_fts_table_name

        self._append()
        for _ in range(3):
            self._rebuild("lexical")
        oldest = min(self._manifests("lexical"))
        reader = sqlite3.connect(self.path, check_same_thread=False)
        collect = projections.collect_superseded_generations
        collected: list[int] = []

        def collect_while_reading(*args, **kwargs):
            # A reader that looked up an old generation is mid-query when
            # the activation's GC starts.
            reader.execute("BEGIN")
            reader.execute(
                f'SELECT COUNT(*) FROM "{lexical_fts_table_name(WORKSPACE_ID, oldest)}"'
            ).fetchone()
            collected.append(collect(*args, **kwargs))
            return collected[-1]

        try:
            with patch.object(
                projections,
                "collect_superseded_generations",
                collect_while_reading,
            ):
                result = projections.LexicalProjectionBuilder(self.connection).rebuild(
                    WORKSPACE_ID
                )
            self.assertEqual("active", result.status)
            self.assertEqual([0], collected)
            self.assertIn(oldest, self._manifests("lexical"))
            self.assertIn(oldest, self._fts_generations("lexical"))
            self.assertFalse(self.connection.in_transaction)
        finally:
            reader.close()

        # The next activation retries and catches up.
        self._rebuild("lexical")
        self.assertNotIn(oldest, self._manifests("lexical"))
        self.assertLessEqual(len(self._manifests("lexical")), 2)

    def test_gc_in_wal_mode_collects_under_an_open_reader(self) -> None:
        from daem0nmcp.retrieval import projections
        from daem0nmcp.retrieval.lexical_config import lexical_fts_table_name

        self.connection.execute("PRAGMA journal_mode=WAL")
        self._append()
        for _ in range(3):
            self._rebuild("lexical")
        oldest = min(self._manifests("lexical"))
        table = lexical_fts_table_name(WORKSPACE_ID, oldest)
        reader = sqlite3.connect(self.path)
        collect = projections.collect_superseded_generations
        collected: list[int] = []

        def collect_while_reading(*args, **kwargs):
            reader.execute("BEGIN")
            reader.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
            collected.append(collect(*args, **kwargs))
            return collected[-1]

        try:
            with patch.object(
                projections,
                "collect_superseded_generations",
                collect_while_reading,
            ):
                result = projections.LexicalProjectionBuilder(self.connection).rebuild(
                    WORKSPACE_ID
                )
            self.assertEqual("active", result.status)
            # WAL readers do not block the drop, and the reader's snapshot
            # still answers from the generation being collected.
            self.assertEqual([1], collected)
            self.assertEqual(
                1, len(reader.execute(f'SELECT * FROM "{table}" LIMIT 1').fetchall())
            )
            self.assertNotIn(oldest, self._manifests("lexical"))
            self.assertNotIn(oldest, self._fts_generations("lexical"))
        finally:
            reader.close()

    def test_repair_left_rebuild_required_generations_are_collected(self) -> None:
        self._append()
        self._rebuild("lexical")
        repaired = self._active("lexical")
        # verify-v7 --repair-projections demotes the active generation and
        # rebuilds in one transaction, so its GC is a no-op.
        self.connection.execute(
            "UPDATE projection_manifests SET status='rebuild_required' "
            "WHERE workspace_id=? AND projection_name='lexical' AND generation=?",
            (WORKSPACE_ID, repaired),
        )
        self.connection.commit()

        self._rebuild("lexical")

        manifests = self._manifests("lexical")
        self.assertNotIn(repaired, manifests)
        self.assertEqual(set(manifests), self._fts_generations("lexical"))
        self.assertEqual(
            0,
            self.connection.execute(
                "SELECT COUNT(*) FROM retrieval_documents WHERE workspace_id=? "
                "AND projection_generation=?",
                (WORKSPACE_ID, repaired),
            ).fetchone()[0],
        )

    def test_an_undroppable_generation_does_not_wedge_the_rest(self) -> None:
        from daem0nmcp.retrieval import projections
        from daem0nmcp.retrieval.lexical_config import lexical_fts_table_name

        self._append()
        with patch.object(
            projections, "collect_superseded_generations", return_value=0
        ):
            for _ in range(4):
                self._rebuild("lexical")
        generations = sorted(self._manifests("lexical"))
        damaged = generations[0]
        original = projections._drop_generation

        def refuse_oldest(
            connection, workspace_id, projection, manifest_id, generation
        ):
            if generation == damaged:
                raise sqlite3.OperationalError("malformed database schema")
            return original(
                connection, workspace_id, projection, manifest_id, generation
            )

        with patch.object(projections, "_drop_generation", refuse_oldest):
            collected = projections.collect_superseded_generations(
                self.connection, WORKSPACE_ID, "lexical"
            )

        manifests = self._manifests("lexical")
        # The damaged generation is skipped, not retried forever, and every
        # later victim is still collected.
        self.assertGreaterEqual(collected, 1)
        self.assertIn(damaged, manifests)
        self.assertIn(damaged, self._fts_generations("lexical"))
        self.assertNotIn(generations[1], manifests)
        self.assertNotIn(generations[1], self._fts_generations("lexical"))
        self.assertIsNotNone(lexical_fts_table_name(WORKSPACE_ID, damaged))

    def test_offline_compaction_drains_a_backlog_and_reclaims_the_file(self) -> None:
        from daem0nmcp.retrieval.operations import compact_projections

        self._append(procedure=True)
        with (
            patch(
                "daem0nmcp.retrieval.projections.collect_superseded_generations",
                return_value=0,
            ),
            patch(
                "daem0nmcp.retrieval.specialized_projection."
                "collect_superseded_generations",
                return_value=0,
            ),
        ):
            for _ in range(30):
                for projection in ("lexical", "procedure"):
                    self._rebuild(projection)
        self.assertEqual(30, len(self._manifests("lexical")))
        before = self.path.stat().st_size

        payload = compact_projections(self.connection, WORKSPACE_ID)

        self.assertEqual(28, payload["collected"]["lexical"])
        self.assertEqual(28, payload["collected"]["procedure"])
        self.assertTrue(payload["vacuumed"])
        self.assertEqual(58, payload["superseded_manifests_before"])
        self.assertEqual(2, payload["superseded_manifests_after"])
        for projection in ("lexical", "procedure"):
            manifests = self._manifests(projection)
            self.assertEqual(2, len(manifests), projection)
            self.assertEqual(set(manifests), self._fts_generations(projection))
        self.assertLess(self.path.stat().st_size, before)

    def test_lock_contention_is_retried_not_dead_lettered(self) -> None:
        from daem0nmcp.retrieval.jobs import ProjectionJobRunner
        from daem0nmcp.retrieval.projections import LexicalProjectionBuilder

        self._append()
        self.connection.execute("UPDATE background_jobs SET available_at_us=0")
        self.connection.commit()
        runner_connection = sqlite3.connect(self.path, timeout=0.1)
        blocker = sqlite3.connect(self.path, check_same_thread=False)
        lexical = LexicalProjectionBuilder(runner_connection)

        def contended_rebuild(workspace_id: str):
            blocker.execute("BEGIN IMMEDIATE")
            try:
                return lexical.rebuild(workspace_id)
            finally:
                blocker.rollback()

        try:
            run = ProjectionJobRunner(
                runner_connection, builders={"lexical": contended_rebuild}
            ).run_once()
            assert run is not None
            self.assertEqual("DATABASE_IN_USE", run.reason)
            self.assertEqual("queued", run.status)
        finally:
            blocker.close()
            runner_connection.close()


class GenerationGcUnitTests(unittest.TestCase):
    def test_gc_inside_a_caller_transaction_is_a_no_op(self) -> None:
        from daem0nmcp.retrieval.projections import collect_superseded_generations

        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("CREATE TABLE t(x)")
            connection.execute("INSERT INTO t VALUES (1)")
            self.assertTrue(connection.in_transaction)
            self.assertEqual(
                0, collect_superseded_generations(connection, WORKSPACE_ID, "lexical")
            )
            self.assertEqual(
                0, collect_superseded_generations(connection, WORKSPACE_ID, "graph")
            )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
