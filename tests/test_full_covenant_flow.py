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
    async def test_complete_covenant_flow(self, db_manager):
        """Test: communion -> counsel -> inscribe -> seal."""
        await db_manager.init_db()

        from daem0nmcp import server

        server._project_contexts.clear()

        project_path = str(db_manager.storage_path.parent.parent)

        # 1. COMMUNION - get_briefing
        briefing = await server.get_briefing(project_path=project_path)
        assert briefing["status"] == "ready"

        # 2. Verify recall works after briefing
        recall_result = await server.recall(topic="test", project_path=project_path)
        assert recall_result.get("status") != "blocked"

        # 3. Verify remember is BLOCKED without counsel
        remember_result = await server.remember(
            category="decision",
            content="Test decision",
            project_path=project_path,
        )
        assert remember_result.get("violation") == "COUNSEL_REQUIRED"

        # 4. SEEK COUNSEL - context_check
        counsel = await server.context_check(
            description="About to make a test decision",
            project_path=project_path,
        )
        assert "preflight_token" in counsel

        # 5. INSCRIBE - remember (now allowed)
        decision = await server.remember(
            category="decision",
            content="Use pytest for testing",
            rationale="Industry standard",
            project_path=project_path,
        )
        assert "id" in decision
        decision_id = decision["id"]

        # 6. SEAL - record_outcome
        outcome = await server.record_outcome(
            memory_id=decision_id,
            outcome="Works great, tests are fast",
            worked=True,
            project_path=project_path,
        )
        assert outcome.get("status") != "blocked"
        assert outcome.get("worked") is True

    @pytest.mark.asyncio
    async def test_enforcement_blocks_are_recoverable(self, db_manager):
        """Test that following the remedy unblocks the operation."""
        await db_manager.init_db()

        from daem0nmcp import server

        server._project_contexts.clear()

        project_path = str(db_manager.storage_path.parent.parent)

        # Try to recall without briefing - should be blocked
        result = await server.recall(topic="test", project_path=project_path)
        assert result.get("violation") == "COMMUNION_REQUIRED"
        assert result["remedy"]["tool"] == "get_briefing"

        # Follow the remedy
        await server.get_briefing(project_path=project_path)

        # Now it should work
        result = await server.recall(topic="test", project_path=project_path)
        assert result.get("status") != "blocked"

    @pytest.mark.asyncio
    async def test_parallel_preflight_tools(self, db_manager):
        """Test that preflight tools can be called in parallel after briefing."""
        await db_manager.init_db()

        import asyncio

        from daem0nmcp import server

        server._project_contexts.clear()
        project_path = str(db_manager.storage_path.parent.parent)

        # Briefing first
        await server.get_briefing(project_path=project_path)

        # Parallel preflight (simulated)
        results = await asyncio.gather(
            server.context_check(
                description="editing test.py", project_path=project_path
            ),
            server.recall_for_file(file_path="test.py", project_path=project_path),
            return_exceptions=True,
        )

        # Both should succeed (not be blocked)
        for result in results:
            if isinstance(result, Exception):
                pytest.fail(f"Parallel call failed: {result}")
            assert result.get("status") != "blocked"
