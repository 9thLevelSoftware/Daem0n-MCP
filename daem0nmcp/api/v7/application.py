"""Policy-first adapter from strict v7 requests to injected business operations."""

from __future__ import annotations

import asyncio
import inspect
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from pydantic import ValidationError

from ...covenant import CovenantGate, InvocationScope
from ...workspace import Workspace
from .errors import DATABASE_IN_USE_RETRY_AFTER_MS, ErrorCode
from .models import CapabilityState
from .policy import V7_TOOL_LEVELS
from .responses import ResponseFactory
from .tasks import (
    durable_task_execution_var,
    is_durable_task_execution,
    task_admission_only_var,
)
from .tools import TOOL_DATA_MODELS, TOOL_INPUT_MODELS


class WorkspaceResolver(Protocol):
    def resolve(self, workspace_id: str) -> Workspace | Any: ...


class ToolOperation(Protocol):
    def __call__(
        self,
        *,
        workspace: Workspace,
        request: AdmittedRequest,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class AdmittedRequest:
    """Validated effective arguments with capability material removed."""

    tool_name: str
    _arguments: Mapping[str, Any]

    def __getattr__(self, name: str) -> Any:
        try:
            return self._arguments[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def model_dump(self) -> dict[str, Any]:
        return dict(self._arguments)


@dataclass(frozen=True, slots=True)
class V7ApplicationDependencies:
    workspace_resolver: WorkspaceResolver
    covenant_gate: CovenantGate
    scope_provider: Callable[[], InvocationScope | None]
    operations: Mapping[str, ToolOperation]
    response_factory: ResponseFactory


_VIOLATION_MESSAGES: Mapping[str, str] = MappingProxyType(
    {
        "UNAUTHORIZED_WORKSPACE": "The requested workspace is unavailable.",
        "COMMUNION_REQUIRED": "A session briefing is required.",
        "COUNSEL_REQUIRED": "A bound preflight capability is required.",
        "IDENTITY_UNAVAILABLE": "An authenticated invocation identity is required.",
        "UNKNOWN_COVENANT_OPERATION": "The operation is not in the v7 policy.",
        "TOKEN_MISSING": "The issued preflight capability was not supplied.",
        "TOKEN_TAMPERED": "The preflight capability is invalid.",
        "TOKEN_EXPIRED": "The preflight capability has expired.",
        "TOKEN_SCOPE_MISMATCH": "The preflight capability scope does not match.",
        "TOKEN_OPERATION_MISMATCH": "The preflight capability targets another tool.",
        "TOKEN_ARGUMENT_MISMATCH": "The effective arguments do not match preflight.",
        "TOKEN_REPLAYED": "The preflight capability has already been consumed.",
        "TOKEN_LEGACY_UNSUPPORTED": "Legacy capability tokens are unsupported.",
        "PREFLIGHT_TARGET_NOT_PROTECTED": "The preflight target is not protected.",
    }
)

_DURABLE_VALIDATION_TOKEN = "durable.task.validation"


async def _resolve(value: object) -> object:
    if inspect.isawaitable(value):
        return await value
    return value


def _canonical_root(workspace: Workspace) -> str:
    return os.path.normcase(str(Path(workspace.root).resolve()))


class V7ToolRouter:
    """Build direct-call-safe handlers without importing any legacy decorators."""

    def __init__(self, dependencies: V7ApplicationDependencies) -> None:
        self._dependencies = dependencies
        unexpected = set(dependencies.operations) - set(V7_TOOL_LEVELS)
        if unexpected:
            raise ValueError("operation registry contains an unknown v7 tool")

    def _remedy(
        self,
        code: str,
        tool_name: str,
        effective: Mapping[str, Any],
    ) -> tuple[str | None, dict[str, Any]]:
        workspace_id = effective.get("workspace_id")
        if code == "COMMUNION_REQUIRED" and isinstance(workspace_id, str):
            return "session_brief", {"workspace_id": workspace_id}
        if code in {"COUNSEL_REQUIRED", "TOKEN_MISSING"} and isinstance(
            workspace_id, str
        ):
            return self._preflight_remedy(workspace_id, tool_name, effective)
        return None, {}

    @staticmethod
    def _preflight_remedy(
        workspace_id: str,
        tool_name: str,
        effective: Mapping[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        target_arguments = {
            key: value
            for key, value in effective.items()
            if key not in {"workspace_id", "preflight_token"}
        }
        return "memory_preflight", {
            "workspace_id": workspace_id,
            "target_tool": tool_name,
            "target_arguments": target_arguments,
        }

    def handler(self, tool_name: str) -> Callable[..., Any]:
        try:
            input_model = TOOL_INPUT_MODELS[tool_name]
        except KeyError as exc:
            raise ValueError("unknown v7 tool") from exc

        async def invoke(**arguments: Any) -> object:
            # Durable admission persists only normalized arguments and an
            # authorization receipt; it deliberately never persists the
            # bearer preflight.  Recognize the exact worker-installed context
            # before adding a syntax-only value for strict model validation.
            # A caller-supplied value always follows the ordinary gate below.
            candidate_arguments = dict(arguments)
            trusted_durable = (
                "preflight_token" not in candidate_arguments
                and is_durable_task_execution(tool_name, candidate_arguments)
            )
            if trusted_durable and "preflight_token" in input_model.model_fields:
                candidate_arguments["preflight_token"] = _DURABLE_VALIDATION_TOKEN
            try:
                validated = input_model.model_validate(candidate_arguments)
            except (TypeError, ValueError, ValidationError):
                return self._dependencies.response_factory.begin(None).failure(
                    ErrorCode.INVALID_ARGUMENT,
                    "Request arguments are invalid.",
                )
            effective = validated.model_dump(mode="json")
            workspace_id = effective.get("workspace_id")
            response = self._dependencies.response_factory.begin(workspace_id)
            if not isinstance(workspace_id, str):
                return response.failure(
                    ErrorCode.UNAUTHORIZED_WORKSPACE,
                    "Workspace unavailable.",
                )
            try:
                workspace = await _resolve(
                    self._dependencies.workspace_resolver.resolve(workspace_id)
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                return response.failure(
                    ErrorCode.UNAUTHORIZED_WORKSPACE,
                    "Workspace unavailable.",
                )
            if (
                not isinstance(workspace, Workspace)
                or workspace.workspace_id != workspace_id
            ):
                return response.failure(
                    ErrorCode.UNAUTHORIZED_WORKSPACE,
                    "Workspace unavailable.",
                )

            durable_execution = trusted_durable and is_durable_task_execution(
                tool_name, effective
            )
            if durable_execution:
                execution = durable_task_execution_var.get()
                if execution is None or not execution.transport_session_id:
                    return response.failure(
                        ErrorCode.IDENTITY_UNAVAILABLE,
                        _VIOLATION_MESSAGES["IDENTITY_UNAVAILABLE"],
                    )
                durable_scope = InvocationScope(
                    execution.principal_id,
                    execution.transport_session_id,
                    _canonical_root(workspace),
                )
                if not self._dependencies.covenant_gate.workspace_authorized(
                    durable_scope
                ):
                    return response.failure(
                        ErrorCode.UNAUTHORIZED_WORKSPACE,
                        _VIOLATION_MESSAGES["UNAUTHORIZED_WORKSPACE"],
                    )
            scope = self._dependencies.scope_provider()
            if (
                not durable_execution
                and scope is not None
                and scope.canonical_workspace != _canonical_root(workspace)
            ):
                return response.failure(
                    ErrorCode.TOKEN_SCOPE_MISMATCH,
                    _VIOLATION_MESSAGES["TOKEN_SCOPE_MISMATCH"],
                )
            token = effective.get("preflight_token")
            operation = self._dependencies.operations.get(tool_name)
            admission_only = task_admission_only_var.get()
            violation = None
            if not durable_execution:
                violation = self._dependencies.covenant_gate.authorize(
                    tool_name,
                    effective,
                    scope,
                    preflight_token=token if isinstance(token, str) else None,
                    consume_capability=(operation is not None and not admission_only),
                )
            if violation is not None:
                code = str(violation.get("violation", "INTERNAL_ERROR"))
                if code not in _VIOLATION_MESSAGES:
                    return response.internal_error()
                remedy_tool, remedy_arguments = self._remedy(
                    code,
                    tool_name,
                    effective,
                )
                return response.failure(
                    ErrorCode(code),
                    _VIOLATION_MESSAGES[code],
                    remedy_tool=remedy_tool,
                    remedy_arguments=remedy_arguments,
                )
            if operation is None:
                capability = CapabilityState(
                    name=tool_name,
                    status="disabled",
                    reason_code="CAPABILITY_NOT_CONFIGURED",
                    remediation="Enable the reviewed capability profile.",
                )
                return response.failure(
                    ErrorCode.CAPABILITY_DISABLED,
                    "Capability is disabled.",
                    capability_states=(capability,),
                )
            if admission_only and not durable_execution:
                capability = CapabilityState(
                    name="tasks",
                    status="disabled",
                    reason_code="TASKS_UNAVAILABLE",
                    remediation=(
                        "Upgrade to a reviewed task-acceptance framework seam."
                    ),
                )
                return response.failure(
                    ErrorCode.TASKS_UNAVAILABLE,
                    "Task execution is unavailable.",
                    capability_states=(capability,),
                )

            sanitized = MappingProxyType(
                {
                    key: value
                    for key, value in effective.items()
                    if key != "preflight_token"
                }
            )
            request = AdmittedRequest(tool_name, sanitized)
            try:
                result = operation(workspace=workspace, request=request)
                result = await _resolve(result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error_code = getattr(exc, "code", None)
                if isinstance(error_code, str) and error_code in {
                    item.value for item in ErrorCode
                }:
                    states = getattr(exc, "capability_states", ())
                    if not isinstance(states, (tuple, list)) or len(states) > 16:
                        states = ()
                    if any(not isinstance(state, CapabilityState) for state in states):
                        states = ()
                    if error_code == ErrorCode.DATABASE_IN_USE.value:
                        # The gate already spent any preflight token, so a
                        # retry needs a fresh one.
                        workspace_id = effective.get("workspace_id")
                        remedy_tool, remedy_arguments = (
                            self._preflight_remedy(workspace_id, tool_name, effective)
                            if isinstance(token, str) and isinstance(workspace_id, str)
                            else (None, {})
                        )
                        return response.failure(
                            ErrorCode.DATABASE_IN_USE,
                            "The workspace database is currently in use.",
                            retryable=True,
                            retry_after_ms=DATABASE_IN_USE_RETRY_AFTER_MS,
                            remedy_tool=remedy_tool,
                            remedy_arguments=remedy_arguments,
                            capability_states=tuple(states),
                        )
                    return response.failure(
                        ErrorCode(error_code),
                        "The operation could not be completed.",
                        capability_states=tuple(states),
                    )
                return response.internal_error(exc)
            try:
                result = TOOL_DATA_MODELS[tool_name].model_validate(result)
            except (TypeError, ValueError, ValidationError):
                return response.internal_error()
            return response.success(result)

        invoke.__name__ = f"v7_handler_{tool_name}"
        invoke.__qualname__ = invoke.__name__
        invoke.__dict__["__daem0nmcp_admission_aware__"] = True
        operation = self._dependencies.operations.get(tool_name)
        if (
            getattr(
                operation,
                "__daem0nmcp_sync_fallback_safe__",
                False,
            )
            is True
        ):
            invoke.__dict__["__daem0nmcp_sync_fallback_safe__"] = True
        return invoke

    def handlers(
        self,
        *,
        exclude: frozenset[str] = frozenset(),
    ) -> Mapping[str, Callable[..., Any]]:
        unknown = set(exclude) - set(V7_TOOL_LEVELS)
        if unknown:
            raise ValueError("excluded handler is not a v7 tool")
        return MappingProxyType(
            {
                name: self.handler(name)
                for name in sorted(V7_TOOL_LEVELS)
                if name not in exclude
            }
        )


__all__ = [
    "AdmittedRequest",
    "V7ApplicationDependencies",
    "V7ToolRouter",
]
