"""Cancellation-safe bounded fallback for optional MCP task tools."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, TypeVar

T = TypeVar("T")


_MISSING = object()


def _argument_value(arguments: Mapping[str, Any], path: str) -> Any:
    value: Any = arguments
    for segment in path.split("."):
        if not isinstance(value, Mapping) or segment not in value:
            return _MISSING
        value = value[segment]
    return value


task_admission_only_var: ContextVar[bool] = ContextVar(
    "v7_task_admission_only",
    default=False,
)


@dataclass(frozen=True, slots=True)
class DurableTaskExecution:
    """Exact credential-free admission installed only by the owned worker."""

    task_id: str
    tool_name: str
    workspace_id: str
    arguments_sha256: str
    principal_id: str
    transport_session_id: str | None


durable_task_execution_var: ContextVar[DurableTaskExecution | None] = ContextVar(
    "v7_durable_task_execution",
    default=None,
)


def durable_task_arguments_sha256(arguments: Mapping[str, object]) -> str:
    encoded = json.dumps(
        arguments,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def is_durable_task_execution(
    tool_name: str,
    arguments: Mapping[str, object],
) -> bool:
    execution = durable_task_execution_var.get()
    workspace_id = arguments.get("workspace_id")
    if (
        execution is None
        or execution.tool_name != tool_name
        or execution.workspace_id != workspace_id
    ):
        return False
    sanitized = {
        key: value for key, value in arguments.items() if key != "preflight_token"
    }
    return execution.arguments_sha256 == durable_task_arguments_sha256(sanitized)


@dataclass(frozen=True, slots=True)
class ForegroundExecutionPolicy:
    """One tool's deterministic, side-effect-free foreground admission bound."""

    max_request_bytes: int
    max_collection_lengths: Mapping[str, int] = field(default_factory=dict)
    max_numeric_values: Mapping[str, float] = field(default_factory=dict)
    deadline_field: str | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_request_bytes, bool)
            or not isinstance(self.max_request_bytes, int)
            or self.max_request_bytes < 1
        ):
            raise ValueError("max_request_bytes must be a positive integer")
        collection_limits = dict(self.max_collection_lengths)
        numeric_limits = dict(self.max_numeric_values)
        if any(
            not isinstance(name, str)
            or not name
            or isinstance(collection_limit, bool)
            or not isinstance(collection_limit, int)
            or collection_limit < 0
            for name, collection_limit in collection_limits.items()
        ):
            raise ValueError("collection limits are invalid")
        if any(
            not isinstance(name, str)
            or not name
            or isinstance(numeric_limit, bool)
            or not isinstance(numeric_limit, (int, float))
            or not math.isfinite(float(numeric_limit))
            or float(numeric_limit) < 0
            for name, numeric_limit in numeric_limits.items()
        ):
            raise ValueError("numeric limits are invalid")
        if self.deadline_field is not None and (
            not isinstance(self.deadline_field, str) or not self.deadline_field
        ):
            raise ValueError("deadline_field is invalid")
        object.__setattr__(
            self,
            "max_collection_lengths",
            MappingProxyType(collection_limits),
        )
        object.__setattr__(
            self,
            "max_numeric_values",
            MappingProxyType(numeric_limits),
        )

    def admits(
        self,
        arguments: Mapping[str, Any],
        *,
        timeout_seconds: float,
    ) -> bool:
        """Return whether validated arguments fit this foreground profile."""

        try:
            encoded = json.dumps(
                dict(arguments),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError):
            return False
        if len(encoded) > self.max_request_bytes:
            return False
        for name, collection_maximum in self.max_collection_lengths.items():
            value = _argument_value(arguments, name)
            if (
                value is not _MISSING
                and value is not None
                and (
                    isinstance(value, (str, bytes, bytearray))
                    or not hasattr(value, "__len__")
                    or len(value) > collection_maximum
                )
            ):
                return False
        for name, numeric_maximum in self.max_numeric_values.items():
            value = _argument_value(arguments, name)
            if (
                value is not _MISSING
                and value is not None
                and (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or float(value) > numeric_maximum
                )
            ):
                return False
        if self.deadline_field is not None:
            value = _argument_value(arguments, self.deadline_field)
            if (
                value is not _MISSING
                and value is not None
                and (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or float(value) > timeout_seconds
                )
            ):
                return False
        return True


def _foreground_policy(
    max_request_bytes: int = 64 * 1024,
    *,
    collections: Mapping[str, int] | None = None,
    numeric: Mapping[str, float] | None = None,
    deadline_field: str | None = None,
) -> ForegroundExecutionPolicy:
    return ForegroundExecutionPolicy(
        max_request_bytes=max_request_bytes,
        max_collection_lengths=collections or {},
        max_numeric_values=numeric or {},
        deadline_field=deadline_field,
    )


# Every task-optional v7 tool has a reviewed foreground profile.  Request
# admission is intentionally based only on already-validated arguments; tools
# whose cost depends on workspace state keep their deeper scan/cardinality
# guards in the operation layer, before mutation begins.
FOREGROUND_EXECUTION_POLICIES: Mapping[str, ForegroundExecutionPolicy] = (
    MappingProxyType(
        {
            "memory_recall": _foreground_policy(),
            "memory_recall_hierarchical": _foreground_policy(),
            "context_compress": _foreground_policy(128 * 1024),
            "memory_store_batch": _foreground_policy(
                512 * 1024,
                collections={"records": 25},
            ),
            "document_ingest_url": _foreground_policy(),
            "memory_verify": _foreground_policy(128 * 1024),
            "sandbox_execute_python": _foreground_policy(
                128 * 1024,
                deadline_field="timeout_seconds",
            ),
            "code_index": _foreground_policy(
                collections={"patterns": 8},
            ),
            "code_impact_analyze": _foreground_policy(
                numeric={"max_depth": 5},
            ),
            "code_todos_scan": _foreground_policy(numeric={"limit": 200}),
            "code_todos_scan_and_store": _foreground_policy(
                numeric={"limit": 100},
            ),
            "code_refactor_propose": _foreground_policy(),
            "memory_related": _foreground_policy(numeric={"max_depth": 4}),
            "memory_chain_trace": _foreground_policy(
                numeric={"max_depth": 6},
            ),
            "knowledge_graph_get": _foreground_policy(
                collections={"record_ids": 100},
                numeric={"max_nodes": 200},
            ),
            "knowledge_graph_render": _foreground_policy(
                collections={"record_ids": 100},
                numeric={"max_nodes": 100},
            ),
            "community_rebuild": _foreground_policy(),
            "entity_backfill": _foreground_policy(),
            "entity_evolution_trace": _foreground_policy(),
            "memory_prune_preview": _foreground_policy(),
            "memory_prune": _foreground_policy(),
            "memory_duplicates_preview": _foreground_policy(),
            "memory_duplicates_cleanup": _foreground_policy(),
            "memory_compaction_preview": _foreground_policy(128 * 1024),
            "memory_compact": _foreground_policy(128 * 1024),
            "projection_rebuild": _foreground_policy(),
            "workspace_export": _foreground_policy(),
            "workspace_import": _foreground_policy(
                2 * 1024 * 1024,
                collections={"bundle.events": 1_000},
            ),
            "workspace_consolidate": _foreground_policy(
                collections={"source_workspace_ids": 4},
            ),
            "workspace_consolidation_preview": _foreground_policy(
                collections={"source_workspace_ids": 4},
            ),
            "workspace_consolidate_and_archive_sources": _foreground_policy(
                collections={"source_workspace_ids": 4},
            ),
            "dream_duplicates_preview": _foreground_policy(),
            "dream_duplicates_purge": _foreground_policy(),
            "decision_simulate": _foreground_policy(),
            "rule_evolution_analyze": _foreground_policy(),
            "decision_debate": _foreground_policy(
                128 * 1024,
                numeric={"max_rounds": 5},
            ),
        }
    )
)


class TaskExecutionError(RuntimeError):
    """Owned task/fallback failure with a stable v7 error code."""

    def __init__(self, code: str) -> None:
        if code not in {
            "TASK_REQUIRED",
            "TASKS_UNAVAILABLE",
            "DEADLINE_EXCEEDED",
            "CANCELLED",
        }:
            raise ValueError("task execution code is invalid")
        self.code = code
        super().__init__(code)


async def await_task_terminal(task: asyncio.Future[T]) -> T:
    """Drain one admitted task despite repeated caller cancellation."""

    while not task.done():
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def _await_event_despite_cancellation(event: asyncio.Event) -> None:
    """Let newly-owned work enter its cancellation-safe body before cancelling."""

    while not event.is_set():
        try:
            await asyncio.shield(event.wait())
        except asyncio.CancelledError:
            continue


def _timeout_seconds(value: object, *, allow_subsecond: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timeout_seconds must be between 1 and 60")
    try:
        timeout = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError("timeout_seconds must be between 1 and 60") from exc
    minimum = 0.001 if allow_subsecond else 1.0
    if not math.isfinite(timeout) or not minimum <= timeout <= 60.0:
        raise ValueError("timeout_seconds must be between 1 and 60")
    return timeout


def validate_sync_timeout_seconds(value: object) -> float:
    """Validate the public 1..60 second synchronous fallback bound."""

    return _timeout_seconds(value, allow_subsecond=False)


async def run_sync_fallback(
    operation: Callable[[], Awaitable[T]],
    *,
    estimated_to_fit: bool,
    timeout_seconds: int | float = 15,
    _test_allow_subsecond: bool = False,
) -> T:
    """Run one optional operation without leaving detached work behind."""
    if not callable(operation):
        raise ValueError("operation must be an awaitable factory")
    if type(estimated_to_fit) is not bool:
        raise ValueError("estimated_to_fit must be boolean")
    timeout = _timeout_seconds(timeout_seconds, allow_subsecond=_test_allow_subsecond)
    if not estimated_to_fit:
        raise TaskExecutionError("TASK_REQUIRED")

    child_started = asyncio.Event()

    async def started_operation() -> T:
        child_started.set()
        return await operation()

    child = asyncio.create_task(started_operation())
    try:
        # Shield keeps wait_for or caller cancellation from cancelling the
        # child before it has even entered the operation's try/finally body.
        return await asyncio.wait_for(asyncio.shield(child), timeout=timeout)
    except asyncio.TimeoutError as exc:
        await _await_event_despite_cancellation(child_started)
        if not child.done():
            child.cancel()
        try:
            terminal_result = await await_task_terminal(child)
        except (asyncio.CancelledError, Exception):
            pass
        else:
            # A durable operation can cross its commit point while deadline
            # cancellation is being delivered.  Its successful receipt is
            # authoritative and must win over the outer timeout.
            return terminal_result
        raise TaskExecutionError("DEADLINE_EXCEEDED") from exc
    except asyncio.CancelledError:
        await _await_event_despite_cancellation(child_started)
        if not child.done():
            child.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await await_task_terminal(child)
        raise


__all__ = [
    "DurableTaskExecution",
    "FOREGROUND_EXECUTION_POLICIES",
    "ForegroundExecutionPolicy",
    "TaskExecutionError",
    "await_task_terminal",
    "durable_task_arguments_sha256",
    "durable_task_execution_var",
    "is_durable_task_execution",
    "run_sync_fallback",
    "task_admission_only_var",
    "validate_sync_timeout_seconds",
]
