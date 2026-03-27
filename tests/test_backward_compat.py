"""Tests for backward compatibility — deprecated tools removed from MCP, accessible as Python functions."""

import pytest


class TestDeprecatedToolsRemovedFromMCP:
    """Verify deprecated individual tools are NOT exposed as MCP tools (v6.0+).

    Individual tools (get_briefing, remember, recall, etc.) are replaced by
    workflow tools (commune, consult, inscribe, etc.). The old functions remain
    importable as Python for use by workflow dispatchers, but are not MCP tools.
    """

    @pytest.mark.asyncio
    async def test_workflow_tools_exposed(self):
        from daem0nmcp.server import mcp

        tools = {t.name for t in await mcp.list_tools()}
        for wf in (
            "commune",
            "consult",
            "inscribe",
            "reflect",
            "understand",
            "govern",
            "explore",
            "maintain",
        ):
            assert wf in tools, f"Workflow tool '{wf}' missing from MCP registry"

    @pytest.mark.asyncio
    async def test_deprecated_tools_not_in_mcp(self):
        from daem0nmcp.server import mcp

        tools = {t.name for t in await mcp.list_tools()}
        deprecated = [
            "get_briefing",
            "remember",
            "recall",
            "context_check",
            "record_outcome",
        ]
        for name in deprecated:
            assert name not in tools, (
                f"Deprecated tool '{name}' should not be in MCP registry"
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
