"""Side-effect-free composition of the complete Daem0nMCP v7 surface."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any

from ...tool_search import ToolMetadata, ToolSearchIndex
from .fastmcp import build_fastmcp_server
from .policy import V7_TOOL_LEVELS
from .registry import InspectableV7Server, ManifestError, V7Manifest
from .resources import ResourceHandlers, build_resource_specs
from .tools import build_tool_specs


def combine_handler_maps(
    *handler_maps: Mapping[str, Callable[..., object]],
) -> Mapping[str, Callable[..., object]]:
    """Merge independently owned handler slices without silent replacement."""

    combined: dict[str, Callable[..., object]] = {}
    for handlers in handler_maps:
        for name, handler in handlers.items():
            if name in combined:
                raise ManifestError(f"duplicate handler: {name}")
            combined[name] = handler
    expected = set(V7_TOOL_LEVELS)
    missing = expected - set(combined)
    unexpected = set(combined) - expected
    if missing or unexpected:
        raise ManifestError(
            "handler set is incomplete: "
            f"missing={sorted(missing)!r}, unexpected={sorted(unexpected)!r}"
        )
    return MappingProxyType(combined)


def build_v7_manifest(
    handler_map: Mapping[str, Callable[..., object]],
    resource_handlers: ResourceHandlers,
) -> V7Manifest:
    """Build the one immutable authority for tools, resources, and policy."""

    return V7Manifest(
        tools=build_tool_specs(handler_map),
        resources=build_resource_specs(resource_handlers),
        policy=V7_TOOL_LEVELS,
    )


def build_tool_search_index(manifest: V7Manifest) -> ToolSearchIndex:
    """Derive local convenience search documents from the manifest only."""

    index = ToolSearchIndex()
    for spec in manifest.tools:
        index.add_tool(
            ToolMetadata(
                name=spec.name,
                title=spec.title or None,
                description=spec.description,
                category=spec.category,
                tags=list(spec.tags),
                parameters=spec.input_schema,
            )
        )
    return index


def build_inspectable_v7_server(
    handler_map: Mapping[str, Callable[..., object]],
    resource_handlers: ResourceHandlers,
) -> InspectableV7Server:
    """Build the dependency-free conformance adapter over the same manifest."""

    return InspectableV7Server(build_v7_manifest(handler_map, resource_handlers))


def build_v7_server(
    handler_map: Mapping[str, Callable[..., object]],
    resource_handlers: ResourceHandlers,
    **fastmcp_options: Any,
) -> Any:
    """Create a fresh FastMCP instance; no decorator import mutates it later."""

    manifest = build_v7_manifest(handler_map, resource_handlers)
    return build_fastmcp_server(manifest, **fastmcp_options)


__all__ = [
    "combine_handler_maps",
    "build_inspectable_v7_server",
    "build_tool_search_index",
    "build_v7_manifest",
    "build_v7_server",
]
