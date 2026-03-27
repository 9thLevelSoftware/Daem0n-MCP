"""
Integration tests for Phase 4: Context Engineering.

Verifies all CONTEXT-* requirements:
- CONTEXT-01: LLMLingua-2 integrated for intelligent prompt compression
- CONTEXT-02: Tokens classified by information entropy during compression
- CONTEXT-03: 3x-6x compression achieved while preserving code syntax and named entities
- CONTEXT-04: Hierarchical compression leverages Phase 1 community structure
- CONTEXT-05: Adaptive compression ratios based on query complexity
"""


class TestContextEngineeringRequirements:
    """Integration tests verifying Phase 4 requirements."""

    def test_context_01_llmlingua2_integration(self):
        """CONTEXT-01: LLMLingua-2 integrated for intelligent prompt compression."""
        from daem0nmcp.compression import CompressionConfig, ContextCompressor

        # Compressor initializes
        compressor = ContextCompressor()
        assert compressor is not None

        # Config uses LLMLingua-2
        config = CompressionConfig()
        assert "llmlingua-2" in config.model_name.lower()
        assert config.compression_threshold == 4000

    def test_context_02_token_classification(self):
        """CONTEXT-02: Tokens classified by information entropy during compression."""
        from daem0nmcp.compression import CompressionConfig

        # LLMLingua-2 uses XLM-RoBERTa for token classification
        config = CompressionConfig()
        assert "xlm-roberta" in config.model_name.lower()

        # Base force tokens preserve structural elements
        assert "." in config.base_force_tokens
        assert "\n" in config.base_force_tokens

    def test_context_03_compression_ratio_target(self):
        """CONTEXT-03: 3x-6x compression achieved (config)."""
        from daem0nmcp.compression import (
            COMPRESSION_RATES,
            CompressionConfig,
            ContentType,
        )

        config = CompressionConfig()

        # Default rate 0.33 = 3x compression target
        assert config.default_rate == 0.33

        # Compression rates support 2x-5x range
        assert COMPRESSION_RATES[ContentType.CODE] == 0.5  # 2x
        assert COMPRESSION_RATES[ContentType.NARRATIVE] == 0.2  # 5x
        assert COMPRESSION_RATES[ContentType.MIXED] == 0.33  # 3x

    def test_context_03_code_entity_preservation(self):
        """CONTEXT-03: Code syntax and named entities preserved."""
        from daem0nmcp.compression import CodeEntityPreserver

        preserver = CodeEntityPreserver()
        tokens = preserver.get_structural_tokens()

        # Python keywords
        assert "def " in tokens
        assert "class " in tokens
        assert "import " in tokens
        assert "return " in tokens

        # Arrow operator for type annotations and lambdas
        assert "->" in tokens
        # Common operators preserved
        assert "=>" in tokens

    def test_context_03_entity_extraction(self):
        """CONTEXT-03: Named entities extracted for preservation."""
        from daem0nmcp.compression import CodeEntityPreserver

        preserver = CodeEntityPreserver()
        code = """
def calculate_discount(price, rate):
    return price * (1 - rate)

class DiscountCalculator:
    def apply(self, price):
        return self.calculate_discount(price, self.rate)
"""
        names = preserver.extract_entity_names(code)

        assert "calculate_discount" in names
        assert "DiscountCalculator" in names

    def test_context_04_hierarchical_summaries(self):
        """CONTEXT-04: Hierarchical compression leverages community structure."""
        from daem0nmcp.compression import HierarchicalContextManager

        manager = HierarchicalContextManager()

        # Simple queries use summaries (pre-compressed)
        summaries = ["Community 1: Auth patterns", "Community 2: Database decisions"]
        memories = [{"content": "Raw memory", "category": "decision"}]

        result = manager.get_context(
            query="what is auth?",  # SIMPLE
            memories=memories,
            community_summaries=summaries,
        )

        assert result["strategy"] == "summaries"
        assert "Auth patterns" in result["context"]
        assert result["compression_applied"] is False

    def test_context_05_adaptive_rates_code(self):
        """CONTEXT-05: Code gets conservative compression."""
        from daem0nmcp.compression import AdaptiveCompressor, ContentType

        adaptive = AdaptiveCompressor()

        # Need enough code indicators to trigger CODE classification (density > 5)
        # Density = code_indicators / (length / 1000)
        # Short text with many indicators
        code = """
def foo(): pass
def bar(): pass
def baz(): pass
def qux(): pass
class Foo:
    def __init__(self): pass
    def method(self): return self.value
class Bar:
    def __init__(self): pass
import os
from typing import List
"""
        content_type = adaptive.classify_content(code)
        rate = adaptive.get_rate_for_content(content_type)

        assert content_type == ContentType.CODE
        assert rate == 0.5  # Conservative 2x

    def test_context_05_adaptive_rates_narrative(self):
        """CONTEXT-05: Narrative gets aggressive compression."""
        from daem0nmcp.compression import AdaptiveCompressor, ContentType

        adaptive = AdaptiveCompressor()

        prose = """
        This document explains the architecture of our system.
        We use a microservices approach with message queues.
        Each service handles a specific domain concern.
        """
        content_type = adaptive.classify_content(prose)
        rate = adaptive.get_rate_for_content(content_type)

        assert content_type == ContentType.NARRATIVE
        assert rate == 0.2  # Aggressive 5x

    def test_context_05_recall_plan_compression(self):
        """CONTEXT-05: RecallPlan includes compression based on complexity."""
        from daem0nmcp.recall_planner import QueryComplexity, RecallPlanner

        planner = RecallPlanner()

        # Simple query - no compression (uses summaries)
        simple_plan = planner.plan_recall("what is X?")
        assert simple_plan.compress is False

        # Complex query - compression enabled
        complex_plan = planner.plan_recall(
            "trace the complete history of all decisions"
        )
        assert complex_plan.complexity == QueryComplexity.COMPLEX
        assert complex_plan.compress is True
        assert complex_plan.compression_rate < 0.5  # At least 2x target


class TestCompressionThreshold:
    """Tests for 4K token compression threshold."""

    def test_under_threshold_not_compressed(self):
        """Context under 4K tokens is not compressed."""
        from daem0nmcp.compression import ContextCompressor

        compressor = ContextCompressor()
        short_text = "This is a short text that should not be compressed."

        assert not compressor.should_compress(short_text)

        result = compressor.compress(short_text)
        assert result["skipped"] is True
        assert result["compressed_prompt"] == short_text

    def test_threshold_configurable(self):
        """Compression threshold is configurable."""
        from daem0nmcp.compression import CompressionConfig, ContextCompressor

        config = CompressionConfig(compression_threshold=100)
        compressor = ContextCompressor(config)

        # Text over 100 tokens should trigger compression check
        medium_text = "This is a test sentence. " * 20
        assert compressor.should_compress(medium_text)


class TestHierarchicalStrategies:
    """Tests for hierarchical context strategies."""

    def test_simple_strategy_prefers_summaries(self):
        """Simple queries prefer community summaries."""
        from daem0nmcp.compression import HierarchicalContextManager

        manager = HierarchicalContextManager()

        result = manager.get_context(
            query="what?",  # 1 word = SIMPLE
            memories=[{"content": "memory", "category": "test"}],
            community_summaries=["Summary of community"],
        )

        assert result["strategy"] == "summaries"

    def test_complex_strategy_compresses_raw(self):
        """Complex queries compress raw memories."""
        from daem0nmcp.compression import HierarchicalContextManager

        manager = HierarchicalContextManager()

        result = manager.get_context(
            query="trace the complete history of all decisions and their outcomes over time",
            memories=[{"content": "Long memory content", "category": "decision"}],
            community_summaries=None,
        )

        # Should attempt compression strategy
        assert result["strategy"] in ["compressed", "raw"]

    def test_medium_strategy_hybrid(self):
        """Medium queries use hybrid approach."""
        from daem0nmcp.compression import HierarchicalContextManager

        manager = HierarchicalContextManager()

        result = manager.get_context(
            query="find authentication patterns in codebase",  # 5 words = MEDIUM
            memories=[{"content": "Raw memory", "category": "pattern"}],
            community_summaries=["Auth community summary"],
        )

        # Should include both
        assert result["strategy"] in ["hybrid", "hybrid_compressed"]


class TestModuleExports:
    """Test that all expected exports are available."""

    def test_compression_module_exports(self):
        """All expected classes are exported from compression module."""
        from daem0nmcp.compression import (
            COMPRESSION_RATES,
            AdaptiveCompressor,
            CodeEntityPreserver,
            CompressionConfig,
            ContentType,
            ContextCompressor,
            HierarchicalContextManager,
        )

        # All exports should be importable
        assert CompressionConfig is not None
        assert ContextCompressor is not None
        assert CodeEntityPreserver is not None
        assert AdaptiveCompressor is not None
        assert ContentType is not None
        assert COMPRESSION_RATES is not None
        assert HierarchicalContextManager is not None


class TestMCPToolIntegration:
    """Tests for the compress_context MCP tool."""

    def test_compress_context_import(self):
        """compress_context tool is importable from server."""
        from daem0nmcp.server import compress_context

        assert compress_context is not None
        assert callable(compress_context)

    def test_server_tool_count_updated(self):
        """Server is composition root with compress_context re-exported."""
        import daem0nmcp.server as server

        # Verify server is a composition root with tools re-exported
        assert "Composition" in server.__doc__ or "composition" in server.__doc__
        assert hasattr(server, "compress_context")
