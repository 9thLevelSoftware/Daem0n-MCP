"""Integration tests for memory compaction feature."""

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


class TestCompactionIntegration:
    """End-to-end tests for compaction workflow."""

    @pytest.mark.asyncio
    async def test_full_compaction_workflow(self, memory_manager):
        """Test complete compaction: create -> compact -> verify graph -> verify recall."""
        # Step 1: Create memories
        created = []
        for i in range(5):
            mem = await memory_manager.remember(
                category="learning",
                content=f"Session {i}: Learned about API design pattern {i} and best practices",
                rationale=f"Discovered while building feature {i}",
                tags=["api", "patterns"],
                project_path="/test",
            )
            created.append(mem)

        # Step 2: Verify they appear in recall
        pre_compact = await memory_manager.recall("API design patterns", limit=20)
        pre_count = len(pre_compact.get("learnings", []))
        assert pre_count == 5

        # Step 3: Dry run first
        dry_result = await memory_manager.compact_memories(
            summary="Comprehensive summary of API design patterns and best practices learned across 5 sessions.",
            limit=10,
            topic="api",
            dry_run=True,
        )
        assert dry_result["status"] == "dry_run"
        assert dry_result["would_compact"] == 5

        # Step 4: Execute compaction
        result = await memory_manager.compact_memories(
            summary="Comprehensive summary of API design patterns and best practices learned across 5 sessions.",
            limit=10,
            topic="api",
            dry_run=False,
        )

        assert result["status"] == "compacted"
        assert result["compacted_count"] == 5
        summary_id = result["summary_id"]

        # Step 5: Verify graph structure
        chain = await memory_manager.trace_chain(summary_id, direction="forward")
        assert len(chain["chain"]) == 5
        assert all(m["relationship"] == "supersedes" for m in chain["chain"])

        # Step 6: Verify recall sees summary, not originals
        post_compact = await memory_manager.recall("API design patterns", limit=20)
        post_ids = [m["id"] for m in post_compact.get("learnings", [])]

        # Summary should be visible
        assert summary_id in post_ids

        # Originals should not be visible
        for orig in created:
            assert orig["id"] not in post_ids

    @pytest.mark.asyncio
    async def test_compaction_atomicity_on_error(self, memory_manager):
        """Verify atomic rollback if something fails mid-transaction."""
        # Create a memory
        mem = await memory_manager.remember(
            category="learning",
            content="Test memory for atomicity verification in compaction",
            project_path="/test",
        )

        # Attempt compaction with invalid parameters (will fail validation)
        result = await memory_manager.compact_memories(
            summary="short",  # Too short - fails validation
            limit=10,
        )
        assert "error" in result

        # Original memory should still be visible (not archived)
        recall_result = await memory_manager.recall("atomicity", limit=10)
        found_ids = [m["id"] for m in recall_result.get("learnings", [])]
        assert mem["id"] in found_ids
