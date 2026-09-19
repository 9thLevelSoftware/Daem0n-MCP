"""Shared construction of strict v7 success and business-error envelopes."""

from __future__ import annotations

import logging
import secrets
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, TypeVar

from .errors import INTERNAL_ERROR_MESSAGE, ErrorCode
from .models import (
    ApiError,
    ApiResponse,
    ApiWarning,
    CapabilityState,
    ErrorRemedy,
    FieldError,
    ResponseMeta,
    WorkspaceId,
)

T = TypeVar("T")

_LOGGER = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _request_id() -> str:
    return f"req_{secrets.token_urlsafe(18)}"


@dataclass(frozen=True, slots=True)
class ResponseContext:
    """One request's immutable identity and timing origin."""

    workspace_id: WorkspaceId | None
    request_id: str
    started_at: datetime
    _clock: Callable[[], datetime]

    def _meta(
        self,
        *,
        warnings: Sequence[ApiWarning] = (),
        capability_states: Sequence[CapabilityState] = (),
    ) -> ResponseMeta:
        finished_at = self._clock()
        elapsed = int(max(0.0, (finished_at - self.started_at).total_seconds()) * 1000)
        return ResponseMeta(
            request_id=self.request_id,
            workspace_id=self.workspace_id,
            started_at=self.started_at,
            duration_ms=min(elapsed, 86_400_000),
            warnings=list(warnings),
            capability_states=list(capability_states),
        )

    def success(
        self,
        data: T,
        *,
        warnings: Sequence[ApiWarning] = (),
        capability_states: Sequence[CapabilityState] = (),
    ) -> ApiResponse[T]:
        return ApiResponse[T](
            ok=True,
            data=data,
            error=None,
            meta=self._meta(
                warnings=warnings,
                capability_states=capability_states,
            ),
        )

    def failure(
        self,
        code: ErrorCode | str,
        message: str,
        *,
        retryable: bool = False,
        retry_after_ms: int | None = None,
        field_errors: Sequence[FieldError] = (),
        remedy_tool: str | None = None,
        remedy_arguments: Mapping[str, Any] | None = None,
        warnings: Sequence[ApiWarning] = (),
        capability_states: Sequence[CapabilityState] = (),
    ) -> ApiResponse[Any]:
        try:
            stable_code = code if isinstance(code, ErrorCode) else ErrorCode(code)
        except (TypeError, ValueError) as exc:
            raise ValueError("error code is not in the stable v7 registry") from exc
        remedy = None
        if remedy_tool is not None:
            remedy = ErrorRemedy(
                tool=remedy_tool,
                arguments=dict(remedy_arguments or {}),
            )
        elif remedy_arguments:
            raise ValueError("remedy arguments require a remedy tool")
        return ApiResponse[Any](
            ok=False,
            data=None,
            error=ApiError(
                code=stable_code,
                message=message,
                retryable=retryable,
                retry_after_ms=retry_after_ms,
                field_errors=list(field_errors),
                remedy=remedy,
                correlation_id=self.request_id,
            ),
            meta=self._meta(
                warnings=warnings,
                capability_states=capability_states,
            ),
        )

    def internal_error(self, error: BaseException | None = None) -> ApiResponse[Any]:
        """Return the one deliberately opaque internal failure envelope.

        The cause is logged (stderr by default, never the MCP wire) under the
        correlation ID the caller receives, so the owner can find it.
        """

        _LOGGER.error(
            "v7 internal error correlation_id=%s",
            self.request_id,
            exc_info=error if error is not None else sys.exc_info()[1],
        )
        return self.failure(
            ErrorCode.INTERNAL_ERROR,
            INTERNAL_ERROR_MESSAGE,
        )


class ResponseFactory:
    """Create per-call response contexts using injectable deterministic seams."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = _utc_now,
        request_id: Callable[[], str] = _request_id,
    ) -> None:
        self._clock = clock
        self._request_id = request_id

    def begin(self, workspace_id: WorkspaceId | None) -> ResponseContext:
        return ResponseContext(
            workspace_id=workspace_id,
            request_id=self._request_id(),
            started_at=self._clock(),
            _clock=self._clock,
        )


__all__ = ["ResponseContext", "ResponseFactory"]
