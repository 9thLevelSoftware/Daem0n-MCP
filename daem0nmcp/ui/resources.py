"""Thin backward-compatible builders and FastMCP UI resource adapters."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .rendering import (
    APP_SPECS,
    MCP_APPS_MIME,
    parse_compat_payload,
    render_app_document,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP


def _build_test_ui() -> str:
    return render_app_document("test", {})


def _build_search_ui(data: dict[str, Any]) -> str:
    return render_app_document("search", data)


def _build_briefing_ui(data: dict[str, Any]) -> str:
    return render_app_document("briefing", data)


def _build_covenant_ui(data: dict[str, Any]) -> str:
    return render_app_document("covenant", data)


def _build_community_ui(data: dict[str, Any]) -> str:
    return render_app_document("community", data)


def _build_graph_ui(data: dict[str, Any]) -> str:
    return render_app_document("graph", data)


_BUILDERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "test": lambda _data: _build_test_ui(),
    "search": _build_search_ui,
    "briefing": _build_briefing_ui,
    "covenant": _build_covenant_ui,
    "community": _build_community_ui,
    "graph": _build_graph_ui,
}


def _static_handler(app_id: str) -> Callable[[], str]:
    def handle() -> str:
        return _BUILDERS[app_id]({})

    handle.__name__ = f"get_{app_id}_ui"
    return handle


def _compat_handler(app_id: str) -> Callable[[str], str]:
    def handle(data: str) -> str:
        return _BUILDERS[app_id](parse_compat_payload(data))

    handle.__name__ = f"get_{app_id}_compat_ui"
    return handle


def register_ui_resources(mcp: FastMCP) -> None:
    """Register stable base resources and bounded compatibility templates."""
    for app_id, spec in APP_SPECS.items():
        mcp.resource(
            uri=spec.resource_uri,
            name=spec.resource_name,
            description=spec.description,
            mime_type=MCP_APPS_MIME,
        )(_static_handler(app_id))
        if app_id != "test":
            mcp.resource(
                uri=f"{spec.resource_uri}/{{data}}",
                name=f"{spec.resource_name} (compatibility data)",
                description=spec.description,
                mime_type=MCP_APPS_MIME,
            )(_compat_handler(app_id))


__all__ = [
    "MCP_APPS_MIME",
    "register_ui_resources",
    "_build_test_ui",
    "_build_search_ui",
    "_build_briefing_ui",
    "_build_covenant_ui",
    "_build_community_ui",
    "_build_graph_ui",
]
