"""Stable error-code registry for the Daem0nMCP v7 wire contract.

The registry is deliberately data-only.  Adding a code is an API change and
must update the schema/conformance fixture in the same review.
"""

from __future__ import annotations

import sqlite3
from enum import Enum
from types import MappingProxyType

from ...bounded_workers import BoundedWorkerBusyError


class ErrorCode(str, Enum):
    """Reviewed business-error codes returned inside ``ApiResponse``."""

    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    UNAUTHORIZED_WORKSPACE = "UNAUTHORIZED_WORKSPACE"
    WORKSPACE_PATH_ESCAPE = "WORKSPACE_PATH_ESCAPE"
    STALE_PROJECTION_ID = "STALE_PROJECTION_ID"
    CAPABILITY_DISABLED = "CAPABILITY_DISABLED"
    CAPABILITY_DEGRADED = "CAPABILITY_DEGRADED"
    LEXICAL_UNAVAILABLE = "LEXICAL_UNAVAILABLE"
    COMMUNION_REQUIRED = "COMMUNION_REQUIRED"
    COUNSEL_REQUIRED = "COUNSEL_REQUIRED"
    IDENTITY_UNAVAILABLE = "IDENTITY_UNAVAILABLE"
    UNKNOWN_COVENANT_OPERATION = "UNKNOWN_COVENANT_OPERATION"
    TOKEN_MISSING = "TOKEN_MISSING"
    TOKEN_TAMPERED = "TOKEN_TAMPERED"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    TOKEN_SCOPE_MISMATCH = "TOKEN_SCOPE_MISMATCH"
    TOKEN_OPERATION_MISMATCH = "TOKEN_OPERATION_MISMATCH"
    TOKEN_ARGUMENT_MISMATCH = "TOKEN_ARGUMENT_MISMATCH"
    TOKEN_REPLAYED = "TOKEN_REPLAYED"
    TOKEN_LEGACY_UNSUPPORTED = "TOKEN_LEGACY_UNSUPPORTED"
    PREFLIGHT_TARGET_NOT_PROTECTED = "PREFLIGHT_TARGET_NOT_PROTECTED"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    TASK_REQUIRED = "TASK_REQUIRED"
    TASKS_UNAVAILABLE = "TASKS_UNAVAILABLE"
    CANCELLED = "CANCELLED"
    DATABASE_IN_USE = "DATABASE_IN_USE"
    EVENT_STREAM_CONFLICT = "EVENT_STREAM_CONFLICT"
    IMPORT_INVALID = "IMPORT_INVALID"
    CROSS_WORKSPACE_IMPORT_UNSUPPORTED = "CROSS_WORKSPACE_IMPORT_UNSUPPORTED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


STABLE_ERROR_CODES: tuple[str, ...] = tuple(code.value for code in ErrorCode)
STABLE_ERROR_CODE_SET = frozenset(STABLE_ERROR_CODES)
ERROR_CODE_REGISTRY = MappingProxyType({code.value: code for code in ErrorCode})

# INTERNAL_ERROR is intentionally less descriptive than every domain error.
# The correlation ID in ApiError is the sole diagnostic handle exposed to a
# caller; server-side logs may contain the corresponding private details.
INTERNAL_ERROR_MESSAGE = "Internal error."


def is_stable_error_code(value: object) -> bool:
    """Return whether *value* is an exact reviewed v7 error code."""

    return isinstance(value, str) and value in STABLE_ERROR_CODE_SET


# Clients back off this long before retrying DATABASE_IN_USE.
DATABASE_IN_USE_RETRY_AFTER_MS = 250


def is_database_busy(error: BaseException | None) -> bool:
    """Return whether *error* is transient contention a caller may retry.

    A full worker pool and a SQLite lock or busy timeout both clear once the
    competing writer finishes, so they map to retryable ``DATABASE_IN_USE``.
    The ``code`` check keeps an already-classified repository error (which is
    not a ``RuntimeServiceError``) busy through the briefing/preflight wrappers.
    """

    if isinstance(error, BoundedWorkerBusyError):
        return True
    if getattr(error, "code", None) == ErrorCode.DATABASE_IN_USE.value:
        return True
    if not isinstance(error, sqlite3.OperationalError):
        return False
    code = getattr(error, "sqlite_errorcode", None)
    if isinstance(code, int):
        return code & 0xFF in {5, 6}  # SQLITE_BUSY, SQLITE_LOCKED
    # sqlite_errorcode is unavailable on Python 3.10; match SQLite's fixed
    # lock diagnostics only, never arbitrary detail text.
    return str(error).casefold() in {
        "database is locked",
        "database table is locked",
        "database schema is locked",
    }


__all__ = [
    "DATABASE_IN_USE_RETRY_AFTER_MS",
    "ERROR_CODE_REGISTRY",
    "INTERNAL_ERROR_MESSAGE",
    "STABLE_ERROR_CODES",
    "STABLE_ERROR_CODE_SET",
    "ErrorCode",
    "is_database_busy",
    "is_stable_error_code",
]
