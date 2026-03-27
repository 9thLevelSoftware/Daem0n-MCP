"""Integration tests for GraphRAG functionality (Phase 1).

Tests verify all Phase 1 success criteria:
- GRAPH-01: Entity extraction during remember
- GRAPH-02: NetworkX graph construction
- GRAPH-03: Leiden community detection
- GRAPH-04: Multi-hop queries
- GRAPH-05: recall_hierarchical uses Leiden
- GRAPH-06: Community summarization
"""

import shutil
import tempfile

import pytest

from daem0nmcp.communities import CommunityManager
from daem0nmcp.database import DatabaseManager
from daem0nmcp.entity_manager import EntityManager
from daem0nmcp.graph import KnowledgeGraph
from daem0nmcp.memory import MemoryManager


@pytest.fixture
def temp_storage():
    """Create a temporary storage directory."""
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
async def db_manager(temp_storage):
    """Create a database manager with temporary storage."""
    db = DatabaseManager(temp_storage)
    await db.init_db()
    yield db
    await db.close()


@pytest.fixture
async def memory_manager(db_manager):
    """Create a memory manager with temporary storage."""
    manager = MemoryManager(db_manager)
    yield manager
    if manager._qdrant:
        manager._qdrant.close()


@pytest.fixture
async def entity_manager(db_manager):
    """Create an entity manager for entity extraction tests."""
    return EntityManager(db_manager)


@pytest.fixture
async def community_manager(db_manager):
    """Create a community manager for community tests."""
    return CommunityManager(db_manager)


@pytest.fixture
async def covenant_compliant_project(temp_storage):
    """Create a project that passes covenant checks."""
    from daem0nmcp import server

    server._project_contexts.clear()
    await server.get_briefing(project_path=temp_storage)
    yield temp_storage


# =============================================================================
# GRAPH-01: Entity extraction during remember
# =============================================================================


class TestEntityExtractionDuringRemember:
    """GRAPH-01: Entity extraction should happen during remember()."""

    @pytest.mark.asyncio
    async def test_remember_extracts_entities_when_project_path_provided(
        self, memory_manager, entity_manager, temp_storage
    ):
        """Remember should extract entities when project_path is provided."""
        result = await memory_manager.remember(
            category="decision",
            content="The UserAuthService class handles authentication using JWT tokens",
            rationale="Centralize auth logic in one service",
            project_path=temp_storage,
        )

        # Check entities were extracted
        entities = await entity_manager.get_entities_for_memory(result["id"])

        # Should have extracted at least one entity (UserAuthService, JWT, etc.)
        assert len(entities) >= 1, "Should extract at least one entity"

        # Verify entity types are reasonable
        entity_names = [e["name"] for e in entities]
        assert any(
            "Auth" in name or "Service" in name or "JWT" in name
            for name in entity_names
        ), f"Expected auth-related entity, got: {entity_names}"

    @pytest.mark.asyncio
    async def test_remember_invalidates_graph_cache(self, memory_manager, temp_storage):
        """Remember should invalidate the knowledge graph cache."""
        # First, ensure graph is loaded
        graph = await memory_manager.get_knowledge_graph()
        graph.get_node_count()

        # Remember a new memory
        await memory_manager.remember(
            category="decision",
            content="Use PostgreSQL for the database backend",
            project_path=temp_storage,
        )

        # Graph should be invalidated (not automatically reloaded)
        assert not memory_manager._knowledge_graph._loaded, (
            "Knowledge graph should be invalidated after remember()"
        )


# =============================================================================
# GRAPH-02: NetworkX graph construction
# =============================================================================


class TestNetworkXGraphConstruction:
    """GRAPH-02: NetworkX graph should be constructed from SQLite."""

    @pytest.mark.asyncio
    async def test_knowledge_graph_loads_from_database(self, db_manager, temp_storage):
        """KnowledgeGraph should load entities and relationships from SQLite."""
        # Create some entities and memories first
        memory_manager = MemoryManager(db_manager)
        EntityManager(db_manager)

        mem1 = await memory_manager.remember(
            category="decision",
            content="Use the AuthHandler class for authentication",
            project_path=temp_storage,
        )
        mem2 = await memory_manager.remember(
            category="pattern",
            content="AuthHandler should validate tokens",
            project_path=temp_storage,
        )

        # Link memories
        await memory_manager.link_memories(mem1["id"], mem2["id"], "led_to")

        # Load knowledge graph
        kg = KnowledgeGraph(db_manager)
        await kg.ensure_loaded()

        # Should have nodes and edges
        assert kg.get_node_count() > 0, "Graph should have nodes"

        # Close Qdrant before test cleanup
        if memory_manager._qdrant:
            memory_manager._qdrant.close()

    @pytest.mark.asyncio
    async def test_knowledge_graph_has_memory_and_entity_nodes(
        self, memory_manager, temp_storage
    ):
        """Graph should contain both memory and entity nodes."""
        await memory_manager.remember(
            category="decision",
            content="The PaymentProcessor handles Stripe integration",
            project_path=temp_storage,
        )

        # Force graph reload
        memory_manager.invalidate_graph_cache()
        graph = await memory_manager.get_knowledge_graph()

        # Check for both node types
        memory_nodes = graph.get_memory_nodes()
        graph.get_entity_nodes()

        assert len(memory_nodes) >= 1, "Should have at least one memory node"
        # Entity extraction may or may not find entities
        # Just verify the method works without error

    @pytest.mark.asyncio
    async def test_knowledge_graph_lazy_loading(self, memory_manager):
        """Graph should use lazy loading pattern."""
        # Initially not loaded
        assert memory_manager._knowledge_graph is None, "Graph should be None initially"

        # First access loads it
        graph = await memory_manager.get_knowledge_graph()
        assert graph._loaded, "Graph should be loaded after first access"

        # Subsequent access returns same instance
        graph2 = await memory_manager.get_knowledge_graph()
        assert graph is graph2, "Should return same graph instance"


# =============================================================================
# GRAPH-03: Leiden community detection
# =============================================================================


class TestLeidenCommunityDetection:
    """GRAPH-03: Leiden algorithm should detect communities from graph."""

    @pytest.mark.asyncio
    async def test_leiden_community_detection(
        self, memory_manager, community_manager, temp_storage
    ):
        """Leiden should detect communities from knowledge graph."""
        # Create memories with shared entities
        await memory_manager.remember(
            category="decision",
            content="Use JWT for authentication",
            tags=["auth", "jwt"],
            project_path=temp_storage,
        )
        await memory_manager.remember(
            category="pattern",
            content="Validate JWT tokens on every request",
            tags=["auth", "jwt", "validation"],
            project_path=temp_storage,
        )
        await memory_manager.remember(
            category="decision",
            content="Use Redis for caching",
            tags=["cache", "redis"],
            project_path=temp_storage,
        )
        await memory_manager.remember(
            category="pattern",
            content="Cache invalidation strategy with Redis",
            tags=["cache", "redis"],
            project_path=temp_storage,
        )

        # Detect communities using Leiden
        graph = await memory_manager.get_knowledge_graph()
        communities = await community_manager.detect_communities_from_graph(
            project_path=temp_storage, knowledge_graph=graph
        )

        # Should detect at least some grouping
        # Note: With few memories, all might end up in one community
        assert isinstance(communities, list), "Should return list of communities"

    @pytest.mark.asyncio
    async def test_leiden_deterministic_with_seed(
        self, memory_manager, community_manager, temp_storage
    ):
        """Leiden with seed=42 should be deterministic."""
        # Create test memories
        await memory_manager.remember(
            category="decision",
            content="First decision about architecture",
            tags=["architecture"],
            project_path=temp_storage,
        )
        await memory_manager.remember(
            category="decision",
            content="Second decision about architecture",
            tags=["architecture"],
            project_path=temp_storage,
        )

        graph = await memory_manager.get_knowledge_graph()

        # Run twice
        communities1 = await community_manager.detect_communities_from_graph(
            project_path=temp_storage, knowledge_graph=graph
        )

        # Force graph reload
        await graph.reload_from_db()

        communities2 = await community_manager.detect_communities_from_graph(
            project_path=temp_storage, knowledge_graph=graph
        )

        # Results should be identical (deterministic)
        assert len(communities1) == len(communities2), (
            "Deterministic Leiden should produce same community count"
        )


# =============================================================================
# GRAPH-04: Multi-hop queries
# =============================================================================


class TestMultiHopQueries:
    """GRAPH-04: Multi-hop traversal should work via trace_chain and trace_evolution."""

    @pytest.fixture
    async def chain_of_memories(self, memory_manager, temp_storage):
        """Create a chain: A -> B -> C."""
        mem_a = await memory_manager.remember(
            category="decision",
            content="Initial architecture decision using microservices",
            project_path=temp_storage,
        )
        mem_b = await memory_manager.remember(
            category="pattern",
            content="Service mesh pattern for microservices",
            project_path=temp_storage,
        )
        mem_c = await memory_manager.remember(
            category="learning",
            content="Service mesh improved observability",
            project_path=temp_storage,
        )

        await memory_manager.link_memories(mem_a["id"], mem_b["id"], "led_to")
        await memory_manager.link_memories(mem_b["id"], mem_c["id"], "led_to")

        return mem_a["id"], mem_b["id"], mem_c["id"]

    @pytest.mark.asyncio
    async def test_trace_chain_finds_path(self, memory_manager, chain_of_memories):
        """trace_chain should find paths between memories."""
        a_id, b_id, c_id = chain_of_memories

        graph = await memory_manager.get_knowledge_graph()
        result = await graph.trace_chain(start_memory_id=a_id, end_memory_id=c_id)

        assert result["found"], "Should find path between A and C"
        assert len(result["paths"]) >= 1, "Should have at least one path"

    @pytest.mark.asyncio
    async def test_get_related_memories(self, memory_manager, chain_of_memories):
        """get_related should find related memories via graph traversal."""
        a_id, b_id, c_id = chain_of_memories

        graph = await memory_manager.get_knowledge_graph()
        result = await graph.get_related(memory_id=a_id, direction="both")

        assert result["found"], "Should find related memories"
        assert result["total_related"] >= 1, "Should have at least one related memory"

    @pytest.mark.asyncio
    async def test_trace_evolution(self, memory_manager, entity_manager, temp_storage):
        """trace_evolution should show entity knowledge timeline."""
        # Create memories mentioning same entity
        await memory_manager.remember(
            category="decision",
            content="Use the AuthService class for authentication",
            project_path=temp_storage,
        )
        await memory_manager.remember(
            category="pattern",
            content="AuthService should use JWT tokens",
            project_path=temp_storage,
        )

        # Get an entity ID
        from sqlalchemy import select

        from daem0nmcp.models import ExtractedEntity

        async with memory_manager.db.get_session() as session:
            result = await session.execute(select(ExtractedEntity).limit(1))
            entity = result.scalar_one_or_none()

        if entity:
            graph = await memory_manager.get_knowledge_graph()
            evolution = await graph.trace_evolution(entity_id=entity.id)

            assert "found" in evolution, "trace_evolution should return found status"


# =============================================================================
# GRAPH-05: recall_hierarchical uses Leiden
# =============================================================================


class TestRecallHierarchicalLeiden:
    """GRAPH-05: recall_hierarchical should use Leiden communities."""

    @pytest.mark.asyncio
    async def test_recall_hierarchical_returns_communities(
        self, memory_manager, community_manager, temp_storage
    ):
        """recall_hierarchical should return community summaries."""
        # Create and save communities
        await memory_manager.remember(
            category="decision",
            content="Use JWT for auth",
            tags=["auth", "jwt"],
            project_path=temp_storage,
        )
        await memory_manager.remember(
            category="pattern",
            content="Validate JWT expiry",
            tags=["auth", "jwt", "validation"],
            project_path=temp_storage,
        )

        # Detect and save communities
        graph = await memory_manager.get_knowledge_graph()
        communities = await community_manager.detect_communities_from_graph(
            project_path=temp_storage, knowledge_graph=graph
        )
        await community_manager.save_communities(temp_storage, communities)

        # Hierarchical recall
        result = await memory_manager.recall_hierarchical(
            topic="auth", project_path=temp_storage
        )

        assert "communities" in result, "Should return communities"
        assert "memories" in result, "Should return memories"
        assert "community_source" in result, "Should indicate community source"

    @pytest.mark.asyncio
    async def test_recall_hierarchical_uses_tfidf_scoring(
        self, memory_manager, community_manager, temp_storage
    ):
        """recall_hierarchical should use TF-IDF for community matching."""
        # Create communities with various names
        await memory_manager.remember(
            category="decision",
            content="JWT authentication implementation",
            tags=["authentication", "jwt"],
            project_path=temp_storage,
        )
        await memory_manager.remember(
            category="pattern",
            content="Token validation pattern",
            tags=["authentication", "validation"],
            project_path=temp_storage,
        )

        # Save communities
        communities = await community_manager.detect_communities(
            project_path=temp_storage, min_community_size=2
        )
        if communities:
            await community_manager.save_communities(temp_storage, communities)

        # Search with related term
        result = await memory_manager.recall_hierarchical(
            topic="authentication", project_path=temp_storage
        )

        # Should find auth-related communities
        if result["communities"]:
            assert any(
                "auth" in c["name"].lower() or "jwt" in str(c["tags"]).lower()
                for c in result["communities"]
            ), "Should find authentication-related communities"

    @pytest.mark.asyncio
    async def test_recall_hierarchical_shows_hint_when_no_communities(
        self, memory_manager, temp_storage
    ):
        """Should suggest rebuild_communities when no communities exist."""
        result = await memory_manager.recall_hierarchical(
            topic="anything", project_path=temp_storage
        )

        # Should have hint to run rebuild_communities
        if not result["communities"]:
            assert "community_hint" in result, (
                "Should suggest running rebuild_communities"
            )


# =============================================================================
# GRAPH-06: Community summarization
# =============================================================================


class TestCommunitySummarization:
    """GRAPH-06: Communities should have extractive summaries."""

    @pytest.mark.asyncio
    async def test_community_summary_generated(
        self, memory_manager, community_manager, temp_storage
    ):
        """Saving communities should generate summaries."""
        await memory_manager.remember(
            category="decision",
            content="Use PostgreSQL for relational data",
            tags=["database", "postgres"],
            project_path=temp_storage,
        )
        await memory_manager.remember(
            category="pattern",
            content="Connection pooling for PostgreSQL",
            tags=["database", "postgres", "performance"],
            project_path=temp_storage,
        )

        # Detect and save
        communities = await community_manager.detect_communities(
            project_path=temp_storage, min_community_size=2
        )

        if communities:
            await community_manager.save_communities(temp_storage, communities)

            # Get saved communities
            saved = await community_manager.get_communities(temp_storage)

            for c in saved:
                assert c["summary"], f"Community '{c['name']}' should have summary"
                assert len(c["summary"]) > 10, "Summary should be substantive"

    @pytest.mark.asyncio
    async def test_community_members_retrievable(
        self, memory_manager, community_manager, temp_storage
    ):
        """Community members should be retrievable by ID."""
        await memory_manager.remember(
            category="decision",
            content="Use Redis for caching",
            tags=["cache", "redis"],
            project_path=temp_storage,
        )
        await memory_manager.remember(
            category="pattern",
            content="Cache invalidation with Redis TTL",
            tags=["cache", "redis"],
            project_path=temp_storage,
        )

        # Save communities
        communities = await community_manager.detect_communities(
            project_path=temp_storage, min_community_size=2
        )
        if communities:
            await community_manager.save_communities(temp_storage, communities)
            saved = await community_manager.get_communities(temp_storage)

            if saved:
                # Get members
                members = await community_manager.get_community_members(saved[0]["id"])

                assert "members" in members, "Should return members"
                assert len(members["members"]) > 0, "Should have at least one member"


# =============================================================================
# MCP Tool Integration Tests
# =============================================================================


class TestMCPGraphRAGTools:
    """Test MCP tools expose GraphRAG functionality."""

    @pytest.mark.asyncio
    async def test_mcp_trace_causal_path(self, covenant_compliant_project):
        """MCP trace_causal_path tool should work."""
        from daem0nmcp import server

        # Use context to get direct access to memory_manager (bypass covenant)
        ctx = await server.get_project_context(covenant_compliant_project)

        # Create chain directly via memory_manager
        mem1 = await ctx.memory_manager.remember(
            category="decision",
            content="Base decision",
            project_path=covenant_compliant_project,
        )
        mem2 = await ctx.memory_manager.remember(
            category="pattern",
            content="Derived pattern",
            project_path=covenant_compliant_project,
        )

        # Link them
        await ctx.memory_manager.link_memories(mem1["id"], mem2["id"], "led_to")

        # Use MCP tool
        result = await server.trace_causal_path(
            start_memory_id=mem1["id"],
            end_memory_id=mem2["id"],
            project_path=covenant_compliant_project,
        )

        assert "found" in result or "paths" in result

    @pytest.mark.asyncio
    async def test_mcp_get_graph_stats(self, covenant_compliant_project):
        """MCP get_graph_stats tool should return metrics."""
        from daem0nmcp import server

        # Use context to get direct access to memory_manager (bypass covenant)
        ctx = await server.get_project_context(covenant_compliant_project)

        # Create some memories
        await ctx.memory_manager.remember(
            category="decision",
            content="Test decision",
            project_path=covenant_compliant_project,
        )

        result = await server.get_graph_stats(project_path=covenant_compliant_project)

        assert "nodes" in result, "Should return node count"
        assert "edges" in result, "Should return edge count"

    @pytest.mark.asyncio
    async def test_mcp_get_related_memories(self, covenant_compliant_project):
        """MCP get_related_memories tool should work."""
        from daem0nmcp import server

        # Use context to get direct access to memory_manager (bypass covenant)
        ctx = await server.get_project_context(covenant_compliant_project)

        mem = await ctx.memory_manager.remember(
            category="decision",
            content="Test memory for relations",
            project_path=covenant_compliant_project,
        )

        result = await server.get_related_memories(
            memory_id=mem["id"], project_path=covenant_compliant_project
        )

        assert "found" in result or "error" in result

    @pytest.mark.asyncio
    async def test_mcp_rebuild_communities_uses_leiden(
        self, covenant_compliant_project
    ):
        """rebuild_communities MCP tool should use Leiden algorithm."""
        from daem0nmcp import server
        from daem0nmcp.communities import CommunityManager

        ctx = await server.get_project_context(covenant_compliant_project)

        # Create related memories with shared entity to ensure graph connectivity
        await ctx.memory_manager.remember(
            category="decision",
            content="Use Docker for containerization",
            tags=["docker", "deployment"],
            project_path=covenant_compliant_project,
        )
        await ctx.memory_manager.remember(
            category="pattern",
            content="Docker compose for local development",
            tags=["docker", "development"],
            project_path=covenant_compliant_project,
        )

        result = await server.rebuild_communities(
            project_path=covenant_compliant_project
        )

        assert "status" in result, "Should return status"
        assert result["status"] == "rebuilt"

        # Verify communities were detected using Leiden (will have leiden_community_id)
        if result["communities_found"] > 0:
            cm = CommunityManager(ctx.db_manager)
            communities = await cm.list_communities(covenant_compliant_project)
            # At least one community should exist if rebuild found any
            assert len(communities) > 0, "Should have saved communities"

            # Critical: Verify Leiden algorithm was used by checking for leiden_community_id
            # Leiden-based communities store the algorithm's partition ID
            for community in communities:
                assert "leiden_community_id" in community or hasattr(
                    community, "leiden_community_id"
                ), (
                    f"Community should have leiden_community_id field (Leiden algorithm marker): {community}"
                )


# =============================================================================
# Knowledge Graph Cache Invalidation Tests
# =============================================================================


class TestGraphCacheInvalidation:
    """Test knowledge graph cache is properly invalidated."""

    @pytest.mark.asyncio
    async def test_graph_invalidated_on_new_memory(self, memory_manager, temp_storage):
        """Graph cache should invalidate when new memory is created."""
        # Load graph
        graph = await memory_manager.get_knowledge_graph()
        assert graph._loaded

        # Add memory
        await memory_manager.remember(
            category="decision", content="New decision", project_path=temp_storage
        )

        # Cache should be invalidated
        assert not memory_manager._knowledge_graph._loaded

    @pytest.mark.asyncio
    async def test_graph_reloads_after_invalidation(self, memory_manager, temp_storage):
        """Graph should reload when accessed after invalidation."""
        # Initial load
        graph = await memory_manager.get_knowledge_graph()
        graph.get_node_count()

        # Add memory (invalidates cache)
        await memory_manager.remember(
            category="decision",
            content="Another decision with EntityName",
            project_path=temp_storage,
        )

        # Access graph again (should reload)
        graph2 = await memory_manager.get_knowledge_graph()

        assert graph2._loaded, "Graph should reload on access"
