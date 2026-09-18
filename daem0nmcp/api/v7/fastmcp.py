"""Pinned FastMCP 3.4.7 adapter for the manifest-owned v7 surface."""

from __future__ import annotations

import inspect
import json
import logging
import operator
from collections.abc import Mapping
from importlib import metadata
from types import MethodType
from typing import Annotated, Any

from pydantic import Field, TypeAdapter

from ... import __version__
from ...covenant import invocation_scope_var
from .errors import ErrorCode
from .middleware import V7InvocationMiddleware
from .registry import ToolSpec, V7Manifest
from .responses import ResponseFactory
from .task_dispatcher import DurableTaskDispatcher, TaskDispatcherError, TaskView
from .tasks import (
    FOREGROUND_EXECUTION_POLICIES,
    ForegroundExecutionPolicy,
    TaskExecutionError,
    run_sync_fallback,
    task_admission_only_var,
    validate_sync_timeout_seconds,
)

PINNED_FASTMCP_VERSION = "3.4.7"
_REDACTED_LOG_VALUE = "<redacted>"
_FASTMCP_OPERATION_LOGGER = "fastmcp.server.mixins.mcp_operations"


class FastMCPCompatibilityError(RuntimeError):
    """Raised when the installed framework cannot honor the v7 contract."""


def _redact_log_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: (
                _REDACTED_LOG_VALUE
                if isinstance(key, str) and key.casefold() == "preflight_token"
                else _redact_log_value(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(_redact_log_value(item) for item in value)
    if isinstance(value, list):
        return [_redact_log_value(item) for item in value]
    return value


class _FrameworkArgumentRedactionFilter(logging.Filter):
    """Copy-redact bearer handles from FastMCP's pre-dispatch DEBUG record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.args = _redact_log_value(record.args)
        return True


_FRAMEWORK_ARGUMENT_REDACTION_FILTER = _FrameworkArgumentRedactionFilter()


def _install_framework_log_redaction() -> None:
    logger = logging.getLogger(_FASTMCP_OPERATION_LOGGER)
    if _FRAMEWORK_ARGUMENT_REDACTION_FILTER not in logger.filters:
        logger.addFilter(_FRAMEWORK_ARGUMENT_REDACTION_FILTER)


def ensure_fastmcp_compatibility(version: str) -> None:
    """Require the one framework release covered by protocol conformance."""
    if version != PINNED_FASTMCP_VERSION:
        raise FastMCPCompatibilityError(
            "FastMCP 3.4.7 is required by the MCP v7 wire contract"
        )


def _installed_fastmcp() -> tuple[type[Any], str]:
    try:
        from fastmcp import FastMCP

        version = metadata.version("fastmcp")
    except (ImportError, metadata.PackageNotFoundError) as exc:
        raise FastMCPCompatibilityError("FastMCP 3.4.7 is not installed") from exc
    ensure_fastmcp_compatibility(version)
    return FastMCP, version


def _task_config_class() -> type[Any]:
    try:
        from fastmcp.server.tasks import TaskConfig
    except ImportError as exc:
        raise FastMCPCompatibilityError(
            "FastMCP task support is unavailable; install the reviewed tasks extra"
        ) from exc
    return TaskConfig


def _parameter_default(field: Any) -> Any:
    if field.is_required():
        return inspect.Parameter.empty
    if field.default_factory is not None:
        # Pydantic model schemas intentionally omit a concrete ``default`` for
        # factories.  Preserve that contract in the synthetic callable instead
        # of materializing a mutable value during server construction.
        return inspect.Parameter.empty
    return field.get_default(call_default_factory=True)


def _parameter_annotation(field: Any) -> Any:
    metadata_items = tuple(field.metadata)
    if field.default_factory is not None:
        metadata_items = (
            Field(default_factory=field.default_factory),
            *metadata_items,
        )
    if not metadata_items:
        return field.annotation
    # Starred subscription items are Python 3.11 syntax.  Building the
    # subscription tuple explicitly preserves the same Annotated type on the
    # project's Python 3.10 floor.
    return operator.getitem(Annotated, (field.annotation, *metadata_items))


def _titled_callable_schema(schema: Any, *, title: str) -> Any:
    """Add the model-root title without changing callable validation."""

    if not isinstance(schema, dict):
        raise FastMCPCompatibilityError("callable schema is unavailable")
    result = dict(schema)
    call_schema = result
    if result.get("type") == "definitions":
        nested = result.get("schema")
        if not isinstance(nested, dict):
            raise FastMCPCompatibilityError("callable schema is malformed")
        call_schema = dict(nested)
        result["schema"] = call_schema
    if call_schema.get("type") != "call":
        raise FastMCPCompatibilityError("callable schema is malformed")
    schema_metadata = dict(call_schema.get("metadata") or {})
    updates = dict(schema_metadata.get("pydantic_js_updates") or {})
    updates["title"] = title
    schema_metadata["pydantic_js_updates"] = updates
    call_schema["metadata"] = schema_metadata
    return result


_TASK_FAILURE_MESSAGES = {
    "TASK_REQUIRED": (
        "This request exceeds the bounded foreground profile; reduce its "
        "scope or retry when durable task execution is available."
    ),
    "TASKS_UNAVAILABLE": "Task execution is unavailable.",
    "DEADLINE_EXCEEDED": "The operation exceeded its synchronous deadline.",
    "CANCELLED": "The operation was cancelled.",
}


def _tool_adapter(
    spec: ToolSpec,
    *,
    tasks_enabled: bool,
    sync_timeout_seconds: float,
    foreground_policy: ForegroundExecutionPolicy | None = None,
):
    async def invoke(**arguments: Any) -> dict[str, Any]:
        request = spec.input_model.model_validate(arguments)

        effective_arguments = request.model_dump(mode="json")

        async def execute() -> Any:
            result = spec.handler(**request.model_dump())
            if inspect.isawaitable(result):
                result = await result
            return result

        try:
            if spec.task_mode == "optional":
                if foreground_policy is None:
                    raise FastMCPCompatibilityError(
                        f"missing foreground policy for {spec.name}"
                    )
                estimated_to_fit = foreground_policy.admits(
                    effective_arguments,
                    timeout_seconds=sync_timeout_seconds,
                )
                if not estimated_to_fit:
                    admission = task_admission_only_var.set(True)
                    try:
                        result = await run_sync_fallback(
                            execute,
                            estimated_to_fit=True,
                            timeout_seconds=sync_timeout_seconds,
                        )
                    finally:
                        task_admission_only_var.reset(admission)
                    admitted_response = spec.output_model.model_validate(result)
                    error = getattr(admitted_response, "error", None)
                    if (
                        error is None
                        or getattr(error, "code", None) != ErrorCode.TASKS_UNAVAILABLE
                    ):
                        return admitted_response.model_dump(mode="json")
                    raise TaskExecutionError("TASK_REQUIRED")
            result = await run_sync_fallback(
                execute,
                estimated_to_fit=True,
                timeout_seconds=sync_timeout_seconds,
            )
        except TaskExecutionError as exc:
            workspace_id = getattr(request, "workspace_id", None)
            error_code = exc.code
            result = (
                ResponseFactory()
                .begin(workspace_id)
                .failure(
                    ErrorCode(error_code),
                    _TASK_FAILURE_MESSAGES[error_code],
                )
            )
        response = spec.output_model.model_validate(result)
        error = getattr(response, "error", None)
        if (
            error is not None
            and getattr(error, "code", None) == ErrorCode.TASK_REQUIRED
            and getattr(error, "message", None)
            != _TASK_FAILURE_MESSAGES["TASK_REQUIRED"]
        ):
            response = response.model_copy(
                update={
                    "error": error.model_copy(
                        update={"message": _TASK_FAILURE_MESSAGES["TASK_REQUIRED"]}
                    )
                }
            )
        return response.model_dump(mode="json")

    invoke.__name__ = f"v7_{spec.name}"
    invoke.__qualname__ = invoke.__name__
    invoke.__doc__ = spec.description
    parameters = []
    for name, field in spec.input_model.model_fields.items():
        parameters.append(
            inspect.Parameter(
                name,
                inspect.Parameter.KEYWORD_ONLY,
                default=_parameter_default(field),
                annotation=_parameter_annotation(field),
            )
        )
    signature = inspect.Signature(
        parameters=parameters,
        return_annotation=spec.output_model,
    )
    invoke.__signature__ = signature  # type: ignore[attr-defined]
    # Pydantic's callable schema parser combines ``inspect.signature`` with
    # ``get_type_hints``.  Once we replace the wrapper's **arguments signature,
    # its annotations must describe those same public parameters or FastMCP's
    # FunctionTool construction fails before the server can start.
    invoke.__annotations__ = {
        name: parameter.annotation for name, parameter in signature.parameters.items()
    }
    invoke.__annotations__["return"] = signature.return_annotation
    callable_schema = _titled_callable_schema(
        TypeAdapter(invoke).core_schema,
        title=spec.input_schema["title"],
    )

    def manifest_callable_schema(_source: Any, _handler: Any) -> Any:
        return callable_schema

    # FastMCP 3.4.7 asks Pydantic for the callable schema at registration.
    # Keep its validation as a real call schema while making the advertised
    # root metadata byte-for-byte equal to the manifest-owned input model.
    invoke.__get_pydantic_core_schema__ = manifest_callable_schema  # type: ignore[attr-defined]
    return invoke


def _resource_adapter(spec: Any):
    async def read_workspace(workspace_id: str) -> str:
        result = spec.handler(workspace_id=workspace_id)
        if inspect.isawaitable(result):
            result = await result
        response = spec.output_model.model_validate(result)
        return json.dumps(
            response.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )

    async def read_static() -> str:
        result = spec.handler()
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, str) or len(result.encode("utf-8")) > 2_000_000:
            raise FastMCPCompatibilityError("static resource output is invalid")
        return result

    read = read_workspace if spec.requires_workspace else read_static
    read.__name__ = f"v7_resource_{spec.name}"
    read.__qualname__ = read.__name__
    read.__doc__ = spec.description
    return read


def _mcp_task(view: TaskView) -> Any:
    import mcp.types

    return mcp.types.Task(
        taskId=view.task_id,
        status=view.status,
        statusMessage=view.status_message,
        createdAt=view.created_at,
        lastUpdatedAt=view.updated_at,
        ttl=view.ttl_ms,
        pollInterval=view.poll_interval_ms,
    )


def _task_protocol_error(exc: Exception) -> Exception:
    from mcp import McpError
    from mcp.types import INVALID_PARAMS, ErrorData

    code = getattr(exc, "code", "TASKS_UNAVAILABLE")
    if code == "COMMUNION_REQUIRED":
        message = "A renewed workspace briefing is required for this task."
    elif code in {"TASK_NOT_FOUND", "UNAUTHORIZED_WORKSPACE"}:
        message = "Task not found."
    elif code == "IDEMPOTENCY_REQUIRED":
        message = "Task execution requires replay-safe idempotency."
    elif code == "TASK_QUEUE_FULL":
        message = "Task capacity is unavailable; retry later."
    else:
        message = "Task request is invalid or unavailable."
    return McpError(ErrorData(code=INVALID_PARAMS, message=message))


def _install_task_protocol(
    server: Any,
    dispatcher: DurableTaskDispatcher,
    invocation_middleware: V7InvocationMiddleware,
) -> None:
    """Replace Docket handlers with the credential-free owned dispatcher."""

    import mcp.types
    from fastmcp.server.context import Context
    from fastmcp.server.middleware.middleware import MiddlewareContext

    low_level = server._mcp_server
    original_call = low_level.request_handlers[mcp.types.CallToolRequest]
    invocation_middleware.configure_task_scope_resolver(dispatcher.task_scope)

    async def run_middleware(request: Any, callback: Any) -> Any:
        async with Context(fastmcp=server) as fastmcp_context:
            context = MiddlewareContext(
                message=request.params,
                source="client",
                type="request",
                method=request.method,
                fastmcp_context=fastmcp_context,
            )
            try:
                return await server._run_middleware(context, callback)
            except TaskDispatcherError as exc:
                raise _task_protocol_error(exc) from None

    async def identity(context: Any) -> tuple[str, str]:
        try:
            return await invocation_middleware.identity(context)
        except Exception as exc:
            raise TaskDispatcherError("TASK_NOT_FOUND") from exc

    async def handle_call(request: Any) -> Any:
        if request.params.task is None:
            return await original_call(request)

        async def submit(context: Any) -> Any:
            try:
                view = await dispatcher.submit(
                    context.message.name,
                    context.message.arguments,
                    scope=invocation_scope_var.get(),
                    task_metadata=context.message.task,
                )
            except TaskDispatcherError as exc:
                raise _task_protocol_error(exc) from None
            return mcp.types.ServerResult(
                mcp.types.CreateTaskResult(task=_mcp_task(view))
            )

        return await run_middleware(request, submit)

    async def handle_get(request: Any) -> Any:
        async def get(context: Any) -> Any:
            principal, session = await identity(context)
            try:
                view = await dispatcher.get_task(
                    request.params.taskId,
                    principal_id=principal,
                    transport_session_id=session,
                )
            except TaskDispatcherError as exc:
                raise _task_protocol_error(exc) from None
            return mcp.types.ServerResult(
                mcp.types.GetTaskResult(**_mcp_task(view).model_dump())
            )

        return await run_middleware(request, get)

    async def handle_result(request: Any) -> Any:
        async def result(context: Any) -> Any:
            principal, session = await identity(context)
            try:
                payload = await dispatcher.get_result(
                    request.params.taskId,
                    principal_id=principal,
                    transport_session_id=session,
                )
            except TaskDispatcherError as exc:
                raise _task_protocol_error(exc) from None
            structured_payload = dict(payload)
            is_error = structured_payload.get("ok") is False
            content = [
                mcp.types.TextContent(
                    type="text",
                    text=json.dumps(
                        structured_payload,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                        allow_nan=False,
                    ),
                )
            ]
            return mcp.types.ServerResult(
                mcp.types.GetTaskPayloadResult.model_validate(
                    {
                        "content": content,
                        "structuredContent": structured_payload,
                        "isError": is_error,
                    }
                )
            )

        return await run_middleware(request, result)

    async def handle_cancel(request: Any) -> Any:
        async def cancel(context: Any) -> Any:
            principal, session = await identity(context)
            try:
                view = await dispatcher.cancel(
                    request.params.taskId,
                    principal_id=principal,
                    transport_session_id=session,
                )
            except TaskDispatcherError as exc:
                raise _task_protocol_error(exc) from None
            return mcp.types.ServerResult(
                mcp.types.CancelTaskResult(**_mcp_task(view).model_dump())
            )

        return await run_middleware(request, cancel)

    async def handle_list(request: Any) -> Any:
        async def list_tasks(context: Any) -> Any:
            principal, session = await identity(context)
            try:
                page = await dispatcher.list_tasks(
                    principal_id=principal,
                    transport_session_id=session,
                    cursor=(None if request.params is None else request.params.cursor),
                )
            except TaskDispatcherError as exc:
                raise _task_protocol_error(exc) from None
            return mcp.types.ServerResult(
                mcp.types.ListTasksResult(
                    tasks=[_mcp_task(view) for view in page.tasks],
                    nextCursor=page.next_cursor,
                )
            )

        return await run_middleware(request, list_tasks)

    low_level.request_handlers[mcp.types.CallToolRequest] = handle_call
    low_level.request_handlers[mcp.types.GetTaskRequest] = handle_get
    low_level.request_handlers[mcp.types.GetTaskPayloadRequest] = handle_result
    low_level.request_handlers[mcp.types.CancelTaskRequest] = handle_cancel
    low_level.request_handlers[mcp.types.ListTasksRequest] = handle_list
    _set_task_capabilities(low_level, enabled=True)


def _set_task_capabilities(low_level: Any, *, enabled: bool) -> None:
    """Advertise only the owned task handlers, independent of Docket."""

    import mcp.types

    original = low_level.get_capabilities

    def get_capabilities(
        _self: Any,
        notification_options: Any,
        experimental_capabilities: Any,
    ) -> Any:
        capabilities = original(
            notification_options,
            experimental_capabilities,
        )
        if not enabled:
            return capabilities.model_copy(update={"tasks": None})
        task_capabilities = mcp.types.ServerTasksCapability(
            list=mcp.types.TasksListCapability(),
            cancel=mcp.types.TasksCancelCapability(),
            requests=mcp.types.ServerTasksRequestsCapability(
                tools=mcp.types.TasksToolsCapability(
                    call=mcp.types.TasksCallCapability()
                )
            ),
        )
        return capabilities.model_copy(update={"tasks": task_capabilities})

    low_level.get_capabilities = MethodType(get_capabilities, low_level)


def _disable_upstream_task_protocol(server: Any) -> None:
    """Remove FastMCP's Docket endpoints when no owned dispatcher is active."""

    import mcp.types

    low_level = server._mcp_server
    for request_type in (
        mcp.types.GetTaskRequest,
        mcp.types.GetTaskPayloadRequest,
        mcp.types.CancelTaskRequest,
        mcp.types.ListTasksRequest,
    ):
        low_level.request_handlers.pop(request_type, None)
    _set_task_capabilities(low_level, enabled=False)


def build_fastmcp_server(
    manifest: V7Manifest,
    *,
    fastmcp_cls: type[Any] | None = None,
    distribution_version: str | None = None,
    task_config_cls: type[Any] | None = None,
    tasks_enabled: bool = False,
    task_dispatcher: DurableTaskDispatcher | None = None,
    auth: Any | None = None,
    middleware: tuple[Any, ...] = (),
    lifespan: Any | None = None,
    sync_timeout_seconds: int | float = 15,
    foreground_policies: Mapping[str, ForegroundExecutionPolicy] = (
        FOREGROUND_EXECUTION_POLICIES
    ),
) -> Any:
    """Create a fresh, fail-closed FastMCP instance from one manifest."""
    if fastmcp_cls is None:
        fastmcp_cls, installed_version = _installed_fastmcp()
        version = distribution_version or installed_version
    else:
        if distribution_version is None:
            raise FastMCPCompatibilityError(
                "an injected FastMCP class requires an explicit version"
            )
        version = distribution_version
    ensure_fastmcp_compatibility(version)
    validated_sync_timeout = validate_sync_timeout_seconds(sync_timeout_seconds)
    _install_framework_log_redaction()

    if tasks_enabled != (task_dispatcher is not None):
        raise FastMCPCompatibilityError(
            "owned task support requires exactly one durable dispatcher"
        )

    optional_names = {
        spec.name for spec in manifest.tools if spec.task_mode == "optional"
    }
    configured_policies = dict(foreground_policies)
    missing_policies = optional_names - set(configured_policies)
    if missing_policies:
        raise FastMCPCompatibilityError(
            "missing foreground execution policies: "
            + ", ".join(sorted(missing_policies))
        )
    invalid_policies = {
        name
        for name in optional_names
        if not isinstance(configured_policies.get(name), ForegroundExecutionPolicy)
    }
    if invalid_policies:
        raise FastMCPCompatibilityError(
            "invalid foreground execution policies: "
            + ", ".join(sorted(invalid_policies))
        )
    for spec in manifest.tools:
        if spec.task_mode == "optional" and (
            getattr(spec.handler, "__daem0nmcp_admission_aware__", False) is not True
        ):
            raise FastMCPCompatibilityError(
                f"task-optional handler is not admission-aware: {spec.name}"
            )

    server = fastmcp_cls(
        "Daem0nMCP",
        version=__version__,
        strict_input_validation=True,
        mask_error_details=True,
        # Task eligibility is component-owned.  A server-wide True default
        # would silently make every forbidden tool and resource task-capable.
        tasks=False,
        on_duplicate="error",
        auth=auth,
        lifespan=lifespan,
    )
    for item in middleware:
        server.add_middleware(item)

    for spec in manifest.tools:
        registration: dict[str, Any] = {
            "name": spec.name,
            "description": spec.description,
            "tags": set(spec.tags),
            "annotations": dict(spec.annotations),
            "meta": dict(spec.meta),
            "version": spec.version,
            "output_schema": spec.output_schema,
        }
        registration["task"] = False
        registered = server.tool(**registration)(
            _tool_adapter(
                spec,
                tasks_enabled=tasks_enabled,
                sync_timeout_seconds=validated_sync_timeout,
                foreground_policy=configured_policies.get(spec.name),
            )
        )
        if spec.task_mode == "optional" and tasks_enabled:
            import mcp.types

            # Advertise the standard task mode while retaining FastMCP's
            # forbidden TaskConfig, so its Docket submission path is inert.
            registered.execution = mcp.types.ToolExecution(taskSupport="optional")

    for resource_spec in manifest.resources:
        server.resource(
            resource_spec.uri_template,
            name=resource_spec.name,
            description=resource_spec.description,
            mime_type=resource_spec.mime_type,
            version=resource_spec.version,
            meta={"daem0nmcp/apiVersion": "7"},
            task=False,
        )(_resource_adapter(resource_spec))

    if task_dispatcher is not None:
        invocation_middleware = next(
            (item for item in middleware if isinstance(item, V7InvocationMiddleware)),
            None,
        )
        if invocation_middleware is None:
            raise FastMCPCompatibilityError(
                "durable tasks require the authenticated v7 middleware"
            )
        _install_task_protocol(
            server,
            task_dispatcher,
            invocation_middleware,
        )
    elif hasattr(server, "_mcp_server"):
        _disable_upstream_task_protocol(server)

    return server


__all__ = [
    "FastMCPCompatibilityError",
    "PINNED_FASTMCP_VERSION",
    "build_fastmcp_server",
    "ensure_fastmcp_compatibility",
]
