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
    async def test_remember_blocked_without_briefing(
        self, db_manager, memory_mgr, covenant_workspace_factory
    ):
        """remember() should be blocked if get_briefing not called."""
        await db_manager.init_db()

        from daem0nmcp import server

        server._project_contexts.clear()

        workspace = covenant_workspace_factory(str(db_manager.storage_path.parent))
        result = await workspace.call_unsealed(
            server.remember,
            category="decision",
            content="Test decision",
            project_path=workspace,
        )

        assert result.get("status") == "blocked"
        assert result.get("violation") == "COMMUNION_REQUIRED"

    @pytest.mark.asyncio
    async def test_remember_blocked_without_counsel(
        self, db_manager, memory_mgr, covenant_workspace_factory
    ):
        """remember() should be blocked if context_check not called after briefing."""
        await db_manager.init_db()

        from daem0nmcp import server

        server._project_contexts.clear()

        project_path = str(db_manager.storage_path.parent)
        workspace = covenant_workspace_factory(project_path)
        await workspace.brief()

        result = await workspace.call_unsealed(
            server.remember,
            category="decision",
            content="Test decision",
            project_path=workspace,
        )

        assert result.get("status") == "blocked"
        assert result.get("violation") == "COUNSEL_REQUIRED"

    @pytest.mark.asyncio
    async def test_remember_allowed_with_full_covenant(
        self, db_manager, memory_mgr, covenant_workspace_factory
    ):
        """remember() should work after briefing + context_check."""
        await db_manager.init_db()

        from daem0nmcp import server

        server._project_contexts.clear()

        project_path = str(db_manager.storage_path.parent)
        workspace = covenant_workspace_factory(project_path)
        await workspace.brief()

        # Mock recall, check_rules, and _check_conflicts to avoid
        # vector dimension mismatch (384-dim stored vs 256-dim query)
        with workspace.installed():
            ctx = await server.get_project_context(workspace)
        with (
            patch.object(ctx.memory_manager, "recall", return_value={}),
            patch.object(ctx.rules_engine, "check_rules", return_value={}),
        ):
            remember_args = {
                "category": "decision",
                "content": "Test decision",
                "project_path": workspace,
            }
            target_operation, target_args = workspace.adapt(
                server.remember, **remember_args
            )
            counsel = await workspace.call_unsealed(
                server.context_check,
                description="About to record a decision",
                project_path=workspace,
                target_operation=target_operation,
                target_args=target_args,
            )

        # Disable Qdrant to avoid dimension mismatch (384-dim bootstrap vs 256-dim model)
        # This test validates covenant enforcement, not vector storage
        ctx.memory_manager._qdrant = None
        with patch.object(ctx.memory_manager, "_check_conflicts", return_value=[]):
            result = await workspace.call_unsealed(
                server.remember,
                **remember_args,
                preflight_token=counsel["preflight_token"],
            )

        assert "id" in result
        assert result.get("status") != "blocked"

    @pytest.mark.asyncio
    async def test_recall_allowed_with_briefing_only(
        self, db_manager, memory_mgr, covenant_workspace_factory
    ):
        """recall() should work after briefing (no counsel required)."""
        await db_manager.init_db()

        from daem0nmcp import server

        server._project_contexts.clear()

        project_path = str(db_manager.storage_path.parent)
        workspace = covenant_workspace_factory(project_path)
        await workspace.brief()

        # Disable Qdrant to avoid dimension mismatch (384-dim bootstrap vs 256-dim model)
        with workspace.installed():
            ctx = await server.get_project_context(workspace)
        ctx.memory_manager._qdrant = None

        result = await workspace.call_unsealed(
            server.recall,
            topic="test",
            project_path=workspace,
        )

        assert result.get("status") != "blocked"

    @pytest.mark.asyncio
    async def test_health_always_allowed(self, db_manager, covenant_workspace_factory):
        """health() should work without any covenant compliance."""
        await db_manager.init_db()

        from daem0nmcp import server

        server._project_contexts.clear()

        project_path = str(db_manager.storage_path.parent)

        workspace = covenant_workspace_factory(project_path)
        with workspace.installed():
            result = await server.health(project_path=workspace)

        assert "version" in result
        assert result.get("status") != "blocked"
