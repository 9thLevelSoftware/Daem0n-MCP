"""Tests for auto entity extraction from memories."""

import shutil
import tempfile

import pytest

from daem0nmcp.models import ExtractedEntity, MemoryEntityRef


class TestExtractedEntityModel:
    """Test the ExtractedEntity model structure."""

    def test_extracted_entity_has_required_fields(self):
        """ExtractedEntity should have all required fields."""
        entity = ExtractedEntity(
            project_path="/test/project",
            entity_type="function",
            name="authenticate_user",
            qualified_name="auth.service.authenticate_user",
            mention_count=3,
        )

        assert entity.project_path == "/test/project"
        assert entity.entity_type == "function"
        assert entity.name == "authenticate_user"
        assert entity.mention_count == 3

    def test_extracted_entity_optional_fields(self):
        """ExtractedEntity should have optional code_entity_id field."""
        entity = ExtractedEntity(
            project_path="/test/project",
            entity_type="class",
            name="UserService",
            code_entity_id="abc123",
        )

        assert entity.code_entity_id == "abc123"
        assert entity.qualified_name is None
        # Note: mention_count default (1) is applied at DB insert time, not object creation


class TestMemoryEntityRefModel:
    """Test the MemoryEntityRef model structure."""

    def test_memory_entity_ref_has_required_fields(self):
        """MemoryEntityRef should link memory to entity."""
        ref = MemoryEntityRef(
            memory_id=1,
            entity_id=42,
            relationship="mentions",
            context_snippet="...calls authenticate_user()...",
        )

        assert ref.memory_id == 1
        assert ref.entity_id == 42
        assert ref.relationship == "mentions"
        assert ref.context_snippet == "...calls authenticate_user()..."

    def test_memory_entity_ref_optional_fields(self):
        """MemoryEntityRef should allow optional context_snippet."""
        ref = MemoryEntityRef(memory_id=1, entity_id=42, relationship="about")

        assert ref.relationship == "about"
        assert ref.context_snippet is None
        # Note: relationship default ('mentions') is applied at DB insert time, not object creation


@pytest.fixture
def extractor():
    """Create an entity extractor."""
    from daem0nmcp.entity_extractor import EntityExtractor

    return EntityExtractor()


class TestEntityExtractor:
    """Test entity extraction from text."""

    def test_extract_function_names(self, extractor):
        """Should extract function names from content."""
        text = (
            "Call authenticate_user() to verify the token, then call get_permissions()"
        )
        entities = extractor.extract_entities(text)

        functions = [e for e in entities if e["type"] == "function"]
        names = [f["name"] for f in functions]

        assert "authenticate_user" in names
        assert "get_permissions" in names

    def test_extract_class_names(self, extractor):
        """Should extract class names from content."""
        text = (
            "The UserService class handles auth. Use AuthController for API endpoints."
        )
        entities = extractor.extract_entities(text)

        classes = [e for e in entities if e["type"] == "class"]
        names = [c["name"] for c in classes]

        assert "UserService" in names
        assert "AuthController" in names

    def test_extract_file_paths(self, extractor):
        """Should extract file paths from content."""
        text = "Edit src/auth/service.py and update tests/test_auth.py"
        entities = extractor.extract_entities(text)

        files = [e for e in entities if e["type"] == "file"]
        names = [f["name"] for f in files]

        assert "src/auth/service.py" in names
        assert "tests/test_auth.py" in names


@pytest.fixture
def temp_storage():
    """Create a temporary storage directory."""
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
async def entity_manager(temp_storage):
    """Create an entity manager with temporary storage."""
    from daem0nmcp.database import DatabaseManager
    from daem0nmcp.entity_manager import EntityManager

    db = DatabaseManager(temp_storage)
    await db.init_db()
    manager = EntityManager(db)
    yield manager
    await db.close()


@pytest.mark.asyncio
async def test_process_memory_extracts_entities(entity_manager, temp_storage):
    """Processing a memory should extract and store entities."""
    from daem0nmcp.database import DatabaseManager
    from daem0nmcp.memory import MemoryManager

    db = DatabaseManager(temp_storage)
    await db.init_db()
    mem_manager = MemoryManager(db)

    # Create a memory with entity references
    mem = await mem_manager.remember(
        category="decision",
        content="Use authenticate_user() in the UserService class for auth",
    )

    # Process it
    result = await entity_manager.process_memory(
        memory_id=mem["id"], content=mem["content"], project_path=temp_storage
    )

    assert result["entities_found"] > 0
    assert result["refs_created"] > 0

    # Verify entities were stored
    entities = await entity_manager.get_entities_for_memory(mem["id"])
    assert len(entities) > 0

    await db.close()


@pytest.mark.asyncio
async def test_get_memories_for_entity(entity_manager, temp_storage):
    """Should retrieve memories by entity name."""
    from daem0nmcp.database import DatabaseManager
    from daem0nmcp.memory import MemoryManager

    db = DatabaseManager(temp_storage)
    await db.init_db()
    mem_manager = MemoryManager(db)

    mem = await mem_manager.remember(
        category="decision", content="Use UserService for auth operations"
    )
    await entity_manager.process_memory(
        memory_id=mem["id"], content=mem["content"], project_path=temp_storage
    )

    result = await entity_manager.get_memories_for_entity(
        entity_name="UserService", project_path=temp_storage
    )

    assert result["found"] is True
    assert len(result["memories"]) == 1
    await db.close()


@pytest.mark.asyncio
async def test_get_popular_entities(entity_manager, temp_storage):
    """Should return most mentioned entities."""
    from daem0nmcp.database import DatabaseManager
    from daem0nmcp.memory import MemoryManager

    db = DatabaseManager(temp_storage)
    await db.init_db()
    mem_manager = MemoryManager(db)

    # Create multiple memories mentioning same entity
    for i in range(3):
        mem = await mem_manager.remember(
            category="decision", content=f"Call authenticate_user() for step {i}"
        )
        await entity_manager.process_memory(
            memory_id=mem["id"], content=mem["content"], project_path=temp_storage
        )

    popular = await entity_manager.get_popular_entities(temp_storage, limit=5)

    assert len(popular) > 0
    await db.close()


@pytest.mark.asyncio
async def test_remember_auto_extracts_entities(temp_storage):
    """remember() should auto-extract entities."""
    from daem0nmcp.database import DatabaseManager
    from daem0nmcp.entity_manager import EntityManager
    from daem0nmcp.memory import MemoryManager

    db = DatabaseManager(temp_storage)
    await db.init_db()
    mem_manager = MemoryManager(db)
    ent_manager = EntityManager(db)

    # Create memory (should auto-extract)
    mem = await mem_manager.remember(
        category="decision",
        content="Call authenticate_user() in UserService",
        project_path=temp_storage,
    )

    # Check entities were extracted
    entities = await ent_manager.get_entities_for_memory(mem["id"])

    await db.close()

    assert len(entities) > 0
    names = [e["name"] for e in entities]
    assert "authenticate_user" in names


# ============================================================================
# MCP Tool Tests for Entity Queries
# ============================================================================


@pytest.fixture
async def covenant_compliant_project_for_entities(tmp_path):
    """Create a project that passes communion and counsel checks for entity tests."""
    from daem0nmcp import server

    project_path = str(tmp_path)

    # Reset server state
    server._project_contexts.clear()

    # Establish communion (get_briefing)
    # This creates the DB at the right path: project_path/.daem0nmcp/storage
    await server.get_briefing(project_path=project_path)

    # Establish counsel (context_check)
    await server.context_check(
        description="Test entity operations", project_path=project_path
    )

    yield project_path


@pytest.mark.asyncio
async def test_mcp_recall_by_entity(covenant_compliant_project_for_entities):
    """Test MCP tool for recalling memories by entity."""
    from daem0nmcp import server

    # Create memory with entity
    await server.remember(
        category="decision",
        content="Use UserService.authenticate() for login",
        project_path=covenant_compliant_project_for_entities,
    )

    # Query by entity
    result = await server.recall_by_entity(
        entity_name="UserService", project_path=covenant_compliant_project_for_entities
    )

    assert "memories" in result
    assert result["found"] is True


@pytest.mark.asyncio
async def test_mcp_recall_by_entity_with_type(covenant_compliant_project_for_entities):
    """Test MCP tool for recalling by entity with type filter."""
    from daem0nmcp import server

    # Create memory with entity
    await server.remember(
        category="pattern",
        content="Call validate_input() before processing",
        project_path=covenant_compliant_project_for_entities,
    )

    # Query by entity with type filter
    result = await server.recall_by_entity(
        entity_name="validate_input",
        entity_type="function",
        project_path=covenant_compliant_project_for_entities,
    )

    assert "memories" in result


@pytest.mark.asyncio
async def test_mcp_list_entities(covenant_compliant_project_for_entities):
    """Test MCP tool for listing entities."""
    from daem0nmcp import server

    # Create memories with entities
    await server.remember(
        category="decision",
        content="Use UserService for authentication",
        project_path=covenant_compliant_project_for_entities,
    )
    await server.remember(
        category="pattern",
        content="Call UserService.validate() before any action",
        project_path=covenant_compliant_project_for_entities,
    )

    # List entities
    result = await server.list_entities(
        project_path=covenant_compliant_project_for_entities
    )

    assert "entities" in result
    assert isinstance(result["entities"], list)


@pytest.mark.asyncio
async def test_mcp_list_entities_with_type_filter(
    covenant_compliant_project_for_entities,
):
    """Test MCP tool for listing entities with type filter."""
    from daem0nmcp import server

    # Create memories with entities
    await server.remember(
        category="decision",
        content="Use authenticate_user() function in AuthService class",
        project_path=covenant_compliant_project_for_entities,
    )

    # List only function entities
    result = await server.list_entities(
        entity_type="function", project_path=covenant_compliant_project_for_entities
    )

    assert "entities" in result
    # All returned entities should be functions
    for entity in result["entities"]:
        assert entity["type"] == "function"


@pytest.mark.asyncio
async def test_mcp_backfill_entities(covenant_compliant_project_for_entities):
    """Test MCP tool for backfilling entities from existing memories."""
    from daem0nmcp import server

    # Create memory (it will auto-extract, but let's test backfill anyway)
    await server.remember(
        category="decision",
        content="The DatabaseManager handles connections",
        project_path=covenant_compliant_project_for_entities,
    )

    # Run backfill
    result = await server.backfill_entities(
        project_path=covenant_compliant_project_for_entities
    )

    assert "memories_processed" in result
    assert "entities_extracted" in result
    assert result["memories_processed"] >= 1


@pytest.mark.asyncio
async def test_mcp_recall_by_entity_missing_project_path():
    """Test that recall_by_entity requires project_path."""
    from daem0nmcp import server

    # Clear any default project path
    original_default = server._default_project_path
    server._default_project_path = None
    server._project_contexts.clear()

    try:
        result = await server.recall_by_entity(
            entity_name="UserService", project_path=None
        )
        assert "error" in result
        assert result["error"] == "MISSING_PROJECT_PATH"
    finally:
        server._default_project_path = original_default


@pytest.mark.asyncio
async def test_mcp_list_entities_missing_project_path():
    """Test that list_entities requires project_path."""
    from daem0nmcp import server

    # Clear any default project path
    original_default = server._default_project_path
    server._default_project_path = None
    server._project_contexts.clear()

    try:
        result = await server.list_entities(project_path=None)
        assert "error" in result
        assert result["error"] == "MISSING_PROJECT_PATH"
    finally:
        server._default_project_path = original_default


@pytest.mark.asyncio
async def test_mcp_backfill_entities_missing_project_path():
    """Test that backfill_entities requires project_path."""
    from daem0nmcp import server

    # Clear any default project path
    original_default = server._default_project_path
    server._default_project_path = None
    server._project_contexts.clear()

    try:
        result = await server.backfill_entities(project_path=None)
        assert "error" in result
        assert result["error"] == "MISSING_PROJECT_PATH"
    finally:
        server._default_project_path = original_default
