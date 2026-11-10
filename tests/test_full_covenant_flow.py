# tests/test_full_covenant_flow.py
"""End-to-end test of the complete Sacred Covenant enforcement flow."""

import pytest


class TestFullCovenantFlow:
    """Test the complete covenant flow from communion to seal."""

    @pytest.fixture
    def db_manager(self, tmp_path):
        from daem0nmcp.database import DatabaseManager

        return DatabaseManager(str(tmp_path / "storage"))

    @pytest.mark.asyncio
    async def test_complete_covenant_flow(self, db_manager, covenant_workspace_factory):
        """Test: communion -> counsel -> inscribe -> seal."""
        await db_manager.init_db()

        from daem0nmcp import server

        server._project_contexts.clear()

        project_path = str(db_manager.storage_path.parent)
        workspace = covenant_workspace_factory(project_path)

        # 1. COMMUNION - get_briefing
        briefing = await workspace.brief()
        assert briefing["status"] == "ready"

        # 2. Verify recall works after briefing
        recall_result = await workspace.call_unsealed(
            server.recall, topic="test", project_path=workspace
        )
        assert recall_result.get("status") != "blocked"

        # 3. Verify remember is BLOCKED without counsel
        remember_result = await workspace.call_unsealed(
            server.remember,
            category="decision",
            content="Test decision",
            project_path=workspace,
        )
        assert remember_result.get("violation") == "COUNSEL_REQUIRED"

        # 4. SEEK COUNSEL - context_check
        decision_args = {
            "category": "decision",
            "content": "Use pytest for testing",
            "rationale": "Industry standard",
            "project_path": workspace,
        }
        target_operation, target_args = workspace.adapt(
            server.remember, **decision_args
        )
        counsel = await workspace.call_unsealed(
            server.context_check,
            description="About to make a test decision",
            project_path=workspace,
            target_operation=target_operation,
            target_args=target_args,
        )
        assert "preflight_token" in counsel

        # 5. INSCRIBE - remember (now allowed)
        decision = await workspace.call_unsealed(
            server.remember,
            **decision_args,
            preflight_token=counsel["preflight_token"],
        )
        assert "id" in decision
        decision_id = decision["id"]

        # 6. SEAL - record_outcome
        outcome = await workspace.call_unsealed(
            server.record_outcome,
            memory_id=decision_id,
            outcome="Works great, tests are fast",
            worked=True,
            project_path=workspace,
        )
        assert outcome.get("status") != "blocked"
        assert outcome.get("worked") is True

    @pytest.mark.asyncio
    async def test_enforcement_blocks_are_recoverable(
        self, db_manager, covenant_workspace_factory
    ):
        """Test that following the remedy unblocks the operation."""
        await db_manager.init_db()

        from daem0nmcp import server

        server._project_contexts.clear()

        project_path = str(db_manager.storage_path.parent)
        workspace = covenant_workspace_factory(project_path)

        # Try to recall without briefing - should be blocked
        result = await workspace.call_unsealed(
            server.recall, topic="test", project_path=workspace
        )
        assert result.get("violation") == "COMMUNION_REQUIRED"
        assert result["remedy"]["tool"] == "commune"

        # Follow the remedy
        await workspace.brief()

        # Now it should work
        result = await workspace.call_unsealed(
            server.recall, topic="test", project_path=workspace
        )
        assert result.get("status") != "blocked"

    @pytest.mark.asyncio
    async def test_parallel_preflight_tools(
        self, db_manager, covenant_workspace_factory
    ):
        """Test that preflight tools can be called in parallel after briefing."""
        await db_manager.init_db()

        import asyncio

        from daem0nmcp import server

        server._project_contexts.clear()
        project_path = str(db_manager.storage_path.parent)
        workspace = covenant_workspace_factory(project_path)

        # Briefing first
        await workspace.brief()

        # Parallel preflight (simulated)
        results = await asyncio.gather(
            workspace.call_unsealed(
                server.context_check,
                description="editing test.py",
                project_path=workspace,
            ),
            workspace.call_unsealed(
                server.recall_for_file,
                file_path="test.py",
                project_path=workspace,
            ),
            return_exceptions=True,
        )

        # Both should succeed (not be blocked)
        for result in results:
            if isinstance(result, Exception):
                pytest.fail(f"Parallel call failed: {result}")
            assert result.get("status") != "blocked"
