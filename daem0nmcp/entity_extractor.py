"""
Entity Extractor - Auto-extract entities from memory content.

Extracts:
- Function names: foo_bar(), fooBar()
- Class names: PascalCase words
- File paths: paths with extensions
- Concepts: Key domain terms
"""

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# Patterns for entity extraction
PATTERNS = {
    # Function calls: word followed by parentheses
    "function": re.compile(r"\b([a-z_][a-z0-9_]*)\s*\(", re.IGNORECASE),
    # Class names: PascalCase words (2+ capital letters or Capital followed by lowercase)
    "class": re.compile(r"\b([A-Z][a-z]+(?:[A-Z][a-z]+)+|[A-Z]{2,}[a-z]+)\b"),
    # File paths: word.ext or path/word.ext
    "file": re.compile(r"(?:[\w./\\-]+/)?[\w.-]+\.[a-z]{1,4}\b"),
    # Module imports: from x import y, import x
    "module": re.compile(r"(?:from\s+|import\s+)([\w.]+)"),
    # Variable-like references: snake_case in backticks or quotes
    "variable": re.compile(r'[`\'"]([a-z_][a-z0-9_]*)[`\'"]'),
}

# Words to ignore (common false positives)
STOP_WORDS = {
    "the",
    "and",
    "for",
    "with",
    "use",
    "get",
    "set",
    "add",
    "new",
    "this",
    "that",
    "from",
    "have",
    "been",
    "will",
    "can",
    "should",
    "def",
    "class",
    "return",
    "import",
    "if",
    "else",
    "elif",
    "true",
    "false",
    "none",
    "null",
    "self",
    "cls",
}


class EntityExtractor:
    """
    Extracts entities from text content using pattern matching.

    Entity types:
    - function: Function or method names
    - class: Class names (PascalCase)
    - file: File paths
    - module: Module/package names
    - variable: Variable references
    """

    def __init__(self, custom_patterns: dict[str, re.Pattern] | None = None):
        self.patterns = {**PATTERNS}
        if custom_patterns:
            self.patterns.update(custom_patterns)

    def extract_entities(self, text: str) -> list[dict[str, Any]]:
        """
        Extract all entities from text.

        Args:
            text: Content to extract entities from

        Returns:
            List of entity dicts with type, name, and context
        """
        if not text:
            return []

        entities = []
        seen = set()  # Dedup by (type, name)

        for entity_type, pattern in self.patterns.items():
            for match in pattern.finditer(text):
                # Get the captured group or full match
                name = match.group(1) if match.groups() else match.group(0)

                # Clean up
                name = name.strip()

                # Skip empty, too short, or stop words
                if not name or len(name) < 2:
                    continue
                if name.lower() in STOP_WORDS:
                    continue

                # Dedup
                key = (entity_type, name.lower())
                if key in seen:
                    continue
                seen.add(key)

                # Get context snippet (50 chars around match)
                start = max(0, match.start() - 25)
                end = min(len(text), match.end() + 25)
                context = "..." + text[start:end] + "..."

                entities.append(
                    {
                        "type": entity_type,
                        "name": name,
                        "context": context,
                        "position": match.start(),
                    }
                )

        return entities

    def extract_concepts(
        self, text: str, min_frequency: int = 1
    ) -> list[dict[str, Any]]:
        """
        Extract domain concepts (key noun phrases).

        Uses simple heuristics:
        - Capitalized phrases (after sentence start)
        - Technical terms (words with underscores, camelCase)
        - Quoted terms

        Args:
            text: Content to analyze
            min_frequency: Minimum occurrences to include

        Returns:
            List of concept entities
        """
        concepts = []

        # Quoted terms
        for match in re.finditer(r'["\']([^"\']+)["\']', text):
            term = match.group(1).strip()
            if len(term) > 2 and len(term) < 50:
                concepts.append(
                    {
                        "type": "concept",
                        "name": term,
                        "context": match.group(0),
                        "position": match.start(),
                    }
                )

        # Technical terms with underscores
        for match in re.finditer(r"\b([A-Z_]+(?:_[A-Z]+)+)\b", text):
            term = match.group(1)
            if len(term) > 3:
                concepts.append(
                    {
                        "type": "concept",
                        "name": term,
                        "context": match.group(0),
                        "position": match.start(),
                    }
                )

        return concepts

    def extract_all(self, text: str) -> list[dict[str, Any]]:
        """
        Extract all entity types including concepts.

        Args:
            text: Content to analyze

        Returns:
            Combined list of all extracted entities
        """
        entities = self.extract_entities(text)
        concepts = self.extract_concepts(text)

        # Combine and deduplicate
        seen = {(e["type"], e["name"].lower()) for e in entities}
        for concept in concepts:
            key = (concept["type"], concept["name"].lower())
            if key not in seen:
                entities.append(concept)
                seen.add(key)

        return sorted(entities, key=lambda e: e["position"])
