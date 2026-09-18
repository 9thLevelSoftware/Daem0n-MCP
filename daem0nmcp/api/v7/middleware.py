"""Transport-derived invocation context for the v7 MCP boundary.

This middleware establishes identity and workspace scope only.  Tool policy
admission remains in the typed v7 handlers, so this layer cannot accidentally
authorize a legacy operation or duplicate one-use capability consumption.
"""

from __future__ import annotations

import inspect
import secrets
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar, cast
from urllib.parse import urlsplit

from pydantic import TypeAdapter

from ...covenant import (
    CovenantGate,
    InvocationScope,
    admitted_call_var,
    covenant_gate_var,
    invocation_scope_var,
    workspace_resolver_var,
)
from ...workspace import Workspace
from .models import WorkspaceId
from .registry import V7_DASHBOARD_RESOURCE_URIS

if TYPE_CHECKING:
    FASTMCP_MIDDLEWARE_AVAILABLE: bool
    from fastmcp.exceptions import ResourceError as _BaseResourceError
    from fastmcp.exceptions import ToolError as _BaseToolError
    from fastmcp.server.middleware import Middleware as _MiddlewareBase
else:
    try:  # Keep the model-free boundary importable without FastMCP.
        from fastmcp.exceptions import ResourceError as _BaseResourceError
        from fastmcp.exceptions import ToolError as _BaseToolError
        from fastmcp.server.middleware import Middleware as _MiddlewareBase

        FASTMCP_MIDDLEWARE_AVAILABLE = True
    except ImportError:
        FASTMCP_MIDDLEWARE_AVAILABLE = False

        class _MiddlewareBase:
            pass

        class _BaseToolError(RuntimeError):
            pass

        class _BaseResourceError(RuntimeError):
            pass


TransportMode = Literal["stdio", "streamable-http"]
RESOURCE_SUFFIXES = frozenset({"warnings", "failures", "rules", "active-context"})
WORKSPACE_OPTIONAL_TOOLS = frozenset({"system_health"})

_WORKSPACE_ID_ADAPTER = TypeAdapter(WorkspaceId)
_ADMISSION_FAILURE = object()
_PROCESS_PRINCIPAL = f"process:{secrets.token_urlsafe(24)}"


class ToolInvocationContextError(_BaseToolError):
    """Sanitized failure to establish a tool invocation scope."""

    def __init__(self) -> None:
        super().__init__("Invocation unavailable")


class ResourceInvocationContextError(_BaseResourceError):
    """Sanitized failure to establish a resource invocation scope."""

    def __init__(self) -> None:
        super().__init__("Resource unavailable")


class ResourceAuthorizationError(_BaseResourceError):
    """Sanitized Communion or exact-scope resource authorization failure."""

    def __init__(self) -> None:
        super().__init__("Resource unavailable")


class WorkspaceResolver(Protocol):
    def resolve(self, workspace_id: str) -> Workspace | Awaitable[Workspace]: ...


R = TypeVar("R")


def _capture(operation: Callable[[], R]) -> R | object:
    """Discard private exception objects before returning a failure sentinel."""

    try:
        return operation()
    except Exception:
        return _ADMISSION_FAILURE


async def _capture_async(operation: Callable[[], Awaitable[R]]) -> R | object:
    """Async capture that deliberately leaves cancellation untouched."""

    try:
        return await operation()
    except Exception:
        return _ADMISSION_FAILURE


async def _resolve_awaitable(value: object) -> object:
    if inspect.isawaitable(value):
        return await value
    return value


def _resource_workspace_id(uri: object) -> WorkspaceId:
    if not isinstance(uri, str):
        uri = str(uri)
    parsed = urlsplit(uri)
    segments = parsed.path.split("/")
    if (
        parsed.scheme != "memory"
        or parsed.netloc != "workspaces"
        or parsed.query
        or parsed.fragment
        or len(segments) != 3
        or segments[0] != ""
        or segments[2] not in RESOURCE_SUFFIXES
    ):
        raise ValueError("resource URI is not a v7 workspace resource")
    return _WORKSPACE_ID_ADAPTER.validate_python(segments[1], strict=True)


class V7InvocationMiddleware(_MiddlewareBase):
    """Install a transport-authenticated scope around v7 tools and resources."""

    def __init__(
        self,
        *,
        gate: CovenantGate,
        workspace_resolver: WorkspaceResolver,
        transport_mode: TransportMode,
        access_token_provider: Callable[[], object] | None = None,
        process_principal: str | None = None,
        session_id_factory: Callable[[], str] | None = None,
        allow_unauthenticated_loopback: bool = False,
        activity_callback: Callable[[Workspace, bool], None] | None = None,
        max_inflight: int = 64,
        max_inflight_per_principal: int = 8,
        max_inflight_per_workspace: int = 16,
    ) -> None:
        if FASTMCP_MIDDLEWARE_AVAILABLE:
            super().__init__()
        if transport_mode not in {"stdio", "streamable-http"}:
            raise ValueError("v7 middleware supports stdio or streamable-http")
        if gate is None:
            raise ValueError("a v7 Covenant gate is required")
        resolver = getattr(workspace_resolver, "resolve", None)
        if not callable(resolver):
            raise ValueError("an explicit workspace resolver is required")
        principal = process_principal or _PROCESS_PRINCIPAL
        if not isinstance(principal, str) or not principal.strip():
            raise ValueError("the process principal must be non-empty")
        if access_token_provider is not None and not callable(access_token_provider):
            raise ValueError("the access-token provider must be callable")
        if activity_callback is not None and not callable(activity_callback):
            raise ValueError("the activity callback must be callable")
        if not isinstance(allow_unauthenticated_loopback, bool):
            raise ValueError("loopback identity policy must be boolean")
        if allow_unauthenticated_loopback and transport_mode != "streamable-http":
            raise ValueError("loopback identity applies only to Streamable HTTP")
        for bound in (
            max_inflight,
            max_inflight_per_principal,
            max_inflight_per_workspace,
        ):
            if type(bound) is not int or bound < 1:
                raise ValueError("admission bounds must be positive integers")

        self._gate = gate
        self._workspace_resolver = workspace_resolver
        self._resolver = resolver
        self._transport_mode = transport_mode
        self._access_token_provider = access_token_provider
        self._activity_callback = activity_callback
        self._activity_workspaces: dict[str, Workspace] = {}
        self._process_principal = principal.strip()
        self._allow_unauthenticated_loopback = allow_unauthenticated_loopback
        self._session_id_factory = session_id_factory or (
            lambda: secrets.token_urlsafe(24)
        )
        self._stdio_session_id: str | None = None
        self._max_inflight = max_inflight
        self._max_inflight_per_principal = max_inflight_per_principal
        self._max_inflight_per_workspace = max_inflight_per_workspace
        self._inflight = 0
        self._principal_inflight: dict[str, int] = {}
        self._workspace_inflight: dict[str, int] = {}
        self._task_scope_resolver: Callable[[str, str, str], InvocationScope] | None = (
            None
        )

    def configure_task_scope_resolver(
        self, resolver: Callable[[str, str, str], InvocationScope]
    ) -> None:
        """Bind lifecycle admission to the owned durable task ledger."""
        if not callable(resolver):
            raise TypeError("task scope resolver must be callable")
        self._task_scope_resolver = resolver

    async def on_request(self, context: Any, call_next: Callable[[Any], Any]) -> Any:
        if context.method not in {
            "tasks/get",
            "tasks/result",
            "tasks/cancel",
            "tasks/list",
        }:
            return await _resolve_awaitable(call_next(context))
        from mcp import McpError
        from mcp.types import INVALID_PARAMS, ErrorData

        try:
            principal, session = await self.identity(context)
        except Exception:
            raise McpError(
                ErrorData(code=INVALID_PARAMS, message="Task not found.")
            ) from None

        async def scoped_call(current: Any) -> Any:
            # Global/principal slots are already held, including during an
            # unknown-ID lookup. Only an authorized ledger row supplies the
            # workspace; callers cannot select a cheaper quota bucket.
            if context.method == "tasks/list":
                return await _resolve_awaitable(call_next(current))
            if self._task_scope_resolver is None:
                raise McpError(
                    ErrorData(code=INVALID_PARAMS, message="Task not found.")
                )
            scope = self._task_scope_resolver(
                context.message.taskId, principal, session
            )
            workspace = scope.canonical_workspace
            count = self._workspace_inflight.get(workspace, 0)
            if count >= self._max_inflight_per_workspace:
                raise _BaseToolError(
                    "ADMISSION_LIMIT_REACHED: retry after an active request completes"
                )
            self._workspace_inflight[workspace] = count + 1
            try:
                return await _resolve_awaitable(call_next(current))
            finally:
                self._workspace_inflight[workspace] -= 1
                if self._workspace_inflight[workspace] == 0:
                    del self._workspace_inflight[workspace]

        try:
            return await self._dispatch(
                context, scoped_call, None, principal_id=principal
            )
        except _BaseToolError:
            raise McpError(
                ErrorData(
                    code=INVALID_PARAMS,
                    message="ADMISSION_LIMIT_REACHED: retry after an active request completes",
                )
            ) from None

    @property
    def stdio_session_id(self) -> str | None:
        return self._stdio_session_id

    async def on_initialize(self, context: Any, call_next: Callable[[Any], Any]) -> Any:
        """Issue a server-owned stdio session only after initialization succeeds."""

        result = call_next(context)
        result = await _resolve_awaitable(result)
        if self._transport_mode == "stdio":
            session_id = self._session_id_factory()
            if not isinstance(session_id, str) or not session_id.strip():
                raise ToolInvocationContextError()
            self._stdio_session_id = session_id.strip()
        return result

    def _default_access_token(self) -> object:
        from fastmcp.server.dependencies import get_access_token

        return get_access_token()

    async def _resolve_workspace(self, workspace_id: WorkspaceId) -> Workspace:
        resolved = self._resolver(workspace_id)
        resolved = await _resolve_awaitable(resolved)
        if not isinstance(resolved, Workspace) or resolved.workspace_id != workspace_id:
            raise ValueError("workspace resolution did not preserve the opaque ID")
        return resolved

    async def _remote_identity(self, context: Any) -> tuple[str, str]:
        fastmcp_context = context.fastmcp_context
        if fastmcp_context.request_context is None:
            raise ValueError("MCP request context is unavailable")
        session_id = fastmcp_context.session_id
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("MCP session is unavailable")

        provider = self._access_token_provider or self._default_access_token
        access_token = provider()
        access_token = await _resolve_awaitable(access_token)
        if access_token is None and self._allow_unauthenticated_loopback:
            return self._process_principal, f"mcp-session:{session_id.strip()}"
        claims = getattr(access_token, "claims", None)
        if not isinstance(claims, Mapping):
            raise ValueError("authenticated OAuth claims are unavailable")
        subject = claims.get("sub")
        if (
            not isinstance(subject, str)
            or not subject.strip()
            or len(subject) > 502
            or any(
                ord(character) < 32 or ord(character) == 127 for character in subject
            )
        ):
            raise ValueError("authenticated OAuth subject is unavailable")
        principal = f"oauth-sub:{subject}"
        return principal, f"mcp-session:{session_id.strip()}"

    async def _scope(self, context: Any, workspace: Workspace) -> InvocationScope:
        principal, session_id = await self.identity(context)
        return InvocationScope(principal, session_id, str(workspace.root))

    async def identity(self, context: Any) -> tuple[str, str]:
        """Return the authenticated principal and current MCP session."""

        if self._transport_mode == "stdio":
            if self._stdio_session_id is None:
                raise ValueError("stdio initialization session is unavailable")
            principal = self._process_principal
            session_id = f"mcp-session:{self._stdio_session_id}"
        else:
            principal, session_id = await self._remote_identity(context)
        return principal, session_id

    async def _admit_tool(self, context: Any) -> InvocationScope | None:
        message = context.message
        name = message.name
        arguments = message.arguments or {}
        if not isinstance(name, str) or not isinstance(arguments, Mapping):
            raise ValueError("tool invocation is malformed")
        raw_workspace_id = arguments.get("workspace_id")
        if raw_workspace_id is None and name in WORKSPACE_OPTIONAL_TOOLS:
            return None
        workspace_id = _WORKSPACE_ID_ADAPTER.validate_python(
            raw_workspace_id,
            strict=True,
        )
        workspace = await self._resolve_workspace(workspace_id)
        scope = await self._scope(context, workspace)
        if self._gate.workspace_authorized(scope):
            self._activity_workspaces[scope.canonical_workspace] = workspace
        return scope

    async def _admit_resource(self, context: Any) -> InvocationScope | None:
        # Fixed UI shells have no data and therefore require neither workspace
        # resolution nor Communion. Any dynamic workspace data still flows
        # through ordinary tool calls and their authenticated admission path.
        resource_uri = str(context.message.uri)
        if resource_uri in V7_DASHBOARD_RESOURCE_URIS:
            return None
        workspace_id = _resource_workspace_id(resource_uri)
        workspace = await self._resolve_workspace(workspace_id)
        return await self._scope(context, workspace)

    async def _dispatch(
        self,
        context: Any,
        call_next: Callable[[Any], Any],
        scope: InvocationScope | None,
        *,
        principal_id: str | None = None,
    ) -> Any:
        # All admission accounting runs on the server event loop before the
        # first await. No waiting queue or permanent per-principal entries grow.
        principal = principal_id if scope is None else scope.principal_id
        workspace = None if scope is None else scope.canonical_workspace
        principal_count = (
            self._principal_inflight.get(principal, 0) if principal is not None else 0
        )
        workspace_count = (
            self._workspace_inflight.get(workspace, 0) if workspace is not None else 0
        )
        if (
            self._inflight >= self._max_inflight
            or principal_count >= self._max_inflight_per_principal
            or workspace_count >= self._max_inflight_per_workspace
        ):
            error_type = (
                _BaseResourceError
                if hasattr(context.message, "uri")
                else _BaseToolError
            )
            raise error_type(
                "ADMISSION_LIMIT_REACHED: retry after an active request completes"
            )
        self._inflight += 1
        if principal is not None:
            self._principal_inflight[principal] = principal_count + 1
        if workspace is not None:
            self._workspace_inflight[workspace] = workspace_count + 1
        scope_token = invocation_scope_var.set(scope)
        gate_token = covenant_gate_var.set(self._gate)
        resolver_token = workspace_resolver_var.set(self._resolver)
        admission_token = admitted_call_var.set(None)
        try:
            result = call_next(context)
            return await _resolve_awaitable(result)
        finally:
            admitted_call_var.reset(admission_token)
            workspace_resolver_var.reset(resolver_token)
            covenant_gate_var.reset(gate_token)
            invocation_scope_var.reset(scope_token)
            self._inflight -= 1
            for key, counts in (
                (principal, self._principal_inflight),
                (workspace, self._workspace_inflight),
            ):
                if key is not None:
                    counts[key] -= 1
                    if counts[key] == 0:
                        del counts[key]

    async def on_call_tool(self, context: Any, call_next: Callable[[Any], Any]) -> Any:
        """Establish tool context without performing handler-level admission."""

        captured = await _capture_async(lambda: self._admit_tool(context))
        if captured is _ADMISSION_FAILURE:
            # Tool handlers own the typed v7 error envelope.  Dispatch with no
            # scope so Covenant admission returns IDENTITY_UNAVAILABLE instead
            # of turning an authentication failure into a framework exception.
            return await self._dispatch(context, call_next, None)
        scope = cast(InvocationScope | None, captured)
        workspace = (
            None
            if scope is None or not self._gate.workspace_authorized(scope)
            else self._activity_workspaces.get(scope.canonical_workspace)
        )
        if workspace is not None and self._activity_callback is not None:
            try:
                self._activity_callback(workspace, True)
            except Exception:
                workspace = None
        try:
            return await self._dispatch(context, call_next, scope)
        finally:
            if workspace is not None and self._activity_callback is not None:
                with suppress(Exception):
                    self._activity_callback(workspace, False)

    async def on_read_resource(
        self,
        context: Any,
        call_next: Callable[[Any], Any],
    ) -> Any:
        """Establish exact workspace context around a v7 resource read."""

        captured = await _capture_async(lambda: self._admit_resource(context))
        if captured is _ADMISSION_FAILURE:
            raise ResourceInvocationContextError()
        return await self._dispatch(
            context,
            call_next,
            cast(InvocationScope | None, captured),
        )


class ResourceCommunionAuthorizer:
    """Require briefing for the exact resource scope installed by middleware."""

    def __init__(self, *, expected_gate: CovenantGate | None = None) -> None:
        self._expected_gate = expected_gate

    def _authorize(self, *, workspace: Workspace, resource_uri: str) -> None:
        scope = invocation_scope_var.get()
        gate = covenant_gate_var.get()
        resolver = workspace_resolver_var.get()
        if scope is None or gate is None or resolver is None:
            raise ValueError("resource invocation context is unavailable")
        if self._expected_gate is not None and gate is not self._expected_gate:
            raise ValueError("resource gate scope does not match")
        workspace_id = _resource_workspace_id(resource_uri)
        if (
            not isinstance(workspace, Workspace)
            or workspace.workspace_id != workspace_id
        ):
            raise ValueError("resource workspace ID does not match")
        candidate = InvocationScope(
            scope.principal_id,
            scope.transport_session_id,
            str(workspace.root),
        )
        if candidate != scope:
            raise ValueError("resource workspace scope does not match")
        if not gate.workspace_authorized(scope):
            raise PermissionError("resource workspace access is unavailable")
        state_store = getattr(gate, "state_store", None)
        if state_store is None or not state_store.is_briefed(scope):
            raise PermissionError("resource Communion is required")

    def authorize(self, *, workspace: Workspace, resource_uri: str) -> None:
        captured = _capture(
            lambda: self._authorize(
                workspace=workspace,
                resource_uri=resource_uri,
            )
        )
        if captured is _ADMISSION_FAILURE:
            raise ResourceAuthorizationError()


__all__ = [
    "FASTMCP_MIDDLEWARE_AVAILABLE",
    "RESOURCE_SUFFIXES",
    "ResourceAuthorizationError",
    "ResourceCommunionAuthorizer",
    "ResourceInvocationContextError",
    "ToolInvocationContextError",
    "TransportMode",
    "V7InvocationMiddleware",
    "WORKSPACE_OPTIONAL_TOOLS",
    "WorkspaceResolver",
]
