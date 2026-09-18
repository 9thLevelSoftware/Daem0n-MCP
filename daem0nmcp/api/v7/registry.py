"""Framework-neutral manifest and inspectable v7 MCP registry."""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel

from ... import __version__
from ...covenant import CovenantLevel
from .policy import V7_TOOL_LEVELS

PINNED_TOOL_NAMES = frozenset(
    {
        "session_brief",
        "memory_preflight",
        "memory_recall",
        "memory_store",
        "memory_record_outcome",
        "system_health",
    }
)
FULL_RESOURCE_URI_TEMPLATES = frozenset(
    {
        "memory://workspaces/{workspace_id}/warnings",
        "memory://workspaces/{workspace_id}/failures",
        "memory://workspaces/{workspace_id}/rules",
        "memory://workspaces/{workspace_id}/active-context",
    }
)
V7_DASHBOARD_RESOURCE_URIS = frozenset(
    {
        "ui://daem0n/test",
        "ui://daem0n/search",
        "ui://daem0n/briefing",
        "ui://daem0n/covenant",
        "ui://daem0n/community",
        "ui://daem0n/graph",
    }
)
FULL_V7_RESOURCE_URI_TEMPLATES = (
    FULL_RESOURCE_URI_TEMPLATES | V7_DASHBOARD_RESOURCE_URIS
)
LEGACY_TOOL_NAMES = frozenset(
    {
        "commune",
        "consult",
        "inscribe",
        "reflect",
        "understand",
        "govern",
        "explore",
        "maintain",
        "simulate_decision",
        "evolve_rule",
        "debate_internal",
    }
)
TASK_MODES = frozenset({"forbidden", "optional"})
ANNOTATION_KEYS = frozenset(
    {
        "readOnlyHint",
        "destructiveHint",
        "idempotentHint",
        "openWorldHint",
    }
)
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,79}$")


class ManifestError(ValueError):
    """Raised when the v7 registry authority is incomplete or unsafe."""


def _frozen_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(value))


def _model_schema(model: type[BaseModel]) -> dict[str, Any]:
    try:
        schema = model.model_json_schema()
    except Exception as exc:  # pragma: no cover - defensive adapter boundary
        raise ManifestError("model schema generation failed") from exc
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ManifestError("tool models must have object-shaped schemas")
    return schema


def _contains_integer_type(schema: Any) -> bool:
    if isinstance(schema, dict):
        if schema.get("type") == "integer":
            return True
        return any(_contains_integer_type(value) for value in schema.values())
    if isinstance(schema, list):
        return any(_contains_integer_type(value) for value in schema)
    return False


def _validate_public_schema(schema: dict[str, Any], *, input_schema: bool) -> None:
    def visit(node: Any) -> None:
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                for name, child in properties.items():
                    if name == "project_path":
                        raise ManifestError("project_path is forbidden on v7 schemas")
                    if (name.endswith("_id") or name.endswith("_ids")) and (
                        _contains_integer_type(child)
                    ):
                        raise ManifestError("integer public ID is forbidden")
                    visit(child)
            for key, value in node.items():
                if key != "properties":
                    visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(schema)
    if input_schema and schema.get("additionalProperties") is not False:
        raise ManifestError("v7 input models must forbid additional properties")


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One complete, immutable MCP tool registration record."""

    name: str
    description: str
    handler: Callable[..., Any]
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    category: str
    tags: tuple[str, ...]
    covenant: CovenantLevel
    task_mode: Literal["forbidden", "optional"]
    annotations: Mapping[str, bool]
    pinned: bool = False
    version: str = __version__
    _meta: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not _NAME_RE.fullmatch(self.name):
            raise ManifestError("tool name is invalid")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ManifestError("tool description is required")
        if not callable(self.handler):
            raise ManifestError("tool handler is missing")
        if not (
            isinstance(self.input_model, type)
            and issubclass(self.input_model, BaseModel)
            and isinstance(self.output_model, type)
            and issubclass(self.output_model, BaseModel)
        ):
            raise ManifestError("tool input and output models are required")
        if not self.category or not self.tags:
            raise ManifestError("tool category and tags are required")
        if not isinstance(self.covenant, CovenantLevel):
            raise ManifestError("tool Covenant level is invalid")
        if self.task_mode not in TASK_MODES:
            raise ManifestError("tool task mode is invalid")
        annotations = dict(self.annotations)
        if set(annotations) != ANNOTATION_KEYS or any(
            type(value) is not bool for value in annotations.values()
        ):
            raise ManifestError("tool annotations are incomplete")
        object.__setattr__(self, "annotations", _frozen_mapping(annotations))
        object.__setattr__(self, "tags", tuple(self.tags))
        custom_meta = dict(self._meta)
        reserved = {
            "daem0nmcp/apiVersion",
            "daem0nmcp/pinned",
            "daem0nmcp/category",
            "daem0nmcp/covenant",
            "daem0nmcp/taskMode",
        }
        if reserved & set(custom_meta):
            raise ManifestError("reserved v7 metadata cannot be overridden")
        object.__setattr__(self, "_meta", _frozen_mapping(custom_meta))

    @property
    def meta(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "daem0nmcp/apiVersion": "7",
                "daem0nmcp/pinned": self.pinned,
                "daem0nmcp/category": self.category,
                "daem0nmcp/covenant": self.covenant.value,
                "daem0nmcp/taskMode": self.task_mode,
                **self._meta,
            }
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return _model_schema(self.input_model)

    @property
    def output_schema(self) -> dict[str, Any]:
        return _model_schema(self.output_model)

    def replace(self, **changes: Any) -> ToolSpec:
        return replace(self, **changes)


@dataclass(frozen=True, slots=True)
class ResourceSpec:
    """One immutable v7 resource-template registration record."""

    uri_template: str
    name: str
    description: str
    handler: Callable[..., Any]
    output_model: type[BaseModel] | None
    mime_type: str = "application/json"
    version: str = "7"
    requires_workspace: bool = True

    def __post_init__(self) -> None:
        if self.requires_workspace:
            if not self.uri_template.startswith("memory://workspaces/{workspace_id}/"):
                raise ManifestError("v7 workspace resource URI template is invalid")
        elif self.uri_template not in V7_DASHBOARD_RESOURCE_URIS:
            raise ManifestError("v7 static resource URI template is invalid")
        if not self.name or not self.description or not callable(self.handler):
            raise ManifestError("resource metadata and handler are required")
        if self.mime_type == "application/json" and not (
            isinstance(self.output_model, type)
            and issubclass(self.output_model, BaseModel)
        ):
            raise ManifestError("resource output model is required")
        if self.mime_type != "application/json" and self.output_model is not None:
            raise ManifestError("static resource output model is invalid")
        if self.mime_type not in {"application/json", "text/html;profile=mcp-app"}:
            raise ManifestError("v7 resource MIME type is invalid")
        if self.version != "7" or type(self.requires_workspace) is not bool:
            raise ManifestError("v7 resource metadata is invalid")


@dataclass(frozen=True, slots=True)
class V7Manifest:
    """The single authority for the complete public v7 MCP registry."""

    tools: tuple[ToolSpec, ...]
    resources: tuple[ResourceSpec, ...]
    policy: Mapping[str, CovenantLevel]
    require_full_surface: bool = True

    def __post_init__(self) -> None:
        tools = tuple(self.tools)
        resources = tuple(self.resources)
        policy = dict(self.policy)
        names = [tool.name for tool in tools]
        if len(names) != len(set(names)):
            raise ManifestError("duplicate v7 tool name")
        if LEGACY_TOOL_NAMES & set(names):
            raise ManifestError("legacy tool names are forbidden on v7")
        if set(names) != set(policy):
            raise ManifestError("tool and Covenant policy sets differ")
        for tool in tools:
            if policy[tool.name] is not tool.covenant:
                raise ManifestError("tool and Covenant policy levels differ")
            input_schema = tool.input_schema
            output_schema = tool.output_schema
            _validate_public_schema(input_schema, input_schema=True)
            _validate_public_schema(output_schema, input_schema=False)
        resource_uris = [resource.uri_template for resource in resources]
        if len(resource_uris) != len(set(resource_uris)):
            raise ManifestError("duplicate v7 resource URI template")
        if self.require_full_surface:
            pinned = {tool.name for tool in tools if tool.pinned}
            if pinned != PINNED_TOOL_NAMES:
                raise ManifestError(
                    "full v7 manifest must have exactly six pinned tools"
                )
            if set(names) != set(V7_TOOL_LEVELS):
                raise ManifestError("full v7 manifest tool set is incomplete")
            if set(resource_uris) != FULL_V7_RESOURCE_URI_TEMPLATES:
                raise ManifestError("full v7 manifest resource set is incomplete")
        object.__setattr__(self, "tools", tools)
        object.__setattr__(self, "resources", resources)
        object.__setattr__(self, "policy", _frozen_mapping(policy))

    @property
    def tool_map(self) -> Mapping[str, ToolSpec]:
        return MappingProxyType({tool.name: tool for tool in self.tools})

    @property
    def resource_map(self) -> Mapping[str, ResourceSpec]:
        return MappingProxyType(
            {resource.uri_template: resource for resource in self.resources}
        )


class InspectableV7Server:
    """Dependency-free adapter used for deterministic registry conformance."""

    def __init__(self, manifest: V7Manifest) -> None:
        self.manifest = manifest
        self._tools = manifest.tool_map
        self._resources = manifest.resource_map

    @property
    def tool_names(self) -> frozenset[str]:
        return frozenset(self._tools)

    @property
    def resource_templates(self) -> frozenset[str]:
        return frozenset(self._resources)

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "inputSchema": spec.input_schema,
                "outputSchema": spec.output_schema,
                "annotations": dict(spec.annotations),
                "tags": list(spec.tags),
                "version": spec.version,
                "_meta": dict(spec.meta),
            }
            for spec in self.manifest.tools
        ]

    def list_resource_templates(self) -> list[dict[str, Any]]:
        return [
            {
                "uriTemplate": spec.uri_template,
                "name": spec.name,
                "description": spec.description,
                "mimeType": spec.mime_type,
                "_meta": {"daem0nmcp/apiVersion": spec.version},
            }
            for spec in self.manifest.resources
        ]

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Any:
        try:
            spec = self._tools[name]
        except KeyError as exc:
            raise KeyError("Unknown tool") from exc
        request = spec.input_model.model_validate(dict(arguments))
        result = spec.handler(**request.model_dump())
        if inspect.isawaitable(result):
            result = await result
        return spec.output_model.model_validate(result)

    async def read_resource(self, uri_template: str, **arguments: Any) -> Any:
        try:
            spec = self._resources[uri_template]
        except KeyError as exc:
            raise KeyError("Unknown resource") from exc
        result = spec.handler(**arguments)
        if inspect.isawaitable(result):
            result = await result
        if spec.output_model is None:
            if not isinstance(result, str):
                raise ManifestError("static resource handler returned invalid output")
            return result
        return spec.output_model.model_validate(result)


__all__ = [
    "InspectableV7Server",
    "FULL_RESOURCE_URI_TEMPLATES",
    "FULL_V7_RESOURCE_URI_TEMPLATES",
    "LEGACY_TOOL_NAMES",
    "ManifestError",
    "PINNED_TOOL_NAMES",
    "ResourceSpec",
    "ToolSpec",
    "V7Manifest",
    "V7_DASHBOARD_RESOURCE_URIS",
]
