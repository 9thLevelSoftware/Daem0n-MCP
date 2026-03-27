# tests/test_surprise.py
"""Tests for surprise-based memory scoring."""

from daem0nmcp.models import Memory


class TestMemorySurpriseField:
    """Test Memory model has surprise_score field."""

    def test_memory_has_surprise_score(self):
        memory = Memory(
            category="decision", content="Test content", surprise_score=0.85
        )
        assert memory.surprise_score == 0.85

    def test_surprise_score_defaults_to_none(self):
        memory = Memory(category="decision", content="Test content")
        assert memory.surprise_score is None

    def test_surprise_score_accepts_float(self):
        memory = Memory(category="decision", content="Test", surprise_score=0.0)
        assert memory.surprise_score == 0.0
