# tests/test_resources.py
"""Tests for MCP Resources providing automatic context injection."""

import shutil
import tempfile

import pytest


class TestMCPResources:
    """Test the MCP resource endpoints."""

    @pytest.fixture
    def temp_storage(self):
        """Create a temporary storage directory."""
        temp_dir = tempfile.mkdtemp()
        yield temp_dir
        shutil.rmtree(temp_dir, ignore_errors=True)

    @pytest.fixture
    async def db_manager(self, temp_storage):
        """Create a database manager with temporary storage."""
        from daem0nmcp.database import DatabaseManager

        db = DatabaseManager(temp_storage)
        await db.init_db()
        yield db
        await db.close()

    @pytest.fixture
    async def memory_mgr(self, db_manager):
        """Create a memory manager."""
        from daem0nmcp.memory import MemoryManager

        mgr = MemoryManager(db_manager)
        yield mgr
        # Close Qdrant if initialized to release locks
        if mgr._qdrant:
            mgr._qdrant.close()

    @pytest.fixture
    async def rules_engine(self, db_manager):
        """Create a rules engine."""
        from daem0nmcp.rules import RulesEngine

        return RulesEngine(db_manager)

    @pytest.mark.asyncio
    async def test_warnings_resource_returns_active_warnings(
        self, db_manager, memory_mgr, temp_storage
    ):
        """daem0n://warnings resource should return active warnings."""
        # Create a warning
        await memory_mgr.remember(
            category="warning",
            content="Don't use eval() in user input handlers",
            rationale="Security vulnerability - arbitrary code execution",
            project_path=temp_storage,
        )

        # Test the resource function directly with the db_manager
        from daem0nmcp.server import _warnings_resource_impl

        result = await _warnings_resource_impl(temp_storage, db_manager)

        assert "eval()" in result or "Don't use" in result

    @pytest.mark.asyncio
    async def test_warnings_resource_returns_no_warnings_message(
        self, db_manager, temp_storage
    ):
        """daem0n://warnings resource should return message when no warnings."""
        from daem0nmcp.server import _warnings_resource_impl

        result = await _warnings_resource_impl(temp_storage, db_manager)

        assert "No active warnings" in result

    @pytest.mark.asyncio
    async def test_failed_resource_returns_failed_approaches(
        self, db_manager, memory_mgr, temp_storage
    ):
        """daem0n://failed resource should return failed approaches."""
        # Create a failed decision
        result = await memory_mgr.remember(
            category="decision",
            content="Try synchronous database calls",
            project_path=temp_storage,
        )
        await memory_mgr.record_outcome(
            memory_id=result["id"],
            outcome="Caused timeouts under load",
            worked=False,
        )

        from daem0nmcp.server import _failed_resource_impl

        result = await _failed_resource_impl(temp_storage, db_manager)

        assert "synchronous" in result.lower() or "timeout" in result.lower()

    @pytest.mark.asyncio
    async def test_failed_resource_returns_no_failures_message(
        self, db_manager, temp_storage
    ):
        """daem0n://failed resource should return message when no failures."""
        from daem0nmcp.server import _failed_resource_impl

        result = await _failed_resource_impl(temp_storage, db_manager)

        assert "No failed approaches" in result

    @pytest.mark.asyncio
    async def test_rules_resource_returns_top_rules(
        self, db_manager, rules_engine, temp_storage
    ):
        """daem0n://rules resource should return high-priority rules."""
        await rules_engine.add_rule(
            trigger="adding API endpoint",
            must_do=["Add rate limiting", "Add OpenAPI spec"],
            must_not=["Skip validation"],
            priority=10,
        )

        from daem0nmcp.server import _rules_resource_impl

        result = await _rules_resource_impl(temp_storage, db_manager)

        assert "rate limiting" in result.lower() or "API endpoint" in result.lower()

    @pytest.mark.asyncio
    async def test_rules_resource_returns_no_rules_message(
        self, db_manager, temp_storage
    ):
        """daem0n://rules resource should return message when no rules."""
        from daem0nmcp.server import _rules_resource_impl

        result = await _rules_resource_impl(temp_storage, db_manager)

        assert "No rules defined" in result

    @pytest.mark.asyncio
    async def test_context_resource_combines_all(
        self, db_manager, memory_mgr, rules_engine, temp_storage
    ):
        """daem0n://context resource should combine warnings, failed, and rules."""
        # Create a warning
        await memory_mgr.remember(
            category="warning",
            content="Never expose internal IDs in URLs",
            project_path=temp_storage,
        )

        # Create a failed decision
        decision = await memory_mgr.remember(
            category="decision",
            content="Using global state for config",
            project_path=temp_storage,
        )
        await memory_mgr.record_outcome(
            memory_id=decision["id"],
            outcome="Made testing impossible",
            worked=False,
        )

        # Create a rule
        await rules_engine.add_rule(
            trigger="modifying database schema",
            must_do=["Create migration file"],
            priority=5,
        )

        from daem0nmcp.server import _context_resource_impl

        result = await _context_resource_impl(temp_storage, db_manager)

        # Should contain sections from all three
        assert "Daem0n Project Context" in result
        assert "Warning" in result or "warning" in result
        assert "Failed" in result or "failed" in result
        assert "Rule" in result or "rule" in result

    @pytest.mark.asyncio
    async def test_resources_limit_output_size(
        self, db_manager, memory_mgr, temp_storage
    ):
        """Resources should limit output to reasonable size."""
        # Create many warnings
        for i in range(20):
            await memory_mgr.remember(
                category="warning",
                content=f"Warning number {i} about something important",
                project_path=temp_storage,
            )

        from daem0nmcp.server import _warnings_resource_impl

        result = await _warnings_resource_impl(temp_storage, db_manager)

        # Should have at most 10 warnings (to keep context efficient)
        warning_count = result.count("Warning number")
        assert warning_count <= 10
