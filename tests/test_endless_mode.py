# tests/test_endless_mode.py
"""Tests for Endless Mode context compression."""

import shutil
import tempfile
from unittest.mock import patch

import pytest

from daem0nmcp.database import DatabaseManager
from daem0nmcp.memory import MemoryManager


@pytest.fixture
def temp_storage():
    """Create a temporary storage directory."""
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    shutil.rmtree(temp_dir)


@pytest.fixture
async def memory_manager(temp_storage):
    """Create a memory manager with temporary storage."""
    db = DatabaseManager(temp_storage)
    await db.init_db()
    manager = MemoryManager(db)
    yield manager
    # Close Qdrant before cleanup to release file locks on Windows
    if manager._qdrant:
        manager._qdrant.close()
    await db.close()


class TestCondensedRecall:
    """Test condensed mode in recall()."""

    @pytest.mark.asyncio
    async def test_recall_accepts_condensed_parameter(self, memory_manager):
        """recall() should accept condensed parameter."""
        # Create a memory with verbose content
        await memory_manager.remember(
            category="decision",
            content="This is a very long decision content that should be truncated when condensed mode is enabled",
            rationale="This is detailed rationale explaining why we made this decision",
            context={"key": "value", "nested": {"data": "here"}},
        )

        # Should not raise - condensed parameter accepted
        result = await memory_manager.recall("decision", condensed=True)
        assert "decisions" in result

    @pytest.mark.asyncio
    async def test_condensed_strips_rationale(self, memory_manager):
        """Condensed mode should strip rationale field."""
        await memory_manager.remember(
            category="decision",
            content="Use JWT tokens",
            rationale="Need stateless auth for horizontal scaling",
        )

        result = await memory_manager.recall("JWT", condensed=True)
        assert len(result["decisions"]) > 0
        # Rationale should be None or absent in condensed mode
        decision = result["decisions"][0]
        assert decision.get("rationale") is None

    @pytest.mark.asyncio
    async def test_condensed_strips_context(self, memory_manager):
        """Condensed mode should strip context field."""
        await memory_manager.remember(
            category="decision",
            content="Use PostgreSQL",
            context={"alternatives": ["MySQL", "MongoDB"], "reason": "ACID compliance"},
        )

        result = await memory_manager.recall("PostgreSQL", condensed=True)
        decision = result["decisions"][0]
        # Context should be None or absent in condensed mode
        assert decision.get("context") is None

    @pytest.mark.asyncio
    async def test_condensed_truncates_long_content(self, memory_manager):
        """Condensed mode should truncate content over 150 chars."""
        long_content = (
            "This is a very long learning content that needs to be truncated " * 10
        )  # >150 chars
        await memory_manager.remember(category="learning", content=long_content)

        result = await memory_manager.recall(
            "long learning content truncated", condensed=True
        )
        assert len(result["learnings"]) > 0, f"No learnings found. Result: {result}"
        learning = result["learnings"][0]
        # Should be truncated to ~150 chars with ellipsis
        assert len(learning["content"]) <= 153  # 150 + "..."
        assert learning["content"].endswith("...")

    @pytest.mark.asyncio
    async def test_non_condensed_preserves_all_fields(self, memory_manager):
        """Non-condensed mode should preserve all fields."""
        await memory_manager.remember(
            category="decision",
            content="Use Redis for caching",
            rationale="Fast in-memory store",
            context={"alternatives": ["Memcached"]},
        )

        result = await memory_manager.recall("Redis", condensed=False)
        decision = result["decisions"][0]
        assert decision["rationale"] == "Fast in-memory store"
        assert decision["context"] == {"alternatives": ["Memcached"]}
        assert "semantic_match" in decision
        assert "recency_weight" in decision

    @pytest.mark.asyncio
    async def test_condensed_and_full_cached_separately(self, memory_manager):
        """Condensed and non-condensed results should be cached separately."""
        await memory_manager.remember(
            category="decision",
            content="Test caching behavior",
            rationale="Important rationale",
        )

        # First call: condensed
        result1 = await memory_manager.recall("caching", condensed=True)
        assert result1["decisions"][0].get("rationale") is None

        # Second call: non-condensed (should NOT return cached condensed result)
        result2 = await memory_manager.recall("caching", condensed=False)
        assert result2["decisions"][0]["rationale"] == "Important rationale"

        # Third call: condensed again (should return cached condensed result)
        result3 = await memory_manager.recall("caching", condensed=True)
        assert result3["decisions"][0].get("rationale") is None


class TestCondensedBriefing:
    """Test condensed mode in briefings."""

    @pytest.mark.asyncio
    async def test_prefetch_focus_areas_uses_condensed(self, memory_manager):
        """Focus area prefetch should use condensed mode by default."""
        # Create memories for focus area
        await memory_manager.remember(
            category="decision",
            content="Auth decision with long rationale",
            rationale="This is very detailed rationale that would bloat the response",
        )

        # Import server and call _prefetch_focus_areas
        from daem0nmcp.server import ProjectContext, _prefetch_focus_areas

        # Create a minimal project context
        ctx = ProjectContext(
            memory_manager=memory_manager,
            rules_engine=None,
            db_manager=None,
            project_path="/test",
            storage_path="/test/.daem0nmcp",
        )

        # Mock recall to capture the condensed parameter
        original_recall = memory_manager.recall
        called_with_condensed = []

        async def tracking_recall(*args, **kwargs):
            called_with_condensed.append(kwargs.get("condensed", False))
            return await original_recall(*args, **kwargs)

        memory_manager.recall = tracking_recall

        await _prefetch_focus_areas(ctx, ["authentication"])

        # Should have called recall with condensed=True
        assert len(called_with_condensed) > 0, "recall should have been called"
        assert called_with_condensed[0] is True, (
            "Should use condensed=True for focus areas"
        )


class TestEndlessModeMCP:
    """Test Endless Mode exposed via MCP tools."""

    @pytest.fixture
    def db_manager(self, tmp_path):
        from daem0nmcp.database import DatabaseManager

        return DatabaseManager(str(tmp_path / "storage"))

    @pytest.mark.asyncio
    async def test_recall_tool_accepts_condensed(self, db_manager):
        """MCP recall tool should accept condensed parameter."""
        await db_manager.init_db()

        from daem0nmcp import server

        server._project_contexts.clear()

        project_path = str(db_manager.storage_path.parent.parent)

        # First, call get_briefing to satisfy Sacred Covenant
        await server.get_briefing(project_path=project_path)

        # Disable Qdrant to avoid dimension mismatch (384-dim bootstrap vs 256-dim model)
        ctx = await server.get_project_context(project_path)
        ctx.memory_manager._qdrant = None

        # Use context_check to satisfy counsel requirement
        with patch.object(ctx.rules_engine, "check_rules", return_value={}):
            await server.context_check(
                description="Testing condensed parameter", project_path=project_path
            )

        await server.remember(
            category="decision",
            content="Use JWT tokens for authentication",
            rationale="Need stateless auth for horizontal scaling",
            project_path=project_path,
        )

        # Call recall with condensed=True via MCP tool
        # Search with content keywords for reliable matching
        result = await server.recall(
            topic="JWT authentication", project_path=project_path, condensed=True
        )

        # Should have results
        assert "decisions" in result, f"Expected 'decisions' key in result: {result}"
        assert len(result["decisions"]) > 0, f"Expected at least one decision: {result}"

        # Condensed output should NOT have rationale
        decision = result["decisions"][0]
        assert decision.get("rationale") is None, (
            f"Condensed mode should strip rationale, got: {decision}"
        )

    @pytest.mark.asyncio
    async def test_recall_tool_condensed_vs_full(self, db_manager):
        """Verify condensed=True strips fields, condensed=False preserves them."""
        await db_manager.init_db()

        from daem0nmcp import server

        server._project_contexts.clear()

        project_path = str(db_manager.storage_path.parent.parent)

        # First, call get_briefing to satisfy Sacred Covenant
        await server.get_briefing(project_path=project_path)

        # Disable Qdrant to avoid dimension mismatch (384-dim bootstrap vs 256-dim model)
        ctx = await server.get_project_context(project_path)
        ctx.memory_manager._qdrant = None

        # Use context_check to satisfy counsel requirement
        with patch.object(ctx.rules_engine, "check_rules", return_value={}):
            await server.context_check(
                description="Testing condensed vs full mode", project_path=project_path
            )

        await server.remember(
            category="decision",
            content="Use PostgreSQL database for persistence",
            rationale="We chose PostgreSQL because of ACID compliance",
            project_path=project_path,
        )

        # Call with condensed=False (default)
        full_result = await server.recall(
            topic="PostgreSQL database", project_path=project_path, condensed=False
        )

        # Call with condensed=True
        condensed_result = await server.recall(
            topic="PostgreSQL database", project_path=project_path, condensed=True
        )

        # Full should have rationale
        assert len(full_result["decisions"]) > 0, (
            f"Expected decisions in full result: {full_result}"
        )
        assert (
            full_result["decisions"][0]["rationale"]
            == "We chose PostgreSQL because of ACID compliance"
        )

        # Condensed should NOT have rationale
        assert len(condensed_result["decisions"]) > 0, (
            f"Expected decisions in condensed result: {condensed_result}"
        )
        assert condensed_result["decisions"][0].get("rationale") is None
