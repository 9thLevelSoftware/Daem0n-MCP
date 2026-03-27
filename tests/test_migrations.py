"""Tests for database migrations."""

import sqlite3
import tempfile
from pathlib import Path

import pytest


class TestMigrations:
    """Test migration functionality."""

    @pytest.fixture
    def legacy_db(self):
        """Create a legacy database without new columns."""
        temp_dir = tempfile.mkdtemp()
        db_path = Path(temp_dir) / "daem0nmcp.db"

        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE memories (
                id INTEGER PRIMARY KEY,
                category TEXT NOT NULL,
                content TEXT NOT NULL,
                rationale TEXT,
                context TEXT DEFAULT '{}',
                tags TEXT DEFAULT '[]',
                file_path TEXT,
                keywords TEXT,
                is_permanent BOOLEAN DEFAULT 0,
                outcome TEXT,
                worked BOOLEAN,
                created_at TIMESTAMP,
                updated_at TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE rules (
                id INTEGER PRIMARY KEY,
                trigger TEXT NOT NULL,
                trigger_keywords TEXT,
                must_do TEXT DEFAULT '[]',
                must_not TEXT DEFAULT '[]',
                ask_first TEXT DEFAULT '[]',
                warnings TEXT DEFAULT '[]',
                priority INTEGER DEFAULT 0,
                enabled BOOLEAN DEFAULT 1,
                created_at TIMESTAMP
            )
        """)
        # memory_versions table needed for migration 14+
        conn.execute("""
            CREATE TABLE memory_versions (
                id INTEGER PRIMARY KEY,
                memory_id INTEGER,
                version_number INTEGER,
                content TEXT,
                rationale TEXT,
                context TEXT,
                tags TEXT,
                outcome TEXT,
                worked BOOLEAN,
                change_type TEXT,
                change_description TEXT,
                changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()

        yield str(db_path)

        import shutil

        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_migration_adds_vector_embedding(self, legacy_db):
        """Verify migration adds vector_embedding column."""
        from daem0nmcp.migrations import run_migrations

        count, applied = run_migrations(legacy_db)

        # Check column exists
        with sqlite3.connect(legacy_db) as conn:
            cursor = conn.execute("PRAGMA table_info(memories)")
            columns = [row[1] for row in cursor.fetchall()]

        assert "vector_embedding" in columns
        assert count >= 1

    def test_migration_is_idempotent(self, legacy_db):
        """Verify running migrations twice is safe."""
        from daem0nmcp.migrations import run_migrations

        count1, _ = run_migrations(legacy_db)
        count2, _ = run_migrations(legacy_db)

        # Second run should do nothing
        assert count2 == 0

    def test_migration_creates_fts_table(self, legacy_db):
        """Verify FTS5 table is created."""
        from daem0nmcp.migrations import run_migrations

        run_migrations(legacy_db)

        with sqlite3.connect(legacy_db) as conn:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='memories_fts'"
            )
            result = cursor.fetchone()

            # FTS table should exist
            assert result is not None

            # Also check triggers
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='memories'"
            )
            triggers = [row[0] for row in cursor.fetchall()]
            assert "memories_ai" in triggers  # After insert
            assert "memories_au" in triggers  # After update
            assert "memories_ad" in triggers  # After delete

    def test_migration_adds_pinned_archived_columns(self, legacy_db):
        """Verify migration adds pinned and archived columns."""
        from daem0nmcp.migrations import run_migrations

        run_migrations(legacy_db)

        with sqlite3.connect(legacy_db) as conn:
            cursor = conn.execute("PRAGMA table_info(memories)")
            columns = [row[1] for row in cursor.fetchall()]

        assert "pinned" in columns
        assert "archived" in columns

    def test_migration_tracks_schema_version(self, legacy_db):
        """Verify migrations are tracked in schema_version table."""
        from daem0nmcp.migrations import run_migrations

        count, applied = run_migrations(legacy_db)

        with sqlite3.connect(legacy_db) as conn:
            cursor = conn.execute("SELECT version FROM schema_version ORDER BY version")
            versions = [row[0] for row in cursor.fetchall()]

        assert len(versions) == count
        assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]


def test_migration_14_bitemporal_columns():
    """Test that migration 14 adds bi-temporal columns correctly."""
    from daem0nmcp.migrations.schema import run_migrations

    with tempfile.TemporaryDirectory() as td:
        db_path = f"{td}/test.db"

        # Create minimal schema matching what earlier migrations expect
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE memories (
                id INTEGER PRIMARY KEY,
                category TEXT,
                content TEXT,
                rationale TEXT,
                context TEXT,
                tags TEXT,
                file_path TEXT,
                keywords TEXT,
                is_permanent BOOLEAN,
                outcome TEXT,
                worked BOOLEAN,
                created_at TIMESTAMP,
                updated_at TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE rules (
                id INTEGER PRIMARY KEY,
                trigger TEXT,
                trigger_keywords TEXT,
                must_do TEXT,
                must_not TEXT,
                ask_first TEXT,
                warnings TEXT,
                priority INTEGER,
                enabled BOOLEAN,
                created_at TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE memory_versions (
                id INTEGER PRIMARY KEY,
                memory_id INTEGER,
                version_number INTEGER,
                content TEXT,
                rationale TEXT,
                context TEXT,
                tags TEXT,
                outcome TEXT,
                worked BOOLEAN,
                change_type TEXT,
                change_description TEXT,
                changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Insert a test record to verify backfill
        conn.execute("""
            INSERT INTO memory_versions (memory_id, version_number, content, change_type, changed_at)
            VALUES (1, 1, 'test', 'created', '2026-01-01 12:00:00')
        """)
        conn.commit()
        conn.close()

        # Run migrations
        count, applied = run_migrations(db_path)

        # Verify columns exist
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(memory_versions)")
        columns = {row[1] for row in cursor.fetchall()}

        assert "valid_from" in columns, "valid_from column missing"
        assert "valid_to" in columns, "valid_to column missing"
        assert "invalidated_by_version_id" in columns, (
            "invalidated_by_version_id column missing"
        )

        # Verify backfill worked (valid_from should equal changed_at)
        cursor.execute(
            "SELECT valid_from, changed_at FROM memory_versions WHERE id = 1"
        )
        row = cursor.fetchone()
        assert row[0] is not None, "valid_from should be backfilled"
        assert row[0] == row[1], "valid_from should equal changed_at after backfill"

        # Verify indexes exist
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE '%temporal%'"
        )
        indexes = {row[0] for row in cursor.fetchall()}
        assert "idx_memory_versions_temporal" in indexes, "Temporal index missing"

        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE '%transaction%'"
        )
        indexes = {row[0] for row in cursor.fetchall()}
        assert "idx_memory_versions_transaction" in indexes, (
            "Transaction time index missing"
        )

        conn.close()
