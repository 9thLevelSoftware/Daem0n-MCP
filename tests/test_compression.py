"""Tests for compression module."""

import pytest

from daem0nmcp.compression import CompressionConfig, ContextCompressor


class TestCompressionConfig:
    """Tests for CompressionConfig."""

    def test_default_threshold(self):
        """Default threshold is 4000 tokens."""
        config = CompressionConfig()
        assert config.compression_threshold == 4000

    def test_default_rate(self):
        """Default compression rate is 0.33 (3x)."""
        config = CompressionConfig()
        assert config.default_rate == 0.33

    def test_default_model(self):
        """Default model is XLM-RoBERTa large."""
        config = CompressionConfig()
        assert "xlm-roberta-large" in config.model_name

    def test_base_force_tokens_includes_punctuation(self):
        """Base force tokens include essential punctuation."""
        config = CompressionConfig()
        assert "." in config.base_force_tokens
        assert "\n" in config.base_force_tokens
        assert "?" in config.base_force_tokens

    def test_custom_threshold(self):
        """Custom threshold is respected."""
        config = CompressionConfig(compression_threshold=2000)
        assert config.compression_threshold == 2000

    def test_custom_rate(self):
        """Custom rate is respected."""
        config = CompressionConfig(default_rate=0.5)
        assert config.default_rate == 0.5

    def test_device_options(self):
        """Device can be set to auto, cpu, or cuda."""
        for device in ["auto", "cpu", "cuda"]:
            config = CompressionConfig(device=device)
            assert config.device == device


class TestContextCompressor:
    """Tests for ContextCompressor."""

    def test_lazy_initialization(self):
        """Compressor doesn't load model on init."""
        compressor = ContextCompressor()
        assert compressor._compressor is None

    def test_count_tokens_short_text(self):
        """Token counting works for short text."""
        compressor = ContextCompressor()
        count = compressor.count_tokens("Hello world")
        assert count > 0
        assert count < 10  # Should be 2-3 tokens

    def test_count_tokens_empty_string(self):
        """Token counting works for empty string."""
        compressor = ContextCompressor()
        count = compressor.count_tokens("")
        assert count == 0

    def test_count_tokens_unicode(self):
        """Token counting handles unicode text."""
        compressor = ContextCompressor()
        count = compressor.count_tokens("Hello")
        assert count > 0

    def test_should_compress_under_threshold(self):
        """should_compress returns False for short text."""
        compressor = ContextCompressor()
        short_text = "This is a short text."
        assert not compressor.should_compress(short_text)

    def test_should_compress_over_threshold(self):
        """should_compress returns True for long text."""
        config = CompressionConfig(
            compression_threshold=10
        )  # Low threshold for testing
        compressor = ContextCompressor(config)
        long_text = "This is a longer text that should exceed the threshold. " * 10
        assert compressor.should_compress(long_text)

    def test_compress_skips_under_threshold(self):
        """compress() returns unchanged text when under threshold."""
        compressor = ContextCompressor()
        short_text = "Short text that won't be compressed."
        result = compressor.compress(short_text)

        assert result["compressed_prompt"] == short_text
        assert result["skipped"] is True
        assert result["ratio"] == 1.0

    def test_compress_returns_all_fields(self):
        """compress() returns all expected fields."""
        compressor = ContextCompressor()
        short_text = "Short text."
        result = compressor.compress(short_text)

        assert "compressed_prompt" in result
        assert "original_tokens" in result
        assert "compressed_tokens" in result
        assert "ratio" in result
        assert "skipped" in result

    def test_compress_simple_returns_string(self):
        """compress_simple returns just the text."""
        compressor = ContextCompressor()
        short_text = "Short text."
        result = compressor.compress_simple(short_text)

        assert isinstance(result, str)
        assert result == short_text

    def test_config_passed_to_compressor(self):
        """Custom config is used by compressor."""
        config = CompressionConfig(compression_threshold=100)
        compressor = ContextCompressor(config)
        assert compressor.config.compression_threshold == 100

    def test_tokenizer_lazy_loaded_on_count(self):
        """Tokenizer is loaded lazily on first count_tokens call."""
        compressor = ContextCompressor()
        assert compressor._tokenizer is None
        compressor.count_tokens("test")
        assert compressor._tokenizer is not None


class TestAdaptiveCompressor:
    """Tests for AdaptiveCompressor."""

    def test_classify_code(self):
        """Classifies code-heavy content correctly."""
        from daem0nmcp.compression import AdaptiveCompressor, ContentType

        adaptive = AdaptiveCompressor()

        code = """
def function_one():
    return 1

def function_two():
    return 2

class MyClass:
    def __init__(self):
        self.value = 42
"""
        assert adaptive.classify_content(code) == ContentType.CODE

    def test_classify_narrative(self):
        """Classifies narrative content correctly."""
        from daem0nmcp.compression import AdaptiveCompressor, ContentType

        adaptive = AdaptiveCompressor()

        prose = """
        This document describes the architecture of our system.
        The system is designed to handle multiple concurrent users
        while maintaining data consistency and performance.
        We use a microservices approach with message queues.
        """
        assert adaptive.classify_content(prose) == ContentType.NARRATIVE

    def test_classify_mixed(self):
        """Classifies mixed content correctly."""
        from daem0nmcp.compression import AdaptiveCompressor, ContentType

        adaptive = AdaptiveCompressor()

        mixed = """
        The authentication module handles user login.

        def authenticate(username, password):
            return check_credentials(username, password)

        Users are validated against the database.
        """
        assert adaptive.classify_content(mixed) == ContentType.MIXED

    def test_get_rate_for_code(self):
        """Code gets conservative 2x compression."""
        from daem0nmcp.compression import AdaptiveCompressor, ContentType

        adaptive = AdaptiveCompressor()

        rate = adaptive.get_rate_for_content(ContentType.CODE)
        assert rate == 0.5  # 2x compression

    def test_get_rate_for_narrative(self):
        """Narrative gets aggressive 5x compression."""
        from daem0nmcp.compression import AdaptiveCompressor, ContentType

        adaptive = AdaptiveCompressor()

        rate = adaptive.get_rate_for_content(ContentType.NARRATIVE)
        assert rate == 0.2  # 5x compression

    def test_compress_includes_content_type(self):
        """Compression result includes detected content type."""
        from daem0nmcp.compression import AdaptiveCompressor

        adaptive = AdaptiveCompressor()

        result = adaptive.compress("This is prose.")
        assert "content_type" in result
        assert result["content_type"] == "narrative"

    def test_compress_simple_returns_string(self):
        """compress_simple returns just the text."""
        from daem0nmcp.compression import AdaptiveCompressor

        adaptive = AdaptiveCompressor()

        result = adaptive.compress_simple("Short text.")
        assert isinstance(result, str)


class TestContextCompressorIntegration:
    """Integration tests that require model loading."""

    @pytest.mark.slow
    def test_compress_actually_compresses(self):
        """Compression achieves target ratio (requires model)."""
        config = CompressionConfig(compression_threshold=100)
        compressor = ContextCompressor(config)

        # Generate text over threshold
        long_text = "This is a test sentence about Python programming. " * 50

        result = compressor.compress(long_text, rate=0.5)

        assert result["skipped"] is False
        assert result["compressed_tokens"] < result["original_tokens"]
        assert result["ratio"] > 1.0

    @pytest.mark.slow
    def test_force_tokens_preserved(self):
        """Force tokens are preserved in compressed output."""
        config = CompressionConfig(compression_threshold=100)
        compressor = ContextCompressor(config)

        # Text with code markers
        code_text = "def foo():\n    return bar()\n" * 20

        result = compressor.compress(code_text, force_tokens=["def", "return"])

        # Output should still contain structure
        assert "\n" in result["compressed_prompt"]


class TestHierarchicalContextManager:
    """Tests for HierarchicalContextManager."""

    def test_simple_query_uses_summaries(self):
        """Simple queries prefer community summaries."""
        from daem0nmcp.compression import HierarchicalContextManager

        manager = HierarchicalContextManager()
        summaries = ["Summary 1: Auth system overview", "Summary 2: Database patterns"]
        memories = [{"content": "Raw memory 1", "category": "decision"}]

        result = manager.get_context(
            query="what is auth?",  # Simple (3 words)
            memories=memories,
            community_summaries=summaries,
        )

        assert result["strategy"] == "summaries"
        assert result["compression_applied"] is False
        assert "Auth system" in result["context"]

    def test_simple_query_fallback_to_raw(self):
        """Simple queries fall back to raw if no summaries."""
        from daem0nmcp.compression import HierarchicalContextManager

        manager = HierarchicalContextManager()
        memories = [{"content": "Raw memory content", "category": "decision"}]

        result = manager.get_context(
            query="what is auth?",
            memories=memories,
            community_summaries=None,
        )

        assert result["strategy"] == "raw_fallback"
        assert "Raw memory content" in result["context"]

    def test_complex_query_uses_compression(self):
        """Complex queries use compression strategy."""
        from daem0nmcp.compression import HierarchicalContextManager

        manager = HierarchicalContextManager()
        memories = [{"content": "Memory " * 100, "category": "decision"}]

        result = manager.get_context(
            query="trace the complete history of all authentication decisions and their outcomes",
            memories=memories,
            community_summaries=None,
        )

        # Strategy should be compression-oriented (may skip if under threshold)
        assert result["strategy"] in ["compressed", "raw"]

    def test_format_memories(self):
        """Memories are formatted correctly."""
        from daem0nmcp.compression import HierarchicalContextManager

        manager = HierarchicalContextManager()
        memories = [
            {"content": "First memory", "category": "decision"},
            {"content": "Second memory", "category": "pattern"},
        ]

        formatted = manager._format_memories(memories)

        assert "[decision] First memory" in formatted
        assert "[pattern] Second memory" in formatted

    def test_format_summaries(self):
        """Summaries are joined correctly."""
        from daem0nmcp.compression import HierarchicalContextManager

        manager = HierarchicalContextManager()
        summaries = ["Summary A", "Summary B"]

        formatted = manager._format_summaries(summaries)

        assert "Summary A" in formatted
        assert "Summary B" in formatted

    def test_result_includes_token_count(self):
        """All results include token count."""
        from daem0nmcp.compression import HierarchicalContextManager

        manager = HierarchicalContextManager()
        memories = [{"content": "Test memory", "category": "test"}]

        result = manager.get_context(
            query="test",
            memories=memories,
            community_summaries=None,
        )

        assert "token_count" in result
        assert isinstance(result["token_count"], int)

    def test_medium_query_hybrid_strategy(self):
        """Medium complexity queries use hybrid strategy."""
        from daem0nmcp.compression import HierarchicalContextManager

        manager = HierarchicalContextManager()
        summaries = ["Summary 1: Auth overview"]
        memories = [{"content": "Raw memory details", "category": "decision"}]

        result = manager.get_context(
            query="how does auth work",  # Medium complexity (4 words, no complex patterns)
            memories=memories,
            community_summaries=summaries,
        )

        # Should be hybrid or hybrid_compressed
        assert result["strategy"] in ["hybrid", "hybrid_compressed"]

    def test_empty_memories_handled(self):
        """Empty memory list is handled gracefully."""
        from daem0nmcp.compression import HierarchicalContextManager

        manager = HierarchicalContextManager()

        result = manager.get_context(
            query="test",
            memories=[],
            community_summaries=None,
        )

        assert result["context"] == ""
        assert result["token_count"] == 0
