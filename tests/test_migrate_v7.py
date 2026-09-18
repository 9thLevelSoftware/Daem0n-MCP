"""Dependency-free v7 migration service tests."""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from daem0nmcp.storage_activation import (
    ActiveDatabasePointer,
    DatabaseFileLock,
    write_active_pointer,
)
from daem0nmcp.workspace import WorkspaceAccessError, WorkspaceRegistry


def _create_legacy_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(
        """
        CREATE TABLE schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO schema_version(version) VALUES (15);
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY, category TEXT, content TEXT NOT NULL,
            rationale TEXT, context TEXT, tags TEXT, file_path TEXT,
            file_path_relative TEXT, keywords TEXT, is_permanent INTEGER,
            vector_embedding BLOB, outcome TEXT, worked INTEGER, pinned INTEGER,
            archived INTEGER, recall_count INTEGER, surprise_score REAL,
            importance_score REAL, source_client TEXT, source_model TEXT,
            created_at TEXT, updated_at TEXT
        );
        CREATE TABLE memory_versions (
            id INTEGER PRIMARY KEY, memory_id INTEGER, version_number INTEGER,
            content TEXT, rationale TEXT, context TEXT, tags TEXT, outcome TEXT,
            worked INTEGER, change_type TEXT, change_description TEXT,
            changed_at TEXT, valid_from TEXT, valid_to TEXT,
            invalidated_by_version_id INTEGER
        );
        CREATE TABLE facts (
            id INTEGER PRIMARY KEY, content_hash TEXT, content TEXT,
            category TEXT, source_memory_id INTEGER, verification_count INTEGER,
            is_verified INTEGER, tags TEXT, created_at TEXT, verified_at TEXT
        );
        CREATE TABLE memory_relationships (
            id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
            relationship TEXT, description TEXT, confidence REAL, created_at TEXT
        );
        INSERT INTO memories VALUES (
            1, 'decision', 'Use SQLite', NULL, '{}', '["db"]', NULL, NULL,
            'sqlite', 0, x'01020304', NULL, NULL, 0, 0, 0, NULL, NULL,
            'client', 'model', '2026-01-01 00:00:00', '2026-01-01 00:00:00'
        );
        INSERT INTO memories VALUES (
            2, 'future-kind', 'Preserve me', NULL, '{bad', '[bad', NULL, NULL,
            NULL, 0, NULL, NULL, NULL, 0, 0, 0, NULL, NULL,
            NULL, NULL, 'not-a-time', NULL
        );
        INSERT INTO memory_versions VALUES (
            1, 1, 1, 'Use SQLite', NULL, '{}', '["db"]', NULL, NULL,
            'created', 'initial', '2026-01-01 00:00:00', NULL, NULL, NULL
        );
        INSERT INTO facts VALUES (
            1, 'legacy-hash', 'SQLite is local', 'database', 1, 2, 1,
            '["db"]', '2026-01-01 00:00:00', NULL
        );
        INSERT INTO memory_relationships VALUES (
            1, 1, 999, 'unknown-edge', 'orphan target', 0.5,
            '2026-01-01 00:00:00'
        );
        """
    )
    connection.commit()
    return connection


def _filesystem_state(root: Path):
    return {
        path.relative_to(root).as_posix(): (
            (
                path.stat().st_size,
                path.stat().st_mtime_ns,
                # SQLite mmap-updates volatile WAL read-lock words without
                # changing file metadata; they are not application writes.
                None if path.name.endswith("-shm") else path.read_bytes(),
            )
            if path.is_file()
            else ("directory",),
        )
        for path in sorted(root.rglob("*"))
    }


async def _read_retained_resources(repository, workspace):
    from daem0nmcp.api.v7.resources import ResourceReadRequest

    rules = await repository.read_rules(
        workspace,
        ResourceReadRequest(
            kind="rules", limit=10, order_by="priority_desc", enabled_only=True
        ),
    )
    active = await repository.read_active_context(
        workspace,
        ResourceReadRequest(kind="active_context", limit=10, order_by="priority_desc"),
    )
    return rules, active


class V7DryRunTests(unittest.TestCase):
    def test_out_of_range_legacy_time_is_preserved_as_invalid_epoch(self):
        from daem0nmcp.migrations.v7 import _parse_legacy_time

        self.assertEqual((0, "invalid", 10**20), _parse_legacy_time(10**20))

    def test_public_migrations_package_exports_offline_v7_service(self):
        from daem0nmcp.migrations import MigrationResult, MigrationV7Service

        self.assertTrue(callable(MigrationV7Service))
        self.assertTrue(hasattr(MigrationResult, "as_dict"))

    def test_dry_run_is_deterministic_wal_aware_and_strictly_read_only(self):
        """Inventory may read committed WAL state but must write no filesystem byte."""
        try:
            from daem0nmcp.migrations.v7 import MigrationV7Service
        except ImportError as exc:  # intentional RED before service exists
            self.fail(f"v7 migration service is missing: {exc}")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            storage = root / ".daem0nmcp" / "storage"
            storage.mkdir(parents=True)
            writer = _create_legacy_database(storage / "daem0nmcp.db")
            try:
                # Keep a committed change in the live WAL while inventory reads mode=ro.
                writer.execute("UPDATE memories SET recall_count=3 WHERE id=1")
                writer.commit()
                registry = WorkspaceRegistry([root], default_root=root)
                service = MigrationV7Service(registry)
                # Provision the durable coordination marker before measuring
                # the read-only inspection. Dry-run must acquire it, while all
                # database, WAL, pointer, and existing metadata bytes remain
                # unchanged.
                with DatabaseFileLock(storage, "shared"):
                    pass
                before = _filesystem_state(root)

                first = service.dry_run(root)
                second = service.dry_run(registry.default.workspace_id)

                self.assertEqual(first, second)
                self.assertEqual("dry_run", first.status)
                self.assertEqual("migrate", first.action)
                self.assertEqual(6, first.source_format)
                self.assertEqual(7, first.target_format)
                self.assertEqual(0, first.active_generation)
                self.assertEqual(15, first.inventory["max_schema_version"])
                self.assertEqual(2, first.inventory["memory_count"])
                self.assertEqual(1, first.inventory["version_count"])
                self.assertEqual(1, first.inventory["fact_count"])
                self.assertEqual(1, first.inventory["relationship_count"])
                self.assertEqual(1, first.inventory["vector_count"])
                self.assertEqual(4, first.inventory["vector_bytes"])
                self.assertEqual(
                    ["future-kind"], first.inventory["unknown_memory_categories"]
                )
                self.assertGreaterEqual(first.inventory["malformed_json_fields"], 2)
                self.assertEqual("ok", first.inventory["quick_check"])
                self.assertRegex(first.inventory["logical_sha256"], r"^[0-9a-f]{64}$")
                self.assertEqual(before, _filesystem_state(root))
                self.assertTrue((storage / ".migrate-v7.lock").exists())
                self.assertFalse((storage / "active-db.json").exists())
            finally:
                writer.close()

    def test_dry_run_resolves_only_registered_workspace(self):
        from daem0nmcp.migrations.v7 import MigrationV7Service

        with (
            tempfile.TemporaryDirectory() as raw,
            tempfile.TemporaryDirectory() as other,
        ):
            root = Path(raw)
            storage = root / ".daem0nmcp" / "storage"
            storage.mkdir(parents=True)
            connection = _create_legacy_database(storage / "daem0nmcp.db")
            connection.close()
            service = MigrationV7Service(WorkspaceRegistry([root], default_root=root))
            with self.assertRaises(WorkspaceAccessError):
                service.dry_run(other)

    def test_active_v7_dry_run_reports_already_active_without_changes(self):
        from daem0nmcp.migrations.v7 import MigrationV7Service

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            storage = root / ".daem0nmcp" / "storage"
            storage.mkdir(parents=True)
            connection = _create_legacy_database(storage / "daem0nmcp.db")
            connection.close()
            # The schema fixture is intentionally legacy; dry-run format comes only
            # from the validated pointer, never from table presence/version max.
            write_active_pointer(
                storage, ActiveDatabasePointer(7, 1, "daem0nmcp.db", None, None)
            )
            service = MigrationV7Service(WorkspaceRegistry([root], default_root=root))
            with DatabaseFileLock(storage, "shared"):
                pass
            before = _filesystem_state(root)
            result = service.dry_run(None)
            self.assertEqual("already_active", result.action)
            self.assertEqual(7, result.source_format)
            self.assertEqual(before, _filesystem_state(root))


class V7ApplyRollbackTests(unittest.TestCase):
    def _workspace(self, raw: str):
        root = Path(raw)
        storage = root / ".daem0nmcp" / "storage"
        storage.mkdir(parents=True)
        connection = _create_legacy_database(storage / "daem0nmcp.db")
        connection.close()
        registry = WorkspaceRegistry([root], default_root=root)
        return root, storage, registry

    def test_migration_paths_validate_every_ancestor_without_recursive_mkdir(self):
        """The service must not follow an untrusted migrations ancestor."""
        source = (
            Path(__file__).resolve().parents[1] / "daem0nmcp" / "migrations" / "v7.py"
        ).read_text(encoding="utf-8")
        self.assertIn("def _validated_migration_root", source)
        prepare = source[source.index("    def _prepare_candidate") :]
        prepare = prepare[: prepare.index("    def _find_resumable")]
        self.assertIn("_validated_migration_root", prepare)
        self.assertNotIn("mkdir(parents=True", prepare)

    def test_populated_retained_resources_receive_deterministic_public_ids(self):
        # Catches migration 19 creating an empty mapping table after retained
        # rules and active-context rows have already acquired public meaning.
        from daem0nmcp.api.v7.public_ids import derive_public_object_id
        from daem0nmcp.api.v7.resource_repository import SQLiteResourceRepository
        from daem0nmcp.migrations.v7 import MigrationV7Service
        from daem0nmcp.storage_activation import resolve_active_database

        with tempfile.TemporaryDirectory() as raw:
            root, storage, registry = self._workspace(raw)
            source = sqlite3.connect(storage / "daem0nmcp.db")
            source.executescript(
                """
                CREATE TABLE rules (
                    id INTEGER PRIMARY KEY, trigger TEXT NOT NULL,
                    must_do TEXT NOT NULL, must_not TEXT NOT NULL,
                    ask_first TEXT NOT NULL, warnings TEXT NOT NULL,
                    priority INTEGER NOT NULL, enabled INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                INSERT INTO rules VALUES (
                    7,'always','["test"]','[]','[]','[]',5,1,
                    '2026-01-01 00:00:00'
                );
                CREATE TABLE active_context (
                    id INTEGER PRIMARY KEY, project_path TEXT NOT NULL,
                    memory_id INTEGER NOT NULL, priority INTEGER NOT NULL,
                    reason TEXT, added_at TEXT NOT NULL, expires_at TEXT
                );
                CREATE TABLE context_triggers (
                    id INTEGER PRIMARY KEY, project_path TEXT NOT NULL,
                    trigger_type TEXT NOT NULL, pattern TEXT NOT NULL,
                    recall_topic TEXT NOT NULL, recall_categories TEXT NOT NULL,
                    is_active INTEGER NOT NULL, priority INTEGER NOT NULL,
                    created_at TEXT NOT NULL, trigger_count INTEGER NOT NULL,
                    last_triggered TEXT
                );
                """
            )
            source.execute(
                "INSERT INTO active_context VALUES (8,?,?,?,?,?,NULL)",
                (str(root), 1, 4, "migration focus", "2026-01-01 00:00:00"),
            )
            source.execute(
                "INSERT INTO context_triggers VALUES "
                "(11,?,'tag_match','auth.*','authentication guidance',"
                "'[\"warning\"]',1,9,'2026-01-02 00:00:00',0,NULL)",
                (str(root),),
            )
            source.commit()
            source.close()

            MigrationV7Service(registry).apply(root)
            resolved = resolve_active_database(storage)
            workspace = registry.default
            expected_rule = derive_public_object_id(workspace.workspace_id, "rule", 7)
            expected_active = derive_public_object_id(
                workspace.workspace_id, "active_context", 8
            )
            expected_trigger = derive_public_object_id(
                workspace.workspace_id, "trigger", 11
            )
            candidate = sqlite3.connect(resolved.path)
            try:
                mappings = candidate.execute(
                    "SELECT object_kind,source_key,public_id "
                    "FROM public_object_ids ORDER BY object_kind,source_key"
                ).fetchall()
                projected_rule = candidate.execute(
                    "SELECT rule_id,priority,stream_version FROM governance_rules"
                ).fetchone()
                projected_trigger = candidate.execute(
                    "SELECT trigger_id,trigger_type,priority,stream_version "
                    "FROM governance_context_triggers"
                ).fetchone()
                governance_events = candidate.execute(
                    "SELECT stream_kind,event_type,stream_id "
                    "FROM governance_events ORDER BY stream_kind"
                ).fetchall()
                governance_payloads = candidate.execute(
                    "SELECT payload_json FROM governance_events"
                ).fetchall()
            finally:
                candidate.close()
            self.assertEqual(
                mappings,
                [
                    ("active_context", "i:8", expected_active),
                    ("rule", "i:7", expected_rule),
                    ("trigger", "i:11", expected_trigger),
                ],
            )
            self.assertEqual((expected_rule, 5, 1), projected_rule)
            self.assertEqual((expected_trigger, "tag", 9, 1), projected_trigger)
            self.assertEqual(
                [
                    ("rule", "rule.created", expected_rule),
                    ("trigger", "context_trigger.created", expected_trigger),
                ],
                governance_events,
            )
            self.assertNotIn(
                str(root), "".join(str(row[0]) for row in governance_payloads)
            )

            repository = SQLiteResourceRepository(lambda _workspace: resolved)
            rules, active = asyncio.run(_read_retained_resources(repository, workspace))
            self.assertEqual([row.rule_id for row in rules], [expected_rule])
            self.assertEqual(
                [row.item.active_context_id for row in active], [expected_active]
            )

    def test_apply_rejects_symlinked_migrations_ancestor_before_outside_write(self):
        from daem0nmcp.migrations.v7 import MigrationV7Error, MigrationV7Service

        with (
            tempfile.TemporaryDirectory() as raw,
            tempfile.TemporaryDirectory() as other,
        ):
            root, storage, registry = self._workspace(raw)
            outside = Path(other)
            migrations = storage / "migrations"
            try:
                migrations.symlink_to(outside, target_is_directory=True)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"directory symlinks unavailable: {exc}")
            before = _filesystem_state(outside)
            with self.assertRaisesRegex(MigrationV7Error, "UNSAFE_MIGRATION_PATH"):
                MigrationV7Service(registry).apply(None)
            self.assertEqual(before, _filesystem_state(outside))

    def test_apply_imports_losslessly_validates_and_activates_pointer(self):
        """Migration retains v6 rows while creating canonical typed history."""
        from daem0nmcp.migrations.v7 import MigrationV7Service
        from daem0nmcp.storage_activation import resolve_active_database

        with tempfile.TemporaryDirectory() as raw:
            root, storage, registry = self._workspace(raw)
            source_fixture = sqlite3.connect(storage / "daem0nmcp.db")
            source_rows = source_fixture.execute(
                "SELECT * FROM memories ORDER BY id"
            ).fetchall()
            source_fixture.close()
            result = MigrationV7Service(registry).apply(root, batch_size=1)
            self.assertEqual("activated", result.status)
            self.assertEqual("migrate", result.action)
            self.assertIsNotNone(result.migration_run_id)
            resolved = resolve_active_database(storage)
            self.assertEqual(7, resolved.format_version)
            self.assertEqual(1, resolved.generation)
            self.assertEqual("daem0nmcp.db", resolved.previous_db)
            self.assertTrue((storage / "daem0nmcp.db").is_file())
            self.assertTrue((resolved.path.parent / "source.snapshot.db").is_file())
            self.assertFalse((resolved.path.parent / "candidate.db.partial").exists())
            source = sqlite3.connect(storage / "daem0nmcp.db")
            self.assertEqual(
                source_rows,
                source.execute("SELECT * FROM memories ORDER BY id").fetchall(),
            )
            source.close()
            candidate = sqlite3.connect(resolved.path)
            candidate.row_factory = sqlite3.Row
            try:
                self.assertEqual(
                    2, candidate.execute("SELECT count(*) FROM memories").fetchone()[0]
                )
                self.assertEqual(
                    3,
                    candidate.execute("SELECT count(*) FROM memory_records").fetchone()[
                        0
                    ],
                )
                self.assertEqual(
                    1,
                    candidate.execute(
                        "SELECT count(*) FROM memory_records WHERE record_type='decision'"
                    ).fetchone()[0],
                )
                legacy = candidate.execute(
                    "SELECT * FROM memory_records WHERE record_type='legacy' AND legacy_type='future-kind'"
                ).fetchone()
                self.assertIsNotNone(legacy)
                placeholder = candidate.execute(
                    "SELECT count(*) FROM legacy_id_map WHERE target_kind='placeholder'"
                ).fetchone()[0]
                self.assertEqual(1, placeholder)
                self.assertEqual(
                    5,
                    candidate.execute("SELECT count(*) FROM legacy_id_map").fetchone()[
                        0
                    ],
                )
                self.assertEqual(
                    6,
                    candidate.execute("SELECT count(*) FROM memory_events").fetchone()[
                        0
                    ],
                )
                self.assertEqual(
                    11,
                    candidate.execute(
                        "SELECT count(*) FROM projection_manifests"
                    ).fetchone()[0],
                )
                self.assertEqual(
                    "active",
                    candidate.execute(
                        "SELECT status FROM v7_migration_runs"
                    ).fetchone()[0],
                )
                self.assertEqual(
                    "ok", candidate.execute("PRAGMA integrity_check").fetchone()[0]
                )
                self.assertEqual(
                    [], candidate.execute("PRAGMA foreign_key_check").fetchall()
                )
            finally:
                candidate.close()
            pointer_before = (storage / "active-db.json").read_bytes()
            again = MigrationV7Service(registry).apply(None)
            self.assertEqual("already_active", again.action)
            self.assertEqual(pointer_before, (storage / "active-db.json").read_bytes())

    def test_cutover_publishes_a_validated_lexical_baseline(self):
        from daem0nmcp.migrations.v7 import MigrationV7Service
        from daem0nmcp.retrieval.providers import LexicalProvider
        from daem0nmcp.retrieval.types import RetrievalQuery
        from daem0nmcp.storage_activation import resolve_active_database

        with tempfile.TemporaryDirectory() as raw:
            root, storage, registry = self._workspace(raw)
            MigrationV7Service(registry).apply(root, batch_size=1)
            resolved = resolve_active_database(storage)
            connection = sqlite3.connect(resolved.path)
            connection.row_factory = sqlite3.Row
            try:
                manifest = connection.execute(
                    "SELECT generation,status,row_count,details_json "
                    "FROM projection_manifests WHERE workspace_id=? "
                    "AND projection_name='lexical' AND status='active'",
                    (registry.default.workspace_id,),
                ).fetchone()
                self.assertIsNotNone(manifest)
                self.assertEqual(1, manifest[0])
                self.assertEqual("active", manifest[1])
                self.assertEqual(
                    connection.execute(
                        "SELECT count(*) FROM memory_records "
                        "WHERE workspace_id=? AND deleted_at_us IS NULL",
                        (registry.default.workspace_id,),
                    ).fetchone()[0],
                    manifest[2],
                )
                details = json.loads(manifest[3])
                self.assertEqual(
                    manifest[2],
                    connection.execute(
                        "SELECT count(*) FROM retrieval_documents "
                        "WHERE workspace_id=? AND projection_generation=1",
                        (registry.default.workspace_id,),
                    ).fetchone()[0],
                )
                self.assertEqual(
                    manifest[2],
                    connection.execute(
                        f'SELECT count(*) FROM "{details["fts_table"]}"'
                    ).fetchone()[0],
                )
                result = asyncio.run(
                    LexicalProvider(connection).search(
                        RetrievalQuery(
                            workspace_id=registry.default.workspace_id,
                            text="SQLite",
                        ),
                        10,
                    )
                )
                self.assertEqual("ready", result.status)
                self.assertTrue(result.candidates)
            finally:
                connection.close()

    def test_lexical_bootstrap_is_idempotent_across_prepublication_resume(self):
        from daem0nmcp.migrations.v7 import (
            MigrationInterrupted,
            MigrationV7Service,
        )
        from daem0nmcp.storage_activation import resolve_active_database

        with tempfile.TemporaryDirectory() as raw:
            root, storage, registry = self._workspace(raw)

            def interrupt(stage, _details):
                if stage == "before_candidate_publish":
                    raise MigrationInterrupted("stop after lexical bootstrap")

            with self.assertRaises(MigrationInterrupted):
                MigrationV7Service(registry, fault_injector=interrupt).apply(
                    root, batch_size=1
                )
            partial = next(storage.glob("migrations/v7/mig_*/candidate.db.partial"))
            staged = sqlite3.connect(partial)
            try:
                self.assertEqual(
                    [(1, "active")],
                    staged.execute(
                        "SELECT generation,status FROM projection_manifests "
                        "WHERE projection_name='lexical' ORDER BY generation"
                    ).fetchall(),
                )
            finally:
                staged.close()

            resumed = MigrationV7Service(registry).apply(root, batch_size=1)
            self.assertEqual("resume", resumed.action)
            active = sqlite3.connect(resolve_active_database(storage).path)
            try:
                self.assertEqual(
                    [(1, "active")],
                    active.execute(
                        "SELECT generation,status FROM projection_manifests "
                        "WHERE projection_name='lexical' ORDER BY generation"
                    ).fetchall(),
                )
            finally:
                active.close()

    def test_fts5_bootstrap_failure_keeps_the_v6_pointer_active(self):
        from daem0nmcp.migrations.v7 import MigrationV7Error, MigrationV7Service
        from daem0nmcp.retrieval.projections import (
            LexicalProjectionBuilder,
            ProjectionBuildError,
        )
        from daem0nmcp.storage_activation import resolve_active_database

        with tempfile.TemporaryDirectory() as raw:
            root, storage, registry = self._workspace(raw)
            with (
                mock.patch.object(
                    LexicalProjectionBuilder,
                    "rebuild",
                    side_effect=ProjectionBuildError(
                        "LEXICAL_UNAVAILABLE", "FTS5 is unavailable"
                    ),
                ),
                self.assertRaisesRegex(MigrationV7Error, "LEXICAL_BOOTSTRAP_FAILED"),
            ):
                MigrationV7Service(registry).apply(root, batch_size=1)

            resolved = resolve_active_database(storage)
            self.assertEqual(6, resolved.format_version)
            self.assertEqual(storage / "daem0nmcp.db", resolved.path)

    def test_populated_pointerless_schema_16_is_still_migrated_as_format_six(self):
        from daem0nmcp.migrations.schema import MIGRATIONS
        from daem0nmcp.migrations.v7 import MigrationV7Service
        from daem0nmcp.storage_activation import resolve_active_database

        with tempfile.TemporaryDirectory() as raw:
            root, storage, registry = self._workspace(raw)
            source = sqlite3.connect(storage / "daem0nmcp.db")
            migration_16 = next(item for item in MIGRATIONS if item[0] == 16)
            for statement in migration_16[2]:
                source.execute(statement)
            source.execute("INSERT INTO schema_version(version) VALUES (16)")
            source.commit()
            source.close()

            result = MigrationV7Service(registry).apply(root)
            self.assertEqual("activated", result.status)
            active = sqlite3.connect(resolve_active_database(storage).path)
            self.assertGreater(
                active.execute("SELECT count(*) FROM memory_events").fetchone()[0], 0
            )
            active.close()
            retained = sqlite3.connect(storage / "daem0nmcp.db")
            self.assertEqual(
                0, retained.execute("SELECT count(*) FROM memory_events").fetchone()[0]
            )
            retained.close()

    def test_malformed_memory_json_is_preserved_and_annotated(self):
        from daem0nmcp.migrations.v7 import MigrationV7Service
        from daem0nmcp.storage_activation import resolve_active_database

        with tempfile.TemporaryDirectory() as raw:
            root, storage, registry = self._workspace(raw)
            MigrationV7Service(registry).apply(root)
            connection = sqlite3.connect(resolve_active_database(storage).path)
            payload_text = connection.execute(
                "SELECT e.payload_json FROM legacy_id_map m "
                "JOIN memory_events e ON e.event_id=m.imported_event_id "
                "WHERE m.source_table='memories' AND m.legacy_id='2'"
            ).fetchone()[0]
            connection.close()
            payload = json.loads(payload_text)
            self.assertEqual("{bad", dict(payload["legacy"]["columns"])["context"])
            self.assertEqual("invalid", payload["normalization"]["context_quality"])
            self.assertEqual("invalid", payload["normalization"]["tags_quality"])

    def test_same_workspace_snapshot_rebuilds_identical_canonical_history(self):
        """Wall-clock/run bookkeeping may vary; canonical IDs and events may not."""
        from daem0nmcp.migrations.v7 import MigrationV7Service
        from daem0nmcp.storage_activation import resolve_active_database

        with tempfile.TemporaryDirectory() as raw:
            root, storage, registry = self._workspace(raw)
            service = MigrationV7Service(registry)

            def canonical_result():
                result = service.apply(root)
                active = resolve_active_database(storage)
                connection = sqlite3.connect(active.path)
                events = connection.execute(
                    "SELECT event_id,event_hash,payload_json,stream_id,stream_version "
                    "FROM memory_events ORDER BY event_id"
                ).fetchall()
                mappings = connection.execute(
                    "SELECT source_table,legacy_id,target_kind,target_id,source_row_hash "
                    "FROM legacy_id_map ORDER BY source_table,legacy_id"
                ).fetchall()
                connection.close()
                return result.migration_run_id, events, mappings

            first = canonical_result()
            (storage / "active-db.json").unlink()
            shutil.rmtree(storage / "migrations")
            second = canonical_result()
            self.assertEqual(first, second)

    def test_interrupted_batches_resume_without_duplicate_events(self):
        """Checkpoint and imported rows commit together, then resume by PK range."""
        from daem0nmcp.migrations.v7 import MigrationInterrupted, MigrationV7Service

        with tempfile.TemporaryDirectory() as raw:
            root, storage, registry = self._workspace(raw)
            calls = []

            def interrupt(stage, details):
                if stage == "after_batch" and details["source_table"] == "memories":
                    calls.append(details)
                    if len(calls) == 1:
                        raise MigrationInterrupted("fixture interruption")

            with self.assertRaises(MigrationInterrupted):
                MigrationV7Service(registry, fault_injector=interrupt).apply(
                    None, batch_size=1
                )
            self.assertFalse((storage / "active-db.json").exists())
            partials = list(storage.glob("migrations/v7/mig_*/candidate.db.partial"))
            self.assertEqual(1, len(partials))
            partial = sqlite3.connect(partials[0])
            self.assertEqual(
                1,
                partial.execute(
                    "SELECT rows_imported FROM v7_migration_checkpoints WHERE source_table='memories'"
                ).fetchone()[0],
            )
            first_events = partial.execute(
                "SELECT count(*) FROM memory_events"
            ).fetchone()[0]
            partial.close()
            resumed = MigrationV7Service(registry).apply(None, batch_size=1)
            self.assertEqual("resume", resumed.action)
            from daem0nmcp.storage_activation import resolve_active_database

            candidate = sqlite3.connect(resolve_active_database(storage).path)
            self.assertGreater(
                candidate.execute("SELECT count(*) FROM memory_events").fetchone()[0],
                first_events,
            )
            self.assertEqual(
                candidate.execute("SELECT count(*) FROM memory_events").fetchone()[0],
                candidate.execute(
                    "SELECT count(DISTINCT event_id) FROM memory_events"
                ).fetchone()[0],
            )
            candidate.close()

    def test_snapshot_and_ddl_crash_boundaries_resume_without_restarting_source(self):
        from daem0nmcp.migrations.v7 import MigrationInterrupted, MigrationV7Service
        from daem0nmcp.storage_activation import resolve_active_database

        for fault_stage in ("after_snapshot", "after_ddl"):
            with (
                self.subTest(fault_stage=fault_stage),
                tempfile.TemporaryDirectory() as raw,
            ):
                root, storage, registry = self._workspace(raw)

                def interrupt(stage, _details, expected_stage=fault_stage):
                    if stage == expected_stage:
                        raise MigrationInterrupted(expected_stage)

                with self.assertRaises(MigrationInterrupted):
                    MigrationV7Service(registry, fault_injector=interrupt).apply(root)
                self.assertFalse((storage / "active-db.json").exists())
                resumed = MigrationV7Service(registry).apply(root)
                self.assertEqual("resume", resumed.action)
                self.assertEqual(7, resolve_active_database(storage).format_version)

    def test_candidate_published_before_pointer_is_resumed_without_reimport(self):
        """A crash after the atomic candidate rename must not strand the run."""
        from daem0nmcp.migrations.v7 import MigrationInterrupted, MigrationV7Service
        from daem0nmcp.storage_activation import resolve_active_database

        with tempfile.TemporaryDirectory() as raw:
            root, storage, registry = self._workspace(raw)

            def interrupt(stage, _details):
                if stage == "before_pointer":
                    raise MigrationInterrupted("process stopped before pointer")

            with self.assertRaises(MigrationInterrupted):
                MigrationV7Service(registry, fault_injector=interrupt).apply(root)
            published = list(storage.glob("migrations/v7/mig_*/candidate.db"))
            self.assertEqual(1, len(published))
            self.assertFalse((storage / "active-db.json").exists())
            self.assertFalse(published[0].with_suffix(".db.partial").exists())

            resumed = MigrationV7Service(registry).apply(root)

            self.assertEqual("activated", resumed.status)
            self.assertEqual("resume", resumed.action)
            self.assertEqual(published[0], resolve_active_database(storage).path)

    def test_pointer_published_with_ready_run_finishes_activation_on_retry(self):
        """A process stop after pointer publication must converge to active metadata."""
        from daem0nmcp.migrations.v7 import MigrationInterrupted, MigrationV7Service
        from daem0nmcp.storage_activation import resolve_active_database

        with tempfile.TemporaryDirectory() as raw:
            root, storage, registry = self._workspace(raw)

            def interrupt(stage, _details):
                if stage == "before_pointer":
                    raise MigrationInterrupted("fixture creates published candidate")

            with self.assertRaises(MigrationInterrupted):
                MigrationV7Service(registry, fault_injector=interrupt).apply(root)
            published = next(storage.glob("migrations/v7/mig_*/candidate.db"))
            run_id = published.parent.name
            write_active_pointer(
                storage,
                ActiveDatabasePointer(
                    7,
                    1,
                    published.relative_to(storage).as_posix(),
                    "daem0nmcp.db",
                    run_id,
                ),
            )

            resumed = MigrationV7Service(registry).apply(root)

            resolved = resolve_active_database(storage)
            self.assertEqual("activated", resumed.status)
            self.assertEqual("resume", resumed.action)
            self.assertEqual(1, resolved.generation)
            connection = sqlite3.connect(resolved.path)
            try:
                self.assertEqual(
                    "active",
                    connection.execute(
                        "SELECT status FROM v7_migration_runs WHERE migration_run_id=?",
                        (run_id,),
                    ).fetchone()[0],
                )
            finally:
                connection.close()

    def test_published_ready_candidate_survives_a_second_checkpoint_interruption(self):
        """Revisiting completed checkpoints may not demote a published ready run."""
        from daem0nmcp.migrations.v7 import MigrationInterrupted, MigrationV7Service
        from daem0nmcp.storage_activation import resolve_active_database

        with tempfile.TemporaryDirectory() as raw:
            root, storage, registry = self._workspace(raw)

            def stop_before_pointer(stage, _details):
                if stage == "before_pointer":
                    raise MigrationInterrupted("first process stop")

            with self.assertRaises(MigrationInterrupted):
                MigrationV7Service(registry, fault_injector=stop_before_pointer).apply(
                    root
                )
            published = next(storage.glob("migrations/v7/mig_*/candidate.db"))
            run_id = published.parent.name

            def stop_after_completed_checkpoint(stage, _details):
                if stage == "after_completed_checkpoint":
                    raise MigrationInterrupted("second process stop")

            with self.assertRaises(MigrationInterrupted):
                MigrationV7Service(
                    registry, fault_injector=stop_after_completed_checkpoint
                ).apply(root)
            connection = sqlite3.connect(published)
            try:
                self.assertEqual(
                    "ready",
                    connection.execute(
                        "SELECT status FROM v7_migration_runs WHERE migration_run_id=?",
                        (run_id,),
                    ).fetchone()[0],
                )
            finally:
                connection.close()
            self.assertFalse((storage / "active-db.json").exists())

            activated = MigrationV7Service(registry).apply(root)

            self.assertEqual(run_id, activated.migration_run_id)
            self.assertEqual(published, resolve_active_database(storage).path)

    def test_source_change_after_checkpoint_refuses_resume(self):
        from daem0nmcp.migrations.v7 import (
            MigrationInterrupted,
            MigrationV7Error,
            MigrationV7Service,
        )

        with tempfile.TemporaryDirectory() as raw:
            root, storage, registry = self._workspace(raw)

            def interrupt(stage, details):
                if stage == "after_batch" and details["source_table"] == "memories":
                    raise MigrationInterrupted("change source")

            with self.assertRaises(MigrationInterrupted):
                MigrationV7Service(registry, fault_injector=interrupt).apply(
                    root, batch_size=1
                )
            source = sqlite3.connect(storage / "daem0nmcp.db")
            source.execute("UPDATE memories SET content='changed' WHERE id=1")
            source.commit()
            source.close()
            with self.assertRaisesRegex(MigrationV7Error, "SOURCE_CHANGED"):
                MigrationV7Service(registry).apply(root)
            self.assertFalse((storage / "active-db.json").exists())

    def test_validation_recomputes_every_legacy_mapping_source_hash(self):
        from daem0nmcp.migrations.v7 import (
            MigrationInterrupted,
            MigrationV7Error,
            MigrationV7Service,
        )

        with tempfile.TemporaryDirectory() as raw:
            root, storage, registry = self._workspace(raw)

            def interrupt(stage, details):
                if (
                    stage == "after_batch"
                    and details["source_table"] == "memory_relationships"
                ):
                    raise MigrationInterrupted("tamper before validation")

            with self.assertRaises(MigrationInterrupted):
                MigrationV7Service(registry, fault_injector=interrupt).apply(None)
            partial = next(storage.glob("migrations/v7/mig_*/candidate.db.partial"))
            candidate = sqlite3.connect(partial)
            candidate.execute(
                "UPDATE legacy_id_map SET source_row_hash=? "
                "WHERE source_table='memories' AND legacy_id='1'",
                ("0" * 64,),
            )
            candidate.commit()
            candidate.close()

            with self.assertRaisesRegex(MigrationV7Error, "VALIDATION_FAILED"):
                MigrationV7Service(registry).apply(None)
            self.assertFalse((storage / "active-db.json").exists())

    def test_validation_retains_every_non_v7_source_table(self):
        from daem0nmcp.migrations.v7 import (
            MigrationInterrupted,
            MigrationV7Error,
            MigrationV7Service,
        )

        with tempfile.TemporaryDirectory() as raw:
            root, storage, registry = self._workspace(raw)
            source = sqlite3.connect(storage / "daem0nmcp.db")
            source.execute(
                "CREATE TABLE retained_settings(key TEXT PRIMARY KEY, value TEXT)"
            )
            source.execute("INSERT INTO retained_settings VALUES ('mode','original')")
            source.commit()
            source.close()

            def interrupt(stage, details):
                if (
                    stage == "after_batch"
                    and details["source_table"] == "memory_relationships"
                ):
                    raise MigrationInterrupted("tamper retained source table")

            with self.assertRaises(MigrationInterrupted):
                MigrationV7Service(registry, fault_injector=interrupt).apply(root)
            partial = next(storage.glob("migrations/v7/mig_*/candidate.db.partial"))
            candidate = sqlite3.connect(partial)
            candidate.execute("UPDATE retained_settings SET value='tampered'")
            candidate.commit()
            candidate.close()
            with self.assertRaisesRegex(MigrationV7Error, "VALIDATION_FAILED"):
                MigrationV7Service(registry).apply(root)
            self.assertFalse((storage / "active-db.json").exists())

    def test_validation_independently_replays_fact_projection_hashes(self):
        from daem0nmcp.migrations.v7 import (
            MigrationInterrupted,
            MigrationV7Error,
            MigrationV7Service,
        )

        with tempfile.TemporaryDirectory() as raw:
            root, storage, registry = self._workspace(raw)

            def interrupt(stage, details):
                if (
                    stage == "after_batch"
                    and details["source_table"] == "memory_relationships"
                ):
                    raise MigrationInterrupted("tamper before validation")

            with self.assertRaises(MigrationInterrupted):
                MigrationV7Service(registry, fault_injector=interrupt).apply(None)
            partial = next(storage.glob("migrations/v7/mig_*/candidate.db.partial"))
            candidate = sqlite3.connect(partial)
            candidate.execute(
                "UPDATE memory_fact_versions SET content_hash=?",
                ("0" * 64,),
            )
            candidate.commit()
            candidate.close()

            with self.assertRaisesRegex(MigrationV7Error, "VALIDATION_FAILED"):
                MigrationV7Service(registry).apply(None)
            self.assertFalse((storage / "active-db.json").exists())

    def test_validation_replays_fact_and_relationship_transaction_closure(self):
        """Closed transaction metadata must be derived from immutable events."""
        from daem0nmcp.migrations.v7 import (
            MigrationInterrupted,
            MigrationV7Error,
            MigrationV7Service,
        )

        for table in ("memory_fact_versions", "memory_relationship_versions"):
            with self.subTest(table=table), tempfile.TemporaryDirectory() as raw:
                root, storage, registry = self._workspace(raw)

                def interrupt(stage, details):
                    if (
                        stage == "after_batch"
                        and details["source_table"] == "memory_relationships"
                    ):
                        raise MigrationInterrupted("tamper transaction closure")

                with self.assertRaises(MigrationInterrupted):
                    MigrationV7Service(registry, fault_injector=interrupt).apply(root)
                partial = next(storage.glob("migrations/v7/mig_*/candidate.db.partial"))
                candidate = sqlite3.connect(partial)
                candidate.execute(
                    f"UPDATE {table} SET transaction_to_us=transaction_from_us+1, "
                    "retracted_by_event_id=asserted_by_event_id"
                )
                candidate.commit()
                candidate.close()

                with self.assertRaisesRegex(MigrationV7Error, "VALIDATION_FAILED"):
                    MigrationV7Service(registry).apply(root)
                self.assertFalse((storage / "active-db.json").exists())

    def test_validation_requires_every_event_derived_typed_projection_key(self):
        """Deleting an entire typed projection cannot redefine the expected set."""
        from daem0nmcp.migrations.v7 import (
            MigrationInterrupted,
            MigrationV7Error,
            MigrationV7Service,
        )

        for table in ("memory_fact_versions", "memory_relationship_versions"):
            with self.subTest(table=table), tempfile.TemporaryDirectory() as raw:
                root, storage, registry = self._workspace(raw)

                def interrupt(stage, details):
                    if (
                        stage == "after_batch"
                        and details["source_table"] == "memory_relationships"
                    ):
                        raise MigrationInterrupted("delete typed projection")

                with self.assertRaises(MigrationInterrupted):
                    MigrationV7Service(registry, fault_injector=interrupt).apply(root)
                partial = next(storage.glob("migrations/v7/mig_*/candidate.db.partial"))
                candidate = sqlite3.connect(partial)
                candidate.execute(f"DELETE FROM {table}")
                candidate.commit()
                candidate.close()
                pointer_before = (
                    (storage / "active-db.json").read_bytes()
                    if (storage / "active-db.json").exists()
                    else None
                )

                with self.assertRaisesRegex(MigrationV7Error, "VALIDATION_FAILED"):
                    MigrationV7Service(registry).apply(root)
                pointer_after = (
                    (storage / "active-db.json").read_bytes()
                    if (storage / "active-db.json").exists()
                    else None
                )
                self.assertEqual(pointer_before, pointer_after)

    def test_repeated_orphan_endpoint_reuses_one_deterministic_placeholder(self):
        """Two legacy edges to one absent memory share one placeholder stream."""
        from daem0nmcp.migrations.v7 import MigrationV7Service
        from daem0nmcp.storage_activation import resolve_active_database

        with tempfile.TemporaryDirectory() as raw:
            root, storage, registry = self._workspace(raw)
            source = sqlite3.connect(storage / "daem0nmcp.db")
            source.execute(
                "INSERT INTO memory_relationships VALUES "
                "(2, 2, 999, 'related_to', 'same orphan later', 0.7, "
                "'2026-01-02 00:00:00')"
            )
            source.commit()
            source.close()

            MigrationV7Service(registry).apply(root)

            candidate = sqlite3.connect(resolve_active_database(storage).path)
            try:
                self.assertEqual(
                    1,
                    candidate.execute(
                        "SELECT count(*) FROM legacy_id_map "
                        "WHERE target_kind='placeholder'"
                    ).fetchone()[0],
                )
                self.assertEqual(
                    1,
                    candidate.execute(
                        "SELECT count(*) FROM memory_records "
                        "WHERE legacy_type='orphan:memories'"
                    ).fetchone()[0],
                )
                self.assertEqual(
                    2,
                    candidate.execute(
                        "SELECT count(*) FROM memory_relationship_versions"
                    ).fetchone()[0],
                )
            finally:
                candidate.close()

    def test_validation_replays_every_memory_projection_field(self):
        """Fields excluded from state_hash still must match the source event exactly."""
        from daem0nmcp.migrations.v7 import (
            MigrationInterrupted,
            MigrationV7Error,
            MigrationV7Service,
        )

        with tempfile.TemporaryDirectory() as raw:
            root, storage, registry = self._workspace(raw)

            def interrupt(stage, details):
                if (
                    stage == "after_batch"
                    and details["source_table"] == "memory_relationships"
                ):
                    raise MigrationInterrupted("tamper before validation")

            with self.assertRaises(MigrationInterrupted):
                MigrationV7Service(registry, fault_injector=interrupt).apply(None)
            partial = next(storage.glob("migrations/v7/mig_*/candidate.db.partial"))
            candidate = sqlite3.connect(partial)
            candidate.execute(
                "UPDATE memory_records SET keywords='tampered', recall_count=99, "
                "surprise_score=0.75 WHERE record_id=("
                "SELECT target_id FROM legacy_id_map "
                "WHERE source_table='memories' AND legacy_id='1')"
            )
            candidate.commit()
            candidate.close()

            with self.assertRaisesRegex(MigrationV7Error, "VALIDATION_FAILED"):
                MigrationV7Service(registry).apply(None)
            self.assertFalse((storage / "active-db.json").exists())

    def test_validated_partial_resumes_publish_without_rebuilding_manifests(self):
        from daem0nmcp.migrations.v7 import MigrationInterrupted, MigrationV7Service
        from daem0nmcp.storage_activation import resolve_active_database

        with tempfile.TemporaryDirectory() as raw:
            root, storage, registry = self._workspace(raw)

            def interrupt(stage, _details):
                if stage == "before_candidate_publish":
                    raise MigrationInterrupted("crash after validation")

            with self.assertRaises(MigrationInterrupted):
                MigrationV7Service(registry, fault_injector=interrupt).apply(None)
            partial = next(storage.glob("migrations/v7/mig_*/candidate.db.partial"))
            candidate = sqlite3.connect(partial)
            root_before = candidate.execute(
                "SELECT json_extract(validation_json,'$.event_root_hash') "
                "FROM v7_migration_runs"
            ).fetchone()[0]
            self.assertEqual(
                11,
                candidate.execute(
                    "SELECT count(*) FROM projection_manifests"
                ).fetchone()[0],
            )
            candidate.close()

            resumed = MigrationV7Service(registry).apply(None)
            self.assertEqual("resume", resumed.action)
            active = sqlite3.connect(resolve_active_database(storage).path)
            self.assertEqual(
                11,
                active.execute("SELECT count(*) FROM projection_manifests").fetchone()[
                    0
                ],
            )
            self.assertEqual(root_before, resumed.validation["event_root_hash"])
            active.close()

    def test_pointer_publication_failure_remains_resumable(self):
        import daem0nmcp.migrations.v7 as migration_module
        from daem0nmcp.migrations.v7 import MigrationV7Service
        from daem0nmcp.storage_activation import resolve_active_database

        with tempfile.TemporaryDirectory() as raw:
            root, storage, registry = self._workspace(raw)
            real_write = migration_module.write_active_pointer
            with (
                mock.patch.object(
                    migration_module,
                    "write_active_pointer",
                    side_effect=OSError("injected pointer failure"),
                ),
                self.assertRaisesRegex(OSError, "injected pointer failure"),
            ):
                MigrationV7Service(registry).apply(None)
            self.assertFalse((storage / "active-db.json").exists())
            self.assertEqual(
                1, len(list(storage.glob("migrations/v7/mig_*/candidate.db.partial")))
            )

            with mock.patch.object(
                migration_module, "write_active_pointer", side_effect=real_write
            ):
                resumed = MigrationV7Service(registry).apply(None)
            self.assertEqual("resume", resumed.action)
            self.assertEqual(7, resolve_active_database(storage).format_version)

    def test_rollback_switches_pointer_only_and_is_idempotent(self):
        """Rollback retains source/candidate and publishes a new generation."""
        from daem0nmcp.migrations.v7 import MigrationV7Service
        from daem0nmcp.storage_activation import resolve_active_database

        with tempfile.TemporaryDirectory() as raw:
            root, storage, registry = self._workspace(raw)
            service = MigrationV7Service(registry)
            migrated = service.apply(None)
            candidate_path = resolve_active_database(storage).path
            source_bytes = (storage / "daem0nmcp.db").read_bytes()
            rolled = service.rollback(None, migrated.migration_run_id)
            self.assertEqual("rolled_back", rolled.status)
            resolved = resolve_active_database(storage)
            self.assertEqual(6, resolved.format_version)
            self.assertEqual(2, resolved.generation)
            self.assertEqual(storage / "daem0nmcp.db", resolved.path)
            self.assertEqual(source_bytes, (storage / "daem0nmcp.db").read_bytes())
            self.assertTrue(candidate_path.is_file())
            rolled_candidate = sqlite3.connect(candidate_path)
            self.assertEqual(
                "rolled_back",
                rolled_candidate.execute(
                    "SELECT status FROM v7_migration_runs"
                ).fetchone()[0],
            )
            rolled_candidate.close()
            repeat = service.rollback(None, migrated.migration_run_id)
            self.assertEqual("already_rolled_back", repeat.action)

    def test_explicit_apply_reactivates_a_successfully_rolled_back_generation(self):
        """Reactivation reuses the validated candidate and advances generation."""
        from daem0nmcp.migrations.v7 import MigrationV7Service
        from daem0nmcp.storage_activation import resolve_active_database

        with tempfile.TemporaryDirectory() as raw:
            root, storage, registry = self._workspace(raw)
            service = MigrationV7Service(registry)
            migrated = service.apply(root)
            candidate = resolve_active_database(storage).path
            service.rollback(root, migrated.migration_run_id)

            try:
                reactivated = service.apply(root)
            except Exception as exc:
                self.fail(
                    "reactivation promoted a rollback-retained lexical manifest: "
                    f"{type(exc).__name__}"
                )

            resolved = resolve_active_database(storage)
            self.assertEqual("activated", reactivated.status)
            self.assertEqual("reactivate", reactivated.action)
            self.assertEqual(3, resolved.generation)
            self.assertEqual(candidate, resolved.path)
            self.assertEqual("daem0nmcp.db", resolved.previous_db)
            self.assertEqual("already_active", service.apply(root).action)

    def test_reactivation_preserves_only_the_previous_active_lexical_generation(self):
        from daem0nmcp.migrations.v7 import MigrationV7Service
        from daem0nmcp.retrieval.projections import LexicalProjectionBuilder
        from daem0nmcp.storage_activation import resolve_active_database

        with tempfile.TemporaryDirectory() as raw:
            root, storage, registry = self._workspace(raw)
            service = MigrationV7Service(registry)
            migrated = service.apply(root)
            candidate_path = resolve_active_database(storage).path
            candidate = sqlite3.connect(candidate_path)
            try:
                builder = LexicalProjectionBuilder(candidate)
                builder.rebuild(registry.default.workspace_id)
                builder.rebuild(registry.default.workspace_id)
                candidate.commit()
            finally:
                candidate.close()
            service.rollback(root, migrated.migration_run_id)

            try:
                reactivated = service.apply(root)
            except Exception as exc:
                self.fail(
                    "reactivation promoted a rollback-retained lexical manifest: "
                    f"{type(exc).__name__}"
                )

            self.assertEqual("reactivate", reactivated.action)
            active = sqlite3.connect(resolve_active_database(storage).path)
            try:
                self.assertEqual(
                    [(1, "ready"), (2, "ready"), (3, "active")],
                    active.execute(
                        "SELECT generation,status FROM projection_manifests "
                        "WHERE projection_name='lexical' ORDER BY generation"
                    ).fetchall(),
                )
            finally:
                active.close()

    def test_rollback_pointer_crash_converges_via_rollback_or_apply_retry(self):
        """A durable rollback pointer remains recoverable before metadata commit."""
        from daem0nmcp.migrations.v7 import MigrationInterrupted, MigrationV7Service
        from daem0nmcp.storage_activation import resolve_active_database

        for retry_action in ("rollback", "apply"):
            with (
                self.subTest(retry_action=retry_action),
                tempfile.TemporaryDirectory() as raw,
            ):
                root, storage, registry = self._workspace(raw)
                migrated = MigrationV7Service(registry).apply(root)
                candidate = resolve_active_database(storage).path

                def stop_after_pointer(stage, _details):
                    if stage == "after_rollback_pointer":
                        raise MigrationInterrupted("rollback metadata not committed")

                with self.assertRaises(MigrationInterrupted):
                    MigrationV7Service(
                        registry, fault_injector=stop_after_pointer
                    ).rollback(root, migrated.migration_run_id)
                rolled_pointer = resolve_active_database(storage)
                self.assertEqual(6, rolled_pointer.format_version)
                self.assertEqual(2, rolled_pointer.generation)
                connection = sqlite3.connect(candidate)
                self.assertEqual(
                    "active",
                    connection.execute(
                        "SELECT status FROM v7_migration_runs"
                    ).fetchone()[0],
                )
                connection.close()

                service = MigrationV7Service(registry)
                if retry_action == "rollback":
                    recovered = service.rollback(root, migrated.migration_run_id)
                    self.assertEqual("rolled_back", recovered.status)
                    self.assertEqual(2, resolve_active_database(storage).generation)
                    connection = sqlite3.connect(candidate)
                    self.assertEqual(
                        "rolled_back",
                        connection.execute(
                            "SELECT status FROM v7_migration_runs"
                        ).fetchone()[0],
                    )
                    connection.close()
                else:
                    recovered = service.apply(root)
                    self.assertEqual("activated", recovered.status)
                    self.assertEqual(3, resolve_active_database(storage).generation)
                    self.assertEqual(candidate, resolve_active_database(storage).path)

    def test_reactivation_validates_evolved_v7_authority_not_original_v6_rows(self):
        """Legitimate post-migration events survive rollback and reactivation."""
        from daem0nmcp.event_store import EventCommand, EventStore
        from daem0nmcp.migrations.v7 import MigrationV7Service
        from daem0nmcp.storage_activation import resolve_active_database

        with tempfile.TemporaryDirectory() as raw:
            root, storage, registry = self._workspace(raw)
            service = MigrationV7Service(registry)
            migrated = service.apply(root)
            candidate_path = resolve_active_database(storage).path
            candidate = sqlite3.connect(candidate_path)
            candidate.row_factory = sqlite3.Row
            mapping = candidate.execute(
                "SELECT target_id FROM legacy_id_map "
                "WHERE source_table='memories' AND legacy_id='1'"
            ).fetchone()
            stream_id = str(mapping[0])
            prior = candidate.execute(
                "SELECT payload_json FROM memory_events "
                "WHERE stream_id=? ORDER BY stream_version DESC LIMIT 1",
                (stream_id,),
            ).fetchone()
            record = dict(json.loads(prior[0])["record"])
            record["content"] = "Evolved through v7 after migration"
            evolved = EventStore(candidate).append_and_project(
                EventCommand(
                    workspace_id=registry.default.workspace_id,
                    stream_id=stream_id,
                    stream_kind="memory",
                    event_type="memory.updated",
                    occurred_at_us=2_000_000_000_000_000,
                    recorded_at_us=2_000_000_000_000_000,
                    actor_type="system",
                    payload={
                        "record": record,
                        "compatibility": {"legacy_memory_id": 1},
                    },
                )
            )
            candidate.execute(
                "UPDATE memories SET content=? WHERE id=1",
                ("Evolved through v7 after migration",),
            )
            candidate.commit()
            candidate.close()

            service.rollback(root, migrated.migration_run_id)
            reactivated = service.apply(root)

            self.assertEqual("reactivate", reactivated.action)
            active = sqlite3.connect(resolve_active_database(storage).path)
            try:
                self.assertEqual(
                    1,
                    active.execute(
                        "SELECT count(*) FROM memory_events WHERE event_id=?",
                        (evolved.event_id,),
                    ).fetchone()[0],
                )
                self.assertEqual(
                    "Evolved through v7 after migration",
                    active.execute(
                        "SELECT content FROM memory_records WHERE record_id=?",
                        (stream_id,),
                    ).fetchone()[0],
                )
            finally:
                active.close()
            self.assertEqual("already_active", service.apply(root).action)

    def test_reactivation_rejects_current_memory_compatibility_divergence(self):
        """A current v6 row may not diverge from legitimate retained v7 activity."""
        from daem0nmcp.event_store import EventCommand, EventStore
        from daem0nmcp.migrations.v7 import MigrationV7Error, MigrationV7Service
        from daem0nmcp.storage_activation import resolve_active_database

        with tempfile.TemporaryDirectory() as raw:
            root, storage, registry = self._workspace(raw)
            service = MigrationV7Service(registry)
            migrated = service.apply(root)
            candidate_path = resolve_active_database(storage).path
            candidate = sqlite3.connect(candidate_path)
            candidate.row_factory = sqlite3.Row
            stream_id = str(
                candidate.execute(
                    "SELECT target_id FROM legacy_id_map "
                    "WHERE source_table='memories' AND legacy_id='1'"
                ).fetchone()[0]
            )
            prior = candidate.execute(
                "SELECT payload_json FROM memory_events WHERE stream_id=? "
                "ORDER BY stream_version DESC LIMIT 1",
                (stream_id,),
            ).fetchone()
            record = dict(json.loads(prior[0])["record"])
            record["content"] = "Legitimate retained v7 state"
            evolved = EventStore(candidate).append_and_project(
                EventCommand(
                    workspace_id=registry.default.workspace_id,
                    stream_id=stream_id,
                    stream_kind="memory",
                    event_type="memory.updated",
                    occurred_at_us=2_000_000_000_000_001,
                    recorded_at_us=2_000_000_000_000_001,
                    actor_type="system",
                    payload={
                        "record": record,
                        "compatibility": {"legacy_memory_id": 1},
                    },
                )
            )
            candidate.execute(
                "UPDATE memories SET content='Legitimate retained v7 state' WHERE id=1"
            )
            candidate.commit()
            candidate.close()
            service.rollback(root, migrated.migration_run_id)

            candidate = sqlite3.connect(candidate_path)
            candidate.execute(
                "UPDATE memories SET content='compatibility-only tamper' WHERE id=1"
            )
            candidate.commit()
            candidate.close()
            pointer_before = resolve_active_database(storage)

            with self.assertRaisesRegex(MigrationV7Error, "VALIDATION_FAILED"):
                service.apply(root)

            pointer_after = resolve_active_database(storage)
            self.assertEqual(pointer_before.pointer_bytes, pointer_after.pointer_bytes)
            candidate = sqlite3.connect(candidate_path)
            try:
                self.assertEqual(
                    1,
                    candidate.execute(
                        "SELECT count(*) FROM memory_events WHERE event_id=?",
                        (evolved.event_id,),
                    ).fetchone()[0],
                )
                self.assertEqual(
                    "Legitimate retained v7 state",
                    candidate.execute(
                        "SELECT content FROM memory_records WHERE record_id=?",
                        (stream_id,),
                    ).fetchone()[0],
                )
            finally:
                candidate.close()

    def test_reactivation_rejects_current_fact_and_relationship_divergence(self):
        """Current fact and edge rows must match their unique live typed claims."""
        from daem0nmcp.migrations.v7 import MigrationV7Error, MigrationV7Service
        from daem0nmcp.storage_activation import resolve_active_database

        mutations = (
            (
                "facts",
                "UPDATE facts SET content='compatibility-only tamper' WHERE id=1",
            ),
            (
                "memory_relationships",
                "UPDATE memory_relationships SET description="
                "'compatibility-only tamper' WHERE id=1",
            ),
        )
        for table, statement in mutations:
            with self.subTest(table=table), tempfile.TemporaryDirectory() as raw:
                root, storage, registry = self._workspace(raw)
                service = MigrationV7Service(registry)
                migrated = service.apply(root)
                candidate_path = resolve_active_database(storage).path
                service.rollback(root, migrated.migration_run_id)
                candidate = sqlite3.connect(candidate_path)
                candidate.execute(statement)
                candidate.commit()
                candidate.close()
                pointer_before = resolve_active_database(storage)

                with self.assertRaisesRegex(MigrationV7Error, "VALIDATION_FAILED"):
                    service.apply(root)

                self.assertEqual(
                    pointer_before.pointer_bytes,
                    resolve_active_database(storage).pointer_bytes,
                )

    def test_reactivation_rejects_ambiguous_live_compatibility_claims(self):
        """Retained validation may not choose arbitrarily between live ID claims."""
        from daem0nmcp.event_store import EventCommand, EventStore
        from daem0nmcp.migrations.v7 import MigrationV7Error, MigrationV7Service
        from daem0nmcp.storage_activation import resolve_active_database

        with tempfile.TemporaryDirectory() as raw:
            root, storage, registry = self._workspace(raw)
            service = MigrationV7Service(registry)
            migrated = service.apply(root)
            candidate_path = resolve_active_database(storage).path
            candidate = sqlite3.connect(candidate_path)
            EventStore(candidate).append_and_project(
                EventCommand(
                    workspace_id=registry.default.workspace_id,
                    stream_id="mem_" + "9" * 64,
                    stream_kind="memory",
                    event_type="memory.created",
                    occurred_at_us=2_000_000_000_000_002,
                    recorded_at_us=2_000_000_000_000_002,
                    actor_type="system",
                    payload={
                        "record": {
                            "record_type": "decision",
                            "legacy_type": None,
                            "content": "ambiguous claim",
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
                            "source_client": None,
                            "source_model": None,
                            "deleted_at_us": None,
                        },
                        "compatibility": {"legacy_memory_id": 1},
                    },
                )
            )
            candidate.commit()
            candidate.close()
            service.rollback(root, migrated.migration_run_id)

            with self.assertRaisesRegex(MigrationV7Error, "VALIDATION_FAILED"):
                service.apply(root)
            self.assertEqual(6, resolve_active_database(storage).format_version)

    def test_manager_shared_lock_blocks_apply_without_creating_run(self):
        from daem0nmcp.migrations.v7 import MigrationV7Service
        from daem0nmcp.storage_activation import DatabaseFileLock, DatabaseInUseError

        with tempfile.TemporaryDirectory() as raw:
            root, storage, registry = self._workspace(raw)
            with (
                DatabaseFileLock(storage, "shared"),
                self.assertRaises(DatabaseInUseError),
            ):
                MigrationV7Service(registry).apply(None)
            self.assertFalse((storage / "migrations").exists())
            self.assertFalse((storage / "active-db.json").exists())

    def test_already_active_validation_rejects_schema_version_twenty(self):
        """A published v7 candidate must include governance migration 21."""

        from daem0nmcp.migrations.v7 import MigrationV7Error, MigrationV7Service
        from daem0nmcp.storage_activation import resolve_active_database

        with tempfile.TemporaryDirectory() as raw:
            root, storage, registry = self._workspace(raw)
            service = MigrationV7Service(registry)
            service.apply(root)
            candidate_path = resolve_active_database(storage).path
            connection = sqlite3.connect(candidate_path)
            try:
                connection.execute("DELETE FROM schema_version WHERE version=21")
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(MigrationV7Error, "ACTIVE_V7_INVALID"):
                service.apply(root)

    def test_already_active_validation_rejects_each_missing_additive_table(self):
        """Activation fails closed when a migration 18/19/20/21 table is absent."""

        from daem0nmcp.migrations.v7 import MigrationV7Error, MigrationV7Service
        from daem0nmcp.storage_activation import resolve_active_database

        required = (
            "dense_projection_refs",
            "record_outcome_view",
            "record_procedures",
            "retrieval_documents",
            "public_object_ids",
            "active_context_entries",
            "governance_events",
            "governance_rules",
            "governance_context_triggers",
            "session_update_sequence",
        )
        with tempfile.TemporaryDirectory() as raw:
            root, storage, registry = self._workspace(raw)
            service = MigrationV7Service(registry)
            service.apply(root)
            candidate_path = resolve_active_database(storage).path
            for missing in required:
                with self.subTest(missing=missing):
                    connection = sqlite3.connect(candidate_path)
                    try:
                        create_sql = connection.execute(
                            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                            (missing,),
                        ).fetchone()
                        self.assertIsNotNone(create_sql)
                        connection.execute(f'DROP TABLE "{missing}"')
                        connection.commit()
                    finally:
                        connection.close()
                    try:
                        with self.assertRaisesRegex(
                            MigrationV7Error, "ACTIVE_V7_INVALID"
                        ):
                            service.apply(root)
                    finally:
                        connection = sqlite3.connect(candidate_path)
                        try:
                            connection.execute(create_sql[0])
                            connection.commit()
                        finally:
                            connection.close()

    def test_apply_does_not_trust_format_seven_pointer_without_schema_or_active_run(
        self,
    ):
        from daem0nmcp.migrations.v7 import MigrationV7Error, MigrationV7Service

        with tempfile.TemporaryDirectory() as raw:
            root, storage, registry = self._workspace(raw)
            write_active_pointer(
                storage,
                ActiveDatabasePointer(7, 1, "daem0nmcp.db", None, None),
            )
            with self.assertRaisesRegex(MigrationV7Error, "ACTIVE_V7_INVALID"):
                MigrationV7Service(registry).apply(None)
            with self.assertRaisesRegex(MigrationV7Error, "ACTIVE_V7_INVALID"):
                MigrationV7Service(registry).rollback(None)


if __name__ == "__main__":
    unittest.main()
