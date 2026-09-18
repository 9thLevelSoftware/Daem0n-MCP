"""Lifecycle contracts for specialized retrieval projections."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

WORKSPACE_ID = "ws_0123456789abcdef01234567"


def _schema_migrations():
    path = (
        Path(__file__).resolve().parents[1] / "daem0nmcp" / "migrations" / "schema.py"
    )
    spec = importlib.util.spec_from_file_location("specialized_test_schema", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.MIGRATIONS


def _apply_migration(connection: sqlite3.Connection, version: int) -> None:
    migration = next(item for item in _schema_migrations() if item[0] == version)
    for statement in migration[2]:
        connection.execute(statement)


class SpecializedProjectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._directory.name) / "specialized.sqlite3"
        self.connection = sqlite3.connect(self.database_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        for version in (16, 17, 18):
            _apply_migration(self.connection, version)
        self.connection.commit()
        self._sequence = 0

    def tearDown(self) -> None:
        self.connection.close()
        self._directory.cleanup()

    @staticmethod
    def _record(
        content: str,
        *,
        record_type: str = "decision",
        context: dict[str, object] | None = None,
        outcome: str | None = None,
        worked: bool | None = None,
    ) -> dict[str, object]:
        return {
            "record_type": record_type,
            "legacy_type": None,
            "content": content,
            "rationale": None,
            "context": context or {},
            "tags": [],
            "file_path": None,
            "file_path_relative": None,
            "keywords": None,
            "is_permanent": False,
            "pinned": False,
            "archived": False,
            "outcome": outcome,
            "worked": worked,
            "recall_count": 0,
            "surprise_score": None,
            "importance_score": None,
            "source_client": "specialized-test",
            "source_model": None,
            "deleted_at_us": None,
        }

    def _append_memory(
        self,
        suffix: str,
        content: str,
        **changes: object,
    ) -> tuple[str, str]:
        from daem0nmcp.event_store import EventCommand, EventStore

        self._sequence += 1
        record_id = "mem_" + suffix * 64
        result = EventStore(self.connection).append_and_project(
            EventCommand(
                workspace_id=WORKSPACE_ID,
                stream_id=record_id,
                stream_kind="memory",
                event_type="memory.created",
                occurred_at_us=100 + self._sequence,
                recorded_at_us=200 + self._sequence,
                actor_type="system",
                payload={"record": self._record(content, **changes)},
            )
        )
        return record_id, result.event_id

    def _build_lexical(self) -> None:
        from daem0nmcp.retrieval.projections import LexicalProjectionBuilder

        LexicalProjectionBuilder(self.connection, clock_us=lambda: 800).rebuild(
            WORKSPACE_ID
        )

    async def test_paused_staging_does_not_hold_sqlite_writer_lock(self):
        from daem0nmcp.retrieval.specialized_projection import (
            SpecializedProjectionBuilder,
            SpecializedProjectionBuildError,
        )

        self._append_memory(
            "c",
            "Procedure staged outside writer transaction.",
            record_type="procedure",
            context={"steps": ["Read canonical state.", "Publish briefly."]},
        )
        self._build_lexical()
        staging_started = threading.Event()
        release_staging = threading.Event()
        builder_connection = sqlite3.connect(
            self.database_path,
            timeout=0.2,
            check_same_thread=False,
        )
        builder_connection.row_factory = sqlite3.Row

        class PausedBuilder(SpecializedProjectionBuilder):
            def _procedure_steps(self, workspace_id):
                staging_started.set()
                release_staging.wait(timeout=2)
                return super()._procedure_steps(workspace_id)

        builder = PausedBuilder(builder_connection, clock_us=lambda: 900)
        build = asyncio.create_task(
            asyncio.to_thread(builder.rebuild, WORKSPACE_ID, "procedure")
        )
        self.assertTrue(await asyncio.to_thread(staging_started.wait, 2))
        try:
            # This follows the canonical foreground event-store writer path.
            self._append_memory("d", "Foreground write during staging.")
            self.connection.commit()
        finally:
            release_staging.set()
        with self.assertRaises(SpecializedProjectionBuildError) as raised:
            await build
        builder_connection.close()

        self.assertEqual("PROJECTION_BUILD_SUPERSEDED", raised.exception.code)

    def _record_outcome(
        self,
        record_id: str,
        content: str,
        outcome: str,
        worked: bool,
    ) -> str:
        from daem0nmcp.event_store import EventCommand, EventStore

        self._sequence += 1
        result = EventStore(self.connection).append_and_project(
            EventCommand(
                workspace_id=WORKSPACE_ID,
                stream_id=record_id,
                stream_kind="memory",
                event_type="memory.outcome_recorded",
                occurred_at_us=100 + self._sequence,
                recorded_at_us=200 + self._sequence,
                actor_type="system",
                payload={
                    "record": self._record(content, outcome=outcome, worked=worked)
                },
            )
        )
        return result.event_id

    def _update_memory(
        self,
        record_id: str,
        content: str,
        *,
        event_type: str = "memory.updated",
        **changes: object,
    ) -> tuple[str, int]:
        from daem0nmcp.event_store import EventCommand, EventStore

        self._sequence += 1
        recorded_at_us = 200 + self._sequence
        result = EventStore(self.connection).append_and_project(
            EventCommand(
                workspace_id=WORKSPACE_ID,
                stream_id=record_id,
                stream_kind="memory",
                event_type=event_type,
                occurred_at_us=100 + self._sequence,
                recorded_at_us=recorded_at_us,
                actor_type="system",
                payload={"record": self._record(content, **changes)},
            )
        )
        return result.event_id, recorded_at_us

    def _append_fact(
        self,
        suffix: str,
        subject_record_id: str,
        *,
        target_record_id: str | None = None,
    ) -> str:
        from daem0nmcp.event_store import EventCommand, EventStore

        self._sequence += 1
        fact_id = "fact_" + suffix * 64
        EventStore(self.connection).append_and_project(
            EventCommand(
                workspace_id=WORKSPACE_ID,
                stream_id=fact_id,
                stream_kind="fact",
                event_type="fact.asserted",
                occurred_at_us=100 + self._sequence,
                recorded_at_us=200 + self._sequence,
                actor_type="system",
                payload={
                    "fact": {
                        "subject_record_id": subject_record_id,
                        "predicate": "uses",
                        "object_kind": (
                            "record_ref" if target_record_id is not None else "text"
                        ),
                        "object": target_record_id or "SQLite",
                        "legacy_type": None,
                        "confidence": 1.0,
                        "verification_count": 1,
                        "is_verified": True,
                        "evidence": [],
                        "metadata": {},
                        "valid_from_us": 100,
                        "valid_to_us": None,
                    }
                },
            )
        )
        return fact_id

    def test_graph_build_includes_record_ref_fact_rows(self):
        from daem0nmcp.retrieval.specialized_projection import (
            SpecializedProjectionBuilder,
        )

        left_id, _ = self._append_memory("e", "Left graph record.")
        right_id, _ = self._append_memory("f", "Right graph record.")
        self._append_fact("e", left_id, target_record_id=right_id)
        self._build_lexical()

        result = SpecializedProjectionBuilder(
            self.connection, clock_us=lambda: 900
        ).rebuild(WORKSPACE_ID, "graph")

        self.assertEqual("active", result.status)
        self.assertEqual(1, result.row_count)
        self.assertEqual(
            "memory_relationship_versions+memory_fact_versions.record_ref",
            result.storage_target,
        )

    def _append_relationship(
        self, suffix: str, source_record_id: str, target_record_id: str
    ) -> str:
        from daem0nmcp.event_store import EventCommand, EventStore

        self._sequence += 1
        relationship_id = "rel_" + suffix * 64
        EventStore(self.connection).append_and_project(
            EventCommand(
                workspace_id=WORKSPACE_ID,
                stream_id=relationship_id,
                stream_kind="relationship",
                event_type="relationship.created",
                occurred_at_us=100 + self._sequence,
                recorded_at_us=200 + self._sequence,
                actor_type="system",
                payload={
                    "relationship": {
                        "source_record_id": source_record_id,
                        "target_record_id": target_record_id,
                        "relationship_type": "depends_on",
                        "legacy_type": None,
                        "description": "explicit edge",
                        "confidence": 1.0,
                        "metadata": {},
                        "valid_from_us": 100,
                        "valid_to_us": None,
                    }
                },
            )
        )
        return relationship_id

    async def test_procedure_build_indexes_only_explicit_structured_steps(self):
        from daem0nmcp.retrieval.specialized import ProcedureProvider
        from daem0nmcp.retrieval.specialized_projection import (
            SpecializedProjectionBuilder,
        )
        from daem0nmcp.retrieval.types import RetrievalQuery

        structured_id, source_event_id = self._append_memory(
            "1",
            "Prose says deploy and revoke but is not parsed.",
            record_type="procedure",
            context={"steps": ["Deploy credential.", "Revoke credential."]},
        )
        self._append_memory(
            "2",
            "Deploy invented prose-only instruction.",
            record_type="procedure",
        )
        self._append_memory(
            "3",
            "A decision with step-shaped metadata.",
            context={"steps": ["Deploy from non-procedure."]},
        )
        self._build_lexical()

        result = SpecializedProjectionBuilder(
            self.connection, clock_us=lambda: 900
        ).rebuild(WORKSPACE_ID, "procedure")
        self.connection.commit()

        self.assertEqual("active", result.status)
        self.assertEqual(1, result.generation)
        self.assertEqual(2, result.row_count)
        rows = self.connection.execute(
            "SELECT record_id,ordinal,step_text,step_hash,source_event_id "
            "FROM record_procedures ORDER BY record_id,ordinal"
        ).fetchall()
        self.assertEqual(
            [
                (structured_id, 0, "Deploy credential."),
                (structured_id, 1, "Revoke credential."),
            ],
            [tuple(row[:3]) for row in rows],
        )
        self.assertEqual([source_event_id, source_event_id], [row[4] for row in rows])
        expected_hashes = [
            hashlib.sha256(
                json.dumps(
                    text, ensure_ascii=False, separators=(",", ":"), sort_keys=True
                ).encode("utf-8")
            ).hexdigest()
            for text in ("Deploy credential.", "Revoke credential.")
        ]
        self.assertEqual(expected_hashes, [row[3] for row in rows])
        manifest = self.connection.execute(
            "SELECT source_event_count,source_event_root_hash,details_json "
            "FROM projection_manifests WHERE manifest_id=?",
            (result.staging_manifest_id,),
        ).fetchone()
        self.assertEqual(3, manifest[0])
        self.assertEqual(result.source_event_root_hash, manifest[1])
        self.assertEqual(
            {
                "build_config_hash": result.build_config_hash,
                "builder_contract_hash": hashlib.sha256(
                    json.dumps(
                        {
                            "build_config_hash": result.build_config_hash,
                            "builder_version": "retrieval-specialized-1",
                            "projection": "procedure",
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest(),
                "content_digest": result.content_digest,
                "fts_table": result.storage_target,
                "projection": "procedure",
                "schema_version": 1,
            },
            json.loads(str(manifest[2])),
        )
        provider = await ProcedureProvider(self.connection).search(
            RetrievalQuery(workspace_id=WORKSPACE_ID, text="deploy credential"),
            10,
        )
        self.assertEqual("ready", provider.status)
        self.assertEqual(
            [structured_id],
            [item.evidence.record_id for item in provider.candidates],
        )

    async def test_outcome_build_uses_current_canonical_outcome_event_state(self):
        from daem0nmcp.retrieval.specialized import OutcomeProvider
        from daem0nmcp.retrieval.specialized_projection import (
            SpecializedProjectionBuilder,
        )
        from daem0nmcp.retrieval.types import RetrievalQuery

        succeeded_id, _ = self._append_memory("4", "Stable release decision.")
        succeeded_event = self._record_outcome(
            succeeded_id,
            "Stable release decision.",
            "Deployment remained healthy.",
            True,
        )
        failed_id, failed_event = self._append_memory(
            "5",
            "Risky release decision.",
            outcome="Deployment failed health checks.",
            worked=False,
        )
        self._append_memory("6", "No outcome yet.")
        self._build_lexical()

        result = SpecializedProjectionBuilder(
            self.connection, clock_us=lambda: 900
        ).rebuild(WORKSPACE_ID, "outcome")
        self.connection.commit()

        self.assertEqual("active", result.status)
        self.assertEqual(2, result.row_count)
        rows = self.connection.execute(
            "SELECT record_id,worked,outcome_text,outcome_event_id,"
            "transaction_at_us FROM record_outcome_view "
            "ORDER BY record_id"
        ).fetchall()
        self.assertEqual(
            [
                (
                    succeeded_id,
                    1,
                    "Deployment remained healthy.",
                    succeeded_event,
                    202,
                ),
                (
                    failed_id,
                    0,
                    "Deployment failed health checks.",
                    failed_event,
                    203,
                ),
            ],
            [tuple(row) for row in rows],
        )
        provider = await OutcomeProvider(
            self.connection, clock_us=lambda: 1_000
        ).search(
            RetrievalQuery(
                workspace_id=WORKSPACE_ID,
                text="failed deployment outcome",
            ),
            10,
        )
        self.assertEqual("ready", provider.status)
        self.assertEqual(failed_id, provider.candidates[0].evidence.record_id)

    def test_outcome_lineage_ignores_later_non_outcome_record_update(self):
        from daem0nmcp.retrieval.specialized_projection import (
            SpecializedProjectionBuilder,
        )

        record_id, _ = self._append_memory("7", "Initial decision.")
        outcome_event_id = self._record_outcome(
            record_id,
            "Initial decision.",
            "The rollout remained healthy.",
            True,
        )
        unrelated_event_id, unrelated_at_us = self._update_memory(
            record_id,
            "Clarified the decision rationale.",
            outcome="The rollout remained healthy.",
            worked=True,
        )

        SpecializedProjectionBuilder(self.connection, clock_us=lambda: 900).rebuild(
            WORKSPACE_ID, "outcome"
        )

        row = self.connection.execute(
            "SELECT outcome_event_id,transaction_at_us "
            "FROM record_outcome_view WHERE record_id=?",
            (record_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertNotEqual(outcome_event_id, unrelated_event_id)
        self.assertNotEqual(202, unrelated_at_us)
        self.assertEqual((outcome_event_id, 202), tuple(row))

    def test_outcome_lineage_accepts_latest_legacy_state_import_assertion(self):
        from daem0nmcp.retrieval.specialized_projection import (
            SpecializedProjectionBuilder,
        )

        record_id, created_event_id = self._append_memory(
            "8",
            "Imported decision.",
            outcome="Imported outcome.",
            worked=True,
        )
        imported_event_id, imported_at_us = self._update_memory(
            record_id,
            "Imported decision.",
            event_type="legacy.memory_state_imported",
            outcome="Imported outcome.",
            worked=True,
        )

        SpecializedProjectionBuilder(self.connection, clock_us=lambda: 900).rebuild(
            WORKSPACE_ID, "outcome"
        )

        row = self.connection.execute(
            "SELECT outcome_event_id,transaction_at_us "
            "FROM record_outcome_view WHERE record_id=?",
            (record_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertNotEqual(created_event_id, imported_event_id)
        self.assertEqual((imported_event_id, imported_at_us), tuple(row))

    def test_temporal_and_graph_bind_canonical_typed_rows_without_copying_them(self):
        from daem0nmcp.retrieval.specialized_projection import (
            SpecializedProjectionBuilder,
        )

        left_id, _ = self._append_memory("7", "Left record.")
        right_id, _ = self._append_memory("8", "Right record.")
        self._append_fact("9", left_id)
        self._append_relationship("a", left_id, right_id)
        self._build_lexical()
        fact_before = [
            tuple(row)
            for row in self.connection.execute(
                "SELECT * FROM memory_fact_versions ORDER BY fact_version_id"
            )
        ]
        relationship_before = [
            tuple(row)
            for row in self.connection.execute(
                "SELECT * FROM memory_relationship_versions "
                "ORDER BY relationship_version_id"
            )
        ]
        builder = SpecializedProjectionBuilder(self.connection, clock_us=lambda: 900)

        temporal = builder.rebuild(WORKSPACE_ID, "temporal")
        graph = builder.rebuild(WORKSPACE_ID, "graph")

        self.assertEqual("active", temporal.status)
        self.assertEqual("active", graph.status)
        self.assertEqual(1, temporal.row_count)
        self.assertEqual(1, graph.row_count)
        self.assertEqual(4, temporal.source_event_count)
        self.assertEqual(temporal.source_event_root_hash, graph.source_event_root_hash)
        self.assertRegex(temporal.content_digest, r"^[0-9a-f]{64}$")
        self.assertRegex(graph.content_digest, r"^[0-9a-f]{64}$")
        self.assertNotEqual(temporal.content_digest, graph.content_digest)
        self.assertEqual(
            fact_before,
            [
                tuple(row)
                for row in self.connection.execute(
                    "SELECT * FROM memory_fact_versions ORDER BY fact_version_id"
                )
            ],
        )
        self.assertEqual(
            relationship_before,
            [
                tuple(row)
                for row in self.connection.execute(
                    "SELECT * FROM memory_relationship_versions "
                    "ORDER BY relationship_version_id"
                )
            ],
        )

    def test_manifest_contract_hashes_bind_specialized_builder_version(self):
        import daem0nmcp.retrieval.specialized_projection as projection_module

        builder = projection_module.SpecializedProjectionBuilder(
            self.connection, clock_us=lambda: 900
        )
        results = {
            name: builder.rebuild(WORKSPACE_ID, name)
            for name in ("graph", "outcome", "procedure", "temporal")
        }

        for name, result in results.items():
            details = json.loads(
                str(
                    self.connection.execute(
                        "SELECT details_json FROM projection_manifests "
                        "WHERE manifest_id=?",
                        (result.staging_manifest_id,),
                    ).fetchone()[0]
                )
            )
            expected_contract_hash = hashlib.sha256(
                json.dumps(
                    {
                        "build_config_hash": result.build_config_hash,
                        "builder_version": "retrieval-specialized-1",
                        "projection": name,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            self.assertEqual(expected_contract_hash, details["builder_contract_hash"])
            self.assertEqual(expected_contract_hash, result.builder_contract_hash)

        original_version = projection_module._BUILDER_VERSION
        try:
            projection_module._BUILDER_VERSION = "retrieval-specialized-next"
            self.assertTrue(
                all(
                    not builder.active_is_current(WORKSPACE_ID, name)
                    for name in results
                )
            )
        finally:
            projection_module._BUILDER_VERSION = original_version

    def test_dry_run_reports_high_water_active_delta_without_writes(self):
        from daem0nmcp.retrieval.specialized_projection import (
            SpecializedProjectionBuilder,
        )

        self._append_memory(
            "b",
            "First procedure.",
            record_type="procedure",
            context={"steps": ["First step."]},
        )
        self._build_lexical()
        builder = SpecializedProjectionBuilder(self.connection, clock_us=lambda: 900)
        first = builder.rebuild(WORKSPACE_ID, "procedure")
        self._append_memory(
            "c",
            "Second procedure.",
            record_type="procedure",
            context={"steps": ["Second step.", "Third step."]},
        )
        before_changes = self.connection.total_changes
        before_manifests = [
            tuple(row)
            for row in self.connection.execute(
                "SELECT * FROM projection_manifests ORDER BY projection_name,generation"
            )
        ]
        before_tables = copy.deepcopy(
            [
                tuple(row)
                for row in self.connection.execute(
                    "SELECT name,sql FROM sqlite_master ORDER BY name"
                )
            ]
        )

        preview = builder.rebuild(WORKSPACE_ID, "procedure", dry_run=True)

        self.assertTrue(preview.dry_run)
        self.assertEqual("ready", preview.status)
        self.assertEqual("ready", preview.capability_status)
        self.assertIsNone(preview.capability_reason)
        self.assertEqual(first.staging_manifest_id, preview.active_manifest_id)
        self.assertEqual(1, preview.active_generation)
        self.assertEqual("active", preview.active_status)
        self.assertEqual(1, preview.active_row_count)
        self.assertEqual(2, preview.row_count_delta)
        self.assertEqual(first.content_digest, preview.active_content_digest)
        self.assertTrue(preview.content_digest_changed)
        self.assertEqual(2, preview.generation)
        self.assertEqual(202, preview.cursor_recorded_at_us)
        self.assertIsNotNone(preview.cursor_event_id)
        self.assertIn("_g2", str(preview.storage_target))
        self.assertEqual(before_changes, self.connection.total_changes)
        self.assertEqual(
            before_manifests,
            [
                tuple(row)
                for row in self.connection.execute(
                    "SELECT * FROM projection_manifests "
                    "ORDER BY projection_name,generation"
                )
            ],
        )
        self.assertEqual(
            before_tables,
            [
                tuple(row)
                for row in self.connection.execute(
                    "SELECT name,sql FROM sqlite_master ORDER BY name"
                )
            ],
        )

    def test_current_retry_reuses_exact_active_generation(self):
        from daem0nmcp.retrieval.specialized_projection import (
            SpecializedProjectionBuilder,
        )

        self._append_memory(
            "d",
            "Reusable procedure.",
            record_type="procedure",
            context={"steps": ["Validate source."]},
        )
        self._build_lexical()
        builder = SpecializedProjectionBuilder(self.connection, clock_us=lambda: 900)
        first = builder.rebuild(WORKSPACE_ID, "procedure")
        before_changes = self.connection.total_changes
        before_tables = {
            str(row[0])
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }

        retried = builder.rebuild(WORKSPACE_ID, "procedure")

        self.assertTrue(retried.reused)
        self.assertEqual(first.generation, retried.generation)
        self.assertEqual(first.staging_manifest_id, retried.staging_manifest_id)
        self.assertEqual(before_changes, self.connection.total_changes)
        self.assertEqual(
            before_tables,
            {
                str(row[0])
                for row in self.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            },
        )
        self.assertEqual(
            [(1, "active")],
            [
                tuple(row)
                for row in self.connection.execute(
                    "SELECT generation,status FROM projection_manifests "
                    "WHERE projection_name='procedure'"
                )
            ],
        )

    def test_failed_procedure_build_retains_prior_active_generation(self):
        from daem0nmcp.retrieval.specialized_projection import (
            SpecializedProjectionBuilder,
            SpecializedProjectionBuildError,
        )

        self._append_memory(
            "e",
            "Valid procedure.",
            record_type="procedure",
            context={"steps": ["Keep active."]},
        )
        self._build_lexical()
        builder = SpecializedProjectionBuilder(self.connection, clock_us=lambda: 900)
        first = builder.rebuild(WORKSPACE_ID, "procedure")
        first_rows = [
            tuple(row)
            for row in self.connection.execute(
                "SELECT * FROM record_procedures ORDER BY record_id,ordinal"
            )
        ]
        self._append_memory(
            "f",
            "Malformed structured procedure.",
            record_type="procedure",
            context={"steps": "invented prose"},
        )

        with self.assertRaises(SpecializedProjectionBuildError) as raised:
            builder.rebuild(WORKSPACE_ID, "procedure")

        self.assertEqual("INVALID_PROCEDURE_STEPS", raised.exception.code)
        self.assertEqual(
            [(1, "active")],
            [
                tuple(row)
                for row in self.connection.execute(
                    "SELECT generation,status FROM projection_manifests "
                    "WHERE projection_name='procedure'"
                )
            ],
        )
        self.assertEqual(
            first_rows,
            [
                tuple(row)
                for row in self.connection.execute(
                    "SELECT * FROM record_procedures ORDER BY record_id,ordinal"
                )
            ],
        )
        self.assertIn(
            str(first.storage_target),
            {
                str(row[0])
                for row in self.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            },
        )

    def test_manifest_tampering_during_staging_cannot_activate(self):
        from daem0nmcp.retrieval.specialized_projection import (
            SpecializedProjectionBuilder,
            SpecializedProjectionBuildError,
        )

        self._append_memory(
            "0",
            "Tamper-resistant procedure.",
            record_type="procedure",
            context={"steps": ["Validate manifest."]},
        )
        self._build_lexical()
        self.connection.execute(
            "CREATE TRIGGER tamper_specialized_manifest "
            "AFTER INSERT ON record_procedures BEGIN "
            "UPDATE projection_manifests SET source_event_root_hash='"
            + ("f" * 64)
            + "' WHERE projection_name='procedure' AND status='building'; END"
        )
        builder = SpecializedProjectionBuilder(self.connection, clock_us=lambda: 900)

        with self.assertRaises(SpecializedProjectionBuildError) as raised:
            builder.rebuild(WORKSPACE_ID, "procedure")

        self.assertEqual("PROJECTION_VALIDATION_FAILED", raised.exception.code)
        self.assertEqual(
            0,
            self.connection.execute(
                "SELECT count(*) FROM projection_manifests "
                "WHERE projection_name='procedure'"
            ).fetchone()[0],
        )
        self.assertEqual(
            0,
            self.connection.execute(
                "SELECT count(*) FROM record_procedures"
            ).fetchone()[0],
        )

    def test_active_is_current_revalidates_procedure_fts_identity(self):
        from daem0nmcp.retrieval.specialized_projection import (
            SpecializedProjectionBuilder,
        )

        self._append_memory(
            "1",
            "Current procedure.",
            record_type="procedure",
            context={"steps": ["Keep FTS exact."]},
        )
        self._build_lexical()
        builder = SpecializedProjectionBuilder(self.connection, clock_us=lambda: 900)
        result = builder.rebuild(WORKSPACE_ID, "procedure")
        self.assertTrue(builder.active_is_current(WORKSPACE_ID, "procedure"))

        self.connection.execute(f'DELETE FROM "{result.storage_target}"')

        self.assertFalse(builder.active_is_current(WORKSPACE_ID, "procedure"))

    def test_procedure_dry_run_reports_fts_capability_unavailable(self):
        from daem0nmcp.retrieval.specialized_projection import (
            SpecializedProjectionBuilder,
        )

        class NoFtsBuilder(SpecializedProjectionBuilder):
            def _probe_fts5(self) -> None:
                raise sqlite3.OperationalError("host detail must be hidden")

        before_changes = self.connection.total_changes

        preview = NoFtsBuilder(self.connection, clock_us=lambda: 900).rebuild(
            WORKSPACE_ID, "procedure", dry_run=True
        )

        self.assertEqual("unavailable", preview.status)
        self.assertEqual("unavailable", preview.capability_status)
        self.assertEqual("PROCEDURE_UNAVAILABLE", preview.capability_reason)
        self.assertEqual(before_changes, self.connection.total_changes)

    def test_active_check_rejects_wrong_procedure_fts_configuration(self):
        from daem0nmcp.retrieval.specialized_projection import (
            SpecializedProjectionBuilder,
        )

        self._append_memory(
            "2",
            "Configured procedure.",
            record_type="procedure",
            context={"steps": ["Preserve tokenizer."]},
        )
        self._build_lexical()
        builder = SpecializedProjectionBuilder(self.connection, clock_us=lambda: 900)
        result = builder.rebuild(WORKSPACE_ID, "procedure")
        rows = [
            tuple(row)
            for row in self.connection.execute(
                f'SELECT * FROM "{result.storage_target}"'
            )
        ]
        self.connection.execute(f'DROP TABLE "{result.storage_target}"')
        self.connection.execute(
            f'CREATE VIRTUAL TABLE "{result.storage_target}" USING fts5('
            "record_id UNINDEXED,ordinal UNINDEXED,step_hash UNINDEXED,"
            "source_event_id UNINDEXED,step_text,tokenize='porter')"
        )
        self.connection.executemany(
            f'INSERT INTO "{result.storage_target}" VALUES (?,?,?,?,?)',
            rows,
        )

        self.assertFalse(builder.active_is_current(WORKSPACE_ID, "procedure"))


if __name__ == "__main__":
    unittest.main()
