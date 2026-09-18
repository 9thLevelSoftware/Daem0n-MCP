"""Static, data-free MCP App shells exposed by the v7 resource manifest.

Dashboard data is deliberately never embedded in a resource URI or read from
an alternate resource route.  A host obtains the dashboard shell here, then
uses the ordinary authenticated v7 tools for the workspace-scoped data it
chooses to display.
"""

from __future__ import annotations

from collections.abc import Callable

from ...ui.rendering import APP_SPECS, MCP_APPS_MIME, render_app_document
from .registry import ResourceSpec

DASHBOARD_RESOURCE_URIS = frozenset(spec.resource_uri for spec in APP_SPECS.values())


def _static_shell(app_id: str) -> Callable[[], str]:
    def read() -> str:
        # Empty input is important: shells carry no workspace or tool data.
        return render_app_document(app_id, {})

    read.__name__ = f"v7_dashboard_{app_id}"
    read.__qualname__ = read.__name__
    return read


def build_dashboard_resource_specs() -> tuple[ResourceSpec, ...]:
    """Build the exact six data-free MCP App shell registrations."""

    return tuple(
        ResourceSpec(
            uri_template=spec.resource_uri,
            name=f"dashboard_{app_id}",
            description=spec.description,
            handler=_static_shell(app_id),
            output_model=None,
            mime_type=MCP_APPS_MIME,
            requires_workspace=False,
        )
        for app_id, spec in APP_SPECS.items()
    )


__all__ = ["DASHBOARD_RESOURCE_URIS", "build_dashboard_resource_specs"]
