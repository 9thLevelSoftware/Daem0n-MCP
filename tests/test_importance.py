# tests/test_importance.py
"""Tests for importance-weighted memory scoring."""

from daem0nmcp.models import Memory


class TestMemoryImportanceField:
    """Test Memory model has importance_score field."""

    def test_memory_has_importance_score(self):
        memory = Memory(
            category="decision", content="Test content", importance_score=0.9
        )
        assert memory.importance_score == 0.9

    def test_importance_score_defaults_to_none(self):
        memory = Memory(category="decision", content="Test content")
        assert memory.importance_score is None
