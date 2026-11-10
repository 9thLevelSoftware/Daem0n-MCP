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
        """Verify recall observes an authoritative external canonical write."""

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

            # Simulate another live process through the authoritative event
            # writer. A raw INSERT into the retained v6 table is intentionally
            # not authoritative in a format-7 store.
            external_db = DatabaseManager(temp_storage)
            await external_db.init_db()
            external = MemoryManager(external_db)
            try:
                await external.remember(
                    category="decision",
                    content="Use Redis for caching",
                    tags=["cache"],
                )
            finally:
                await external_db.close()

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
