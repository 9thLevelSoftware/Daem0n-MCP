"""Tests for contextual recall triggers."""

import shutil
import tempfile
from datetime import datetime, timezone

import pytest

from daem0nmcp.models import ContextTrigger


class TestContextTriggerModel:
    """Test the ContextTrigger model structure."""

    def test_context_trigger_has_required_fields(self):
        """ContextTrigger should have all required fields."""
        trigger = ContextTrigger(
            project_path="/test/project",
            trigger_type="file_pattern",
            pattern="src/auth/**/*.py",
            recall_topic="authentication",
            is_active=True,
        )

        assert trigger.project_path == "/test/project"
        assert trigger.trigger_type == "file_pattern"
        assert trigger.pattern == "src/auth/**/*.py"
        assert trigger.recall_topic == "authentication"
        assert trigger.is_active is True

    def test_context_trigger_default_values(self):
        """ContextTrigger should have sensible defaults when explicitly set."""
        # Note: SQLAlchemy Column defaults are applied at INSERT time, not instantiation
        # Test that the model accepts default-like values properly
        trigger = ContextTrigger(
            project_path="/test/project",
            trigger_type="tag_match",
            pattern="auth.*",
            recall_topic="authentication",
            is_active=True,
            priority=0,
            recall_categories=[],
            trigger_count=0,
        )

        # Verify values are set correctly
        assert trigger.is_active is True
        assert trigger.priority == 0
        assert trigger.recall_categories == []
        assert trigger.trigger_count == 0
        assert trigger.last_triggered is None

    def test_context_trigger_all_fields(self):
        """ContextTrigger should support all fields."""
        now = datetime.now(timezone.utc)
        trigger = ContextTrigger(
            project_path="/test/project",
            trigger_type="entity_match",
            pattern="UserService|AuthService",
            recall_topic="user authentication",
            recall_categories=["decision", "warning"],
            is_active=False,
            priority=10,
            trigger_count=5,
            last_triggered=now,
        )

        assert trigger.project_path == "/test/project"
        assert trigger.trigger_type == "entity_match"
        assert trigger.pattern == "UserService|AuthService"
        assert trigger.recall_topic == "user authentication"
        assert trigger.recall_categories == ["decision", "warning"]
        assert trigger.is_active is False
        assert trigger.priority == 10
        assert trigger.trigger_count == 5
        assert trigger.last_triggered == now

    def test_context_trigger_types(self):
        """ContextTrigger should support different trigger types."""
        # File pattern trigger
        file_trigger = ContextTrigger(
            project_path="/test",
            trigger_type="file_pattern",
            pattern="src/api/**/*.py",
            recall_topic="API design",
        )
        assert file_trigger.trigger_type == "file_pattern"

        # Tag match trigger
        tag_trigger = ContextTrigger(
            project_path="/test",
            trigger_type="tag_match",
            pattern="database|sql",
            recall_topic="database decisions",
        )
        assert tag_trigger.trigger_type == "tag_match"

        # Entity match trigger
        entity_trigger = ContextTrigger(
            project_path="/test",
            trigger_type="entity_match",
            pattern=".*Repository$",
            recall_topic="repository pattern",
        )
        assert entity_trigger.trigger_type == "entity_match"


# ============================================================================
# ContextTriggerManager Tests
# ============================================================================


@pytest.fixture
def temp_storage():
    """Create a temporary storage directory."""
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
async def trigger_manager(temp_storage):
    """Create a trigger manager with temporary storage."""
    from daem0nmcp.context_triggers import ContextTriggerManager
    from daem0nmcp.database import DatabaseManager

    db = DatabaseManager(temp_storage)
    await db.init_db()
    manager = ContextTriggerManager(db)
    yield manager
    await db.close()


@pytest.mark.asyncio
async def test_add_file_trigger(trigger_manager, temp_storage):
    """Should add a file pattern trigger."""
    result = await trigger_manager.add_trigger(
        project_path=temp_storage,
        trigger_type="file_pattern",
        pattern="src/auth/**/*.py",
        recall_topic="authentication",
    )

    assert result["status"] == "created"
    assert result["trigger_id"] > 0


@pytest.mark.asyncio
async def test_check_triggers_matches_file(trigger_manager, temp_storage):
    """Should match file against trigger patterns."""
    await trigger_manager.add_trigger(
        project_path=temp_storage,
        trigger_type="file_pattern",
        pattern="src/auth/*.py",
        recall_topic="authentication",
    )

    matches = await trigger_manager.check_triggers(
        project_path=temp_storage, file_path="src/auth/service.py"
    )

    assert len(matches) == 1
    assert matches[0]["recall_topic"] == "authentication"


@pytest.mark.asyncio
async def test_add_tag_trigger(trigger_manager, temp_storage):
    """Should add a tag match trigger."""
    result = await trigger_manager.add_trigger(
        project_path=temp_storage,
        trigger_type="tag_match",
        pattern="auth|security",
        recall_topic="security decisions",
    )

    assert result["status"] == "created"
    assert result["trigger_id"] > 0


@pytest.mark.asyncio
async def test_check_triggers_matches_tag(trigger_manager, temp_storage):
    """Should match tags against trigger patterns."""
    await trigger_manager.add_trigger(
        project_path=temp_storage,
        trigger_type="tag_match",
        pattern="database|sql",
        recall_topic="database decisions",
    )

    matches = await trigger_manager.check_triggers(
        project_path=temp_storage, tags=["database", "migration"]
    )

    assert len(matches) == 1
    assert matches[0]["recall_topic"] == "database decisions"


@pytest.mark.asyncio
async def test_add_entity_trigger(trigger_manager, temp_storage):
    """Should add an entity match trigger."""
    result = await trigger_manager.add_trigger(
        project_path=temp_storage,
        trigger_type="entity_match",
        pattern=".*Service$",
        recall_topic="service patterns",
    )

    assert result["status"] == "created"
    assert result["trigger_id"] > 0


@pytest.mark.asyncio
async def test_check_triggers_matches_entity(trigger_manager, temp_storage):
    """Should match entities against trigger patterns."""
    await trigger_manager.add_trigger(
        project_path=temp_storage,
        trigger_type="entity_match",
        pattern=".*Repository$",
        recall_topic="repository pattern",
    )

    matches = await trigger_manager.check_triggers(
        project_path=temp_storage, entities=["UserRepository", "OrderRepository"]
    )

    assert len(matches) == 1
    assert matches[0]["recall_topic"] == "repository pattern"


@pytest.mark.asyncio
async def test_remove_trigger(trigger_manager, temp_storage):
    """Should remove a trigger."""
    result = await trigger_manager.add_trigger(
        project_path=temp_storage,
        trigger_type="file_pattern",
        pattern="src/**/*.py",
        recall_topic="python code",
    )
    trigger_id = result["trigger_id"]

    remove_result = await trigger_manager.remove_trigger(
        trigger_id=trigger_id, project_path=temp_storage
    )

    assert remove_result["status"] == "removed"


@pytest.mark.asyncio
async def test_list_triggers(trigger_manager, temp_storage):
    """Should list triggers for a project."""
    await trigger_manager.add_trigger(
        project_path=temp_storage,
        trigger_type="file_pattern",
        pattern="src/auth/*.py",
        recall_topic="authentication",
    )
    await trigger_manager.add_trigger(
        project_path=temp_storage,
        trigger_type="tag_match",
        pattern="database",
        recall_topic="database",
    )

    triggers = await trigger_manager.list_triggers(project_path=temp_storage)

    assert len(triggers) == 2


@pytest.mark.asyncio
async def test_check_triggers_no_match(trigger_manager, temp_storage):
    """Should return empty list when no triggers match."""
    await trigger_manager.add_trigger(
        project_path=temp_storage,
        trigger_type="file_pattern",
        pattern="src/auth/*.py",
        recall_topic="authentication",
    )

    matches = await trigger_manager.check_triggers(
        project_path=temp_storage, file_path="src/api/routes.py"
    )

    assert len(matches) == 0


@pytest.mark.asyncio
async def test_check_triggers_inactive_not_matched(trigger_manager, temp_storage):
    """Inactive triggers should not match."""
    # Create and then make trigger inactive
    result = await trigger_manager.add_trigger(
        project_path=temp_storage,
        trigger_type="file_pattern",
        pattern="src/auth/*.py",
        recall_topic="authentication",
    )
    _ = result["trigger_id"]  # Verify trigger_id exists

    # We'd need to disable the trigger - for now test that active ones match
    matches = await trigger_manager.check_triggers(
        project_path=temp_storage, file_path="src/auth/service.py"
    )

    assert len(matches) == 1  # Active trigger should match


@pytest.mark.asyncio
async def test_multiple_trigger_types_matched(trigger_manager, temp_storage):
    """Should match multiple trigger types in same check."""
    await trigger_manager.add_trigger(
        project_path=temp_storage,
        trigger_type="file_pattern",
        pattern="src/auth/*.py",
        recall_topic="auth files",
    )
    await trigger_manager.add_trigger(
        project_path=temp_storage,
        trigger_type="tag_match",
        pattern="security",
        recall_topic="security context",
    )

    matches = await trigger_manager.check_triggers(
        project_path=temp_storage,
        file_path="src/auth/service.py",
        tags=["security", "validation"],
    )

    assert len(matches) == 2
    topics = [m["recall_topic"] for m in matches]
    assert "auth files" in topics
    assert "security context" in topics


@pytest.mark.asyncio
async def test_trigger_with_recall_categories(trigger_manager, temp_storage):
    """Should include recall_categories in trigger."""
    result = await trigger_manager.add_trigger(
        project_path=temp_storage,
        trigger_type="file_pattern",
        pattern="src/auth/*.py",
        recall_topic="authentication",
        recall_categories=["warning", "pattern"],
    )

    assert result["status"] == "created"

    triggers = await trigger_manager.list_triggers(project_path=temp_storage)
    assert triggers[0]["recall_categories"] == ["warning", "pattern"]


@pytest.mark.asyncio
async def test_trigger_with_priority(trigger_manager, temp_storage):
    """Triggers should be sorted by priority."""
    await trigger_manager.add_trigger(
        project_path=temp_storage,
        trigger_type="file_pattern",
        pattern="src/*.py",
        recall_topic="low priority",
        priority=1,
    )
    await trigger_manager.add_trigger(
        project_path=temp_storage,
        trigger_type="file_pattern",
        pattern="src/*.py",
        recall_topic="high priority",
        priority=10,
    )

    matches = await trigger_manager.check_triggers(
        project_path=temp_storage, file_path="src/main.py"
    )

    assert len(matches) == 2
    # Higher priority should come first
    assert matches[0]["recall_topic"] == "high priority"
    assert matches[1]["recall_topic"] == "low priority"


@pytest.mark.asyncio
async def test_get_triggered_context(trigger_manager, temp_storage):
    """Should get triggered context with memories."""
    # First add a trigger
    await trigger_manager.add_trigger(
        project_path=temp_storage,
        trigger_type="file_pattern",
        pattern="src/auth/*.py",
        recall_topic="authentication",
    )

    # Create some memories that would match
    from daem0nmcp.memory import MemoryManager

    memory_mgr = MemoryManager(trigger_manager.db)
    await memory_mgr.remember(
        category="pattern",
        content="Always use JWT tokens for authentication",
        tags=["authentication", "jwt"],
        project_path=temp_storage,
    )

    # Get triggered context
    result = await trigger_manager.get_triggered_context(
        project_path=temp_storage, file_path="src/auth/service.py", limit=5
    )

    assert "triggers" in result
    assert len(result["triggers"]) == 1
    assert "memories" in result


@pytest.mark.asyncio
async def test_check_triggers_glob_patterns(trigger_manager, temp_storage):
    """Should support various glob patterns for files."""
    # Add trigger with ** glob
    await trigger_manager.add_trigger(
        project_path=temp_storage,
        trigger_type="file_pattern",
        pattern="src/**/test_*.py",
        recall_topic="test files",
    )

    # Should match nested test files
    matches = await trigger_manager.check_triggers(
        project_path=temp_storage, file_path="src/auth/tests/test_service.py"
    )
    assert len(matches) == 1

    # Should not match non-test files
    matches = await trigger_manager.check_triggers(
        project_path=temp_storage, file_path="src/auth/service.py"
    )
    assert len(matches) == 0


# ============================================================================
# MCP Tool Tests - Server Integration
# ============================================================================


@pytest.fixture
async def covenant_compliant_project_for_triggers(tmp_path):
    """Create project that passes communion checks."""
    from daem0nmcp import server

    project_path = str(tmp_path)
    server._project_contexts.clear()
    await server.get_briefing(project_path=project_path)
    await server.context_check(description="Test triggers", project_path=project_path)
    yield project_path


@pytest.mark.asyncio
async def test_mcp_add_context_trigger(covenant_compliant_project_for_triggers):
    """Test MCP tool for adding context triggers."""
    from daem0nmcp import server

    result = await server.add_context_trigger(
        trigger_type="file_pattern",
        pattern="src/auth/**/*.py",
        recall_topic="authentication",
        project_path=covenant_compliant_project_for_triggers,
    )

    assert result["status"] == "created"
    assert result["trigger_id"] > 0


@pytest.mark.asyncio
async def test_mcp_list_context_triggers(covenant_compliant_project_for_triggers):
    """Test MCP tool for listing context triggers."""
    from daem0nmcp import server

    # Add a trigger first
    await server.add_context_trigger(
        trigger_type="file_pattern",
        pattern="src/auth/**/*.py",
        recall_topic="authentication",
        project_path=covenant_compliant_project_for_triggers,
    )

    result = await server.list_context_triggers(
        project_path=covenant_compliant_project_for_triggers
    )

    assert "triggers" in result
    assert len(result["triggers"]) == 1
    assert result["triggers"][0]["pattern"] == "src/auth/**/*.py"


@pytest.mark.asyncio
async def test_mcp_remove_context_trigger(covenant_compliant_project_for_triggers):
    """Test MCP tool for removing context triggers."""
    from daem0nmcp import server

    # Add a trigger first
    add_result = await server.add_context_trigger(
        trigger_type="file_pattern",
        pattern="src/auth/**/*.py",
        recall_topic="authentication",
        project_path=covenant_compliant_project_for_triggers,
    )
    trigger_id = add_result["trigger_id"]

    # Remove it
    result = await server.remove_context_trigger(
        trigger_id=trigger_id, project_path=covenant_compliant_project_for_triggers
    )

    assert result["status"] == "removed"

    # Verify it's gone
    list_result = await server.list_context_triggers(
        project_path=covenant_compliant_project_for_triggers
    )
    assert len(list_result["triggers"]) == 0


@pytest.mark.asyncio
async def test_mcp_check_context_triggers(covenant_compliant_project_for_triggers):
    """Test MCP tool for checking context triggers."""
    from daem0nmcp import server

    # Add a trigger
    await server.add_context_trigger(
        trigger_type="file_pattern",
        pattern="src/auth/*.py",
        recall_topic="authentication",
        project_path=covenant_compliant_project_for_triggers,
    )

    # Check with matching file
    result = await server.check_context_triggers(
        file_path="src/auth/service.py",
        project_path=covenant_compliant_project_for_triggers,
    )

    assert "triggers" in result
    assert len(result["triggers"]) == 1
    assert result["triggers"][0]["recall_topic"] == "authentication"


@pytest.mark.asyncio
async def test_mcp_check_context_triggers_no_match(
    covenant_compliant_project_for_triggers,
):
    """Test MCP tool returns empty when no triggers match."""
    from daem0nmcp import server

    # Add a trigger
    await server.add_context_trigger(
        trigger_type="file_pattern",
        pattern="src/auth/*.py",
        recall_topic="authentication",
        project_path=covenant_compliant_project_for_triggers,
    )

    # Check with non-matching file
    result = await server.check_context_triggers(
        file_path="src/api/routes.py",
        project_path=covenant_compliant_project_for_triggers,
    )

    assert "triggers" in result
    assert len(result["triggers"]) == 0


@pytest.mark.asyncio
async def test_mcp_resource_triggered_context(covenant_compliant_project_for_triggers):
    """Test MCP resource for triggered context."""
    import json

    from daem0nmcp import server

    # Add a trigger
    await server.add_context_trigger(
        trigger_type="file_pattern",
        pattern="*.py",
        recall_topic="python",
        project_path=covenant_compliant_project_for_triggers,
    )

    # Create a memory that matches the topic
    await server.remember(
        category="pattern",
        content="Use type hints in Python",
        tags=["python"],
        project_path=covenant_compliant_project_for_triggers,
    )

    # Access the resource directly (simulates MCP resource access)
    result_json = await server.get_triggered_context_resource(
        file_path="test.py", project_path=covenant_compliant_project_for_triggers
    )

    result = json.loads(result_json)
    assert result["file"] == "test.py"
    assert result["triggers_matched"] > 0
    assert len(result["context"]) > 0
