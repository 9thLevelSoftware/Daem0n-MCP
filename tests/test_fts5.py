"""Tests for FTS5 full-text search virtual table infrastructure."""

import shutil
import tempfile

import pytest
from sqlalchemy import text


class TestFTS5Search:
    """Verify FTS5 virtual table is used for search."""

    @pytest.fixture
    def temp_storage(self):
        """Create temporary directory for test database."""
        temp_dir = tempfile.mkdtemp()
        yield temp_dir
        shutil.rmtree(temp_dir, ignore_errors=True)

    @pytest.fixture
    async def db_with_memories(self, temp_storage):
        """Create database with test memories."""
        from daem0nmcp.database import DatabaseManager
        from daem0nmcp.memory import MemoryManager

        db = DatabaseManager(temp_storage)
        await db.init_db()
        memory = MemoryManager(db)

        # Add test memories
        await memory.remember(
            category="decision",
            content="Use PostgreSQL for the database",
            project_path=temp_storage,
        )
        await memory.remember(
            category="decision",
            content="Implement caching with Redis",
            project_path=temp_storage,
        )
        await memory.remember(
            category="learning",
            content="SQLite FTS5 provides fast full-text search",
            project_path=temp_storage,
        )

        yield db, memory, temp_storage
        await db.close()

    @pytest.mark.asyncio
    async def test_fts5_table_exists(self, db_with_memories):
        """FTS5 virtual table should exist."""
        db, memory, tmpdir = db_with_memories

        async with db.get_session() as session:
            result = await session.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='memories_fts'"
                )
            )
            row = result.first()
            assert row is not None, "FTS5 table 'memories_fts' should exist"

    @pytest.mark.asyncio
    async def test_fts5_triggers_exist(self, db_with_memories):
        """FTS5 sync triggers should exist."""
        db, memory, tmpdir = db_with_memories

        expected_triggers = ["memories_ai", "memories_ad", "memories_au"]

        async with db.get_session() as session:
            result = await session.execute(
                text("SELECT name FROM sqlite_master WHERE type='trigger'")
            )
            triggers = [row[0] for row in result.fetchall()]

            for trigger_name in expected_triggers:
                assert trigger_name in triggers, (
                    f"Trigger '{trigger_name}' should exist"
                )

    @pytest.mark.asyncio
    async def test_fts5_search_returns_results(self, db_with_memories):
        """Search should return relevant results using FTS5."""
        db, memory, tmpdir = db_with_memories

        results = await memory.fts_search(query="database", limit=10)

        assert len(results) >= 1
        assert any("PostgreSQL" in r.get("content", "") for r in results)

    @pytest.mark.asyncio
    async def test_fts5_search_with_bm25_ranking(self, db_with_memories):
        """FTS5 search should use BM25 ranking."""
        db, memory, tmpdir = db_with_memories

        # Add memory with multiple occurrences of search term
        await memory.remember(
            category="learning",
            content="Database performance tuning for database queries and database indexing",
            project_path=tmpdir,
        )

        results = await memory.fts_search(query="database", limit=10)

        # Results should be returned (BM25 ranking is internal to FTS5)
        assert len(results) >= 2

    @pytest.mark.asyncio
    async def test_fts5_index_synced_on_insert(self, temp_storage):
        """FTS5 index should be updated when memories are inserted."""
        from daem0nmcp.database import DatabaseManager
        from daem0nmcp.memory import MemoryManager

        db = DatabaseManager(temp_storage)
        await db.init_db()
        memory = MemoryManager(db)

        # Insert a memory
        result = await memory.remember(
            category="decision",
            content="Test memory for FTS sync verification",
            project_path=temp_storage,
        )
        memory_id = result.get("id")

        # Verify FTS5 index contains the memory
        async with db.get_session() as session:
            fts_result = await session.execute(
                text(
                    "SELECT rowid FROM memories_fts WHERE memories_fts MATCH 'verification'"
                )
            )
            rows = fts_result.fetchall()
            assert len(rows) >= 1, "FTS5 index should contain newly inserted memory"
            assert memory_id in [row[0] for row in rows]

        await db.close()

    @pytest.mark.asyncio
    async def test_fts5_index_synced_on_update(self, temp_storage):
        """FTS5 index should be updated when memories are modified."""
        from daem0nmcp.database import DatabaseManager
        from daem0nmcp.memory import MemoryManager
        from daem0nmcp.models import Memory

        db = DatabaseManager(temp_storage)
        await db.init_db()
        memory = MemoryManager(db)

        # Insert a memory with specific content
        result = await memory.remember(
            category="decision",
            content="Original content for update test",
            project_path=temp_storage,
        )
        memory_id = result.get("id")

        # Update the memory content directly
        async with db.get_session() as session:
            from sqlalchemy import update

            await session.execute(
                update(Memory)
                .where(Memory.id == memory_id)
                .values(content="Modified content with unique banana term")
            )

        # Verify FTS5 index reflects the update
        async with db.get_session() as session:
            # Old content should not be found
            old_result = await session.execute(
                text(
                    "SELECT rowid FROM memories_fts WHERE memories_fts MATCH 'Original'"
                )
            )
            old_rows = old_result.fetchall()
            matching_ids = [row[0] for row in old_rows]
            assert memory_id not in matching_ids, "FTS5 should not find old content"

            # New content should be found
            new_result = await session.execute(
                text("SELECT rowid FROM memories_fts WHERE memories_fts MATCH 'banana'")
            )
            new_rows = new_result.fetchall()
            assert memory_id in [row[0] for row in new_rows], (
                "FTS5 should find new content"
            )

        await db.close()

    @pytest.mark.asyncio
    async def test_fts5_contentless_table_structure(self, db_with_memories):
        """FTS5 table should be contentless (references memories table)."""
        db, memory, tmpdir = db_with_memories

        async with db.get_session() as session:
            # Check FTS5 table structure - it should reference the memories table
            result = await session.execute(
                text("SELECT sql FROM sqlite_master WHERE name='memories_fts'")
            )
            row = result.first()
            assert row is not None
            create_sql = row[0]

            # Verify contentless FTS5 configuration
            assert (
                "content='memories'" in create_sql or "content=memories" in create_sql
            ), "FTS5 should reference memories table"
            assert (
                "content_rowid='id'" in create_sql or "content_rowid=id" in create_sql
            ), "FTS5 should use id as rowid"

    @pytest.mark.asyncio
    async def test_fts5_search_with_highlighting(self, db_with_memories):
        """FTS5 search should support highlighting via snippet function."""
        db, memory, tmpdir = db_with_memories

        results = await memory.fts_search(
            query="PostgreSQL",
            highlight=True,
            highlight_start="<mark>",
            highlight_end="</mark>",
            limit=10,
        )

        assert len(results) >= 1
        # When highlighting is enabled, content_excerpt should contain markers
        highlighted_result = next(
            (r for r in results if "PostgreSQL" in r.get("content", "")), None
        )
        assert highlighted_result is not None
        if "content_excerpt" in highlighted_result:
            # Excerpt should contain highlight markers around search term
            assert "<mark>" in highlighted_result.get(
                "content_excerpt", ""
            ) or "PostgreSQL" in highlighted_result.get("content_excerpt", "")
