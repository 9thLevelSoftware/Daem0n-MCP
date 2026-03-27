"""
Code Entity Preserver - Token extraction for code preservation.

Extracts important code tokens (function names, class names, keywords)
to use as force_tokens during compression, ensuring code structure
is preserved even with aggressive compression.
"""

import re


class CodeEntityPreserver:
    """
    Extracts code entities to preserve during compression.

    Identifies function names, class names, and structural tokens
    that must be preserved to maintain code meaning after compression.

    Usage:
        preserver = CodeEntityPreserver()
        force_tokens = preserver.get_force_tokens(code)
        compressor.compress(code, force_tokens=force_tokens)
    """

    # Maximum number of entity names to extract (to avoid overwhelming compressor)
    MAX_ENTITY_NAMES: int = 50

    # Language-agnostic structural tokens
    STRUCTURAL_TOKENS: list[str] = [
        # Python keywords
        "def ",
        "class ",
        "return ",
        "import ",
        "from ",
        "async ",
        "await ",
        "if ",
        "else:",
        "elif ",
        "for ",
        "while ",
        "try:",
        "except ",
        "finally:",
        "with ",
        "as ",
        "yield ",
        "raise ",
        "pass",
        "break",
        "continue",
        "lambda ",
        "assert ",
        "global ",
        "nonlocal ",
        "None",
        "True",
        "False",
        # JavaScript/TypeScript keywords
        "function ",
        "const ",
        "let ",
        "var ",
        "export ",
        "import ",
        "default",
        "async ",
        "await ",
        "new ",
        "this.",
        "super",
        "extends ",
        "implements ",
        "interface ",
        "type ",
        "enum ",
        "readonly ",
        "private ",
        "public ",
        "protected ",
        # Control flow (multi-language)
        "switch ",
        "case ",
        "default:",
        "throw ",
        "catch ",
        # Common operators/delimiters
        "=>",
        "->",
        "::",
        "==",
        "!=",
        "===",
        "!==",
        "self.",
        "this.",
    ]

    # Patterns to extract identifiers
    IDENTIFIER_PATTERNS = [
        r"\bdef\s+(\w+)\s*\(",  # Python function
        r"\bclass\s+(\w+)",  # Python/JS class
        r"\bfunction\s+(\w+)\s*\(",  # JS function
        r"\bconst\s+(\w+)\s*=",  # JS const
        r"\blet\s+(\w+)\s*=",  # JS let
        r"\bvar\s+(\w+)\s*=",  # JS var
        r"\b(\w+)\s*:\s*function",  # Object method
        r"\basync\s+def\s+(\w+)",  # Python async function
        r"\basync\s+function\s+(\w+)",  # JS async function
        r"\b(\w+)\s*=\s*async",  # Arrow async
        r"\binterface\s+(\w+)",  # TS interface
        r"\btype\s+(\w+)\s*=",  # TS type alias
        r"\benum\s+(\w+)",  # TS/Java enum
    ]

    def __init__(self, additional_structural: list[str] = None):
        """
        Initialize preserver with optional additional structural tokens.

        Args:
            additional_structural: Extra tokens to always preserve.
        """
        self._structural = list(self.STRUCTURAL_TOKENS)
        if additional_structural:
            self._structural.extend(additional_structural)

    def get_structural_tokens(self) -> list[str]:
        """Get the list of structural tokens to preserve."""
        return list(self._structural)

    def extract_identifiers(self, code: str) -> set[str]:
        """
        Extract function/class/variable names from code.

        Args:
            code: Source code text

        Returns:
            Set of identifier names
        """
        identifiers: set[str] = set()

        for pattern in self.IDENTIFIER_PATTERNS:
            matches = re.findall(pattern, code)
            identifiers.update(matches)

        return identifiers

    def extract_entity_names(self, code: str) -> list[str]:
        """
        Extract function and class names from code, limited to MAX_ENTITY_NAMES.

        Args:
            code: Source code text

        Returns:
            List of entity names (limited to MAX_ENTITY_NAMES)
        """
        identifiers = self.extract_identifiers(code)
        # Sort by length (shorter names are usually more important)
        # and limit to MAX_ENTITY_NAMES
        sorted_names = sorted(identifiers, key=len)
        return sorted_names[: self.MAX_ENTITY_NAMES]

    def get_force_tokens(self, code: str) -> list[str]:
        """
        Get all tokens to force-preserve during compression.

        Combines structural tokens with extracted identifiers.

        Args:
            code: Source code text

        Returns:
            List of tokens to preserve
        """
        tokens = list(self._structural)
        tokens.extend(self.extract_entity_names(code))
        return tokens

    def is_code_heavy(self, text: str) -> bool:
        """
        Determine if text is code-heavy (vs. narrative).

        Uses heuristics based on code indicator density.
        Code-heavy text should get conservative compression.

        Args:
            text: Text to analyze

        Returns:
            True if text is code-heavy
        """
        if not text:
            return False

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

        code_score = sum(text.count(ind) for ind in code_indicators)

        # Normalize by length (per 1000 chars)
        density = code_score / max(len(text) / 1000, 1)

        # Threshold: more than 5 code indicators per 1000 chars
        return density > 5
