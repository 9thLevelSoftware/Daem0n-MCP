"""Tests for FTS5 full-text search."""

import shutil
import tempfile

import pytest


class TestFTS5Search:
    """Test FTS5 full-text search functionality."""

    @pytest.fixture
    def temp_storage(self):
        temp_dir = tempfile.mkdtemp()
        yield temp_dir
        shutil.rmtree(temp_dir)

    @pytest.mark.asyncio
    async def test_fts_search_finds_content(self, temp_storage):
        """Verify FTS5 search works for content."""
        from daem0nmcp.database import DatabaseManager
        from daem0nmcp.memory import MemoryManager

        db = DatabaseManager(temp_storage)
        await db.init_db()
        manager = MemoryManager(db)

        # Add memories
        await manager.remember(
            category="decision",
            content="Use PostgreSQL for the database layer",
            tags=["database", "architecture"],
        )
        await manager.remember(
            category="warning",
            content="MySQL has issues with JSON columns",
            tags=["database"],
        )

        # FTS search
        results = await manager.fts_search("PostgreSQL database")
        assert len(results) >= 1
        assert any("PostgreSQL" in r["content"] for r in results)

        await db.close()

    @pytest.mark.asyncio
    async def test_fts_search_with_tag_filter(self, temp_storage):
        """Verify FTS search can filter by tags."""
        from daem0nmcp.database import DatabaseManager
        from daem0nmcp.memory import MemoryManager

        db = DatabaseManager(temp_storage)
        await db.init_db()
        manager = MemoryManager(db)

        await manager.remember(
            category="decision",
            content="Use Redis for caching",
            tags=["cache", "performance"],
        )
        await manager.remember(
            category="decision",
            content="Use Redis for session storage",
            tags=["auth", "sessions"],
        )

        # Search with tag filter
        results = await manager.fts_search("Redis", tags=["cache"])
        assert len(results) == 1
        assert results[0]["content"] == "Use Redis for caching"

        await db.close()
