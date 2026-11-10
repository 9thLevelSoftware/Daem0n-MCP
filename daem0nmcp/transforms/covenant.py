"""FastMCP boundary for action-aware Sacred Covenant enforcement."""

from __future__ import annotations

import json
import logging
import os
import secrets
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..covenant import (
    COUNSEL_TTL_SECONDS,
    COVENANT_POLICY,
    ArgumentNormalizationError,
    CovenantGate,
    CovenantLevel,
    CovenantStateStore,
    CovenantViolation,
    InvocationScope,
    UnknownCovenantOperation,
    admitted_call_var,
    authority_from_environment,
    covenant_gate_var,
    invocation_scope_var,
    workspace_resolver_var,
)
from ..covenant import _Admission as Admission

logger = logging.getLogger(__name__)

client_meta_var: ContextVar[dict[str, Any] | None] = ContextVar(
    "client_meta_var", default=None
)
_PROCESS_PRINCIPAL = secrets.token_urlsafe(24)

try:
    from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
    from fastmcp.tools import ToolResult
    from mcp import types as mt

    _FASTMCP_MIDDLEWARE_AVAILABLE = True
except ImportError:
    _FASTMCP_MIDDLEWARE_AVAILABLE = False
    Middleware = object  # type: ignore[assignment]
    MiddlewareContext = Any  # type: ignore[assignment,misc]
    CallNext = Any  # type: ignore[assignment,misc]

    @dataclass
    class ToolResult:  # type: ignore[no-redef]
        structured_content: dict[str, Any]


def _default_workspace_resolver(selector: str | None) -> str:
    from ..context_manager import workspace_registry

    return str(workspace_registry.resolve(selector).root)


def _as_workspace_root(value: Any) -> str:
    root = getattr(value, "root", value)
    return str(Path(root).resolve())


class CovenantTransform:
    """Compatibility facade that resolves only the authoritative policy."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.policy = COVENANT_POLICY

    def check_tool_access(
        self,
        tool_name: str,
        project_path: str | None = None,
        get_state: Callable[[str | None], dict[str, Any] | None] | None = None,
        *,
        action: str | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        operation = f"{tool_name}.{action}" if action else tool_name
        try:
            level = self.policy.resolve(operation, arguments)
        except (ArgumentNormalizationError, UnknownCovenantOperation):
            return CovenantViolation.build(
                "UNKNOWN_COVENANT_OPERATION"
                if operation not in self.policy.operations
                else "TOKEN_ARGUMENT_MISMATCH",
                operation,
                project_path,
            )
        if level is CovenantLevel.EXEMPT:
            return None
        return CovenantViolation.build("IDENTITY_UNAVAILABLE", operation, project_path)

    def __repr__(self) -> str:
        return f"CovenantTransform(operations={len(self.policy.operations)})"


class CovenantMiddleware(Middleware if _FASTMCP_MIDDLEWARE_AVAILABLE else object):
    """Resolve and authorize every consolidated workflow before dispatch."""

    def __init__(
        self,
        get_state: Callable[[str | None], dict[str, Any] | None] | None = None,
        counsel_ttl_seconds: int = COUNSEL_TTL_SECONDS,
        exempt_tools: set[str] | None = None,
        communion_required_tools: set[str] | None = None,
        counsel_required_tools: set[str] | None = None,
        dream_scheduler: Any | None = None,
        *,
        gate: CovenantGate | None = None,
        scope_provider: Callable[[Any, str], InvocationScope | None] | None = None,
        workspace_resolver: Callable[[str | None], Any] | None = None,
        transport_mode: str = "remote",
        access_token_provider: Callable[[], Any] | None = None,
    ) -> None:
        if _FASTMCP_MIDDLEWARE_AVAILABLE:
            super().__init__()
        self._dream_scheduler = dream_scheduler
        self._scope_provider = scope_provider
        self._workspace_resolver = workspace_resolver or _default_workspace_resolver
        self._transport_mode = transport_mode
        self._access_token_provider = access_token_provider
        self._stdio_session_id: str | None = None
        self._gate_injected = gate is not None
        self._gate = gate or self._build_gate(local_stdio=transport_mode == "stdio")

    @staticmethod
    def _build_gate(*, local_stdio: bool) -> CovenantGate | None:
        authority = authority_from_environment(local_stdio=local_stdio)
        if authority is None:
            return None
        return CovenantGate(state_store=CovenantStateStore(), authority=authority)

    @property
    def gate(self) -> CovenantGate | None:
        return self._gate

    @property
    def client_name(self) -> None:
        """Deprecated diagnostic property; clientInfo is never an identity."""
        return None

    def configure_transport(self, transport_mode: str) -> None:
        self._transport_mode = transport_mode
        self._stdio_session_id = None
        if not self._gate_injected:
            self._gate = self._build_gate(local_stdio=transport_mode == "stdio")

    def set_dream_scheduler(self, scheduler: Any) -> None:
        self._dream_scheduler = scheduler

    async def on_initialize(
        self,
        context: MiddlewareContext[mt.InitializeRequest],
        call_next: CallNext[mt.InitializeRequest, mt.InitializeResult | None],
    ) -> mt.InitializeResult | None:
        result = await call_next(context)
        if self._transport_mode == "stdio":
            self._stdio_session_id = secrets.token_urlsafe(24)
        return result

    def _resolve_workspace(self, selector: str | None) -> str:
        return _as_workspace_root(self._workspace_resolver(selector))

    def _scope_for(self, context: Any, workspace: str) -> InvocationScope | None:
        if self._scope_provider is not None:
            scope = self._scope_provider(context, workspace)
            if scope is None:
                return None
            if scope.canonical_workspace != os.path.normcase(
                str(Path(workspace).resolve())
            ):
                return None
            return scope
        if self._transport_mode == "stdio" and self._stdio_session_id:
            return InvocationScope(
                _PROCESS_PRINCIPAL,
                self._stdio_session_id,
                workspace,
            )
        if self._transport_mode != "remote":
            return None
        try:
            provider = self._access_token_provider
            if provider is None:
                from fastmcp.server.dependencies import get_access_token

                provider = get_access_token
            access_token = provider()
            fastmcp_context = context.fastmcp_context
            if fastmcp_context.request_context is None:
                return None
            session_id = fastmcp_context.session_id
        except Exception:
            return None
        if not isinstance(session_id, str) or not session_id.strip():
            return None
        claims = getattr(access_token, "claims", None)
        if not isinstance(claims, dict):
            return None
        subject = claims.get("sub")
        if isinstance(subject, str) and subject.strip():
            principal = f"oauth-sub:{subject.strip()}"
        else:
            client_id = getattr(access_token, "client_id", None)
            if not isinstance(client_id, str) or not client_id.strip():
                return None
            principal = f"oauth-client:{client_id.strip()}"
        return InvocationScope(
            principal,
            f"mcp-session:{session_id.strip()}",
            workspace,
        )

    @staticmethod
    def _parse_client_meta(raw_meta: Any) -> dict[str, Any] | None:
        if raw_meta is None:
            return None
        try:
            parsed = json.loads(raw_meta) if isinstance(raw_meta, str) else raw_meta
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _tool_result(violation: dict[str, Any]) -> ToolResult:
        return ToolResult(structured_content=violation)

    @staticmethod
    def _call_succeeded(result: Any) -> bool:
        if isinstance(result, dict):
            return "error" not in result and result.get("status") != "blocked"
        structured = getattr(result, "structured_content", None)
        return not (
            isinstance(structured, dict)
            and ("error" in structured or structured.get("status") == "blocked")
        )

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        if self._dream_scheduler is not None:
            self._dream_scheduler.notify_tool_call()

        workflow = context.message.name
        original_arguments = context.message.arguments or {}
        arguments = dict(original_arguments)
        action = arguments.get("action")
        operation = f"{workflow}.{action}" if isinstance(action, str) else workflow
        try:
            level = COVENANT_POLICY.resolve(operation, arguments)
        except (ArgumentNormalizationError, UnknownCovenantOperation):
            return self._tool_result(
                CovenantViolation.build(
                    "UNKNOWN_COVENANT_OPERATION"
                    if operation not in COVENANT_POLICY.operations
                    else "TOKEN_ARGUMENT_MISMATCH",
                    operation,
                    None,
                )
            )

        try:
            workspace = self._resolve_workspace(arguments.get("project_path"))
        except (OSError, RuntimeError, ValueError) as exc:
            code = getattr(exc, "code", "UNAUTHORIZED_WORKSPACE")
            return self._tool_result(CovenantViolation.build(code, operation, None))
        scope = self._scope_for(context, workspace)
        preflight_token = arguments.get("preflight_token")
        if self._gate is None:
            violation = (
                None
                if level is CovenantLevel.EXEMPT
                else CovenantViolation.build(
                    "IDENTITY_UNAVAILABLE", operation, workspace
                )
            )
        else:
            violation = self._gate.authorize(
                operation,
                arguments,
                scope,
                preflight_token=preflight_token,
            )
        if violation is not None:
            logger.info(
                "Covenant blocked %s: %s",
                operation,
                violation.get("violation", "UNKNOWN"),
            )
            return self._tool_result(violation)

        filtered_arguments = {
            key: value
            for key, value in arguments.items()
            if key not in {"_client_meta", "preflight_token"}
        }
        context.message.arguments = filtered_arguments
        admission = None
        if self._gate is not None and scope is not None:
            try:
                fingerprint = self._gate.fingerprint(operation, arguments, scope)
            except ArgumentNormalizationError:
                return self._tool_result(
                    CovenantViolation.build(
                        "TOKEN_ARGUMENT_MISMATCH", operation, workspace
                    )
                )
            except UnknownCovenantOperation:
                return self._tool_result(
                    CovenantViolation.build(
                        "UNKNOWN_COVENANT_OPERATION", operation, workspace
                    )
                )
            admission = Admission(operation, fingerprint)
        meta_token = client_meta_var.set(
            self._parse_client_meta(arguments.get("_client_meta"))
        )
        scope_token = invocation_scope_var.set(scope)
        gate_token = covenant_gate_var.set(self._gate)
        resolver_token = workspace_resolver_var.set(self._workspace_resolver)
        admitted_token = admitted_call_var.set(admission)
        try:
            result = await call_next(context)
            if (
                operation == "commune.briefing"
                and self._gate is not None
                and scope is not None
                and self._call_succeeded(result)
            ):
                self._gate.record_briefing(scope)
            return result
        finally:
            admitted_call_var.reset(admitted_token)
            workspace_resolver_var.reset(resolver_token)
            covenant_gate_var.reset(gate_token)
            invocation_scope_var.reset(scope_token)
            client_meta_var.reset(meta_token)

    def __repr__(self) -> str:
        return (
            f"CovenantMiddleware(transport={self._transport_mode!r}, "
            f"identity={'ready' if self._scope_provider else 'transport-derived'})"
        )
