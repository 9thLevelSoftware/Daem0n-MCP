"""Framework-neutral, policy-first JSON resources for the v7 MCP surface.

The module deliberately knows nothing about FastMCP or database engines.  A
composition root injects workspace resolution, exact-scope Communion
authorization, and four bounded readers.  This keeps resource registration
from becoming an alternate path around the v7 policy boundary.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Generic, Literal, Protocol, TypeVar, cast

from pydantic import Field, StringConstraints, TypeAdapter

from ...workspace import Workspace
from .dashboard_resources import build_dashboard_resource_specs
from .models import (
    ActiveContextId,
    AwareDateTime,
    RecordSummary,
    RuleId,
    UserText,
    UtcDateTime,
    WireModel,
    WorkspaceId,
)
from .registry import ResourceSpec

MAX_RESOURCE_ITEMS = 50
RESOURCE_FETCH_LIMIT = MAX_RESOURCE_ITEMS + 1

WARNING_RESOURCE_URI_TEMPLATE = "memory://workspaces/{workspace_id}/warnings"
FAILURE_RESOURCE_URI_TEMPLATE = "memory://workspaces/{workspace_id}/failures"
RULE_RESOURCE_URI_TEMPLATE = "memory://workspaces/{workspace_id}/rules"
ACTIVE_CONTEXT_RESOURCE_URI_TEMPLATE = (
    "memory://workspaces/{workspace_id}/active-context"
)
RESOURCE_URI_TEMPLATES = (
    WARNING_RESOURCE_URI_TEMPLATE,
    FAILURE_RESOURCE_URI_TEMPLATE,
    RULE_RESOURCE_URI_TEMPLATE,
    ACTIVE_CONTEXT_RESOURCE_URI_TEMPLATE,
)

_WORKSPACE_ID_ADAPTER = TypeAdapter(WorkspaceId)
_UTC_DATETIME_ADAPTER = TypeAdapter(UtcDateTime)
_ACCESS_FAILURE = object()

# Rule text is user-authored and may mention paths.
BoundedText = Annotated[
    UserText,
    StringConstraints(strict=True, min_length=1, max_length=2000),
]


class ResourceAccessError(RuntimeError):
    """The only caller-visible resource read failure.

    Its deliberately invariant representation prevents workspace enumeration
    and keeps repository, authorization, and canonical-path details out of MCP
    resource errors.
    """

    code = "RESOURCE_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("Resource unavailable")


class RuleView(WireModel):
    """Bounded public representation of one workspace rule."""

    rule_id: RuleId
    trigger: BoundedText
    must_do: list[BoundedText] = Field(default_factory=list, max_length=50)
    must_not: list[BoundedText] = Field(default_factory=list, max_length=50)
    ask_first: list[BoundedText] = Field(default_factory=list, max_length=50)
    warnings: list[BoundedText] = Field(default_factory=list, max_length=50)
    priority: Annotated[int, Field(strict=True, ge=-1000, le=1000)]
    enabled: Annotated[bool, Field(strict=True)]
    created_at: AwareDateTime


class ActiveContextItem(WireModel):
    """One always-hot record without a legacy row ID or canonical path."""

    active_context_id: ActiveContextId
    record: RecordSummary
    priority: Annotated[int, Field(strict=True, ge=-100, le=100)]
    reason: (
        Annotated[
            UserText,
            StringConstraints(strict=True, min_length=1, max_length=2000),
        ]
        | None
    ) = None
    added_at: AwareDateTime
    expires_at: AwareDateTime | None = None


T = TypeVar("T", bound=WireModel)


class ResourceDocument(WireModel, Generic[T]):
    """The common, strict wire document shared by all four resources."""

    api_version: Literal["7"] = "7"
    workspace_id: WorkspaceId
    generated_at: UtcDateTime
    items: list[T] = Field(default_factory=list, max_length=MAX_RESOURCE_ITEMS)
    truncated: bool


class WarningResourceDocument(ResourceDocument[RecordSummary]):
    """Newest non-archived warning summaries."""


class FailureResourceDocument(ResourceDocument[RecordSummary]):
    """Newest non-archived failed-outcome summaries."""


class RuleResourceDocument(ResourceDocument[RuleView]):
    """Highest-priority enabled rule views."""


class ActiveContextResourceDocument(ResourceDocument[ActiveContextItem]):
    """Highest-priority, unexpired active-context entries."""


ResourceKind = Literal["warnings", "failures", "rules", "active_context"]
ResourceOrder = Literal["updated_at_desc", "priority_desc"]


@dataclass(frozen=True, slots=True)
class ResourceReadRequest:
    """Fail-closed query contract passed to an injected repository reader."""

    kind: ResourceKind
    limit: int
    order_by: ResourceOrder
    include_archived: bool = False
    include_deleted: bool = False
    include_expired: bool = False
    enabled_only: bool | None = None


@dataclass(frozen=True, slots=True)
class ResourceRow(Generic[T]):
    """Internal repository row carrying deletion state beside a wire item."""

    item: T
    deleted: bool = False


class WorkspaceResolver(Protocol):
    def resolve(self, workspace_id: str) -> Workspace | Awaitable[Workspace]: ...


class CommunionAuthorizer(Protocol):
    def authorize(
        self,
        *,
        workspace: Workspace,
        resource_uri: str,
    ) -> None | Awaitable[None]: ...


class ResourceReader(Protocol):
    def __call__(
        self,
        workspace: Workspace,
        request: ResourceReadRequest,
    ) -> object | Awaitable[object]: ...


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class ResourceDependencies:
    workspace_resolver: WorkspaceResolver
    communion_authorizer: CommunionAuthorizer
    warning_reader: ResourceReader
    failure_reader: ResourceReader
    rule_reader: ResourceReader
    active_context_reader: ResourceReader
    clock: Callable[[], datetime] = _utc_now


async def _resolve(value: object) -> object:
    if inspect.isawaitable(value):
        return await value
    return value


R = TypeVar("R")


def _capture(operation: Callable[[], R]) -> R | object:
    """Drop private exception objects before returning a failure sentinel."""

    try:
        return operation()
    except Exception:
        return _ACCESS_FAILURE


async def _capture_async(operation: Callable[[], Awaitable[R]]) -> R | object:
    """Async equivalent that deliberately does not catch cancellation."""

    try:
        return await operation()
    except Exception:
        return _ACCESS_FAILURE


def _rows(value: object) -> Sequence[object]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TypeError("resource reader must return a bounded sequence")
    return value


def _unwrap(row: object, model: type[T]) -> tuple[T, bool]:
    if isinstance(row, ResourceRow):
        if type(row.deleted) is not bool:
            raise TypeError("resource deletion state must be boolean")
        item = row.item
        deleted = row.deleted
    else:
        item = row
        deleted = False
    validator = model.model_validate
    return validator(item), deleted


class ResourceHandlers:
    """Bound handlers used by both inspectable and FastMCP registry adapters."""

    def __init__(self, dependencies: ResourceDependencies) -> None:
        self._dependencies = dependencies

    async def _admit(
        self,
        workspace_id: str,
        uri_template: str,
    ) -> tuple[WorkspaceId, Workspace, str, datetime]:
        validated_id = _WORKSPACE_ID_ADAPTER.validate_python(workspace_id, strict=True)
        workspace_value = self._dependencies.workspace_resolver.resolve(validated_id)
        workspace = await _resolve(workspace_value)
        if (
            not isinstance(workspace, Workspace)
            or workspace.workspace_id != validated_id
        ):
            raise ValueError("workspace resolver returned a mismatched workspace")
        resource_uri = uri_template.format(workspace_id=validated_id)
        authorization = self._dependencies.communion_authorizer.authorize(
            workspace=workspace,
            resource_uri=resource_uri,
        )
        await _resolve(authorization)
        generated_at = _UTC_DATETIME_ADAPTER.validate_python(
            self._dependencies.clock(),
            strict=True,
        )
        return validated_id, workspace, resource_uri, generated_at

    async def _read(
        self,
        *,
        workspace_id: str,
        uri_template: str,
        reader: ResourceReader,
        request: ResourceReadRequest,
    ) -> tuple[WorkspaceId, datetime, Sequence[object]]:
        async def operation() -> tuple[WorkspaceId, datetime, Sequence[object]]:
            validated_id, workspace, _resource_uri, generated_at = await self._admit(
                workspace_id,
                uri_template,
            )
            result = reader(workspace, request)
            return validated_id, generated_at, _rows(await _resolve(result))

        captured = await _capture_async(operation)
        if captured is _ACCESS_FAILURE:
            raise ResourceAccessError()
        return cast(tuple[WorkspaceId, datetime, Sequence[object]], captured)

    async def warnings(self, workspace_id: str) -> WarningResourceDocument:
        """Return the newest active warnings for one authorized workspace."""

        request = ResourceReadRequest(
            kind="warnings",
            limit=RESOURCE_FETCH_LIMIT,
            order_by="updated_at_desc",
        )
        workspace, generated_at, rows = await self._read(
            workspace_id=workspace_id,
            uri_template=WARNING_RESOURCE_URI_TEMPLATE,
            reader=self._dependencies.warning_reader,
            request=request,
        )

        def build() -> WarningResourceDocument:
            items = self._record_items(rows)
            return WarningResourceDocument(
                workspace_id=workspace,
                generated_at=generated_at,
                items=items[:MAX_RESOURCE_ITEMS],
                truncated=len(items) > MAX_RESOURCE_ITEMS,
            )

        captured = _capture(build)
        if captured is _ACCESS_FAILURE:
            raise ResourceAccessError()
        return cast(WarningResourceDocument, captured)

    async def failures(self, workspace_id: str) -> FailureResourceDocument:
        """Return the newest active failed-outcome summaries."""

        request = ResourceReadRequest(
            kind="failures",
            limit=RESOURCE_FETCH_LIMIT,
            order_by="updated_at_desc",
        )
        workspace, generated_at, rows = await self._read(
            workspace_id=workspace_id,
            uri_template=FAILURE_RESOURCE_URI_TEMPLATE,
            reader=self._dependencies.failure_reader,
            request=request,
        )

        def build() -> FailureResourceDocument:
            items = self._record_items(rows)
            return FailureResourceDocument(
                workspace_id=workspace,
                generated_at=generated_at,
                items=items[:MAX_RESOURCE_ITEMS],
                truncated=len(items) > MAX_RESOURCE_ITEMS,
            )

        captured = _capture(build)
        if captured is _ACCESS_FAILURE:
            raise ResourceAccessError()
        return cast(FailureResourceDocument, captured)

    async def rules(self, workspace_id: str) -> RuleResourceDocument:
        """Return the highest-priority enabled rules."""

        request = ResourceReadRequest(
            kind="rules",
            limit=RESOURCE_FETCH_LIMIT,
            order_by="priority_desc",
            enabled_only=True,
        )
        workspace, generated_at, rows = await self._read(
            workspace_id=workspace_id,
            uri_template=RULE_RESOURCE_URI_TEMPLATE,
            reader=self._dependencies.rule_reader,
            request=request,
        )

        def build() -> RuleResourceDocument:
            items: list[RuleView] = []
            for row in rows:
                item, deleted = _unwrap(row, RuleView)
                if not deleted and item.enabled:
                    items.append(item)
            items.sort(
                key=lambda item: (item.priority, item.created_at, item.rule_id),
                reverse=True,
            )
            return RuleResourceDocument(
                workspace_id=workspace,
                generated_at=generated_at,
                items=items[:MAX_RESOURCE_ITEMS],
                truncated=len(items) > MAX_RESOURCE_ITEMS,
            )

        captured = _capture(build)
        if captured is _ACCESS_FAILURE:
            raise ResourceAccessError()
        return cast(RuleResourceDocument, captured)

    async def active_context(
        self,
        workspace_id: str,
    ) -> ActiveContextResourceDocument:
        """Return the highest-priority non-expired active-context entries."""

        request = ResourceReadRequest(
            kind="active_context",
            limit=RESOURCE_FETCH_LIMIT,
            order_by="priority_desc",
        )
        workspace, generated_at, rows = await self._read(
            workspace_id=workspace_id,
            uri_template=ACTIVE_CONTEXT_RESOURCE_URI_TEMPLATE,
            reader=self._dependencies.active_context_reader,
            request=request,
        )

        def build() -> ActiveContextResourceDocument:
            items: list[ActiveContextItem] = []
            for row in rows:
                item, deleted = _unwrap(row, ActiveContextItem)
                if (
                    not deleted
                    and item.record.current_status != "archived"
                    and (item.expires_at is None or item.expires_at > generated_at)
                ):
                    items.append(item)
            items.sort(
                key=lambda item: (
                    item.priority,
                    item.added_at,
                    item.active_context_id,
                ),
                reverse=True,
            )
            return ActiveContextResourceDocument(
                workspace_id=workspace,
                generated_at=generated_at,
                items=items[:MAX_RESOURCE_ITEMS],
                truncated=len(items) > MAX_RESOURCE_ITEMS,
            )

        captured = _capture(build)
        if captured is _ACCESS_FAILURE:
            raise ResourceAccessError()
        return cast(ActiveContextResourceDocument, captured)

    @staticmethod
    def _record_items(rows: Sequence[object]) -> list[RecordSummary]:
        items: list[RecordSummary] = []
        for row in rows:
            item, deleted = _unwrap(row, RecordSummary)
            if not deleted and item.current_status != "archived":
                items.append(item)
        items.sort(
            key=lambda item: (item.updated_at, item.record_id),
            reverse=True,
        )
        return items


def build_resource_specs(handlers: ResourceHandlers) -> tuple[ResourceSpec, ...]:
    """Return four scoped JSON templates and six static dashboard shells."""

    workspace_specs = (
        ResourceSpec(
            uri_template=WARNING_RESOURCE_URI_TEMPLATE,
            name="workspace_warnings",
            description="Newest active warnings for an authorized workspace.",
            handler=handlers.warnings,
            output_model=WarningResourceDocument,
        ),
        ResourceSpec(
            uri_template=FAILURE_RESOURCE_URI_TEMPLATE,
            name="workspace_failures",
            description="Newest active failed outcomes for an authorized workspace.",
            handler=handlers.failures,
            output_model=FailureResourceDocument,
        ),
        ResourceSpec(
            uri_template=RULE_RESOURCE_URI_TEMPLATE,
            name="workspace_rules",
            description="Highest-priority enabled rules for an authorized workspace.",
            handler=handlers.rules,
            output_model=RuleResourceDocument,
        ),
        ResourceSpec(
            uri_template=ACTIVE_CONTEXT_RESOURCE_URI_TEMPLATE,
            name="workspace_active_context",
            description="Highest-priority active context for an authorized workspace.",
            handler=handlers.active_context,
            output_model=ActiveContextResourceDocument,
        ),
    )
    return workspace_specs + build_dashboard_resource_specs()


__all__ = [
    "ACTIVE_CONTEXT_RESOURCE_URI_TEMPLATE",
    "ActiveContextItem",
    "ActiveContextResourceDocument",
    "CommunionAuthorizer",
    "FAILURE_RESOURCE_URI_TEMPLATE",
    "FailureResourceDocument",
    "MAX_RESOURCE_ITEMS",
    "RESOURCE_FETCH_LIMIT",
    "RESOURCE_URI_TEMPLATES",
    "RULE_RESOURCE_URI_TEMPLATE",
    "ResourceAccessError",
    "ResourceDependencies",
    "ResourceDocument",
    "ResourceHandlers",
    "ResourceReadRequest",
    "ResourceReader",
    "ResourceRow",
    "RuleResourceDocument",
    "RuleView",
    "WARNING_RESOURCE_URI_TEMPLATE",
    "WarningResourceDocument",
    "WorkspaceResolver",
    "build_resource_specs",
]
