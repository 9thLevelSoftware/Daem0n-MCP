"""Dependency-free contracts for the v7 lexical retrieval projection."""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

WORKSPACE_ID = "ws_0123456789abcdef01234567"
OTHER_WORKSPACE_ID = "ws_76543210fedcba9876543210"


def _schema_migrations():
    path = (
        Path(__file__).resolve().parents[1] / "daem0nmcp" / "migrations" / "schema.py"
    )
    spec = importlib.util.spec_from_file_location("retrieval_test_schema", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.MIGRATIONS


def _apply_migration(connection: sqlite3.Connection, version: int) -> None:
    migration = next(
        (item for item in _schema_migrations() if item[0] == version), None
    )
    if migration is None:
        raise AssertionError(f"additive SQL migration {version} is missing")
    for statement in migration[2]:
        connection.execute(statement)


class RetrievalMigration18Tests(unittest.TestCase):
    """The lexical and dense metadata projections remain fully rebuildable."""

    def test_migration_18_creates_projection_tables_without_global_fts_corpus(self):
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            _apply_migration(connection, 16)
            _apply_migration(connection, 17)
            _apply_migration(connection, 18)
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
            self.assertTrue(
                {
                    "retrieval_documents",
                    "record_procedures",
                    "record_outcome_view",
                    "dense_projection_refs",
                }
                <= tables
            )
            self.assertNotIn("retrieval_documents_fts", tables)
            self.assertFalse(
                {
                    "retrieval_documents_fts_insert",
                    "retrieval_documents_fts_update",
                    "retrieval_documents_fts_delete",
                }
                & triggers
            )
            dense_columns = {
                row[1].lower()
                for row in connection.execute(
                    "PRAGMA table_info(dense_projection_refs)"
                )
            }
            self.assertFalse(dense_columns & {"vector", "embedding", "payload", "blob"})
        finally:
            connection.close()

    def test_orm_and_upgrade_metadata_include_retrieval_projection_schema(self):
        root = Path(__file__).resolve().parents[1]
        tree = ast.parse((root / "daem0nmcp" / "models.py").read_text(encoding="utf-8"))
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
        self.assertEqual(
            {
                "retrieval_documents": "RetrievalDocument",
                "record_procedures": "RecordProcedure",
                "record_outcome_view": "RecordOutcomeView",
                "dense_projection_refs": "DenseProjectionRef",
            },
            {
                table: declared.get(table)
                for table in (
                    "retrieval_documents",
                    "record_procedures",
                    "record_outcome_view",
                    "dense_projection_refs",
                )
            },
        )
        upgrade = (root / "scripts" / "upgrade.py").read_text(encoding="utf-8")
        self.assertIn(
            "from daem0nmcp.schema_version import CURRENT_SCHEMA_VERSION",
            upgrade,
        )

        model_source = (root / "daem0nmcp" / "models.py").read_text(encoding="utf-8")
        for class_name in (
            "RecordProcedure",
            "RecordOutcomeView",
            "DenseProjectionRef",
        ):
            class_source = model_source.split(f"class {class_name}", 1)[1].split(
                "\nclass ", 1
            )[0]
            self.assertIn('"sqlite_with_rowid": False', class_source)
        retrieval_source = model_source.split("class RetrievalDocument", 1)[1].split(
            "\nclass ", 1
        )[0]
        self.assertIn(
            'rationale = Column(Text, nullable=False, server_default="")',
            retrieval_source,
        )
        self.assertIn(
            'tags_text = Column(Text, nullable=False, server_default="")',
            retrieval_source,
        )
        self.assertIn(
            'visibility = Column(String, nullable=False, server_default="workspace")',
            retrieval_source,
        )
        self.assertIn(
            'archived = Column(Integer, nullable=False, server_default="0")',
            retrieval_source,
        )


class LexicalProjectionTests(unittest.IsolatedAsyncioTestCase):
    """Build and query a generation without any optional model dependency."""

    def setUp(self) -> None:
        self._database_directory = tempfile.TemporaryDirectory()
        database_path = Path(self._database_directory.name) / "retrieval.sqlite3"
        self.connection = sqlite3.connect(database_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        _apply_migration(self.connection, 16)
        _apply_migration(self.connection, 17)
        _apply_migration(self.connection, 18)
        self.connection.commit()

    def tearDown(self) -> None:
        self.connection.close()
        self._database_directory.cleanup()

    @staticmethod
    def _record(content: str, rationale: str, tags: list[str], **changes):
        state = {
            "record_type": "decision",
            "legacy_type": None,
            "content": content,
            "rationale": rationale,
            "context": {},
            "tags": tags,
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
        state.update(changes)
        return state

    def _append_record(
        self,
        suffix: str,
        content: str,
        rationale: str,
        tags: list[str],
        workspace_id: str = WORKSPACE_ID,
        **changes,
    ) -> str:
        from daem0nmcp.event_store import EventCommand, EventStore

        record_id = "mem_" + suffix * 64
        EventStore(self.connection).append_and_project(
            EventCommand(
                workspace_id=workspace_id,
                stream_id=record_id,
                stream_kind="memory",
                event_type="memory.created",
                occurred_at_us=100,
                recorded_at_us=101,
                actor_type="system",
                payload={"record": self._record(content, rationale, tags, **changes)},
            )
        )
        return record_id

    def _update_record(
        self,
        suffix: str,
        content: str,
        rationale: str,
        tags: list[str],
        *,
        version: int = 2,
        event_type: str = "memory.updated",
        **changes,
    ):
        from daem0nmcp.event_store import EventCommand, EventStore

        record_id = "mem_" + suffix * 64
        return EventStore(self.connection).append_and_project(
            EventCommand(
                workspace_id=WORKSPACE_ID,
                stream_id=record_id,
                stream_kind="memory",
                event_type=event_type,
                occurred_at_us=100 + version,
                recorded_at_us=101 + version,
                actor_type="system",
                payload={"record": self._record(content, rationale, tags, **changes)},
                expected_stream_version=version,
            )
        )

    async def test_build_activates_generation_and_fts_search_returns_evidence(self):
        from daem0nmcp.retrieval.projections import LexicalProjectionBuilder
        from daem0nmcp.retrieval.providers import LexicalProvider
        from daem0nmcp.retrieval.types import RetrievalQuery

        durable = self._append_record(
            "1",
            "SQLite WAL transactions",
            "Durable local database writes",
            ["storage", "database"],
        )
        self._append_record(
            "2",
            "Graph traversal",
            "Bounded relationship walks",
            ["graph"],
        )
        result = LexicalProjectionBuilder(
            self.connection, clock_us=lambda: 500
        ).rebuild(WORKSPACE_ID)
        self.connection.commit()

        self.assertEqual("lexical", result.projection_name)
        self.assertEqual(1, result.generation)
        self.assertEqual("active", result.status)
        self.assertEqual(2, result.row_count)
        provider_result = await LexicalProvider(self.connection).search(
            RetrievalQuery(workspace_id=WORKSPACE_ID, text="durable database"),
            limit=10,
        )
        self.assertEqual("ready", provider_result.status)
        self.assertEqual(1, provider_result.manifest_generation)
        self.assertEqual(
            [durable],
            [candidate.evidence.record_id for candidate in provider_result.candidates],
        )
        self.assertEqual("lexical", provider_result.candidates[0].evidence.provider)
        self.assertEqual(
            self.connection.execute(
                "SELECT source_event_id FROM memory_records WHERE record_id=?",
                (durable,),
            ).fetchone()[0],
            provider_result.candidates[0].evidence.event_id,
        )

    async def test_multi_term_queries_are_recall_oriented_without_fts_operators(self):
        """A missing natural-language term must not erase otherwise relevant hits."""

        from daem0nmcp.retrieval.projections import LexicalProjectionBuilder
        from daem0nmcp.retrieval.providers import LexicalProvider
        from daem0nmcp.retrieval.types import RetrievalQuery

        tag_match = self._append_record(
            "a",
            "Pinned outbound address validation",
            "Reject private DNS answers before socket creation",
            ["ssrf"],
        )
        content_match = self._append_record(
            "b",
            "Layered request controls",
            "Bound every ingestion stage",
            ["network"],
        )
        LexicalProjectionBuilder(self.connection).rebuild(WORKSPACE_ID)
        self.connection.commit()

        result = await LexicalProvider(self.connection).search(
            RetrievalQuery(
                workspace_id=WORKSPACE_ID,
                text="ssrf controls",
                tags=frozenset({"ssrf"}),
            ),
            10,
        )

        self.assertEqual("ready", result.status)
        self.assertEqual(
            {tag_match, content_match},
            {candidate.evidence.record_id for candidate in result.candidates},
        )

    async def test_multi_term_queries_prefer_complete_matches_before_or_fallback(self):
        """A complete match must not be diluted by a one-token fallback hit."""

        from daem0nmcp.retrieval.projections import LexicalProjectionBuilder
        from daem0nmcp.retrieval.providers import LexicalProvider
        from daem0nmcp.retrieval.types import RetrievalQuery

        complete = self._append_record(
            "c",
            "Deprecated TF-IDF fallback",
            "Archived compatibility path",
            ["legacy"],
            archived=True,
        )
        self._append_record(
            "d",
            "Fallback behavior needs a warning",
            "Unrelated active record",
            ["warning"],
        )
        LexicalProjectionBuilder(self.connection).rebuild(WORKSPACE_ID)
        self.connection.commit()

        result = await LexicalProvider(self.connection).search(
            RetrievalQuery(
                workspace_id=WORKSPACE_ID,
                text="deprecated TF-IDF fallback",
            ),
            10,
        )

        self.assertEqual("ready", result.status)
        self.assertEqual(
            [complete],
            [candidate.evidence.record_id for candidate in result.candidates],
        )

    async def test_or_fallback_ignores_question_scaffolding(self):
        """Generic question words must not create lexical ranking noise."""

        from daem0nmcp.retrieval.projections import LexicalProjectionBuilder
        from daem0nmcp.retrieval.providers import LexicalProvider
        from daem0nmcp.retrieval.types import RetrievalQuery

        self._append_record(
            "e",
            "Do not enable an unsafe fallback",
            "A generic warning for a different subsystem",
            ["warning"],
        )
        LexicalProjectionBuilder(self.connection).rebuild(WORKSPACE_ID)
        self.connection.commit()

        result = await LexicalProvider(self.connection).search(
            RetrievalQuery(
                workspace_id=WORKSPACE_ID,
                text="How do we prevent server-side request forgery?",
            ),
            10,
        )

        self.assertEqual("ready", result.status)
        self.assertEqual((), result.candidates)

    async def test_rebuild_switches_atomically_and_dry_run_writes_nothing(self):
        from daem0nmcp.retrieval.projections import LexicalProjectionBuilder

        builder = LexicalProjectionBuilder(self.connection, clock_us=lambda: 500)
        self._append_record("3", "first", "initial", ["one"])
        first = builder.rebuild(WORKSPACE_ID)
        self.connection.commit()
        before = self.connection.total_changes

        preview = builder.rebuild(WORKSPACE_ID, dry_run=True)
        self.assertTrue(preview.dry_run)
        self.assertEqual(2, preview.generation)
        self.assertEqual("ready", getattr(preview, "capability_status", None))
        self.assertEqual(1, getattr(preview, "active_generation", None))
        self.assertEqual("active", getattr(preview, "active_status", None))
        self.assertEqual(1, getattr(preview, "active_row_count", None))
        self.assertEqual(0, getattr(preview, "row_count_delta", None))
        self.assertFalse(getattr(preview, "content_digest_changed", True))
        self.assertEqual(
            "retrieval_fts_0123456789abcdef01234567_g2",
            getattr(preview, "storage_target", None),
        )
        self.assertIsNotNone(getattr(preview, "active_manifest_id", None))
        self.assertIsNotNone(getattr(preview, "staging_manifest_id", None))
        self.assertEqual(before, self.connection.total_changes)
        self.assertEqual(
            [(1, "active")],
            [
                tuple(row)
                for row in self.connection.execute(
                    "SELECT generation,status FROM projection_manifests "
                    "WHERE workspace_id=? AND projection_name='lexical'",
                    (WORKSPACE_ID,),
                )
            ],
        )

        self._append_record("4", "second", "new generation", ["two"])
        second = builder.rebuild(WORKSPACE_ID)
        self.connection.commit()
        self.assertEqual(1, first.generation)
        self.assertEqual(2, second.generation)
        self.assertEqual(
            [(1, "ready"), (2, "active")],
            [
                tuple(row)
                for row in self.connection.execute(
                    "SELECT generation,status FROM projection_manifests "
                    "WHERE workspace_id=? AND projection_name='lexical' "
                    "ORDER BY generation",
                    (WORKSPACE_ID,),
                )
            ],
        )

    async def test_dry_run_fails_closed_when_fts5_capability_is_unavailable(self):
        from daem0nmcp.retrieval.projections import LexicalProjectionBuilder

        class MissingFtsBuilder(LexicalProjectionBuilder):
            def _probe_fts5(self) -> None:
                raise sqlite3.OperationalError("no such function: fts5_source_id")

        self._append_record("d", "dry run", "capability", ["fts"])
        before = self.connection.total_changes

        result = MissingFtsBuilder(self.connection).rebuild(WORKSPACE_ID, dry_run=True)

        self.assertEqual("unavailable", result.status)
        self.assertEqual("unavailable", result.capability_status)
        self.assertEqual(1, result.source_event_count)
        self.assertIsNotNone(result.source_high_water_event_id)
        self.assertEqual(101, result.source_high_water_recorded_at_us)

        self.assertEqual(before, self.connection.total_changes)
        self.assertEqual(
            0,
            self.connection.execute(
                "SELECT count(*) FROM projection_manifests WHERE workspace_id=?",
                (WORKSPACE_ID,),
            ).fetchone()[0],
        )

    async def test_staging_hash_mismatch_cannot_activate_with_matching_counts(self):
        from daem0nmcp.retrieval.projections import (
            LexicalProjectionBuilder,
            ProjectionBuildError,
        )

        self._append_record("e", "hash authority", "fixture", ["hash"])
        self.connection.execute(
            """
            CREATE TEMP TRIGGER corrupt_staged_retrieval_hash
            AFTER INSERT ON retrieval_documents
            WHEN NEW.projection_generation=1
            BEGIN
                UPDATE retrieval_documents SET content_hash=printf('%064d', 0)
                WHERE document_rowid=NEW.document_rowid;
            END
            """
        )

        with self.assertRaisesRegex(
            ProjectionBuildError, "PROJECTION_VALIDATION_FAILED"
        ):
            LexicalProjectionBuilder(self.connection).rebuild(WORKSPACE_ID)

        self.assertEqual(
            0,
            self.connection.execute(
                "SELECT count(*) FROM projection_manifests WHERE workspace_id=?",
                (WORKSPACE_ID,),
            ).fetchone()[0],
        )

    async def test_staging_fts_rowids_must_exactly_match_projected_documents(self):
        from daem0nmcp.retrieval.projections import (
            LexicalProjectionBuilder,
            ProjectionBuildError,
        )

        class MisalignedFtsBuilder(LexicalProjectionBuilder):
            def _populate_fts(
                self,
                fts_table: str,
                workspace_id: str,
                generation: int,
            ) -> None:
                super()._populate_fts(fts_table, workspace_id, generation)
                row = self.connection.execute(
                    f'SELECT rowid,content,rationale,tags_text FROM "{fts_table}" '
                    "ORDER BY rowid LIMIT 1"
                ).fetchone()
                assert row is not None
                self.connection.execute(
                    f'DELETE FROM "{fts_table}" WHERE rowid=?', (row[0],)
                )
                self.connection.execute(
                    f'INSERT INTO "{fts_table}"('
                    "rowid,content,rationale,tags_text) VALUES (?,?,?,?)",
                    (int(row[0]) + 10_000, row[1], row[2], row[3]),
                )

        self._append_record("f", "row identity", "fixture", ["fts"])

        with self.assertRaisesRegex(
            ProjectionBuildError, "PROJECTION_VALIDATION_FAILED"
        ):
            MisalignedFtsBuilder(self.connection).rebuild(WORKSPACE_ID)

        self.assertEqual(
            0,
            self.connection.execute(
                "SELECT count(*) FROM projection_manifests WHERE workspace_id=?",
                (WORKSPACE_ID,),
            ).fetchone()[0],
        )

    async def test_missing_fts_is_explicitly_unavailable_without_like_fallback(self):
        from daem0nmcp.retrieval.projections import LexicalProjectionBuilder
        from daem0nmcp.retrieval.providers import LexicalProvider
        from daem0nmcp.retrieval.types import RetrievalQuery

        self._append_record("5", "searchable phrase", "fixture", ["tag"])
        LexicalProjectionBuilder(self.connection).rebuild(WORKSPACE_ID)
        self.connection.commit()
        details = json.loads(
            self.connection.execute(
                "SELECT details_json FROM projection_manifests "
                "WHERE workspace_id=? AND projection_name='lexical' "
                "AND status='active'",
                (WORKSPACE_ID,),
            ).fetchone()[0]
        )
        self.connection.execute(f'DROP TABLE "{details["fts_table"]}"')

        provider = LexicalProvider(self.connection)
        for text in ("searchable", "", "bad\x00query"):
            with self.subTest(text=repr(text)):
                result = await provider.search(
                    RetrievalQuery(workspace_id=WORKSPACE_ID, text=text),
                    limit=10,
                )
                self.assertEqual("unavailable", result.status)
                self.assertEqual("LEXICAL_UNAVAILABLE", result.reason)
                self.assertEqual((), result.candidates)

    async def test_failed_staging_build_leaves_prior_generation_active(self):
        from daem0nmcp.retrieval.projections import (
            LexicalProjectionBuilder,
            ProjectionBuildError,
        )

        self._append_record("6", "stable generation", "fixture", ["stable"])
        builder = LexicalProjectionBuilder(self.connection, clock_us=lambda: 500)
        builder.rebuild(WORKSPACE_ID)
        self.connection.commit()
        self.connection.execute(
            """
            CREATE TEMP TRIGGER reject_second_lexical_generation
            BEFORE INSERT ON retrieval_documents
            WHEN NEW.projection_generation=2
            BEGIN SELECT RAISE(ABORT, 'fixture rejection'); END
            """
        )
        self.connection.commit()
        self._append_record("7", "staging record", "fixture", ["staging"])

        with self.assertRaisesRegex(ProjectionBuildError, "LEXICAL_UNAVAILABLE"):
            builder.rebuild(WORKSPACE_ID)
        self.assertEqual(
            [(1, "active")],
            [
                tuple(row)
                for row in self.connection.execute(
                    "SELECT generation,status FROM projection_manifests "
                    "WHERE workspace_id=? AND projection_name='lexical'",
                    (WORKSPACE_ID,),
                )
            ],
        )
        self.assertEqual(
            0,
            self.connection.execute(
                "SELECT count(*) FROM retrieval_documents "
                "WHERE workspace_id=? AND projection_generation=2",
                (WORKSPACE_ID,),
            ).fetchone()[0],
        )

    async def test_only_active_generation_is_searched_and_fts_operators_are_data(self):
        from daem0nmcp.event_store import EventCommand, EventStore
        from daem0nmcp.retrieval.projections import LexicalProjectionBuilder
        from daem0nmcp.retrieval.providers import LexicalProvider
        from daem0nmcp.retrieval.types import RetrievalQuery

        record_id = self._append_record("8", "obsoleteword", "first", ["old"])
        builder = LexicalProjectionBuilder(self.connection, clock_us=lambda: 500)
        builder.rebuild(WORKSPACE_ID)
        EventStore(self.connection).append_and_project(
            EventCommand(
                workspace_id=WORKSPACE_ID,
                stream_id=record_id,
                stream_kind="memory",
                event_type="memory.updated",
                occurred_at_us=200,
                recorded_at_us=201,
                actor_type="system",
                payload={"record": self._record("replacementword", "second", ["new"])},
            )
        )
        builder.rebuild(WORKSPACE_ID)
        self.connection.commit()
        provider = LexicalProvider(self.connection)

        old = await provider.search(
            RetrievalQuery(workspace_id=WORKSPACE_ID, text="obsoleteword"), 10
        )
        new = await provider.search(
            RetrievalQuery(workspace_id=WORKSPACE_ID, text="replacementword"), 10
        )
        operator_text = await provider.search(
            RetrievalQuery(workspace_id=WORKSPACE_ID, text='" OR * NOT'), 10
        )
        self.assertEqual((), old.candidates)
        self.assertEqual(
            [record_id], [item.evidence.record_id for item in new.candidates]
        )
        self.assertEqual("ready", operator_text.status)
        self.assertEqual((), operator_text.candidates)
        self.assertEqual(
            2,
            self.connection.execute(
                "SELECT generation FROM projection_manifests "
                "WHERE workspace_id=? AND projection_name='lexical' "
                "AND status='active'",
                (WORKSPACE_ID,),
            ).fetchone()[0],
        )

    async def test_manifest_details_bind_builder_configuration_and_content(self):
        from daem0nmcp.retrieval.projections import LexicalProjectionBuilder
        from daem0nmcp.retrieval.providers import LexicalProvider
        from daem0nmcp.retrieval.types import RetrievalQuery

        self._append_record("9", "manifest content", "fixture", ["manifest"])
        result = LexicalProjectionBuilder(
            self.connection, clock_us=lambda: 500
        ).rebuild(WORKSPACE_ID)
        details = json.loads(
            self.connection.execute(
                "SELECT details_json FROM projection_manifests "
                "WHERE workspace_id=? AND projection_name='lexical' "
                "AND status='active'",
                (WORKSPACE_ID,),
            ).fetchone()[0]
        )
        self.assertEqual(result.build_config_hash, details["build_config_hash"])
        self.assertEqual(result.content_digest, details["content_digest"])
        self.assertEqual("lexical", details["projection"])

        self.connection.execute(
            "UPDATE projection_manifests SET details_json=? "
            "WHERE workspace_id=? AND projection_name='lexical' "
            "AND status='active'",
            (
                json.dumps({**details, "build_config_hash": "0" * 64}),
                WORKSPACE_ID,
            ),
        )
        stale = await LexicalProvider(self.connection).search(
            RetrievalQuery(workspace_id=WORKSPACE_ID, text="manifest"),
            10,
        )
        self.assertEqual("unavailable", stale.status)
        self.assertEqual("LEXICAL_UNAVAILABLE", stale.reason)

    async def test_canonical_append_invalidates_and_queues_stale_lexical_projection(
        self,
    ):
        from daem0nmcp.retrieval.projections import LexicalProjectionBuilder
        from daem0nmcp.retrieval.providers import LexicalProvider
        from daem0nmcp.retrieval.types import RetrievalQuery

        builder = LexicalProjectionBuilder(self.connection, clock_us=lambda: 500)
        self._append_record("c", "alpha baseline", "fixture", ["initial"])
        builder.rebuild(WORKSPACE_ID)
        self.connection.execute(
            """
            INSERT INTO projection_manifests (
                manifest_id,workspace_id,projection_name,generation,
                projection_version,status,source_event_count,
                source_event_root_hash,row_count,builder_version,details_json,
                started_at_us,completed_at_us,activated_at_us
            ) VALUES (?,?,'dense',1,1,'active',1,?,1,'dense-fixture',?,1,1,1)
            """,
            (
                "prj_" + "9" * 64,
                WORKSPACE_ID,
                "9" * 64,
                json.dumps({"projection": "dense"}),
            ),
        )
        self.connection.commit()

        new_record = self._append_record(
            "d", "novelterm added later", "fixture", ["new"]
        )
        self._append_record("e", "another canonical write", "fixture", ["new"])
        self.connection.commit()

        self.assertEqual(
            [(1, "active")],
            [
                tuple(row)
                for row in self.connection.execute(
                    "SELECT generation,status FROM projection_manifests "
                    "WHERE workspace_id=? AND projection_name='lexical'",
                    (WORKSPACE_ID,),
                )
            ],
        )
        stale_details = json.loads(
            self.connection.execute(
                "SELECT details_json FROM projection_manifests "
                "WHERE workspace_id=? AND projection_name='lexical' "
                "AND status='active'",
                (WORKSPACE_ID,),
            ).fetchone()[0]
        )
        latest_event_id = str(
            self.connection.execute(
                "SELECT event_id FROM memory_events WHERE stream_id=?",
                ("mem_" + "e" * 64,),
            ).fetchone()[0]
        )
        self.assertEqual(
            latest_event_id, stale_details.get("rebuild_required_event_id")
        )
        self.assertEqual(
            2,
            self.connection.execute(
                "SELECT COUNT(*) FROM background_jobs WHERE workspace_id=? "
                "AND job_type='retrieval.projection_rebuild'",
                (WORKSPACE_ID,),
            ).fetchone()[0],
        )
        job = self.connection.execute(
            "SELECT status,job_type,payload_json,source_event_id,available_at_us "
            "FROM background_jobs WHERE workspace_id=? "
            "AND idempotency_key='active-projection:lexical'",
            (WORKSPACE_ID,),
        ).fetchone()
        self.assertIsNotNone(job)
        self.assertEqual(("queued", "retrieval.projection_rebuild"), tuple(job[:2]))
        self.assertEqual(
            {
                "projection_names": ["lexical"],
                "source_event_id": job[3],
                "workspace_id": WORKSPACE_ID,
            },
            json.loads(job[2]),
        )
        self.assertEqual(5_000_101, job[4])
        self.assertEqual(
            [("active-projection:lexical", 100), ("active-projection:dense", 50)],
            [
                tuple(row)
                for row in self.connection.execute(
                    "SELECT idempotency_key,priority FROM background_jobs "
                    "ORDER BY priority DESC"
                )
            ],
        )
        stale = await LexicalProvider(self.connection).search(
            RetrievalQuery(workspace_id=WORKSPACE_ID, text="novelterm"), 10
        )
        self.assertEqual("degraded", stale.status)
        self.assertEqual("LEXICAL_REBUILD_REQUIRED", stale.reason)
        self.assertEqual(
            [new_record],
            [item.evidence.record_id for item in stale.candidates],
        )
        manifest = self.connection.execute(
            "SELECT source_event_count,row_count FROM projection_manifests "
            "WHERE workspace_id=? AND projection_name='lexical' "
            "AND status='active'",
            (WORKSPACE_ID,),
        ).fetchone()
        self.assertEqual((1, 3), tuple(manifest))
        retained = await LexicalProvider(self.connection).search(
            RetrievalQuery(workspace_id=WORKSPACE_ID, text="alpha"), 10
        )
        self.assertEqual("degraded", retained.status)
        self.assertEqual(
            ["mem_" + "c" * 64],
            [item.evidence.record_id for item in retained.candidates],
        )

        rebuilt = builder.rebuild(WORKSPACE_ID)
        self.connection.commit()
        fresh = await LexicalProvider(self.connection).search(
            RetrievalQuery(workspace_id=WORKSPACE_ID, text="novelterm"), 10
        )
        self.assertEqual(2, rebuilt.generation)
        self.assertEqual(
            [new_record],
            [item.evidence.record_id for item in fresh.candidates],
        )

    async def test_active_delta_tracks_update_archive_pin_outcome_and_delete(self):
        from daem0nmcp.retrieval.projections import LexicalProjectionBuilder
        from daem0nmcp.retrieval.providers import LexicalProvider
        from daem0nmcp.retrieval.types import RetrievalQuery

        record_id = self._append_record("f", "oldterm", "old", ["before"])
        LexicalProjectionBuilder(self.connection).rebuild(WORKSPACE_ID)
        self.connection.commit()

        updated = self._update_record(
            "f",
            "newterm",
            "new rationale",
            ["after"],
            pinned=True,
            archived=True,
            outcome="verified",
            worked=True,
        )
        self.connection.commit()
        provider = LexicalProvider(self.connection)
        result = await provider.search(
            RetrievalQuery(workspace_id=WORKSPACE_ID, text="newterm"), 10
        )
        document = self.connection.execute(
            "SELECT content,rationale,tags_text,archived,source_event_id "
            "FROM retrieval_documents WHERE workspace_id=? AND record_id=?",
            (WORKSPACE_ID, record_id),
        ).fetchone()

        self.assertEqual(
            [record_id], [row.evidence.record_id for row in result.candidates]
        )
        self.assertEqual(
            ("newterm", "new rationale", "after", 1, updated.event_id),
            tuple(document),
        )

        self._update_record(
            "f",
            "newterm",
            "new rationale",
            ["after"],
            version=3,
            event_type="memory.deleted",
            pinned=True,
            archived=True,
            outcome="verified",
            worked=True,
            deleted_at_us=103,
        )
        self.connection.commit()
        deleted = await provider.search(
            RetrievalQuery(workspace_id=WORKSPACE_ID, text="newterm"), 10
        )
        manifest_count = self.connection.execute(
            "SELECT row_count FROM projection_manifests WHERE workspace_id=? "
            "AND projection_name='lexical' AND status='active'",
            (WORKSPACE_ID,),
        ).fetchone()[0]

        self.assertEqual((), deleted.candidates)
        self.assertEqual(0, manifest_count)
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM retrieval_documents WHERE workspace_id=? "
                "AND record_id=?",
                (WORKSPACE_ID, record_id),
            ).fetchone()
        )

    def test_active_delta_fts_failure_rolls_back_the_canonical_append(self):
        from daem0nmcp.retrieval.projections import (
            LexicalProjectionBuilder,
            ProjectionBuildError,
        )

        record_id = self._append_record("7", "baseline", "old", ["before"])
        LexicalProjectionBuilder(self.connection).rebuild(WORKSPACE_ID)
        self.connection.commit()
        before = tuple(
            self.connection.execute(
                "SELECT content,stream_version,source_event_id FROM memory_records "
                "WHERE record_id=?",
                (record_id,),
            ).fetchone()
        )
        event_count = self.connection.execute(
            "SELECT count(*) FROM memory_events WHERE workspace_id=?",
            (WORKSPACE_ID,),
        ).fetchone()[0]
        self.connection.execute(
            "CREATE TRIGGER fail_lexical_delta BEFORE UPDATE ON retrieval_documents "
            "BEGIN SELECT RAISE(ABORT,'forced lexical failure'); END"
        )

        with self.assertRaises(ProjectionBuildError) as raised:
            self._update_record("7", "changed", "new", ["after"])

        self.assertEqual("LEXICAL_UNAVAILABLE", raised.exception.code)
        self.assertEqual(
            before,
            tuple(
                self.connection.execute(
                    "SELECT content,stream_version,source_event_id FROM memory_records "
                    "WHERE record_id=?",
                    (record_id,),
                ).fetchone()
            ),
        )
        self.assertEqual(
            event_count,
            self.connection.execute(
                "SELECT count(*) FROM memory_events WHERE workspace_id=?",
                (WORKSPACE_ID,),
            ).fetchone()[0],
        )

    def test_incompatible_active_manifest_queues_rebuild_without_delta(self):
        from daem0nmcp.retrieval.projections import LexicalProjectionBuilder

        self._append_record("8", "baseline", "old", ["before"])
        LexicalProjectionBuilder(self.connection).rebuild(WORKSPACE_ID)
        details = json.loads(
            self.connection.execute(
                "SELECT details_json FROM projection_manifests WHERE workspace_id=? "
                "AND projection_name='lexical' AND status='active'",
                (WORKSPACE_ID,),
            ).fetchone()[0]
        )
        self.connection.execute(
            "UPDATE projection_manifests SET details_json=? WHERE workspace_id=? "
            "AND projection_name='lexical' AND status='active'",
            (json.dumps({**details, "build_config_hash": "0" * 64}), WORKSPACE_ID),
        )
        self.connection.commit()

        record_id = self._append_record("9", "queued fallback", "new", ["after"])
        self.connection.commit()

        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM retrieval_documents WHERE workspace_id=? "
                "AND record_id=?",
                (WORKSPACE_ID, record_id),
            ).fetchone()
        )
        job = self.connection.execute(
            "SELECT status,source_event_id,available_at_us,created_at_us "
            "FROM background_jobs WHERE workspace_id=? "
            "AND idempotency_key='active-projection:lexical'",
            (WORKSPACE_ID,),
        ).fetchone()
        self.assertEqual("queued", job[0])
        self.assertIsNotNone(job[1])
        self.assertEqual(job[3], job[2])

    def test_cold_workspace_queues_immediate_lexical_rebuild(self):
        record_id = self._append_record(
            "0", "cold lexical fallback", "no active generation", ["cold"]
        )
        self.connection.commit()

        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM retrieval_documents WHERE workspace_id=? "
                "AND record_id=?",
                (WORKSPACE_ID, record_id),
            ).fetchone()
        )
        job = self.connection.execute(
            "SELECT status,available_at_us,created_at_us,payload_json "
            "FROM background_jobs WHERE workspace_id=? "
            "AND idempotency_key='active-projection:lexical'",
            (WORKSPACE_ID,),
        ).fetchone()
        self.assertEqual(("queued", 101, 101), tuple(job[:3]))
        self.assertEqual(["lexical"], json.loads(job[3])["projection_names"])

    async def test_bm25_corpus_isolated_by_workspace_and_generation(self):
        from daem0nmcp.retrieval.projections import LexicalProjectionBuilder
        from daem0nmcp.retrieval.providers import LexicalProvider
        from daem0nmcp.retrieval.types import RetrievalQuery

        record_a = self._append_record(
            "a",
            "alpha " * 8 + "beta",
            "active a",
            ["active"],
        )
        record_b = self._append_record(
            "b",
            "alpha " + "beta " * 8,
            "active b",
            ["active"],
        )
        builder = LexicalProjectionBuilder(self.connection, clock_us=lambda: 500)
        builder.rebuild(WORKSPACE_ID)
        self.connection.commit()
        provider = LexicalProvider(self.connection)
        query = RetrievalQuery(workspace_id=WORKSPACE_ID, text="alpha beta")
        before = await provider.search(query, 10)

        # A large unrelated corpus must not alter either active ranking or its
        # BM25 diagnostics.  The shared-table implementation lets it do both.
        for index in range(100):
            generation = 100 + index
            for record_id, content in (
                (record_a, "alpha filler"),
                (record_b, "alpha filler"),
            ):
                source = self.connection.execute(
                    "SELECT source_event_id,content_hash FROM memory_records "
                    "WHERE record_id=?",
                    (record_id,),
                ).fetchone()
                self.connection.execute(
                    """
                    INSERT INTO retrieval_documents (
                        workspace_id,projection_generation,record_id,content,
                        rationale,tags_text,category,valid_from_us,valid_to_us,
                        transaction_from_us,transaction_to_us,visibility,
                        archived,content_hash,source_event_id
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        OTHER_WORKSPACE_ID,
                        generation,
                        record_id,
                        content,
                        "pollution",
                        "other",
                        "decision",
                        None,
                        None,
                        100,
                        None,
                        "workspace",
                        0,
                        source[1],
                        source[0],
                    ),
                )
        self.connection.commit()

        after = await provider.search(query, 10)
        before_view = tuple(
            (item.evidence.record_id, item.raw_score) for item in before.candidates
        )
        after_view = tuple(
            (item.evidence.record_id, item.raw_score) for item in after.candidates
        )
        self.assertEqual(before_view, after_view)
        details = json.loads(
            self.connection.execute(
                "SELECT details_json FROM projection_manifests "
                "WHERE workspace_id=? AND projection_name='lexical' "
                "AND status='active'",
                (WORKSPACE_ID,),
            ).fetchone()[0]
        )
        fts_table = details["fts_table"]
        self.assertRegex(
            fts_table,
            r"^retrieval_fts_[0-9a-f]{24}_g[1-9][0-9]*$",
        )
        self.assertIsNotNone(
            self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (fts_table,),
            ).fetchone()
        )

    async def test_build_snapshot_starts_before_canonical_record_read(self):
        from daem0nmcp.event_store import EventCommand, EventStore
        from daem0nmcp.retrieval.projections import LexicalProjectionBuilder

        with tempfile.TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "projection-race.sqlite3"
            first = sqlite3.connect(database_path, timeout=0.01)
            second = sqlite3.connect(database_path, timeout=0.01)
            try:
                first.execute("PRAGMA foreign_keys=ON")
                second.execute("PRAGMA foreign_keys=ON")
                for version in (16, 17, 18):
                    _apply_migration(first, version)
                first.commit()
                EventStore(first).append_and_project(
                    EventCommand(
                        workspace_id=WORKSPACE_ID,
                        stream_id="mem_" + "1" * 64,
                        stream_kind="memory",
                        event_type="memory.created",
                        occurred_at_us=100,
                        recorded_at_us=101,
                        actor_type="system",
                        payload={"record": self._record("first", "race", ["one"])},
                    )
                )
                first.commit()

                class InterleavingBuilder(LexicalProjectionBuilder):
                    writer_blocked = False

                    def _records(self, workspace_id):
                        records = super()._records(workspace_id)
                        try:
                            EventStore(second).append_and_project(
                                EventCommand(
                                    workspace_id=WORKSPACE_ID,
                                    stream_id="mem_" + "2" * 64,
                                    stream_kind="memory",
                                    event_type="memory.created",
                                    occurred_at_us=200,
                                    recorded_at_us=201,
                                    actor_type="system",
                                    payload={
                                        "record": self_outer._record(
                                            "second", "race", ["two"]
                                        )
                                    },
                                )
                            )
                            second.commit()
                        except sqlite3.OperationalError:
                            second.rollback()
                            self.writer_blocked = True
                        return records

                self_outer = self
                builder = InterleavingBuilder(first, clock_us=lambda: 500)
                result = builder.rebuild(WORKSPACE_ID)
                first.commit()

                self.assertEqual(result.row_count, result.source_event_count)
                self.assertTrue(builder.writer_blocked)
            finally:
                first.close()
                second.close()

    async def test_unknown_visibility_fails_closed_without_staging_rows(self):
        from daem0nmcp.retrieval.projections import (
            LexicalProjectionBuilder,
            ProjectionBuildError,
        )

        self._append_record(
            "c",
            "classified",
            "must not widen",
            ["private"],
            context={"visibility": "confidential"},
        )

        with self.assertRaises(ProjectionBuildError) as raised:
            LexicalProjectionBuilder(self.connection).rebuild(WORKSPACE_ID)
        self.assertEqual("INVALID_RECORD_VISIBILITY", raised.exception.code)
        self.assertEqual(
            0,
            self.connection.execute(
                "SELECT count(*) FROM projection_manifests"
            ).fetchone()[0],
        )

    async def test_malformed_unicode_queries_are_safe_no_results(self):
        from daem0nmcp.retrieval.projections import LexicalProjectionBuilder
        from daem0nmcp.retrieval.providers import LexicalProvider
        from daem0nmcp.retrieval.types import RetrievalQuery

        self._append_record("d", "searchable", "fixture", ["query"])
        LexicalProjectionBuilder(self.connection).rebuild(WORKSPACE_ID)
        self.connection.commit()
        provider = LexicalProvider(self.connection)

        for malformed in ("bad\x00query", "bad\ud800query"):
            with self.subTest(malformed=repr(malformed)):
                result = await provider.search(
                    RetrievalQuery(workspace_id=WORKSPACE_ID, text=malformed),
                    10,
                )
                self.assertEqual("ready", result.status)
                self.assertEqual("INVALID_FTS_QUERY", result.reason)
                self.assertEqual((), result.candidates)

    async def test_lexical_sql_runs_off_loop_under_a_total_timeout(self):
        from daem0nmcp.retrieval.projections import LexicalProjectionBuilder
        from daem0nmcp.retrieval.providers import LexicalProvider
        from daem0nmcp.retrieval.types import RetrievalQuery

        self._append_record("e", "searchable", "fixture", ["timeout"])
        LexicalProjectionBuilder(self.connection).rebuild(WORKSPACE_ID)
        started = threading.Event()
        release = threading.Event()
        heartbeat = []

        class BlockingProvider(LexicalProvider):
            def _search_sync(self, query, limit, started_ns):
                started.set()
                release.wait(timeout=2)
                return super()._search_sync(query, limit, started_ns)

        async def pulse():
            for _ in range(3):
                await asyncio.sleep(0.005)
                heartbeat.append("tick")

        pulse_task = asyncio.create_task(pulse())
        try:
            result = await BlockingProvider(
                self.connection,
                timeout_seconds=0.02,
            ).search(
                RetrievalQuery(workspace_id=WORKSPACE_ID, text="searchable"),
                10,
            )
        finally:
            release.set()
            await pulse_task

        self.assertTrue(started.is_set())
        self.assertTrue(heartbeat)
        self.assertEqual("unavailable", result.status)
        self.assertEqual("LEXICAL_TIMEOUT", result.reason)


if __name__ == "__main__":
    unittest.main()
