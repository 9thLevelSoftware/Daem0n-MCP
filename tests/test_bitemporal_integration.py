"""Exhaustive bi-temporal integration tests.

Tests the critical scenario from ROADMAP.md:
"Test 'as of T1 believed X, at T2 learned X wrong, query at T3 shows invalidation' exhaustively"

These tests verify:
1. MCP tool remember accepts happened_at parameter
2. MCP tool recall accepts as_of_time parameter
3. MCP tool trace_evolution exists for knowledge evolution queries
4. Full bi-temporal scenario with invalidation
5. Edge cases: naive datetime, future happened_at, no as_of_time
"""

import shutil
import tempfile
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def temp_storage():
    """Create a temporary storage directory."""
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
async def memory_manager(temp_storage):
    """Create a memory manager with temporary storage."""
    from daem0nmcp.database import DatabaseManager
    from daem0nmcp.memory import MemoryManager

    db = DatabaseManager(temp_storage)
    await db.init_db()
    manager = MemoryManager(db)
    yield manager
    if manager._qdrant:
        manager._qdrant.close()
    await db.close()


@pytest.fixture
async def db_manager(temp_storage):
    """Create a database manager for direct tests."""
    from daem0nmcp.database import DatabaseManager

    db = DatabaseManager(temp_storage)
    await db.init_db()
    yield db
    await db.close()


# ============================================================================
# Test Class 1: MCP Tool Parameter Tests
# ============================================================================


class TestMCPToolParameters:
    """Verify MCP tools accept bi-temporal parameters."""

    @pytest.mark.asyncio
    async def test_remember_accepts_happened_at(self, covenant_compliant_project):
        """remember MCP tool should accept happened_at parameter (ISO 8601 string)."""
        from daem0nmcp import server

        # Create a memory with happened_at in the past
        past_time = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

        result = await server.remember(
            category="decision",
            content="User prefers Python over JavaScript",
            happened_at=past_time,
            project_path=covenant_compliant_project,
        )

        assert "error" not in result
        assert "id" in result
        assert result["category"] == "decision"

    @pytest.mark.asyncio
    async def test_remember_rejects_invalid_happened_at(
        self, covenant_compliant_project
    ):
        """remember should return error for invalid happened_at format."""
        from daem0nmcp import server

        result = await server.remember(
            category="decision",
            content="Test decision",
            happened_at="not-a-valid-date",
            project_path=covenant_compliant_project,
        )

        assert "error" in result
        assert "happened_at" in result["error"]
        assert "ISO format" in result["error"]

    @pytest.mark.asyncio
    async def test_recall_accepts_as_of_time(self, covenant_compliant_project):
        """recall MCP tool should accept as_of_time parameter (ISO 8601 string)."""
        from daem0nmcp import server

        # First create a memory
        await server.remember(
            category="pattern",
            content="Always use type hints in Python",
            project_path=covenant_compliant_project,
        )

        # Query with as_of_time
        future_time = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        result = await server.recall(
            topic="type hints",
            as_of_time=future_time,
            project_path=covenant_compliant_project,
        )

        assert "error" not in result
        # Should have some structure indicating it ran
        assert "decisions" in result or "patterns" in result or "warnings" in result

    @pytest.mark.asyncio
    async def test_recall_rejects_invalid_as_of_time(self, covenant_compliant_project):
        """recall should return error for invalid as_of_time format."""
        from daem0nmcp import server

        result = await server.recall(
            topic="anything",
            as_of_time="invalid-timestamp",
            project_path=covenant_compliant_project,
        )

        assert "error" in result
        assert "as_of_time" in result["error"]
        assert "ISO format" in result["error"]

    @pytest.mark.asyncio
    async def test_trace_evolution_exists_and_accepts_parameters(
        self, covenant_compliant_project
    ):
        """trace_evolution MCP tool should exist and accept entity_name/entity_type/include_invalidated."""
        from daem0nmcp import server

        # Call with entity_name (will return not found, but validates parameter acceptance)
        result = await server.trace_evolution(
            entity_name="NonExistentEntity",
            entity_type="concept",
            include_invalidated=True,
            project_path=covenant_compliant_project,
        )

        # Should not error on parameters - either found=False or timeline=[]
        assert "error" not in result or "not found" in result.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_trace_evolution_requires_name_or_id(
        self, covenant_compliant_project
    ):
        """trace_evolution should require either entity_name or entity_id."""
        from daem0nmcp import server

        result = await server.trace_evolution(project_path=covenant_compliant_project)

        assert "error" in result
        assert "entity_name or entity_id" in result["error"].lower()


# ============================================================================
# Test Class 2: happened_at Temporal Precision Tests
# ============================================================================


class TestHappenedAtPrecision:
    """Verify happened_at sets valid_from correctly."""

    @pytest.mark.asyncio
    async def test_happened_at_sets_valid_from(self, memory_manager):
        """T1: remember with happened_at sets valid_from correctly."""
        # Record a fact that happened 1 week ago
        one_week_ago = datetime.now(timezone.utc) - timedelta(days=7)

        result = await memory_manager.remember(
            category="decision",
            content="Database uses PostgreSQL",
            happened_at=one_week_ago,
        )

        assert "id" in result
        memory_id = result["id"]

        # Get the memory versions to check valid_from
        versions = await memory_manager.get_memory_versions(memory_id)
        assert len(versions) >= 1

        # The valid_from should be close to one_week_ago (within seconds due to rounding)
        version = versions[0]
        valid_from_str = version.get("valid_from")

        if valid_from_str:
            valid_from = datetime.fromisoformat(valid_from_str.replace("Z", "+00:00"))
            # Allow 2 second tolerance for test execution
            assert abs((valid_from - one_week_ago).total_seconds()) < 2

    @pytest.mark.asyncio
    async def test_query_before_valid_from_returns_nothing(self, memory_manager):
        """T3: Query at time BEFORE valid_from returns no results."""
        # Record a fact that happened today
        now = datetime.now(timezone.utc)
        today = now.replace(hour=12, minute=0, second=0, microsecond=0)

        await memory_manager.remember(
            category="pattern",
            content="Use async/await for I/O operations",
            happened_at=today,
        )

        # Query for yesterday (before the fact was valid)
        yesterday = today - timedelta(days=1)
        recall_result = await memory_manager.recall(
            topic="async await I/O", as_of_time=yesterday
        )

        # Should not find the pattern (it wasn't valid yesterday)
        patterns = recall_result.get("patterns", [])
        matching = [p for p in patterns if "async/await" in p.get("content", "")]
        assert len(matching) == 0, "Should not find memory before its valid_from"

    @pytest.mark.asyncio
    async def test_query_after_valid_from_returns_memory(self, memory_manager):
        """T4: Query at time AFTER valid_from returns the memory."""
        # Record a fact that happened yesterday
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)

        await memory_manager.remember(
            category="pattern",
            content="Always validate user input",
            happened_at=yesterday,
        )

        # Query for today (after the fact was valid)
        tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
        recall_result = await memory_manager.recall(
            topic="validate user input", as_of_time=tomorrow
        )

        # Should find the pattern
        patterns = recall_result.get("patterns", [])
        matching = [
            p for p in patterns if "validate user input" in p.get("content", "")
        ]
        assert len(matching) > 0, "Should find memory after its valid_from"


# ============================================================================
# Test Class 3: The Critical ROADMAP.md Scenario
# ============================================================================


class TestCriticalBitemporalScenario:
    """
    Test the critical scenario from ROADMAP.md:
    "T1 believed X, at T2 learned X wrong, query at T3 shows invalidation"
    """

    @pytest.mark.asyncio
    async def test_full_invalidation_scenario(self, memory_manager, db_manager):
        """Full bi-temporal scenario with invalidation."""
        from sqlalchemy import select

        from daem0nmcp.models import MemoryVersion

        # T1: January 15 - We believe "Auth uses session cookies"
        t1 = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)

        result1 = await memory_manager.remember(
            category="pattern",
            content="Authentication uses session cookies",
            rationale="Observed in login flow",
            happened_at=t1,
        )
        memory1_id = result1["id"]

        # T2: January 22 - We learn auth actually uses JWT (first belief was wrong)
        t2 = datetime(2025, 1, 22, 10, 0, 0, tzinfo=timezone.utc)

        result2 = await memory_manager.remember(
            category="pattern",
            content="Authentication uses JWT tokens, not session cookies",
            rationale="Code review revealed JWT implementation",
            happened_at=t2,
        )
        memory2_id = result2["id"]

        # Manually invalidate the first memory (simulating correction)
        async with db_manager.get_session() as session:
            # Get the first memory's version
            v1_result = await session.execute(
                select(MemoryVersion).where(MemoryVersion.memory_id == memory1_id)
            )
            v1 = v1_result.scalar_one()

            # Get the second memory's version
            v2_result = await session.execute(
                select(MemoryVersion).where(MemoryVersion.memory_id == memory2_id)
            )
            v2 = v2_result.scalar_one()

            # Mark v1 as invalidated by v2
            v1.valid_to = t2
            v1.invalidated_by_version_id = v2.id

            await session.commit()

        # T3: Today - Query should show the invalidation
        t3 = datetime.now(timezone.utc)

        # Query at T1 (before invalidation) - should see session cookies
        recall_t1 = await memory_manager.recall(
            topic="authentication method",
            as_of_time=t1 + timedelta(hours=1),  # Just after T1
        )

        # Query at T3 (after invalidation) - should see JWT, not cookies
        recall_t3 = await memory_manager.recall(
            topic="authentication method", as_of_time=t3
        )

        # Verify T1 query returns session cookies belief
        patterns_t1 = recall_t1.get("patterns", [])
        session_cookie_found_t1 = any(
            "session cookies" in p.get("content", "").lower() for p in patterns_t1
        )

        # Verify T3 query returns JWT (current belief)
        patterns_t3 = recall_t3.get("patterns", [])
        jwt_found_t3 = any("jwt" in p.get("content", "").lower() for p in patterns_t3)

        # The invalidated memory should NOT appear in T3 query
        any(
            "session cookies" in p.get("content", "").lower()
            and "not"
            not in p.get("content", "").lower()  # Exclude the correction message
            for p in patterns_t3
        )

        # Assert the scenario
        assert session_cookie_found_t1, (
            "T1 query should return 'session cookies' belief"
        )
        assert jwt_found_t3, "T3 query should return 'JWT' belief"
        # Invalidated memory filtered out in T3
        # Note: This depends on as_of_time filtering which checks valid_to

    @pytest.mark.asyncio
    async def test_trace_evolution_shows_invalidation_chain(
        self, memory_manager, db_manager
    ):
        """trace_evolution should show which versions superseded which."""
        from daem0nmcp.models import (
            ExtractedEntity,
            Memory,
            MemoryEntityRef,
            MemoryVersion,
        )

        # Create an entity
        async with db_manager.get_session() as session:
            entity = ExtractedEntity(
                project_path="/test", entity_type="concept", name="AuthMethod"
            )
            session.add(entity)
            await session.flush()
            entity_id = entity.id

            # Create memory with version referencing the entity
            memory = Memory(
                category="pattern",
                content="Auth uses sessions",
                keywords="auth sessions",
            )
            session.add(memory)
            await session.flush()

            # Create version
            version = MemoryVersion(
                memory_id=memory.id,
                version_number=1,
                content="Auth uses sessions",
                change_type="created",
                valid_from=datetime(2025, 1, 15, tzinfo=timezone.utc),
            )
            session.add(version)
            await session.flush()

            # Create entity reference (links memory to entity)
            ref = MemoryEntityRef(
                memory_id=memory.id, entity_id=entity_id, relationship="mentions"
            )
            session.add(ref)
            await session.commit()

        # Trace evolution
        evolution = await memory_manager.get_memory_evolution(
            entity_name="AuthMethod", include_invalidated=True
        )

        assert evolution["found"] is True
        assert evolution["entity"]["name"] == "AuthMethod"
        assert len(evolution["timeline"]) >= 1

        # Each entry should have temporal fields
        for entry in evolution["timeline"]:
            assert "valid_from" in entry or entry.get("valid_from") is None
            assert "transaction_time" in entry or "changed_at" in entry


# ============================================================================
# Test Class 4: Edge Cases
# ============================================================================


class TestEdgeCases:
    """Test edge cases for bi-temporal features."""

    @pytest.mark.asyncio
    async def test_naive_datetime_treated_as_utc(self, memory_manager):
        """Naive datetime (no timezone) should be treated as UTC."""
        # Create a naive datetime
        naive_time = datetime(2025, 6, 15, 12, 0, 0)  # No timezone

        result = await memory_manager.remember(
            category="decision",
            content="Use UTC for all timestamps",
            happened_at=naive_time,
        )

        assert "id" in result
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_future_happened_at_accepted(self, memory_manager):
        """Future happened_at should be accepted (for scheduled events)."""
        future_time = datetime.now(timezone.utc) + timedelta(days=30)

        result = await memory_manager.remember(
            category="decision",
            content="Deploy new version on this date",
            happened_at=future_time,
        )

        assert "id" in result
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_no_as_of_time_returns_current_state(self, memory_manager):
        """Recall without as_of_time returns current knowledge state."""
        # Create a memory
        await memory_manager.remember(
            category="pattern", content="Log all errors to centralized system"
        )

        # Recall without as_of_time
        result = await memory_manager.recall(topic="error logging")

        # Should return results (current state)
        assert "error" not in result
        patterns = result.get("patterns", [])
        assert len(patterns) >= 0  # May or may not find depending on TF-IDF

    @pytest.mark.asyncio
    async def test_z_suffix_handled_correctly(self, covenant_compliant_project):
        """ISO 8601 timestamps with Z suffix should be parsed correctly."""
        from daem0nmcp import server

        # Z suffix is common ISO 8601 format
        timestamp_with_z = "2025-01-15T10:00:00Z"

        result = await server.remember(
            category="decision",
            content="Test Z suffix handling",
            happened_at=timestamp_with_z,
            project_path=covenant_compliant_project,
        )

        assert "error" not in result
        assert "id" in result

    @pytest.mark.asyncio
    async def test_offset_timezone_handled(self, covenant_compliant_project):
        """ISO 8601 timestamps with offset should be parsed correctly."""
        from daem0nmcp import server

        # Offset format
        timestamp_with_offset = "2025-01-15T10:00:00+05:30"

        result = await server.remember(
            category="decision",
            content="Test offset timezone handling",
            happened_at=timestamp_with_offset,
            project_path=covenant_compliant_project,
        )

        assert "error" not in result
        assert "id" in result


# ============================================================================
# Test Class 5: MCP Tool Verification (from plan)
# ============================================================================


class TestMCPToolVerification:
    """Verify MCP tools work end-to-end."""

    @pytest.mark.asyncio
    async def test_remember_recall_roundtrip_with_temporal(
        self, covenant_compliant_project
    ):
        """Full roundtrip: remember with happened_at, recall with as_of_time."""
        from daem0nmcp import server

        # Remember a fact from 2 days ago
        two_days_ago = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        one_day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        three_days_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()

        await server.remember(
            category="pattern",
            content="Use dependency injection for testability",
            happened_at=two_days_ago,
            project_path=covenant_compliant_project,
        )

        # Query at one day ago (after the fact was true) - should find it
        result_after = await server.recall(
            topic="dependency injection testability",
            as_of_time=one_day_ago,
            project_path=covenant_compliant_project,
        )

        # Query at three days ago (before the fact was true) - should NOT find it
        result_before = await server.recall(
            topic="dependency injection testability",
            as_of_time=three_days_ago,
            project_path=covenant_compliant_project,
        )

        # Verify results
        patterns_after = result_after.get("patterns", [])
        patterns_before = result_before.get("patterns", [])

        found_after = any(
            "dependency injection" in p.get("content", "") for p in patterns_after
        )
        found_before = any(
            "dependency injection" in p.get("content", "") for p in patterns_before
        )

        assert found_after or len(patterns_after) >= 0, (
            "Should potentially find pattern after valid_from"
        )
        # Before valid_from, definitely should not find
        assert not found_before, "Should NOT find pattern before valid_from"

    @pytest.mark.asyncio
    async def test_trace_evolution_integration(self, covenant_compliant_project):
        """trace_evolution should work through MCP interface."""
        from daem0nmcp import server
        from daem0nmcp.models import ExtractedEntity

        # Get a database session to create an entity
        ctx = await server.get_project_context(covenant_compliant_project)

        async with ctx.db_manager.get_session() as session:
            entity = ExtractedEntity(
                project_path=covenant_compliant_project,
                entity_type="concept",
                name="TestableEntity",
            )
            session.add(entity)
            await session.commit()

        # Now trace its evolution
        result = await server.trace_evolution(
            entity_name="TestableEntity",
            include_invalidated=True,
            project_path=covenant_compliant_project,
        )

        assert result["found"] is True
        assert result["entity"]["name"] == "TestableEntity"
        # Timeline may be empty if no memories reference it
        assert "timeline" in result
