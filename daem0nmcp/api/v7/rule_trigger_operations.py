"""Canonical v7 rule and context-trigger operation adapters.

The retained rule and trigger tables are compatibility projections in the
active format-7 database.  This module never exposes their integer keys or
stored project paths: every public identity is resolved through the immutable
``PublicObjectIdRepository`` while the shared activation lock is held.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import os
import re
import secrets
import sqlite3
import threading
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from ...bounded_workers import BoundedWorkerBusyError, BoundedWorkerPool
from ...event_store import (
    GovernanceEventCommand,
    GovernanceEventStore,
    canonical_json_bytes,
    sha256_json,
)
from ...retrieval import RetrievalQuery
from ...schema_version import CURRENT_SCHEMA_VERSION
from ...similarity import TFIDFIndex
from ...trigger_security import (
    MAX_ACTIVE_TRIGGERS,
    SafeUserPattern,
    TriggerPatternError,
    bounded_glob_match,
    validate_active_trigger_count,
    validate_file_path,
    validate_glob_pattern,
)
from ...workspace import Workspace, WorkspaceRegistry
from .application import AdmittedRequest
from .errors import STABLE_ERROR_CODE_SET
from .models import MutationReceipt, Page, RecordSummary, RetrievalData
from .public_ids import (
    PublicObjectIdNotFound,
    PublicObjectIdRepository,
)
from .resources import RuleView
from .runtime_services import WorkspaceStorageResolver
from .tasks import await_task_terminal
from .tools import RuleCheckData, TriggerMatch, TriggerMatchData, TriggerView

_MAX_RULES = 1_000
_MAX_TRIGGER_MATCHES = 5
_CURSOR_RE = re.compile(r"^cur_([0-9a-f]{64})_([0-9a-f]{64})$")
_RULE_COLUMNS = (
    "rule_id,workspace_id,trigger,must_do_json,must_not_json,ask_first_json,"
    "warnings_json,priority,enabled,stream_version,source_event_id,"
    "created_at_us,updated_at_us"
)
_TRIGGER_COLUMNS = (
    "trigger_id,workspace_id,trigger_type,pattern,recall_query,categories_json,"
    "enabled,priority,stream_version,source_event_id,created_at_us,updated_at_us,"
    "deleted_at_us"
)
_REQUIRED_RULE_COLUMNS = frozenset(_RULE_COLUMNS.split(",")) | {"state_hash"}
_REQUIRED_TRIGGER_COLUMNS = frozenset(_TRIGGER_COLUMNS.split(",")) | {"state_hash"}
_REQUIRED_COMPATIBILITY_RULE_COLUMNS = frozenset(
    {
        "id",
        "trigger",
        "must_do",
        "must_not",
        "ask_first",
        "warnings",
        "priority",
        "enabled",
        "created_at",
    }
)
_REQUIRED_COMPATIBILITY_TRIGGER_COLUMNS = frozenset(
    {
        "id",
        "project_path",
        "trigger_type",
        "pattern",
        "recall_topic",
        "recall_categories",
        "is_active",
        "priority",
        "created_at",
        "trigger_count",
        "last_triggered",
    }
)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

_RULE_TRIGGER_WORKERS = BoundedWorkerPool(
    max_workers=4,
    thread_name_prefix="daem0nmcp-v7-rule-trigger",
)


class RuleTriggerOperationError(RuntimeError):
    """Stable, path-free business failure understood by the v7 router."""

    def __init__(self, code: str) -> None:
        if code not in STABLE_ERROR_CODE_SET:
            raise ValueError("rule/trigger operation error code is not stable")
        self.code = code
        super().__init__(code)


class _WorkerCancelledError(RuntimeError):
    pass


class RecallService(Protocol):
    def retrieve(
        self,
        workspace: Workspace,
        query: RetrievalQuery,
        linked_workspace_ids: frozenset[str],
    ) -> object: ...


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class RuleTriggerOperationDependencies:
    """Reviewed dependencies for rule and trigger operations."""

    storage_resolver: WorkspaceStorageResolver = field(
        default_factory=WorkspaceStorageResolver
    )
    clock: Callable[[], datetime] = field(default=_default_clock)
    pattern_matcher: SafeUserPattern = field(default_factory=SafeUserPattern)
    recall_service: RecallService | None = None
    cursor_secret: bytes = field(default_factory=lambda: secrets.token_bytes(32))

    def __post_init__(self) -> None:
        if not hasattr(self.storage_resolver, "locked_active"):
            raise TypeError("storage_resolver must provide locked_active")
        if not callable(self.clock):
            raise TypeError("clock must be callable")
        if not isinstance(self.pattern_matcher, SafeUserPattern):
            raise TypeError("pattern_matcher must be SafeUserPattern")
        if self.recall_service is not None and not callable(
            getattr(self.recall_service, "retrieve", None)
        ):
            raise TypeError("recall_service must provide retrieve")
        if not isinstance(self.cursor_secret, bytes) or len(self.cursor_secret) < 32:
            raise ValueError("cursor_secret must contain at least 32 bytes")


def _authorize(
    workspace: Workspace,
    request: AdmittedRequest,
    tool_name: str,
) -> None:
    if (
        not isinstance(workspace, Workspace)
        or not isinstance(request, AdmittedRequest)
        or request.tool_name != tool_name
        or request.workspace_id != workspace.workspace_id
    ):
        raise RuleTriggerOperationError("UNAUTHORIZED_WORKSPACE")
    try:
        canonical = workspace.root.resolve(strict=True)
        registered = WorkspaceRegistry([canonical], default_root=canonical).default
        exact_root = os.path.normcase(str(workspace.root)) == os.path.normcase(
            str(canonical)
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        raise RuleTriggerOperationError("UNAUTHORIZED_WORKSPACE") from None
    if registered.workspace_id != workspace.workspace_id or not exact_root:
        raise RuleTriggerOperationError("UNAUTHORIZED_WORKSPACE")


def _table_columns(connection: sqlite3.Connection, table: str) -> frozenset[str]:
    return frozenset(
        str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
    )


def _open_database(
    path: Path,
    *,
    writable: bool,
    domain: str,
) -> sqlite3.Connection:
    connection: sqlite3.Connection | None = None
    try:
        if domain == "rule":
            projection_table = "governance_rules"
            compatibility_table = "rules"
            projection_columns = _REQUIRED_RULE_COLUMNS
            compatibility_columns = _REQUIRED_COMPATIBILITY_RULE_COLUMNS
        elif domain == "trigger":
            projection_table = "governance_context_triggers"
            compatibility_table = "context_triggers"
            projection_columns = _REQUIRED_TRIGGER_COLUMNS
            compatibility_columns = _REQUIRED_COMPATIBILITY_TRIGGER_COLUMNS
        else:
            raise ValueError("unsupported operation domain")
        mode = "rw" if writable else "ro"
        connection = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode={mode}",
            uri=True,
            timeout=5.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        if not writable:
            connection.execute("PRAGMA query_only=ON")
        version_row = connection.execute(
            "SELECT COALESCE(MAX(version),0) FROM schema_version"
        ).fetchone()
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN (?,?,'public_object_ids','governance_events')",
                (projection_table, compatibility_table),
            )
        }
        required_tables = {
            projection_table,
            compatibility_table,
            "public_object_ids",
            "governance_events",
        }
        if (
            version_row is None
            or int(version_row[0]) < CURRENT_SCHEMA_VERSION
            or tables != required_tables
            or not projection_columns <= _table_columns(connection, projection_table)
            or not compatibility_columns
            <= _table_columns(connection, compatibility_table)
        ):
            raise RuleTriggerOperationError("CAPABILITY_DEGRADED")
        return connection
    except RuleTriggerOperationError:
        if connection is not None:
            connection.close()
        raise
    except Exception:
        if connection is not None:
            connection.close()
        raise RuleTriggerOperationError("CAPABILITY_DEGRADED") from None


def _now(dependencies: RuleTriggerOperationDependencies) -> datetime:
    try:
        value = dependencies.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError
        return value.astimezone(timezone.utc)
    except Exception:
        raise RuleTriggerOperationError("CAPABILITY_DEGRADED") from None


def _stored_timestamp(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .replace(tzinfo=None)
        .isoformat(sep=" ", timespec="microseconds")
    )


def _timestamp_us(value: datetime) -> int:
    try:
        delta = value.astimezone(timezone.utc) - _EPOCH
        result = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    except (OverflowError, ValueError):
        raise RuleTriggerOperationError("CAPABILITY_DEGRADED") from None
    if not 0 <= result <= 9_223_372_036_854_775_807:
        raise RuleTriggerOperationError("CAPABILITY_DEGRADED")
    return result


def _public_timestamp(value: object) -> datetime:
    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, int) and not isinstance(value, bool):
            return _EPOCH + timedelta(microseconds=value)
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value)
        else:
            raise ValueError
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        raise RuleTriggerOperationError("CAPABILITY_DEGRADED") from None


def _json_list(value: object) -> list[str]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, RecursionError):
        raise RuleTriggerOperationError("CAPABILITY_DEGRADED") from None
    if not isinstance(parsed, list) or not all(
        isinstance(item, str) for item in parsed
    ):
        raise RuleTriggerOperationError("CAPABILITY_DEGRADED")
    return parsed


def _encoded_list(values: object) -> str:
    if not isinstance(values, (list, set, frozenset, tuple)):
        raise RuleTriggerOperationError("INVALID_ARGUMENT")
    sequence = sorted(values) if isinstance(values, (set, frozenset)) else list(values)
    try:
        return canonical_json_bytes(sequence).decode("utf-8")
    except Exception:
        raise RuleTriggerOperationError("INVALID_ARGUMENT") from None


def _translate_error(error: Exception) -> RuleTriggerOperationError:
    if isinstance(error, RuleTriggerOperationError):
        return error
    if isinstance(error, PublicObjectIdNotFound):
        return RuleTriggerOperationError("NOT_FOUND")
    code = getattr(error, "code", None)
    if isinstance(code, str) and code in STABLE_ERROR_CODE_SET:
        return RuleTriggerOperationError(code)
    return RuleTriggerOperationError("CAPABILITY_DEGRADED")


def _legacy_id(workspace_id: str, domain: str, idempotency_key: str) -> int:
    digest = sha256_json(["daem0nmcp", "v7", domain, workspace_id, idempotency_key])
    value = int(digest[:16], 16) & ((1 << 63) - 1)
    return value or 1


def _cursor(
    dependencies: RuleTriggerOperationDependencies,
    domain: str,
    workspace_id: str,
    selector: object,
    public_id: str,
    position: object,
) -> str:
    payload = canonical_json_bytes(
        ["daem0nmcp", "v7", domain, workspace_id, selector, public_id, position]
    )
    signature = hmac.new(
        dependencies.cursor_secret,
        payload,
        hashlib.sha256,
    ).hexdigest()
    return f"cur_{public_id.rsplit('_', 1)[-1]}_{signature}"


def _cursor_public_id(cursor: str, prefix: str) -> str:
    match = _CURSOR_RE.fullmatch(cursor)
    if match is None:
        raise RuleTriggerOperationError("INVALID_ARGUMENT")
    return f"{prefix}_{match.group(1)}"


def _rule_view(
    row: sqlite3.Row,
) -> RuleView:
    try:
        enabled = row["enabled"]
        if enabled not in (0, 1):
            raise RuleTriggerOperationError("CAPABILITY_DEGRADED")
        return RuleView(
            rule_id=row["rule_id"],
            trigger=row["trigger"],
            must_do=_json_list(row["must_do_json"]),
            must_not=_json_list(row["must_not_json"]),
            ask_first=_json_list(row["ask_first_json"]),
            warnings=_json_list(row["warnings_json"]),
            priority=row["priority"],
            enabled=bool(enabled),
            created_at=_public_timestamp(row["created_at_us"]),
        )
    except RuleTriggerOperationError:
        raise
    except Exception:
        raise RuleTriggerOperationError("CAPABILITY_DEGRADED") from None


async def _run_read(operation: Callable[[], Any]) -> Any:
    worker = asyncio.create_task(_RULE_TRIGGER_WORKERS.run(operation))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as cancellation:
        with suppress(asyncio.CancelledError, Exception):
            await await_task_terminal(worker)
        raise cancellation
    except BoundedWorkerBusyError as exc:
        raise RuleTriggerOperationError("TASK_REQUIRED") from exc


async def _run_mutation(
    operation: Callable[[threading.Event], Any],
) -> Any:
    cancelled = threading.Event()
    worker = asyncio.create_task(
        _RULE_TRIGGER_WORKERS.run(lambda: operation(cancelled))
    )
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as cancellation:
        cancelled.set()
        try:
            result = await await_task_terminal(worker)
        except (_WorkerCancelledError, BoundedWorkerBusyError):
            raise cancellation from None
        except Exception:
            raise cancellation from None
        return result
    except BoundedWorkerBusyError as exc:
        raise RuleTriggerOperationError("TASK_REQUIRED") from exc


def _rule_create_sync(
    dependencies: RuleTriggerOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
    cancelled: threading.Event,
) -> RuleView:
    created_at = _now(dependencies)
    created_at_us = _timestamp_us(created_at)
    if cancelled.is_set():
        raise _WorkerCancelledError()
    source_id = _legacy_id(
        workspace.workspace_id,
        "rule-create",
        request.idempotency_key,
    )
    expected = {
        "trigger": request.trigger,
        "must_do": list(request.must_do),
        "must_not": list(request.must_not),
        "ask_first": list(request.ask_first),
        "warnings": list(request.warnings),
        "priority": request.priority,
        "enabled": True,
    }
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            connection = _open_database(
                active.path,
                writable=True,
                domain="rule",
            )
            try:
                connection.execute("BEGIN IMMEDIATE")
                repository = PublicObjectIdRepository(connection)
                public_id = repository.get_or_create(
                    workspace.workspace_id,
                    "rule",
                    source_id,
                )
                row = connection.execute(
                    f"SELECT {_RULE_COLUMNS} FROM governance_rules "
                    "WHERE workspace_id=? AND rule_id=? LIMIT 1",
                    (workspace.workspace_id, public_id),
                ).fetchone()
                if row is None:
                    state = {
                        "rule_id": public_id,
                        **expected,
                        "created_at_us": created_at_us,
                        "updated_at_us": created_at_us,
                    }
                    GovernanceEventStore(
                        connection, assume_transaction=True
                    ).append_and_project(
                        GovernanceEventCommand(
                            workspace_id=workspace.workspace_id,
                            stream_id=public_id,
                            stream_kind="rule",
                            event_type="rule.created",
                            occurred_at_us=created_at_us,
                            recorded_at_us=created_at_us,
                            actor_type="client",
                            correlation_id=request.idempotency_key,
                            payload=state,
                            expected_stream_version=1,
                        )
                    )
                    connection.execute(
                        "INSERT INTO rules "
                        "(id,trigger,must_do,must_not,ask_first,warnings,"
                        "priority,enabled,created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            source_id,
                            expected["trigger"],
                            _encoded_list(expected["must_do"]),
                            _encoded_list(expected["must_not"]),
                            _encoded_list(expected["ask_first"]),
                            _encoded_list(expected["warnings"]),
                            expected["priority"],
                            int(expected["enabled"]),
                            _stored_timestamp(created_at),
                        ),
                    )
                    row = connection.execute(
                        f"SELECT {_RULE_COLUMNS} FROM governance_rules "
                        "WHERE workspace_id=? AND rule_id=? LIMIT 1",
                        (workspace.workspace_id, public_id),
                    ).fetchone()
                else:
                    actual = {
                        "trigger": row["trigger"],
                        "must_do": _json_list(row["must_do_json"]),
                        "must_not": _json_list(row["must_not_json"]),
                        "ask_first": _json_list(row["ask_first_json"]),
                        "warnings": _json_list(row["warnings_json"]),
                        "priority": row["priority"],
                        "enabled": bool(row["enabled"]),
                    }
                    if actual != expected:
                        raise RuleTriggerOperationError("IDEMPOTENCY_CONFLICT")
                if row is None:
                    raise RuleTriggerOperationError("CAPABILITY_DEGRADED")
                result = _rule_view(row)
                if cancelled.is_set():
                    raise _WorkerCancelledError()
                connection.commit()
                return result
            except (RuleTriggerOperationError, _WorkerCancelledError):
                if connection.in_transaction:
                    connection.rollback()
                raise
            except Exception as exc:
                if connection.in_transaction:
                    connection.rollback()
                raise _translate_error(exc) from None
            finally:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
    except (RuleTriggerOperationError, _WorkerCancelledError):
        raise
    except Exception as exc:
        raise _translate_error(exc) from None


def _resolved_rule_row(
    connection: sqlite3.Connection,
    workspace_id: str,
    public_id: str,
) -> tuple[sqlite3.Row, int]:
    repository = PublicObjectIdRepository(connection)
    try:
        resolved = repository.resolve_public_id(
            workspace_id,
            "rule",
            public_id,
        )
    except PublicObjectIdNotFound:
        raise RuleTriggerOperationError("NOT_FOUND") from None
    except Exception as exc:
        raise _translate_error(exc) from None
    if isinstance(resolved.source_key, bool) or not isinstance(
        resolved.source_key, int
    ):
        raise RuleTriggerOperationError("CAPABILITY_DEGRADED")
    rows = connection.execute(
        f"SELECT {_RULE_COLUMNS} FROM governance_rules "
        "WHERE workspace_id=? AND rule_id=? LIMIT 2",
        (workspace_id, public_id),
    ).fetchall()
    if not rows:
        raise RuleTriggerOperationError("NOT_FOUND")
    if len(rows) != 1:
        raise RuleTriggerOperationError("CAPABILITY_DEGRADED")
    return rows[0], resolved.source_key


def _rule_update_sync(
    dependencies: RuleTriggerOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
    cancelled: threading.Event,
) -> RuleView:
    if cancelled.is_set():
        raise _WorkerCancelledError()
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            connection = _open_database(
                active.path,
                writable=True,
                domain="rule",
            )
            try:
                connection.execute("BEGIN IMMEDIATE")
                row, source_id = _resolved_rule_row(
                    connection,
                    workspace.workspace_id,
                    request.rule_id,
                )
                patch = request.patch
                if not isinstance(patch, Mapping) or not patch:
                    raise RuleTriggerOperationError("INVALID_ARGUMENT")
                state: dict[str, object] = {
                    "rule_id": row["rule_id"],
                    "trigger": row["trigger"],
                    "must_do": _json_list(row["must_do_json"]),
                    "must_not": _json_list(row["must_not_json"]),
                    "ask_first": _json_list(row["ask_first_json"]),
                    "warnings": _json_list(row["warnings_json"]),
                    "priority": row["priority"],
                    "enabled": bool(row["enabled"]),
                    "created_at_us": row["created_at_us"],
                    "updated_at_us": row["updated_at_us"],
                }
                changed = False
                for field_name in (
                    "trigger",
                    "must_do",
                    "must_not",
                    "ask_first",
                    "warnings",
                    "priority",
                    "enabled",
                ):
                    if field_name not in patch or patch[field_name] is None:
                        continue
                    value = patch[field_name]
                    if field_name in {
                        "must_do",
                        "must_not",
                        "ask_first",
                        "warnings",
                    }:
                        value = list(value)
                    elif field_name == "enabled":
                        value = bool(value)
                    if state[field_name] != value:
                        state[field_name] = value
                        changed = True
                if changed:
                    updated_at_us = _timestamp_us(_now(dependencies))
                    created_at_us = state["created_at_us"]
                    if type(created_at_us) is not int or updated_at_us < created_at_us:
                        raise RuleTriggerOperationError("CAPABILITY_DEGRADED")
                    state["updated_at_us"] = updated_at_us
                    GovernanceEventStore(
                        connection, assume_transaction=True
                    ).append_and_project(
                        GovernanceEventCommand(
                            workspace_id=workspace.workspace_id,
                            stream_id=request.rule_id,
                            stream_kind="rule",
                            event_type="rule.updated",
                            occurred_at_us=updated_at_us,
                            recorded_at_us=updated_at_us,
                            actor_type="client",
                            payload=state,
                            expected_stream_version=int(row["stream_version"]) + 1,
                        )
                    )
                    compatibility = connection.execute(
                        "UPDATE rules SET trigger=?,must_do=?,must_not=?,"
                        "ask_first=?,warnings=?,priority=?,enabled=? WHERE id=?",
                        (
                            state["trigger"],
                            _encoded_list(state["must_do"]),
                            _encoded_list(state["must_not"]),
                            _encoded_list(state["ask_first"]),
                            _encoded_list(state["warnings"]),
                            state["priority"],
                            int(bool(state["enabled"])),
                            source_id,
                        ),
                    )
                    if compatibility.rowcount != 1:
                        raise RuleTriggerOperationError("CAPABILITY_DEGRADED")
                updated = connection.execute(
                    f"SELECT {_RULE_COLUMNS} FROM governance_rules "
                    "WHERE workspace_id=? AND rule_id=? LIMIT 1",
                    (workspace.workspace_id, request.rule_id),
                ).fetchone()
                if updated is None:
                    raise RuleTriggerOperationError("CAPABILITY_DEGRADED")
                result = _rule_view(updated)
                if cancelled.is_set():
                    raise _WorkerCancelledError()
                connection.commit()
                return result
            except (RuleTriggerOperationError, _WorkerCancelledError):
                if connection.in_transaction:
                    connection.rollback()
                raise
            except Exception as exc:
                if connection.in_transaction:
                    connection.rollback()
                raise _translate_error(exc) from None
            finally:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
    except (RuleTriggerOperationError, _WorkerCancelledError):
        raise
    except Exception as exc:
        raise _translate_error(exc) from None


def _rule_cursor_position(row: sqlite3.Row) -> list[object]:
    return [row["priority"], row["created_at_us"], row["rule_id"]]


def _rule_list_sync(
    dependencies: RuleTriggerOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
) -> Page[RuleView]:
    selector = {"enabled_only": request.enabled_only}
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            connection = _open_database(
                active.path,
                writable=False,
                domain="rule",
            )
            try:
                cursor_row: sqlite3.Row | None = None
                if request.cursor is not None:
                    public_id = _cursor_public_id(request.cursor, "rule")
                    cursor_row, _ = _resolved_rule_row(
                        connection,
                        workspace.workspace_id,
                        public_id,
                    )
                    expected = _cursor(
                        dependencies,
                        "rule-list",
                        workspace.workspace_id,
                        selector,
                        public_id,
                        _rule_cursor_position(cursor_row),
                    )
                    if not hmac.compare_digest(request.cursor, expected):
                        raise RuleTriggerOperationError("INVALID_ARGUMENT")
                    if request.enabled_only and not bool(cursor_row["enabled"]):
                        raise RuleTriggerOperationError("INVALID_ARGUMENT")
                where = "workspace_id=?"
                parameters: list[object] = [workspace.workspace_id]
                if request.enabled_only:
                    where += " AND enabled=1"
                if cursor_row is not None:
                    where += (
                        " AND (priority<? OR (priority=? AND created_at_us<?) OR "
                        "(priority=? AND created_at_us=? AND rule_id>?))"
                    )
                    parameters.extend(
                        [
                            cursor_row["priority"],
                            cursor_row["priority"],
                            cursor_row["created_at_us"],
                            cursor_row["priority"],
                            cursor_row["created_at_us"],
                            cursor_row["rule_id"],
                        ]
                    )
                parameters.append(request.limit + 1)
                rows = connection.execute(
                    f"SELECT {_RULE_COLUMNS} FROM governance_rules WHERE {where} "
                    "ORDER BY priority DESC,created_at_us DESC,rule_id ASC LIMIT ?",
                    parameters,
                ).fetchall()
                more = len(rows) > request.limit
                selected = rows[: request.limit]
                items = [_rule_view(row) for row in selected]
                next_cursor = None
                if more and selected:
                    next_cursor = _cursor(
                        dependencies,
                        "rule-list",
                        workspace.workspace_id,
                        selector,
                        items[-1].rule_id,
                        _rule_cursor_position(selected[-1]),
                    )
                return Page[RuleView](
                    items=items,
                    next_cursor=next_cursor,
                    truncated=more,
                )
            finally:
                connection.close()
    except RuleTriggerOperationError:
        raise
    except Exception as exc:
        raise _translate_error(exc) from None


def _deduplicated(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))[:50]


def _rule_check_sync(
    dependencies: RuleTriggerOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
) -> RuleCheckData:
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            connection = _open_database(
                active.path,
                writable=False,
                domain="rule",
            )
            try:
                rows = connection.execute(
                    f"SELECT {_RULE_COLUMNS} FROM governance_rules "
                    "WHERE workspace_id=? AND enabled=1 "
                    "ORDER BY priority DESC,created_at_us DESC,rule_id ASC LIMIT ?",
                    (workspace.workspace_id, _MAX_RULES + 1),
                ).fetchall()
                if len(rows) > _MAX_RULES:
                    raise RuleTriggerOperationError("CAPABILITY_DEGRADED")
                indexed_rows = dict(enumerate(rows, start=1))
                views_by_id = {
                    index: _rule_view(row) for index, row in indexed_rows.items()
                }
                index = TFIDFIndex()
                for document_id, row in indexed_rows.items():
                    index.add_document(document_id, str(row["trigger"]))
                context_text = canonical_json_bytes(request.context).decode("utf-8")
                query = request.proposed_action + "\n" + context_text
                scores = dict(
                    index.search(
                        query,
                        top_k=_MAX_RULES,
                        threshold=0.15,
                    )
                )
                matched_rows = sorted(
                    (
                        (document_id, row)
                        for document_id, row in indexed_rows.items()
                        if document_id in scores
                    ),
                    key=lambda item: (
                        -int(item[1]["priority"]),
                        -scores[item[0]],
                        int(item[1]["created_at_us"]),
                        str(item[1]["rule_id"]),
                    ),
                )[:50]
                matched = [views_by_id[document_id] for document_id, _ in matched_rows]
                return RuleCheckData(
                    matched_rules=matched,
                    must_do=_deduplicated(
                        [value for rule in matched for value in rule.must_do]
                    ),
                    must_not=_deduplicated(
                        [value for rule in matched for value in rule.must_not]
                    ),
                    ask_first=_deduplicated(
                        [value for rule in matched for value in rule.ask_first]
                    ),
                    warnings=_deduplicated(
                        [value for rule in matched for value in rule.warnings]
                    ),
                )
            finally:
                connection.close()
    except RuleTriggerOperationError:
        raise
    except Exception as exc:
        raise _translate_error(exc) from None


_PUBLIC_TO_STORED_TRIGGER_TYPE = MappingProxyType(
    {"file": "file_pattern", "tag": "tag_match", "entity": "entity_match"}
)


def _trigger_view(
    row: sqlite3.Row,
) -> TriggerView:
    try:
        enabled = row["enabled"]
        if enabled not in (0, 1):
            raise RuleTriggerOperationError("CAPABILITY_DEGRADED")
        trigger_type = row["trigger_type"]
        if trigger_type not in _PUBLIC_TO_STORED_TRIGGER_TYPE:
            raise RuleTriggerOperationError("CAPABILITY_DEGRADED")
        categories = _json_list(row["categories_json"])
        return TriggerView.model_validate(
            {
                "trigger_id": row["trigger_id"],
                "trigger_type": trigger_type,
                "pattern": row["pattern"],
                "recall_query": row["recall_query"],
                "categories": None if not categories else set(categories),
                "enabled": bool(enabled),
                "updated_at": _public_timestamp(row["updated_at_us"]),
            }
        )
    except RuleTriggerOperationError:
        raise
    except Exception:
        raise RuleTriggerOperationError("CAPABILITY_DEGRADED") from None


def _validate_trigger_pattern(
    dependencies: RuleTriggerOperationDependencies,
    trigger_type: str,
    pattern: str,
) -> str:
    try:
        stored_type = _PUBLIC_TO_STORED_TRIGGER_TYPE[trigger_type]
        if trigger_type == "file":
            validate_glob_pattern(pattern)
        else:
            dependencies.pattern_matcher.validate(pattern)
        return stored_type
    except (KeyError, TriggerPatternError):
        raise RuleTriggerOperationError("INVALID_ARGUMENT") from None


def _trigger_create_sync(
    dependencies: RuleTriggerOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
    cancelled: threading.Event,
) -> TriggerView:
    stored_type = _validate_trigger_pattern(
        dependencies,
        request.trigger_type,
        request.pattern,
    )
    created_at = _now(dependencies)
    created_at_us = _timestamp_us(created_at)
    if cancelled.is_set():
        raise _WorkerCancelledError()
    source_id = _legacy_id(
        workspace.workspace_id,
        "context-trigger-create",
        request.idempotency_key,
    )
    expected = {
        "trigger_type": request.trigger_type,
        "pattern": request.pattern,
        "recall_query": request.recall_query,
        "categories": sorted(request.categories or set()),
        "enabled": bool(request.enabled),
        "priority": 0,
    }
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            connection = _open_database(
                active.path,
                writable=True,
                domain="trigger",
            )
            try:
                connection.execute("BEGIN IMMEDIATE")
                repository = PublicObjectIdRepository(connection)
                public_id = repository.get_or_create(
                    workspace.workspace_id,
                    "trigger",
                    source_id,
                )
                row = connection.execute(
                    f"SELECT {_TRIGGER_COLUMNS} "
                    "FROM governance_context_triggers "
                    "WHERE workspace_id=? AND trigger_id=? LIMIT 1",
                    (workspace.workspace_id, public_id),
                ).fetchone()
                if row is None:
                    if expected["enabled"]:
                        active_count = int(
                            connection.execute(
                                "SELECT count(*) "
                                "FROM governance_context_triggers "
                                "WHERE workspace_id=? AND enabled=1 "
                                "AND deleted_at_us IS NULL",
                                (workspace.workspace_id,),
                            ).fetchone()[0]
                        )
                        try:
                            validate_active_trigger_count(active_count + 1)
                        except TriggerPatternError:
                            raise RuleTriggerOperationError(
                                "INVALID_ARGUMENT"
                            ) from None
                    state = {
                        "trigger_id": public_id,
                        **expected,
                        "created_at_us": created_at_us,
                        "updated_at_us": created_at_us,
                        "deleted_at_us": None,
                    }
                    GovernanceEventStore(
                        connection, assume_transaction=True
                    ).append_and_project(
                        GovernanceEventCommand(
                            workspace_id=workspace.workspace_id,
                            stream_id=public_id,
                            stream_kind="trigger",
                            event_type="context_trigger.created",
                            occurred_at_us=created_at_us,
                            recorded_at_us=created_at_us,
                            actor_type="client",
                            correlation_id=request.idempotency_key,
                            payload=state,
                            expected_stream_version=1,
                        )
                    )
                    connection.execute(
                        "INSERT INTO context_triggers "
                        "(id,project_path,trigger_type,pattern,recall_topic,"
                        "recall_categories,is_active,priority,created_at,"
                        "trigger_count,last_triggered) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            source_id,
                            str(workspace.root),
                            stored_type,
                            expected["pattern"],
                            expected["recall_query"],
                            _encoded_list(expected["categories"]),
                            int(expected["enabled"]),
                            0,
                            _stored_timestamp(created_at),
                            0,
                            None,
                        ),
                    )
                    row = connection.execute(
                        f"SELECT {_TRIGGER_COLUMNS} "
                        "FROM governance_context_triggers "
                        "WHERE workspace_id=? AND trigger_id=? LIMIT 1",
                        (workspace.workspace_id, public_id),
                    ).fetchone()
                else:
                    actual = {
                        "trigger_type": row["trigger_type"],
                        "pattern": row["pattern"],
                        "recall_query": row["recall_query"],
                        "categories": _json_list(row["categories_json"]),
                        "enabled": bool(row["enabled"]),
                        "priority": row["priority"],
                    }
                    if row["deleted_at_us"] is not None or actual != expected:
                        raise RuleTriggerOperationError("IDEMPOTENCY_CONFLICT")
                if row is None:
                    raise RuleTriggerOperationError("CAPABILITY_DEGRADED")
                result = _trigger_view(row)
                if cancelled.is_set():
                    raise _WorkerCancelledError()
                connection.commit()
                return result
            except (RuleTriggerOperationError, _WorkerCancelledError):
                if connection.in_transaction:
                    connection.rollback()
                raise
            except Exception as exc:
                if connection.in_transaction:
                    connection.rollback()
                raise _translate_error(exc) from None
            finally:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
    except (RuleTriggerOperationError, _WorkerCancelledError):
        raise
    except Exception as exc:
        raise _translate_error(exc) from None


def _resolved_trigger_source(
    connection: sqlite3.Connection,
    workspace_id: str,
    public_id: str,
) -> tuple[int, PublicObjectIdRepository]:
    repository = PublicObjectIdRepository(connection)
    try:
        resolved = repository.resolve_public_id(
            workspace_id,
            "trigger",
            public_id,
        )
    except PublicObjectIdNotFound:
        raise RuleTriggerOperationError("NOT_FOUND") from None
    except Exception as exc:
        raise _translate_error(exc) from None
    if isinstance(resolved.source_key, bool) or not isinstance(
        resolved.source_key, int
    ):
        raise RuleTriggerOperationError("CAPABILITY_DEGRADED")
    return resolved.source_key, repository


def _resolved_trigger_row(
    connection: sqlite3.Connection,
    workspace: Workspace,
    public_id: str,
) -> tuple[sqlite3.Row, int]:
    source_id, _ = _resolved_trigger_source(
        connection,
        workspace.workspace_id,
        public_id,
    )
    rows = connection.execute(
        f"SELECT {_TRIGGER_COLUMNS} FROM governance_context_triggers "
        "WHERE workspace_id=? AND trigger_id=? AND deleted_at_us IS NULL LIMIT 2",
        (workspace.workspace_id, public_id),
    ).fetchall()
    if not rows:
        raise RuleTriggerOperationError("NOT_FOUND")
    if len(rows) != 1:
        raise RuleTriggerOperationError("CAPABILITY_DEGRADED")
    return rows[0], source_id


def _trigger_cursor_position(row: sqlite3.Row) -> list[object]:
    return [row["created_at_us"], row["trigger_id"]]


def _trigger_list_sync(
    dependencies: RuleTriggerOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
) -> Page[TriggerView]:
    selector = {"active_only": request.active_only}
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            connection = _open_database(
                active.path,
                writable=False,
                domain="trigger",
            )
            try:
                cursor_row: sqlite3.Row | None = None
                if request.cursor is not None:
                    public_id = _cursor_public_id(request.cursor, "trg")
                    cursor_row, _ = _resolved_trigger_row(
                        connection,
                        workspace,
                        public_id,
                    )
                    expected = _cursor(
                        dependencies,
                        "context-trigger-list",
                        workspace.workspace_id,
                        selector,
                        public_id,
                        _trigger_cursor_position(cursor_row),
                    )
                    if not hmac.compare_digest(request.cursor, expected):
                        raise RuleTriggerOperationError("INVALID_ARGUMENT")
                    if request.active_only and not bool(cursor_row["enabled"]):
                        raise RuleTriggerOperationError("INVALID_ARGUMENT")
                where = "workspace_id=? AND deleted_at_us IS NULL"
                parameters: list[object] = [workspace.workspace_id]
                if request.active_only:
                    where += " AND enabled=1"
                if cursor_row is not None:
                    where += (
                        " AND (created_at_us<? OR (created_at_us=? AND trigger_id>?))"
                    )
                    parameters.extend(
                        [
                            cursor_row["created_at_us"],
                            cursor_row["created_at_us"],
                            cursor_row["trigger_id"],
                        ]
                    )
                parameters.append(request.limit + 1)
                rows = connection.execute(
                    f"SELECT {_TRIGGER_COLUMNS} "
                    "FROM governance_context_triggers "
                    f"WHERE {where} ORDER BY created_at_us DESC,trigger_id ASC LIMIT ?",
                    parameters,
                ).fetchall()
                more = len(rows) > request.limit
                selected = rows[: request.limit]
                items = [_trigger_view(row) for row in selected]
                next_cursor = None
                if more and selected:
                    next_cursor = _cursor(
                        dependencies,
                        "context-trigger-list",
                        workspace.workspace_id,
                        selector,
                        items[-1].trigger_id,
                        _trigger_cursor_position(selected[-1]),
                    )
                return Page[TriggerView](
                    items=items,
                    next_cursor=next_cursor,
                    truncated=more,
                )
            finally:
                connection.close()
    except RuleTriggerOperationError:
        raise
    except Exception as exc:
        raise _translate_error(exc) from None


def _operation_id(*parts: object) -> str:
    return "op_" + sha256_json(["daem0nmcp", "v7", "rule-trigger-operation", *parts])


def _trigger_delete_sync(
    dependencies: RuleTriggerOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
    cancelled: threading.Event,
) -> MutationReceipt:
    if cancelled.is_set():
        raise _WorkerCancelledError()
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            connection = _open_database(
                active.path,
                writable=True,
                domain="trigger",
            )
            try:
                connection.execute("BEGIN IMMEDIATE")
                source_id, _ = _resolved_trigger_source(
                    connection,
                    workspace.workspace_id,
                    request.trigger_id,
                )
                row = connection.execute(
                    f"SELECT {_TRIGGER_COLUMNS} "
                    "FROM governance_context_triggers "
                    "WHERE workspace_id=? AND trigger_id=? LIMIT 1",
                    (workspace.workspace_id, request.trigger_id),
                ).fetchone()
                if row is None:
                    raise RuleTriggerOperationError("CAPABILITY_DEGRADED")
                changed = row["deleted_at_us"] is None
                event_id = str(row["source_event_id"])
                if changed:
                    deleted_at_us = _timestamp_us(_now(dependencies))
                    if deleted_at_us < int(row["created_at_us"]):
                        raise RuleTriggerOperationError("CAPABILITY_DEGRADED")
                    state = {
                        "trigger_id": row["trigger_id"],
                        "trigger_type": row["trigger_type"],
                        "pattern": row["pattern"],
                        "recall_query": row["recall_query"],
                        "categories": _json_list(row["categories_json"]),
                        "enabled": False,
                        "priority": row["priority"],
                        "created_at_us": row["created_at_us"],
                        "updated_at_us": deleted_at_us,
                        "deleted_at_us": deleted_at_us,
                    }
                    appended = GovernanceEventStore(
                        connection, assume_transaction=True
                    ).append_and_project(
                        GovernanceEventCommand(
                            workspace_id=workspace.workspace_id,
                            stream_id=request.trigger_id,
                            stream_kind="trigger",
                            event_type="context_trigger.deleted",
                            occurred_at_us=deleted_at_us,
                            recorded_at_us=deleted_at_us,
                            actor_type="client",
                            payload=state,
                            expected_stream_version=int(row["stream_version"]) + 1,
                        )
                    )
                    event_id = appended.event_id
                    compatibility = connection.execute(
                        "UPDATE context_triggers SET is_active=0 "
                        "WHERE id=? AND project_path=?",
                        (source_id, str(workspace.root)),
                    )
                    if compatibility.rowcount != 1:
                        raise RuleTriggerOperationError("CAPABILITY_DEGRADED")
                result = MutationReceipt(
                    operation_id=_operation_id(
                        workspace.workspace_id,
                        "context-trigger-delete",
                        request.trigger_id,
                    ),
                    affected_ids=[request.trigger_id],
                    event_ids=[event_id],
                    counts={"selected": 1, "changed": int(changed)},
                    idempotent_replay=not changed,
                )
                if cancelled.is_set():
                    raise _WorkerCancelledError()
                connection.commit()
                return result
            except (RuleTriggerOperationError, _WorkerCancelledError):
                if connection.in_transaction:
                    connection.rollback()
                raise
            except Exception as exc:
                if connection.in_transaction:
                    connection.rollback()
                raise _translate_error(exc) from None
            finally:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
    except (RuleTriggerOperationError, _WorkerCancelledError):
        raise
    except Exception as exc:
        raise _translate_error(exc) from None


@dataclass(frozen=True, slots=True)
class _MatchedTrigger:
    trigger: TriggerView
    matched_value: str


def _matched_value(
    dependencies: RuleTriggerOperationDependencies,
    row: sqlite3.Row,
    request: AdmittedRequest,
) -> str | None:
    trigger_type = row["trigger_type"]
    pattern = str(row["pattern"])
    try:
        if trigger_type == "file":
            if request.relative_file_path is None:
                return None
            return (
                request.relative_file_path
                if bounded_glob_match(pattern, request.relative_file_path).matched
                else None
            )
        if trigger_type == "tag":
            values = request.tags
            result = dependencies.pattern_matcher.matches(
                row["trigger_id"], pattern, values, field="tags"
            )
        elif trigger_type == "entity":
            values = request.entities
            result = dependencies.pattern_matcher.matches(
                row["trigger_id"], pattern, values, field="entities"
            )
        else:
            raise RuleTriggerOperationError("CAPABILITY_DEGRADED")
        if not result.matched:
            return None
        index = result.candidates_evaluated - 1
        if not 0 <= index < len(values):
            raise RuleTriggerOperationError("CAPABILITY_DEGRADED")
        return values[index]
    except RuleTriggerOperationError:
        raise
    except TriggerPatternError:
        raise RuleTriggerOperationError("CAPABILITY_DEGRADED") from None


def _trigger_matches_sync(
    dependencies: RuleTriggerOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
) -> tuple[list[_MatchedTrigger], bool]:
    try:
        if request.relative_file_path is not None:
            validate_file_path(request.relative_file_path)
        dependencies.pattern_matcher.validate_candidates(request.tags, field="tags")
        dependencies.pattern_matcher.validate_candidates(
            request.entities, field="entities"
        )
        dependencies.pattern_matcher.validate_candidate_total(
            len(request.tags) + len(request.entities)
        )
    except TriggerPatternError:
        raise RuleTriggerOperationError("INVALID_ARGUMENT") from None
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            connection = _open_database(
                active.path,
                writable=False,
                domain="trigger",
            )
            try:
                rows = connection.execute(
                    f"SELECT {_TRIGGER_COLUMNS} "
                    "FROM governance_context_triggers "
                    "WHERE workspace_id=? AND enabled=1 "
                    "AND deleted_at_us IS NULL "
                    "ORDER BY priority DESC,created_at_us ASC,trigger_id ASC LIMIT ?",
                    (workspace.workspace_id, MAX_ACTIVE_TRIGGERS + 1),
                ).fetchall()
                try:
                    validate_active_trigger_count(len(rows))
                except TriggerPatternError:
                    raise RuleTriggerOperationError("CAPABILITY_DEGRADED") from None
                matches: list[_MatchedTrigger] = []
                truncated = False
                for row in rows:
                    value = _matched_value(dependencies, row, request)
                    if value is None:
                        continue
                    if len(matches) == _MAX_TRIGGER_MATCHES:
                        truncated = True
                        break
                    matches.append(
                        _MatchedTrigger(
                            trigger=_trigger_view(row),
                            matched_value=value,
                        )
                    )
                return matches, truncated
            finally:
                connection.close()
    except RuleTriggerOperationError:
        raise
    except Exception as exc:
        raise _translate_error(exc) from None


async def _recall_for_trigger(
    dependencies: RuleTriggerOperationDependencies,
    workspace: Workspace,
    matched: _MatchedTrigger,
    limit: int,
) -> list[RecordSummary]:
    service = dependencies.recall_service
    if service is None:
        raise RuleTriggerOperationError("CAPABILITY_DEGRADED")
    try:
        query = RetrievalQuery(
            workspace_id=workspace.workspace_id,
            text=matched.trigger.recall_query,
            limit=limit,
            candidate_limit=max(50, limit),
            categories=(
                None
                if matched.trigger.categories is None
                else frozenset(matched.trigger.categories)
            ),
        )
        value = service.retrieve(workspace, query, frozenset())
        if inspect.isawaitable(value):
            value = await value
        result = RetrievalData.model_validate(value)
        records: list[RecordSummary] = []
        seen: set[str] = set()
        for item in result.items:
            if item.record.record_id in seen:
                continue
            seen.add(item.record.record_id)
            records.append(item.record)
            if len(records) == limit:
                break
        return records
    except asyncio.CancelledError:
        raise
    except RuleTriggerOperationError:
        raise
    except Exception as exc:
        raise _translate_error(exc) from None


def build_rule_trigger_operations(
    dependencies: RuleTriggerOperationDependencies,
) -> Mapping[str, Callable[..., Any]]:
    """Return the exact immutable rule/trigger operation registry."""

    if not isinstance(dependencies, RuleTriggerOperationDependencies):
        raise TypeError("dependencies must be RuleTriggerOperationDependencies")

    async def rule_create(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> RuleView:
        _authorize(workspace, request, "rule_create")
        return await _run_mutation(
            lambda cancelled: _rule_create_sync(
                dependencies,
                workspace,
                request,
                cancelled,
            )
        )

    async def rule_update(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> RuleView:
        _authorize(workspace, request, "rule_update")
        return await _run_mutation(
            lambda cancelled: _rule_update_sync(
                dependencies,
                workspace,
                request,
                cancelled,
            )
        )

    async def rule_list(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> Page[RuleView]:
        _authorize(workspace, request, "rule_list")
        return await _run_read(
            lambda: _rule_list_sync(dependencies, workspace, request)
        )

    async def rule_check(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> RuleCheckData:
        _authorize(workspace, request, "rule_check")
        return await _run_read(
            lambda: _rule_check_sync(dependencies, workspace, request)
        )

    async def context_trigger_create(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> TriggerView:
        _authorize(workspace, request, "context_trigger_create")
        return await _run_mutation(
            lambda cancelled: _trigger_create_sync(
                dependencies,
                workspace,
                request,
                cancelled,
            )
        )

    async def context_trigger_delete(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> MutationReceipt:
        _authorize(workspace, request, "context_trigger_delete")
        return await _run_mutation(
            lambda cancelled: _trigger_delete_sync(
                dependencies,
                workspace,
                request,
                cancelled,
            )
        )

    async def context_trigger_list(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> Page[TriggerView]:
        _authorize(workspace, request, "context_trigger_list")
        return await _run_read(
            lambda: _trigger_list_sync(dependencies, workspace, request)
        )

    async def context_triggers_match(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> TriggerMatchData:
        _authorize(workspace, request, "context_triggers_match")
        matched, truncated = await _run_read(
            lambda: _trigger_matches_sync(dependencies, workspace, request)
        )
        matches: list[TriggerMatch] = []
        for item in matched:
            records = await _recall_for_trigger(
                dependencies,
                workspace,
                item,
                request.limit,
            )
            matches.append(
                TriggerMatch(
                    trigger=item.trigger,
                    matched_value=item.matched_value,
                    records=records,
                )
            )
        return TriggerMatchData(matches=matches, truncated=truncated)

    return MappingProxyType(
        {
            "context_trigger_create": context_trigger_create,
            "context_trigger_delete": context_trigger_delete,
            "context_trigger_list": context_trigger_list,
            "context_triggers_match": context_triggers_match,
            "rule_check": rule_check,
            "rule_create": rule_create,
            "rule_list": rule_list,
            "rule_update": rule_update,
        }
    )


__all__ = [
    "RuleTriggerOperationDependencies",
    "RuleTriggerOperationError",
    "build_rule_trigger_operations",
]
