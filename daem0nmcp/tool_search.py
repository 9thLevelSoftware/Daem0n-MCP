# daem0nmcp/tool_search.py
"""
Tool Search Index - Dynamic tool discovery for MCP.

Inspired by Anthropic's MCP Tool Search feature.
Reduces context bloat by allowing clients to search for relevant tools
instead of loading all tool definitions upfront.
"""

from dataclasses import dataclass, field

from .bm25_index import BM25Index


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
    """
    Search index for MCP tools.

    Enables dynamic tool discovery:
    1. Tools register with metadata
    2. Clients search by natural language query
    3. Only relevant tools are loaded into context

    Expected context savings: 85% for large tool libraries.
    """

    def __init__(self):
        self._tools: dict[str, ToolMetadata] = {}
        self._bm25 = BM25Index()
        self._categories: set[str] = set()

    def add_tool(self, tool: ToolMetadata) -> None:
        """Add a tool to the search index."""
        self._tools[tool.name] = tool

        # Build searchable text
        search_text = f"{tool.name} {tool.description}"
        if tool.tags:
            search_text += " " + " ".join(tool.tags)
        if tool.examples:
            search_text += " " + " ".join(tool.examples)

        # Use tool name as doc_id (hash for BM25)
        doc_id = hash(tool.name) & 0x7FFFFFFF  # Positive int
        self._bm25.add_document(doc_id, search_text, tags=tool.tags)

        if tool.category:
            self._categories.add(tool.category)

    def remove_tool(self, name: str) -> None:
        """Remove a tool from the index."""
        if name in self._tools:
            doc_id = hash(name) & 0x7FFFFFFF
            self._bm25.remove_document(doc_id)
            del self._tools[name]

    def search(
        self, query: str, top_k: int = 10, category: str | None = None
    ) -> list[ToolMetadata]:
        """
        Search for tools matching the query.

        Args:
            query: Natural language search query
            top_k: Maximum results
            category: Optional category filter

        Returns:
            List of matching ToolMetadata objects
        """
        if not self._tools:
            return []

        results = self._bm25.search(query, top_k=top_k * 2)

        # Map back to tools
        matched_tools = []
        for doc_id, _score in results:
            for name, tool in self._tools.items():
                if (hash(name) & 0x7FFFFFFF) == doc_id:
                    if category is None or tool.category == category:
                        matched_tools.append(tool)
                    break

        return matched_tools[:top_k]

    def get_tool(self, name: str) -> ToolMetadata | None:
        """Get a specific tool by name."""
        return self._tools.get(name)

    def get_categories(self) -> list[str]:
        """Get all tool categories."""
        return sorted(self._categories)

    def get_tools_by_category(self, category: str) -> list[ToolMetadata]:
        """Get all tools in a category."""
        return [tool for tool in self._tools.values() if tool.category == category]

    def __len__(self) -> int:
        return len(self._tools)
