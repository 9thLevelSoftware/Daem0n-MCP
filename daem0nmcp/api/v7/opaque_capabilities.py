"""Opaque public handles over the existing signed Covenant authority."""

from __future__ import annotations

import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from ...covenant import (
    CapabilityAuthority,
    InvocationScope,
    TokenValidationError,
)

_HANDLE_PATTERN = re.compile(r"^cap_[A-Za-z0-9_-]{12,252}$")
_K = TypeVar("_K")


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
    principal: str
    session: tuple[str, str]


class OpaqueCapabilityCapacityError(RuntimeError):
    """Raised instead of evicting another caller's live capability."""

    code = "CAPABILITY_DEGRADED"


def _bound(name: str, value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= 1_000_000
    ):
        raise ValueError(f"{name} must be between 1 and 1000000")
    return value


def _decrement(counts: dict[_K, int], key: _K) -> None:
    remaining = counts.get(key, 0) - 1
    if remaining > 0:
        counts[key] = remaining
    else:
        counts.pop(key, None)


class OpaqueCapabilityAuthority:
    """Keep signed claims server-side and expose only random lookup handles.

    Only unconsumed handles hold capacity: the gate releases a handle as soon
    as its nonce is consumed, leaving a claim-free marker so a replay keeps
    its ``TOKEN_REPLAYED`` code.  Live handles are bounded per transport
    session, per principal and globally, and a caller at its own bound is
    refused without touching anyone else's handles.  Clients that share one
    principal (stdio and unauthenticated loopback HTTP use the process
    principal) are told apart only by session, so there the per-session bound
    is the isolation and the principal bound is shared by every session.
    """

    def __init__(
        self,
        delegate: CapabilityAuthority,
        *,
        token_factory: Callable[[], str] | None = None,
        max_handles: int = 4096,
        max_handles_per_principal: int = 1024,
        max_handles_per_session: int = 256,
        clock: Callable[[], int | float] = time.time,
    ) -> None:
        if not isinstance(delegate, CapabilityAuthority):
            raise TypeError("delegate must be a CapabilityAuthority")
        if token_factory is not None and not callable(token_factory):
            raise TypeError("token_factory must be callable")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._delegate = delegate
        self._token_factory = token_factory or (
            lambda: f"cap_{secrets.token_urlsafe(32)}"
        )
        self._max_handles = _bound("max_handles", max_handles)
        self._max_per_principal = _bound(
            "max_handles_per_principal", max_handles_per_principal
        )
        self._max_per_session = _bound(
            "max_handles_per_session", max_handles_per_session
        )
        self._clock = clock
        self._handles: OrderedDict[str, _StoredCapability] = OrderedDict()
        self._principal_live: dict[str, int] = {}
        self._session_live: dict[tuple[str, str], int] = {}
        # Consumed handles keep no claims, only what a replay answer needs.
        self._consumed: dict[str, tuple[int, tuple[str, str]]] = {}
        self._session_consumed: dict[tuple[str, str], OrderedDict[str, None]] = {}
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

    def _drop_live_locked(self, handle: str) -> _StoredCapability | None:
        stored = self._handles.pop(handle, None)
        if stored is not None:
            _decrement(self._principal_live, stored.principal)
            _decrement(self._session_live, stored.session)
        return stored

    def _drop_consumed_locked(self, handle: str) -> None:
        marker = self._consumed.pop(handle, None)
        if marker is None:
            return
        owned = self._session_consumed.get(marker[1])
        if owned is not None:
            owned.pop(handle, None)
            if not owned:
                del self._session_consumed[marker[1]]

    def _prune_locked(self, now: int) -> None:
        for handle in [
            handle
            for handle, stored in self._handles.items()
            if stored.expires_at <= now
        ]:
            self._drop_live_locked(handle)
        for handle in [
            handle for handle, marker in self._consumed.items() if marker[0] <= now
        ]:
            self._drop_consumed_locked(handle)

    def issue(
        self,
        scope: InvocationScope,
        operation: str,
        args_sha256: str,
    ) -> OpaqueIssuedCapability:
        principal = scope.principal_id
        session = (principal, scope.transport_session_id)
        with self._lock:
            try:
                now = int(self._clock())
            except (OverflowError, TypeError, ValueError) as exc:
                raise RuntimeError("opaque capability clock is unavailable") from exc
            self._prune_locked(now)
            if self._session_live.get(session, 0) >= self._max_per_session:
                raise OpaqueCapabilityCapacityError(
                    "opaque capability capacity for this session is unavailable"
                )
            if self._principal_live.get(principal, 0) >= self._max_per_principal:
                raise OpaqueCapabilityCapacityError(
                    "opaque capability capacity for this principal is unavailable"
                )
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
            if validated in self._handles or validated in self._consumed:
                raise RuntimeError("opaque capability handle collision")
            self._handles[validated] = _StoredCapability(
                raw_token=issued.token,
                expires_at=issued.expires_at,
                principal=principal,
                session=session,
            )
            self._principal_live[principal] = self._principal_live.get(principal, 0) + 1
            self._session_live[session] = self._session_live.get(session, 0) + 1
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
            replayed = stored is None and validated in self._consumed
        if replayed:
            raise TokenValidationError("TOKEN_REPLAYED")
        if stored is None:
            raise TokenValidationError("TOKEN_TAMPERED")
        try:
            return self._delegate.verify(stored.raw_token)
        except TokenValidationError as exc:
            if exc.code == "TOKEN_EXPIRED":
                with self._lock:
                    self._drop_live_locked(validated)
            raise

    def discard(self, token: str) -> None:
        """Remove a handle whose downstream grant registration failed."""

        validated = self._validate_handle(token)
        with self._lock:
            self._drop_live_locked(validated)

    def release_consumed(self, token: str) -> None:
        """Free the capacity of a handle whose one-use nonce was just consumed.

        The signed claims are dropped.  A marker without claims stays until
        the capability would have expired so a replay is still answered with
        ``TOKEN_REPLAYED``; markers are bounded per session, and a replay of
        one that was pushed out is rejected as an unknown handle.
        """

        validated = self._validate_handle(token)
        with self._lock:
            stored = self._drop_live_locked(validated)
            if stored is None:
                return
            self._consumed[validated] = (stored.expires_at, stored.session)
            owned = self._session_consumed.setdefault(stored.session, OrderedDict())
            owned[validated] = None
            while len(owned) > self._max_per_session:
                self._drop_consumed_locked(next(iter(owned)))


__all__ = [
    "OpaqueCapabilityAuthority",
    "OpaqueCapabilityCapacityError",
    "OpaqueIssuedCapability",
]
