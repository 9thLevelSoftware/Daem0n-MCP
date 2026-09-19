"""Canonical read adapters for generation-scoped v7 discovery data.

Every handler reads or builds workspace- and generation-scoped v7 projections.
Retained v6 discovery tables are deliberately not consulted here.
"""

# ruff: noqa: I001 -- preserve the repository's CRLF import block.

from __future__ import annotations

import ast
import asyncio
import base64
import hashlib
import hmac
import inspect
import json
import os
import posixpath
import re
import secrets
import sqlite3
import threading
import unicodedata
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Protocol

from ...bounded_workers import BoundedWorkerBusyError, BoundedWorkerPool
from ...discovery_projection import (
    CodeEdgeProjectionSeed,
    CodeEntityProjectionSeed,
    CodeProjectionIntegrityError,
    DiscoveryProjectionBuilder,
    verify_code_projection,
)
from ...event_store import canonical_json_bytes, parse_canonical_json, sha256_json
from ...retrieval import RetrievalQuery
from ...schema_version import REQUIRED_V7_SCHEMA_VERSIONS
from ...workspace import (
    IndexPathError,
    Workspace,
    WorkspaceRegistry,
    resolve_index_file,
    resolve_index_target,
    validate_index_patterns,
)
from .application import AdmittedRequest
from .errors import STABLE_ERROR_CODE_SET
from .models import CapabilityState, Page, RecordSummary, RetrievalData
from .public_ids import (
    PublicObjectIdNotFound,
    PublicObjectIdRepository,
    PublicObjectKind,
    StaleProjectionId,
    derive_public_object_id,
)
from .runtime_protocols import ActiveStorageResolver, WorkerPool
from .runtime_services import RuntimeServiceError, WorkspaceStorageResolver
from .tools import (
    CodeEntitySummary,
    CodeIndexData,
    CommunityDetail,
    CommunitySummary,
    DiagnosticSummary,
    EntitySummary,
    KnowledgeGraphStatsData,
    ProjectionManifest,
)


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_REQUIRED_TABLES = frozenset(
    {
        "memory_events",
        "memory_fact_versions",
        "memory_records",
        "memory_relationship_versions",
        "projection_manifests",
        "schema_version",
        "public_object_ids",
        "discovery_projection_partitions",
        "discovery_entities",
        "discovery_entity_records",
        "discovery_communities",
        "discovery_community_members",
        "discovery_code_entities",
    }
)
_CURSOR_RE = re.compile(r"^cur_v1\.([A-Za-z0-9_-]+)\.([0-9a-f]{64})$")
_ENTITY_ID_RE = re.compile(r"^ent_[0-9a-f]{64}$")
_COMMUNITY_ID_RE = re.compile(r"^com_[0-9a-f]{64}$")
_CODE_ID_RE = re.compile(r"^code_[0-9a-f]{64}$")
_RECORD_ID_RE = re.compile(r"^mem_[0-9a-f]{64}$")
_ENTITY_TYPE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,79}$")
_CODE_EXTENSION_RE = re.compile(r"^\.[a-z0-9][a-z0-9_+-]{0,15}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_MAX_PARTITION_ROWS = 200_000
_MAX_PARTITION_MEMBERSHIPS = 1_000_000
_MAX_INDEX_FILES = 10_000
_MAX_INDEX_FILE_BYTES = 5 * 1024 * 1024
_MAX_INDEX_TOTAL_BYTES = 100 * 1024 * 1024
_MAX_INDEX_ENTITIES = 200_000
_MAX_INDEX_EDGES = 200_000
_MAX_RELATIONSHIPS_PER_FILE = 50_000
_INDEX_SKIP_PARTS = frozenset(
    {
        ".git",
        ".daem0nmcp",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "target",
        "vendor",
        "venv",
    }
)
_CODE_KINDS = frozenset(
    {"file", "module", "class", "function", "method", "variable", "symbol"}
)
_RECORD_COLUMNS = (
    "record_id,record_type,content,content_hash,tags_json,file_path,"
    "file_path_relative,archived,deleted_at_us,created_at_us,updated_at_us"
)
_QUALIFIED_RECORD_COLUMNS = ",".join(
    f"record.{column.strip()}" for column in _RECORD_COLUMNS.split(",")
)


class DiscoveryOperationError(RuntimeError):
    """Stable, path-free failure understood by the shared v7 router."""

    def __init__(
        self,
        code: str,
        *,
        capability_states: tuple[CapabilityState, ...] = (),
    ) -> None:
        if code not in STABLE_ERROR_CODE_SET:
            raise ValueError("discovery operation error code is not stable")
        self.code = code
        self.capability_states = capability_states
        super().__init__(code)


class _DiscoveryMutationCancelledError(RuntimeError):
    """Internal signal proving a discovery mutation stopped before publish."""


class _RecallService(Protocol):
    def retrieve(
        self,
        workspace: Workspace,
        query: RetrievalQuery,
        linked_workspace_ids: frozenset[str],
    ) -> object: ...


def _python_symbol(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _python_symbol(node.value)
        return None if prefix is None else f"{prefix}.{node.attr}"
    return None


def _python_relationships(
    source: bytes, module_name: str = ""
) -> tuple[list[dict[str, object]], ...]:
    try:
        tree = ast.parse(source.decode("utf-8", errors="strict"))
    except (SyntaxError, UnicodeDecodeError, ValueError):
        raise RuntimeError("python relationship parse failed") from None
    calls: list[dict[str, object]] = []
    imports: list[dict[str, object]] = []
    references: list[dict[str, object]] = []
    # Preserve explicit bindings and conservatively suppress names assigned or
    # shadowed anywhere in this source. A missing static edge is preferable to
    # authenticating a guessed receiver as a dependency. This deliberately does
    # not infer instance types, dynamic imports or runtime monkey-patching.
    bindings: dict[str, set[str]] = {}
    shadowed: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".", 1)[0]
                target = alias.name if alias.asname else local
                bindings.setdefault(local, set()).add(target)
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level + (node.module or "")
            for alias in node.names:
                if alias.name != "*":
                    target = (
                        f"{prefix}.{alias.name}"
                        if node.module
                        else f"{prefix}{alias.name}"
                    )
                    bindings.setdefault(alias.asname or alias.name, set()).add(target)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            shadowed.add(node.id)
        elif isinstance(node, ast.arg):
            shadowed.add(node.arg)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            shadowed.update(node.names)
    local_definitions = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    for definition_name in local_definitions:
        if definition_name in bindings and module_name:
            bindings[definition_name].add(f"{module_name}.{definition_name}")

    def resolved_symbol(node: ast.AST) -> str | None:
        name = _python_symbol(node)
        if not name:
            return None
        head, separator, tail = name.partition(".")
        if head in shadowed:
            return None
        targets = bindings.get(head)
        if targets is not None:
            if len(targets) != 1:
                return None
            target = next(iter(targets))
            # Relative imports retain their level and cannot be accidentally
            # resolved as a similarly named absolute module.
            return target + (separator + tail if separator else "")
        if head in local_definitions and module_name:
            return f"{module_name}.{name}"
        return None

    for node in ast.walk(tree):
        line = getattr(node, "lineno", None)
        if isinstance(line, bool) or not isinstance(line, int) or line < 1:
            continue
        if isinstance(node, ast.Call):
            name = resolved_symbol(node.func)
            if name:
                calls.append({"name": name, "line": line})
        elif isinstance(node, ast.Import):
            imports.extend({"name": alias.name, "line": line} for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            imports.extend(
                {
                    "name": f"{module}.{alias.name}"
                    if node.module
                    else f"{module}{alias.name}",
                    "line": line,
                }
                for alias in node.names
                if alias.name != "*"
            )
        elif isinstance(node, (ast.Name, ast.Attribute)) and isinstance(
            getattr(node, "ctx", None), ast.Load
        ):
            name = resolved_symbol(node)
            if name:
                references.append({"name": name, "line": line})
        if len(calls) + len(imports) + len(references) > _MAX_RELATIONSHIPS_PER_FILE:
            raise RuntimeError("code relationship extraction exceeded its bound")
    return calls, imports, references


def _relationship_slice(
    values: list[dict[str, object]], start_line: object, end_line: object
) -> list[dict[str, object]]:
    if (
        isinstance(start_line, bool)
        or not isinstance(start_line, int)
        or isinstance(end_line, bool)
        or not isinstance(end_line, int)
    ):
        raise TypeError("tree-sitter entity lines are malformed")
    result: list[dict[str, object]] = []
    for value in values:
        line = value.get("line")
        if isinstance(line, int) and start_line <= line <= end_line:
            result.append(value)
    return result


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _default_worker_pool() -> BoundedWorkerPool:
    return BoundedWorkerPool(
        max_workers=4,
        thread_name_prefix="daem0nmcp-v7-discovery",
    )


class _StrictTreeSitterProducer:
    """Parse caller-supplied bounded bytes without the legacy silent failures."""

    def __init__(self, delegate: object, languages: Mapping[str, str]) -> None:
        self._delegate = delegate
        self._languages = MappingProxyType(dict(languages))

    @property
    def available(self) -> bool:
        return bool(getattr(self._delegate, "available", False))

    def get_supported_extensions(self) -> list[str]:
        return list(self._languages)

    def index_source_strict(
        self,
        file_path: Path,
        project_path: Path,
        source: bytes,
    ) -> object:
        if not isinstance(source, bytes):
            raise TypeError("source must be bytes")
        language_name = self._languages.get(file_path.suffix.lower())
        if language_name is None:
            raise ValueError("unsupported source extension")
        get_tree = getattr(self._delegate, "_get_cached_tree", None)
        get_parser = getattr(self._delegate, "get_parser", None)
        extract = getattr(self._delegate, "_extract_entities", None)
        extract_imports = getattr(self._delegate, "_extract_imports", None)
        if not callable(get_tree) or not callable(get_parser) or not callable(extract):
            raise TypeError("tree-sitter producer is incomplete")
        tree = get_tree(file_path, source, language_name)
        if tree is None:
            raise RuntimeError("tree-sitter parse failed")
        _parser, language = get_parser(language_name)
        root_node = getattr(tree, "root_node", None)
        if (
            language is None
            or root_node is None
            or bool(getattr(root_node, "has_error", False))
        ):
            raise RuntimeError("tree-sitter parse was incomplete")
        relative = file_path.relative_to(project_path).as_posix()
        if language_name == "python":
            module_name = (
                file_path.parent.name
                if file_path.stem == "__init__"
                else file_path.stem
            )
            calls, imports, references = _python_relationships(source, module_name)
        else:
            calls = []
            references = []
            imports = []
            if callable(extract_imports):
                raw_imports = extract_imports(tree, language, language_name, source)
                if not isinstance(raw_imports, list):
                    raise TypeError("tree-sitter imports are malformed")
                imports = [
                    {"name": value, "line": None}
                    for value in raw_imports
                    if isinstance(value, str) and value
                ]

        def entities() -> object:
            for entity in extract(
                tree,
                language,
                language_name,
                source,
                relative,
            ):
                if not isinstance(entity, Mapping):
                    raise TypeError("tree-sitter returned a malformed entity")
                value = dict(entity)
                start_line = value.get("line_start")
                end_line = value.get("line_end")
                value["calls"] = _relationship_slice(calls, start_line, end_line)
                value["references"] = _relationship_slice(
                    references, start_line, end_line
                )
                # A file-level import does not make every function in that
                # file depend on the imported symbol. Attribute import edges
                # only to entities that use the binding or contain the import.
                used_names = {
                    str(item["name"])
                    for item in (*value["calls"], *value["references"])
                }
                inline_imports = _relationship_slice(imports, start_line, end_line)
                value["imports"] = [
                    item
                    for item in imports
                    if item in inline_imports
                    or any(
                        name == item["name"] or name.startswith(str(item["name"]) + ".")
                        for name in used_names
                    )
                    or language_name != "python"
                ]
                yield value

        return entities()


def _default_code_indexer_factory() -> object:
    from ...code_indexer import LANGUAGE_CONFIG, TreeSitterIndexer

    return _StrictTreeSitterProducer(TreeSitterIndexer(), LANGUAGE_CONFIG)


@dataclass(frozen=True, slots=True)
class DiscoveryOperationDependencies:
    """Owned dependencies for canonical discovery reads."""

    storage_resolver: ActiveStorageResolver = field(
        default_factory=WorkspaceStorageResolver
    )
    clock: Callable[[], datetime] = field(default=_default_clock)
    cursor_secret: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    worker_pool: WorkerPool = field(default_factory=_default_worker_pool)
    recall_service: _RecallService | None = None
    code_indexer_factory: Callable[[], object] = _default_code_indexer_factory
    capability_statuses: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not callable(getattr(self.storage_resolver, "locked_active", None)):
            raise TypeError("storage_resolver must provide locked_active")
        if not callable(self.clock):
            raise TypeError("clock must be callable")
        if not isinstance(self.cursor_secret, bytes) or len(self.cursor_secret) < 32:
            raise ValueError("cursor_secret must contain at least 32 bytes")
        if not callable(getattr(self.worker_pool, "run", None)) or not callable(
            getattr(self.worker_pool, "shutdown", None)
        ):
            raise TypeError("worker_pool must provide run and shutdown")
        if self.recall_service is not None and not callable(
            getattr(self.recall_service, "retrieve", None)
        ):
            raise TypeError("recall_service must provide retrieve")
        if not callable(self.code_indexer_factory):
            raise TypeError("code_indexer_factory must be callable")
        statuses = dict(self.capability_statuses)
        if any(
            not isinstance(name, str)
            or status not in {"ready", "disabled", "degraded", "failed"}
            for name, status in statuses.items()
        ):
            raise ValueError("capability_statuses contains an invalid status")
        object.__setattr__(self, "capability_statuses", MappingProxyType(statuses))

    def close(self) -> None:
        """Join and release the dependency-owned bounded worker pool."""

        self.worker_pool.shutdown()


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
        raise DiscoveryOperationError("UNAUTHORIZED_WORKSPACE")
    try:
        canonical = workspace.root.resolve(strict=True)
        registered = WorkspaceRegistry([canonical], default_root=canonical).default
        exact_root = os.path.normcase(str(workspace.root)) == os.path.normcase(
            str(canonical)
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        raise DiscoveryOperationError("UNAUTHORIZED_WORKSPACE") from None
    if registered.workspace_id != workspace.workspace_id or not exact_root:
        raise DiscoveryOperationError("UNAUTHORIZED_WORKSPACE")


def _database_path(workspace: Workspace, active: Any) -> Path:
    try:
        root = workspace.root.resolve(strict=True)
        candidate = Path(active.path)
        if candidate.is_symlink():
            raise ValueError
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
        if not resolved.is_file():
            raise ValueError
        return resolved
    except (OSError, RuntimeError, TypeError, ValueError):
        raise DiscoveryOperationError("WORKSPACE_PATH_ESCAPE") from None


def _verify_database_connection(connection: sqlite3.Connection) -> None:
    try:
        versions = {
            int(row[0])
            for row in connection.execute("SELECT version FROM schema_version")
        }
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    except Exception:
        raise DiscoveryOperationError("CAPABILITY_DEGRADED") from None
    if not versions.issuperset(
        REQUIRED_V7_SCHEMA_VERSIONS
    ) or not _REQUIRED_TABLES.issubset(tables):
        raise DiscoveryOperationError("CAPABILITY_DEGRADED")


def _open_database(path: Path) -> sqlite3.Connection:
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode=ro",
            uri=True,
            timeout=5.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA query_only=ON")
        _verify_database_connection(connection)
        return connection
    except DiscoveryOperationError:
        if connection is not None:
            connection.close()
        raise
    except Exception:
        if connection is not None:
            connection.close()
        raise DiscoveryOperationError("CAPABILITY_DEGRADED") from None


def _open_writable_database(path: Path) -> sqlite3.Connection:
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            path,
            timeout=5.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        _verify_database_connection(connection)
        return connection
    except DiscoveryOperationError:
        if connection is not None:
            connection.close()
        raise
    except Exception:
        if connection is not None:
            connection.close()
        raise DiscoveryOperationError("CAPABILITY_DEGRADED") from None


def _datetime_us(value: object) -> int:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise DiscoveryOperationError("CAPABILITY_DEGRADED")
    try:
        delta = value.astimezone(timezone.utc) - _EPOCH
        result = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    except (OverflowError, ValueError):
        raise DiscoveryOperationError("CAPABILITY_DEGRADED") from None
    if not -(2**63) <= result <= 2**63 - 1:
        raise DiscoveryOperationError("CAPABILITY_DEGRADED")
    return result


def _datetime_from_us(value: object) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DiscoveryOperationError("CAPABILITY_DEGRADED")
    try:
        return _EPOCH + timedelta(microseconds=value)
    except (OverflowError, ValueError):
        raise DiscoveryOperationError("CAPABILITY_DEGRADED") from None


def _manifest(
    connection: sqlite3.Connection,
    workspace_id: str,
    projection_name: str = "graph",
) -> ProjectionManifest:
    rows = connection.execute(
        "SELECT projection_name,generation,source_event_root_hash,"
        "activated_at_us,completed_at_us FROM projection_manifests "
        "WHERE workspace_id=? AND projection_name=? "
        "AND status='active' LIMIT 2",
        (workspace_id, projection_name),
    ).fetchall()
    if len(rows) != 1:
        raise DiscoveryOperationError("CAPABILITY_DEGRADED")
    row = rows[0]
    built_at = row[3] if row[3] is not None else row[4]
    if built_at is None:
        raise DiscoveryOperationError("CAPABILITY_DEGRADED")
    try:
        return ProjectionManifest(
            projection=row[0],
            generation=row[1],
            source_root_hash=row[2],
            built_at=_datetime_from_us(built_at),
        )
    except DiscoveryOperationError:
        raise
    except Exception:
        raise DiscoveryOperationError("CAPABILITY_DEGRADED") from None


def _graph_counts(
    connection: sqlite3.Connection,
    workspace_id: str,
    now_us: int,
) -> tuple[int, int]:
    invalid_relationship = connection.execute(
        "SELECT 1 FROM memory_relationship_versions AS relation "
        "LEFT JOIN memory_records AS source "
        "ON source.workspace_id=relation.workspace_id "
        "AND source.record_id=relation.source_record_id "
        "LEFT JOIN memory_records AS target "
        "ON target.workspace_id=relation.workspace_id "
        "AND target.record_id=relation.target_record_id "
        "WHERE relation.workspace_id=? "
        "AND relation.transaction_to_us IS NULL "
        "AND relation.valid_from_us<=? "
        "AND (relation.valid_to_us IS NULL OR relation.valid_to_us>?) "
        "AND (source.record_id IS NULL OR target.record_id IS NULL) LIMIT 1",
        (workspace_id, now_us, now_us),
    ).fetchone()
    if invalid_relationship is not None:
        raise DiscoveryOperationError("CAPABILITY_DEGRADED")

    invalid_fact = connection.execute(
        "SELECT 1 FROM memory_fact_versions AS fact "
        "LEFT JOIN memory_records AS source "
        "ON source.workspace_id=fact.workspace_id "
        "AND source.record_id=fact.subject_record_id "
        "LEFT JOIN memory_records AS target "
        "ON target.workspace_id=fact.workspace_id "
        "AND target.record_id=json_extract(fact.object_json,'$') "
        "WHERE fact.workspace_id=? AND fact.object_kind='record_ref' "
        "AND fact.transaction_to_us IS NULL "
        "AND fact.valid_from_us<=? "
        "AND (fact.valid_to_us IS NULL OR fact.valid_to_us>?) "
        "AND (json_type(fact.object_json)<>'text' "
        "OR length(json_extract(fact.object_json,'$'))<>68 "
        "OR substr(json_extract(fact.object_json,'$'),1,4)<>'mem_' "
        "OR substr(json_extract(fact.object_json,'$'),5) "
        "GLOB '*[^0-9a-f]*' OR source.record_id IS NULL "
        "OR target.record_id IS NULL) LIMIT 1",
        (workspace_id, now_us, now_us),
    ).fetchone()
    if invalid_fact is not None:
        raise DiscoveryOperationError("CAPABILITY_DEGRADED")

    row = connection.execute(
        "WITH live_edges(source_id,target_id) AS ("
        "SELECT source_record_id,target_record_id "
        "FROM memory_relationship_versions WHERE workspace_id=? "
        "AND transaction_to_us IS NULL AND valid_from_us<=? "
        "AND (valid_to_us IS NULL OR valid_to_us>?) "
        "UNION ALL "
        "SELECT subject_record_id,json_extract(object_json,'$') "
        "FROM memory_fact_versions WHERE workspace_id=? "
        "AND object_kind='record_ref' AND transaction_to_us IS NULL "
        "AND valid_from_us<=? AND (valid_to_us IS NULL OR valid_to_us>?)"
        "), nodes(record_id) AS ("
        "SELECT source_id FROM live_edges UNION SELECT target_id FROM live_edges"
        ") SELECT (SELECT count(*) FROM nodes),"
        "(SELECT count(*) FROM live_edges)",
        (
            workspace_id,
            now_us,
            now_us,
            workspace_id,
            now_us,
            now_us,
        ),
    ).fetchone()
    if (
        row is None
        or any(isinstance(value, bool) or not isinstance(value, int) for value in row)
        or row[0] < 0
        or row[1] < 0
    ):
        raise DiscoveryOperationError("CAPABILITY_DEGRADED")
    return int(row[0]), int(row[1])


def _translate_error(error: Exception) -> DiscoveryOperationError:
    if isinstance(error, DiscoveryOperationError):
        return error
    if isinstance(error, PublicObjectIdNotFound):
        return DiscoveryOperationError("NOT_FOUND")
    if isinstance(error, StaleProjectionId):
        return DiscoveryOperationError("STALE_PROJECTION_ID")
    code = getattr(error, "code", None)
    if isinstance(code, str) and code in STABLE_ERROR_CODE_SET:
        return DiscoveryOperationError(code)
    return DiscoveryOperationError("CAPABILITY_DEGRADED")


def _active_projection(
    connection: sqlite3.Connection,
    workspace_id: str,
    projection_name: str,
) -> ProjectionManifest:
    manifest = _manifest(connection, workspace_id, projection_name)
    if projection_name == "graph":
        from ...retrieval.specialized_projection import (
            SpecializedProjectionBuilder,
        )

        if not SpecializedProjectionBuilder(connection).active_is_current(
            workspace_id, "graph"
        ):
            raise DiscoveryOperationError("CAPABILITY_DEGRADED")
    return manifest


def _partition_metadata(
    connection: sqlite3.Connection,
    workspace_id: str,
    projection_name: str,
    generation: int,
    partition_name: str,
) -> tuple[int, str]:
    rows = connection.execute(
        "SELECT row_count,content_hash FROM discovery_projection_partitions "
        "WHERE workspace_id=? AND projection_name=? AND generation=? "
        "AND partition_name=? LIMIT 2",
        (workspace_id, projection_name, generation, partition_name),
    ).fetchall()
    if len(rows) != 1:
        raise DiscoveryOperationError("CAPABILITY_DEGRADED")
    row_count, content_hash = rows[0]
    if (
        isinstance(row_count, bool)
        or not isinstance(row_count, int)
        or row_count < 0
        or not isinstance(content_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", content_hash) is None
    ):
        raise DiscoveryOperationError("CAPABILITY_DEGRADED")
    if row_count > _MAX_PARTITION_ROWS:
        raise DiscoveryOperationError("TASK_REQUIRED")
    return row_count, content_hash


def _safe_code_path(value: object) -> bool:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 1024
        or "\\" in value
        or "\x00" in value
        or value.startswith(("/", "~"))
        or re.match(r"^[A-Za-z]:", value) is not None
        or value in {".", ".."}
    ):
        return False
    components = value.split("/")
    return (
        all(component not in {"", ".", ".."} for component in components)
        and posixpath.normpath(value) == value
    )


def _validate_entity_partition(
    connection: sqlite3.Connection,
    workspace_id: str,
    generation: int,
) -> None:
    expected_count, expected_hash = _partition_metadata(
        connection, workspace_id, "graph", generation, "entities"
    )
    rows = connection.execute(
        "SELECT entity.entity_id,entity.name,entity.normalized_name,"
        "entity.entity_type,entity.mention_count,entity.identity_hash,"
        "mapping.source_key,mapping.projection_generation "
        "FROM discovery_entities AS entity "
        "LEFT JOIN public_object_ids AS mapping "
        "ON mapping.public_id=entity.entity_id "
        "AND mapping.workspace_id=entity.workspace_id "
        "AND mapping.object_kind='entity' "
        "WHERE entity.workspace_id=? AND entity.graph_generation=? "
        "ORDER BY entity.identity_hash LIMIT ?",
        (workspace_id, generation, _MAX_PARTITION_ROWS + 1),
    ).fetchall()
    if len(rows) > _MAX_PARTITION_ROWS:
        raise DiscoveryOperationError("TASK_REQUIRED")
    memberships = connection.execute(
        "SELECT entity_id,record_id,mention_count FROM discovery_entity_records "
        "WHERE workspace_id=? AND graph_generation=? "
        "ORDER BY entity_id,record_id LIMIT ?",
        (workspace_id, generation, _MAX_PARTITION_MEMBERSHIPS + 1),
    ).fetchall()
    if len(memberships) > _MAX_PARTITION_MEMBERSHIPS:
        raise DiscoveryOperationError("TASK_REQUIRED")
    indexed: dict[str, list[list[object]]] = {str(row[0]): [] for row in rows}
    for membership in memberships:
        entity_id = str(membership[0])
        if (
            entity_id not in indexed
            or _RECORD_ID_RE.fullmatch(str(membership[1])) is None
            or isinstance(membership[2], bool)
            or not isinstance(membership[2], int)
            or membership[2] < 1
        ):
            raise DiscoveryOperationError("CAPABILITY_DEGRADED")
        indexed[entity_id].append([membership[1], membership[2]])
    payload: list[object] = []
    for row in rows:
        entity_id = str(row[0])
        name = row[1]
        normalized_name = row[2]
        entity_type = row[3]
        mention_count = row[4]
        identity_hash = row[5]
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= 256
            or name != unicodedata.normalize("NFC", name)
            or _CONTROL_RE.search(name) is not None
            or normalized_name != unicodedata.normalize("NFC", name.casefold())
            or not isinstance(entity_type, str)
            or _ENTITY_TYPE_RE.fullmatch(entity_type) is None
            or isinstance(mention_count, bool)
            or not isinstance(mention_count, int)
            or mention_count < len(indexed[entity_id])
            or identity_hash != sha256_json(["entity", entity_type, normalized_name])
            or row[6] != f"s:{identity_hash}"
            or row[7] != 0
            or _ENTITY_ID_RE.fullmatch(entity_id) is None
            or entity_id
            != derive_public_object_id(
                workspace_id,
                PublicObjectKind.ENTITY,
                identity_hash,
            )
        ):
            raise DiscoveryOperationError("CAPABILITY_DEGRADED")
        payload.append(
            {
                "entity_id": row[0],
                "entity_type": row[3],
                "identity_hash": row[5],
                "mention_count": row[4],
                "name": row[1],
                "normalized_name": row[2],
                "records": indexed[str(row[0])],
            }
        )
    if len(rows) != expected_count or sha256_json(payload) != expected_hash:
        raise DiscoveryOperationError("CAPABILITY_DEGRADED")


def _validate_community_partition(
    connection: sqlite3.Connection,
    workspace_id: str,
    generation: int,
) -> None:
    expected_count, expected_hash = _partition_metadata(
        connection, workspace_id, "graph", generation, "communities"
    )
    rows = connection.execute(
        "SELECT community.community_id,community.label,community.level,"
        "community.parent_community_id,community.member_count,"
        "community.identity_hash,mapping.source_key,mapping.projection_generation "
        "FROM discovery_communities AS community "
        "LEFT JOIN public_object_ids AS mapping "
        "ON mapping.public_id=community.community_id "
        "AND mapping.workspace_id=community.workspace_id "
        "AND mapping.object_kind='community' "
        "WHERE community.workspace_id=? AND community.graph_generation=? "
        "ORDER BY mapping.source_key LIMIT ?",
        (workspace_id, generation, _MAX_PARTITION_ROWS + 1),
    ).fetchall()
    if len(rows) > _MAX_PARTITION_ROWS:
        raise DiscoveryOperationError("TASK_REQUIRED")
    memberships = connection.execute(
        "SELECT community_id,record_id FROM discovery_community_members "
        "WHERE workspace_id=? AND graph_generation=? "
        "ORDER BY community_id,record_id LIMIT ?",
        (workspace_id, generation, _MAX_PARTITION_MEMBERSHIPS + 1),
    ).fetchall()
    if len(memberships) > _MAX_PARTITION_MEMBERSHIPS:
        raise DiscoveryOperationError("TASK_REQUIRED")
    indexed: dict[str, list[str]] = {str(row[0]): [] for row in rows}
    for membership in memberships:
        community_id = str(membership[0])
        if (
            community_id not in indexed
            or _RECORD_ID_RE.fullmatch(str(membership[1])) is None
        ):
            raise DiscoveryOperationError("CAPABILITY_DEGRADED")
        indexed[community_id].append(str(membership[1]))
    source_by_id: dict[str, str] = {}
    level_by_id: dict[str, int] = {}
    for row in rows:
        community_id = str(row[0])
        source_key = row[6]
        if (
            not isinstance(source_key, str)
            or not source_key.startswith("s:")
            or not source_key[2:]
            or _CONTROL_RE.search(source_key[2:]) is not None
            or row[7] != generation
            or _COMMUNITY_ID_RE.fullmatch(community_id) is None
            or community_id
            != derive_public_object_id(
                workspace_id,
                PublicObjectKind.COMMUNITY,
                source_key[2:],
                generation,
            )
            or isinstance(row[2], bool)
            or not isinstance(row[2], int)
            or not 0 <= row[2] <= 32
        ):
            raise DiscoveryOperationError("CAPABILITY_DEGRADED")
        source_by_id[community_id] = source_key[2:]
        level_by_id[community_id] = row[2]
    payload: list[object] = []
    for row in rows:
        community_id = str(row[0])
        label = row[1]
        level = row[2]
        parent_id = row[3]
        parent_source = None if parent_id is None else source_by_id.get(str(parent_id))
        source_key = source_by_id[community_id]
        if (
            not isinstance(label, str)
            or not 1 <= len(label) <= 256
            or label != unicodedata.normalize("NFC", label)
            or _CONTROL_RE.search(label) is not None
            or (parent_id is not None and parent_source is None)
            or (parent_id is not None and level_by_id[str(parent_id)] <= level)
            or row[4] != len(indexed[str(row[0])])
            or row[5]
            != sha256_json(
                [
                    "community",
                    source_key,
                    label,
                    level,
                    parent_source,
                    indexed[community_id],
                ]
            )
        ):
            raise DiscoveryOperationError("CAPABILITY_DEGRADED")
        payload.append(
            {
                "community_id": row[0],
                "identity_hash": row[5],
                "label": row[1],
                "level": row[2],
                "members": indexed[str(row[0])],
                "parent_community_id": row[3],
            }
        )
    if len(rows) != expected_count or sha256_json(payload) != expected_hash:
        raise DiscoveryOperationError("CAPABILITY_DEGRADED")


def _validate_code_partition(
    connection: sqlite3.Connection,
    workspace_id: str,
    manifest: ProjectionManifest,
) -> None:
    try:
        verify_code_projection(
            connection,
            workspace_id,
            manifest.generation,
            expected_root_hash=manifest.source_root_hash,
            max_entities=_MAX_PARTITION_ROWS,
            max_edges=_MAX_INDEX_EDGES,
        )
    except CodeProjectionIntegrityError as exc:
        code = (
            "TASK_REQUIRED"
            if exc.code == "CODE_PROJECTION_TOO_LARGE"
            else "CAPABILITY_DEGRADED"
        )
        raise DiscoveryOperationError(code) from None


def _cursor_binding(
    workspace_id: str,
    tool_name: str,
    generation: int,
    selector: object,
) -> str:
    return sha256_json(
        [
            "daem0nmcp",
            "v7",
            "discovery-cursor-binding",
            workspace_id,
            tool_name,
            generation,
            selector,
        ]
    )


def _encode_cursor(secret: bytes, binding: str, after_id: str) -> str:
    payload = canonical_json_bytes(
        {"after": after_id, "binding": binding, "version": 1}
    )
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    signature = hmac.new(secret, payload, hashlib.sha256).hexdigest()
    return f"cur_v1.{encoded}.{signature}"


def _decode_cursor(
    secret: bytes,
    cursor: object,
    binding: str,
    pattern: re.Pattern[str],
) -> str:
    if not isinstance(cursor, str):
        raise DiscoveryOperationError("INVALID_ARGUMENT")
    match = _CURSOR_RE.fullmatch(cursor)
    if match is None:
        raise DiscoveryOperationError("INVALID_ARGUMENT")
    try:
        encoded = match.group(1)
        payload = base64.b64decode(
            encoded + "=" * (-len(encoded) % 4),
            altchars=b"-_",
            validate=True,
        )
        expected = hmac.new(secret, payload, hashlib.sha256).hexdigest()
        value = parse_canonical_json(payload.decode("utf-8"))
    except Exception:
        raise DiscoveryOperationError("INVALID_ARGUMENT") from None
    if (
        not hmac.compare_digest(match.group(2), expected)
        or not isinstance(value, dict)
        or set(value) != {"after", "binding", "version"}
        or value.get("version") != 1
        or value.get("binding") != binding
        or not isinstance(value.get("after"), str)
        or pattern.fullmatch(value["after"]) is None
    ):
        raise DiscoveryOperationError("INVALID_ARGUMENT")
    return value["after"]


def _record_summary(row: sqlite3.Row) -> RecordSummary:
    try:
        content = row["content"]
        tags = json.loads(str(row["tags_json"]))
        if (
            not isinstance(content, str)
            or not content
            or not isinstance(tags, list)
            or row["file_path"] is not None
            or row["deleted_at_us"] is not None
        ):
            raise ValueError
        created = _datetime_from_us(row["created_at_us"])
        updated = _datetime_from_us(row["updated_at_us"])
        if updated < created:
            raise ValueError
        return RecordSummary(
            record_id=row["record_id"],
            record_type=row["record_type"],
            excerpt=content[:4000],
            tags=tags,
            relative_file_path=row["file_path_relative"],
            current_status="archived" if bool(row["archived"]) else "current",
            content_hash=row["content_hash"],
            created_at=created,
            updated_at=updated,
        )
    except Exception:
        raise DiscoveryOperationError("CAPABILITY_DEGRADED") from None


def _read_snapshot(
    dependencies: DiscoveryOperationDependencies,
    workspace: Workspace,
    reader: Callable[[sqlite3.Connection], Any],
) -> Any:
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            connection = _open_database(_database_path(workspace, active))
            try:
                connection.execute("BEGIN")
                result = reader(connection)
                connection.rollback()
                return result
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise
            finally:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
    except DiscoveryOperationError:
        raise
    except Exception as error:
        raise _translate_error(error) from None


def _entity_summary(row: sqlite3.Row, generation: int) -> EntitySummary:
    try:
        return EntitySummary(
            entity_id=row["entity_id"],
            name=row["name"],
            entity_type=row["entity_type"],
            mention_count=row["mention_count"],
            manifest_generation=generation,
        )
    except Exception:
        raise DiscoveryOperationError("CAPABILITY_DEGRADED") from None


def _community_summary(row: sqlite3.Row, generation: int) -> CommunitySummary:
    try:
        return CommunitySummary(
            community_id=row["community_id"],
            label=row["label"],
            level=row["level"],
            member_count=row["member_count"],
            parent_community_id=row["parent_community_id"],
            manifest_generation=generation,
        )
    except Exception:
        raise DiscoveryOperationError("CAPABILITY_DEGRADED") from None


def _code_summary(row: sqlite3.Row, generation: int) -> CodeEntitySummary:
    try:
        return CodeEntitySummary(
            code_entity_id=row["code_entity_id"],
            kind=row["kind"],
            qualified_name=row["qualified_name"],
            relative_file_path=row["relative_file_path"],
            start_line=row["start_line"],
            end_line=row["end_line"],
            manifest_generation=generation,
        )
    except Exception:
        raise DiscoveryOperationError("CAPABILITY_DEGRADED") from None


def _index_kind(value: object) -> str:
    if not isinstance(value, str):
        raise DiscoveryOperationError("CAPABILITY_DEGRADED")
    if value in _CODE_KINDS:
        return value
    if value in {"enum", "impl", "interface", "struct", "trait"}:
        return "class"
    return "symbol"


def _index_root(workspace: Workspace, relative_root: str) -> Path:
    try:
        root = resolve_index_target(workspace, relative_root)
        canonical = root.resolve(strict=True)
        canonical.relative_to(workspace.root)
        if not canonical.is_dir() or root.is_symlink():
            raise IndexPathError("index root is invalid")
        return canonical
    except (IndexPathError, OSError, RuntimeError, ValueError):
        raise DiscoveryOperationError("WORKSPACE_PATH_ESCAPE") from None


def _index_files(
    workspace: Workspace,
    index_root: Path,
    patterns: list[str],
    supported_extensions: frozenset[str],
    cancelled: threading.Event,
) -> tuple[list[Path], int, int]:
    unique: dict[str, Path] = {}
    seen: set[str] = set()
    skipped = 0
    files_seen = 0
    visited = 0
    total_bytes = 0
    try:
        for pattern in sorted(set(validate_index_patterns(patterns))):
            if cancelled.is_set():
                raise _DiscoveryMutationCancelledError
            for candidate in index_root.glob(pattern):
                if cancelled.is_set():
                    raise _DiscoveryMutationCancelledError
                visited += 1
                if visited > _MAX_INDEX_FILES * 4:
                    raise DiscoveryOperationError("TASK_REQUIRED")
                try:
                    relative_candidate = candidate.relative_to(workspace.root)
                except ValueError:
                    raise DiscoveryOperationError("WORKSPACE_PATH_ESCAPE") from None
                if any(part in _INDEX_SKIP_PARTS for part in relative_candidate.parts):
                    if candidate.is_file():
                        skipped += 1
                    continue
                if not candidate.is_file():
                    continue
                resolved = resolve_index_file(workspace.root, candidate)
                resolved.relative_to(index_root)
                key = os.path.normcase(str(resolved))
                if key in seen:
                    continue
                seen.add(key)
                files_seen += 1
                if resolved.suffix.lower() not in supported_extensions:
                    skipped += 1
                    continue
                size = resolved.stat().st_size
                if size > _MAX_INDEX_FILE_BYTES:
                    skipped += 1
                    continue
                total_bytes += size
                if total_bytes > _MAX_INDEX_TOTAL_BYTES:
                    raise DiscoveryOperationError("TASK_REQUIRED")
                unique[key] = resolved
                if len(unique) > _MAX_INDEX_FILES:
                    raise DiscoveryOperationError("TASK_REQUIRED")
    except _DiscoveryMutationCancelledError:
        raise
    except DiscoveryOperationError:
        raise
    except (IndexPathError, OSError, RuntimeError, ValueError):
        raise DiscoveryOperationError("WORKSPACE_PATH_ESCAPE") from None
    return (
        sorted(unique.values(), key=lambda item: item.as_posix()),
        skipped,
        files_seen,
    )


def _supported_index_extensions(indexer: object) -> frozenset[str]:
    getter = getattr(indexer, "get_supported_extensions", None)
    if not callable(getter):
        raise DiscoveryOperationError("CAPABILITY_DEGRADED")
    try:
        raw = getter()
        if not isinstance(raw, (list, tuple, set, frozenset)):
            raise TypeError
        if not raw or len(raw) > 128:
            raise ValueError
        extensions = frozenset(raw)
        if len(extensions) != len(raw) or any(
            not isinstance(extension, str)
            or _CODE_EXTENSION_RE.fullmatch(extension) is None
            or extension != extension.lower()
            for extension in extensions
        ):
            raise ValueError
        return extensions
    except DiscoveryOperationError:
        raise
    except Exception:
        raise DiscoveryOperationError("CAPABILITY_DEGRADED") from None


def _read_index_source(file_path: Path) -> bytes:
    try:
        with file_path.open("rb") as source_file:
            source = source_file.read(_MAX_INDEX_FILE_BYTES + 1)
        if len(source) > _MAX_INDEX_FILE_BYTES:
            raise DiscoveryOperationError("CAPABILITY_DEGRADED")
        return source
    except DiscoveryOperationError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise DiscoveryOperationError("CAPABILITY_DEGRADED") from None


def _raw_relationships(
    raw: Mapping[str, object], kind: str
) -> tuple[tuple[str, int | None], ...]:
    values = raw.get(f"{kind}s", [])
    if (
        not isinstance(values, (list, tuple))
        or len(values) > _MAX_RELATIONSHIPS_PER_FILE
    ):
        raise DiscoveryOperationError("CAPABILITY_DEGRADED")
    result: list[tuple[str, int | None]] = []
    for value in values:
        line: object = None
        if isinstance(value, str):
            relationship_name: object = value
        elif isinstance(value, Mapping):
            relationship_name = value.get("name")
            line = value.get("line")
        else:
            raise DiscoveryOperationError("CAPABILITY_DEGRADED")
        if (
            not isinstance(relationship_name, str)
            or not 1 <= len(relationship_name) <= 256
            or relationship_name != unicodedata.normalize("NFC", relationship_name)
            or _CONTROL_RE.search(relationship_name) is not None
            or (
                line is not None
                and (
                    isinstance(line, bool)
                    or not isinstance(line, int)
                    or line < 1
                    or line > 2**31 - 1
                )
            )
        ):
            raise DiscoveryOperationError("CAPABILITY_DEGRADED")
        result.append((relationship_name, line if isinstance(line, int) else None))
    return tuple(result)


def _resolve_code_edges(
    seeds: Mapping[str, CodeEntityProjectionSeed],
    relationships: Mapping[str, list[tuple[str, str, int | None]]],
) -> tuple[CodeEdgeProjectionSeed, ...]:
    exact: dict[str, list[str]] = {}
    leaf: dict[str, list[str]] = {}
    modules: dict[str, list[str]] = {}
    for source_key, seed in seeds.items():
        normalized = unicodedata.normalize("NFC", seed.qualified_name)
        exact.setdefault(normalized, []).append(source_key)
        path = Path(seed.relative_file_path)
        if path.suffix == ".py":
            module_path = path.with_suffix("")
            if path.stem == "__init__":
                module_path = path.parent
            basename = module_path.name
            if normalized.startswith(basename + "."):
                absolute_name = (
                    ".".join(module_path.parts) + normalized[len(basename) :]
                )
                if absolute_name != normalized:
                    exact.setdefault(absolute_name, []).append(source_key)
        leaf.setdefault(normalized.rsplit(".", 1)[-1], []).append(source_key)
        if seed.kind in {"file", "module"}:
            modules.setdefault(normalized, []).append(source_key)
    resolved: dict[tuple[str, str, str], int | None] = {}
    for source_key in sorted(relationships):
        for kind, raw_name, source_line in relationships[source_key]:
            normalized = unicodedata.normalize("NFC", raw_name)
            if normalized.startswith("."):
                level = len(normalized) - len(normalized.lstrip("."))
                package = Path(seeds[source_key].relative_file_path).parent.parts
                if level > len(package):
                    continue
                normalized = ".".join(
                    (*package[: len(package) - level + 1], normalized[level:])
                )
            candidates = exact.get(normalized, [])
            if kind != "import":
                local_candidates = [
                    key
                    for key in candidates
                    if seeds[key].relative_file_path
                    == seeds[source_key].relative_file_path
                ]
                if local_candidates:
                    candidates = local_candidates
            if not candidates and kind != "import" and "." not in normalized:
                # Unqualified references can only use their source file's
                # declarations. Cross-file resolution requires an exact name
                # supplied by an explicit import binding.
                candidates = [
                    key
                    for key in leaf.get(normalized, [])
                    if seeds[key].relative_file_path
                    == seeds[source_key].relative_file_path
                ]
            if len(candidates) != 1 and kind == "import":
                candidates = modules.get(normalized, [])
            if len(candidates) != 1:
                continue
            target_key = candidates[0]
            if target_key == source_key:
                continue
            edge_key = (source_key, target_key, kind)
            prior = resolved.get(edge_key)
            if prior is None or (source_line is not None and source_line < prior):
                resolved[edge_key] = source_line
            if len(resolved) > _MAX_INDEX_EDGES:
                raise DiscoveryOperationError("TASK_REQUIRED")
    return tuple(
        CodeEdgeProjectionSeed(source, target, kind, line)
        for (source, target, kind), line in sorted(resolved.items())
    )


def _code_capability_error(
    dependencies: DiscoveryOperationDependencies,
) -> DiscoveryOperationError | None:
    status = dependencies.capability_statuses.get("apps")
    if status is None or status == "ready":
        return None
    capability_status: Literal["disabled", "degraded", "failed"]
    if status == "disabled":
        capability_status = "disabled"
    elif status == "degraded":
        capability_status = "degraded"
    else:
        capability_status = "failed"
    code = (
        "CAPABILITY_DISABLED"
        if capability_status == "disabled"
        else "CAPABILITY_DEGRADED"
    )
    reason = (
        "CAPABILITY_DISABLED"
        if capability_status == "disabled"
        else "CAPABILITY_CONFIGURATION_INVALID"
        if capability_status == "failed"
        else "CAPABILITY_DEGRADED"
    )
    remediation = (
        "Set DAEM0NMCP_APPS_ENABLED=true and install the apps dependency profile."
    )
    return DiscoveryOperationError(
        code,
        capability_states=(
            CapabilityState(
                name="apps",
                status=capability_status,
                reason_code=reason,
                remediation=remediation,
            ),
        ),
    )


def _code_index_sync(
    dependencies: DiscoveryOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
    cancelled: threading.Event,
) -> CodeIndexData:
    capability_error = _code_capability_error(dependencies)
    if capability_error is not None:
        raise capability_error
    try:
        indexer = dependencies.code_indexer_factory()
    except Exception:
        raise DiscoveryOperationError("CAPABILITY_DEGRADED") from None
    if not bool(getattr(indexer, "available", False)):
        raise DiscoveryOperationError("CAPABILITY_DISABLED")
    supported_extensions = _supported_index_extensions(indexer)
    index_source = getattr(indexer, "index_source_strict", None)
    if not callable(index_source):
        raise DiscoveryOperationError("CAPABILITY_DEGRADED")
    index_root = _index_root(workspace, request.relative_root)
    files, skipped, files_seen = _index_files(
        workspace,
        index_root,
        list(request.patterns),
        supported_extensions,
        cancelled,
    )
    seeds: dict[str, CodeEntityProjectionSeed] = {}
    relationships: dict[str, list[tuple[str, str, int | None]]] = {}
    relationship_count = 0
    source_digests: dict[str, str] = {}
    indexed_files = 0
    total_source_bytes = 0
    for file_path in files:
        if cancelled.is_set():
            raise _DiscoveryMutationCancelledError
        try:
            resolved = resolve_index_file(workspace.root, file_path)
            resolved.relative_to(index_root)
            relative = resolved.relative_to(workspace.root).as_posix()
            source = _read_index_source(resolved)
            source_digests[relative] = hashlib.sha256(source).hexdigest()
            total_source_bytes += len(source)
            if total_source_bytes > _MAX_INDEX_TOTAL_BYTES:
                raise DiscoveryOperationError("TASK_REQUIRED")
            raw_entities = index_source(resolved, workspace.root, source)
            for raw in raw_entities:
                if cancelled.is_set():
                    raise _DiscoveryMutationCancelledError
                if not isinstance(raw, Mapping):
                    raise DiscoveryOperationError("CAPABILITY_DEGRADED")
                kind = _index_kind(raw.get("entity_type"))
                qualified = raw.get("qualified_name") or raw.get("name")
                start = raw.get("line_start")
                end = raw.get("line_end")
                if (
                    not isinstance(qualified, str)
                    or isinstance(start, bool)
                    or not isinstance(start, int)
                    or isinstance(end, bool)
                    or not isinstance(end, int)
                ):
                    raise DiscoveryOperationError("CAPABILITY_DEGRADED")
                source_key = sha256_json(
                    ["code-source", kind, qualified, relative, start, end]
                )
                seed = CodeEntityProjectionSeed(
                    source_key=source_key,
                    kind=kind,
                    qualified_name=qualified,
                    relative_file_path=relative,
                    start_line=start,
                    end_line=end,
                )
                prior = seeds.get(source_key)
                if prior is not None and prior != seed:
                    raise DiscoveryOperationError("CAPABILITY_DEGRADED")
                seeds[source_key] = seed
                relation_values = relationships.setdefault(source_key, [])
                for kind in ("call", "import", "reference"):
                    values = tuple(
                        (kind, name, line)
                        for name, line in _raw_relationships(raw, kind)
                    )
                    relationship_count += len(values)
                    if relationship_count > _MAX_INDEX_EDGES:
                        raise DiscoveryOperationError("TASK_REQUIRED")
                    relation_values.extend(values)
                    if len(relation_values) > _MAX_RELATIONSHIPS_PER_FILE:
                        raise DiscoveryOperationError("TASK_REQUIRED")
                if len(seeds) > _MAX_INDEX_ENTITIES:
                    raise DiscoveryOperationError("TASK_REQUIRED")
            indexed_files += 1
        except _DiscoveryMutationCancelledError:
            raise
        except DiscoveryOperationError:
            raise
        except (IndexPathError, OSError, RuntimeError, TypeError, ValueError):
            raise DiscoveryOperationError("CAPABILITY_DEGRADED") from None

    edge_seeds = _resolve_code_edges(seeds, relationships)
    try:
        if cancelled.is_set():
            raise _DiscoveryMutationCancelledError
        with dependencies.storage_resolver.locked_active(workspace) as active:
            connection = _open_writable_database(_database_path(workspace, active))
            try:
                if cancelled.is_set():
                    raise _DiscoveryMutationCancelledError

                def before_commit() -> None:
                    if cancelled.is_set():
                        raise _DiscoveryMutationCancelledError
                    current_files, current_skipped, current_seen = _index_files(
                        workspace,
                        index_root,
                        list(request.patterns),
                        supported_extensions,
                        cancelled,
                    )
                    if (
                        current_files != files
                        or current_skipped != skipped
                        or current_seen != files_seen
                    ):
                        raise DiscoveryOperationError("EVENT_STREAM_CONFLICT")
                    for source_path in current_files:
                        current = resolve_index_file(workspace.root, source_path)
                        relative_path = current.relative_to(workspace.root).as_posix()
                        digest = hashlib.sha256(_read_index_source(current)).hexdigest()
                        if source_digests.get(relative_path) != digest:
                            raise DiscoveryOperationError("EVENT_STREAM_CONFLICT")

                result = DiscoveryProjectionBuilder(connection).rebuild_code(
                    workspace.workspace_id,
                    entities=tuple(seeds[key] for key in sorted(seeds)),
                    edges=edge_seeds,
                    force=request.force,
                    before_commit=before_commit,
                )
                manifest = _active_projection(
                    connection,
                    workspace.workspace_id,
                    "code",
                )
                _validate_code_partition(
                    connection,
                    workspace.workspace_id,
                    manifest,
                )
            finally:
                connection.close()
    except _DiscoveryMutationCancelledError:
        raise
    except DiscoveryOperationError:
        raise
    except Exception as error:
        raise _translate_error(error) from None
    return CodeIndexData(
        manifest=manifest,
        files_seen=files_seen,
        files_indexed=indexed_files,
        skipped=skipped,
        diagnostics=(
            [
                DiagnosticSummary(
                    code="CODE_INDEX_CURRENT",
                    message="The canonical code projection already matches the index.",
                )
            ]
            if result.reused
            else []
        ),
    )


def _code_search_sync(
    dependencies: DiscoveryOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
) -> Page[CodeEntitySummary]:
    def reader(connection: sqlite3.Connection) -> Page[CodeEntitySummary]:
        manifest = _active_projection(connection, workspace.workspace_id, "code")
        _validate_code_partition(connection, workspace.workspace_id, manifest)
        query = unicodedata.normalize("NFC", request.query.casefold())
        kinds = None if request.entity_kinds is None else sorted(request.entity_kinds)
        selector = {"entity_kinds": kinds, "query": query}
        binding = _cursor_binding(
            workspace.workspace_id,
            "code_search",
            manifest.generation,
            selector,
        )
        after_id = None
        if request.cursor is not None:
            after_id = _decode_cursor(
                dependencies.cursor_secret,
                request.cursor,
                binding,
                _CODE_ID_RE,
            )
        where = (
            "workspace_id=? AND code_generation=? "
            "AND (instr(normalized_name,?)>0 "
            "OR instr(relative_file_path,?)>0)"
        )
        parameters: list[object] = [
            workspace.workspace_id,
            manifest.generation,
            query,
            query,
        ]
        if kinds is not None:
            placeholders = ",".join("?" for _ in kinds)
            where += f" AND kind IN ({placeholders})"
            parameters.extend(kinds)
        if after_id is not None:
            where += " AND code_entity_id>?"
            parameters.append(after_id)
        rows = connection.execute(
            "SELECT code_entity_id,kind,qualified_name,relative_file_path,"
            "start_line,end_line FROM discovery_code_entities WHERE "
            + where
            + " ORDER BY code_entity_id LIMIT ?",
            (*parameters, request.limit + 1),
        ).fetchall()
        truncated = len(rows) > request.limit
        selected = rows[: request.limit]
        return Page[CodeEntitySummary](
            items=[_code_summary(row, manifest.generation) for row in selected],
            next_cursor=(
                _encode_cursor(
                    dependencies.cursor_secret,
                    binding,
                    str(selected[-1]["code_entity_id"]),
                )
                if truncated and selected
                else None
            ),
            truncated=truncated,
        )

    return _read_snapshot(dependencies, workspace, reader)


def _entity_list_sync(
    dependencies: DiscoveryOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
) -> Page[EntitySummary]:
    def reader(connection: sqlite3.Connection) -> Page[EntitySummary]:
        manifest = _active_projection(connection, workspace.workspace_id, "graph")
        _validate_entity_partition(
            connection, workspace.workspace_id, manifest.generation
        )
        selector = {"entity_type": request.entity_type}
        binding = _cursor_binding(
            workspace.workspace_id,
            "entity_list",
            manifest.generation,
            selector,
        )
        after_id = None
        if request.cursor is not None:
            after_id = _decode_cursor(
                dependencies.cursor_secret,
                request.cursor,
                binding,
                _ENTITY_ID_RE,
            )
        where = "workspace_id=? AND graph_generation=?"
        parameters: list[object] = [
            workspace.workspace_id,
            manifest.generation,
        ]
        if request.entity_type is not None:
            where += " AND entity_type=?"
            parameters.append(request.entity_type)
        if after_id is not None:
            where += " AND entity_id>?"
            parameters.append(after_id)
        rows = connection.execute(
            "SELECT entity_id,name,entity_type,mention_count "
            "FROM discovery_entities WHERE " + where + " ORDER BY entity_id LIMIT ?",
            (*parameters, request.limit + 1),
        ).fetchall()
        truncated = len(rows) > request.limit
        selected = rows[: request.limit]
        return Page[EntitySummary](
            items=[_entity_summary(row, manifest.generation) for row in selected],
            next_cursor=(
                _encode_cursor(
                    dependencies.cursor_secret,
                    binding,
                    str(selected[-1]["entity_id"]),
                )
                if truncated and selected
                else None
            ),
            truncated=truncated,
        )

    return _read_snapshot(dependencies, workspace, reader)


def _resolve_community_id(
    connection: sqlite3.Connection,
    workspace_id: str,
    community_id: str,
    generation: int,
) -> str:
    resolved = PublicObjectIdRepository(connection).resolve_public_id(
        workspace_id,
        PublicObjectKind.COMMUNITY,
        community_id,
        generation,
    )
    return resolved.public_id


def _community_list_sync(
    dependencies: DiscoveryOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
) -> Page[CommunitySummary]:
    def reader(connection: sqlite3.Connection) -> Page[CommunitySummary]:
        manifest = _active_projection(connection, workspace.workspace_id, "graph")
        _validate_community_partition(
            connection, workspace.workspace_id, manifest.generation
        )
        parent_id = None
        if request.parent_community_id is not None:
            parent_id = _resolve_community_id(
                connection,
                workspace.workspace_id,
                request.parent_community_id,
                manifest.generation,
            )
        selector = {"level": request.level, "parent_community_id": parent_id}
        binding = _cursor_binding(
            workspace.workspace_id,
            "community_list",
            manifest.generation,
            selector,
        )
        after_id = None
        if request.cursor is not None:
            after_id = _decode_cursor(
                dependencies.cursor_secret,
                request.cursor,
                binding,
                _COMMUNITY_ID_RE,
            )
        where = "workspace_id=? AND graph_generation=?"
        parameters: list[object] = [
            workspace.workspace_id,
            manifest.generation,
        ]
        if request.level is not None:
            where += " AND level=?"
            parameters.append(request.level)
        if parent_id is not None:
            where += " AND parent_community_id=?"
            parameters.append(parent_id)
        if after_id is not None:
            where += " AND community_id>?"
            parameters.append(after_id)
        rows = connection.execute(
            "SELECT community_id,label,level,parent_community_id,member_count "
            "FROM discovery_communities WHERE "
            + where
            + " ORDER BY community_id LIMIT ?",
            (*parameters, request.limit + 1),
        ).fetchall()
        truncated = len(rows) > request.limit
        selected = rows[: request.limit]
        return Page[CommunitySummary](
            items=[_community_summary(row, manifest.generation) for row in selected],
            next_cursor=(
                _encode_cursor(
                    dependencies.cursor_secret,
                    binding,
                    str(selected[-1]["community_id"]),
                )
                if truncated and selected
                else None
            ),
            truncated=truncated,
        )

    try:
        return _read_snapshot(dependencies, workspace, reader)
    except Exception as error:
        raise _translate_error(error) from None


def _community_get_sync(
    dependencies: DiscoveryOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
) -> CommunityDetail:
    def reader(connection: sqlite3.Connection) -> CommunityDetail:
        manifest = _active_projection(connection, workspace.workspace_id, "graph")
        _validate_community_partition(
            connection, workspace.workspace_id, manifest.generation
        )
        community_id = _resolve_community_id(
            connection,
            workspace.workspace_id,
            request.community_id,
            manifest.generation,
        )
        rows = connection.execute(
            "SELECT community_id,label,level,parent_community_id,member_count "
            "FROM discovery_communities WHERE workspace_id=? "
            "AND graph_generation=? AND community_id=? LIMIT 2",
            (workspace.workspace_id, manifest.generation, community_id),
        ).fetchall()
        if not rows:
            raise DiscoveryOperationError("NOT_FOUND")
        if len(rows) != 1:
            raise DiscoveryOperationError("CAPABILITY_DEGRADED")
        community = _community_summary(rows[0], manifest.generation)
        if not request.include_members:
            if request.cursor is not None:
                raise DiscoveryOperationError("INVALID_ARGUMENT")
            return CommunityDetail(
                community=community,
                members=Page[RecordSummary](
                    items=[], next_cursor=None, truncated=False
                ),
            )
        selector = {"community_id": community_id, "include_members": True}
        binding = _cursor_binding(
            workspace.workspace_id,
            "community_get",
            manifest.generation,
            selector,
        )
        after_id = None
        if request.cursor is not None:
            after_id = _decode_cursor(
                dependencies.cursor_secret,
                request.cursor,
                binding,
                _RECORD_ID_RE,
            )
        parameters: list[object] = [
            workspace.workspace_id,
            manifest.generation,
            community_id,
        ]
        after = ""
        if after_id is not None:
            after = " AND member.record_id>?"
            parameters.append(after_id)
        member_rows = connection.execute(
            f"SELECT {_QUALIFIED_RECORD_COLUMNS} "
            "FROM discovery_community_members AS member "
            "JOIN memory_records AS record ON record.workspace_id=member.workspace_id "
            "AND record.record_id=member.record_id WHERE member.workspace_id=? "
            "AND member.graph_generation=? AND member.community_id=? "
            + after
            + " ORDER BY member.record_id LIMIT ?",
            (*parameters, request.limit + 1),
        ).fetchall()
        truncated = len(member_rows) > request.limit
        selected = member_rows[: request.limit]
        return CommunityDetail(
            community=community,
            members=Page[RecordSummary](
                items=[_record_summary(row) for row in selected],
                next_cursor=(
                    _encode_cursor(
                        dependencies.cursor_secret,
                        binding,
                        str(selected[-1]["record_id"]),
                    )
                    if truncated and selected
                    else None
                ),
                truncated=truncated,
            ),
        )

    try:
        return _read_snapshot(dependencies, workspace, reader)
    except Exception as error:
        raise _translate_error(error) from None


@dataclass(frozen=True, slots=True)
class _EntitySelection:
    generation: int
    entity_id: str
    name: str
    record_ids: tuple[str, ...]
    next_cursor: str | None
    truncated: bool


def _entity_selection_sync(
    dependencies: DiscoveryOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
) -> _EntitySelection:
    def reader(connection: sqlite3.Connection) -> _EntitySelection:
        manifest = _active_projection(connection, workspace.workspace_id, "graph")
        _validate_entity_partition(
            connection, workspace.workspace_id, manifest.generation
        )
        if request.entity_id is not None:
            PublicObjectIdRepository(connection).resolve_public_id(
                workspace.workspace_id,
                PublicObjectKind.ENTITY,
                request.entity_id,
            )
            rows = connection.execute(
                "SELECT entity_id,name,entity_type FROM discovery_entities "
                "WHERE workspace_id=? AND graph_generation=? AND entity_id=? "
                "LIMIT 2",
                (
                    workspace.workspace_id,
                    manifest.generation,
                    request.entity_id,
                ),
            ).fetchall()
        else:
            if not isinstance(request.entity_name, str):
                raise DiscoveryOperationError("INVALID_ARGUMENT")
            normalized = unicodedata.normalize("NFC", request.entity_name.casefold())
            where = "workspace_id=? AND graph_generation=? AND normalized_name=?"
            parameters: list[object] = [
                workspace.workspace_id,
                manifest.generation,
                normalized,
            ]
            if request.entity_type is not None:
                where += " AND entity_type=?"
                parameters.append(request.entity_type)
            rows = connection.execute(
                "SELECT entity_id,name,entity_type FROM discovery_entities WHERE "
                + where
                + " ORDER BY entity_id LIMIT 2",
                parameters,
            ).fetchall()
        if not rows:
            raise DiscoveryOperationError("NOT_FOUND")
        if len(rows) != 1:
            raise DiscoveryOperationError("CONFLICT")
        entity_id = str(rows[0]["entity_id"])
        selector = {
            "entity_id": request.entity_id,
            "entity_name": request.entity_name,
            "entity_type": request.entity_type,
        }
        binding = _cursor_binding(
            workspace.workspace_id,
            "memory_recall_entity",
            manifest.generation,
            selector,
        )
        after_id = None
        if request.cursor is not None:
            after_id = _decode_cursor(
                dependencies.cursor_secret,
                request.cursor,
                binding,
                _RECORD_ID_RE,
            )
        parameters = [workspace.workspace_id, manifest.generation, entity_id]
        after = ""
        if after_id is not None:
            after = " AND record_id>?"
            parameters.append(after_id)
        members = connection.execute(
            "SELECT record_id FROM discovery_entity_records WHERE "
            "workspace_id=? AND graph_generation=? AND entity_id=?"
            + after
            + " ORDER BY record_id LIMIT ?",
            (*parameters, request.limit + 1),
        ).fetchall()
        truncated = len(members) > request.limit
        selected = tuple(str(row[0]) for row in members[: request.limit])
        return _EntitySelection(
            generation=manifest.generation,
            entity_id=entity_id,
            name=str(rows[0]["name"]),
            record_ids=selected,
            next_cursor=(
                _encode_cursor(
                    dependencies.cursor_secret,
                    binding,
                    selected[-1],
                )
                if truncated and selected
                else None
            ),
            truncated=truncated,
        )

    try:
        return _read_snapshot(dependencies, workspace, reader)
    except Exception as error:
        raise _translate_error(error) from None


def _generation_is_current_sync(
    dependencies: DiscoveryOperationDependencies,
    workspace: Workspace,
    generation: int,
) -> None:
    def reader(connection: sqlite3.Connection) -> None:
        manifest = _active_projection(connection, workspace.workspace_id, "graph")
        if manifest.generation != generation:
            raise DiscoveryOperationError("CAPABILITY_DEGRADED")

    _read_snapshot(dependencies, workspace, reader)


async def _memory_recall_entity(
    dependencies: DiscoveryOperationDependencies,
    workspace: Workspace,
    request: AdmittedRequest,
) -> Page[RecordSummary]:
    selection = await _run_blocking(
        dependencies,
        lambda: _entity_selection_sync(dependencies, workspace, request),
    )
    if not selection.record_ids:
        return Page[RecordSummary](items=[], next_cursor=None, truncated=False)
    service = dependencies.recall_service
    if service is None:
        raise DiscoveryOperationError("CAPABILITY_DEGRADED")
    try:
        query = RetrievalQuery(
            workspace_id=workspace.workspace_id,
            text=selection.name,
            limit=len(selection.record_ids),
            candidate_limit=max(50, len(selection.record_ids)),
            record_ids=frozenset(selection.record_ids),
            token_budget=max(2_400, len(selection.record_ids) * 512),
        )
        value = service.retrieve(workspace, query, frozenset())
        if inspect.isawaitable(value):
            value = await value
        result = RetrievalData.model_validate(value)
        indexed: dict[str, RecordSummary] = {}
        for item in result.items:
            record_id = item.record.record_id
            if record_id in indexed:
                raise DiscoveryOperationError("CAPABILITY_DEGRADED")
            indexed[record_id] = item.record
        if set(indexed) != set(selection.record_ids):
            # A lexical index that is stale, contended, or mid-swap has not
            # caught up with the entity's records yet; the caller may retry.
            if not any(
                diagnostic.provider == "lexical" and diagnostic.status == "ready"
                for diagnostic in result.provider_diagnostics
            ):
                raise DiscoveryOperationError("DATABASE_IN_USE")
            raise DiscoveryOperationError("CAPABILITY_DEGRADED")
        await _run_blocking(
            dependencies,
            lambda: _generation_is_current_sync(
                dependencies,
                workspace,
                selection.generation,
            ),
        )
        return Page[RecordSummary](
            items=[indexed[record_id] for record_id in selection.record_ids],
            next_cursor=selection.next_cursor,
            truncated=selection.truncated,
        )
    except asyncio.CancelledError:
        raise
    except DiscoveryOperationError:
        raise
    except Exception as error:
        raise _translate_error(error) from None


def _stats_sync(
    dependencies: DiscoveryOperationDependencies,
    workspace: Workspace,
) -> KnowledgeGraphStatsData:
    try:
        with dependencies.storage_resolver.locked_active(workspace) as active:
            connection = _open_database(_database_path(workspace, active))
            try:
                connection.execute("BEGIN")
                from ...retrieval.specialized_projection import (
                    SpecializedProjectionBuilder,
                )

                if not SpecializedProjectionBuilder(connection).active_is_current(
                    workspace.workspace_id, "graph"
                ):
                    raise DiscoveryOperationError("CAPABILITY_DEGRADED")
                manifest = _manifest(connection, workspace.workspace_id)
                try:
                    now = dependencies.clock()
                except Exception:
                    raise DiscoveryOperationError("CAPABILITY_DEGRADED") from None
                node_count, edge_count = _graph_counts(
                    connection,
                    workspace.workspace_id,
                    _datetime_us(now),
                )
                connection.rollback()
                return KnowledgeGraphStatsData(
                    node_count=node_count,
                    edge_count=edge_count,
                    type_counts={"record": node_count},
                    manifest=manifest,
                )
            except DiscoveryOperationError:
                if connection.in_transaction:
                    connection.rollback()
                raise
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise DiscoveryOperationError("CAPABILITY_DEGRADED") from None
            finally:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
    except DiscoveryOperationError:
        raise
    except RuntimeServiceError:
        raise DiscoveryOperationError("CAPABILITY_DEGRADED") from None
    except Exception:
        raise DiscoveryOperationError("CAPABILITY_DEGRADED") from None


async def _run_blocking(
    dependencies: DiscoveryOperationDependencies,
    operation: Callable[[], Any],
) -> Any:
    worker = asyncio.create_task(dependencies.worker_pool.run(operation))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as cancellation:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if worker.done():
            with suppress(Exception):
                worker.result()
        raise cancellation
    except BoundedWorkerBusyError:
        raise DiscoveryOperationError("TASK_REQUIRED") from None


async def _run_mutation(
    dependencies: DiscoveryOperationDependencies,
    operation: Callable[[threading.Event], Any],
) -> Any:
    cancelled = threading.Event()
    worker = asyncio.create_task(
        dependencies.worker_pool.run(lambda: operation(cancelled))
    )
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as cancellation:
        cancelled.set()
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        try:
            result = worker.result()
        except (_DiscoveryMutationCancelledError, BoundedWorkerBusyError):
            raise cancellation from None
        except Exception:
            raise cancellation from None
        return result
    except _DiscoveryMutationCancelledError:
        raise DiscoveryOperationError("CANCELLED") from None
    except BoundedWorkerBusyError:
        raise DiscoveryOperationError("TASK_REQUIRED") from None


def build_discovery_operations(
    dependencies: DiscoveryOperationDependencies,
) -> Mapping[str, Callable[..., Any]]:
    """Return immutable handlers backed only by canonical v7 projections."""

    if not isinstance(dependencies, DiscoveryOperationDependencies):
        raise TypeError("dependencies must be DiscoveryOperationDependencies")

    async def knowledge_graph_stats(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> KnowledgeGraphStatsData:
        _authorize(workspace, request, "knowledge_graph_stats")
        return await _run_blocking(
            dependencies,
            lambda: _stats_sync(dependencies, workspace),
        )

    async def code_search(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> Page[CodeEntitySummary]:
        _authorize(workspace, request, "code_search")
        return await _run_blocking(
            dependencies,
            lambda: _code_search_sync(dependencies, workspace, request),
        )

    async def code_index(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> CodeIndexData:
        _authorize(workspace, request, "code_index")
        return await _run_mutation(
            dependencies,
            lambda cancelled: _code_index_sync(
                dependencies,
                workspace,
                request,
                cancelled,
            ),
        )

    async def community_get(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> CommunityDetail:
        _authorize(workspace, request, "community_get")
        return await _run_blocking(
            dependencies,
            lambda: _community_get_sync(dependencies, workspace, request),
        )

    async def community_list(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> Page[CommunitySummary]:
        _authorize(workspace, request, "community_list")
        return await _run_blocking(
            dependencies,
            lambda: _community_list_sync(dependencies, workspace, request),
        )

    async def entity_list(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> Page[EntitySummary]:
        _authorize(workspace, request, "entity_list")
        return await _run_blocking(
            dependencies,
            lambda: _entity_list_sync(dependencies, workspace, request),
        )

    async def memory_recall_entity(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> Page[RecordSummary]:
        _authorize(workspace, request, "memory_recall_entity")
        return await _memory_recall_entity(dependencies, workspace, request)

    return MappingProxyType(
        {
            "code_index": code_index,
            "code_search": code_search,
            "community_get": community_get,
            "community_list": community_list,
            "entity_list": entity_list,
            "knowledge_graph_stats": knowledge_graph_stats,
            "memory_recall_entity": memory_recall_entity,
        }
    )


__all__ = [
    "DiscoveryOperationDependencies",
    "DiscoveryOperationError",
    "build_discovery_operations",
]
