"""Tests for consolidated workflow tools registered in MCP server."""

import pytest


class TestWorkflowToolsRegistered:
    """Verify 8 workflow tools are registered in the MCP server."""

    @pytest.fixture
    async def tool_names(self):
        """Get all registered tool names from the MCP server."""
        from daem0nmcp.server import mcp

        tools = await mcp.list_tools()
        return {t.name for t in tools}

    async def test_commune_tool_registered(self, tool_names):
        assert "commune" in tool_names

    async def test_consult_tool_registered(self, tool_names):
        assert "consult" in tool_names

    async def test_inscribe_tool_registered(self, tool_names):
        assert "inscribe" in tool_names

    async def test_reflect_tool_registered(self, tool_names):
        assert "reflect" in tool_names

    async def test_understand_tool_registered(self, tool_names):
        assert "understand" in tool_names

    async def test_govern_tool_registered(self, tool_names):
        assert "govern" in tool_names

    async def test_explore_tool_registered(self, tool_names):
        assert "explore" in tool_names

    async def test_maintain_tool_registered(self, tool_names):
        assert "maintain" in tool_names

    async def test_all_eight_workflow_tools_registered(self, tool_names):
        """All 8 workflow tools should be present."""
        expected = {
            "commune",
            "consult",
            "inscribe",
            "reflect",
            "understand",
            "govern",
            "explore",
            "maintain",
        }
        missing = expected - tool_names
        assert not missing, f"Missing workflow tools: {missing}"
