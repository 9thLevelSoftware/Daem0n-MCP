"""Shared construction of strict v7 success and business-error envelopes."""

from __future__ import annotations

import logging
import re
import secrets
import sys
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, TypeVar

from pydantic import ValidationError

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
_LOCATION_PART = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def _exception_chain(error: BaseException) -> list[BaseException]:
    """Return *error* and its causes, root cause first, as tracebacks do."""

    chain: list[BaseException] = []
    link: BaseException | None = error
    while link is not None and all(link is not seen for seen in chain):
        chain.append(link)
        link = link.__cause__ or (
            None if link.__suppress_context__ else link.__context__
        )
    return chain[::-1]


def _without_input_values(chain: list[BaseException]) -> str:
    """Render a traceback whose pydantic errors omit the offending values.

    ``str(ValidationError)`` quotes a slice of each invalid input, which for
    service output can be stored memory content or a token.
    """

    rendered = []
    for link in chain:
        if isinstance(link, ValidationError):
            detail = "; ".join(
                ".".join(
                    str(part)
                    if isinstance(part, int) or _LOCATION_PART.match(str(part))
                    else "?"
                    for part in item["loc"]
                )
                + f" ({item['type']})"
                for item in link.errors(include_url=False)
            )
        else:
            detail = str(link)
        rendered.append(
            "Traceback (most recent call last):\n"
            + "".join(traceback.format_tb(link.__traceback__))
            + f"{type(link).__module__}.{type(link).__qualname__}: {detail}"
        )
    return "\n\nThe above exception led to:\n\n".join(rendered)


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
        correlation ID the caller receives, so the owner can find it.  Logs
        may still hold short exception messages such as local paths; redact
        them before sharing.  Pydantic input values are never logged.
        """

        cause = error if error is not None else sys.exc_info()[1]
        chain = [] if cause is None else _exception_chain(cause)
        if any(isinstance(link, ValidationError) for link in chain):
            _LOGGER.error(
                "v7 internal error correlation_id=%s\n%s",
                self.request_id,
                _without_input_values(chain),
            )
        else:
            _LOGGER.error(
                "v7 internal error correlation_id=%s",
                self.request_id,
                exc_info=cause,
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
