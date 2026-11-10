"""Deterministic, dependency-free discovery index for MCP tools."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

_TOKEN = re.compile(r"[a-z0-9]+")
# Function words say nothing about which tool is meant, and every written
# description contains them, so they must not count as a match.
_STOP_WORDS = frozenset(
    {
        "a",
        "about",
        "all",
        "also",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "been",
        "being",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "doing",
        "for",
        "from",
        "get",
        "had",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "me",
        "my",
        "no",
        "not",
        "of",
        "on",
        "or",
        "our",
        "should",
        "so",
        "some",
        "such",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "to",
        "too",
        "us",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    }
)
# A term in the tool's name, title or tags outweighs one in its description.
_NAME_WEIGHT = 2.0


def _stem(token: str) -> str:
    """Fold plural and verb endings so "deleted" finds "delete"."""

    if len(token) > 4 and token.endswith("ies"):
        token = token[:-3] + "y"
    else:
        for suffix in ("ing", "ed", "es", "s"):
            if token.endswith(suffix) and len(token) - len(suffix) >= 3:
                token = token[: -len(suffix)]
                break
    if len(token) > 3 and token.endswith("e"):
        token = token[:-1]
    return token


def _raw_terms(value: str) -> frozenset[str]:
    return frozenset(_stem(token) for token in _TOKEN.findall(value.casefold()))


def _tokens(value: str) -> frozenset[str]:
    """Return the stemmed content words of ``value``."""

    return frozenset(
        _stem(token)
        for token in _TOKEN.findall(value.casefold())
        if token not in _STOP_WORDS
    )


def _document_id(name: str) -> int:
    """Return a stable positive SQLite-compatible identifier for ``name``."""

    digest = hashlib.sha256(name.encode("utf-8", errors="strict")).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFF_FFFF_FFFF_FFFF


@dataclass
class ToolMetadata:
    """Metadata for an MCP tool."""

    name: str
    description: str
    category: str | None = None
    tags: list[str] = field(default_factory=list)
    parameters: dict | None = None
    examples: list[str] = field(default_factory=list)
    title: str | None = None


def _searchable(tool: ToolMetadata) -> str:
    return " ".join(
        (
            tool.name,
            tool.title or "",
            tool.description,
            *tool.tags,
            *tool.examples,
        )
    )


class ToolSearchIndex:
    """Small deterministic search index derived from the registered manifest."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolMetadata] = {}
        self._documents: dict[int, frozenset[str]] = {}
        self._name_terms: dict[int, frozenset[str]] = {}
        self._doc_to_name: dict[int, str] = {}

    @property
    def document_ids(self) -> Mapping[str, int]:
        """Expose a read-only, stable name-to-document-id mapping for audits."""

        return MappingProxyType(
            {name: _document_id(name) for name in sorted(self._tools)}
        )

    def add_tool(self, tool: ToolMetadata) -> None:
        """Add or atomically replace a tool in the search index."""

        if not isinstance(tool.name, str) or not tool.name.strip():
            raise ValueError("tool name must be a non-empty string")
        if not isinstance(tool.description, str):
            raise ValueError("tool description must be a string")

        name = tool.name
        doc_id = _document_id(name)
        owner = self._doc_to_name.get(doc_id)
        if owner is not None and owner != name:
            raise ValueError("stable tool document identifier collision")

        if tool.title is not None and not isinstance(tool.title, str):
            raise ValueError("tool title must be a string")

        self._tools[name] = tool
        self._documents[doc_id] = _raw_terms(_searchable(tool))
        self._name_terms[doc_id] = _raw_terms(
            " ".join((name, tool.title or "", *tool.tags))
        )
        self._doc_to_name[doc_id] = name

    def remove_tool(self, name: str) -> None:
        """Remove a tool and every corresponding search document."""

        if name not in self._tools:
            return
        doc_id = _document_id(name)
        del self._tools[name]
        self._documents.pop(doc_id, None)
        self._name_terms.pop(doc_id, None)
        self._doc_to_name.pop(doc_id, None)

    def search(
        self, query: str, top_k: int = 10, category: str | None = None
    ) -> list[ToolMetadata]:
        """Return deterministic matches for a natural-language query.

        Each query word that a tool's text contains scores by how rare the
        word is among the indexed tools, doubled when the tool's name, title
        or tags contain it.  Function words are ignored unless the query has
        nothing else, and plural and verb endings are folded.
        """

        if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k <= 0:
            raise ValueError("top_k must be a positive integer")
        if not self._tools:
            return []
        query_tokens = _tokens(query) or _raw_terms(query)
        if not query_tokens:
            return []
        normalized_query = " ".join(_TOKEN.findall(query.casefold()))

        total = len(self._documents)
        weights = {
            token: math.log(
                1.0
                + total
                / sum(token in document for document in self._documents.values())
            )
            for token in sorted(query_tokens)
            if any(token in document for document in self._documents.values())
        }

        ranked: list[tuple[float, int, str, ToolMetadata]] = []
        for doc_id, document_tokens in self._documents.items():
            name = self._doc_to_name[doc_id]
            tool = self._tools[name]
            if category is not None and tool.category != category:
                continue
            matched = sorted(weights.keys() & document_tokens)
            if not matched:
                continue
            name_terms = self._name_terms[doc_id]
            score = sum(
                weights[token] * (_NAME_WEIGHT if token in name_terms else 1.0)
                for token in matched
            )
            phrase = int(normalized_query in _searchable(tool).casefold())
            ranked.append((-round(score, 9), -phrase, name, tool))

        ranked.sort(key=lambda item: item[:3])
        return [item[3] for item in ranked[:top_k]]

    def get_tool(self, name: str) -> ToolMetadata | None:
        """Get a specific tool by name."""

        return self._tools.get(name)

    def get_categories(self) -> list[str]:
        """Get all currently represented tool categories."""

        return sorted(
            {
                tool.category
                for tool in self._tools.values()
                if tool.category is not None
            }
        )

    def get_tools_by_category(self, category: str) -> list[ToolMetadata]:
        """Get tools in a category in deterministic name order."""

        return [
            self._tools[name]
            for name in sorted(self._tools)
            if self._tools[name].category == category
        ]

    def __len__(self) -> int:
        return len(self._tools)
