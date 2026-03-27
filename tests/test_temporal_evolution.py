"""Tests for knowledge evolution tracking."""

import shutil
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from daem0nmcp.models import ExtractedEntity, MemoryEntityRef, MemoryVersion


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


class TestTraceKnowledgeEvolution:
    """Test the trace_knowledge_evolution function."""

    @pytest.mark.asyncio
    async def test_entity_not_found(self, db_manager):
        """Returns error when entity doesn't exist."""
        from daem0nmcp.graph.temporal import trace_knowledge_evolution

        async with db_manager.get_session() as session:
            result = await trace_knowledge_evolution(
                session=session,
                entity_id=99999,
                include_invalidated=True,
            )

        assert result["found"] is False
        assert "not found" in result["error"]
        assert result["timeline"] == []
        assert result["current_beliefs"] == []

    @pytest.mark.asyncio
    async def test_entity_with_no_memories(self, db_manager):
        """Returns empty timeline when entity has no memory references."""
        from daem0nmcp.graph.temporal import trace_knowledge_evolution

        async with db_manager.get_session() as session:
            # Create an entity with no memory references
            entity = ExtractedEntity(
                project_path="/test",
                entity_type="concept",
                name="TestConcept",
            )
            session.add(entity)
            await session.flush()
            entity_id = entity.id

            result = await trace_knowledge_evolution(
                session=session,
                entity_id=entity_id,
                include_invalidated=True,
            )

        assert result["found"] is True
        assert result["entity"]["name"] == "TestConcept"
        assert result["timeline"] == []
        assert result["current_beliefs"] == []
        assert "No memories reference this entity" in result.get("message", "")

    @pytest.mark.asyncio
    async def test_basic_evolution_timeline(self, db_manager):
        """Traces evolution of entity through multiple memory versions."""
        from daem0nmcp.graph.temporal import trace_knowledge_evolution
        from daem0nmcp.models import Memory

        async with db_manager.get_session() as session:
            # Create entity
            entity = ExtractedEntity(
                project_path="/test",
                entity_type="class",
                name="UserService",
            )
            session.add(entity)
            await session.flush()

            # Create memory
            memory = Memory(
                category="decision",
                content="UserService should use JWT authentication",
                rationale="Security best practice",
            )
            session.add(memory)
            await session.flush()

            # Link memory to entity
            ref = MemoryEntityRef(
                memory_id=memory.id,
                entity_id=entity.id,
                relationship="about",
            )
            session.add(ref)

            # Create version 1
            now = datetime.now(timezone.utc)
            version1 = MemoryVersion(
                memory_id=memory.id,
                version_number=1,
                content="UserService should use JWT authentication",
                rationale="Security best practice",
                context={},
                tags=[],
                change_type="created",
                changed_at=now,
                valid_from=now,
                valid_to=None,
            )
            session.add(version1)
            await session.flush()

            result = await trace_knowledge_evolution(
                session=session,
                entity_id=entity.id,
                include_invalidated=True,
            )

        assert result["found"] is True
        assert result["entity"]["name"] == "UserService"
        assert result["total_versions"] == 1
        assert len(result["timeline"]) == 1
        assert len(result["current_beliefs"]) == 1

        entry = result["timeline"][0]
        assert entry["memory_id"] == memory.id
        assert entry["version_number"] == 1
        assert entry["is_current"] is True
        assert entry["valid_from"] is not None
        assert entry["valid_to"] is None
        assert entry["transaction_time"] is not None

    @pytest.mark.asyncio
    async def test_evolution_with_invalidation(self, db_manager):
        """Tracks invalidation chain when facts are superseded."""
        from daem0nmcp.graph.temporal import trace_knowledge_evolution
        from daem0nmcp.models import Memory

        async with db_manager.get_session() as session:
            # Create entity
            entity = ExtractedEntity(
                project_path="/test",
                entity_type="concept",
                name="auth_strategy",
            )
            session.add(entity)
            await session.flush()

            # Create memory
            memory = Memory(
                category="decision",
                content="Use session-based auth",
            )
            session.add(memory)
            await session.flush()

            # Link memory to entity
            ref = MemoryEntityRef(
                memory_id=memory.id,
                entity_id=entity.id,
                relationship="about",
            )
            session.add(ref)

            # Create version 1 (old belief, now invalidated)
            now = datetime.now(timezone.utc)
            version1 = MemoryVersion(
                memory_id=memory.id,
                version_number=1,
                content="Use session-based auth",
                context={},
                tags=[],
                change_type="created",
                changed_at=now - timedelta(days=7),
                valid_from=now - timedelta(days=7),
                valid_to=now,  # Invalidated now
            )
            session.add(version1)
            await session.flush()
            version1_id = version1.id

            # Create version 2 (new belief that invalidated version 1)
            version2 = MemoryVersion(
                memory_id=memory.id,
                version_number=2,
                content="Use JWT-based auth instead",
                context={},
                tags=[],
                change_type="content_updated",
                changed_at=now,
                valid_from=now,
                valid_to=None,  # Currently valid
            )
            session.add(version2)
            await session.flush()

            # Update version 1 to point to its invalidator
            version1.invalidated_by_version_id = version2.id

            result = await trace_knowledge_evolution(
                session=session,
                entity_id=entity.id,
                include_invalidated=True,
            )

        assert result["found"] is True
        assert result["total_versions"] == 2
        assert result["invalidated_count"] == 1

        # Timeline should have both versions
        assert len(result["timeline"]) == 2

        # Only version 2 should be in current beliefs
        assert len(result["current_beliefs"]) == 1
        assert result["current_beliefs"][0]["version_number"] == 2

        # Invalidation chain should show version 1 was invalidated by version 2
        assert len(result["invalidation_chain"]) == 1
        invalidation = result["invalidation_chain"][0]
        assert invalidation["invalidated_version_id"] == version1_id
        assert invalidation["invalidated_by_version_id"] == version2.id

    @pytest.mark.asyncio
    async def test_exclude_invalidated_versions(self, db_manager):
        """Can filter out invalidated versions from timeline."""
        from daem0nmcp.graph.temporal import trace_knowledge_evolution
        from daem0nmcp.models import Memory

        async with db_manager.get_session() as session:
            # Create entity
            entity = ExtractedEntity(
                project_path="/test",
                entity_type="concept",
                name="db_choice",
            )
            session.add(entity)
            await session.flush()

            # Create memory
            memory = Memory(
                category="decision",
                content="Use MySQL",
            )
            session.add(memory)
            await session.flush()

            # Link memory to entity
            ref = MemoryEntityRef(
                memory_id=memory.id,
                entity_id=entity.id,
                relationship="about",
            )
            session.add(ref)

            now = datetime.now(timezone.utc)

            # Create invalidated version
            version1 = MemoryVersion(
                memory_id=memory.id,
                version_number=1,
                content="Use MySQL",
                context={},
                tags=[],
                change_type="created",
                changed_at=now - timedelta(days=1),
                valid_from=now - timedelta(days=1),
                valid_to=now,  # Invalidated
            )
            session.add(version1)

            # Create current version
            version2 = MemoryVersion(
                memory_id=memory.id,
                version_number=2,
                content="Use PostgreSQL instead",
                context={},
                tags=[],
                change_type="content_updated",
                changed_at=now,
                valid_from=now,
                valid_to=None,  # Current
            )
            session.add(version2)
            await session.flush()

            # Query excluding invalidated
            result = await trace_knowledge_evolution(
                session=session,
                entity_id=entity.id,
                include_invalidated=False,
            )

        assert result["found"] is True
        # Should only have version 2
        assert result["total_versions"] == 1
        assert len(result["timeline"]) == 1
        assert result["timeline"][0]["version_number"] == 2


class TestGetMemoryEvolution:
    """Test the MemoryManager.get_memory_evolution method."""

    @pytest.mark.asyncio
    async def test_entity_not_found_by_name(self, memory_manager):
        """Returns error when entity name not found."""
        result = await memory_manager.get_memory_evolution(
            entity_name="NonExistentEntity"
        )

        assert result["found"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_lookup_entity_by_name(self, memory_manager):
        """Looks up entity by name and traces evolution."""
        from daem0nmcp.models import Memory

        async with memory_manager.db.get_session() as session:
            # Create entity
            entity = ExtractedEntity(
                project_path="/test",
                entity_type="class",
                name="PaymentService",
            )
            session.add(entity)
            await session.flush()

            # Create memory
            memory = Memory(
                category="pattern",
                content="PaymentService uses idempotency keys",
            )
            session.add(memory)
            await session.flush()

            # Link
            ref = MemoryEntityRef(
                memory_id=memory.id,
                entity_id=entity.id,
                relationship="about",
            )
            session.add(ref)

            # Create version
            now = datetime.now(timezone.utc)
            version = MemoryVersion(
                memory_id=memory.id,
                version_number=1,
                content="PaymentService uses idempotency keys",
                context={},
                tags=[],
                change_type="created",
                changed_at=now,
                valid_from=now,
            )
            session.add(version)
            await session.commit()

        result = await memory_manager.get_memory_evolution(entity_name="PaymentService")

        assert result["found"] is True
        assert result["entity"]["name"] == "PaymentService"
        assert result["entity"]["type"] == "class"
        assert len(result["timeline"]) == 1

    @pytest.mark.asyncio
    async def test_filter_by_entity_type(self, memory_manager):
        """Can filter entity lookup by type."""
        async with memory_manager.db.get_session() as session:
            # Create two entities with same name, different types
            entity1 = ExtractedEntity(
                project_path="/test",
                entity_type="class",
                name="Config",
            )
            entity2 = ExtractedEntity(
                project_path="/test",
                entity_type="module",
                name="Config",
            )
            session.add_all([entity1, entity2])
            await session.commit()

        # Should find the module, not the class
        result = await memory_manager.get_memory_evolution(
            entity_name="Config", entity_type="module"
        )

        assert result["found"] is True
        assert result["entity"]["type"] == "module"

    @pytest.mark.asyncio
    async def test_include_invalidated_parameter(self, memory_manager):
        """Respects include_invalidated parameter."""
        from daem0nmcp.models import Memory

        async with memory_manager.db.get_session() as session:
            # Create entity
            entity = ExtractedEntity(
                project_path="/test",
                entity_type="concept",
                name="cache_strategy",
            )
            session.add(entity)
            await session.flush()

            # Create memory
            memory = Memory(
                category="decision",
                content="Use Redis",
            )
            session.add(memory)
            await session.flush()

            # Link
            ref = MemoryEntityRef(
                memory_id=memory.id,
                entity_id=entity.id,
            )
            session.add(ref)

            now = datetime.now(timezone.utc)

            # Invalidated version
            v1 = MemoryVersion(
                memory_id=memory.id,
                version_number=1,
                content="Use Memcached",
                context={},
                tags=[],
                change_type="created",
                changed_at=now - timedelta(hours=1),
                valid_from=now - timedelta(hours=1),
                valid_to=now,
            )
            # Current version
            v2 = MemoryVersion(
                memory_id=memory.id,
                version_number=2,
                content="Use Redis instead",
                context={},
                tags=[],
                change_type="content_updated",
                changed_at=now,
                valid_from=now,
            )
            session.add_all([v1, v2])
            await session.commit()

        # With invalidated
        result_all = await memory_manager.get_memory_evolution(
            entity_name="cache_strategy", include_invalidated=True
        )
        assert result_all["total_versions"] == 2

        # Without invalidated
        result_current = await memory_manager.get_memory_evolution(
            entity_name="cache_strategy", include_invalidated=False
        )
        assert result_current["total_versions"] == 1


class TestEvolutionTimelineFormat:
    """Test the format of evolution timeline entries."""

    @pytest.mark.asyncio
    async def test_timeline_entry_has_all_temporal_fields(self, db_manager):
        """Each timeline entry includes all required bi-temporal fields."""
        from daem0nmcp.graph.temporal import trace_knowledge_evolution
        from daem0nmcp.models import Memory

        async with db_manager.get_session() as session:
            # Create entity
            entity = ExtractedEntity(
                project_path="/test",
                entity_type="function",
                name="process_payment",
            )
            session.add(entity)
            await session.flush()

            # Create memory
            memory = Memory(
                category="learning",
                content="process_payment needs retry logic",
                outcome="Worked after adding retries",
                worked=True,
            )
            session.add(memory)
            await session.flush()

            # Link
            ref = MemoryEntityRef(
                memory_id=memory.id,
                entity_id=entity.id,
            )
            session.add(ref)

            now = datetime.now(timezone.utc)
            version = MemoryVersion(
                memory_id=memory.id,
                version_number=1,
                content="process_payment needs retry logic",
                context={"files": ["payment.py"]},
                tags=["payment", "reliability"],
                change_type="created",
                change_description="Initial learning",
                changed_at=now,
                valid_from=now,
                outcome="Worked after adding retries",
                worked=True,
            )
            session.add(version)
            await session.flush()

            result = await trace_knowledge_evolution(
                session=session,
                entity_id=entity.id,
            )

        entry = result["timeline"][0]

        # All required temporal fields present
        assert "valid_from" in entry
        assert "valid_to" in entry
        assert "transaction_time" in entry
        assert "is_current" in entry
        assert "invalidated_by_version_id" in entry

        # Other expected fields
        assert "memory_id" in entry
        assert "version_id" in entry
        assert "version_number" in entry
        assert "content_preview" in entry
        assert "change_type" in entry
        assert "outcome" in entry
        assert "worked" in entry

        # Verify values
        assert entry["is_current"] is True
        assert entry["valid_to"] is None
        assert entry["outcome"] == "Worked after adding retries"
        assert entry["worked"] is True
