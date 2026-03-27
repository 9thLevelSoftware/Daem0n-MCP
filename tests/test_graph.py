"""Tests for graph memory functionality (relationship edges between memories)."""

import shutil
import tempfile

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


class TestLinkMemories:
    """Tests for the link_memories functionality."""

    @pytest.fixture
    async def two_memories(self, memory_manager):
        """Create two memories to link."""
        mem1 = await memory_manager.remember(
            category="decision",
            content="Use PostgreSQL for the database",
            rationale="Better for complex queries",
            project_path="/test/project",
        )
        mem2 = await memory_manager.remember(
            category="pattern",
            content="Always use connection pooling with PostgreSQL",
            rationale="Prevents connection exhaustion",
            project_path="/test/project",
        )
        return mem1["id"], mem2["id"]

    @pytest.mark.asyncio
    async def test_link_creates_relationship(self, memory_manager, two_memories):
        """Linking two memories creates a relationship edge."""
        source_id, target_id = two_memories

        result = await memory_manager.link_memories(
            source_id=source_id,
            target_id=target_id,
            relationship="led_to",
            description="Database choice led to pooling pattern",
        )

        assert result["status"] == "linked"
        assert result["source_id"] == source_id
        assert result["target_id"] == target_id
        assert result["relationship"] == "led_to"

    @pytest.mark.asyncio
    async def test_link_validates_relationship_type(self, memory_manager, two_memories):
        """Invalid relationship types are rejected."""
        source_id, target_id = two_memories

        result = await memory_manager.link_memories(
            source_id=source_id, target_id=target_id, relationship="invalid_type"
        )

        assert "error" in result
        assert (
            "relationship" in result["error"].lower()
            or "invalid" in result["error"].lower()
        )

    @pytest.mark.asyncio
    async def test_link_validates_memory_exists(self, memory_manager, two_memories):
        """Linking to non-existent memory fails."""
        source_id, _ = two_memories

        result = await memory_manager.link_memories(
            source_id=source_id, target_id=99999, relationship="led_to"
        )

        assert "error" in result

    @pytest.mark.asyncio
    async def test_link_prevents_self_reference(self, memory_manager, two_memories):
        """Cannot link a memory to itself."""
        source_id, _ = two_memories

        result = await memory_manager.link_memories(
            source_id=source_id, target_id=source_id, relationship="led_to"
        )

        assert "error" in result
        assert "self" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_link_prevents_duplicates(self, memory_manager, two_memories):
        """Cannot create duplicate relationships."""
        source_id, target_id = two_memories

        # First link succeeds
        await memory_manager.link_memories(
            source_id=source_id, target_id=target_id, relationship="led_to"
        )

        # Duplicate returns already_exists
        result = await memory_manager.link_memories(
            source_id=source_id, target_id=target_id, relationship="led_to"
        )

        assert result.get("status") == "already_exists" or "error" in result


class TestUnlinkMemories:
    """Tests for the unlink_memories functionality."""

    @pytest.fixture
    async def linked_memories(self, memory_manager):
        """Create two linked memories."""
        mem1 = await memory_manager.remember(
            category="decision",
            content="Use Redis for caching",
            project_path="/test/project",
        )
        mem2 = await memory_manager.remember(
            category="pattern",
            content="Cache invalidation strategy",
            project_path="/test/project",
        )
        await memory_manager.link_memories(
            source_id=mem1["id"], target_id=mem2["id"], relationship="led_to"
        )
        return mem1["id"], mem2["id"]

    @pytest.mark.asyncio
    async def test_unlink_removes_relationship(self, memory_manager, linked_memories):
        """Unlinking removes the relationship edge."""
        source_id, target_id = linked_memories

        result = await memory_manager.unlink_memories(
            source_id=source_id, target_id=target_id, relationship="led_to"
        )

        assert result["status"] == "unlinked"

    @pytest.mark.asyncio
    async def test_unlink_nonexistent_returns_not_found(
        self, memory_manager, linked_memories
    ):
        """Unlinking non-existent relationship returns not_found."""
        source_id, target_id = linked_memories

        result = await memory_manager.unlink_memories(
            source_id=source_id,
            target_id=target_id,
            relationship="depends_on",  # Different relationship type
        )

        assert result.get("status") == "not_found" or "error" in result


class TestTraceChain:
    """Tests for the trace_chain functionality."""

    @pytest.fixture
    async def chain_of_three(self, memory_manager):
        """Create a chain: A -> B -> C."""
        mem_a = await memory_manager.remember(
            category="decision",
            content="Memory A - the root decision",
            project_path="/test/project",
        )
        mem_b = await memory_manager.remember(
            category="pattern",
            content="Memory B - derived pattern",
            project_path="/test/project",
        )
        mem_c = await memory_manager.remember(
            category="learning",
            content="Memory C - final learning",
            project_path="/test/project",
        )

        await memory_manager.link_memories(mem_a["id"], mem_b["id"], "led_to")
        await memory_manager.link_memories(mem_b["id"], mem_c["id"], "led_to")

        return mem_a["id"], mem_b["id"], mem_c["id"]

    @pytest.mark.asyncio
    async def test_trace_forward_from_root(self, memory_manager, chain_of_three):
        """Tracing forward from root finds descendants."""
        a_id, b_id, c_id = chain_of_three

        result = await memory_manager.trace_chain(memory_id=a_id, direction="forward")

        assert result["memory_id"] == a_id
        found_ids = [m["id"] for m in result["chain"]]
        assert b_id in found_ids
        assert c_id in found_ids

    @pytest.mark.asyncio
    async def test_trace_backward_from_leaf(self, memory_manager, chain_of_three):
        """Tracing backward from leaf finds ancestors."""
        a_id, b_id, c_id = chain_of_three

        result = await memory_manager.trace_chain(memory_id=c_id, direction="backward")

        assert result["memory_id"] == c_id
        found_ids = [m["id"] for m in result["chain"]]
        assert a_id in found_ids
        assert b_id in found_ids

    @pytest.mark.asyncio
    async def test_trace_both_from_middle(self, memory_manager, chain_of_three):
        """Tracing both directions from middle finds all."""
        a_id, b_id, c_id = chain_of_three

        result = await memory_manager.trace_chain(memory_id=b_id, direction="both")

        found_ids = [m["id"] for m in result["chain"]]
        assert a_id in found_ids
        assert c_id in found_ids

    @pytest.mark.asyncio
    async def test_trace_respects_max_depth(self, memory_manager, chain_of_three):
        """Tracing respects max_depth limit."""
        a_id, b_id, c_id = chain_of_three

        result = await memory_manager.trace_chain(
            memory_id=a_id, direction="forward", max_depth=1
        )

        found_ids = [m["id"] for m in result["chain"]]
        assert b_id in found_ids
        assert c_id not in found_ids  # Beyond depth 1

    @pytest.mark.asyncio
    async def test_trace_invalid_direction(self, memory_manager, chain_of_three):
        """Invalid direction returns error."""
        a_id, _, _ = chain_of_three

        result = await memory_manager.trace_chain(memory_id=a_id, direction="invalid")

        assert "error" in result


class TestGetGraph:
    """Tests for the get_graph functionality."""

    @pytest.fixture
    async def diamond_graph(self, memory_manager):
        """
        Create a diamond graph:
            A
           / \\
          B   C
           \\ /
            D
        """
        mem_a = await memory_manager.remember(
            category="decision", content="A", project_path="/test"
        )
        mem_b = await memory_manager.remember(
            category="pattern", content="B", project_path="/test"
        )
        mem_c = await memory_manager.remember(
            category="pattern", content="C", project_path="/test"
        )
        mem_d = await memory_manager.remember(
            category="learning", content="D", project_path="/test"
        )

        await memory_manager.link_memories(mem_a["id"], mem_b["id"], "led_to")
        await memory_manager.link_memories(mem_a["id"], mem_c["id"], "led_to")
        await memory_manager.link_memories(mem_b["id"], mem_d["id"], "led_to")
        await memory_manager.link_memories(mem_c["id"], mem_d["id"], "led_to")

        return mem_a["id"], mem_b["id"], mem_c["id"], mem_d["id"]

    @pytest.mark.asyncio
    async def test_get_graph_returns_nodes_and_edges(
        self, memory_manager, diamond_graph
    ):
        """get_graph returns all nodes and edges."""
        a_id, b_id, c_id, d_id = diamond_graph

        result = await memory_manager.get_graph(memory_ids=[a_id, b_id, c_id, d_id])

        assert "nodes" in result
        assert "edges" in result
        assert len(result["nodes"]) == 4
        assert len(result["edges"]) == 4

    @pytest.mark.asyncio
    async def test_get_graph_generates_mermaid(self, memory_manager, diamond_graph):
        """get_graph can generate mermaid diagram."""
        a_id, b_id, c_id, d_id = diamond_graph

        result = await memory_manager.get_graph(
            memory_ids=[a_id, b_id, c_id, d_id], format="mermaid"
        )

        assert "mermaid" in result
        assert (
            "graph" in result["mermaid"].lower()
            or "flowchart" in result["mermaid"].lower()
        )

    @pytest.mark.asyncio
    async def test_get_graph_requires_ids_or_topic(self, memory_manager):
        """get_graph requires either memory_ids or topic."""
        result = await memory_manager.get_graph()

        assert "error" in result


class TestCompactionGraph:
    """Tests for graph relationships created by compaction."""

    @pytest.fixture
    async def compacted_memories(self, memory_manager):
        """Create and compact some memories, returning IDs."""
        original_ids = []
        for i in range(3):
            mem = await memory_manager.remember(
                category="learning",
                content=f"Original learning {i} about testing patterns and best practices",
                project_path="/test",
            )
            original_ids.append(mem["id"])

        result = await memory_manager.compact_memories(
            summary="Comprehensive summary of testing patterns and best practices learned over multiple sessions.",
            limit=10,
            dry_run=False,
        )

        return {"summary_id": result["summary_id"], "original_ids": original_ids}

    @pytest.mark.asyncio
    async def test_compaction_creates_supersedes_edges(
        self, memory_manager, compacted_memories
    ):
        """Compaction creates supersedes edges from summary to originals."""
        summary_id = compacted_memories["summary_id"]
        original_ids = compacted_memories["original_ids"]

        # Trace forward from summary should find all originals
        result = await memory_manager.trace_chain(
            memory_id=summary_id, direction="forward", relationship_types=["supersedes"]
        )

        found_ids = [m["id"] for m in result["chain"]]
        for orig_id in original_ids:
            assert orig_id in found_ids, (
                f"Original {orig_id} should be linked via supersedes"
            )

    @pytest.mark.asyncio
    async def test_compaction_archives_originals(
        self, memory_manager, compacted_memories
    ):
        """Original memories are archived after compaction."""
        original_ids = compacted_memories["original_ids"]

        # Recall should NOT find archived memories
        result = await memory_manager.recall("testing patterns", limit=20)

        all_found_ids = []
        for cat in ["decisions", "patterns", "warnings", "learnings"]:
            all_found_ids.extend([m["id"] for m in result.get(cat, [])])

        for orig_id in original_ids:
            assert orig_id not in all_found_ids, (
                f"Archived memory {orig_id} should not appear in recall"
            )

    @pytest.mark.asyncio
    async def test_summary_appears_in_recall(self, memory_manager, compacted_memories):
        """Summary memory appears in recall results."""
        summary_id = compacted_memories["summary_id"]

        result = await memory_manager.recall("testing patterns", limit=20)

        all_found_ids = []
        for cat in ["decisions", "patterns", "warnings", "learnings"]:
            all_found_ids.extend([m["id"] for m in result.get(cat, [])])

        assert summary_id in all_found_ids, "Summary should appear in recall"
