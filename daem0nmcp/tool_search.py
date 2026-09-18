"""Deterministic, dependency-free discovery index for MCP tools."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(value: str) -> frozenset[str]:
    return frozenset(_TOKEN.findall(value.casefold()))


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


class ToolSearchIndex:
    """Small deterministic search index derived from the registered manifest."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolMetadata] = {}
        self._documents: dict[int, frozenset[str]] = {}
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

        searchable = " ".join(
            (
                name,
                tool.description,
                *tool.tags,
                *tool.examples,
            )
        )
        self._tools[name] = tool
        self._documents[doc_id] = _tokens(searchable)
        self._doc_to_name[doc_id] = name

    def remove_tool(self, name: str) -> None:
        """Remove a tool and every corresponding search document."""

        if name not in self._tools:
            return
        doc_id = _document_id(name)
        del self._tools[name]
        self._documents.pop(doc_id, None)
        self._doc_to_name.pop(doc_id, None)

    def search(
        self, query: str, top_k: int = 10, category: str | None = None
    ) -> list[ToolMetadata]:
        """Return deterministic token-overlap matches for a natural-language query."""

        if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k <= 0:
            raise ValueError("top_k must be a positive integer")
        if not self._tools:
            return []
        query_tokens = _tokens(query)
        if not query_tokens:
            return []
        normalized_query = " ".join(_TOKEN.findall(query.casefold()))

        ranked: list[tuple[int, int, str, ToolMetadata]] = []
        for doc_id, document_tokens in self._documents.items():
            name = self._doc_to_name[doc_id]
            tool = self._tools[name]
            if category is not None and tool.category != category:
                continue
            overlap = len(query_tokens & document_tokens)
            if overlap == 0:
                continue
            searchable = " ".join(
                (name, tool.description, *tool.tags, *tool.examples)
            ).casefold()
            phrase = int(normalized_query in searchable)
            ranked.append((-overlap, -phrase, name, tool))

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
