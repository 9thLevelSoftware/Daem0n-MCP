"""Tests for index freshness tracking."""

import shutil
import tempfile
import time

import pytest


class TestIndexFreshness:
    """Test that indexes are rebuilt when DB changes."""

    @pytest.fixture
    def temp_storage(self):
        temp_dir = tempfile.mkdtemp()
        yield temp_dir
        shutil.rmtree(temp_dir)

    @pytest.mark.asyncio
    async def test_memory_index_rebuilds_after_external_change(self, temp_storage):
        """Verify TF-IDF index rebuilds when DB is modified externally."""
        import sqlite3
        import time

        from daem0nmcp.database import DatabaseManager
        from daem0nmcp.memory import MemoryManager

        db = DatabaseManager(temp_storage)
        await db.init_db()
        manager = MemoryManager(db)

        try:
            # Add a memory and trigger index build
            await manager.remember(
                category="decision",
                content="Use PostgreSQL for database",
                tags=["database"],
            )
            result1 = await manager.recall("PostgreSQL")
            assert result1["found"] >= 1

            # Simulate external modification (another process added a memory)
            # Need to wait long enough to ensure timestamp is different (SQLite datetime has second precision)
            time.sleep(1.1)  # Sleep more than 1 second to ensure different timestamp

            conn = sqlite3.connect(str(db.db_path))
            conn.execute("""
                INSERT INTO memories (category, content, keywords, tags, context, created_at, updated_at)
                VALUES ('decision', 'Use Redis for caching', 'redis caching', '["cache"]', '{}',
                        datetime('now'), datetime('now'))
            """)
            conn.commit()
            conn.close()

            # Force freshness check - should detect change and rebuild
            rebuilt = await manager._check_index_freshness()
            assert rebuilt is True, (
                "Index should have been rebuilt after external change"
            )

            # Now search should find the new memory
            result2 = await manager.recall("Redis caching")
            assert result2["found"] >= 1, f"Should find Redis memory, got: {result2}"

        finally:
            # Close Qdrant client (holds its own SQLite handle)
            if manager._qdrant:
                manager._qdrant.client.close()
            # Ensure database is properly closed
            await db.close()
            # Give Windows time to release file handles
            time.sleep(0.1)

    @pytest.mark.asyncio
    async def test_rebuild_index_tool(self, temp_storage):
        """Test the rebuild_index MCP tool."""
        from daem0nmcp.database import DatabaseManager
        from daem0nmcp.memory import MemoryManager
        from daem0nmcp.rules import RulesEngine

        db = DatabaseManager(temp_storage)
        await db.init_db()
        memory = MemoryManager(db)
        rules = RulesEngine(db)

        try:
            # Add some data
            await memory.remember(category="decision", content="Test memory")
            await rules.add_rule(trigger="test trigger", must_do=["test action"])

            # Force index build
            await memory.recall("test")
            await rules.check_rules("test")

            # Rebuild should work
            result = await memory.rebuild_index()
            assert result["memories_indexed"] >= 1

            result = await rules.rebuild_index()
            assert result["rules_indexed"] >= 1

        finally:
            # Close Qdrant client (holds its own SQLite handle)
            if memory._qdrant:
                memory._qdrant.client.close()
            await db.close()
            # Give Windows time to release file handles
            time.sleep(0.1)
