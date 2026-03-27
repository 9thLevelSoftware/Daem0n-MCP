# tests/test_fact_model.py
"""Tests for static Fact model (Engram-inspired)."""

from daem0nmcp.models import Fact


class TestFactModel:
    """Test Fact model for static knowledge."""

    def test_fact_creation(self):
        fact = Fact(
            content_hash="abc123",
            content="Python uses indentation for blocks",
            category="language",
            source_memory_id=1,
        )
        assert fact.content_hash == "abc123"
        assert fact.content == "Python uses indentation for blocks"

    def test_fact_has_verification_count(self):
        fact = Fact(content_hash="abc123", content="Test", verification_count=5)
        assert fact.verification_count == 5

    def test_fact_defaults(self):
        fact = Fact(content_hash="abc123", content="Test")
        assert fact.verification_count is None or fact.verification_count == 0
        assert fact.is_verified is None or fact.is_verified is False
