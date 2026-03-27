"""
JIT (Just-In-Time) Compressor - Automatic tiered compression for retrieval results.

Implements COMP-01/02/03/04: When retrieval results exceed token thresholds,
JIT compression fires automatically with tiered rates (soft/hard/emergency)
and returns compression metadata so callers can see what happened.

Tiers:
- Soft (>4K tokens): ~2x compression (rate 0.5)
- Hard (>8K tokens): ~3x compression (rate 0.33)
- Emergency (>16K tokens): ~5x compression (rate 0.2)

Dynamic rates increase proportionally to overshoot using square root dampening.
Code syntax and entity names are preserved via AdaptiveCompressor + CodeEntityPreserver.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from daem0nmcp.compression import AdaptiveCompressor

logger = logging.getLogger(__name__)


@dataclass
class JITCompressionConfig:
    """Configuration for JIT compression tiers and rates."""

    # Thresholds (token counts)
    soft_threshold: int = 4000  # Moderate compression kicks in
    hard_threshold: int = 8000  # Aggressive compression
    emergency_threshold: int = 16000  # Maximum compression

    # Base compression rates per tier
    soft_rate: float = 0.5  # 2x compression at soft
    hard_rate: float = 0.33  # 3x compression at hard
    emergency_rate: float = 0.2  # 5x compression at emergency

    # Rate clamps
    min_rate: float = 0.15  # Never compress more than ~7x
    max_rate: float = 0.6  # Never compress less than ~1.7x


class JITCompressor:
    """
    Just-in-time compression with configurable tiered thresholds.

    Automatically compresses retrieval results when they exceed token limits.
    Uses AdaptiveCompressor for content-aware compression that preserves
    code syntax and entity names.

    Three tiers with increasing aggression:
    - Soft (>4K tokens): Moderate 2x compression
    - Hard (>8K tokens): Aggressive 3x compression
    - Emergency (>16K tokens): Maximum 5x compression

    Dynamic rates scale proportionally to overshoot using square root dampening,
    clamped to [min_rate, max_rate] bounds.

    Usage:
        jit = JITCompressor()
        result = jit.compress_if_needed(large_text)
        if result["threshold_triggered"]:
            print(f"Compressed from {result['original_tokens']} to {result['compressed_tokens']}")
    """

    def __init__(
        self,
        adaptive_compressor: AdaptiveCompressor | None = None,
        config: JITCompressionConfig | None = None,
    ):
        """
        Initialize JIT compressor.

        Args:
            adaptive_compressor: AdaptiveCompressor instance for content-aware compression.
                                 Lazy-loaded on first use if None.
            config: JIT compression config with tier thresholds and rates.
                    Uses defaults if None.
        """
        self._compressor = adaptive_compressor
        self.config = config or JITCompressionConfig()
        self._initialized = False

    def _ensure_initialized(self) -> None:
        """Lazy-load AdaptiveCompressor on first use."""
        if self._initialized:
            return

        if self._compressor is None:
            from .adaptive import AdaptiveCompressor

            self._compressor = AdaptiveCompressor()
            logger.debug("JITCompressor: lazy-loaded AdaptiveCompressor")

        self._initialized = True

    def _determine_tier(self, token_count: int) -> str | None:
        """
        Determine compression tier based on token count.

        Args:
            token_count: Number of tokens in the text.

        Returns:
            Tier name ("emergency", "hard", "soft") or None if below all thresholds.
        """
        if token_count > self.config.emergency_threshold:
            return "emergency"
        elif token_count > self.config.hard_threshold:
            return "hard"
        elif token_count > self.config.soft_threshold:
            return "soft"
        return None

    def _compute_dynamic_rate(self, token_count: int, tier: str) -> float:
        """
        Compute dynamic compression rate proportional to overshoot.

        Uses square root dampening: higher overshoot compresses more aggressively
        but with diminishing returns to avoid over-compression.

        Formula: dynamic_rate = base_rate * sqrt(soft_threshold / token_count)
        Result is clamped to [min_rate, max_rate].

        Args:
            token_count: Number of tokens in the text.
            tier: Compression tier ("soft", "hard", "emergency").

        Returns:
            Clamped compression rate.
        """
        # Get base rate for tier
        tier_rates = {
            "soft": self.config.soft_rate,
            "hard": self.config.hard_rate,
            "emergency": self.config.emergency_rate,
        }
        base_rate = tier_rates[tier]

        # Compute overshoot factor relative to soft threshold
        overshoot_factor = self.config.soft_threshold / max(token_count, 1)

        # Apply square root dampening
        dynamic_rate = base_rate * math.sqrt(overshoot_factor)

        # Clamp to bounds
        clamped = max(self.config.min_rate, min(self.config.max_rate, dynamic_rate))

        logger.debug(
            f"JITCompressor: tier={tier}, base_rate={base_rate}, "
            f"overshoot_factor={overshoot_factor:.3f}, "
            f"dynamic_rate={dynamic_rate:.3f}, clamped={clamped:.3f}"
        )

        return clamped

    def compress_if_needed(
        self,
        text: str,
        already_compressed: bool = False,
        additional_force_tokens: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Compress text if it exceeds the soft threshold.

        Returns the text unchanged (with metadata) if:
        - already_compressed is True (prevents double-compression)
        - Token count is at or below the soft threshold

        Otherwise, determines the appropriate tier, computes a dynamic
        compression rate, and applies content-aware compression.

        Args:
            text: Text to potentially compress.
            already_compressed: If True, skip compression entirely.
            additional_force_tokens: Extra tokens to preserve during compression.

        Returns:
            Dict with keys:
                - text: The (possibly compressed) text
                - original_tokens: Token count before compression
                - compressed_tokens: Token count after compression
                - compression_rate: Achieved compression ratio (1.0 = no compression)
                - threshold_triggered: Tier name or None
                - content_type: Detected content type (only when compressed)
        """
        self._ensure_initialized()

        # Count tokens using tiktoken cl100k_base (same counter as thresholds)
        token_count = self._compressor.compressor.count_tokens(text)

        # Skip if already compressed or below threshold
        if already_compressed or token_count <= self.config.soft_threshold:
            return {
                "text": text,
                "original_tokens": token_count,
                "compressed_tokens": token_count,
                "compression_rate": 1.0,
                "threshold_triggered": None,
            }

        # Determine tier and compute dynamic rate
        tier = self._determine_tier(token_count)
        rate = self._compute_dynamic_rate(token_count, tier)

        # Compress using AdaptiveCompressor (content-aware, code-preserving)
        result = self._compressor.compress(
            text,
            rate_override=rate,
            additional_force_tokens=additional_force_tokens,
        )

        compressed_result = {
            "text": result["compressed_prompt"],
            "original_tokens": result["original_tokens"],
            "compressed_tokens": result["compressed_tokens"],
            "compression_rate": round(result["ratio"], 2),
            "threshold_triggered": tier,
            "content_type": result.get("content_type"),
        }

        logger.info(
            f"JITCompressor: {tier} tier triggered, "
            f"{result['original_tokens']} -> {result['compressed_tokens']} tokens "
            f"(rate={rate:.2f}, ratio={result['ratio']:.2f})"
        )

        return compressed_result


# Module-level singleton for convenience
_default_jit: JITCompressor | None = None


def get_jit_compressor() -> JITCompressor:
    """Get or create the global JITCompressor singleton."""
    global _default_jit
    if _default_jit is None:
        _default_jit = JITCompressor()
    return _default_jit


def jit_compress(
    text: str,
    already_compressed: bool = False,
    additional_force_tokens: list[str] | None = None,
) -> dict[str, Any]:
    """
    Convenience function for JIT compression using the global singleton.

    Args:
        text: Text to potentially compress.
        already_compressed: If True, skip compression.
        additional_force_tokens: Extra tokens to preserve.

    Returns:
        Compression result dict with metadata.
    """
    compressor = get_jit_compressor()
    return compressor.compress_if_needed(
        text,
        already_compressed=already_compressed,
        additional_force_tokens=additional_force_tokens,
    )
