"""Tests for the v7 cutover and lazy legacy Python compatibility exports."""

import pytest


class TestDeprecatedToolsRemovedFromMCP:
    """Verify the wire registry is exact v7 while Python adapters stay lazy."""

    @pytest.mark.asyncio
    async def test_exact_v7_tools_exposed(self):
        from daem0nmcp.api.v7.policy import V7_TOOL_LEVELS
        from daem0nmcp.server import mcp

        tools = {t.name for t in await mcp.list_tools()}
        assert tools == set(V7_TOOL_LEVELS)
        assert len(tools) == 75
        assert tools.isdisjoint(
            {
                "commune",
                "consult",
                "inscribe",
                "reflect",
                "understand",
                "govern",
                "explore",
                "maintain",
            }
        )

    @pytest.mark.asyncio
    async def test_deprecated_tools_not_in_mcp(self):
        from daem0nmcp.server import mcp

        tools = {t.name for t in await mcp.list_tools()}
        retired = [
            "get_briefing",
            "remember",
            "recall",
            "context_check",
            "record_outcome",
            "commune",
            "consult",
            "inscribe",
            "reflect",
            "understand",
            "govern",
            "explore",
            "maintain",
        ]
        for name in retired:
            assert name not in tools, (
                f"Retired tool '{name}' should not be in MCP registry"
            )

    def test_deprecated_functions_still_importable(self):
        """Old functions remain importable for workflow dispatchers."""
        from daem0nmcp.server import (
            context_check,
            get_briefing,
            recall,
            record_outcome,
            remember,
        )

        assert callable(get_briefing)
        assert callable(remember)
        assert callable(recall)
        assert callable(context_check)
        assert callable(record_outcome)
