"""Tests for active working context management."""

import shutil
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from daem0nmcp.models import ActiveContextItem


class TestActiveContextModel:
    """Test the ActiveContextItem model structure."""

    def test_active_context_item_has_required_fields(self):
        """ActiveContextItem should have all required fields."""
        item = ActiveContextItem(
            project_path="/test/project",
            memory_id=42,
            priority=1,
            added_at=datetime.now(timezone.utc),
            reason="Critical auth decision",
        )

        assert item.project_path == "/test/project"
        assert item.memory_id == 42
        assert item.priority == 1
        assert item.reason == "Critical auth decision"


# Fixtures for ActiveContextManager tests
@pytest.fixture
def temp_storage():
    """Create a temporary storage directory."""
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
async def active_context_manager(temp_storage):
    """Create an active context manager with temporary storage."""
    from daem0nmcp.active_context import ActiveContextManager
    from daem0nmcp.database import DatabaseManager

    db = DatabaseManager(temp_storage)
    await db.init_db()
    manager = ActiveContextManager(db)
    yield manager
    await db.close()


class TestActiveContextManager:
    """Test the ActiveContextManager class."""

    @pytest.mark.asyncio
    async def test_add_to_active_context(self, active_context_manager):
        """Test adding a memory to active context."""
        from daem0nmcp.memory import MemoryManager

        mem_manager = MemoryManager(active_context_manager.db)
        mem = await mem_manager.remember(
            category="decision", content="Critical auth decision"
        )

        result = await active_context_manager.add_to_context(
            project_path="/test/project",
            memory_id=mem["id"],
            reason="Must inform all auth work",
        )

        assert result["status"] == "added"
        assert result["memory_id"] == mem["id"]

    @pytest.mark.asyncio
    async def test_add_nonexistent_memory_fails(self, active_context_manager):
        """Test that adding a nonexistent memory returns an error."""
        result = await active_context_manager.add_to_context(
            project_path="/test/project", memory_id=99999, reason="This should fail"
        )

        assert "error" in result
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_add_duplicate_memory_returns_already_exists(
        self, active_context_manager
    ):
        """Test that adding the same memory twice returns already_exists."""
        from daem0nmcp.memory import MemoryManager

        mem_manager = MemoryManager(active_context_manager.db)
        mem = await mem_manager.remember(category="warning", content="Critical warning")

        # Add first time
        await active_context_manager.add_to_context(
            project_path="/test/project", memory_id=mem["id"]
        )

        # Add second time
        result = await active_context_manager.add_to_context(
            project_path="/test/project", memory_id=mem["id"]
        )

        assert result["status"] == "already_exists"

    @pytest.mark.asyncio
    async def test_remove_from_context(self, active_context_manager):
        """Test removing a memory from active context."""
        from daem0nmcp.memory import MemoryManager

        mem_manager = MemoryManager(active_context_manager.db)
        mem = await mem_manager.remember(
            category="pattern", content="Important pattern"
        )

        await active_context_manager.add_to_context(
            project_path="/test/project", memory_id=mem["id"]
        )

        result = await active_context_manager.remove_from_context(
            project_path="/test/project", memory_id=mem["id"]
        )

        assert result["status"] == "removed"

    @pytest.mark.asyncio
    async def test_remove_nonexistent_returns_not_found(self, active_context_manager):
        """Test removing a nonexistent item returns not_found."""
        result = await active_context_manager.remove_from_context(
            project_path="/test/project", memory_id=99999
        )

        assert result["status"] == "not_found"

    @pytest.mark.asyncio
    async def test_get_active_context(self, active_context_manager):
        """Test getting all items in active context."""
        from daem0nmcp.memory import MemoryManager

        mem_manager = MemoryManager(active_context_manager.db)

        # Add multiple memories
        mem1 = await mem_manager.remember(category="decision", content="Decision 1")
        mem2 = await mem_manager.remember(category="warning", content="Warning 1")

        await active_context_manager.add_to_context(
            project_path="/test/project", memory_id=mem1["id"], priority=1
        )
        await active_context_manager.add_to_context(
            project_path="/test/project", memory_id=mem2["id"], priority=2
        )

        result = await active_context_manager.get_active_context("/test/project")

        assert result["count"] == 2
        # Higher priority should come first
        assert result["items"][0]["memory_id"] == mem2["id"]
        assert result["items"][1]["memory_id"] == mem1["id"]

    @pytest.mark.asyncio
    async def test_clear_context(self, active_context_manager):
        """Test clearing all items from active context."""
        from daem0nmcp.memory import MemoryManager

        mem_manager = MemoryManager(active_context_manager.db)
        mem = await mem_manager.remember(category="decision", content="Test")

        await active_context_manager.add_to_context(
            project_path="/test/project", memory_id=mem["id"]
        )

        result = await active_context_manager.clear_context("/test/project")

        assert result["status"] == "cleared"
        assert result["removed_count"] == 1

        # Verify context is empty
        context = await active_context_manager.get_active_context("/test/project")
        assert context["count"] == 0

    @pytest.mark.asyncio
    async def test_context_limit(self, active_context_manager):
        """Test that active context respects the 10-item limit."""
        from daem0nmcp.memory import MemoryManager

        mem_manager = MemoryManager(active_context_manager.db)

        # Add 10 memories (at limit)
        for i in range(10):
            mem = await mem_manager.remember(
                category="decision", content=f"Decision {i}"
            )
            result = await active_context_manager.add_to_context(
                project_path="/test/project", memory_id=mem["id"]
            )
            assert result["status"] == "added"

        # 11th should fail
        mem11 = await mem_manager.remember(category="decision", content="Decision 11")
        result = await active_context_manager.add_to_context(
            project_path="/test/project", memory_id=mem11["id"]
        )

        assert result["error"] == "CONTEXT_FULL"

    @pytest.mark.asyncio
    async def test_cleanup_expired(self, active_context_manager):
        """Test cleanup of expired context items."""
        from daem0nmcp.memory import MemoryManager

        mem_manager = MemoryManager(active_context_manager.db)
        mem = await mem_manager.remember(category="decision", content="Temp decision")

        # Add with past expiry
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        await active_context_manager.add_to_context(
            project_path="/test/project", memory_id=mem["id"], expires_at=past
        )

        result = await active_context_manager.cleanup_expired("/test/project")

        assert result["status"] == "cleaned"
        assert result["expired_count"] == 1


class TestActiveContextMCPTools:
    """Test the MCP tools for active context management."""

    @pytest.mark.asyncio
    async def test_mcp_set_active_context(self, covenant_compliant_project):
        """Test the MCP tool for setting active context."""
        from daem0nmcp import server

        # Create a memory first
        mem = await server.remember(
            category="warning",
            content="Never use eval() for user input",
            project_path=covenant_compliant_project,
        )

        # Add to active context
        result = await server.set_active_context(
            memory_id=mem["id"],
            reason="Critical security warning",
            project_path=covenant_compliant_project,
        )

        assert result["status"] == "added"

        # Get active context
        context = await server.get_active_context(
            project_path=covenant_compliant_project
        )

        assert context["count"] == 1
        assert context["items"][0]["memory_id"] == mem["id"]

    @pytest.mark.asyncio
    async def test_mcp_remove_from_active_context(self, covenant_compliant_project):
        """Test removing a memory from active context via MCP tool."""
        from daem0nmcp import server

        # Create and add a memory
        mem = await server.remember(
            category="decision",
            content="Use PostgreSQL for main database",
            project_path=covenant_compliant_project,
        )

        await server.set_active_context(
            memory_id=mem["id"], project_path=covenant_compliant_project
        )

        # Remove from context
        result = await server.remove_from_active_context(
            memory_id=mem["id"], project_path=covenant_compliant_project
        )

        assert result["status"] == "removed"

        # Verify it's gone
        context = await server.get_active_context(
            project_path=covenant_compliant_project
        )
        assert context["count"] == 0

    @pytest.mark.asyncio
    async def test_mcp_clear_active_context(self, covenant_compliant_project):
        """Test clearing all active context via MCP tool."""
        from daem0nmcp import server

        # Create and add multiple memories
        mem1 = await server.remember(
            category="pattern",
            content="Pattern 1",
            project_path=covenant_compliant_project,
        )
        mem2 = await server.remember(
            category="pattern",
            content="Pattern 2",
            project_path=covenant_compliant_project,
        )

        await server.set_active_context(
            memory_id=mem1["id"], project_path=covenant_compliant_project
        )
        await server.set_active_context(
            memory_id=mem2["id"], project_path=covenant_compliant_project
        )

        # Clear all
        result = await server.clear_active_context(
            project_path=covenant_compliant_project
        )

        assert result["status"] == "cleared"
        assert result["removed_count"] == 2

    @pytest.mark.asyncio
    async def test_mcp_set_active_context_with_expiry(self, covenant_compliant_project):
        """Test setting active context with expiration."""
        from daem0nmcp import server

        mem = await server.remember(
            category="learning",
            content="Temporary focus area",
            project_path=covenant_compliant_project,
        )

        result = await server.set_active_context(
            memory_id=mem["id"],
            reason="Temporary focus",
            expires_in_hours=24,
            project_path=covenant_compliant_project,
        )

        assert result["status"] == "added"

    @pytest.mark.asyncio
    async def test_mcp_set_active_context_missing_project_path(self):
        """Test that set_active_context requires project_path."""
        from daem0nmcp import server

        result = await server.set_active_context(memory_id=1, project_path=None)

        assert "error" in result
        assert result["error"] == "MISSING_PROJECT_PATH"


class TestBriefingIncludesActiveContext:
    """Test that get_briefing includes active context."""

    @pytest.mark.asyncio
    async def test_briefing_includes_active_context(self, covenant_compliant_project):
        """get_briefing should include active context items."""
        from daem0nmcp import server

        # Create and activate a memory
        mem = await server.remember(
            category="warning",
            content="Database migration in progress",
            project_path=covenant_compliant_project,
        )
        await server.set_active_context(
            memory_id=mem["id"],
            reason="Ongoing migration",
            project_path=covenant_compliant_project,
        )

        # Get briefing
        briefing = await server.get_briefing(project_path=covenant_compliant_project)

        assert "active_context" in briefing
        assert briefing["active_context"]["count"] == 1
        assert briefing["active_context"]["items"][0]["memory_id"] == mem["id"]


class TestAutoActivationOnFailure:
    """Test auto-activation of failed decisions in active context."""

    @pytest.mark.asyncio
    async def test_failed_decision_auto_activates(self, temp_storage):
        """Failed decisions should auto-activate in context."""
        from daem0nmcp.active_context import ActiveContextManager
        from daem0nmcp.database import DatabaseManager
        from daem0nmcp.memory import MemoryManager

        db = DatabaseManager(temp_storage)
        await db.init_db()

        try:
            mem_manager = MemoryManager(db)

            # Create a decision
            mem = await mem_manager.remember(
                category="decision",
                content="Use synchronous DB calls",
                project_path=temp_storage,
            )

            # Record it as failed
            await mem_manager.record_outcome(
                memory_id=mem["id"],
                outcome="Caused timeout issues",
                worked=False,
                project_path=temp_storage,
            )

            # Check active context
            acm = ActiveContextManager(db)
            context = await acm.get_active_context(temp_storage)

            # Failed decision should be auto-added
            assert context["count"] == 1
            assert context["items"][0]["memory_id"] == mem["id"]
            assert "failed" in context["items"][0]["reason"].lower()
        finally:
            await db.close()
