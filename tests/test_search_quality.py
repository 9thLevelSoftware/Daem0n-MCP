"""Tests for Iteration 1: Search Quality enhancements."""

import pytest
from pydantic import ValidationError

from daem0nmcp.config import Settings


class TestSearchConfig:
    """Test search configuration settings."""

    def test_default_hybrid_weight(self):
        """Default hybrid weight is 0.3."""
        settings = Settings()
        assert settings.hybrid_vector_weight == 0.3

    def test_default_diversity_setting(self):
        """Default max_per_file is 3."""
        settings = Settings()
        assert settings.search_diversity_max_per_file == 3

    def test_hybrid_weight_from_env(self, monkeypatch):
        """Hybrid weight can be set via environment."""
        monkeypatch.setenv("DAEM0NMCP_HYBRID_VECTOR_WEIGHT", "0.5")
        settings = Settings()
        assert settings.hybrid_vector_weight == 0.5

    def test_diversity_from_env(self, monkeypatch):
        """Diversity limit can be set via environment."""
        monkeypatch.setenv("DAEM0NMCP_SEARCH_DIVERSITY_MAX_PER_FILE", "5")
        settings = Settings()
        assert settings.search_diversity_max_per_file == 5

    def test_invalid_hybrid_weight_rejected(self):
        """Invalid hybrid weight should raise ValidationError."""
        with pytest.raises(ValidationError):
            Settings(hybrid_vector_weight=1.5)

    def test_invalid_diversity_rejected(self):
        """Negative diversity should raise ValidationError."""
        with pytest.raises(ValidationError):
            Settings(search_diversity_max_per_file=-1)


class TestInferTags:
    """Test lightweight tag inference."""

    def test_infer_bugfix_from_fix(self):
        """Detects 'fix' as bugfix."""
        from daem0nmcp.memory import _infer_tags

        tags = _infer_tags("Fixed the login bug", "decision")
        assert "bugfix" in tags

    def test_infer_tech_debt_from_todo(self):
        """Detects 'TODO' as tech-debt."""
        from daem0nmcp.memory import _infer_tags

        tags = _infer_tags("TODO: refactor this later", "pattern")
        assert "tech-debt" in tags

    def test_infer_perf_from_cache(self):
        """Detects 'cache' as perf."""
        from daem0nmcp.memory import _infer_tags

        tags = _infer_tags("Added caching layer", "decision")
        assert "perf" in tags

    def test_infer_warning_from_category(self):
        """Warning category auto-adds warning tag."""
        from daem0nmcp.memory import _infer_tags

        tags = _infer_tags("Don't use this API", "warning")
        assert "warning" in tags

    def test_no_duplicate_with_existing_tags(self):
        """Doesn't duplicate existing tags."""
        from daem0nmcp.memory import _infer_tags

        tags = _infer_tags("Fixed a bug", "decision", existing_tags=["bugfix"])
        assert tags.count("bugfix") == 0  # Not added again

    def test_multiple_tags_inferred(self):
        """Can infer multiple tags from one content."""
        from daem0nmcp.memory import _infer_tags

        tags = _infer_tags("Temporary fix for slow performance", "decision")
        assert "tech-debt" in tags  # 'temporary'
        assert "perf" in tags  # 'slow', 'performance'
        assert "bugfix" in tags  # 'fix'

    def test_no_false_positive_on_prefix(self):
        """Words containing patterns as substrings shouldn't trigger tags."""
        from daem0nmcp.memory import _infer_tags

        # "prefix" contains "fix" but shouldn't trigger bugfix
        tags = _infer_tags("Use prefix conventions", "decision")
        assert "bugfix" not in tags

    def test_no_false_positive_on_breakfast(self):
        """'breakfast' contains 'fast' but shouldn't trigger perf."""
        from daem0nmcp.memory import _infer_tags

        tags = _infer_tags("Breakfast meeting at 9am", "decision")
        assert "perf" not in tags

    def test_no_false_positive_on_tissue(self):
        """'tissue' contains 'issue' but shouldn't trigger bugfix."""
        from daem0nmcp.memory import _infer_tags

        tags = _infer_tags("Use tissue paper for packaging", "decision")
        assert "bugfix" not in tags

    def test_true_positive_still_works_fix(self):
        """Actual 'fix' word should still trigger bugfix."""
        from daem0nmcp.memory import _infer_tags

        tags = _infer_tags("Need to fix the login flow", "decision")
        assert "bugfix" in tags

    def test_true_positive_still_works_fast(self):
        """Actual 'fast' word should still trigger perf."""
        from daem0nmcp.memory import _infer_tags

        tags = _infer_tags("Make the API fast", "decision")
        assert "perf" in tags


class TestHybridWeightWiring:
    """Test hybrid weight is used from config."""

    def test_memory_uses_config_weight(self, monkeypatch):
        """MemoryManager should use config hybrid weight."""
        import importlib

        monkeypatch.setenv("DAEM0NMCP_HYBRID_VECTOR_WEIGHT", "0.7")

        # Force reload of settings and vectors module
        from daem0nmcp import config, vectors

        config.settings = config.Settings()
        importlib.reload(vectors)

        from daem0nmcp.similarity import TFIDFIndex
        from daem0nmcp.vectors import HybridSearch

        tfidf = TFIDFIndex()
        hybrid = HybridSearch(tfidf)

        assert hybrid.vector_weight == 0.7
