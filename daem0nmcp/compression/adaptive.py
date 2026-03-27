"""
Adaptive Compressor - Content-aware compression rates.

Implements CONTEXT-05: Adaptive compression ratios based on query complexity.
Different content types (code, narrative, mixed) get different compression
rates to balance size reduction with information preservation.
"""

import logging
from enum import Enum
from typing import Any

from .compressor import ContextCompressor
from .entity_preserver import CodeEntityPreserver

logger = logging.getLogger(__name__)


class ContentType(Enum):
    """Content type classification for compression rate selection."""

    CODE = "code"  # Code-heavy content (functions, classes)
    NARRATIVE = "narrative"  # Prose, documentation, natural language
    MIXED = "mixed"  # Combination of code and prose


# Compression rates by content type
# Lower rate = more aggressive compression
COMPRESSION_RATES: dict[ContentType, float] = {
    ContentType.CODE: 0.5,  # 2x compression (conservative for code)
    ContentType.NARRATIVE: 0.2,  # 5x compression (aggressive for prose)
    ContentType.MIXED: 0.33,  # 3x compression (balanced)
}


class AdaptiveCompressor:
    """
    Content-aware compression with automatic rate selection.

    Analyzes content to determine if it's code-heavy, narrative, or mixed,
    then applies appropriate compression rate:
    - Code: 2x (0.5 rate) - Preserves syntax, function names
    - Narrative: 5x (0.2 rate) - Aggressive, prose tolerates more loss
    - Mixed: 3x (0.33 rate) - Balanced approach

    Usage:
        adaptive = AdaptiveCompressor()
        result = adaptive.compress(context)  # Auto-detects type
        result = adaptive.compress(context, content_type=ContentType.CODE)
    """

    def __init__(
        self,
        compressor: ContextCompressor | None = None,
        entity_preserver: CodeEntityPreserver | None = None,
    ):
        """
        Initialize adaptive compressor.

        Args:
            compressor: ContextCompressor instance. Creates new if None.
            entity_preserver: CodeEntityPreserver for code detection. Creates new if None.
        """
        self.compressor = compressor or ContextCompressor()
        self.entity_preserver = entity_preserver or CodeEntityPreserver()

    def classify_content(self, context: str) -> ContentType:
        """
        Classify content type based on code indicators.

        Uses heuristics to determine if content is primarily:
        - Code: Many function definitions, imports, operators
        - Narrative: Natural language without code patterns
        - Mixed: Some code patterns but also significant prose

        Args:
            context: Text to classify

        Returns:
            ContentType enum value
        """
        if not context:
            return ContentType.NARRATIVE

        code_indicators = [
            "def ",
            "class ",
            "function ",
            "import ",
            "from ",
            "=>",
            "->",
            "::",
            "();",
            "{}",
            "[]",
            "self.",
            "this.",
            "return ",
            "async ",
            "await ",
            "const ",
            "let ",
            "var ",
            "export ",
        ]

        code_score = sum(context.count(ind) for ind in code_indicators)

        # Normalize by length (per 1000 chars)
        density = code_score / max(len(context) / 1000, 1)

        if density > 5:
            return ContentType.CODE
        elif density < 1:
            return ContentType.NARRATIVE
        else:
            return ContentType.MIXED

    def get_rate_for_content(self, content_type: ContentType) -> float:
        """Get compression rate for content type."""
        return COMPRESSION_RATES[content_type]

    def compress(
        self,
        context: str,
        content_type: ContentType | None = None,
        rate_override: float | None = None,
        additional_force_tokens: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Compress with adaptive rate based on content type.

        Args:
            context: Text to compress
            content_type: Override auto-detection. Auto-detects if None.
            rate_override: Override the content-based rate. Uses content rate if None.
            additional_force_tokens: Extra tokens to preserve.

        Returns:
            Compression result dict with added 'content_type' field.
        """
        # Classify content if not provided
        if content_type is None:
            content_type = self.classify_content(context)

        # Determine rate
        rate = rate_override or self.get_rate_for_content(content_type)

        # Build force tokens based on content type
        if content_type == ContentType.CODE:
            # Use full code preservation
            force_tokens = self.entity_preserver.get_force_tokens(context)
        elif content_type == ContentType.MIXED:
            # Use structural tokens but not full entity extraction
            force_tokens = self.entity_preserver.get_structural_tokens()
        else:
            # Narrative: minimal force tokens
            force_tokens = None

        # Add any additional tokens
        if additional_force_tokens:
            if force_tokens is None:
                force_tokens = additional_force_tokens
            else:
                force_tokens = list(force_tokens) + additional_force_tokens

        # Compress
        result = self.compressor.compress(context, rate=rate, force_tokens=force_tokens)

        # Add content type to result
        result["content_type"] = content_type.value

        logger.debug(
            f"AdaptiveCompressor: type={content_type.value}, rate={rate}, "
            f"ratio={result.get('ratio', 1.0):.2f}"
        )

        return result

    def compress_simple(
        self,
        context: str,
        content_type: ContentType | None = None,
    ) -> str:
        """
        Simple adaptive compression returning just the text.

        Args:
            context: Text to compress
            content_type: Override auto-detection

        Returns:
            Compressed text
        """
        result = self.compress(context, content_type=content_type)
        return result["compressed_prompt"]
