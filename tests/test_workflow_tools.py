"""Wire-registry cutover tests for the exact v7 granular tool surface."""

import pytest


class TestV7ToolsRegistered:
    """Verify the eight v6 workflow routers cannot re-enter the wire surface."""

    @pytest.fixture
    async def tool_names(self):
        """Get all registered tool names from the MCP server."""
        from daem0nmcp.server import mcp

        tools = await mcp.list_tools()
        return {t.name for t in tools}

    async def test_exact_v7_tool_set_registered(self, tool_names):
        from daem0nmcp.api.v7.policy import V7_TOOL_LEVELS

        assert tool_names == set(V7_TOOL_LEVELS)
        assert len(tool_names) == 75

    async def test_retired_workflow_routers_are_absent(self, tool_names):
        retired = {
            "commune",
            "consult",
            "inscribe",
            "reflect",
            "understand",
            "govern",
            "explore",
            "maintain",
        }
        assert tool_names.isdisjoint(retired)
