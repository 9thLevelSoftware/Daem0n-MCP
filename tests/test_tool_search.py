# tests/test_tool_search.py
"""Tests for MCP tool search index."""

from daem0nmcp.tool_search import ToolMetadata, ToolSearchIndex


class TestToolSearchIndex:
    """Test tool search functionality."""

    def test_add_and_search_tool(self):
        index = ToolSearchIndex()
        index.add_tool(
            ToolMetadata(
                name="remember",
                description="Store a memory for later recall",
                category="memory",
                tags=["store", "save", "persist"],
            )
        )
        index.add_tool(
            ToolMetadata(
                name="recall",
                description="Search and retrieve memories",
                category="memory",
                tags=["search", "retrieve", "find"],
            )
        )
        # Add more tools for better BM25 IDF scores
        index.add_tool(
            ToolMetadata(
                name="add_rule", description="Add a decision rule", category="rules"
            )
        )
        index.add_tool(
            ToolMetadata(
                name="check_rules",
                description="Check which rules apply",
                category="rules",
            )
        )

        results = index.search("store memory")
        assert len(results) >= 1
        assert results[0].name == "remember"

    def test_search_by_category(self):
        index = ToolSearchIndex()
        index.add_tool(
            ToolMetadata(
                name="remember", description="Store memory for later", category="memory"
            )
        )
        index.add_tool(
            ToolMetadata(
                name="add_rule",
                description="Add a rule to the system",
                category="rules",
            )
        )
        index.add_tool(
            ToolMetadata(
                name="recall", description="Retrieve stored memories", category="memory"
            )
        )

        results = index.search("memory", category="memory")
        assert all(r.category == "memory" for r in results)

    def test_get_all_categories(self):
        index = ToolSearchIndex()
        index.add_tool(ToolMetadata(name="t1", description="d1", category="a"))
        index.add_tool(ToolMetadata(name="t2", description="d2", category="b"))

        categories = index.get_categories()
        assert set(categories) == {"a", "b"}

    def test_empty_search(self):
        index = ToolSearchIndex()
        results = index.search("anything")
        assert results == []
