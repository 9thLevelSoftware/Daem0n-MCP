"""Integration tests for Sacred Covenant enforcement on MCP tools."""

from unittest.mock import patch

import pytest


class TestCovenantIntegration:
    """Test that tools are properly decorated with enforcement."""

    @pytest.fixture
    def db_manager(self, tmp_path):
        from daem0nmcp.database import DatabaseManager

        return DatabaseManager(str(tmp_path / "storage"))

    @pytest.fixture
    def memory_mgr(self, db_manager):
        from daem0nmcp.memory import MemoryManager

        return MemoryManager(db_manager)

    @pytest.mark.asyncio
    async def test_remember_blocked_without_briefing(self, db_manager, memory_mgr):
        """remember() should be blocked if get_briefing not called."""
        await db_manager.init_db()

        from daem0nmcp import server

        server._project_contexts.clear()

        result = await server.remember(
            category="decision",
            content="Test decision",
            project_path=str(db_manager.storage_path.parent.parent),
        )

        assert result.get("status") == "blocked"
        assert result.get("violation") == "COMMUNION_REQUIRED"

    @pytest.mark.asyncio
    async def test_remember_blocked_without_counsel(self, db_manager, memory_mgr):
        """remember() should be blocked if context_check not called after briefing."""
        await db_manager.init_db()

        from daem0nmcp import server

        server._project_contexts.clear()

        project_path = str(db_manager.storage_path.parent.parent)

        await server.get_briefing(project_path=project_path)

        result = await server.remember(
            category="decision",
            content="Test decision",
            project_path=project_path,
        )

        assert result.get("status") == "blocked"
        assert result.get("violation") == "COUNSEL_REQUIRED"

    @pytest.mark.asyncio
    async def test_remember_allowed_with_full_covenant(self, db_manager, memory_mgr):
        """remember() should work after briefing + context_check."""
        await db_manager.init_db()

        from daem0nmcp import server

        server._project_contexts.clear()

        project_path = str(db_manager.storage_path.parent.parent)

        await server.get_briefing(project_path=project_path)

        # Mock recall, check_rules, and _check_conflicts to avoid
        # vector dimension mismatch (384-dim stored vs 256-dim query)
        ctx = await server.get_project_context(project_path)
        with (
            patch.object(ctx.memory_manager, "recall", return_value={}),
            patch.object(ctx.rules_engine, "check_rules", return_value={}),
        ):
            await server.context_check(
                description="About to record a decision",
                project_path=project_path,
            )

        # Disable Qdrant to avoid dimension mismatch (384-dim bootstrap vs 256-dim model)
        # This test validates covenant enforcement, not vector storage
        ctx.memory_manager._qdrant = None
        with patch.object(ctx.memory_manager, "_check_conflicts", return_value=[]):
            result = await server.remember(
                category="decision",
                content="Test decision",
                project_path=project_path,
            )

        assert "id" in result
        assert result.get("status") != "blocked"

    @pytest.mark.asyncio
    async def test_recall_allowed_with_briefing_only(self, db_manager, memory_mgr):
        """recall() should work after briefing (no counsel required)."""
        await db_manager.init_db()

        from daem0nmcp import server

        server._project_contexts.clear()

        project_path = str(db_manager.storage_path.parent.parent)

        await server.get_briefing(project_path=project_path)

        # Disable Qdrant to avoid dimension mismatch (384-dim bootstrap vs 256-dim model)
        ctx = await server.get_project_context(project_path)
        ctx.memory_manager._qdrant = None

        result = await server.recall(
            topic="test",
            project_path=project_path,
        )

        assert result.get("status") != "blocked"

    @pytest.mark.asyncio
    async def test_health_always_allowed(self, db_manager):
        """health() should work without any covenant compliance."""
        await db_manager.init_db()

        from daem0nmcp import server

        server._project_contexts.clear()

        project_path = str(db_manager.storage_path.parent.parent)

        result = await server.health(project_path=project_path)

        assert "version" in result
        assert result.get("status") != "blocked"
