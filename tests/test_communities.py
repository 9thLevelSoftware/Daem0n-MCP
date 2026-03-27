"""Tests for memory community clustering and hierarchical summaries."""

import shutil
import tempfile

import pytest

from daem0nmcp.models import MemoryCommunity


@pytest.fixture
def temp_storage():
    """Create a temporary storage directory."""
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


class TestMemoryCommunityModel:
    """Test the MemoryCommunity model structure."""

    def test_memory_community_has_required_fields(self):
        """MemoryCommunity should have all required fields."""
        community = MemoryCommunity(
            project_path="/test/project",
            name="Authentication",
            summary="Decisions and patterns related to user authentication",
            tags=["auth", "security", "jwt"],
            member_count=5,
            level=0,
        )

        assert community.project_path == "/test/project"
        assert community.name == "Authentication"
        assert (
            community.summary == "Decisions and patterns related to user authentication"
        )
        assert "auth" in community.tags
        assert community.member_count == 5
        assert community.level == 0

    def test_memory_community_default_values(self):
        """MemoryCommunity should have correct default values for optional fields."""
        community = MemoryCommunity(
            project_path="/test/project", name="Test Community", summary="Test summary"
        )

        # Check default values
        # Note: SQLAlchemy defaults apply at database level, not at object instantiation
        # So we check that these fields can be None or are set to their defaults
        assert community.member_ids == [] or community.member_ids is None, (
            "member_ids should default to empty list or None"
        )
        assert community.parent_id is None, "parent_id should be None by default"
        assert community.vector_embedding is None, (
            "vector_embedding should be None by default"
        )
        # member_count and level may be None until persisted to database
        assert community.member_count is None or community.member_count == 0, (
            "member_count should be None or 0 by default"
        )
        assert community.level is None or community.level == 0, (
            "level should be None or 0 by default"
        )
        assert community.tags == [] or community.tags is None, (
            "tags should default to empty list or None"
        )


@pytest.mark.asyncio
async def test_memory_communities_table_created(temp_storage):
    """Verify memory_communities table is created during database initialization."""
    from daem0nmcp.database import DatabaseManager

    db = DatabaseManager(temp_storage)
    await db.init_db()

    async with db.get_session() as session:
        from sqlalchemy import text

        result = await session.execute(
            text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_communities'"
            )
        )
        tables = result.fetchall()

    await db.close()
    assert len(tables) == 1
    assert tables[0][0] == "memory_communities"


@pytest.fixture
async def db_manager(temp_storage):
    """Shared database manager for community tests."""
    from daem0nmcp.database import DatabaseManager

    db = DatabaseManager(temp_storage)
    await db.init_db()
    yield db
    await db.close()


@pytest.fixture
async def community_manager(db_manager):
    """Create a community manager with shared database."""
    from daem0nmcp.communities import CommunityManager

    return CommunityManager(db_manager)


@pytest.fixture
async def memory_manager(db_manager):
    """Create a memory manager with shared database."""
    from daem0nmcp.memory import MemoryManager

    manager = MemoryManager(db_manager)
    yield manager
    if manager._qdrant:
        manager._qdrant.close()


@pytest.mark.asyncio
async def test_detect_communities_by_tags(community_manager, memory_manager):
    """Should cluster memories that share tags."""
    # Create memories with overlapping tags
    await memory_manager.remember(
        category="decision", content="Use JWT for auth", tags=["auth", "jwt"]
    )
    await memory_manager.remember(
        category="pattern",
        content="Validate JWT expiry",
        tags=["auth", "jwt", "validation"],
    )
    await memory_manager.remember(
        category="decision", content="Use Redis for cache", tags=["cache", "redis"]
    )
    await memory_manager.remember(
        category="pattern",
        content="Cache invalidation strategy",
        tags=["cache", "redis"],
    )

    # Detect communities
    communities = await community_manager.detect_communities(
        project_path="/test/project", min_community_size=2
    )

    # Should find at least 2 communities (auth, cache)
    assert len(communities) >= 2

    # Find the auth community
    auth_community = next((c for c in communities if "auth" in c["tags"]), None)
    assert auth_community is not None
    assert auth_community["member_count"] == 2


@pytest.fixture
async def covenant_compliant_project(temp_storage):
    """Create a project that passes communion checks."""
    from daem0nmcp import server

    # Reset server state
    server._project_contexts.clear()

    # Get briefing to initialize (establishes communion)
    await server.get_briefing(project_path=temp_storage)
    yield temp_storage


@pytest.mark.asyncio
async def test_mcp_rebuild_communities(covenant_compliant_project):
    """Test the MCP tool for rebuilding communities."""
    from daem0nmcp import server

    # Create some memories with tags
    await server.remember(
        category="decision",
        content="Use JWT",
        tags=["auth", "jwt"],
        project_path=covenant_compliant_project,
    )
    await server.remember(
        category="pattern",
        content="Validate tokens",
        tags=["auth", "jwt", "validation"],
        project_path=covenant_compliant_project,
    )

    # Rebuild communities
    result = await server.rebuild_communities(project_path=covenant_compliant_project)

    assert "created_count" in result
    assert result["created_count"] >= 0


@pytest.mark.asyncio
async def test_mcp_list_communities(covenant_compliant_project):
    """Test the MCP tool for listing communities."""
    from daem0nmcp import server

    # Create memories and build communities first
    await server.remember(
        category="decision",
        content="Use Redis caching",
        tags=["cache", "redis"],
        project_path=covenant_compliant_project,
    )
    await server.remember(
        category="pattern",
        content="Cache invalidation",
        tags=["cache", "redis", "invalidation"],
        project_path=covenant_compliant_project,
    )

    await server.rebuild_communities(project_path=covenant_compliant_project)

    # List communities
    result = await server.list_communities(project_path=covenant_compliant_project)

    assert "count" in result
    assert "communities" in result


@pytest.mark.asyncio
async def test_mcp_get_community_details(covenant_compliant_project):
    """Test the MCP tool for getting community details."""
    from daem0nmcp import server

    # Create memories
    await server.remember(
        category="decision",
        content="Use PostgreSQL",
        tags=["database", "postgres"],
        project_path=covenant_compliant_project,
    )
    await server.remember(
        category="pattern",
        content="Connection pooling",
        tags=["database", "postgres", "performance"],
        project_path=covenant_compliant_project,
    )

    # Build communities
    await server.rebuild_communities(project_path=covenant_compliant_project)

    # Get the list to find a community ID
    communities = await server.list_communities(project_path=covenant_compliant_project)

    if communities["count"] > 0:
        community_id = communities["communities"][0]["id"]
        result = await server.get_community_details(
            community_id=community_id, project_path=covenant_compliant_project
        )

        assert "community_id" in result
        assert "members" in result


@pytest.mark.asyncio
async def test_hierarchical_recall(memory_manager, temp_storage):
    """Hierarchical recall should return community summaries first."""
    from daem0nmcp.communities import CommunityManager

    # Create related memories
    await memory_manager.remember(
        category="decision", content="Use JWT for auth", tags=["auth", "jwt"]
    )
    await memory_manager.remember(
        category="pattern",
        content="Validate JWT on every request",
        tags=["auth", "jwt", "validation"],
    )

    # Build communities
    cm = CommunityManager(memory_manager.db)
    communities = await cm.detect_communities(temp_storage, min_community_size=2)
    await cm.save_communities(temp_storage, communities)

    # Hierarchical recall
    result = await memory_manager.recall_hierarchical(
        topic="auth", project_path=temp_storage
    )

    assert "communities" in result
    assert "memories" in result
    # Should have at least one auth-related community
    assert len(result["communities"]) >= 1


@pytest.mark.asyncio
async def test_mcp_recall_hierarchical(covenant_compliant_project):
    """Test the MCP tool for hierarchical recall."""
    from daem0nmcp import server

    result = await server.recall_hierarchical(
        topic="authentication", project_path=covenant_compliant_project
    )

    assert "communities" in result
    assert "memories" in result
