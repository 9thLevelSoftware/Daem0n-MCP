from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

WORKSPACE_ID = "ws_0123456789abcdef01234567"


def _apply_v7_schema(connection: sqlite3.Connection) -> None:
    from daem0nmcp.migrations.schema import MIGRATIONS
    from daem0nmcp.schema_version import CURRENT_SCHEMA_VERSION

    connection.execute("CREATE TABLE schema_version(version INTEGER PRIMARY KEY)")
    for version, _description, statements in MIGRATIONS:
        if 16 <= version <= CURRENT_SCHEMA_VERSION:
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_version(version) VALUES (?)", (version,)
            )
    connection.commit()


class DiscoveryProjectionSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "discovery.db"
        self.connection = sqlite3.connect(self.database)
        self.addCleanup(self.connection.close)
        self.connection.execute("PRAGMA foreign_keys=ON")

    def test_migration_22_publishes_all_canonical_discovery_tables(self) -> None:
        """Missing a generation table would force a handler back onto v6 paths."""
        from daem0nmcp.migrations.schema import MIGRATIONS
        from daem0nmcp.schema_version import CURRENT_SCHEMA_VERSION

        self.assertGreaterEqual(CURRENT_SCHEMA_VERSION, 22)
        self.assertEqual(
            22,
            next(
                version for version, _description, _sql in MIGRATIONS if version == 22
            ),
        )
        _apply_v7_schema(self.connection)

        present = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertTrue(
            {
                "discovery_projection_partitions",
                "discovery_entities",
                "discovery_entity_records",
                "discovery_communities",
                "discovery_community_members",
                "discovery_code_entities",
            }
            <= present
        )

    def test_code_is_a_real_wire_projection_name(self) -> None:
        """Without the code discriminant CodeIndexData cannot name its manifest."""
        from datetime import datetime, timezone

        from pydantic import ValidationError

        from daem0nmcp.api.v7.tools import (
            ProjectionManifest,
            ProjectionRebuildInput,
        )

        manifest = ProjectionManifest(
            projection="code",
            generation=1,
            built_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
            source_root_hash="a" * 64,
        )
        self.assertEqual("code", manifest.projection)

        with self.assertRaises(ValidationError):
            ProjectionRebuildInput(
                workspace_id=WORKSPACE_ID,
                projection="code",
            )

    def test_session_brief_can_report_all_seven_projection_manifests(self) -> None:
        """Adding code must not silently evict another active projection."""
        from datetime import datetime, timezone

        from daem0nmcp.api.v7.tools import ProjectionManifest, SessionBriefData

        built_at = datetime(2026, 8, 9, tzinfo=timezone.utc)
        names = (
            "lexical",
            "dense",
            "graph",
            "temporal",
            "procedure",
            "outcome",
            "code",
        )
        brief = SessionBriefData(
            workspace_id=WORKSPACE_ID,
            briefed_at=built_at,
            workspace_statistics={},
            projection_freshness=[
                ProjectionManifest(
                    projection=name,
                    generation=1,
                    built_at=built_at,
                    source_root_hash=f"{index:064x}",
                )
                for index, name in enumerate(names, 1)
            ],
        )
        self.assertEqual(
            names, tuple(item.projection for item in brief.projection_freshness)
        )


class DiscoveryProjectionBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.database = self.root / "discovery.db"
        self.connection = sqlite3.connect(self.database)
        self.addCleanup(self.connection.close)
        self.connection.execute("PRAGMA foreign_keys=ON")
        _apply_v7_schema(self.connection)
        self.record_id = self._append_record("a", "Authentication decision")
        self._rebuild_graph()

    def _append_record(self, suffix: str, content: str) -> str:
        from daem0nmcp.event_store import EventCommand, EventStore

        record_id = "mem_" + suffix * 64
        EventStore(self.connection).append_and_project(
            EventCommand(
                workspace_id=WORKSPACE_ID,
                stream_id=record_id,
                stream_kind="memory",
                event_type="memory.created",
                occurred_at_us=100,
                recorded_at_us=200 + ord(suffix),
                actor_type="system",
                payload={
                    "record": {
                        "record_type": "decision",
                        "legacy_type": None,
                        "content": content,
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
                        "source_client": "discovery-test",
                        "source_model": None,
                        "deleted_at_us": None,
                    }
                },
            )
        )
        self.connection.commit()
        return record_id

    def _rebuild_graph(self) -> int:
        from daem0nmcp.retrieval.specialized_projection import (
            SpecializedProjectionBuilder,
        )

        return (
            SpecializedProjectionBuilder(self.connection, clock_us=lambda: 900)
            .rebuild(WORKSPACE_ID, "graph")
            .generation
        )

    @staticmethod
    def _seeds(record_id: str):
        from daem0nmcp.discovery_projection import (
            CommunityProjectionSeed,
            EntityProjectionSeed,
            EntityRecordSeed,
        )

        return (
            (
                EntityProjectionSeed(
                    name="Authentication",
                    entity_type="concept",
                    records=(EntityRecordSeed(record_id, 2),),
                ),
            ),
            (
                CommunityProjectionSeed(
                    source_key="auth-parent",
                    label="Authentication",
                    level=1,
                    member_record_ids=(record_id,),
                ),
                CommunityProjectionSeed(
                    source_key="auth-leaf",
                    label="Session authentication",
                    level=0,
                    parent_source_key="auth-parent",
                    member_record_ids=(record_id,),
                ),
            ),
        )

    def test_graph_ids_are_stable_or_generation_bound_and_rows_are_immutable(
        self,
    ) -> None:
        """A graph rebuild must not retarget a community or rename an entity."""
        from daem0nmcp.discovery_projection import DiscoveryProjectionBuilder

        entities, communities = self._seeds(self.record_id)
        builder = DiscoveryProjectionBuilder(self.connection, clock_us=lambda: 1_000)
        first = builder.populate_graph(
            WORKSPACE_ID,
            entities=entities,
            communities=communities,
        )
        second_record = self._append_record("b", "Session implementation")
        self.assertNotEqual(self.record_id, second_record)
        self._rebuild_graph()
        second = builder.populate_graph(
            WORKSPACE_ID,
            entities=entities,
            communities=communities,
        )

        self.assertEqual(first.entity_ids, second.entity_ids)
        self.assertNotEqual(first.community_ids, second.community_ids)
        self.assertEqual((1, 2), (first.graph_generation, second.graph_generation))
        with self.assertRaises(sqlite3.DatabaseError):
            self.connection.execute(
                "UPDATE discovery_entities SET name='Retargeted' "
                "WHERE workspace_id=? AND graph_generation=1",
                (WORKSPACE_ID,),
            )

    def test_graph_build_rolls_back_rows_and_public_ids_on_invalid_parent(self) -> None:
        """A bad hierarchy must not strand identifiers outside its generation."""
        from daem0nmcp.discovery_projection import (
            CommunityProjectionSeed,
            DiscoveryProjectionBuilder,
            DiscoveryProjectionBuildError,
        )

        before = self.connection.execute(
            "SELECT count(*) FROM public_object_ids"
        ).fetchone()[0]
        with self.assertRaises(DiscoveryProjectionBuildError) as raised:
            DiscoveryProjectionBuilder(self.connection).populate_graph(
                WORKSPACE_ID,
                entities=(),
                communities=(
                    CommunityProjectionSeed(
                        source_key="orphan",
                        label="Orphan",
                        level=0,
                        parent_source_key="missing",
                        member_record_ids=(self.record_id,),
                    ),
                ),
            )
        self.assertEqual("INVALID_DISCOVERY_SEED", raised.exception.code)
        self.assertEqual(
            before,
            self.connection.execute(
                "SELECT count(*) FROM public_object_ids"
            ).fetchone()[0],
        )
        self.assertEqual(
            0,
            self.connection.execute(
                "SELECT count(*) FROM discovery_communities"
            ).fetchone()[0],
        )

    def test_code_rebuild_binds_ids_to_generation_and_rejects_unsafe_paths(
        self,
    ) -> None:
        """A code ID cannot survive a rebuild or carry a path outside the workspace."""
        from daem0nmcp.discovery_projection import (
            CodeEntityProjectionSeed,
            DiscoveryProjectionBuilder,
            DiscoveryProjectionBuildError,
        )

        builder = DiscoveryProjectionBuilder(self.connection, clock_us=lambda: 2_000)
        seed = CodeEntityProjectionSeed(
            source_key="legacy-code-1",
            kind="function",
            qualified_name="auth.login",
            relative_file_path="src/auth.py",
            start_line=10,
            end_line=20,
        )
        first = builder.rebuild_code(WORKSPACE_ID, entities=(seed,))
        mappings_after_first = self.connection.execute(
            "SELECT count(*) FROM public_object_ids WHERE object_kind='code'"
        ).fetchone()[0]
        reused = builder.rebuild_code(WORKSPACE_ID, entities=(seed,))
        self.assertEqual(
            mappings_after_first,
            self.connection.execute(
                "SELECT count(*) FROM public_object_ids WHERE object_kind='code'"
            ).fetchone()[0],
        )
        second = builder.rebuild_code(WORKSPACE_ID, entities=(seed,), force=True)

        self.assertTrue(reused.reused)
        self.assertEqual(first.code_entity_ids, reused.code_entity_ids)
        self.assertNotEqual(first.code_entity_ids, second.code_entity_ids)
        self.assertEqual((1, 2), (first.code_generation, second.code_generation))
        roots = [
            row[0]
            for row in self.connection.execute(
                "SELECT source_event_root_hash FROM projection_manifests "
                "WHERE workspace_id=? AND projection_name='code' "
                "ORDER BY generation",
                (WORKSPACE_ID,),
            )
        ]
        self.assertEqual(2, len(roots))
        self.assertNotEqual(
            roots[0],
            roots[1],
            "a code partition digest must bind its generation-scoped public IDs",
        )
        with self.assertRaises(DiscoveryProjectionBuildError) as raised:
            builder.rebuild_code(
                WORKSPACE_ID,
                entities=(
                    CodeEntityProjectionSeed(
                        source_key="unsafe",
                        kind="file",
                        qualified_name="secret",
                        relative_file_path="../secret.py",
                        start_line=1,
                        end_line=1,
                    ),
                ),
                force=True,
            )
        self.assertEqual("INVALID_RELATIVE_PATH", raised.exception.code)

    def _install_legacy_rows(self, *, unsafe_code_path: bool = False) -> str:
        from daem0nmcp.event_store import sha256_json

        self.connection.executescript(
            """
            CREATE TABLE extracted_entities(
                id INTEGER PRIMARY KEY, project_path TEXT NOT NULL,
                entity_type TEXT NOT NULL, name TEXT NOT NULL,
                qualified_name TEXT, mention_count INTEGER NOT NULL
            );
            CREATE TABLE memory_entity_refs(
                id INTEGER PRIMARY KEY, memory_id INTEGER NOT NULL,
                entity_id INTEGER NOT NULL
            );
            CREATE TABLE memory_communities(
                id INTEGER PRIMARY KEY, project_path TEXT NOT NULL,
                name TEXT NOT NULL, member_count INTEGER NOT NULL,
                member_ids TEXT NOT NULL, level INTEGER NOT NULL,
                parent_id INTEGER
            );
            CREATE TABLE code_entities(
                id TEXT PRIMARY KEY, project_path TEXT NOT NULL,
                entity_type TEXT NOT NULL, name TEXT NOT NULL,
                qualified_name TEXT, file_path TEXT NOT NULL,
                line_start INTEGER, line_end INTEGER
            );
            """
        )
        project = str(self.root)
        code_path = (
            str(self.root.parent / "outside.py")
            if unsafe_code_path
            else str(self.root / "src" / "auth.py")
        )
        self.connection.execute(
            "INSERT INTO extracted_entities VALUES (1,?,'concept','Authentication',NULL,2)",
            (project,),
        )
        self.connection.execute("INSERT INTO memory_entity_refs VALUES (1,7,1)")
        self.connection.execute(
            "INSERT INTO memory_communities VALUES (1,?,'Authentication',1,'[7]',0,NULL)",
            (project,),
        )
        self.connection.execute(
            "INSERT INTO code_entities VALUES ('legacy-code',?,'function','login','auth.login',?,10,20)",
            (project, code_path),
        )
        run_id = "mig_" + "d" * 64
        self.connection.execute(
            "INSERT INTO v7_migration_runs("
            "migration_run_id,workspace_id,source_db_sha256,source_schema_version,"
            "source_format_version,target_format_version,status,snapshot_name,"
            "candidate_name,source_inventory_json,created_at_us,updated_at_us) "
            "VALUES (?,?,?,15,6,7,'active','source.db','candidate.db','{}',1,1)",
            (run_id, WORKSPACE_ID, "e" * 64),
        )
        source_event_id = self.connection.execute(
            "SELECT source_event_id FROM memory_records WHERE record_id=?",
            (self.record_id,),
        ).fetchone()[0]
        self.connection.execute(
            "INSERT INTO legacy_id_map VALUES (?,?,?,?,?,?,?,?)",
            (
                run_id,
                "memories",
                "7",
                WORKSPACE_ID,
                "memory",
                self.record_id,
                sha256_json({"legacy": 7}),
                source_event_id,
            ),
        )
        self.connection.commit()
        return run_id

    def test_legacy_import_maps_memory_ids_and_never_persists_raw_paths(self) -> None:
        """Legacy discovery membership must resolve through the migration map."""
        from daem0nmcp.discovery_projection import DiscoveryProjectionBuilder

        run_id = self._install_legacy_rows()
        result = DiscoveryProjectionBuilder(
            self.connection, clock_us=lambda: 3_000
        ).import_legacy(
            WORKSPACE_ID,
            workspace_root=self.root,
            migration_run_id=run_id,
        )

        self.assertEqual(1, len(result.entity_ids))
        self.assertEqual(1, len(result.community_ids))
        self.assertEqual(1, len(result.code_entity_ids))
        self.assertEqual(
            self.record_id,
            self.connection.execute(
                "SELECT record_id FROM discovery_entity_records"
            ).fetchone()[0],
        )
        self.assertEqual(
            "src/auth.py",
            self.connection.execute(
                "SELECT relative_file_path FROM discovery_code_entities"
            ).fetchone()[0],
        )

    def test_legacy_import_rejects_a_code_path_outside_the_workspace(self) -> None:
        """A retained absolute path may be translated, never trusted or leaked."""
        from daem0nmcp.discovery_projection import (
            DiscoveryProjectionBuilder,
            DiscoveryProjectionBuildError,
        )

        run_id = self._install_legacy_rows(unsafe_code_path=True)
        with self.assertRaises(DiscoveryProjectionBuildError) as raised:
            DiscoveryProjectionBuilder(self.connection).import_legacy(
                WORKSPACE_ID,
                workspace_root=self.root,
                migration_run_id=run_id,
            )
        self.assertEqual("UNSAFE_LEGACY_PATH", raised.exception.code)
        self.assertEqual(
            0,
            self.connection.execute(
                "SELECT count(*) FROM discovery_projection_partitions"
            ).fetchone()[0],
        )


if __name__ == "__main__":
    unittest.main()
