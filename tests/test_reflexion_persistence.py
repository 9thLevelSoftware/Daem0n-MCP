"""Tests for reflection persistence in Metacognitive Architecture."""

from unittest.mock import AsyncMock

import pytest

from daem0nmcp.reflexion.persistence import (
    Reflection,
    compute_error_signature,
    create_reflection_from_evaluation,
    has_seen_error_before,
    persist_reflection,
    retrieve_similar_reflections,
)


@pytest.fixture
def mock_memory_manager():
    """Create a mock MemoryManager."""
    manager = AsyncMock()
    manager.remember = AsyncMock(return_value={"id": 42})
    manager.recall = AsyncMock(return_value={"memories": []})
    return manager


@pytest.fixture
def sample_reflection():
    """Create a sample Reflection for testing."""
    return Reflection(
        error_type="conflict",
        error_signature="abc123",
        content="Detected conflict with stored memory about PostgreSQL",
        context="Answering question about database choice",
        query="What database should we use?",
        iteration=2,
        quality_delta=0.15,
    )


class TestComputeErrorSignature:
    """Tests for error signature computation."""

    def test_same_content_same_signature(self):
        """Same content should produce same signature."""
        sig1 = compute_error_signature("conflict", "Same error message")
        sig2 = compute_error_signature("conflict", "Same error message")
        assert sig1 == sig2

    def test_different_content_different_signature(self):
        """Different content should produce different signatures."""
        sig1 = compute_error_signature("conflict", "Error message A")
        sig2 = compute_error_signature("conflict", "Error message B")
        assert sig1 != sig2

    def test_different_type_different_signature(self):
        """Different error type should produce different signatures."""
        sig1 = compute_error_signature("conflict", "Same message")
        sig2 = compute_error_signature("factual_error", "Same message")
        assert sig1 != sig2

    def test_case_insensitive(self):
        """Signatures should be case-insensitive."""
        sig1 = compute_error_signature("conflict", "Error Message")
        sig2 = compute_error_signature("conflict", "error message")
        assert sig1 == sig2

    def test_whitespace_normalized(self):
        """Signatures should normalize whitespace."""
        sig1 = compute_error_signature("conflict", "error message")
        sig2 = compute_error_signature("conflict", "  error message  ")
        assert sig1 == sig2

    def test_signature_length(self):
        """Signature should be 16 characters (truncated hash)."""
        sig = compute_error_signature("conflict", "Some error")
        assert len(sig) == 16
        # Should be hex characters
        assert all(c in "0123456789abcdef" for c in sig)


class TestPersistReflection:
    """Tests for persist_reflection function."""

    @pytest.mark.asyncio
    async def test_persist_behavior_changing_reflection(
        self, mock_memory_manager, sample_reflection
    ):
        """Reflection that changed behavior should be stored."""
        memory_id = await persist_reflection(
            reflection=sample_reflection,
            memory_manager=mock_memory_manager,
            changed_behavior=True,
        )

        assert memory_id == 42
        mock_memory_manager.remember.assert_called_once()

        # Check remember was called with correct args
        call_args = mock_memory_manager.remember.call_args
        assert call_args.kwargs["category"] == "reflection"
        assert "reflection" in call_args.kwargs["tags"]
        assert sample_reflection.error_type in call_args.kwargs["tags"]

    @pytest.mark.asyncio
    async def test_skip_non_behavior_changing_reflection(
        self, mock_memory_manager, sample_reflection
    ):
        """Reflection that didn't change behavior should NOT be stored."""
        memory_id = await persist_reflection(
            reflection=sample_reflection,
            memory_manager=mock_memory_manager,
            changed_behavior=False,
        )

        assert memory_id is None
        mock_memory_manager.remember.assert_not_called()

    @pytest.mark.asyncio
    async def test_deduplicate_similar_reflection(
        self, mock_memory_manager, sample_reflection
    ):
        """Similar existing reflection should deduplicate."""
        # Mock existing reflection found
        mock_memory_manager.recall.return_value = {
            "memories": [{"id": 1, "content": "Similar reflection"}]
        }

        memory_id = await persist_reflection(
            reflection=sample_reflection,
            memory_manager=mock_memory_manager,
            changed_behavior=True,
        )

        # Should not store - deduplicated
        assert memory_id is None
        mock_memory_manager.remember.assert_not_called()

    @pytest.mark.asyncio
    async def test_tags_include_error_signature(
        self, mock_memory_manager, sample_reflection
    ):
        """Stored reflection should include error signature in tags."""
        await persist_reflection(
            reflection=sample_reflection,
            memory_manager=mock_memory_manager,
            changed_behavior=True,
        )

        call_args = mock_memory_manager.remember.call_args
        tags = call_args.kwargs["tags"]
        assert f"sig:{sample_reflection.error_signature}" in tags

    @pytest.mark.asyncio
    async def test_context_includes_metadata(
        self, mock_memory_manager, sample_reflection
    ):
        """Stored reflection should include context metadata."""
        await persist_reflection(
            reflection=sample_reflection,
            memory_manager=mock_memory_manager,
            changed_behavior=True,
        )

        call_args = mock_memory_manager.remember.call_args
        context = call_args.kwargs["context"]
        assert context["error_signature"] == sample_reflection.error_signature
        assert context["iteration_count"] == sample_reflection.iteration
        assert context["quality_improvement"] == sample_reflection.quality_delta

    @pytest.mark.asyncio
    async def test_remember_failure_returns_none(
        self, mock_memory_manager, sample_reflection
    ):
        """If remember() fails, should return None gracefully."""
        mock_memory_manager.remember.side_effect = Exception("DB error")

        memory_id = await persist_reflection(
            reflection=sample_reflection,
            memory_manager=mock_memory_manager,
            changed_behavior=True,
        )

        assert memory_id is None


class TestRetrieveSimilarReflections:
    """Tests for retrieve_similar_reflections function."""

    @pytest.mark.asyncio
    async def test_retrieve_by_signature(self, mock_memory_manager):
        """Should retrieve reflections by error signature."""
        mock_memory_manager.recall.return_value = {
            "memories": [
                {"id": 1, "content": "Past reflection 1"},
                {"id": 2, "content": "Past reflection 2"},
            ]
        }

        reflections = await retrieve_similar_reflections(
            error_signature="abc123",
            memory_manager=mock_memory_manager,
            limit=5,
        )

        assert len(reflections) == 2
        mock_memory_manager.recall.assert_called_once()

    @pytest.mark.asyncio
    async def test_retrieve_empty_when_none(self, mock_memory_manager):
        """Should return empty list when no reflections found."""
        mock_memory_manager.recall.return_value = {"memories": []}

        reflections = await retrieve_similar_reflections(
            error_signature="nonexistent",
            memory_manager=mock_memory_manager,
        )

        assert reflections == []

    @pytest.mark.asyncio
    async def test_retrieve_with_error_type_filter(self, mock_memory_manager):
        """Should include error type in tags when specified."""
        await retrieve_similar_reflections(
            error_signature="abc123",
            memory_manager=mock_memory_manager,
            error_type="conflict",
        )

        call_args = mock_memory_manager.recall.call_args
        assert "conflict" in call_args.kwargs["tags"]

    @pytest.mark.asyncio
    async def test_retrieve_respects_limit(self, mock_memory_manager):
        """Should pass limit to recall."""
        await retrieve_similar_reflections(
            error_signature="abc123",
            memory_manager=mock_memory_manager,
            limit=3,
        )

        call_args = mock_memory_manager.recall.call_args
        assert call_args.kwargs["limit"] == 3

    @pytest.mark.asyncio
    async def test_recall_failure_returns_empty(self, mock_memory_manager):
        """If recall() fails, should return empty list gracefully."""
        mock_memory_manager.recall.side_effect = Exception("DB error")

        reflections = await retrieve_similar_reflections(
            error_signature="abc123",
            memory_manager=mock_memory_manager,
        )

        assert reflections == []


class TestHasSeenErrorBefore:
    """Tests for has_seen_error_before function."""

    @pytest.mark.asyncio
    async def test_returns_true_when_exists(self, mock_memory_manager):
        """Should return True when similar reflection exists."""
        mock_memory_manager.recall.return_value = {"memories": [{"id": 1}]}

        result = await has_seen_error_before(
            error_signature="abc123",
            memory_manager=mock_memory_manager,
        )

        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_when_not_exists(self, mock_memory_manager):
        """Should return False when no similar reflection."""
        mock_memory_manager.recall.return_value = {"memories": []}

        result = await has_seen_error_before(
            error_signature="abc123",
            memory_manager=mock_memory_manager,
        )

        assert result is False


class TestCreateReflectionFromEvaluation:
    """Tests for create_reflection_from_evaluation function."""

    def test_create_conflict_reflection(self):
        """Should create conflict reflection from verification results."""
        verification_results = [
            {
                "status": "conflict",
                "claim_text": "We use PostgreSQL",
                "conflict_reason": "Memory says SQLite",
            }
        ]

        reflection = create_reflection_from_evaluation(
            critique="CONFLICTS DETECTED",
            verification_results=verification_results,
            query="What database?",
            context="Database discussion",
            iteration=1,
            quality_before=0.5,
            quality_after=0.7,
        )

        assert reflection is not None
        assert reflection.error_type == "conflict"
        assert "conflict" in reflection.content.lower()

    def test_create_unverified_reflection(self):
        """Should create unverified claim reflection."""
        verification_results = [
            {
                "status": "unverified",
                "claim_text": "The API returns JSON",
            }
        ]

        reflection = create_reflection_from_evaluation(
            critique="UNVERIFIED CLAIMS",
            verification_results=verification_results,
            query="How does the API work?",
            context="API discussion",
            iteration=1,
            quality_before=0.6,
            quality_after=0.8,
        )

        assert reflection is not None
        assert reflection.error_type == "unverified_claim"

    def test_create_factual_error_reflection(self):
        """Should create factual error reflection from critique."""
        verification_results = [{"status": "verified", "claim_text": "Something"}]

        reflection = create_reflection_from_evaluation(
            critique="CONFLICTS DETECTED: said X but should be Y",
            verification_results=verification_results,
            query="What is X?",
            context="Explaining X",
            iteration=1,
            quality_before=0.4,
            quality_after=0.75,
        )

        assert reflection is not None
        assert reflection.error_type == "factual_error"

    def test_create_quality_improvement_reflection(self):
        """Should create quality improvement reflection when delta > 0.1."""
        verification_results = [{"status": "verified", "claim_text": "Something"}]

        reflection = create_reflection_from_evaluation(
            critique="Response was improved for clarity",
            verification_results=verification_results,
            query="Explain this",
            context="Explanation",
            iteration=1,
            quality_before=0.5,
            quality_after=0.75,  # delta = 0.25 > 0.1
        )

        assert reflection is not None
        assert reflection.error_type == "quality_improvement"
        assert "0.25" in reflection.content  # Quality delta in content

    def test_no_reflection_when_insignificant(self):
        """Should return None when nothing significant to learn."""
        verification_results = [{"status": "verified", "claim_text": "Python is used"}]

        reflection = create_reflection_from_evaluation(
            critique="No issues found",
            verification_results=verification_results,
            query="What language?",
            context="Language discussion",
            iteration=1,
            quality_before=0.8,
            quality_after=0.82,  # Minimal improvement
        )

        assert reflection is None

    def test_reflection_includes_query(self):
        """Created reflection should include original query."""
        verification_results = [
            {"status": "conflict", "claim_text": "X", "conflict_reason": "Y"}
        ]

        reflection = create_reflection_from_evaluation(
            critique="Issue",
            verification_results=verification_results,
            query="What is the config?",
            context="Config discussion",
            iteration=2,
            quality_before=0.5,
            quality_after=0.7,
        )

        assert reflection.query == "What is the config?"

    def test_reflection_includes_iteration(self):
        """Created reflection should track iteration number."""
        verification_results = [
            {"status": "conflict", "claim_text": "X", "conflict_reason": "Y"}
        ]

        reflection = create_reflection_from_evaluation(
            critique="Issue",
            verification_results=verification_results,
            query="Query",
            context="Context",
            iteration=3,
            quality_before=0.5,
            quality_after=0.7,
        )

        assert reflection.iteration == 3

    def test_quality_delta_computed(self):
        """Quality delta should be after - before."""
        verification_results = [
            {"status": "conflict", "claim_text": "X", "conflict_reason": "Y"}
        ]

        reflection = create_reflection_from_evaluation(
            critique="Issue",
            verification_results=verification_results,
            query="Query",
            context="Context",
            iteration=1,
            quality_before=0.4,
            quality_after=0.7,
        )

        assert reflection.quality_delta == pytest.approx(0.3)

    def test_conflict_takes_priority_over_unverified(self):
        """Conflicts should be prioritized over unverified claims."""
        verification_results = [
            {"status": "unverified", "claim_text": "Unverified claim"},
            {
                "status": "conflict",
                "claim_text": "Conflict claim",
                "conflict_reason": "Reason",
            },
        ]

        reflection = create_reflection_from_evaluation(
            critique="Issues found",
            verification_results=verification_results,
            query="Query",
            context="Context",
            iteration=1,
            quality_before=0.5,
            quality_after=0.7,
        )

        # Conflict should be detected first
        assert reflection.error_type == "conflict"
