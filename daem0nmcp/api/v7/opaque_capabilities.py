"""Opaque public handles over the existing signed Covenant authority."""

from __future__ import annotations

import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ...covenant import (
    CapabilityAuthority,
    InvocationScope,
    TokenValidationError,
)

_HANDLE_PATTERN = re.compile(r"^cap_[A-Za-z0-9_-]{12,252}$")


@dataclass(frozen=True, slots=True)
class OpaqueIssuedCapability:
    """The issuance fields consumed by ``CovenantGate`` without raw claims."""

    token: str
    nonce: str
    expires_at: int
    args_sha256: str


@dataclass(frozen=True, slots=True)
class _StoredCapability:
    raw_token: str
    expires_at: int


class OpaqueCapabilityCapacityError(RuntimeError):
    """Raised instead of evicting another caller's live capability."""

    code = "CAPABILITY_DEGRADED"


class OpaqueCapabilityAuthority:
    """Keep signed claims server-side and expose only random lookup handles."""

    def __init__(
        self,
        delegate: CapabilityAuthority,
        *,
        token_factory: Callable[[], str] | None = None,
        max_handles: int = 4096,
        clock: Callable[[], int | float] = time.time,
    ) -> None:
        if not isinstance(delegate, CapabilityAuthority):
            raise TypeError("delegate must be a CapabilityAuthority")
        if token_factory is not None and not callable(token_factory):
            raise TypeError("token_factory must be callable")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if (
            isinstance(max_handles, bool)
            or not isinstance(max_handles, int)
            or not 1 <= max_handles <= 1_000_000
        ):
            raise ValueError("max_handles must be between 1 and 1000000")
        self._delegate = delegate
        self._token_factory = token_factory or (
            lambda: f"cap_{secrets.token_urlsafe(32)}"
        )
        self._max_handles = max_handles
        self._clock = clock
        self._handles: OrderedDict[str, _StoredCapability] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def kid(self) -> str:
        return self._delegate.kid

    @property
    def ttl_seconds(self) -> int:
        return self._delegate.ttl_seconds

    @staticmethod
    def _validate_handle(token: object) -> str:
        if isinstance(token, str) and token.lstrip().startswith("{"):
            raise TokenValidationError("TOKEN_LEGACY_UNSUPPORTED")
        if not isinstance(token, str) or _HANDLE_PATTERN.fullmatch(token) is None:
            raise TokenValidationError("TOKEN_TAMPERED")
        return token

    def issue(
        self,
        scope: InvocationScope,
        operation: str,
        args_sha256: str,
    ) -> OpaqueIssuedCapability:
        with self._lock:
            try:
                now = int(self._clock())
            except (OverflowError, TypeError, ValueError) as exc:
                raise RuntimeError("opaque capability clock is unavailable") from exc
            expired = [
                handle
                for handle, stored in self._handles.items()
                if stored.expires_at <= now
            ]
            for handle in expired:
                self._handles.pop(handle, None)
            if len(self._handles) >= self._max_handles:
                raise OpaqueCapabilityCapacityError(
                    "opaque capability capacity is unavailable"
                )

            issued = self._delegate.issue(scope, operation, args_sha256)
            handle = self._token_factory()
            try:
                validated = self._validate_handle(handle)
            except TokenValidationError as exc:
                raise ValueError("token_factory returned an invalid handle") from exc
            if validated in self._handles:
                raise RuntimeError("opaque capability handle collision")
            self._handles[validated] = _StoredCapability(
                raw_token=issued.token,
                expires_at=issued.expires_at,
            )
        return OpaqueIssuedCapability(
            token=validated,
            nonce=issued.nonce,
            expires_at=issued.expires_at,
            args_sha256=issued.args_sha256,
        )

    def verify(self, token: str) -> dict[str, Any]:
        validated = self._validate_handle(token)
        with self._lock:
            stored = self._handles.get(validated)
        if stored is None:
            raise TokenValidationError("TOKEN_TAMPERED")
        try:
            return self._delegate.verify(stored.raw_token)
        except TokenValidationError as exc:
            if exc.code == "TOKEN_EXPIRED":
                with self._lock:
                    self._handles.pop(validated, None)
            raise

    def discard(self, token: str) -> None:
        """Remove a handle whose downstream grant registration failed."""

        validated = self._validate_handle(token)
        with self._lock:
            self._handles.pop(validated, None)


__all__ = [
    "OpaqueCapabilityAuthority",
    "OpaqueCapabilityCapacityError",
    "OpaqueIssuedCapability",
]
