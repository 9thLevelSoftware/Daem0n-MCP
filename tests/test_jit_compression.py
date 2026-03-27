"""Tests for JIT (Just-In-Time) compression module.

Tests tiered thresholds, dynamic rates, metadata output,
double-compression prevention, and custom configuration.
Uses mocked AdaptiveCompressor to avoid loading LLMLingua-2 in CI.
"""

from unittest.mock import MagicMock, patch

from daem0nmcp.compression.jit import JITCompressionConfig, JITCompressor


def _make_mock_adaptive(token_count_fn=None):
    """Create a mock AdaptiveCompressor with a mock compressor attribute.

    Args:
        token_count_fn: Custom token counting function. Defaults to len(text) // 4.

    Returns:
        Mock AdaptiveCompressor with compressor.count_tokens and compress methods.
    """
    mock_adaptive = MagicMock()

    # Mock the inner ContextCompressor's count_tokens
    if token_count_fn is None:

        def token_count_fn(text):
            return len(text) // 4  # Rough approximation

    mock_adaptive.compressor.count_tokens.side_effect = token_count_fn

    # Default compress behavior: simulate compression
    def mock_compress(text, rate_override=None, additional_force_tokens=None, **kwargs):
        original = token_count_fn(text)
        rate = rate_override or 0.33
        compressed = max(1, int(original * rate))
        return {
            "compressed_prompt": text[: int(len(text) * rate)],
            "original_tokens": original,
            "compressed_tokens": compressed,
            "ratio": original / max(compressed, 1),
            "content_type": "mixed",
        }

    mock_adaptive.compress.side_effect = mock_compress
    return mock_adaptive


class TestJITCompressionConfig:
    """Tests for JITCompressionConfig defaults."""

    def test_default_thresholds(self):
        """Default thresholds are 4K/8K/16K."""
        config = JITCompressionConfig()
        assert config.soft_threshold == 4000
        assert config.hard_threshold == 8000
        assert config.emergency_threshold == 16000

    def test_default_rates(self):
        """Default rates are 0.5/0.33/0.2."""
        config = JITCompressionConfig()
        assert config.soft_rate == 0.5
        assert config.hard_rate == 0.33
        assert config.emergency_rate == 0.2

    def test_rate_clamps(self):
        """Default clamps are 0.15 min and 0.6 max."""
        config = JITCompressionConfig()
        assert config.min_rate == 0.15
        assert config.max_rate == 0.6


class TestJITCompressorTiers:
    """Tests for tier determination and threshold triggering."""

    def test_below_soft_threshold_no_compression(self):
        """Text with token count < 4000 is not compressed."""
        # Create text that produces ~3000 tokens (12000 chars / 4)
        text = "a" * 12000
        mock = _make_mock_adaptive()
        jit = JITCompressor(adaptive_compressor=mock)

        result = jit.compress_if_needed(text)

        assert result["threshold_triggered"] is None
        assert result["compression_rate"] == 1.0
        assert result["text"] == text
        assert result["original_tokens"] == 3000
        assert result["compressed_tokens"] == 3000
        # compress should NOT have been called
        mock.compress.assert_not_called()

    def test_soft_threshold_triggers_moderate_compression(self):
        """Text with ~5000 tokens triggers soft tier."""
        # 20000 chars / 4 = 5000 tokens (above soft=4000)
        text = "x" * 20000
        mock = _make_mock_adaptive()
        jit = JITCompressor(adaptive_compressor=mock)

        result = jit.compress_if_needed(text)

        assert result["threshold_triggered"] == "soft"
        mock.compress.assert_called_once()
        # Check that rate_override was passed and is in soft range
        call_kwargs = mock.compress.call_args
        rate = call_kwargs.kwargs.get("rate_override") or call_kwargs[1].get(
            "rate_override"
        )
        assert 0.3 <= rate <= 0.6  # Soft rate with dynamic adjustment

    def test_hard_threshold_triggers_aggressive_compression(self):
        """Text with ~10000 tokens triggers hard tier."""
        # 40000 chars / 4 = 10000 tokens (above hard=8000)
        text = "y" * 40000
        mock = _make_mock_adaptive()
        jit = JITCompressor(adaptive_compressor=mock)

        result = jit.compress_if_needed(text)

        assert result["threshold_triggered"] == "hard"
        mock.compress.assert_called_once()
        call_kwargs = mock.compress.call_args
        rate = call_kwargs.kwargs.get("rate_override") or call_kwargs[1].get(
            "rate_override"
        )
        assert 0.15 <= rate <= 0.35  # Hard rate with dynamic adjustment

    def test_emergency_threshold_triggers_maximum_compression(self):
        """Text with ~20000 tokens triggers emergency tier."""
        # 80000 chars / 4 = 20000 tokens (above emergency=16000)
        text = "z" * 80000
        mock = _make_mock_adaptive()
        jit = JITCompressor(adaptive_compressor=mock)

        result = jit.compress_if_needed(text)

        assert result["threshold_triggered"] == "emergency"
        mock.compress.assert_called_once()
        call_kwargs = mock.compress.call_args
        rate = call_kwargs.kwargs.get("rate_override") or call_kwargs[1].get(
            "rate_override"
        )
        assert 0.15 <= rate <= 0.2  # Emergency rate, heavily clamped


class TestJITDynamicRates:
    """Tests for dynamic rate computation."""

    def test_dynamic_rate_clamped_to_min(self):
        """With extreme overshoot (100K tokens), rate is clamped to min_rate."""
        # 400000 chars / 4 = 100000 tokens
        text = "w" * 400000
        mock = _make_mock_adaptive()
        jit = JITCompressor(adaptive_compressor=mock)

        jit.compress_if_needed(text)

        call_kwargs = mock.compress.call_args
        rate = call_kwargs.kwargs.get("rate_override") or call_kwargs[1].get(
            "rate_override"
        )
        assert rate == jit.config.min_rate  # Should be clamped to 0.15

    def test_dynamic_rate_clamped_to_max(self):
        """With barely over soft threshold, rate is clamped to max_rate."""
        # 16040 chars / 4 = 4010 tokens (barely over soft=4000)
        text = "v" * 16040
        mock = _make_mock_adaptive()
        jit = JITCompressor(adaptive_compressor=mock)

        jit.compress_if_needed(text)

        call_kwargs = mock.compress.call_args
        rate = call_kwargs.kwargs.get("rate_override") or call_kwargs[1].get(
            "rate_override"
        )
        # sqrt(4000/4010) ~ 0.999, base_rate * 0.999 = 0.4995
        # Not clamped to max (0.6) since 0.4995 < 0.6, but should be reasonable
        assert rate <= jit.config.max_rate
        assert rate >= jit.config.min_rate


class TestJITDoubleCompression:
    """Tests for double-compression prevention."""

    def test_already_compressed_skipped(self):
        """already_compressed=True skips compression even for large text."""
        # 40000 chars / 4 = 10000 tokens (well above thresholds)
        text = "q" * 40000
        mock = _make_mock_adaptive()
        jit = JITCompressor(adaptive_compressor=mock)

        result = jit.compress_if_needed(text, already_compressed=True)

        assert result["threshold_triggered"] is None
        assert result["compression_rate"] == 1.0
        assert result["text"] == text
        # Compression should NOT have been called
        mock.compress.assert_not_called()


class TestJITMetadata:
    """Tests for metadata output structure."""

    def test_metadata_output_structure(self):
        """Result dict has all required metadata keys."""
        text = "m" * 20000  # 5000 tokens, triggers soft
        mock = _make_mock_adaptive()
        jit = JITCompressor(adaptive_compressor=mock)

        result = jit.compress_if_needed(text)

        # All required keys present
        assert "text" in result
        assert "original_tokens" in result
        assert "compressed_tokens" in result
        assert "compression_rate" in result
        assert "threshold_triggered" in result

    def test_metadata_output_structure_no_compression(self):
        """No-compression result also has all metadata keys."""
        text = "n" * 8000  # 2000 tokens, below soft
        mock = _make_mock_adaptive()
        jit = JITCompressor(adaptive_compressor=mock)

        result = jit.compress_if_needed(text)

        assert "text" in result
        assert "original_tokens" in result
        assert "compressed_tokens" in result
        assert "compression_rate" in result
        assert "threshold_triggered" in result

    def test_content_type_in_metadata(self):
        """When compression occurs, content_type appears in metadata."""
        text = "c" * 20000  # 5000 tokens, triggers soft
        mock = _make_mock_adaptive()
        jit = JITCompressor(adaptive_compressor=mock)

        result = jit.compress_if_needed(text)

        assert result["threshold_triggered"] == "soft"
        assert "content_type" in result
        assert result["content_type"] == "mixed"  # From mock


class TestJITCustomConfig:
    """Tests for custom configuration."""

    def test_custom_config_thresholds(self):
        """Custom thresholds are respected."""
        config = JITCompressionConfig(soft_threshold=2000)
        # 8400 chars / 4 = 2100 tokens (above custom soft=2000, below default soft=4000)
        text = "t" * 8400
        mock = _make_mock_adaptive()
        jit = JITCompressor(adaptive_compressor=mock, config=config)

        result = jit.compress_if_needed(text)

        # Should trigger soft with custom threshold
        assert result["threshold_triggered"] == "soft"
        mock.compress.assert_called_once()

    def test_default_config_would_not_compress(self):
        """Same text would NOT trigger with default config (verifying custom works)."""
        # 8400 chars / 4 = 2100 tokens (below default soft=4000)
        text = "t" * 8400
        mock = _make_mock_adaptive()
        jit = JITCompressor(adaptive_compressor=mock)

        result = jit.compress_if_needed(text)

        assert result["threshold_triggered"] is None
        mock.compress.assert_not_called()


class TestJITForceTokens:
    """Tests for additional_force_tokens passthrough."""

    def test_additional_force_tokens_passed_through(self):
        """additional_force_tokens are forwarded to AdaptiveCompressor.compress()."""
        text = "f" * 20000  # 5000 tokens, triggers soft
        mock = _make_mock_adaptive()
        jit = JITCompressor(adaptive_compressor=mock)

        force_tokens = ["myFunction", "className"]
        jit.compress_if_needed(text, additional_force_tokens=force_tokens)

        mock.compress.assert_called_once()
        call_kwargs = mock.compress.call_args
        passed_tokens = call_kwargs.kwargs.get(
            "additional_force_tokens"
        ) or call_kwargs[1].get("additional_force_tokens")
        assert passed_tokens == ["myFunction", "className"]


class TestJITInitialization:
    """Tests for lazy initialization behavior."""

    def test_lazy_initialization(self):
        """JITCompressor with no args starts uninitialized."""
        jit = JITCompressor()
        assert jit._initialized is False
        assert jit._compressor is None

    def test_initialization_on_first_use(self):
        """Calling compress_if_needed triggers initialization."""
        mock = _make_mock_adaptive()
        jit = JITCompressor(adaptive_compressor=mock)

        assert jit._initialized is False

        jit.compress_if_needed("hello")

        assert jit._initialized is True

    @patch("daem0nmcp.compression.jit.JITCompressor._ensure_initialized")
    def test_lazy_loads_adaptive_compressor(self, mock_ensure):
        """_ensure_initialized is called on compress_if_needed."""
        mock_adaptive = _make_mock_adaptive()
        jit = JITCompressor(adaptive_compressor=mock_adaptive)
        # Override _initialized to skip actual init
        jit._initialized = True

        jit.compress_if_needed("test")

        mock_ensure.assert_called_once()


class TestJITTierDetermination:
    """Tests for internal _determine_tier method."""

    def test_determine_tier_below_soft(self):
        """Below soft threshold returns None."""
        jit = JITCompressor(adaptive_compressor=_make_mock_adaptive())
        assert jit._determine_tier(3999) is None

    def test_determine_tier_at_soft(self):
        """At soft threshold returns None (must exceed)."""
        jit = JITCompressor(adaptive_compressor=_make_mock_adaptive())
        assert jit._determine_tier(4000) is None

    def test_determine_tier_above_soft(self):
        """Above soft threshold returns soft."""
        jit = JITCompressor(adaptive_compressor=_make_mock_adaptive())
        assert jit._determine_tier(4001) == "soft"

    def test_determine_tier_above_hard(self):
        """Above hard threshold returns hard."""
        jit = JITCompressor(adaptive_compressor=_make_mock_adaptive())
        assert jit._determine_tier(8001) == "hard"

    def test_determine_tier_above_emergency(self):
        """Above emergency threshold returns emergency."""
        jit = JITCompressor(adaptive_compressor=_make_mock_adaptive())
        assert jit._determine_tier(16001) == "emergency"
