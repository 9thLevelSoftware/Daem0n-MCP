"""Public v7 edit-preflight and reviewed capture operations."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from ...capture_candidates import (
    CaptureCandidate,
    CaptureCandidateError,
    CaptureCandidateStore,
)
from ...covenant import InvocationScope, invocation_scope_var
from ...edit_bridge import EditApprovalBroker, EditBridgeError
from ...event_store import canonical_json_bytes
from ...workspace import Workspace, WorkspaceRegistry
from .application import AdmittedRequest
from .errors import STABLE_ERROR_CODE_SET
from .tools import (
    CaptureCandidateView,
    EditPreflightData,
    MemoryCaptureListData,
    MemoryCapturePromoteData,
)


class EditCaptureOperationError(RuntimeError):
    def __init__(self, code: str) -> None:
        if code not in STABLE_ERROR_CODE_SET:
            code = "CAPABILITY_DEGRADED"
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class EditCaptureOperationDependencies:
    broker: EditApprovalBroker
    candidates: CaptureCandidateStore
    cursor_secret: bytes
    scope_provider: Callable[[], InvocationScope | None] = field(
        default=invocation_scope_var.get
    )

    def __post_init__(self) -> None:
        if not isinstance(self.broker, EditApprovalBroker):
            raise TypeError("edit approval broker is required")
        if not isinstance(self.candidates, CaptureCandidateStore):
            raise TypeError("capture candidate store is required")
        if not isinstance(self.cursor_secret, bytes) or len(self.cursor_secret) < 32:
            raise ValueError("capture cursor secret must contain at least 32 bytes")
        if not callable(self.scope_provider):
            raise TypeError("edit scope provider must be callable")


def _authorize(workspace: Workspace, request: AdmittedRequest, tool: str) -> None:
    if (
        not isinstance(workspace, Workspace)
        or request.tool_name != tool
        or request.workspace_id != workspace.workspace_id
    ):
        raise EditCaptureOperationError("UNAUTHORIZED_WORKSPACE")
    try:
        root = workspace.root.resolve(strict=True)
        expected = WorkspaceRegistry([root], default_root=root).default
    except (OSError, RuntimeError, TypeError, ValueError):
        raise EditCaptureOperationError("UNAUTHORIZED_WORKSPACE") from None
    if expected.workspace_id != workspace.workspace_id or os.path.normcase(
        str(workspace.root)
    ) != os.path.normcase(str(root)):
        raise EditCaptureOperationError("UNAUTHORIZED_WORKSPACE")


def _scope(
    dependencies: EditCaptureOperationDependencies, workspace: Workspace
) -> InvocationScope:
    scope = dependencies.scope_provider()
    if scope is None:
        raise EditCaptureOperationError("IDENTITY_UNAVAILABLE")
    try:
        canonical = os.path.normcase(str(workspace.root.resolve(strict=True)))
    except (OSError, RuntimeError, ValueError):
        raise EditCaptureOperationError("UNAUTHORIZED_WORKSPACE") from None
    if scope.canonical_workspace != canonical:
        raise EditCaptureOperationError("TOKEN_SCOPE_MISMATCH")
    return scope


def _candidate_view(candidate: CaptureCandidate) -> CaptureCandidateView:
    return CaptureCandidateView(
        candidate_id=candidate.candidate_id,
        source_kind=candidate.source_kind,
        proposed_record=dict(candidate.record),
        provenance=dict(candidate.provenance),
        created_at=candidate.created_at,
    )


def _cursor(
    dependencies: EditCaptureOperationDependencies,
    *,
    scope: InvocationScope,
    workspace_id: str,
    boundary: tuple[int, str],
) -> str:
    payload = {
        "v": 1,
        "workspace_id": workspace_id,
        "principal": hashlib.sha256(scope.principal_id.encode("utf-8")).hexdigest(),
        "session": hashlib.sha256(
            scope.transport_session_id.encode("utf-8")
        ).hexdigest(),
        "created_at_us": boundary[0],
        "candidate_id": boundary[1],
    }
    encoded = base64.urlsafe_b64encode(canonical_json_bytes(payload)).rstrip(b"=")
    signature = hmac.new(
        dependencies.cursor_secret, b"capture." + encoded, hashlib.sha256
    ).hexdigest()
    return f"cur_v1_capture_{encoded.decode('ascii')}_{signature}"


def _decode_cursor(
    dependencies: EditCaptureOperationDependencies,
    *,
    scope: InvocationScope,
    workspace_id: str,
    cursor: str | None,
) -> tuple[int, str] | None:
    if cursor is None:
        return None
    try:
        prefix = "cur_v1_capture_"
        if not cursor.startswith(prefix):
            raise ValueError
        encoded, signature = cursor[len(prefix) :].rsplit("_", 1)
        expected = hmac.new(
            dependencies.cursor_secret,
            b"capture." + encoded.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        payload = json.loads(
            base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        )
        if (
            not isinstance(payload, dict)
            or payload.get("v") != 1
            or payload.get("workspace_id") != workspace_id
            or payload.get("principal")
            != hashlib.sha256(scope.principal_id.encode("utf-8")).hexdigest()
            or payload.get("session")
            != hashlib.sha256(scope.transport_session_id.encode("utf-8")).hexdigest()
            or isinstance(payload.get("created_at_us"), bool)
            or not isinstance(payload.get("created_at_us"), int)
            or not isinstance(payload.get("candidate_id"), str)
        ):
            raise ValueError
        return int(payload["created_at_us"]), str(payload["candidate_id"])
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        raise EditCaptureOperationError("INVALID_ARGUMENT") from None


def _translate(error: Exception) -> EditCaptureOperationError:
    code = getattr(error, "code", None)
    return EditCaptureOperationError(
        code if isinstance(code, str) else "CAPABILITY_DEGRADED"
    )


def build_edit_capture_operations(
    dependencies: EditCaptureOperationDependencies,
) -> Mapping[str, Callable[..., Any]]:
    if not isinstance(dependencies, EditCaptureOperationDependencies):
        raise TypeError("edit/capture dependencies are required")

    async def edit_preflight(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> EditPreflightData:
        _authorize(workspace, request, "edit_preflight")
        scope = _scope(dependencies, workspace)
        try:
            receipt = await dependencies.broker.issue_receipt(
                workspace,
                scope,
                edit_request_id=request.edit_request_id,
                description=request.description,
            )
        except (EditBridgeError, CaptureCandidateError) as error:
            raise _translate(error) from None
        return EditPreflightData(
            edit_request_id=receipt.edit_request_id,
            edit_receipt=receipt.receipt,
            expires_at=receipt.expires_at,
        )

    async def memory_capture_list(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> MemoryCaptureListData:
        _authorize(workspace, request, "memory_capture_list")
        scope = _scope(dependencies, workspace)
        before = _decode_cursor(
            dependencies,
            scope=scope,
            workspace_id=workspace.workspace_id,
            cursor=request.cursor,
        )
        try:
            candidates, next_boundary = await dependencies.candidates.list_pending(
                workspace,
                limit=request.limit,
                before=before,
            )
        except CaptureCandidateError as error:
            raise _translate(error) from None
        next_cursor = (
            None
            if next_boundary is None
            else _cursor(
                dependencies,
                scope=scope,
                workspace_id=workspace.workspace_id,
                boundary=next_boundary,
            )
        )
        return MemoryCaptureListData(
            items=[_candidate_view(candidate) for candidate in candidates],
            next_cursor=next_cursor,
            truncated=next_cursor is not None,
        )

    async def memory_capture_promote(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> MemoryCapturePromoteData:
        _authorize(workspace, request, "memory_capture_promote")
        _scope(dependencies, workspace)
        record = {
            "record_type": request.record_type,
            "content": request.content,
            "rationale": request.rationale,
            "context": dict(request.context),
            "tags": list(request.tags),
        }
        try:
            promotion = await dependencies.candidates.promote(
                workspace,
                candidate_id=request.candidate_id,
                record=record,
                idempotency_key=request.idempotency_key,
            )
        except CaptureCandidateError as error:
            raise _translate(error) from None
        return MemoryCapturePromoteData(
            candidate_id=promotion.candidate.candidate_id,
            record=promotion.record,
            event_id=promotion.event_id,
            idempotent_replay=promotion.idempotent_replay,
        )

    return MappingProxyType(
        {
            "edit_preflight": edit_preflight,
            "memory_capture_list": memory_capture_list,
            "memory_capture_promote": memory_capture_promote,
        }
    )


__all__ = [
    "EditCaptureOperationDependencies",
    "EditCaptureOperationError",
    "build_edit_capture_operations",
]
