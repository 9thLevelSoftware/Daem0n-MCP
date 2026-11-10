"""Gateway registration for inspectable, framework-neutral v7 surfaces."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from pydantic import ValidationError

from .models import (
    ApiError,
    ApiResponse,
    ErrorCode,
    ErrorRemedy,
    FieldError,
    ResponseMeta,
)
from .registry import PINNED_TOOL_NAMES, V7Manifest
from .responses import ResponseFactory
from .tools import TOOL_INPUT_MODELS, ToolCallData, ToolDescriptor, ToolsSearchData

GATEWAY_TOOL_NAMES = frozenset({"daem0n_tools_search", "daem0n_tool_call"})
CORE_LISTED_TOOLS = PINNED_TOOL_NAMES | {"edit_preflight"} | GATEWAY_TOOL_NAMES
_DIAGNOSTIC_FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_.\[\]-]*")


class _ToolArgumentValidationError(ValidationError):
    """Identify validation failures at the target adapter's input boundary."""


def declared_argument_names(schema: object) -> frozenset[str]:
    """Every property name an input schema declares, at any depth."""

    names: set[str] = set()
    pending = [schema]
    while pending:
        node = pending.pop()
        if isinstance(node, Mapping):
            properties = node.get("properties")
            if isinstance(properties, Mapping):
                names.update(name for name in properties if isinstance(name, str))
            pending.extend(node.values())
        elif isinstance(node, (list, tuple)):
            pending.extend(node)
    return frozenset(names)


def _argument_field(
    location: tuple[object, ...], declared: frozenset[str] | None = None
) -> str:
    """Render where an argument failed, as far as the schema names the path.

    A key of a free-form map, or the name of an unexpected field, is chosen
    by the caller.  With ``declared`` the path stops at the first such name,
    so the text holds list positions and names the schema itself declares.
    """

    field = "arguments"
    for part in location:
        if declared is not None and not isinstance(part, int) and part not in declared:
            break
        candidate = f"{field}.{part}"
        if len(candidate) > 256 or _DIAGNOSTIC_FIELD.fullmatch(candidate) is None:
            break
        field = candidate
    return field


MAX_REPORTED_ARGUMENT_PROBLEMS = 10
# Failures looked at before giving up on finding further distinct ones: many
# bad entries of one map all reduce to the same reported path.
MAX_EXAMINED_ARGUMENT_PROBLEMS = 100


def argument_rejection_text(problems: list[tuple[str, str]]) -> str:
    """Name the offending fields and the violated rule, never the value."""

    distinct = list(dict.fromkeys(problems))
    shown = distinct[:MAX_REPORTED_ARGUMENT_PROBLEMS]
    listed = "; ".join(f"{field} ({rule})" for field, rule in shown)
    more = "; and more" if len(distinct) > len(shown) else ""
    return (
        f"Input validation error: {listed}{more}. "
        "Compare the arguments with the tool's input schema."
    )


def model_argument_problems(
    exc: ValidationError, declared: frozenset[str]
) -> list[tuple[str, str]]:
    """Reduce a pydantic failure to field paths and its fixed error types."""

    problems = dict.fromkeys(
        (_argument_field(tuple(error["loc"]), declared), str(error["type"]))
        for error in exc.errors(
            include_url=False, include_context=False, include_input=False
        )[:MAX_EXAMINED_ARGUMENT_PROBLEMS]
    )
    return list(problems)[: MAX_REPORTED_ARGUMENT_PROBLEMS + 1]


def build_gateway_operations(
    response_factory: ResponseFactory,
) -> Mapping[str, Callable[..., object]]:
    """Register inspectable placeholders; the MCP server binds live handlers."""

    async def tools_search(**arguments: Any) -> ApiResponse[Any]:
        return response_factory.begin(None).failure(
            ErrorCode.CAPABILITY_DISABLED,
            "The tool gateway is available only on an MCP server.",
        )

    async def tool_call(**arguments: Any) -> ApiResponse[Any]:
        return response_factory.begin(None).failure(
            ErrorCode.CAPABILITY_DISABLED,
            "The tool gateway is available only on an MCP server.",
        )

    return {
        "daem0n_tools_search": tools_search,
        "daem0n_tool_call": tool_call,
    }


def bind_gateway_handlers(
    *,
    manifest: V7Manifest,
    adapters: Mapping[str, Callable[..., Awaitable[dict[str, Any]]]],
    listed_tools: frozenset[str] | None,
    response_factory: ResponseFactory,
) -> dict[str, Callable[..., Awaitable[ApiResponse[Any]]]]:
    """Bind discovery and single-pass invocation to this server's adapters."""
    from .factory import build_tool_search_index

    index = build_tool_search_index(manifest)
    specs = {spec.name: spec for spec in manifest.tools}
    declared_names = {
        name: declared_argument_names(spec.input_schema) for name, spec in specs.items()
    }

    async def tools_search(*, query: str, limit: int = 5) -> ApiResponse[Any]:
        response = response_factory.begin(None)
        matches = [
            match
            for match in index.search(query, top_k=limit + 2)
            if match.name not in GATEWAY_TOOL_NAMES
        ][:limit]
        descriptors = []
        for match in matches:
            spec = specs[match.name]
            descriptors.append(
                ToolDescriptor(
                    name=spec.name,
                    description=spec.description,
                    category=spec.category,
                    covenant=spec.covenant.value,
                    read_only=spec.annotations["readOnlyHint"],
                    destructive=spec.annotations["destructiveHint"],
                    idempotent=spec.annotations["idempotentHint"],
                    open_world=spec.annotations["openWorldHint"],
                    listed=listed_tools is None or spec.name in listed_tools,
                    input_schema=spec.input_schema,
                )
            )
        return response.success(ToolsSearchData(tools=descriptors))

    async def tool_call(
        *, workspace_id: str, tool: str, arguments: dict[str, Any]
    ) -> ApiResponse[Any]:
        response = response_factory.begin(workspace_id)
        if tool in GATEWAY_TOOL_NAMES or tool not in adapters:
            return response.failure(
                ErrorCode.INVALID_ARGUMENT,
                "Unknown tool; use daem0n_tools_search.",
                field_errors=[
                    FieldError(
                        field="tool",
                        code="UNKNOWN_TOOL",
                        message="Unknown or non-callable tool.",
                    )
                ],
            )
        if "workspace_id" in arguments:
            return response.failure(
                ErrorCode.INVALID_ARGUMENT,
                "Pass workspace_id at the top level.",
                field_errors=[
                    FieldError(
                        field="arguments.workspace_id",
                        code="WORKSPACE_ID_TOP_LEVEL",
                        message="Pass workspace_id at the top level.",
                    )
                ],
            )
        target = dict(arguments)
        if "workspace_id" in TOOL_INPUT_MODELS[tool].model_fields:
            target["workspace_id"] = workspace_id
        try:
            result = await adapters[tool](**target)
        except _ToolArgumentValidationError as exc:
            field_errors = [
                FieldError(
                    field=_argument_field(error["loc"], declared_names[tool]),
                    code="INVALID_VALUE",
                    message="Invalid value.",
                )
                for error in exc.errors()[:50]
            ]
            return response.failure(
                ErrorCode.INVALID_ARGUMENT,
                "Arguments do not match the tool's input schema; "
                "see daem0n_tools_search.",
                field_errors=field_errors,
            )
        except Exception as exc:
            return response.internal_error(exc)

        try:
            ok = result["ok"]
            if not isinstance(ok, bool):
                raise ValueError("adapter response ok must be boolean")
            if ok:
                if result.get("data") is None or result.get("error") is not None:
                    raise ValueError("adapter response must contain only data")
            elif result.get("data") is not None or result.get("error") is None:
                raise ValueError("adapter response must contain only error")
            meta = ResponseMeta.model_validate(result["meta"])
            response = response.with_covenant(meta.covenant)
            if ok:
                return response.success(
                    ToolCallData(tool=tool, data=result["data"]),
                    warnings=meta.warnings,
                    capability_states=meta.capability_states,
                )
            error = ApiError.model_validate(result["error"])
            remedy = error.remedy
            if (
                remedy is not None
                and listed_tools is not None
                and remedy.tool not in listed_tools
            ):
                remedy = ErrorRemedy(
                    tool="daem0n_tool_call",
                    arguments={
                        "workspace_id": workspace_id,
                        "tool": remedy.tool,
                        "arguments": {
                            key: value
                            for key, value in remedy.arguments.items()
                            if key != "workspace_id"
                        },
                    },
                )
            return response.relay_failure(
                error,
                remedy=remedy,
                warnings=meta.warnings,
                capability_states=meta.capability_states,
            )
        except Exception as exc:
            return response.internal_error(exc)

    return {
        "daem0n_tools_search": tools_search,
        "daem0n_tool_call": tool_call,
    }


__all__ = [
    "CORE_LISTED_TOOLS",
    "GATEWAY_TOOL_NAMES",
    "MAX_EXAMINED_ARGUMENT_PROBLEMS",
    "MAX_REPORTED_ARGUMENT_PROBLEMS",
    "argument_rejection_text",
    "bind_gateway_handlers",
    "build_gateway_operations",
    "declared_argument_names",
    "model_argument_problems",
]
