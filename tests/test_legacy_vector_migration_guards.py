"""Dependency-free regressions for legacy vector writers at the v7 boundary."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

_OLD_VECTOR = b"o" * (384 * 4)
_NEW_VECTOR = b"new-vector"


def _create_legacy_memory_database(path: Path, embedding: bytes | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE schema_version(
                version INTEGER PRIMARY KEY,
                applied_at TEXT
            );
            INSERT INTO schema_version(version) VALUES (18);
            CREATE TABLE memories(
                id INTEGER PRIMARY KEY,
                content TEXT NOT NULL,
                rationale TEXT,
                category TEXT NOT NULL,
                tags TEXT,
                file_path TEXT,
                worked INTEGER,
                is_permanent INTEGER NOT NULL DEFAULT 0,
                vector_embedding BLOB
            );
            """
        )
        connection.execute(
            """
            INSERT INTO memories(
                id, content, rationale, category, tags, file_path, worked,
                is_permanent, vector_embedding
            ) VALUES (1, 'legacy content', 'legacy rationale', 'decision',
                      'legacy,test', 'legacy.py', 1, 0, ?)
            """,
            (embedding,),
        )
        connection.commit()
    finally:
        connection.close()


def _read_embedding(path: Path) -> bytes | None:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT vector_embedding FROM memories WHERE id=1"
        ).fetchone()
        return None if row is None else row[0]
    finally:
        connection.close()


def _read_schema_version(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        return int(
            connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_version"
            ).fetchone()[0]
        )
    finally:
        connection.close()


def _add_pointerless_v7_signature(path: Path) -> None:
    required = {
        "memory_events",
        "memory_records",
        "memory_fact_versions",
        "memory_relationship_versions",
        "projection_manifests",
        "enrichment_decisions",
        "background_jobs",
        "v7_migration_runs",
        "v7_migration_checkpoints",
        "legacy_id_map",
        "retrieval_documents",
        "record_procedures",
        "record_outcome_view",
        "dense_projection_refs",
    }
    connection = sqlite3.connect(path)
    try:
        for table in sorted(required):
            connection.execute(f'CREATE TABLE "{table}"(marker INTEGER)')
        connection.execute("INSERT INTO memory_records(marker) VALUES (1)")
        connection.commit()
    finally:
        connection.close()


def _make_v7_storage(root: Path, embedding: bytes | None) -> tuple[Path, Path]:
    from daem0nmcp.storage_activation import (
        ActiveDatabasePointer,
        write_active_pointer,
    )

    storage = root / ".daem0nmcp" / "storage"
    run_id = "mig_" + "a" * 64
    active = storage / "migrations" / "v7" / run_id / "candidate.db"
    source = storage / "daem0nmcp.db"
    _create_legacy_memory_database(source, embedding)
    _create_legacy_memory_database(active, embedding)
    write_active_pointer(
        storage,
        ActiveDatabasePointer(
            format_version=7,
            generation=1,
            active_db=f"migrations/v7/{run_id}/candidate.db",
            previous_db="daem0nmcp.db",
            migration_run_id=run_id,
        ),
    )
    return storage, active


def _probe_exclusive_lock(storage: Path, calls: list[str], label: str) -> None:
    from daem0nmcp.storage_activation import DatabaseFileLock, DatabaseInUseError

    try:
        with DatabaseFileLock(storage, "exclusive"):
            calls.append(f"{label}:unlocked")
    except DatabaseInUseError:
        calls.append(f"{label}:locked")


def _fake_vector_modules(
    calls: list[str],
    *,
    on_encode=None,
    on_qdrant_init=None,
    on_qdrant_upsert=None,
):
    vectors = types.ModuleType("daem0nmcp.vectors")

    def encode_document(text: str) -> bytes:
        calls.append(f"encode:{text}")
        if on_encode is not None:
            on_encode()
        return _NEW_VECTOR

    def decode(value: bytes) -> list[float]:
        calls.append(f"decode:{len(value)}")
        return [0.25, 0.75]

    vectors.encode_document = encode_document
    vectors.decode = decode
    vectors.is_available = lambda: True

    qdrant_store = types.ModuleType("daem0nmcp.qdrant_store")

    class FakeQdrantVectorStore:
        def __init__(self, path: str) -> None:
            calls.append(f"qdrant-init:{path}")
            if on_qdrant_init is not None:
                on_qdrant_init()

        def upsert_memory(self, memory_id, embedding, metadata) -> None:
            calls.append(f"qdrant-upsert:{memory_id}")
            if on_qdrant_upsert is not None:
                on_qdrant_upsert()

        def close(self) -> None:
            calls.append("qdrant-close")

    qdrant_store.QdrantVectorStore = FakeQdrantVectorStore
    return vectors, qdrant_store


@contextlib.contextmanager
def _installed_fake_vector_modules(calls: list[str], **callbacks):
    import daem0nmcp

    vectors, qdrant_store = _fake_vector_modules(calls, **callbacks)
    with (
        patch.dict(
            sys.modules,
            {
                "daem0nmcp.vectors": vectors,
                "daem0nmcp.qdrant_store": qdrant_store,
            },
        ),
        patch.object(daem0nmcp, "vectors", vectors, create=True),
    ):
        yield


class EmbeddingModelMigrationGuardTests(unittest.TestCase):
    def test_pointerless_canonical_v7_copy_refuses_reencoding(self):
        from daem0nmcp.migrations import migrate_embedding_model

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            storage = root / ".daem0nmcp" / "storage"
            database = storage / "daem0nmcp.db"
            _create_legacy_memory_database(database, _OLD_VECTOR)
            _add_pointerless_v7_signature(database)
            calls: list[str] = []

            with (
                _installed_fake_vector_modules(calls),
                patch.object(
                    sys,
                    "argv",
                    ["migrate_embedding_model", "--project-path", str(root)],
                ),
                self.assertLogs(migrate_embedding_model.logger, level="ERROR"),
                self.assertRaises(SystemExit) as raised,
            ):
                migrate_embedding_model.main()

            self.assertEqual(1, raised.exception.code)
            self.assertEqual(_OLD_VECTOR, _read_embedding(database))
            self.assertEqual([], calls)

    def test_v7_active_database_refuses_before_sqlite_or_qdrant_mutation(self):
        from daem0nmcp.migrations import migrate_embedding_model

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            storage, active = _make_v7_storage(root, _OLD_VECTOR)
            (storage / "qdrant").mkdir()
            calls: list[str] = []

            with (
                _installed_fake_vector_modules(calls),
                patch.object(
                    sys,
                    "argv",
                    [
                        "migrate_embedding_model",
                        "--project-path",
                        str(root),
                        "--batch-size",
                        "1",
                    ],
                ),
                self.assertLogs(migrate_embedding_model.logger, level="ERROR") as logs,
                self.assertRaises(SystemExit) as raised,
            ):
                migrate_embedding_model.main()

            self.assertEqual(1, raised.exception.code)
            self.assertIn("format 7", " ".join(logs.output).lower())
            self.assertIn("rebuild-projection", " ".join(logs.output))
            self.assertEqual(_OLD_VECTOR, _read_embedding(active))
            self.assertEqual([], calls)

    def test_pointerless_v6_database_still_reencodes_and_upserts(self):
        from daem0nmcp.migrations import migrate_embedding_model

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            storage = root / ".daem0nmcp" / "storage"
            database = storage / "daem0nmcp.db"
            _create_legacy_memory_database(database, _OLD_VECTOR)
            (storage / "qdrant").mkdir()
            calls: list[str] = []

            with (
                _installed_fake_vector_modules(
                    calls,
                    on_encode=lambda: _probe_exclusive_lock(storage, calls, "encode"),
                    on_qdrant_init=lambda: _probe_exclusive_lock(
                        storage, calls, "qdrant-init"
                    ),
                    on_qdrant_upsert=lambda: _probe_exclusive_lock(
                        storage, calls, "qdrant-upsert"
                    ),
                ),
                patch.object(
                    sys,
                    "argv",
                    [
                        "migrate_embedding_model",
                        "--project-path",
                        str(root),
                        "--batch-size",
                        "1",
                    ],
                ),
            ):
                migrate_embedding_model.main()

            self.assertEqual(_NEW_VECTOR, _read_embedding(database))
            self.assertTrue(any(call.startswith("encode:") for call in calls))
            self.assertIn("qdrant-upsert:1", calls)
            self.assertIn("encode:locked", calls)
            self.assertIn("qdrant-init:locked", calls)
            self.assertIn("qdrant-upsert:locked", calls)


class QdrantMigrationGuardTests(unittest.TestCase):
    def test_pointerless_canonical_v7_copy_refuses_before_qdrant(self):
        from daem0nmcp.migrations import migrate_vectors

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            storage = root / ".daem0nmcp" / "storage"
            database = storage / "daem0nmcp.db"
            _create_legacy_memory_database(database, _OLD_VECTOR)
            _add_pointerless_v7_signature(database)

            with self.assertRaisesRegex(RuntimeError, "v6-only"):
                asyncio.run(migrate_vectors.run_migration(str(root)))

            self.assertEqual(_OLD_VECTOR, _read_embedding(database))
            self.assertFalse((storage / "qdrant").exists())

    def test_direct_v7_helper_refuses_before_database_or_qdrant_use(self):
        from daem0nmcp.migrations import migrate_vectors

        calls: list[str] = []

        class FakeDatabase:
            format_version = 7

            async def init_db(self) -> None:
                calls.append("database-init")

        class FakeQdrant:
            def get_count(self) -> int:
                calls.append("qdrant-count")
                return 0

            def upsert_memory(self, *args, **kwargs) -> None:
                calls.append("qdrant-upsert")

        with self.assertRaisesRegex(RuntimeError, "v6-only"):
            asyncio.run(
                migrate_vectors.migrate_vectors_to_qdrant(FakeDatabase(), FakeQdrant())
            )

        self.assertEqual([], calls)

    def test_v7_active_database_refuses_before_optional_imports_or_qdrant_creation(
        self,
    ):
        from daem0nmcp.migrations import migrate_vectors

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            storage, active = _make_v7_storage(root, _OLD_VECTOR)

            with self.assertRaisesRegex(RuntimeError, "v6-only"):
                asyncio.run(migrate_vectors.run_migration(str(root)))

            self.assertEqual(_OLD_VECTOR, _read_embedding(active))
            self.assertFalse((storage / "qdrant").exists())

    def test_post_resolution_format_flip_refuses_before_qdrant_path_or_constructor(
        self,
    ):
        from daem0nmcp.config import Settings
        from daem0nmcp.migrations import migrate_vectors

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            storage = root / ".daem0nmcp" / "storage"
            _create_legacy_memory_database(storage / "daem0nmcp.db", _OLD_VECTOR)
            calls: list[str] = []
            database_module = types.ModuleType("daem0nmcp.database")
            qdrant_module = types.ModuleType("daem0nmcp.qdrant_store")

            class FlippedDatabaseManager:
                format_version = 7

                def __init__(self, storage_path: str) -> None:
                    calls.append("database-init")

                async def close(self) -> None:
                    calls.append("database-close")

            class ForbiddenQdrant:
                def __init__(self, path: str) -> None:
                    calls.append("qdrant-init")

            database_module.DatabaseManager = FlippedDatabaseManager
            qdrant_module.QdrantVectorStore = ForbiddenQdrant

            def qdrant_path(settings_self):
                calls.append("qdrant-path")
                return str(storage / "qdrant")

            with (
                patch.dict(
                    sys.modules,
                    {
                        "daem0nmcp.database": database_module,
                        "daem0nmcp.qdrant_store": qdrant_module,
                    },
                ),
                patch.object(Settings, "get_qdrant_path", qdrant_path),
                self.assertRaisesRegex(RuntimeError, "v6-only"),
            ):
                asyncio.run(migrate_vectors.run_migration(str(root)))

            self.assertEqual(["database-init", "database-close"], calls)

    def test_pointerless_v6_run_still_constructs_and_executes_migration(self):
        from daem0nmcp.migrations import migrate_vectors

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            storage = root / ".daem0nmcp" / "storage"
            database = storage / "daem0nmcp.db"
            _create_legacy_memory_database(database, _OLD_VECTOR)
            calls: list[str] = []

            database_module = types.ModuleType("daem0nmcp.database")
            qdrant_module = types.ModuleType("daem0nmcp.qdrant_store")

            class FakeDatabaseManager:
                def __init__(self, storage_path: str) -> None:
                    calls.append(f"database-init:{storage_path}")
                    self.format_version = 6

                async def close(self) -> None:
                    calls.append("database-close")

            class FakeQdrantVectorStore:
                def __init__(self, path: str) -> None:
                    calls.append(f"qdrant-init:{path}")
                    _probe_exclusive_lock(storage, calls, "qdrant-init")

                def close(self) -> None:
                    calls.append("qdrant-close")

            database_module.DatabaseManager = FakeDatabaseManager
            qdrant_module.QdrantVectorStore = FakeQdrantVectorStore

            async def fake_migrate(db, qdrant, progress_callback):
                calls.append("migration-called")
                _probe_exclusive_lock(storage, calls, "migration")
                progress_callback(1, 1)
                return {
                    "migrated": 1,
                    "skipped": 0,
                    "failed": 0,
                    "total": 1,
                    "errors": [],
                }

            with (
                patch.dict(
                    sys.modules,
                    {
                        "daem0nmcp.database": database_module,
                        "daem0nmcp.qdrant_store": qdrant_module,
                    },
                ),
                patch.object(
                    migrate_vectors, "migrate_vectors_to_qdrant", fake_migrate
                ),
            ):
                result = asyncio.run(migrate_vectors.run_migration(str(root)))

            self.assertEqual(1, result["migrated"])
            self.assertIn("migration-called", calls)
            self.assertIn("database-close", calls)
            self.assertIn("qdrant-close", calls)
            self.assertIn("qdrant-init:locked", calls)
            self.assertIn("migration:locked", calls)

    def test_pointerless_v6_cli_uses_schema_15_initializer_and_migrates(self):
        import daem0nmcp
        from daem0nmcp.config import settings
        from daem0nmcp.migrations import migrate_vectors

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            storage = root / ".daem0nmcp" / "storage"
            database = storage / "daem0nmcp.db"
            _create_legacy_memory_database(database, _OLD_VECTOR)
            connection = sqlite3.connect(database)
            try:
                connection.execute("DELETE FROM schema_version")
                connection.execute("INSERT INTO schema_version(version) VALUES (15)")
                connection.commit()
            finally:
                connection.close()

            calls: list[str] = []
            memory = types.SimpleNamespace(
                id=1,
                vector_embedding=_OLD_VECTOR,
                category="decision",
                tags=["legacy", "test"],
                file_path="legacy.py",
                worked=True,
                is_permanent=False,
            )

            class FakeScalars:
                def all(self):
                    return [memory]

            class FakeResult:
                def scalars(self):
                    return FakeScalars()

            class FakeSession:
                async def execute(self, query):
                    return FakeResult()

            @contextlib.asynccontextmanager
            async def fake_session_scope():
                yield FakeSession()

            database_module = types.ModuleType("daem0nmcp.database")

            class FakeDatabaseManager:
                format_version = 6

                def __init__(self, storage_path: str) -> None:
                    self.db_path = Path(storage_path) / "daem0nmcp.db"
                    calls.append(f"database-init:{storage_path}")

                async def init_db(self) -> None:
                    calls.append("uncapped-init")
                    current = sqlite3.connect(self.db_path)
                    try:
                        current.executemany(
                            "INSERT INTO schema_version(version) VALUES (?)",
                            [(version,) for version in range(16, 24)],
                        )
                        current.commit()
                    finally:
                        current.close()

                async def init_legacy_v6(self) -> None:
                    calls.append("legacy-init:15")

                def get_session(self):
                    return fake_session_scope()

                async def close(self) -> None:
                    calls.append("database-close")

            database_module.DatabaseManager = FakeDatabaseManager

            class FakeVectorColumn:
                def isnot(self, value):
                    return ("is-not", value)

            models_module = types.ModuleType("daem0nmcp.models")
            models_module.Memory = types.SimpleNamespace(
                vector_embedding=FakeVectorColumn()
            )

            class FakeQuery:
                def where(self, predicate):
                    return self

            sqlalchemy_module = types.ModuleType("sqlalchemy")
            sqlalchemy_module.select = lambda model: FakeQuery()

            vectors_module = types.ModuleType("daem0nmcp.vectors")
            vectors_module.decode = lambda value: [0.25] * settings.embedding_dimension

            qdrant_module = types.ModuleType("daem0nmcp.qdrant_store")

            class FakeQdrantVectorStore:
                def __init__(self, path: str) -> None:
                    calls.append(f"qdrant-init:{path}")

                def get_count(self) -> int:
                    return 0

                def upsert_memory(self, memory_id, embedding, metadata) -> None:
                    calls.append(f"qdrant-upsert:{memory_id}")

                def close(self) -> None:
                    calls.append("qdrant-close")

            qdrant_module.QdrantVectorStore = FakeQdrantVectorStore

            with (
                patch.dict(
                    sys.modules,
                    {
                        "daem0nmcp.database": database_module,
                        "daem0nmcp.models": models_module,
                        "daem0nmcp.qdrant_store": qdrant_module,
                        "daem0nmcp.vectors": vectors_module,
                        "sqlalchemy": sqlalchemy_module,
                    },
                ),
                patch.object(daem0nmcp, "vectors", vectors_module, create=True),
            ):
                result = asyncio.run(migrate_vectors.run_migration(str(root)))

            self.assertEqual(1, result["migrated"])
            self.assertEqual(15, _read_schema_version(database))
            self.assertIn("legacy-init:15", calls)
            self.assertNotIn("uncapped-init", calls)
            self.assertIn("qdrant-upsert:1", calls)


class UpgradeVectorPhaseGuardTests(unittest.TestCase):
    def test_upgrade_script_assesses_source_checkout_before_package_install(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "upgrade.py"
        with tempfile.TemporaryDirectory() as raw:
            project = Path(raw)
            database = project / ".daem0nmcp" / "storage" / "daem0nmcp.db"
            _create_legacy_memory_database(database, _OLD_VECTOR)

            for isolated in (False, True):
                with self.subTest(isolated=isolated):
                    command = [sys.executable]
                    if isolated:
                        command.append("-I")
                    command.extend(
                        [
                            str(script),
                            "--project",
                            str(project),
                            "--no-install",
                        ]
                    )
                    completed = subprocess.run(
                        command,
                        cwd=project,
                        capture_output=True,
                        text=True,
                        timeout=30,
                        check=False,
                    )
                    self.assertEqual(
                        0,
                        completed.returncode,
                        completed.stdout + completed.stderr,
                    )
                    self.assertIn("UPGRADE ASSESSMENT", completed.stdout)
                    self.assertNotIn("ModuleNotFoundError", completed.stderr)

    def test_pointerless_canonical_v7_copy_classifies_current_and_refuses_writer(self):
        from scripts import upgrade

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            storage = root / ".daem0nmcp" / "storage"
            database = storage / "daem0nmcp.db"
            _create_legacy_memory_database(database, _OLD_VECTOR)
            _add_pointerless_v7_signature(database)

            fingerprint = upgrade.detect_version(str(root))
            self.assertEqual(7, fingerprint.format_version)
            self.assertEqual("v7", fingerprint.estimated_version)
            with self.assertRaisesRegex(RuntimeError, "v6-only"):
                upgrade._migrate_embeddings(
                    fingerprint.db_path,
                    format_version=fingerprint.format_version,
                )
            self.assertEqual(_OLD_VECTOR, _read_embedding(database))

    def test_upgrade_rejects_spoofed_or_noncanonical_active_pointer(self):
        from scripts import upgrade

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            storage = root / ".daem0nmcp" / "storage"
            run_id = "mig_" + "b" * 64
            active_name = f"migrations/v7/{run_id}/candidate.db"
            active = storage / active_name
            _create_legacy_memory_database(storage / "daem0nmcp.db", _OLD_VECTOR)
            _create_legacy_memory_database(active, _OLD_VECTOR)
            valid = {
                "active_db": active_name,
                "format_version": 7,
                "generation": 2,
                "migration_run_id": run_id,
                "previous_db": "daem0nmcp.db",
            }
            invalid_pointer_bytes = (
                json.dumps(
                    {
                        **valid,
                        "format_version": 6,
                        "generation": 0,
                        "migration_run_id": None,
                        "previous_db": None,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8"),
                json.dumps(valid, indent=2).encode("utf-8"),
                (
                    '{"active_db":"'
                    + active_name
                    + '","format_version":7,"format_version":6,'
                    + '"generation":2,"migration_run_id":"'
                    + run_id
                    + '","previous_db":"daem0nmcp.db"}'
                ).encode("utf-8"),
            )

            for raw_pointer in invalid_pointer_bytes:
                with self.subTest(raw_pointer=raw_pointer):
                    (storage / "active-db.json").write_bytes(raw_pointer)
                    with self.assertRaises(ValueError):
                        upgrade.resolve_db_path(str(root))
                    self.assertEqual(_OLD_VECTOR, _read_embedding(active))

    def test_dangling_active_pointer_never_falls_back_to_v6_database(self):
        from scripts import upgrade

        source = (
            Path(__file__).resolve().parents[1] / "scripts" / "upgrade.py"
        ).read_text(encoding="utf-8")
        self.assertIn("resolve_active_database(storage)", source)

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            storage = root / ".daem0nmcp" / "storage"
            database = storage / "daem0nmcp.db"
            _create_legacy_memory_database(database, _OLD_VECTOR)
            try:
                os.symlink("missing-pointer-target", storage / "active-db.json")
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable in this environment")

            with self.assertRaisesRegex(ValueError, "active-db"):
                upgrade.resolve_db_path(str(root))
            self.assertEqual(_OLD_VECTOR, _read_embedding(database))

    def test_v7_selection_is_preserved_and_writer_refuses_before_mutation(self):
        from scripts import upgrade

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            storage, active = _make_v7_storage(root, _OLD_VECTOR)
            (storage / "qdrant").mkdir()

            fingerprint = upgrade.detect_version(str(root))
            self.assertEqual(7, fingerprint.format_version)
            with self.assertRaisesRegex(RuntimeError, "v6-only"):
                upgrade._migrate_embeddings(
                    fingerprint.db_path,
                    batch_size=1,
                    format_version=fingerprint.format_version,
                )

            calls: list[str] = []
            with (
                _installed_fake_vector_modules(calls),
                self.assertRaisesRegex(RuntimeError, "v6-only"),
            ):
                upgrade._migrate_embeddings(
                    fingerprint.db_path,
                    batch_size=1,
                    format_version=6,
                )

            self.assertEqual(_OLD_VECTOR, _read_embedding(active))
            self.assertEqual([], calls)

    def test_v7_ignores_retained_legacy_blob_and_classifies_current(self):
        from scripts import upgrade

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, active = _make_v7_storage(root, _OLD_VECTOR)
            fingerprint = upgrade.detect_version(str(root))
            state = upgrade.ProjectState(str(root), fingerprint)
            state.classify()
            calls: list[str] = []

            def forbidden_writer(*args, **kwargs):
                calls.append("writer-called")
                raise AssertionError("format 7 reached the legacy embedding writer")

            with patch.object(upgrade, "_migrate_embeddings", forbidden_writer):
                upgrade.run_phase3([state], skip_embeddings=False, auto_yes=True)

            self.assertEqual("v7", fingerprint.estimated_version)
            self.assertTrue(state.skipped)
            self.assertFalse(state.needs_embedding_migration)
            self.assertEqual([], calls)
            self.assertEqual([], state.errors)
            self.assertEqual(_OLD_VECTOR, _read_embedding(active))

    def test_pointerless_v6_upgrade_writer_still_reencodes_and_upserts(self):
        from scripts import upgrade

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            storage = root / ".daem0nmcp" / "storage"
            database = storage / "daem0nmcp.db"
            _create_legacy_memory_database(database, _OLD_VECTOR)
            (storage / "qdrant").mkdir()
            fingerprint = upgrade.detect_version(str(root))
            self.assertEqual(6, fingerprint.format_version)
            calls: list[str] = []

            with _installed_fake_vector_modules(
                calls,
                on_encode=lambda: _probe_exclusive_lock(storage, calls, "encode"),
                on_qdrant_init=lambda: _probe_exclusive_lock(
                    storage, calls, "qdrant-init"
                ),
                on_qdrant_upsert=lambda: _probe_exclusive_lock(
                    storage, calls, "qdrant-upsert"
                ),
            ):
                migrated, failed = upgrade._migrate_embeddings(
                    fingerprint.db_path,
                    batch_size=1,
                    format_version=fingerprint.format_version,
                )

            self.assertEqual((1, 0), (migrated, failed))
            self.assertEqual(_NEW_VECTOR, _read_embedding(database))
            self.assertIn("qdrant-upsert:1", calls)
            self.assertIn("encode:locked", calls)
            self.assertIn("qdrant-init:locked", calls)
            self.assertIn("qdrant-upsert:locked", calls)


class SchemaBackfillGuardTests(unittest.TestCase):
    def test_pointerless_canonical_v7_copy_refuses_backfill(self):
        from daem0nmcp.migrations import schema

        with tempfile.TemporaryDirectory() as raw:
            database = Path(raw) / "copied-v7.db"
            _create_legacy_memory_database(database, None)
            _add_pointerless_v7_signature(database)

            with self.assertRaisesRegex(RuntimeError, "v6-only"):
                schema.migrate_and_backfill_vectors(str(database))
            self.assertIsNone(_read_embedding(database))

    def test_v7_helper_refuses_before_schema_or_vector_mutation(self):
        from daem0nmcp.migrations import schema

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, active = _make_v7_storage(root, None)

            with self.assertRaisesRegex(RuntimeError, "v6-only"):
                schema.migrate_and_backfill_vectors(str(active))

            self.assertIsNone(_read_embedding(active))
            connection = sqlite3.connect(active)
            try:
                maximum = connection.execute(
                    "SELECT MAX(version) FROM schema_version"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(18, maximum)

    def test_v7_module_main_resolves_pointer_and_returns_failure(self):
        from daem0nmcp.config import settings
        from daem0nmcp.migrations import schema

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            storage, active = _make_v7_storage(root, None)
            output = io.StringIO()

            with (
                patch.object(
                    type(settings), "get_storage_path", return_value=str(storage)
                ),
                contextlib.redirect_stdout(output),
            ):
                result = schema.main()

            self.assertEqual(1, result)
            self.assertIn("format 7", output.getvalue().lower())
            self.assertIn("rebuild-projection", output.getvalue())
            self.assertIsNone(_read_embedding(active))

    def test_pointerless_v6_helper_still_backfills(self):
        from daem0nmcp.migrations import schema

        with tempfile.TemporaryDirectory() as raw:
            database = Path(raw) / "daem0nmcp.db"
            _create_legacy_memory_database(database, None)
            calls: list[str] = []

            storage = database.parent
            with _installed_fake_vector_modules(
                calls,
                on_encode=lambda: _probe_exclusive_lock(storage, calls, "encode"),
            ):
                result = schema.migrate_and_backfill_vectors(str(database))

            self.assertEqual(1, result["vectors_backfilled"])
            self.assertEqual(_NEW_VECTOR, _read_embedding(database))
            self.assertIn("encode:locked", calls)


class CliBackfillGuardTests(unittest.TestCase):
    def test_v7_cli_backfill_exits_before_dispatching_legacy_helper(self):
        import daem0nmcp.migrations as migrations
        from daem0nmcp import cli
        from daem0nmcp.storage_activation import resolve_active_database

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            storage, active = _make_v7_storage(root, None)
            calls: list[str] = []

            database_module = types.ModuleType("daem0nmcp.database")
            memory_module = types.ModuleType("daem0nmcp.memory")
            rules_module = types.ModuleType("daem0nmcp.rules")

            class FakeDatabaseManager:
                def __init__(self, storage_path: str) -> None:
                    resolved = resolve_active_database(storage_path)
                    self.db_path = resolved.path
                    self.format_version = resolved.format_version

            class NoOp:
                def __init__(self, *args, **kwargs) -> None:
                    pass

            database_module.DatabaseManager = FakeDatabaseManager
            memory_module.MemoryManager = NoOp
            rules_module.RulesEngine = NoOp

            def forbidden_backfill(*args, **kwargs):
                calls.append("backfill-called")
                raise AssertionError("legacy backfill was dispatched for format 7")

            output = io.StringIO()
            with (
                patch.dict(
                    sys.modules,
                    {
                        "daem0nmcp.database": database_module,
                        "daem0nmcp.memory": memory_module,
                        "daem0nmcp.rules": rules_module,
                    },
                ),
                patch.object(
                    migrations,
                    "migrate_and_backfill_vectors",
                    forbidden_backfill,
                ),
                patch.object(
                    sys,
                    "argv",
                    [
                        "daem0nmcp.cli",
                        "--project-path",
                        str(root),
                        "migrate",
                        "--backfill-vectors",
                    ],
                ),
                contextlib.redirect_stdout(output),
                self.assertRaises(SystemExit) as raised,
            ):
                cli.main()

            self.assertEqual(1, raised.exception.code)
            self.assertEqual([], calls)
            self.assertIn("format 7", output.getvalue().lower())
            self.assertIn("rebuild-projection", output.getvalue())
            self.assertIsNone(_read_embedding(active))


class LegacyVectorMigrationDocumentationTests(unittest.TestCase):
    def test_readme_marks_all_legacy_writers_v6_only_and_gives_v7_command(self):
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Deprecated (format 6 only)", readme)
        self.assertIn("migrate_embedding_model", readme)
        self.assertIn("migrate_vectors", readme)
        self.assertIn("migrate --backfill-vectors", readme)
        self.assertIn("rebuild-projection --projection dense", readme)


if __name__ == "__main__":
    unittest.main()
