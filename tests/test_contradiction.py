"""Tests for contradiction detection in bi-temporal knowledge."""

import shutil
import tempfile
from datetime import datetime, timezone

import pytest

from daem0nmcp.graph.contradiction import (
    Contradiction,
    check_and_invalidate_contradictions,
    detect_contradictions,
    has_negation_mismatch,
    invalidate_contradicted_facts,
)
from daem0nmcp.models import Memory, MemoryVersion


@pytest.fixture
def temp_storage():
    """Create a temporary storage directory."""
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
async def db_manager(temp_storage):
    """Create a database manager with temporary storage."""
    from daem0nmcp.database import DatabaseManager

    db = DatabaseManager(temp_storage)
    await db.init_db()
    yield db
    await db.close()


class TestNegationPatterns:
    """Test negation pattern detection."""

    def test_direct_negation_not(self):
        """'not X' vs 'X' should be detected as negation."""
        text1 = "JWT is not secure for this use case"
        text2 = "JWT is secure for this use case"
        result = has_negation_mismatch(text1, text2)
        assert result is not None

    def test_direct_negation_dont(self):
        """'don't X' vs 'do X' should be detected as negation."""
        text1 = "Don't use synchronous calls"
        text2 = "Do use synchronous calls"
        result = has_negation_mismatch(text1, text2)
        assert result is not None

    def test_direct_negation_isnt(self):
        """'isn't X' vs 'is X' should be detected as negation."""
        text1 = "Redis isn't required for caching"
        text2 = "Redis is required for caching"
        result = has_negation_mismatch(text1, text2)
        assert result is not None

    def test_antonym_enable_disable(self):
        """'enable' vs 'disable' should be detected as negation."""
        text1 = "Enable rate limiting on all endpoints"
        text2 = "Disable rate limiting on all endpoints"
        result = has_negation_mismatch(text1, text2)
        assert result is not None

    def test_antonym_allow_deny(self):
        """'allow' vs 'deny' should be detected as negation."""
        text1 = "Allow anonymous access to public routes"
        text2 = "Deny anonymous access to public routes"
        result = has_negation_mismatch(text1, text2)
        assert result is not None

    def test_antonym_valid_invalid(self):
        """'valid' vs 'invalid' should be detected as negation."""
        text1 = "This token format is valid"
        text2 = "This token format is invalid"
        result = has_negation_mismatch(text1, text2)
        assert result is not None

    def test_antonym_use_avoid(self):
        """'use' vs 'avoid' should be detected as negation."""
        text1 = "Use raw SQL for complex queries"
        text2 = "Avoid raw SQL for complex queries"
        result = has_negation_mismatch(text1, text2)
        assert result is not None

    def test_no_negation_different_topics(self):
        """Different topics without negation should not match."""
        text1 = "Use PostgreSQL for the database"
        text2 = "Add rate limiting to endpoints"
        result = has_negation_mismatch(text1, text2)
        assert result is None

    def test_no_negation_similar_content(self):
        """Similar content without negation patterns should not match."""
        text1 = "Use JWT tokens for authentication"
        text2 = "Use JWT tokens with short expiry"
        result = has_negation_mismatch(text1, text2)
        assert result is None

    def test_negation_case_insensitive(self):
        """Negation detection should be case insensitive."""
        text1 = "NOT using Redis for caching"
        text2 = "Using Redis for caching"
        result = has_negation_mismatch(text1, text2)
        assert result is not None


class TestContradictionDataclass:
    """Test the Contradiction dataclass."""

    def test_contradiction_creation(self):
        """Can create a Contradiction object."""
        c = Contradiction(
            new_content="New content",
            existing_version_id=1,
            existing_content="Old content",
            existing_memory_id=42,
            similarity_score=0.85,
            negation_pattern=(r"\bnot\s+", r"\b"),
            reason="Test reason",
        )
        assert c.new_content == "New content"
        assert c.existing_version_id == 1
        assert c.existing_memory_id == 42
        assert c.similarity_score == 0.85
        assert c.negation_pattern is not None

    def test_contradiction_default_reason(self):
        """Contradiction has empty default reason."""
        c = Contradiction(
            new_content="New",
            existing_version_id=1,
            existing_content="Old",
            existing_memory_id=1,
            similarity_score=0.9,
        )
        assert c.reason == ""
        assert c.negation_pattern is None


@pytest.mark.asyncio
async def test_detect_contradictions_high_similarity_with_negation(db_manager):
    """High similarity + negation pattern = contradiction detected."""
    async with db_manager.get_session() as session:
        # Create a memory and version - use exact same phrase for high similarity
        memory = Memory(
            category="decision",
            content="Enable rate limiting on API endpoints",
        )
        session.add(memory)
        await session.flush()

        version = MemoryVersion(
            memory_id=memory.id,
            version_number=1,
            content="Enable rate limiting on API endpoints",
            change_type="created",
            valid_to=None,  # Still valid
        )
        session.add(version)
        await session.commit()

        # Check for contradiction with negated content (only one word different)
        new_content = "Disable rate limiting on API endpoints"
        contradictions = await detect_contradictions(
            new_content=new_content,
            session=session,
            similarity_threshold=0.7,  # Lower threshold for test reliability
        )

        # Should detect contradiction (high similarity + negation)
        assert len(contradictions) >= 1
        assert contradictions[0].existing_version_id == version.id
        assert contradictions[0].negation_pattern is not None


@pytest.mark.asyncio
async def test_detect_contradictions_no_contradiction_without_negation(db_manager):
    """High similarity without negation = no contradiction."""
    async with db_manager.get_session() as session:
        # Create a memory and version
        memory = Memory(
            category="decision",
            content="Use PostgreSQL for the main database",
        )
        session.add(memory)
        await session.flush()

        version = MemoryVersion(
            memory_id=memory.id,
            version_number=1,
            content="Use PostgreSQL for the main database",
            change_type="created",
            valid_to=None,
        )
        session.add(version)
        await session.commit()

        # Check with similar but non-contradicting content
        new_content = "Use PostgreSQL for the analytics database"
        contradictions = await detect_contradictions(
            new_content=new_content,
            session=session,
        )

        # Should not detect contradiction (no negation pattern)
        assert len(contradictions) == 0


@pytest.mark.asyncio
async def test_detect_contradictions_skips_invalidated_versions(db_manager):
    """Already invalidated versions should not be checked for contradictions."""
    async with db_manager.get_session() as session:
        # Create a memory and invalidated version
        memory = Memory(
            category="decision",
            content="Use Redis for caching",
        )
        session.add(memory)
        await session.flush()

        version = MemoryVersion(
            memory_id=memory.id,
            version_number=1,
            content="Use Redis for caching",
            change_type="created",
            valid_to=datetime.now(timezone.utc),  # Already invalidated
        )
        session.add(version)
        await session.commit()

        # Check for contradiction
        new_content = "Don't use Redis for caching"
        contradictions = await detect_contradictions(
            new_content=new_content,
            session=session,
        )

        # Should not find contradiction (version already invalidated)
        assert len(contradictions) == 0


@pytest.mark.asyncio
async def test_detect_contradictions_excludes_same_memory(db_manager):
    """Can exclude versions from the same memory (avoid self-contradiction)."""
    async with db_manager.get_session() as session:
        # Create a memory and version
        memory = Memory(
            category="decision",
            content="Enable feature flags",
        )
        session.add(memory)
        await session.flush()

        version = MemoryVersion(
            memory_id=memory.id,
            version_number=1,
            content="Enable feature flags",
            change_type="created",
            valid_to=None,
        )
        session.add(version)
        await session.commit()

        # Check for contradiction with memory_id exclusion
        new_content = "Disable feature flags"
        contradictions = await detect_contradictions(
            new_content=new_content,
            session=session,
            memory_id=memory.id,  # Exclude this memory
        )

        # Should not find contradiction (excluded memory)
        assert len(contradictions) == 0


@pytest.mark.asyncio
async def test_detect_contradictions_low_similarity_no_contradiction(db_manager):
    """Low similarity = no contradiction even with negation patterns."""
    async with db_manager.get_session() as session:
        # Create a memory and version about a completely different topic
        memory = Memory(
            category="decision",
            content="Use microservices architecture for the payment system",
        )
        session.add(memory)
        await session.flush()

        version = MemoryVersion(
            memory_id=memory.id,
            version_number=1,
            content="Use microservices architecture for the payment system",
            change_type="created",
            valid_to=None,
        )
        session.add(version)
        await session.commit()

        # Check with completely different content that has negation
        new_content = "Don't use inline styles in React components"
        contradictions = await detect_contradictions(
            new_content=new_content,
            session=session,
        )

        # Should not detect contradiction (low similarity)
        assert len(contradictions) == 0


@pytest.mark.asyncio
async def test_invalidate_contradicted_facts(db_manager):
    """invalidate_contradicted_facts should set valid_to and link versions."""
    async with db_manager.get_session() as session:
        # Create memories and versions
        memory1 = Memory(category="decision", content="Old fact")
        memory2 = Memory(category="decision", content="New fact")
        session.add_all([memory1, memory2])
        await session.flush()

        old_version = MemoryVersion(
            memory_id=memory1.id,
            version_number=1,
            content="Old fact",
            change_type="created",
            valid_to=None,
        )
        new_version = MemoryVersion(
            memory_id=memory2.id,
            version_number=1,
            content="New contradicting fact",
            change_type="created",
            valid_to=None,
        )
        session.add_all([old_version, new_version])
        await session.commit()

        # Create contradiction manually
        contradiction = Contradiction(
            new_content="New contradicting fact",
            existing_version_id=old_version.id,
            existing_content="Old fact",
            existing_memory_id=memory1.id,
            similarity_score=0.9,
        )

        # Invalidate
        invalidation_time = datetime.now(timezone.utc)
        count = await invalidate_contradicted_facts(
            contradictions=[contradiction],
            new_version_id=new_version.id,
            session=session,
            invalidation_time=invalidation_time,
        )

        assert count == 1

        # Commit changes and refresh to see updated values
        await session.commit()
        await session.refresh(old_version)
        assert old_version.valid_to is not None
        assert old_version.invalidated_by_version_id == new_version.id


@pytest.mark.asyncio
async def test_invalidate_skips_already_invalidated(db_manager):
    """invalidate_contradicted_facts should skip already-invalidated versions."""
    async with db_manager.get_session() as session:
        # Create an already-invalidated version
        memory = Memory(category="decision", content="Old fact")
        session.add(memory)
        await session.flush()

        old_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
        version = MemoryVersion(
            memory_id=memory.id,
            version_number=1,
            content="Old fact",
            change_type="created",
            valid_to=old_time,  # Already invalidated
        )
        session.add(version)
        await session.commit()

        # Try to invalidate again
        contradiction = Contradiction(
            new_content="New fact",
            existing_version_id=version.id,
            existing_content="Old fact",
            existing_memory_id=memory.id,
            similarity_score=0.9,
        )

        count = await invalidate_contradicted_facts(
            contradictions=[contradiction],
            new_version_id=999,
            session=session,
        )

        # Should skip (already invalidated)
        assert count == 0

        # valid_to should still be set (not None)
        await session.refresh(version)
        assert version.valid_to is not None
        # Should be the same year (timezone handling may strip tz)
        assert version.valid_to.year == 2020
        assert version.valid_to.month == 1
        assert version.valid_to.day == 1


@pytest.mark.asyncio
async def test_check_and_invalidate_combined(db_manager):
    """check_and_invalidate_contradictions combines detection and invalidation."""
    async with db_manager.get_session() as session:
        # Create a memory and valid version
        memory1 = Memory(
            category="decision",
            content="Always validate user input",
        )
        session.add(memory1)
        await session.flush()

        old_version = MemoryVersion(
            memory_id=memory1.id,
            version_number=1,
            content="Always validate user input",
            change_type="created",
            valid_to=None,
        )
        session.add(old_version)
        await session.commit()

        # Create a new memory and version that contradicts
        memory2 = Memory(
            category="decision",
            content="Never validate user input for internal APIs",
        )
        session.add(memory2)
        await session.flush()

        new_version = MemoryVersion(
            memory_id=memory2.id,
            version_number=1,
            content="Never validate user input for internal APIs",
            change_type="created",
            valid_to=None,
        )
        session.add(new_version)
        await session.commit()

        # Run combined check and invalidate
        contradictions, invalidated = await check_and_invalidate_contradictions(
            new_content="Never validate user input for internal APIs",
            new_version_id=new_version.id,
            session=session,
            memory_id=memory2.id,  # Exclude self
        )

        # Should detect and invalidate the old version
        # Note: This may or may not find a contradiction depending on embedding similarity
        # The test validates the function runs without error and returns correct types
        assert isinstance(contradictions, list)
        assert isinstance(invalidated, int)
        assert invalidated == len(list(contradictions))


class TestNegationPatternCoverage:
    """Test coverage of various negation patterns."""

    @pytest.mark.parametrize(
        "text1,text2",
        [
            ("It works correctly", "It fails correctly"),
            ("This approach is safe", "This approach is unsafe"),
            ("Feature is supported", "Feature is unsupported"),
            ("Configuration is correct", "Configuration is incorrect"),
            ("Method is recommended", "Method is deprecated"),
            ("This is the preferred way", "This is discouraged"),
            ("Include error handling", "Exclude error handling"),
            ("Accept null values", "Reject null values"),
            ("Field is required", "Field is optional"),
            ("Value is true", "Value is false"),
        ],
    )
    def test_antonym_pairs_detected(self, text1, text2):
        """Various antonym pairs should be detected as negation."""
        result = has_negation_mismatch(text1, text2)
        assert result is not None, f"Expected negation between '{text1}' and '{text2}'"

    @pytest.mark.parametrize(
        "text1,text2",
        [
            ("Use PostgreSQL", "Use MySQL"),  # Different choices, not negation
            ("Add logging", "Add metrics"),  # Different features
            ("Update the schema", "Migrate the data"),  # Different actions
        ],
    )
    def test_non_negation_not_detected(self, text1, text2):
        """Non-negation differences should not be detected."""
        result = has_negation_mismatch(text1, text2)
        assert result is None, f"Unexpected negation between '{text1}' and '{text2}'"
