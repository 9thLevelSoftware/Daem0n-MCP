"""
Daem0n MCP Apps UI Module

Provides visual UI resources for MCP Apps hosts (SEP-1865).

Exports:
    MCP_APPS_MIME: MIME type for MCP Apps HTML resources
    register_ui_resources: Register UI resources with FastMCP
    format_with_ui_hint: Wrap results with UI hint and text fallback
    format_search_results: Format search results as text
    format_briefing: Format briefing as text
"""

from .fallback import (
    format_briefing,
    format_community_cluster,
    format_covenant_status,
    format_graph_path,
    format_search_results,
    format_with_ui_hint,
)
from .resources import MCP_APPS_MIME, register_ui_resources

__all__ = [
    "MCP_APPS_MIME",
    "register_ui_resources",
    "format_with_ui_hint",
    "format_search_results",
    "format_briefing",
    "format_covenant_status",
    "format_community_cluster",
    "format_graph_path",
]
