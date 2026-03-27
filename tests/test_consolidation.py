"""Tests for episodic-to-semantic memory consolidation."""

from unittest.mock import AsyncMock

import pytest

from daem0nmcp.reflexion.consolidation import (
    DEFAULT_CONSOLIDATION_THRESHOLD,
    check_and_consolidate,
    consolidate_reflections,
    extract_common_elements,
    identify_pattern_type,
)


@pytest.fixture
def mock_memory_manager():
    """Create a mock MemoryManager with link_memories."""
    manager = AsyncMock()
    manager.remember = AsyncMock(return_value={"id": 100})
    manager.recall = AsyncMock(return_value={"memories": []})
    manager.link_memories = AsyncMock()
    return manager


class TestExtractCommonElements:
    """Tests for common element extraction."""

    def test_extract_from_similar_content(self):
        """Should extract common words from similar content."""
        contents = [
            "Error with database connection timeout",
            "Database connection timed out",
            "Connection to database failed, timeout",
            "Timeout error on database connection",
            "Database timeout during connection",
        ]

        common = extract_common_elements(contents)

        assert "database" in common.lower()
        assert "connection" in common.lower() or "timeout" in common.lower()

    def test_extract_empty_returns_empty(self):
        """Empty input should return empty string."""
        assert extract_common_elements([]) == ""

    def test_extract_single_content(self):
        """Single content should return truncated content."""
        contents = ["This is a single reflection about an error"]
        common = extract_common_elements(contents)
        assert len(common) > 0

    def test_filters_stop_words(self):
        """Should filter common stop words."""
        contents = [
            "The error is in the database",
            "An error was in a database",
            "Error in the database",
        ]
        common = extract_common_elements(contents)
        # Stop words like "the", "is", "in", "a", "an" should be filtered
        assert "the" not in common.lower().split()
        assert "error" in common.lower() or "database" in common.lower()


class TestIdentifyPatternType:
    """Tests for pattern type identification."""

    def test_identify_conflict_pattern(self):
        """Should identify conflict as primary pattern type."""
        reflections = [
            {"tags": ["reflection", "conflict"]},
            {"tags": ["reflection", "conflict"]},
            {"tags": ["reflection", "factual_error"]},
        ]

        pattern_type = identify_pattern_type(reflections)
        assert pattern_type == "conflict"

    def test_identify_general_when_no_type(self):
        """Should return 'general' when no error types found."""
        reflections = [
            {"tags": ["reflection"]},
            {"tags": ["reflection"]},
        ]

        pattern_type = identify_pattern_type(reflections)
        assert pattern_type == "general"

    def test_identify_factual_error_pattern(self):
        """Should identify factual_error as pattern type."""
        reflections = [
            {"tags": ["reflection", "factual_error"]},
            {"tags": ["reflection", "factual_error"]},
            {"tags": ["reflection", "conflict"]},
            {"tags": ["reflection", "factual_error"]},
        ]

        pattern_type = identify_pattern_type(reflections)
        assert pattern_type == "factual_error"

    def test_empty_reflections_return_general(self):
        """Empty list should return 'general'."""
        pattern_type = identify_pattern_type([])
        assert pattern_type == "general"


class TestConsolidateReflections:
    """Tests for consolidate_reflections function."""

    @pytest.mark.asyncio
    async def test_consolidate_threshold(self, mock_memory_manager):
        """Should consolidate when threshold is met."""
        # Mock 5+ reflections with same signature
        mock_memory_manager.recall.return_value = {
            "memories": [
                {
                    "id": i,
                    "content": f"Reflection {i} about database error",
                    "tags": ["reflection", "conflict", "sig:abc123"],
                }
                for i in range(1, 7)  # 6 reflections
            ]
        }

        pattern_id = await consolidate_reflections(
            error_signature="abc123",
            memory_manager=mock_memory_manager,
            consolidation_threshold=5,
        )

        assert pattern_id == 100
        mock_memory_manager.remember.assert_called_once()

        # Check pattern memory was created with correct category
        call_args = mock_memory_manager.remember.call_args
        assert call_args.kwargs["category"] == "pattern"
        assert "learned-pattern" in call_args.kwargs["tags"]
        assert "consolidated" in call_args.kwargs["tags"]

    @pytest.mark.asyncio
    async def test_no_consolidate_below_threshold(self, mock_memory_manager):
        """Should NOT consolidate when below threshold."""
        # Mock only 3 reflections
        mock_memory_manager.recall.return_value = {
            "memories": [
                {"id": i, "content": f"Reflection {i}", "tags": ["reflection"]}
                for i in range(1, 4)  # 3 reflections
            ]
        }

        pattern_id = await consolidate_reflections(
            error_signature="abc123",
            memory_manager=mock_memory_manager,
            consolidation_threshold=5,
        )

        assert pattern_id is None
        mock_memory_manager.remember.assert_not_called()

    @pytest.mark.asyncio
    async def test_consolidate_links_supersedes(self, mock_memory_manager):
        """Should link pattern to source reflections via supersedes."""
        mock_memory_manager.recall.return_value = {
            "memories": [
                {
                    "id": i,
                    "content": f"Reflection {i}",
                    "tags": ["reflection", "sig:abc123"],
                }
                for i in range(1, 6)
            ]
        }

        await consolidate_reflections(
            error_signature="abc123",
            memory_manager=mock_memory_manager,
            consolidation_threshold=5,
        )

        # Should have called link_memories for each source reflection
        assert mock_memory_manager.link_memories.call_count == 5

        # Check supersedes relationship
        for call in mock_memory_manager.link_memories.call_args_list:
            assert call.kwargs["relationship"] == "supersedes"
            assert call.kwargs["source_id"] == 100  # Pattern ID

    @pytest.mark.asyncio
    async def test_consolidate_without_link_memories(self, mock_memory_manager):
        """Should work even if link_memories not available."""
        # Remove link_memories method
        del mock_memory_manager.link_memories

        mock_memory_manager.recall.return_value = {
            "memories": [
                {
                    "id": i,
                    "content": f"Reflection {i}",
                    "tags": ["reflection", "sig:abc123"],
                }
                for i in range(1, 6)
            ]
        }

        pattern_id = await consolidate_reflections(
            error_signature="abc123",
            memory_manager=mock_memory_manager,
            consolidation_threshold=5,
        )

        # Should still create pattern, just without links
        assert pattern_id == 100

    @pytest.mark.asyncio
    async def test_consolidate_includes_signature_in_pattern(self, mock_memory_manager):
        """Pattern should include error signature in tags."""
        mock_memory_manager.recall.return_value = {
            "memories": [
                {
                    "id": i,
                    "content": f"Reflection {i}",
                    "tags": ["reflection", "sig:xyz789"],
                }
                for i in range(1, 6)
            ]
        }

        await consolidate_reflections(
            error_signature="xyz789",
            memory_manager=mock_memory_manager,
            consolidation_threshold=5,
        )

        call_args = mock_memory_manager.remember.call_args
        assert "sig:xyz789" in call_args.kwargs["tags"]

    @pytest.mark.asyncio
    async def test_consolidate_handles_recall_error(self, mock_memory_manager):
        """Should return None if recall fails."""
        mock_memory_manager.recall.side_effect = Exception("Database error")

        pattern_id = await consolidate_reflections(
            error_signature="abc123",
            memory_manager=mock_memory_manager,
        )

        assert pattern_id is None

    @pytest.mark.asyncio
    async def test_consolidate_handles_remember_error(self, mock_memory_manager):
        """Should return None if remember fails."""
        mock_memory_manager.recall.return_value = {
            "memories": [
                {
                    "id": i,
                    "content": f"Reflection {i}",
                    "tags": ["reflection", "sig:abc123"],
                }
                for i in range(1, 6)
            ]
        }
        mock_memory_manager.remember.side_effect = Exception("Storage error")

        pattern_id = await consolidate_reflections(
            error_signature="abc123",
            memory_manager=mock_memory_manager,
            consolidation_threshold=5,
        )

        assert pattern_id is None


class TestCheckAndConsolidate:
    """Tests for check_and_consolidate function."""

    @pytest.mark.asyncio
    async def test_consolidates_multiple_signatures(self, mock_memory_manager):
        """Should consolidate all signatures that meet threshold."""
        # First call: get all reflections
        # Second call: check for existing pattern (none)
        # Third call: consolidate sig:aaa
        # etc.
        call_count = [0]

        async def mock_recall(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # All reflections
                return {
                    "memories": [
                        {
                            "id": i,
                            "content": f"Reflection {i}",
                            "tags": ["reflection", "conflict", "sig:aaa"],
                        }
                        for i in range(1, 6)
                    ]
                    + [
                        {
                            "id": i + 10,
                            "content": f"Reflection {i}",
                            "tags": ["reflection", "factual_error", "sig:bbb"],
                        }
                        for i in range(1, 6)
                    ]
                }
            elif "pattern" in kwargs.get("categories", []):
                # Check for existing pattern
                return {"memories": []}
            else:
                # Consolidation recall
                return {
                    "memories": [
                        {
                            "id": i,
                            "content": f"Reflection {i}",
                            "tags": [
                                "reflection",
                                f"sig:{kwargs.get('tags', ['sig:unknown'])[1][4:]}",
                            ],
                        }
                        for i in range(1, 6)
                    ]
                }

        mock_memory_manager.recall = AsyncMock(side_effect=mock_recall)

        pattern_ids = await check_and_consolidate(
            memory_manager=mock_memory_manager,
            consolidation_threshold=5,
        )

        # Should create patterns for both signatures
        assert len(pattern_ids) == 2

    @pytest.mark.asyncio
    async def test_skips_already_consolidated(self, mock_memory_manager):
        """Should skip signatures that are already consolidated."""
        call_count = [0]

        async def mock_recall(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # All reflections with sig:aaa
                return {
                    "memories": [
                        {
                            "id": i,
                            "content": f"Reflection {i}",
                            "tags": ["reflection", "sig:aaa"],
                        }
                        for i in range(1, 6)
                    ]
                }
            elif "pattern" in kwargs.get("categories", []):
                # Return existing pattern - already consolidated
                return {"memories": [{"id": 50, "content": "Existing pattern"}]}
            else:
                return {"memories": []}

        mock_memory_manager.recall = AsyncMock(side_effect=mock_recall)

        pattern_ids = await check_and_consolidate(
            memory_manager=mock_memory_manager,
            consolidation_threshold=5,
        )

        # Should not create any new patterns
        assert len(pattern_ids) == 0
        mock_memory_manager.remember.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_below_threshold(self, mock_memory_manager):
        """Should ignore signatures below threshold."""
        mock_memory_manager.recall.return_value = {
            "memories": [
                {
                    "id": i,
                    "content": f"Reflection {i}",
                    "tags": ["reflection", "sig:aaa"],
                }
                for i in range(1, 4)  # Only 3 reflections, below threshold
            ]
        }

        pattern_ids = await check_and_consolidate(
            memory_manager=mock_memory_manager,
            consolidation_threshold=5,
        )

        assert len(pattern_ids) == 0
        mock_memory_manager.remember.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_recall_error(self, mock_memory_manager):
        """Should return empty list if initial recall fails."""
        mock_memory_manager.recall.side_effect = Exception("Database error")

        pattern_ids = await check_and_consolidate(
            memory_manager=mock_memory_manager,
        )

        assert pattern_ids == []


class TestDefaultThreshold:
    """Tests for default consolidation threshold."""

    def test_default_threshold_is_five(self):
        """Default threshold should be 5 per CONTEXT.md."""
        assert DEFAULT_CONSOLIDATION_THRESHOLD == 5
