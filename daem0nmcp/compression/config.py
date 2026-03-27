"""Compression configuration for LLMLingua-2."""

from dataclasses import dataclass, field


@dataclass
class CompressionConfig:
    """Configuration for context compression."""

    # Threshold: Only compress if context exceeds this token count
    compression_threshold: int = 4000

    # Default compression rate (0.33 = 3x compression)
    default_rate: float = 0.33

    # Model selection: large (better quality) or small (less memory)
    model_name: str = "microsoft/llmlingua-2-xlm-roberta-large-meetingbank"

    # Device: "cuda", "cpu", or "auto"
    device: str = "auto"

    # Base tokens to always preserve (punctuation, structure)
    base_force_tokens: list[str] = field(
        default_factory=lambda: [
            ".",
            "\n",
            "?",
            "!",
            ",",
            ":",  # Punctuation
            "(",
            ")",
            "{",
            "}",
            "[",
            "]",  # Brackets
        ]
    )

    # Whether to drop consecutive duplicate tokens
    drop_consecutive: bool = True
