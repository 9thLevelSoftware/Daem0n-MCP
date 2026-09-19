"""Offline, deterministic architecture-format 7 migration service.

The module is intentionally standard-library only.  In particular, dry-run and
the data migration do not import SQLAlchemy, retrieval, vector, model, Qdrant,
or network packages.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..event_store import (
    CompatibilityStreamError,
    EventCommand,
    EventStore,
    build_live_compatibility_claim_index,
    canonical_json_bytes,
    deterministic_id,
    event_hash_for,
    memory_content_hash,
    memory_state_hash,
    sha256_json,
)
from ..schema_version import CURRENT_SCHEMA_VERSION, REQUIRED_V7_SCHEMA_VERSIONS
from ..storage_activation import (
    ActiveDatabasePointer,
    DatabaseFileLock,
    resolve_active_database,
    write_active_pointer,
)
from ..workspace import WorkspaceRegistry, resolve_derived_path

TARGET_FORMAT_VERSION = 7
DEFAULT_BATCH_SIZE = 500
_KNOWN_MEMORY_TYPES = {
    "decision",
    "pattern",
    "warning",
    "learning",
    "procedure",
    "observation",
}
_KNOWN_RELATIONSHIPS = {
    "led_to",
    "supersedes",
    "depends_on",
    "conflicts_with",
    "related_to",
    "evidence_for",
    "derived_from",
    "invalidates",
}
_KNOWN_CHANGES = {
    "created",
    "content_updated",
    "outcome_recorded",
    "relationship_changed",
    "state_changed",
}
_COMPATIBILITY_SOURCE_KINDS = {
    "memories": "memory",
    "facts": "fact",
    "memory_relationships": "relationship",
}
_V7_TABLE_NAMES = frozenset(
    {
        "memory_events",
        "memory_records",
        "memory_fact_versions",
        "memory_relationship_versions",
        "projection_manifests",
        "enrichment_decisions",
        "background_jobs",
        "v7_migration_runs",
        "v7_migration_checkpoints",
        "legacy_id_map",
        "retrieval_documents",
        "record_procedures",
        "record_outcome_view",
        "dense_projection_refs",
        "public_object_ids",
        "active_context_entries",
        "governance_events",
        "governance_rules",
        "governance_context_triggers",
        "session_update_sequence",
        "discovery_projection_partitions",
        "discovery_entities",
        "discovery_entity_records",
        "discovery_communities",
        "discovery_community_members",
        "discovery_code_entities",
        "discovery_code_edges",
    }
)


class MigrationV7Error(RuntimeError):
    """Stable migration error with a machine-readable code."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(f"{code}: {detail}")


class MigrationInterrupted(RuntimeError):  # noqa: N818 -- retained public Python API name
    """Testable/process-interruption boundary; committed checkpoints remain."""


@dataclass(frozen=True)
class MigrationResult:
    status: str
    action: str
    workspace_id: str
    source_format: int
    target_format: int = TARGET_FORMAT_VERSION
    migration_run_id: str | None = None
    active_generation: int = 0
    inventory: dict[str, Any] = field(default_factory=dict)
    checkpoints: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    error: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _quoted_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _readonly_connection(path: Path) -> sqlite3.Connection:
    # Migration dry-run must not write any byte.  On Windows the no-lock VFS
    # keeps read-only connections off the ``-shm`` read marks.  POSIX has no
    # equivalent: ``unix-none`` lacks shared memory, so it cannot open a WAL
    # database at all.  There, with no ``-wal``/``-shm`` the main file is the
    # whole database and is read as immutable (SQLite would otherwise create
    # both files); a live WAL is read through the default VFS, whose only
    # write is the volatile ``-shm`` wal-index.
    resolved = path.resolve()
    if sys.platform == "win32":
        parameters = "&vfs=win32-none"
    elif not any(Path(f"{resolved}{suffix}").exists() for suffix in ("-wal", "-shm")):
        parameters = "&immutable=1"
    else:
        parameters = ""
    uri = "file:" + quote(resolved.as_posix(), safe="/:") + f"?mode=ro{parameters}"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=0")
    return connection


def _table_names(connection: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        )
    ]


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _count(connection: sqlite3.Connection, table: str) -> int:
    if not _table_exists(connection, table):
        return 0
    return int(
        connection.execute(
            f"SELECT count(*) FROM {_quoted_identifier(table)}"
        ).fetchone()[0]
    )


def _encode_sqlite_value(value: Any, *, table: str, column: str) -> Any:
    if value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return {"$sqlite_real": repr(value)}
    if isinstance(value, bytes):
        digest = hashlib.sha256(value).hexdigest()
        if table == "memories" and column == "vector_embedding":
            return {"$legacy_vector": {"length": len(value), "sha256": digest}}
        return {
            "$blob_base64": base64.b64encode(value).decode("ascii"),
            "length": len(value),
            "sha256": digest,
        }
    raise MigrationV7Error(
        "UNREADABLE_SOURCE_ROW", f"unsupported SQLite value in {table}.{column}"
    )


def _table_logical_rows(
    connection: sqlite3.Connection, table: str
) -> Iterable[dict[str, Any]]:
    info = connection.execute(
        f"PRAGMA table_info({_quoted_identifier(table)})"
    ).fetchall()
    columns = [str(row[1]) for row in info]
    primary = [
        str(row[1])
        for row in sorted(
            (row for row in info if int(row[5]) > 0), key=lambda row: int(row[5])
        )
    ]
    order = primary or ["rowid"]
    order_sql = ",".join(_quoted_identifier(name) for name in order)
    select_columns = ",".join(_quoted_identifier(name) for name in columns)
    try:
        cursor = connection.execute(
            f"SELECT {select_columns} FROM {_quoted_identifier(table)} ORDER BY {order_sql}"
        )
    except sqlite3.OperationalError:
        # WITHOUT ROWID and virtual tables without declared PKs still need a
        # deterministic order.  Ordering by every declared column is stable.
        order_sql = ",".join(_quoted_identifier(name) for name in columns)
        cursor = connection.execute(
            f"SELECT {select_columns} FROM {_quoted_identifier(table)} ORDER BY {order_sql}"
        )
    for row in cursor:
        yield {
            "columns": [
                [name, _encode_sqlite_value(row[index], table=table, column=name)]
                for index, name in enumerate(columns)
            ]
        }


def _logical_inventory_hash(
    connection: sqlite3.Connection, tables: list[str]
) -> tuple[str, dict[str, str]]:
    schema = [
        [str(row[0]), str(row[1]), str(row[2]), row[3]]
        for row in connection.execute(
            """
            SELECT type, name, tbl_name, sql FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
            ORDER BY type, name
            """
        )
    ]
    root = hashlib.sha256()
    root.update(canonical_json_bytes({"schema": schema}))
    rolling: dict[str, str] = {}
    for table in tables:
        digest = hashlib.sha256()
        for row in _table_logical_rows(connection, table):
            encoded = canonical_json_bytes(row)
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        rolling[table] = digest.hexdigest()
        root.update(canonical_json_bytes([table, rolling[table]]))
    return root.hexdigest(), rolling


def _distinct(connection: sqlite3.Connection, table: str, column: str) -> list[Any]:
    if not _table_exists(connection, table):
        return []
    columns = {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({_quoted_identifier(table)})")
    }
    if column not in columns:
        return []
    return [
        row[0]
        for row in connection.execute(
            f"SELECT DISTINCT {_quoted_identifier(column)} FROM {_quoted_identifier(table)} "
            f"ORDER BY {_quoted_identifier(column)}"
        )
    ]


def _malformed_json_count(connection: sqlite3.Connection) -> int:
    total = 0
    for table, columns in (
        ("memories", ("context", "tags")),
        ("memory_versions", ("context", "tags")),
        ("facts", ("tags",)),
    ):
        if not _table_exists(connection, table):
            continue
        available = {
            str(row[1])
            for row in connection.execute(
                f"PRAGMA table_info({_quoted_identifier(table)})"
            )
        }
        selected = [column for column in columns if column in available]
        if not selected:
            continue
        sql = ",".join(_quoted_identifier(column) for column in selected)
        for row in connection.execute(f"SELECT {sql} FROM {_quoted_identifier(table)}"):
            for value in row:
                if value is None:
                    continue
                try:
                    json.loads(value)
                except (TypeError, ValueError, RecursionError):
                    total += 1
    return total


def _malformed_time_count(connection: sqlite3.Connection) -> int:
    total = 0
    targets = (
        ("memories", ("created_at", "updated_at")),
        ("memory_versions", ("changed_at", "valid_from", "valid_to")),
        ("facts", ("created_at", "verified_at")),
        ("memory_relationships", ("created_at",)),
    )
    for table, columns in targets:
        if not _table_exists(connection, table):
            continue
        available = {
            str(row[1])
            for row in connection.execute(
                f"PRAGMA table_info({_quoted_identifier(table)})"
            )
        }
        selected = [column for column in columns if column in available]
        if not selected:
            continue
        sql = ",".join(_quoted_identifier(column) for column in selected)
        for row in connection.execute(f"SELECT {sql} FROM {_quoted_identifier(table)}"):
            for value in row:
                if value is None:
                    continue
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    continue
                try:
                    datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                except (ValueError, OverflowError):
                    total += 1
    return total


def _orphan_relationship_count(connection: sqlite3.Connection) -> int:
    if not _table_exists(connection, "memory_relationships") or not _table_exists(
        connection, "memories"
    ):
        return 0
    return int(
        connection.execute(
            """
            SELECT count(*) FROM memory_relationships r
            LEFT JOIN memories source ON source.id=r.source_id
            LEFT JOIN memories target ON target.id=r.target_id
            WHERE source.id IS NULL OR target.id IS NULL
            """
        ).fetchone()[0]
    )


def inventory_database(path: Path) -> dict[str, Any]:
    """Build a deterministic logical inventory inside one read transaction."""

    connection = _readonly_connection(path)
    try:
        connection.execute("BEGIN")
        tables = _table_names(connection)
        table_counts = {table: _count(connection, table) for table in tables}
        logical_hash, table_hashes = _logical_inventory_hash(connection, tables)
        max_schema = 0
        if "schema_version" in tables:
            max_schema = int(
                connection.execute(
                    "SELECT COALESCE(MAX(version),0) FROM schema_version"
                ).fetchone()[0]
            )
        vector_count = 0
        vector_bytes = 0
        if "memories" in tables:
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(memories)")
            }
            if "vector_embedding" in columns:
                vector_count, vector_bytes = connection.execute(
                    "SELECT count(vector_embedding), COALESCE(sum(length(vector_embedding)),0) "
                    "FROM memories WHERE vector_embedding IS NOT NULL"
                ).fetchone()
        unknown_memory = sorted(
            "<null>" if value is None else str(value)
            for value in _distinct(connection, "memories", "category")
            if value not in _KNOWN_MEMORY_TYPES
        )
        unknown_relationships = sorted(
            "<null>" if value is None else str(value)
            for value in _distinct(connection, "memory_relationships", "relationship")
            if value not in _KNOWN_RELATIONSHIPS
        )
        unknown_changes = sorted(
            "<null>" if value is None else str(value)
            for value in _distinct(connection, "memory_versions", "change_type")
            if value not in _KNOWN_CHANGES
        )
        quick_rows = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
        foreign_rows = [
            list(row) for row in connection.execute("PRAGMA foreign_key_check")
        ]
        memory_count = table_counts.get("memories", 0)
        version_count = table_counts.get("memory_versions", 0)
        fact_count = table_counts.get("facts", 0)
        relationship_count = table_counts.get("memory_relationships", 0)
        orphan_count = _orphan_relationship_count(connection)
        malformed_json = _malformed_json_count(connection)
        malformed_time = _malformed_time_count(connection)
        connection.rollback()
    finally:
        connection.close()
    db_size = path.stat().st_size
    wal_path = Path(str(path) + "-wal")
    wal_size = wal_path.stat().st_size if wal_path.is_file() else 0
    source_size = db_size + wal_size
    return {
        "active_db": path.name,
        "database_bytes": db_size,
        "wal_bytes": wal_size,
        "logical_sha256": logical_hash,
        "table_hashes": table_hashes,
        "tables": table_counts,
        "max_schema_version": max_schema,
        "memory_count": memory_count,
        "version_count": version_count,
        "fact_count": fact_count,
        "relationship_count": relationship_count,
        "vector_count": int(vector_count),
        "vector_bytes": int(vector_bytes),
        "unknown_memory_categories": unknown_memory,
        "unknown_relationship_types": unknown_relationships,
        "unknown_change_types": unknown_changes,
        "malformed_json_fields": malformed_json,
        "malformed_time_fields": malformed_time,
        "orphan_references": orphan_count,
        "quick_check": quick_rows[0] if len(quick_rows) == 1 else quick_rows,
        "foreign_key_violations": foreign_rows,
        "estimated_event_rows": memory_count
        + version_count
        + fact_count
        + relationship_count
        + orphan_count,
        "estimated_projection_rows": memory_count
        + fact_count
        + relationship_count
        + orphan_count,
        "required_bytes_estimate": source_size * 2 + 64 * 1024 * 1024,
    }


def _physical_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _is_link_or_reparse(path: Path) -> bool:
    """Detect POSIX links and Windows reparse points without following them."""

    try:
        details = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise MigrationV7Error(
            "UNSAFE_MIGRATION_PATH", "migration path metadata is unavailable"
        ) from exc
    return stat.S_ISLNK(details.st_mode) or bool(
        getattr(details, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _validated_migration_root(storage: Path, *, create: bool) -> Path | None:
    """Return an owned migration root after validating each lexical component.

    Dry-run passes ``create=False`` so a missing component returns ``None`` and
    no directory is created.  Apply creates one component at a time and rejects
    links, reparse points, non-directories, and resolved paths outside storage.
    """

    try:
        storage_root = storage.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise MigrationV7Error(
            "UNSAFE_MIGRATION_PATH", "storage directory is unavailable"
        ) from exc
    current = storage
    for component in ("migrations", "v7"):
        current = current / component
        if _is_link_or_reparse(current):
            raise MigrationV7Error(
                "UNSAFE_MIGRATION_PATH",
                f"migration component is a link or reparse point: {component}",
            )
        if not current.exists():
            if not create:
                return None
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            except OSError as exc:
                raise MigrationV7Error(
                    "UNSAFE_MIGRATION_PATH",
                    f"migration component cannot be created: {component}",
                ) from exc
        try:
            details = current.lstat()
            resolved = current.resolve(strict=True)
            resolved.relative_to(storage_root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise MigrationV7Error(
                "UNSAFE_MIGRATION_PATH",
                f"migration component escapes storage: {component}",
            ) from exc
        if _is_link_or_reparse(current) or not stat.S_ISDIR(details.st_mode):
            raise MigrationV7Error(
                "UNSAFE_MIGRATION_PATH",
                f"migration component is not an owned directory: {component}",
            )
    return current


def _partial_candidates(root: Path | None) -> list[Path]:
    """Enumerate resumable candidates without traversing untrusted run links."""

    if root is None:
        return []
    candidates: list[Path] = []
    try:
        children = sorted(root.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise MigrationV7Error(
            "UNSAFE_MIGRATION_PATH", "migration root cannot be enumerated"
        ) from exc
    for run_dir in children:
        if not run_dir.name.startswith("mig_"):
            continue
        if _is_link_or_reparse(run_dir):
            raise MigrationV7Error(
                "UNSAFE_MIGRATION_PATH", "migration run directory is unsafe"
            )
        try:
            is_directory = stat.S_ISDIR(run_dir.lstat().st_mode)
        except OSError as exc:
            raise MigrationV7Error(
                "UNSAFE_MIGRATION_PATH", "migration run metadata is unavailable"
            ) from exc
        if not is_directory or not re.fullmatch(r"mig_[0-9a-f]{64}", run_dir.name):
            raise MigrationV7Error(
                "UNSAFE_MIGRATION_PATH", "migration run directory is malformed"
            )
        candidate = run_dir / "candidate.db.partial"
        if _is_link_or_reparse(candidate):
            raise MigrationV7Error(
                "UNSAFE_MIGRATION_PATH", "partial candidate is unsafe"
            )
        if not candidate.exists():
            continue
        try:
            if not stat.S_ISREG(candidate.lstat().st_mode):
                raise MigrationV7Error(
                    "UNSAFE_MIGRATION_PATH", "partial candidate is not a regular file"
                )
        except OSError as exc:
            raise MigrationV7Error(
                "UNSAFE_MIGRATION_PATH", "partial candidate metadata is unavailable"
            ) from exc
        candidates.append(candidate)
    return candidates


def _sqlite_backup(source: Path, destination: Path) -> None:
    source_uri = "file:" + quote(source.resolve().as_posix(), safe="/:") + "?mode=ro"
    source_connection = sqlite3.connect(source_uri, uri=True)
    target_connection = sqlite3.connect(destination)
    try:
        source_connection.execute("PRAGMA foreign_keys=ON")
        source_connection.execute("PRAGMA busy_timeout=0")
        source_connection.backup(target_connection)
        target_connection.commit()
    finally:
        target_connection.close()
        source_connection.close()


def _integrity(path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        quick = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        foreign = [list(row) for row in connection.execute("PRAGMA foreign_key_check")]
    finally:
        connection.close()
    if quick != ["ok"] or foreign:
        raise MigrationV7Error(
            "SOURCE_INTEGRITY_FAILED",
            f"integrity={quick!r}, foreign_key_violations={len(foreign)}",
        )
    return {"integrity_check": "ok", "foreign_key_violations": foreign}


def _lossless_row(row: sqlite3.Row, table: str) -> dict[str, Any]:
    return {
        "table": table,
        "columns": [
            [
                name,
                (
                    {
                        "$legacy_vector": {
                            "length": len(row[name]),
                            "sha256": hashlib.sha256(row[name]).hexdigest(),
                            "snapshot": "source.snapshot.db",
                        }
                    }
                    if table == "memories"
                    and name == "vector_embedding"
                    and isinstance(row[name], bytes)
                    else _encode_sqlite_value(row[name], table=table, column=name)
                ),
            ]
            for name in tuple(row.keys())
        ],
    }


def _parse_legacy_json(value: Any, expected: type, default: Any) -> tuple[Any, str]:
    if value is None:
        return default, "missing"
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, RecursionError):
        return default, "invalid"
    if not isinstance(parsed, expected):
        return default, "invalid"
    return parsed, "exact"


def _parse_legacy_time(value: Any) -> tuple[int, str, Any]:
    if value is None or value == "":
        return 0, "missing", value
    if isinstance(value, bool):
        return 0, "invalid", value
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        microseconds = int(float(value) * 1_000_000)
        if -(2**63) <= microseconds <= 2**63 - 1:
            return microseconds, "exact", value
        return 0, "invalid", value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        return 0, "invalid", value
    quality = "exact"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
        quality = "naive_assumed_utc"
    microseconds = int(parsed.timestamp() * 1_000_000)
    if not -(2**63) <= microseconds <= 2**63 - 1:
        return 0, "invalid", value
    return microseconds, quality, value


def _column(row: sqlite3.Row, name: str, default: Any = None) -> Any:
    return row[name] if name in tuple(row.keys()) else default


def _legacy_bool(value: Any, default: bool = False) -> bool:
    return bool(value) if value in (0, 1, False, True) else default


def _legacy_score(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and 0 <= number <= 1 else None


def _memory_state(
    row: sqlite3.Row, *, base: dict[str, Any] | None = None
) -> dict[str, Any]:
    category = _column(row, "category") if base is None else base["record_type"]
    legacy_type = None
    if base is None:
        if category not in _KNOWN_MEMORY_TYPES:
            legacy_type = "<null>" if category is None else str(category)
            category = "legacy"
    else:
        legacy_type = base["legacy_type"]
    context, _ = _parse_legacy_json(_column(row, "context"), dict, {})
    tags, _ = _parse_legacy_json(_column(row, "tags"), list, [])
    worked_value = _column(row, "worked")
    worked = None if worked_value is None else _legacy_bool(worked_value)
    content = _column(row, "content", "")
    if not isinstance(content, str):
        content = "<null>" if content is None else str(content)
    return {
        "record_type": category,
        "legacy_type": legacy_type,
        "content": content,
        "rationale": _column(row, "rationale"),
        "context": context,
        "tags": tags,
        "file_path": base.get("file_path")
        if base is not None
        else _column(row, "file_path"),
        "file_path_relative": base.get("file_path_relative")
        if base is not None
        else _column(row, "file_path_relative"),
        "keywords": base.get("keywords")
        if base is not None
        else _column(row, "keywords"),
        "is_permanent": base.get("is_permanent", False)
        if base is not None
        else _legacy_bool(_column(row, "is_permanent")),
        "pinned": base.get("pinned", False)
        if base is not None
        else _legacy_bool(_column(row, "pinned")),
        "archived": base.get("archived", False)
        if base is not None
        else _legacy_bool(_column(row, "archived")),
        "outcome": _column(row, "outcome"),
        "worked": worked,
        "recall_count": base.get("recall_count", 0)
        if base is not None
        else max(0, int(_column(row, "recall_count", 0) or 0)),
        "surprise_score": base.get("surprise_score")
        if base is not None
        else _legacy_score(_column(row, "surprise_score")),
        "importance_score": base.get("importance_score")
        if base is not None
        else _legacy_score(_column(row, "importance_score")),
        "source_client": base.get("source_client")
        if base is not None
        else _column(row, "source_client"),
        "source_model": base.get("source_model")
        if base is not None
        else _column(row, "source_model"),
        "deleted_at_us": None,
    }


def _memory_normalization(row: sqlite3.Row) -> dict[str, str]:
    """Describe every lossy legacy JSON fallback while raw text stays in ``legacy``."""

    _, context_quality = _parse_legacy_json(_column(row, "context"), dict, {})
    _, tags_quality = _parse_legacy_json(_column(row, "tags"), list, [])
    return {
        "context_quality": context_quality,
        "tags_quality": tags_quality,
    }


def _source_row_hash(row: sqlite3.Row, table: str) -> str:
    return sha256_json(_lossless_row(row, table))


def _event_root(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for row in connection.execute(
        "SELECT event_hash FROM memory_events ORDER BY event_id"
    ):
        digest.update(bytes.fromhex(str(row[0])))
    return digest.hexdigest()


def _projected_memory_state(row: sqlite3.Row) -> dict[str, Any]:
    try:
        context = json.loads(row["context_json"])
        tags = json.loads(row["tags_json"])
    except (TypeError, ValueError, RecursionError) as exc:
        raise MigrationV7Error(
            "VALIDATION_FAILED", "current memory projection JSON is invalid"
        ) from exc
    return {
        "record_type": row["record_type"],
        "legacy_type": row["legacy_type"],
        "content": row["content"],
        "rationale": row["rationale"],
        "context": context,
        "tags": tags,
        "file_path": row["file_path"],
        "file_path_relative": row["file_path_relative"],
        "keywords": row["keywords"],
        "is_permanent": bool(row["is_permanent"]),
        "pinned": bool(row["pinned"]),
        "archived": bool(row["archived"]),
        "outcome": row["outcome"],
        "worked": None if row["worked"] is None else bool(row["worked"]),
        "recall_count": int(row["recall_count"]),
        "surprise_score": row["surprise_score"],
        "importance_score": row["importance_score"],
        "source_client": row["source_client"],
        "source_model": row["source_model"],
        "deleted_at_us": row["deleted_at_us"],
    }


def _validate_retained_compatibility(
    connection: sqlite3.Connection,
    workspace_id: str,
) -> None:
    """Compare current retained-v6 rows to their unique live typed claims."""

    try:
        claims = build_live_compatibility_claim_index(connection, workspace_id)
    except CompatibilityStreamError as exc:
        raise MigrationV7Error(
            "VALIDATION_FAILED", "current compatibility claims are invalid"
        ) from exc

    compatibility_rows: dict[str, dict[str, sqlite3.Row]] = {}
    for table in _COMPATIBILITY_SOURCE_KINDS:
        rows = {
            str(row["id"]): row
            for row in connection.execute(
                f"SELECT * FROM {_quoted_identifier(table)} ORDER BY id"
            )
        }
        compatibility_rows[table] = rows
        live_ids = {
            legacy_id
            for (source_table, legacy_id), _stream_id in claims.items()
            if source_table == table
        }
        if live_ids != set(rows):
            raise MigrationV7Error(
                "VALIDATION_FAILED",
                f"current compatibility identity set differs: {table}",
            )

    for legacy_id, source in compatibility_rows["memories"].items():
        stream_id = claims[("memories", legacy_id)]
        projected = connection.execute(
            "SELECT * FROM memory_records WHERE workspace_id=? AND record_id=?",
            (workspace_id, stream_id),
        ).fetchone()
        if projected is None or canonical_json_bytes(
            _memory_state(source)
        ) != canonical_json_bytes(_projected_memory_state(projected)):
            raise MigrationV7Error(
                "VALIDATION_FAILED", "current memory compatibility row diverges"
            )

    for legacy_id, source in compatibility_rows["facts"].items():
        stream_id = claims[("facts", legacy_id)]
        projected = connection.execute(
            "SELECT * FROM memory_fact_versions WHERE workspace_id=? "
            "AND fact_id=? AND transaction_to_us IS NULL",
            (workspace_id, stream_id),
        ).fetchone()
        tags, tags_quality = _parse_legacy_json(_column(source, "tags"), list, [])
        valid_from, time_quality, time_original = _parse_legacy_time(
            _column(source, "created_at")
        )
        category = _column(source, "category")
        normalized = (
            re.sub(r"[^a-z0-9]+", ".", str(category).lower()).strip(".")
            if category
            else ""
        )
        predicate = f"legacy.fact.{normalized}" if normalized else "legacy.fact"
        source_memory_id = _column(source, "source_memory_id")
        expected_subject = (
            None
            if source_memory_id is None
            else claims.get(("memories", str(source_memory_id)))
        )
        try:
            object_value = json.loads(projected["object_json"])
            evidence = json.loads(projected["evidence_json"])
            metadata = json.loads(projected["metadata_json"])
        except (TypeError, ValueError, RecursionError) as exc:
            raise MigrationV7Error(
                "VALIDATION_FAILED", "current fact projection JSON is invalid"
            ) from exc
        if projected is None or not all(
            (
                projected["subject_record_id"] == expected_subject,
                projected["predicate"] == predicate[:120],
                projected["object_kind"] == "text",
                object_value == str(_column(source, "content", "")),
                projected["legacy_type"] is None,
                projected["confidence"] == 1.0,
                projected["verification_count"]
                == max(0, int(_column(source, "verification_count", 0) or 0)),
                projected["is_verified"]
                == int(_legacy_bool(_column(source, "is_verified"))),
                evidence == [],
                metadata
                == {
                    "legacy_tags": tags,
                    "tags_quality": tags_quality,
                    "raw_content_hash": _column(source, "content_hash"),
                    "time_original": time_original,
                    "time_quality": time_quality,
                },
                projected["valid_from_us"] == valid_from,
                projected["valid_to_us"] is None,
            )
        ):
            raise MigrationV7Error(
                "VALIDATION_FAILED", "current fact compatibility row diverges"
            )

    orphan_targets: dict[str, set[str]] = {}
    for row in connection.execute(
        "SELECT legacy_id,target_id FROM legacy_id_map WHERE workspace_id=? "
        "AND source_table='memory_relationships.orphan' "
        "AND target_kind='placeholder'",
        (workspace_id,),
    ):
        orphan_targets.setdefault(str(row[0]), set()).add(str(row[1]))

    def relationship_endpoint(value: Any) -> str:
        legacy_id = str(value)
        live = claims.get(("memories", legacy_id))
        if live is not None:
            return live
        placeholders = orphan_targets.get(f"memories:{legacy_id}", set())
        if len(placeholders) != 1:
            raise MigrationV7Error(
                "VALIDATION_FAILED", "current relationship endpoint is ambiguous"
            )
        placeholder = next(iter(placeholders))
        row = connection.execute(
            "SELECT deleted_at_us FROM memory_records WHERE workspace_id=? "
            "AND record_id=?",
            (workspace_id, placeholder),
        ).fetchone()
        if row is None or row[0] is not None:
            raise MigrationV7Error(
                "VALIDATION_FAILED", "current relationship endpoint is unavailable"
            )
        return placeholder

    for legacy_id, source in compatibility_rows["memory_relationships"].items():
        stream_id = claims[("memory_relationships", legacy_id)]
        projected = connection.execute(
            "SELECT * FROM memory_relationship_versions WHERE workspace_id=? "
            "AND relationship_id=? AND transaction_to_us IS NULL",
            (workspace_id, stream_id),
        ).fetchone()
        relation_value = _column(source, "relationship")
        if relation_value in _KNOWN_RELATIONSHIPS:
            relationship_type = str(relation_value)
            legacy_type = None
        else:
            relationship_type = "legacy"
            legacy_type = "<null>" if relation_value is None else str(relation_value)
        confidence = _legacy_score(_column(source, "confidence", 1.0))
        if confidence is None:
            confidence = 1.0
        valid_from, _quality, _original = _parse_legacy_time(
            _column(source, "created_at")
        )
        if projected is None or not all(
            (
                projected["source_record_id"]
                == relationship_endpoint(_column(source, "source_id")),
                projected["target_record_id"]
                == relationship_endpoint(_column(source, "target_id")),
                projected["relationship_type"] == relationship_type,
                projected["legacy_type"] == legacy_type,
                projected["description"] == _column(source, "description"),
                projected["confidence"] == confidence,
                projected["valid_from_us"] == valid_from,
                projected["valid_to_us"] is None,
            )
        ):
            raise MigrationV7Error(
                "VALIDATION_FAILED",
                "current relationship compatibility row diverges",
            )


def _run_migration_16(path: Path, workspace_id: str) -> None:
    from .schema import run_migrations

    run_migrations(str(path), workspace_id=workspace_id)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=30000")
    finally:
        connection.close()


class MigrationV7Service:
    """Resolve registered workspaces and perform offline v7 lifecycle actions."""

    def __init__(
        self,
        registry: WorkspaceRegistry,
        *,
        fault_injector: Callable[[str, dict[str, Any]], None] | None = None,
        clock_us: Callable[[], int] | None = None,
    ) -> None:
        if not isinstance(registry, WorkspaceRegistry):
            raise TypeError("MigrationV7Service requires WorkspaceRegistry")
        self.registry = registry
        self._fault_injector = fault_injector
        self._clock_us = clock_us or (lambda: time.time_ns() // 1_000)

    def _fault(self, stage: str, **details: Any) -> None:
        if self._fault_injector is not None:
            self._fault_injector(stage, details)

    def _storage(self, selector):
        workspace = self.registry.resolve(selector)
        storage = resolve_derived_path(workspace.root, ".daem0nmcp", "storage")
        return workspace, storage

    def _validate_active_v7(self, resolved, workspace_id: str) -> None:
        """Fail closed before treating a pointer target as already active."""

        required = _V7_TABLE_NAMES
        connection = sqlite3.connect(resolved.path)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            tables = set(_table_names(connection))
            versions = (
                {
                    int(row[0])
                    for row in connection.execute("SELECT version FROM schema_version")
                }
                if "schema_version" in tables
                else set()
            )
            version = max(versions, default=0)
            quick = [
                str(row[0]) for row in connection.execute("PRAGMA integrity_check")
            ]
            foreign = list(connection.execute("PRAGMA foreign_key_check"))
            if (
                version < CURRENT_SCHEMA_VERSION
                or not versions >= REQUIRED_V7_SCHEMA_VERSIONS
                or not required <= tables
                or quick != ["ok"]
                or foreign
            ):
                raise MigrationV7Error(
                    "ACTIVE_V7_INVALID", "active target schema or integrity is invalid"
                )
            run_id = resolved.migration_run_id
            if run_id is None:
                return
            expected_relative = f"migrations/v7/{run_id}/candidate.db"
            if resolved.relative_path != expected_relative:
                raise MigrationV7Error(
                    "ACTIVE_V7_INVALID",
                    "active target does not match its migration run",
                )
            run = connection.execute(
                "SELECT workspace_id, source_db_sha256, status, target_format_version "
                "FROM v7_migration_runs WHERE migration_run_id=?",
                (run_id,),
            ).fetchone()
            if (
                run is None
                or run[0] != workspace_id
                or run[2] != "active"
                or int(run[3]) != TARGET_FORMAT_VERSION
            ):
                raise MigrationV7Error(
                    "ACTIVE_V7_INVALID", "active migration metadata is invalid"
                )
            snapshot = resolved.path.parent / "source.snapshot.db"
            if (
                snapshot.is_symlink()
                or not snapshot.is_file()
                or _physical_sha256(snapshot) != run[1]
            ):
                raise MigrationV7Error(
                    "ACTIVE_V7_INVALID", "active migration snapshot fingerprint differs"
                )
        except sqlite3.Error as exc:
            raise MigrationV7Error(
                "ACTIVE_V7_INVALID", "active target cannot be validated"
            ) from exc
        finally:
            connection.close()

    def _load_published_run(
        self,
        candidate: Path,
        run_id: str,
        workspace_id: str,
        *,
        error_code: str,
    ) -> tuple[str, int, dict[str, Any], dict[str, Any]]:
        """Validate immutable run identity and return status/source metadata."""

        if (
            candidate.parent.name != run_id
            or _is_link_or_reparse(candidate)
            or not candidate.is_file()
        ):
            raise MigrationV7Error(error_code, "published candidate path is invalid")
        try:
            if not stat.S_ISREG(candidate.lstat().st_mode):
                raise MigrationV7Error(
                    error_code, "published candidate is not a regular file"
                )
        except OSError as exc:
            raise MigrationV7Error(
                error_code, "published candidate metadata is unavailable"
            ) from exc
        connection = sqlite3.connect(candidate)
        try:
            row = connection.execute(
                "SELECT workspace_id,source_db_sha256,source_format_version,status,"
                "source_inventory_json,validation_json FROM v7_migration_runs "
                "WHERE migration_run_id=?",
                (run_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise MigrationV7Error(
                error_code, "published run metadata is invalid"
            ) from exc
        finally:
            connection.close()
        if row is None or row[0] != workspace_id:
            raise MigrationV7Error(error_code, "published run identity is invalid")
        snapshot = candidate.parent / "source.snapshot.db"
        if (
            _is_link_or_reparse(snapshot)
            or not snapshot.is_file()
            or _physical_sha256(snapshot) != row[1]
        ):
            raise MigrationV7Error(error_code, "published snapshot is invalid")
        try:
            inventory = json.loads(row[4])
            canonical_inventory = canonical_json_bytes(inventory).decode("utf-8")
        except (TypeError, ValueError, RecursionError) as exc:
            raise MigrationV7Error(
                error_code, "published inventory is invalid"
            ) from exc
        if not isinstance(inventory, dict) or canonical_inventory != row[4]:
            raise MigrationV7Error(error_code, "published inventory is non-canonical")
        validation: dict[str, Any] = {}
        if row[5] is not None:
            try:
                parsed_validation = json.loads(row[5])
                canonical_validation = canonical_json_bytes(parsed_validation).decode(
                    "utf-8"
                )
            except (TypeError, ValueError, RecursionError) as exc:
                raise MigrationV7Error(
                    error_code, "published validation is invalid"
                ) from exc
            if (
                not isinstance(parsed_validation, dict)
                or canonical_validation != row[5]
            ):
                raise MigrationV7Error(
                    error_code, "published validation is non-canonical"
                )
            validation = parsed_validation
        return str(row[3]), int(row[2]), inventory, validation

    def _mark_candidate_active(
        self, candidate: Path, run_id: str, workspace_id: str
    ) -> None:
        """Atomically finish a fully validated candidate's metadata transition."""

        now = self._clock_us()
        active = sqlite3.connect(candidate)
        try:
            active.execute("PRAGMA foreign_keys=ON")
            active.execute("BEGIN IMMEDIATE")
            changed = active.execute(
                "UPDATE v7_migration_runs SET status='active', updated_at_us=?, "
                "activated_at_us=?, rolled_back_at_us=NULL, last_error_json=NULL "
                "WHERE migration_run_id=? AND status='ready'",
                (now, now, run_id),
            ).rowcount
            if changed != 1:
                raise MigrationV7Error(
                    "ACTIVATION_STATE_INVALID", "candidate run is not ready"
                )
            active.execute(
                "UPDATE projection_manifests SET status='active', activated_at_us=? "
                "WHERE workspace_id=? AND status='ready' AND generation=1 "
                "AND builder_version='v7-migration-1' "
                "AND projection_name IN "
                "('memory_records','memory_fact_versions',"
                "'memory_relationship_versions')",
                (now, workspace_id),
            )
            active.commit()
        except Exception:
            active.rollback()
            raise
        finally:
            active.close()

    def _bootstrap_lexical(self, candidate: Path, workspace_id: str) -> None:
        """Require an idempotent, validated lexical partition before cutover."""

        from ..retrieval.projections import (
            LexicalProjectionBuilder,
            ProjectionBuildError,
        )

        connection = sqlite3.connect(candidate)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            builder = LexicalProjectionBuilder(connection, clock_us=self._clock_us)
            if not builder.active_is_current(workspace_id):
                builder.rebuild(workspace_id)
            if not builder.active_is_current(workspace_id):
                raise ProjectionBuildError(
                    "PROJECTION_VALIDATION_FAILED",
                    "lexical bootstrap did not produce an active partition",
                )
            connection.commit()
        except Exception as exc:
            connection.rollback()
            raise MigrationV7Error(
                "LEXICAL_BOOTSTRAP_FAILED",
                "required lexical projection could not be activated",
            ) from exc
        finally:
            connection.close()

    def _mark_candidate_rolled_back(self, candidate: Path, run_id: str) -> None:
        """Durably complete rollback metadata after the pointer is already v6."""

        now = self._clock_us()
        connection = sqlite3.connect(candidate)
        try:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE v7_migration_runs SET status='rolled_back', updated_at_us=?, "
                "rolled_back_at_us=? WHERE migration_run_id=? AND status='active'",
                (now, now, run_id),
            ).rowcount
            if changed != 1:
                raise MigrationV7Error(
                    "ROLLBACK_STATE_INVALID", "candidate run is not active"
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _recover_ready_pointer(
        self, resolved, workspace_id: str
    ) -> MigrationResult | None:
        """Finish activation when the pointer was durable before metadata commit."""

        run_id = resolved.migration_run_id
        if run_id is None:
            return None
        expected_relative = f"migrations/v7/{run_id}/candidate.db"
        if resolved.relative_path != expected_relative:
            return None
        status, source_format, inventory, prior_validation = self._load_published_run(
            resolved.path,
            run_id,
            workspace_id,
            error_code="ACTIVE_V7_INVALID",
        )
        if status != "ready":
            return None
        validation = self._finalize_candidate(
            resolved.path,
            run_id,
            workspace_id,
            inventory,
            retained_authority=prior_validation.get("authority") == "retained_v7",
        )
        self._bootstrap_lexical(resolved.path, workspace_id)
        self._mark_candidate_active(resolved.path, run_id, workspace_id)
        self._validate_active_v7(resolved, workspace_id)
        return MigrationResult(
            status="activated",
            action="resume",
            workspace_id=workspace_id,
            source_format=source_format,
            migration_run_id=run_id,
            active_generation=resolved.generation,
            inventory=inventory,
            validation=validation,
        )

    def dry_run(
        self, selector: str | os.PathLike[str] | None = None
    ) -> MigrationResult:
        workspace, storage = self._storage(selector)
        with DatabaseFileLock(storage, "shared"):
            resolved = resolve_active_database(storage)
            if resolved.format_version == TARGET_FORMAT_VERSION:
                from .v7_schema_upgrade import V7SchemaUpgradeService

                try:
                    upgrade = V7SchemaUpgradeService(
                        fault_injector=self._fault_injector,
                        clock_us=self._clock_us,
                    ).inspect(storage, workspace.workspace_id, resolved)
                except MigrationV7Error as exc:
                    if exc.code == "V7_PHYSICAL_SCHEMA_INVALID":
                        try:
                            self._validate_active_v7(resolved, workspace.workspace_id)
                        except MigrationV7Error as active_error:
                            raise active_error from exc
                        raise
                    if exc.code != "UNSUPPORTED_V7_SCHEMA":
                        raise
                    upgrade = None
                if upgrade is not None:
                    return upgrade
            inventory = inventory_database(resolved.path)
            inventory["active_db"] = resolved.relative_path
            if resolved.format_version == TARGET_FORMAT_VERSION:
                action = "already_active"
            else:
                partials = _partial_candidates(
                    _validated_migration_root(storage, create=False)
                )
                action = "resume" if partials else "migrate"
            return MigrationResult(
                status="dry_run",
                action=action,
                workspace_id=workspace.workspace_id,
                source_format=resolved.format_version,
                migration_run_id=resolved.migration_run_id,
                active_generation=resolved.generation,
                inventory=inventory,
                validation={
                    "quick_check": inventory["quick_check"],
                    "foreign_key_violations": inventory["foreign_key_violations"],
                },
            )

    def apply(
        self,
        selector: str | os.PathLike[str] | None = None,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> MigrationResult:
        if (
            not isinstance(batch_size, int)
            or isinstance(batch_size, bool)
            or not 1 <= batch_size <= 10_000
        ):
            raise ValueError("batch_size must be between 1 and 10000")
        workspace, storage = self._storage(selector)
        with DatabaseFileLock(storage, "exclusive"):
            resolved = resolve_active_database(storage)
            if resolved.format_version == TARGET_FORMAT_VERSION:
                from .v7_schema_upgrade import V7SchemaUpgradeService

                try:
                    upgrade = V7SchemaUpgradeService(
                        fault_injector=self._fault_injector,
                        clock_us=self._clock_us,
                    ).apply(storage, workspace.workspace_id, resolved)
                except MigrationV7Error as exc:
                    if exc.code == "V7_PHYSICAL_SCHEMA_INVALID":
                        try:
                            self._validate_active_v7(resolved, workspace.workspace_id)
                        except MigrationV7Error as active_error:
                            raise active_error from exc
                        raise
                    if exc.code != "UNSUPPORTED_V7_SCHEMA":
                        raise
                    upgrade = None
                if upgrade is not None:
                    return upgrade
                recovered = self._recover_ready_pointer(
                    resolved, workspace.workspace_id
                )
                if recovered is not None:
                    return recovered
                self._validate_active_v7(resolved, workspace.workspace_id)
                return MigrationResult(
                    status="already_active",
                    action="already_active",
                    workspace_id=workspace.workspace_id,
                    source_format=resolved.format_version,
                    migration_run_id=resolved.migration_run_id,
                    active_generation=resolved.generation,
                )
            inventory = inventory_database(resolved.path)
            inventory["active_db"] = resolved.relative_path
            resumed = self._find_resumable(
                storage, workspace.workspace_id, inventory, resolved
            )
            if resumed is None:
                run_id, run_dir, snapshot, candidate = self._prepare_candidate(
                    storage,
                    workspace.workspace_id,
                    resolved.path,
                    inventory,
                )
                action = "migrate"
            else:
                run_id, run_dir, snapshot, candidate, action = resumed
            try:
                if action == "reactivate":
                    checkpoints = self._checkpoint_summary(candidate, run_id)
                else:
                    checkpoints = self._import_candidate(
                        candidate,
                        run_id,
                        workspace.workspace_id,
                        batch_size,
                    )
                validation = self._finalize_candidate(
                    candidate,
                    run_id,
                    workspace.workspace_id,
                    inventory,
                    retained_authority=action == "reactivate",
                )
                self._bootstrap_lexical(candidate, workspace.workspace_id)
                self._fault("before_candidate_publish", migration_run_id=run_id)
                connection = sqlite3.connect(candidate)
                try:
                    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                finally:
                    connection.close()
                published = run_dir / "candidate.db"
                already_published = candidate == published
                if published.exists() and not already_published:
                    raise MigrationV7Error(
                        "CANDIDATE_EXISTS", "validated candidate already exists"
                    )
                if not already_published:
                    os.replace(candidate, published)
                relative_candidate = published.relative_to(storage).as_posix()
                pointer = ActiveDatabasePointer(
                    format_version=TARGET_FORMAT_VERSION,
                    generation=resolved.generation + 1,
                    active_db=relative_candidate,
                    previous_db=resolved.relative_path,
                    migration_run_id=run_id,
                )
                self._fault("before_pointer", migration_run_id=run_id)
                try:
                    write_active_pointer(storage, pointer)
                except Exception:
                    try:
                        self._restore_pointer(storage, resolved)
                    finally:
                        if (
                            not already_published
                            and published.is_file()
                            and not published.is_symlink()
                        ):
                            os.replace(published, candidate)
                    raise
                try:
                    self._mark_candidate_active(
                        published, run_id, workspace.workspace_id
                    )
                except Exception:
                    try:
                        self._restore_pointer(storage, resolved)
                    finally:
                        if (
                            not already_published
                            and published.is_file()
                            and not published.is_symlink()
                        ):
                            os.replace(published, candidate)
                    raise
                return MigrationResult(
                    status="activated",
                    action=action,
                    workspace_id=workspace.workspace_id,
                    source_format=resolved.format_version,
                    migration_run_id=run_id,
                    active_generation=pointer.generation,
                    inventory=inventory,
                    checkpoints=checkpoints,
                    validation=validation,
                )
            except MigrationInterrupted:
                raise
            except Exception as exc:
                self._mark_failed(candidate, run_id, exc)
                raise

    def _prepare_candidate(
        self,
        storage: Path,
        workspace_id: str,
        source: Path,
        inventory: dict[str, Any],
    ) -> tuple[str, Path, Path, Path]:
        migration_root = _validated_migration_root(storage, create=True)
        if migration_root is None:  # pragma: no cover - create=True is exhaustive
            raise MigrationV7Error(
                "UNSAFE_MIGRATION_PATH", "migration directory could not be created"
            )
        staging = migration_root / f".snapshot-{inventory['logical_sha256']}.partial"
        if staging.exists() or _is_link_or_reparse(staging):
            raise MigrationV7Error("STALE_SNAPSHOT", "unowned staging snapshot exists")
        _sqlite_backup(source, staging)
        try:
            if _is_link_or_reparse(staging) or not stat.S_ISREG(
                staging.lstat().st_mode
            ):
                raise MigrationV7Error(
                    "UNSAFE_MIGRATION_PATH", "staging snapshot is not a regular file"
                )
            _integrity(staging)
            snapshot_hash = _physical_sha256(staging)
            run_id = deterministic_id(
                "mig",
                "migration",
                workspace_id,
                "snapshot",
                snapshot_hash,
                TARGET_FORMAT_VERSION,
            )
            run_dir = migration_root / run_id
            if run_dir.exists() or _is_link_or_reparse(run_dir):
                raise MigrationV7Error(
                    "MIGRATION_RUN_EXISTS", "run directory requires explicit recovery"
                )
            run_dir.mkdir(mode=0o700)
            if _is_link_or_reparse(run_dir) or not stat.S_ISDIR(
                run_dir.lstat().st_mode
            ):
                raise MigrationV7Error(
                    "UNSAFE_MIGRATION_PATH", "migration run is not an owned directory"
                )
            snapshot = run_dir / "source.snapshot.db"
            os.replace(staging, snapshot)
            self._fault("after_snapshot", migration_run_id=run_id)
        except Exception:
            if staging.exists() and not staging.is_symlink():
                staging.unlink()
            raise
        candidate = run_dir / "candidate.db.partial"
        self._initialize_candidate(
            snapshot,
            candidate,
            run_id,
            workspace_id,
            inventory,
            snapshot_hash,
        )
        return run_id, run_dir, snapshot, candidate

    def _initialize_candidate(
        self,
        snapshot: Path,
        candidate: Path,
        run_id: str,
        workspace_id: str,
        inventory: dict[str, Any],
        snapshot_hash: str,
    ) -> None:
        """Clone one validated snapshot and durably establish resumable metadata."""

        if candidate.exists() or _is_link_or_reparse(candidate):
            raise MigrationV7Error(
                "UNSAFE_MIGRATION_PATH", "partial candidate name is already occupied"
            )
        _sqlite_backup(snapshot, candidate)
        if _is_link_or_reparse(candidate) or not stat.S_ISREG(
            candidate.lstat().st_mode
        ):
            raise MigrationV7Error(
                "UNSAFE_MIGRATION_PATH", "partial candidate is not a regular file"
            )
        _run_migration_16(candidate, workspace_id)
        connection = sqlite3.connect(candidate)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            now = self._clock_us()
            connection.execute(
                """
                INSERT INTO v7_migration_runs (
                    migration_run_id, workspace_id, source_db_sha256,
                    source_schema_version, source_format_version,
                    target_format_version, status, snapshot_name, candidate_name,
                    source_inventory_json, created_at_us, updated_at_us
                ) VALUES (?, ?, ?, ?, 6, 7, 'importing', ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    workspace_id,
                    snapshot_hash,
                    int(inventory["max_schema_version"]),
                    "source.snapshot.db",
                    "candidate.db.partial",
                    canonical_json_bytes(inventory).decode("utf-8"),
                    now,
                    now,
                ),
            )
            connection.commit()
        finally:
            connection.close()
        self._fault("after_ddl", migration_run_id=run_id)

    def _find_resumable(
        self,
        storage: Path,
        workspace_id: str,
        inventory: dict[str, Any],
        resolved,
    ) -> tuple[str, Path, Path, Path, str] | None:
        root = _validated_migration_root(storage, create=False)
        candidates = _partial_candidates(root)
        if not candidates:
            if root is None:
                return None
            snapshot_only: list[Path] = []
            published_matches: list[tuple[str, Path, Path, Path, str]] = []
            canonical_inventory = canonical_json_bytes(inventory).decode("utf-8")
            for run_dir in sorted(root.iterdir(), key=lambda path: path.name):
                if not re.fullmatch(r"mig_[0-9a-f]{64}", run_dir.name):
                    continue
                snapshot = run_dir / "source.snapshot.db"
                published = run_dir / "candidate.db"
                partial = run_dir / "candidate.db.partial"
                if (
                    snapshot.exists()
                    and not published.exists()
                    and not partial.exists()
                ):
                    snapshot_only.append(snapshot)
                if published.exists() and not partial.exists():
                    (
                        status,
                        _source_format,
                        stored_inventory,
                        prior_validation,
                    ) = self._load_published_run(
                        published,
                        run_dir.name,
                        workspace_id,
                        error_code="INVALID_RESUME",
                    )
                    if (
                        canonical_json_bytes(stored_inventory).decode("utf-8")
                        != canonical_inventory
                    ):
                        continue
                    relative_published = published.relative_to(storage).as_posix()
                    rollback_pointer_matches = (
                        resolved.format_version == 6
                        and resolved.migration_run_id == run_dir.name
                        and resolved.previous_db == relative_published
                    )
                    if (
                        status in {"active", "rolled_back"}
                        and not rollback_pointer_matches
                    ):
                        raise MigrationV7Error(
                            "MIGRATION_RUN_EXISTS",
                            "published rollback state does not match the active pointer",
                        )
                    if status not in {"ready", "active", "rolled_back"}:
                        raise MigrationV7Error(
                            "MIGRATION_RUN_EXISTS",
                            "matching published run is not resumable",
                        )
                    retained = (
                        status in {"active", "rolled_back"}
                        or prior_validation.get("authority") == "retained_v7"
                    )
                    published_matches.append(
                        (
                            run_dir.name,
                            run_dir,
                            snapshot,
                            published,
                            "reactivate" if retained else "resume",
                        )
                    )
            if published_matches:
                if len(published_matches) != 1:
                    raise MigrationV7Error(
                        "AMBIGUOUS_RESUME", "multiple published candidates match source"
                    )
                return published_matches[0]
            if not snapshot_only:
                return None
            if len(snapshot_only) != 1:
                raise MigrationV7Error(
                    "AMBIGUOUS_RESUME", "multiple snapshot-only runs exist"
                )
            snapshot = snapshot_only[0]
            run_dir = snapshot.parent
            if (
                _is_link_or_reparse(run_dir)
                or _is_link_or_reparse(snapshot)
                or not snapshot.is_file()
            ):
                raise MigrationV7Error(
                    "UNSAFE_MIGRATION_PATH", "snapshot-only run is unsafe"
                )
            snapshot_hash = _physical_sha256(snapshot)
            run_id = deterministic_id(
                "mig",
                "migration",
                workspace_id,
                "snapshot",
                snapshot_hash,
                TARGET_FORMAT_VERSION,
            )
            if run_dir.name != run_id:
                raise MigrationV7Error(
                    "INVALID_RESUME", "snapshot-only run identity is invalid"
                )
            snapshot_inventory = inventory_database(snapshot)
            if snapshot_inventory["logical_sha256"] != inventory["logical_sha256"]:
                raise MigrationV7Error(
                    "SOURCE_CHANGED", "source inventory changed after snapshot"
                )
            candidate = run_dir / "candidate.db.partial"
            self._initialize_candidate(
                snapshot,
                candidate,
                run_id,
                workspace_id,
                inventory,
                snapshot_hash,
            )
            return run_id, run_dir, snapshot, candidate, "resume"
        if len(candidates) != 1:
            raise MigrationV7Error(
                "AMBIGUOUS_RESUME", "multiple partial candidates exist"
            )
        candidate = candidates[0]
        run_dir = candidate.parent
        run_id = run_dir.name
        if (
            _is_link_or_reparse(candidate)
            or _is_link_or_reparse(run_dir)
            or not re.fullmatch(r"mig_[0-9a-f]{64}", run_id)
        ):
            raise MigrationV7Error(
                "UNSAFE_MIGRATION_PATH", "partial candidate path is unsafe"
            )
        connection = sqlite3.connect(candidate)
        try:
            row = connection.execute(
                "SELECT workspace_id, source_db_sha256, source_inventory_json FROM v7_migration_runs WHERE migration_run_id=?",
                (run_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None or row[0] != workspace_id:
            raise MigrationV7Error(
                "INVALID_RESUME", "candidate run metadata does not match workspace"
            )
        if row[2] != canonical_json_bytes(inventory).decode("utf-8"):
            raise MigrationV7Error(
                "SOURCE_CHANGED", "source inventory changed after snapshot"
            )
        snapshot = run_dir / "source.snapshot.db"
        if (
            _is_link_or_reparse(snapshot)
            or not snapshot.is_file()
            or _physical_sha256(snapshot) != row[1]
        ):
            raise MigrationV7Error(
                "INVALID_RESUME", "snapshot fingerprint does not match run"
            )
        return run_id, run_dir, snapshot, candidate, "resume"

    def _checkpoint_summary(self, candidate: Path, run_id: str) -> dict[str, Any]:
        connection = sqlite3.connect(candidate)
        try:
            return {
                str(row[0]): {
                    "last_legacy_pk": row[1],
                    "rows_imported": int(row[2]),
                    "rolling_hash": str(row[3]),
                    "completed": bool(row[4]),
                }
                for row in connection.execute(
                    "SELECT source_table, last_legacy_pk, rows_imported, "
                    "rolling_hash, completed FROM v7_migration_checkpoints "
                    "WHERE migration_run_id=? ORDER BY source_table",
                    (run_id,),
                )
            }
        finally:
            connection.close()

    def _checkpoint(
        self, connection, run_id: str, table: str
    ) -> tuple[int, int, str, bool]:
        row = connection.execute(
            "SELECT last_legacy_pk, rows_imported, rolling_hash, completed "
            "FROM v7_migration_checkpoints "
            "WHERE migration_run_id=? AND source_table=?",
            (run_id, table),
        ).fetchone()
        if row is None:
            return 0, 0, hashlib.sha256(b"").hexdigest(), False
        return int(row[0] or 0), int(row[1]), str(row[2]), bool(row[3])

    def _save_checkpoint(
        self,
        connection,
        run_id: str,
        table: str,
        last_pk: int | None,
        rows_imported: int,
        rolling_hash: str,
        completed: bool,
    ) -> None:
        now = self._clock_us()
        connection.execute(
            """
            INSERT INTO v7_migration_checkpoints (
                migration_run_id, source_table, last_legacy_pk, rows_imported,
                rolling_hash, completed, updated_at_us
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(migration_run_id, source_table) DO UPDATE SET
                last_legacy_pk=excluded.last_legacy_pk,
                rows_imported=excluded.rows_imported,
                rolling_hash=excluded.rolling_hash,
                completed=excluded.completed,
                updated_at_us=excluded.updated_at_us
            """,
            (
                run_id,
                table,
                None if last_pk is None else str(last_pk),
                rows_imported,
                rolling_hash,
                int(completed),
                now,
            ),
        )
        connection.execute(
            "UPDATE v7_migration_runs SET updated_at_us=?, status='importing' WHERE migration_run_id=?",
            (now, run_id),
        )

    def _import_candidate(
        self,
        candidate: Path,
        run_id: str,
        workspace_id: str,
        batch_size: int,
    ) -> dict[str, Any]:
        connection = sqlite3.connect(candidate)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            for table, importer in (
                ("memories", self._import_memory_row),
                ("facts", self._import_fact_row),
                ("memory_relationships", self._import_relationship_row),
            ):
                last_pk, imported, rolling, completed = self._checkpoint(
                    connection, run_id, table
                )
                if completed:
                    self._fault(
                        "after_completed_checkpoint",
                        migration_run_id=run_id,
                        source_table=table,
                        last_legacy_pk=last_pk,
                        rows_imported=imported,
                    )
                    continue
                while True:
                    rows = connection.execute(
                        f"SELECT * FROM {_quoted_identifier(table)} WHERE id > ? ORDER BY id LIMIT ?",
                        (last_pk, batch_size),
                    ).fetchall()
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        if not rows:
                            self._save_checkpoint(
                                connection,
                                run_id,
                                table,
                                last_pk or None,
                                imported,
                                rolling,
                                True,
                            )
                            connection.commit()
                            break
                        store = EventStore(connection)
                        for row in rows:
                            importer(connection, store, row, run_id, workspace_id)
                            row_hash = _source_row_hash(row, table)
                            rolling = sha256_json([rolling, row_hash])
                            last_pk = int(row["id"])
                            imported += 1
                        self._save_checkpoint(
                            connection, run_id, table, last_pk, imported, rolling, False
                        )
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                    self._fault(
                        "after_batch",
                        migration_run_id=run_id,
                        source_table=table,
                        last_legacy_pk=last_pk,
                        rows_imported=imported,
                    )
            return {
                str(row[0]): {
                    "last_legacy_pk": row[1],
                    "rows_imported": int(row[2]),
                    "rolling_hash": str(row[3]),
                    "completed": bool(row[4]),
                }
                for row in connection.execute(
                    "SELECT source_table, last_legacy_pk, rows_imported, rolling_hash, completed "
                    "FROM v7_migration_checkpoints WHERE migration_run_id=? ORDER BY source_table",
                    (run_id,),
                )
            }
        finally:
            connection.close()

    def _import_memory_row(
        self,
        connection: sqlite3.Connection,
        store: EventStore,
        row: sqlite3.Row,
        run_id: str,
        workspace_id: str,
    ) -> None:
        legacy_id = str(row["id"])
        record_id = deterministic_id(
            "mem", "memory", workspace_id, "legacy", "memories", legacy_id
        )
        state = _memory_state(row)
        stream_version = 0
        versions = connection.execute(
            "SELECT * FROM memory_versions WHERE memory_id=? ORDER BY version_number, id",
            (row["id"],),
        ).fetchall()
        change_events = {
            "created": "memory.created",
            "content_updated": "memory.updated",
            "outcome_recorded": "memory.outcome_recorded",
            "relationship_changed": "memory.relationship_changed",
            "state_changed": "memory.state_changed",
        }
        for version in versions:
            stream_version += 1
            version_state = _memory_state(version, base=state)
            occurred, valid_quality, valid_original = _parse_legacy_time(
                _column(version, "valid_from") or _column(version, "changed_at")
            )
            recorded, transaction_quality, transaction_original = _parse_legacy_time(
                _column(version, "changed_at")
            )
            change_type = _column(version, "change_type")
            store.append_and_project(
                EventCommand(
                    workspace_id=workspace_id,
                    stream_id=record_id,
                    stream_kind="memory",
                    event_type=change_events.get(change_type, "legacy.memory_version"),
                    occurred_at_us=occurred,
                    recorded_at_us=recorded,
                    actor_type="migration",
                    correlation_id=run_id,
                    expected_stream_version=stream_version,
                    payload={
                        "legacy": _lossless_row(version, "memory_versions"),
                        "record": version_state,
                        "normalization": _memory_normalization(version),
                        "time": {
                            "valid_original": valid_original,
                            "valid_quality": valid_quality,
                            "transaction_original": transaction_original,
                            "transaction_quality": transaction_quality,
                        },
                    },
                )
            )
        stream_version += 1
        occurred, occurred_quality, occurred_original = _parse_legacy_time(
            _column(row, "updated_at") or _column(row, "created_at")
        )
        recorded, recorded_quality, recorded_original = _parse_legacy_time(
            _column(row, "updated_at") or _column(row, "created_at")
        )
        final = store.append_and_project(
            EventCommand(
                workspace_id=workspace_id,
                stream_id=record_id,
                stream_kind="memory",
                event_type="legacy.memory_state_imported",
                occurred_at_us=occurred,
                recorded_at_us=recorded,
                actor_type="migration",
                correlation_id=run_id,
                expected_stream_version=stream_version,
                payload={
                    "legacy": _lossless_row(row, "memories"),
                    "record": state,
                    "normalization": _memory_normalization(row),
                    "time": {
                        "occurred_original": occurred_original,
                        "occurred_quality": occurred_quality,
                        "recorded_original": recorded_original,
                        "recorded_quality": recorded_quality,
                    },
                },
            )
        )
        connection.execute(
            """
            INSERT INTO legacy_id_map (
                migration_run_id, source_table, legacy_id, workspace_id,
                target_kind, target_id, source_row_hash, imported_event_id
            ) VALUES (?, 'memories', ?, ?, 'memory', ?, ?, ?)
            """,
            (
                run_id,
                legacy_id,
                workspace_id,
                record_id,
                _source_row_hash(row, "memories"),
                final.event_id,
            ),
        )

    def _mapped_memory(
        self,
        connection: sqlite3.Connection,
        store: EventStore,
        legacy_id: Any,
        run_id: str,
        workspace_id: str,
        occurred_at_us: int,
    ) -> str:
        legacy_text = str(legacy_id)
        row = connection.execute(
            "SELECT target_id FROM legacy_id_map WHERE migration_run_id=? AND source_table='memories' AND legacy_id=?",
            (run_id, legacy_text),
        ).fetchone()
        if row is not None:
            return str(row[0])
        row = connection.execute(
            "SELECT target_id FROM legacy_id_map WHERE migration_run_id=? "
            "AND source_table='memory_relationships.orphan' AND legacy_id=? "
            "AND target_kind='placeholder'",
            (run_id, f"memories:{legacy_text}"),
        ).fetchone()
        if row is not None:
            return str(row[0])
        record_id = deterministic_id(
            "mem", "memory", workspace_id, "legacy-placeholder", "memories", legacy_text
        )
        state = {
            "record_type": "legacy",
            "legacy_type": "orphan:memories",
            "content": f"Missing legacy memory reference {legacy_text}",
            "rationale": "Placeholder preserves an orphaned v6 relationship endpoint.",
            "context": {"source_table": "memories", "legacy_id": legacy_text},
            "tags": ["legacy", "orphan"],
            "file_path": None,
            "file_path_relative": None,
            "keywords": None,
            "is_permanent": True,
            "pinned": False,
            "archived": True,
            "outcome": None,
            "worked": None,
            "recall_count": 0,
            "surprise_score": None,
            "importance_score": None,
            "source_client": None,
            "source_model": None,
            "deleted_at_us": None,
        }
        event = store.append_and_project(
            EventCommand(
                workspace_id=workspace_id,
                stream_id=record_id,
                stream_kind="memory",
                event_type="legacy.placeholder_created",
                occurred_at_us=occurred_at_us,
                recorded_at_us=occurred_at_us,
                actor_type="migration",
                correlation_id=run_id,
                expected_stream_version=1,
                payload={
                    "legacy": {"table": "memories", "id": legacy_text, "missing": True},
                    "record": state,
                },
            )
        )
        connection.execute(
            """
            INSERT INTO legacy_id_map (
                migration_run_id, source_table, legacy_id, workspace_id,
                target_kind, target_id, source_row_hash, imported_event_id
            ) VALUES (?, 'memory_relationships.orphan', ?, ?, 'placeholder', ?, ?, ?)
            """,
            (
                run_id,
                f"memories:{legacy_text}",
                workspace_id,
                record_id,
                sha256_json({"table": "memories", "id": legacy_text, "missing": True}),
                event.event_id,
            ),
        )
        return record_id

    def _import_fact_row(
        self,
        connection: sqlite3.Connection,
        store: EventStore,
        row: sqlite3.Row,
        run_id: str,
        workspace_id: str,
    ) -> None:
        legacy_id = str(row["id"])
        fact_id = deterministic_id(
            "fact", "fact", workspace_id, "legacy", "facts", legacy_id
        )
        subject = None
        source_memory_id = _column(row, "source_memory_id")
        if source_memory_id is not None:
            mapped = connection.execute(
                "SELECT target_id FROM legacy_id_map WHERE migration_run_id=? "
                "AND source_table='memories' AND legacy_id=?",
                (run_id, str(source_memory_id)),
            ).fetchone()
            subject = str(mapped[0]) if mapped is not None else None
        category = _column(row, "category")
        normalized = (
            re.sub(r"[^a-z0-9]+", ".", str(category).lower()).strip(".")
            if category
            else ""
        )
        predicate = f"legacy.fact.{normalized}" if normalized else "legacy.fact"
        valid_from, quality, original = _parse_legacy_time(_column(row, "created_at"))
        tags, tags_quality = _parse_legacy_json(_column(row, "tags"), list, [])
        event = store.append_and_project(
            EventCommand(
                workspace_id=workspace_id,
                stream_id=fact_id,
                stream_kind="fact",
                event_type="fact.asserted",
                occurred_at_us=valid_from,
                recorded_at_us=valid_from,
                actor_type="migration",
                correlation_id=run_id,
                expected_stream_version=1,
                payload={
                    "legacy": _lossless_row(row, "facts"),
                    "fact": {
                        "subject_record_id": subject,
                        "predicate": predicate[:120],
                        "object_kind": "text",
                        "object": str(_column(row, "content", "")),
                        "legacy_type": None,
                        "confidence": 1.0,
                        "verification_count": max(
                            0, int(_column(row, "verification_count", 0) or 0)
                        ),
                        "is_verified": _legacy_bool(_column(row, "is_verified")),
                        "evidence": [],
                        "metadata": {
                            "legacy_tags": tags,
                            "tags_quality": tags_quality,
                            "raw_content_hash": _column(row, "content_hash"),
                            "time_original": original,
                            "time_quality": quality,
                        },
                        "valid_from_us": valid_from,
                        "valid_to_us": None,
                    },
                },
            )
        )
        connection.execute(
            """
            INSERT INTO legacy_id_map (
                migration_run_id, source_table, legacy_id, workspace_id,
                target_kind, target_id, source_row_hash, imported_event_id
            ) VALUES (?, 'facts', ?, ?, 'fact', ?, ?, ?)
            """,
            (
                run_id,
                legacy_id,
                workspace_id,
                fact_id,
                _source_row_hash(row, "facts"),
                event.event_id,
            ),
        )

    def _import_relationship_row(
        self,
        connection: sqlite3.Connection,
        store: EventStore,
        row: sqlite3.Row,
        run_id: str,
        workspace_id: str,
    ) -> None:
        occurred, quality, original = _parse_legacy_time(_column(row, "created_at"))
        source = self._mapped_memory(
            connection, store, _column(row, "source_id"), run_id, workspace_id, occurred
        )
        target = self._mapped_memory(
            connection, store, _column(row, "target_id"), run_id, workspace_id, occurred
        )
        legacy_id = str(row["id"])
        relationship_id = deterministic_id(
            "rel",
            "relationship",
            workspace_id,
            "legacy",
            "memory_relationships",
            legacy_id,
        )
        legacy_type_value = _column(row, "relationship")
        if legacy_type_value in _KNOWN_RELATIONSHIPS:
            relation_type = str(legacy_type_value)
            legacy_type = None
        else:
            relation_type = "legacy"
            legacy_type = (
                "<null>" if legacy_type_value is None else str(legacy_type_value)
            )
        confidence = _column(row, "confidence", 1.0)
        confidence = _legacy_score(confidence)
        if confidence is None:
            confidence = 1.0
        event = store.append_and_project(
            EventCommand(
                workspace_id=workspace_id,
                stream_id=relationship_id,
                stream_kind="relationship",
                event_type="relationship.created",
                occurred_at_us=occurred,
                recorded_at_us=occurred,
                actor_type="migration",
                correlation_id=run_id,
                expected_stream_version=1,
                payload={
                    "legacy": _lossless_row(row, "memory_relationships"),
                    "relationship": {
                        "source_record_id": source,
                        "target_record_id": target,
                        "relationship_type": relation_type,
                        "legacy_type": legacy_type,
                        "description": _column(row, "description"),
                        "confidence": confidence,
                        "metadata": {
                            "time_original": original,
                            "time_quality": quality,
                        },
                        "valid_from_us": occurred,
                        "valid_to_us": None,
                    },
                },
            )
        )
        connection.execute(
            """
            INSERT INTO legacy_id_map (
                migration_run_id, source_table, legacy_id, workspace_id,
                target_kind, target_id, source_row_hash, imported_event_id
            ) VALUES (?, 'memory_relationships', ?, ?, 'relationship', ?, ?, ?)
            """,
            (
                run_id,
                legacy_id,
                workspace_id,
                relationship_id,
                _source_row_hash(row, "memory_relationships"),
                event.event_id,
            ),
        )

    def _finalize_candidate(
        self,
        candidate: Path,
        run_id: str,
        workspace_id: str,
        source_inventory: dict[str, Any],
        *,
        retained_authority: bool = False,
    ) -> dict[str, Any]:
        connection = sqlite3.connect(candidate)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            now = self._clock_us()
            connection.execute(
                "UPDATE v7_migration_runs SET status='validating', updated_at_us=? WHERE migration_run_id=?",
                (now, run_id),
            )
            quick = [
                str(row[0]) for row in connection.execute("PRAGMA integrity_check")
            ]
            foreign = [
                list(row) for row in connection.execute("PRAGMA foreign_key_check")
            ]
            if quick != ["ok"] or foreign:
                raise MigrationV7Error(
                    "VALIDATION_FAILED", "candidate integrity check failed"
                )

            # Every copied source table remains logically exact. The v7
            # destinations are excluded because a pointerless format-6 database
            # may already contain additive migration 16 and those tables are the
            # intended import targets. Schema version retains every source row
            # but gains versions 16/17, so it is compared separately below.
            for table, expected_hash in source_inventory["table_hashes"].items():
                if retained_authority:
                    break
                if table in _V7_TABLE_NAMES or table == "schema_version":
                    continue
                if not _table_exists(connection, table):
                    raise MigrationV7Error(
                        "VALIDATION_FAILED", f"source table is missing: {table}"
                    )
                if _count(connection, table) != int(
                    source_inventory["tables"].get(table, -1)
                ):
                    raise MigrationV7Error(
                        "VALIDATION_FAILED", f"source table row count changed: {table}"
                    )
                digest = hashlib.sha256()
                for source_row in _table_logical_rows(connection, table):
                    encoded = canonical_json_bytes(source_row)
                    digest.update(len(encoded).to_bytes(8, "big"))
                    digest.update(encoded)
                if digest.hexdigest() != expected_hash:
                    raise MigrationV7Error(
                        "VALIDATION_FAILED", f"source table changed: {table}"
                    )

            snapshot_path = candidate.parent / "source.snapshot.db"
            snapshot_connection = _readonly_connection(snapshot_path)
            try:
                if _table_exists(snapshot_connection, "schema_version"):
                    source_versions = snapshot_connection.execute(
                        "SELECT * FROM schema_version ORDER BY version"
                    ).fetchall()
                    candidate_versions = [
                        connection.execute(
                            "SELECT * FROM schema_version WHERE version=?",
                            (row[0],),
                        ).fetchone()
                        for row in source_versions
                    ]
                    if any(
                        candidate_row is None
                        or tuple(candidate_row) != tuple(source_row)
                        for source_row, candidate_row in zip(
                            source_versions, candidate_versions, strict=True
                        )
                    ):
                        raise MigrationV7Error(
                            "VALIDATION_FAILED", "source schema history changed"
                        )
            finally:
                snapshot_connection.close()

            # A mapping count alone cannot prove lossless provenance. Recompute
            # every source row's canonical hash and compare the exact mapping.
            expected_kinds = {
                "memories": "memory",
                "facts": "fact",
                "memory_relationships": "relationship",
            }
            for table, target_kind in expected_kinds.items():
                if retained_authority:
                    break
                if not _table_exists(connection, table):
                    continue
                for source_row in connection.execute(
                    f"SELECT * FROM {_quoted_identifier(table)} ORDER BY id"
                ):
                    mapping = connection.execute(
                        "SELECT target_kind, target_id, source_row_hash, imported_event_id "
                        "FROM legacy_id_map WHERE migration_run_id=? "
                        "AND source_table=? AND legacy_id=?",
                        (run_id, table, str(source_row["id"])),
                    ).fetchone()
                    if (
                        mapping is None
                        or mapping[0] != target_kind
                        or mapping[2] != _source_row_hash(source_row, table)
                        or not isinstance(mapping[3], str)
                    ):
                        raise MigrationV7Error(
                            "VALIDATION_FAILED", f"mapping provenance mismatch: {table}"
                        )
                    imported = connection.execute(
                        "SELECT stream_id,stream_kind,event_type,payload_json "
                        "FROM memory_events WHERE event_id=?",
                        (mapping[3],),
                    ).fetchone()
                    try:
                        imported_payload = (
                            json.loads(imported[3]) if imported is not None else {}
                        )
                    except (TypeError, ValueError, RecursionError) as exc:
                        raise MigrationV7Error(
                            "VALIDATION_FAILED", f"mapping event is invalid: {table}"
                        ) from exc
                    if (
                        imported is None
                        or imported[0] != mapping[1]
                        or imported[1] != target_kind
                        or sha256_json(imported_payload.get("legacy")) != mapping[2]
                    ):
                        raise MigrationV7Error(
                            "VALIDATION_FAILED", f"mapping event mismatch: {table}"
                        )
                    if table == "memories" and (
                        imported[2] != "legacy.memory_state_imported"
                        or canonical_json_bytes(imported_payload.get("record"))
                        != canonical_json_bytes(_memory_state(source_row))
                    ):
                        raise MigrationV7Error(
                            "VALIDATION_FAILED",
                            "final memory state does not round-trip",
                        )

            heads: dict[tuple[str, str], tuple[int, str]] = {}
            legacy_version_claims: dict[tuple[str, str], int] = {}
            required_fact_versions: set[tuple[str, str, str, int, str]] = set()
            required_relationship_versions: set[tuple[str, str, str, int, str]] = set()
            event_count = 0
            for row in connection.execute(
                "SELECT * FROM memory_events ORDER BY workspace_id, stream_id, stream_version"
            ):
                event_count += 1
                try:
                    payload = json.loads(row["payload_json"])
                except (TypeError, ValueError) as exc:
                    raise MigrationV7Error(
                        "VALIDATION_FAILED", "event payload is invalid"
                    ) from exc
                if canonical_json_bytes(payload).decode("utf-8") != row["payload_json"]:
                    raise MigrationV7Error(
                        "VALIDATION_FAILED", "event payload is noncanonical"
                    )
                if sha256_json(payload) != row["payload_hash"]:
                    raise MigrationV7Error(
                        "VALIDATION_FAILED", "event payload hash mismatch"
                    )
                columns = {
                    "actor_id": row["actor_id"],
                    "actor_type": row["actor_type"],
                    "causation_event_id": row["causation_event_id"],
                    "correlation_id": row["correlation_id"],
                    "event_schema_version": row["event_schema_version"],
                    "event_type": row["event_type"],
                    "occurred_at_us": row["occurred_at_us"],
                    "payload_hash": row["payload_hash"],
                    "previous_event_hash": row["previous_event_hash"],
                    "recorded_at_us": row["recorded_at_us"],
                    "stream_id": row["stream_id"],
                    "stream_kind": row["stream_kind"],
                    "stream_version": row["stream_version"],
                    "workspace_id": row["workspace_id"],
                }
                if (
                    event_hash_for(columns) != row["event_hash"]
                    or row["event_id"] != "evt_" + row["event_hash"]
                ):
                    raise MigrationV7Error(
                        "VALIDATION_FAILED", "event envelope hash mismatch"
                    )
                key = (str(row["workspace_id"]), str(row["stream_id"]))
                prior = heads.get(key)
                if prior is None:
                    if (
                        row["stream_version"] != 1
                        or row["previous_event_hash"] is not None
                    ):
                        raise MigrationV7Error(
                            "VALIDATION_FAILED", "event stream does not start at one"
                        )
                elif (
                    row["stream_version"] != prior[0] + 1
                    or row["previous_event_hash"] != prior[1]
                ):
                    raise MigrationV7Error(
                        "VALIDATION_FAILED", "event stream chain is discontinuous"
                    )
                heads[key] = (int(row["stream_version"]), str(row["event_hash"]))
                if row["stream_kind"] == "fact":
                    required_fact_versions.add(
                        (
                            deterministic_id(
                                "fact",
                                "fact-version",
                                str(row["stream_id"]),
                                int(row["stream_version"]),
                            ),
                            str(row["workspace_id"]),
                            str(row["stream_id"]),
                            int(row["stream_version"]),
                            str(row["event_id"]),
                        )
                    )
                elif row["stream_kind"] == "relationship":
                    required_relationship_versions.add(
                        (
                            deterministic_id(
                                "rel",
                                "relationship-version",
                                str(row["stream_id"]),
                                int(row["stream_version"]),
                            ),
                            str(row["workspace_id"]),
                            str(row["stream_id"]),
                            int(row["stream_version"]),
                            str(row["event_id"]),
                        )
                    )
                legacy = payload.get("legacy")
                if (
                    isinstance(legacy, dict)
                    and legacy.get("table") == "memory_versions"
                ):
                    claim = (sha256_json(legacy), str(row["stream_id"]))
                    legacy_version_claims[claim] = (
                        legacy_version_claims.get(claim, 0) + 1
                    )

            actual_fact_versions = {
                (str(row[0]), str(row[1]), str(row[2]), int(row[3]), str(row[4]))
                for row in connection.execute(
                    "SELECT fact_version_id,workspace_id,fact_id,version,"
                    "asserted_by_event_id FROM memory_fact_versions"
                )
            }
            if actual_fact_versions != required_fact_versions:
                raise MigrationV7Error(
                    "VALIDATION_FAILED",
                    "fact projection version keys do not match events",
                )
            actual_relationship_versions = {
                (str(row[0]), str(row[1]), str(row[2]), int(row[3]), str(row[4]))
                for row in connection.execute(
                    "SELECT relationship_version_id,workspace_id,relationship_id,"
                    "version,asserted_by_event_id FROM memory_relationship_versions"
                )
            }
            if actual_relationship_versions != required_relationship_versions:
                raise MigrationV7Error(
                    "VALIDATION_FAILED",
                    "relationship projection version keys do not match events",
                )

            if not retained_authority and _table_exists(connection, "memory_versions"):
                for version in connection.execute(
                    "SELECT * FROM memory_versions ORDER BY id"
                ):
                    mapped = connection.execute(
                        "SELECT target_id FROM legacy_id_map "
                        "WHERE migration_run_id=? AND source_table='memories' "
                        "AND legacy_id=?",
                        (run_id, str(version["memory_id"])),
                    ).fetchone()
                    claim = (
                        _source_row_hash(version, "memory_versions"),
                        str(mapped[0]) if mapped is not None else "",
                    )
                    if legacy_version_claims.get(claim) != 1:
                        raise MigrationV7Error(
                            "VALIDATION_FAILED",
                            "legacy memory version is not represented exactly once",
                        )

            # Independent record replay: the last memory payload must hash to the
            # current typed projection without trusting projector implementation.
            for record in connection.execute("SELECT * FROM memory_records"):
                event = connection.execute(
                    "SELECT * FROM memory_events WHERE event_id=?",
                    (record["source_event_id"],),
                ).fetchone()
                payload = json.loads(event["payload_json"]) if event is not None else {}
                projected = payload.get("record")
                first = connection.execute(
                    "SELECT occurred_at_us FROM memory_events "
                    "WHERE workspace_id=? AND stream_id=? AND stream_version=1",
                    (record["workspace_id"], record["record_id"]),
                ).fetchone()
                if not isinstance(projected, dict) or event is None or first is None:
                    raise MigrationV7Error(
                        "VALIDATION_FAILED", "memory projection source is missing"
                    )
                projection_values = (
                    record["record_id"] == event["stream_id"],
                    record["workspace_id"] == event["workspace_id"],
                    event["stream_kind"] == "memory",
                    record["record_type"] == projected.get("record_type"),
                    record["legacy_type"] == projected.get("legacy_type"),
                    record["content"] == projected.get("content"),
                    record["content_hash"] == memory_content_hash(projected),
                    record["rationale"] == projected.get("rationale"),
                    record["context_json"]
                    == canonical_json_bytes(projected.get("context", {})).decode(
                        "utf-8"
                    ),
                    record["tags_json"]
                    == canonical_json_bytes(projected.get("tags", [])).decode("utf-8"),
                    record["file_path"] == projected.get("file_path"),
                    record["file_path_relative"] == projected.get("file_path_relative"),
                    record["keywords"] == projected.get("keywords"),
                    record["is_permanent"] == int(projected.get("is_permanent", False)),
                    record["pinned"] == int(projected.get("pinned", False)),
                    record["archived"] == int(projected.get("archived", False)),
                    record["outcome"] == projected.get("outcome"),
                    record["worked"]
                    == (
                        None
                        if (projected_worked := projected.get("worked")) is None
                        else int(projected_worked)
                    ),
                    record["recall_count"] == projected.get("recall_count", 0),
                    record["surprise_score"] == projected.get("surprise_score"),
                    record["importance_score"] == projected.get("importance_score"),
                    record["source_client"] == projected.get("source_client"),
                    record["source_model"] == projected.get("source_model"),
                    record["stream_version"] == event["stream_version"],
                    record["source_event_id"] == event["event_id"],
                    record["created_at_us"] == first[0],
                    record["updated_at_us"] == event["recorded_at_us"],
                    record["deleted_at_us"] == projected.get("deleted_at_us"),
                    record["state_hash"] == memory_state_hash(projected),
                )
                if not all(projection_values):
                    raise MigrationV7Error(
                        "VALIDATION_FAILED", "memory projection replay mismatch"
                    )

            # Independently replay typed fact assertions from their immutable
            # event payloads. This deliberately does not trust EventStore's
            # projection row or its stored content hash.
            for projected in connection.execute(
                "SELECT * FROM memory_fact_versions ORDER BY fact_id, version"
            ):
                event = connection.execute(
                    "SELECT * FROM memory_events WHERE event_id=?",
                    (projected["asserted_by_event_id"],),
                ).fetchone()
                payload = json.loads(event["payload_json"]) if event is not None else {}
                fact = payload.get("fact")
                if not isinstance(fact, dict):
                    raise MigrationV7Error(
                        "VALIDATION_FAILED", "fact projection source is missing"
                    )
                expected_hash = sha256_json(
                    {
                        "subject_record_id": fact.get("subject_record_id"),
                        "predicate": fact.get("predicate"),
                        "object_kind": fact.get("object_kind"),
                        "object": fact.get("object"),
                        "legacy_type": fact.get("legacy_type"),
                        "confidence": fact.get("confidence", 1.0),
                        "verification_count": fact.get("verification_count", 0),
                        "is_verified": fact.get("is_verified", False),
                        "evidence": fact.get("evidence", []),
                        "metadata": fact.get("metadata", {}),
                        "valid_from_us": fact.get("valid_from_us"),
                        "valid_to_us": fact.get("valid_to_us"),
                    }
                )
                expected_version_id = deterministic_id(
                    "fact",
                    "fact-version",
                    projected["fact_id"],
                    projected["version"],
                )
                successor = connection.execute(
                    "SELECT recorded_at_us,event_id FROM memory_events "
                    "WHERE workspace_id=? AND stream_id=? AND stream_version=?",
                    (
                        projected["workspace_id"],
                        projected["fact_id"],
                        int(projected["version"]) + 1,
                    ),
                ).fetchone()
                expected_transaction_to = (
                    successor[0] if successor is not None else None
                )
                expected_retraction = (
                    successor[1]
                    if successor is not None
                    else (
                        event["event_id"]
                        if event["event_type"] == "fact.retracted"
                        or fact.get("valid_to_us") is not None
                        else None
                    )
                )
                fact_values = (
                    projected["fact_version_id"] == expected_version_id,
                    projected["workspace_id"] == event["workspace_id"],
                    projected["fact_id"] == event["stream_id"],
                    projected["version"] == event["stream_version"],
                    projected["subject_record_id"] == fact.get("subject_record_id"),
                    projected["predicate"] == fact.get("predicate"),
                    projected["object_kind"] == fact.get("object_kind"),
                    projected["object_json"]
                    == canonical_json_bytes(fact.get("object")).decode("utf-8"),
                    projected["legacy_type"] == fact.get("legacy_type"),
                    projected["content_hash"] == expected_hash,
                    projected["confidence"] == float(fact.get("confidence", 1.0)),
                    projected["verification_count"]
                    == int(fact.get("verification_count", 0)),
                    projected["is_verified"] == int(fact.get("is_verified", False)),
                    projected["evidence_json"]
                    == canonical_json_bytes(fact.get("evidence", [])).decode("utf-8"),
                    projected["metadata_json"]
                    == canonical_json_bytes(fact.get("metadata", {})).decode("utf-8"),
                    projected["valid_from_us"] == fact.get("valid_from_us"),
                    projected["valid_to_us"] == fact.get("valid_to_us"),
                    projected["transaction_from_us"] == event["recorded_at_us"],
                    projected["transaction_to_us"] == expected_transaction_to,
                    projected["retracted_by_event_id"] == expected_retraction,
                )
                if not all(fact_values):
                    raise MigrationV7Error(
                        "VALIDATION_FAILED", "fact projection replay mismatch"
                    )

            for projected in connection.execute(
                "SELECT * FROM memory_relationship_versions "
                "ORDER BY relationship_id, version"
            ):
                event = connection.execute(
                    "SELECT * FROM memory_events WHERE event_id=?",
                    (projected["asserted_by_event_id"],),
                ).fetchone()
                payload = json.loads(event["payload_json"]) if event is not None else {}
                relation = payload.get("relationship")
                if not isinstance(relation, dict):
                    raise MigrationV7Error(
                        "VALIDATION_FAILED", "relationship projection source is missing"
                    )
                expected_hash = sha256_json(
                    {
                        "source_record_id": relation.get("source_record_id"),
                        "target_record_id": relation.get("target_record_id"),
                        "relationship_type": relation.get("relationship_type"),
                        "legacy_type": relation.get("legacy_type"),
                        "description": relation.get("description"),
                        "confidence": relation.get("confidence", 1.0),
                        "metadata": relation.get("metadata", {}),
                        "valid_from_us": relation.get("valid_from_us"),
                        "valid_to_us": relation.get("valid_to_us"),
                    }
                )
                expected_version_id = deterministic_id(
                    "rel",
                    "relationship-version",
                    projected["relationship_id"],
                    projected["version"],
                )
                successor = connection.execute(
                    "SELECT recorded_at_us,event_id FROM memory_events "
                    "WHERE workspace_id=? AND stream_id=? AND stream_version=?",
                    (
                        projected["workspace_id"],
                        projected["relationship_id"],
                        int(projected["version"]) + 1,
                    ),
                ).fetchone()
                expected_transaction_to = (
                    successor[0] if successor is not None else None
                )
                expected_retraction = (
                    successor[1]
                    if successor is not None
                    else (
                        event["event_id"]
                        if event["event_type"] == "relationship.removed"
                        or relation.get("valid_to_us") is not None
                        else None
                    )
                )
                relation_values = (
                    projected["relationship_version_id"] == expected_version_id,
                    projected["workspace_id"] == event["workspace_id"],
                    projected["relationship_id"] == event["stream_id"],
                    projected["version"] == event["stream_version"],
                    projected["source_record_id"] == relation.get("source_record_id"),
                    projected["target_record_id"] == relation.get("target_record_id"),
                    projected["relationship_type"] == relation.get("relationship_type"),
                    projected["legacy_type"] == relation.get("legacy_type"),
                    projected["description"] == relation.get("description"),
                    projected["confidence"] == float(relation.get("confidence", 1.0)),
                    projected["metadata_json"]
                    == canonical_json_bytes(relation.get("metadata", {})).decode(
                        "utf-8"
                    ),
                    projected["content_hash"] == expected_hash,
                    projected["valid_from_us"] == relation.get("valid_from_us"),
                    projected["valid_to_us"] == relation.get("valid_to_us"),
                    projected["transaction_from_us"] == event["recorded_at_us"],
                    projected["transaction_to_us"] == expected_transaction_to,
                    projected["retracted_by_event_id"] == expected_retraction,
                )
                if not all(relation_values):
                    raise MigrationV7Error(
                        "VALIDATION_FAILED", "relationship projection replay mismatch"
                    )

            if retained_authority:
                _validate_retained_compatibility(connection, workspace_id)

            expected_maps = {
                "memories": int(source_inventory["memory_count"]),
                "facts": int(source_inventory["fact_count"]),
                "memory_relationships": int(source_inventory["relationship_count"]),
            }
            for table, expected in expected_maps.items():
                if retained_authority:
                    break
                actual = int(
                    connection.execute(
                        "SELECT count(*) FROM legacy_id_map WHERE migration_run_id=? AND source_table=?",
                        (run_id, table),
                    ).fetchone()[0]
                )
                if actual != expected:
                    raise MigrationV7Error(
                        "VALIDATION_FAILED", f"mapping count mismatch: {table}"
                    )

            root_hash = _event_root(connection)
            cursor = connection.execute(
                "SELECT recorded_at_us, event_id FROM memory_events ORDER BY recorded_at_us DESC, event_id DESC LIMIT 1"
            ).fetchone()
            manifests = (
                ("memory_records", "ready", _count(connection, "memory_records")),
                (
                    "memory_fact_versions",
                    "ready",
                    _count(connection, "memory_fact_versions"),
                ),
                (
                    "memory_relationship_versions",
                    "ready",
                    _count(connection, "memory_relationship_versions"),
                ),
                ("dense", "rebuild_required", 0),
                ("graph", "rebuild_required", 0),
                ("temporal", "rebuild_required", 0),
                ("procedure", "rebuild_required", 0),
                ("outcome", "rebuild_required", 0),
                ("communities", "rebuild_required", 0),
                ("entities", "rebuild_required", 0),
            )
            for projection_name, status, row_count in manifests:
                manifest_id = deterministic_id(
                    "prj", "projection", workspace_id, projection_name, 1, root_hash
                )
                connection.execute(
                    """
                    INSERT INTO projection_manifests (
                        manifest_id, workspace_id, projection_name, generation,
                        projection_version, status, source_event_count,
                        source_event_root_hash, cursor_recorded_at_us,
                        cursor_event_id, row_count, builder_version, details_json,
                        started_at_us, completed_at_us
                    ) VALUES (?, ?, ?, 1, 1, ?, ?, ?, ?, ?, ?, 'v7-migration-1', '{}', ?, ?)
                    ON CONFLICT(workspace_id, projection_name, generation) DO UPDATE SET
                        manifest_id=excluded.manifest_id,
                        projection_version=excluded.projection_version,
                        status=excluded.status,
                        source_event_count=excluded.source_event_count,
                        source_event_root_hash=excluded.source_event_root_hash,
                        cursor_recorded_at_us=excluded.cursor_recorded_at_us,
                        cursor_event_id=excluded.cursor_event_id,
                        row_count=excluded.row_count,
                        builder_version=excluded.builder_version,
                        details_json=excluded.details_json,
                        completed_at_us=excluded.completed_at_us
                    """,
                    (
                        manifest_id,
                        workspace_id,
                        projection_name,
                        status,
                        event_count,
                        root_hash,
                        cursor[0] if cursor is not None else None,
                        cursor[1] if cursor is not None else None,
                        row_count,
                        now,
                        now,
                    ),
                )
            validation = {
                "integrity_check": "ok",
                "foreign_key_violations": [],
                "event_count": event_count,
                "event_root_hash": root_hash,
                "memory_record_count": _count(connection, "memory_records"),
                "fact_version_count": _count(connection, "memory_fact_versions"),
                "relationship_version_count": _count(
                    connection, "memory_relationship_versions"
                ),
            }
            if retained_authority:
                validation["authority"] = "retained_v7"
            connection.execute(
                """
                UPDATE v7_migration_runs
                SET status='ready', validation_json=?, updated_at_us=?, validated_at_us=?
                WHERE migration_run_id=?
                """,
                (canonical_json_bytes(validation).decode("utf-8"), now, now, run_id),
            )
            connection.commit()
            return validation
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _mark_failed(self, candidate: Path, run_id: str, exc: Exception) -> None:
        if not candidate.is_file():
            return
        try:
            connection = sqlite3.connect(candidate)
            now = self._clock_us()
            error = {
                "code": getattr(exc, "code", type(exc).__name__),
                "message": str(exc)[:1000],
            }
            connection.execute(
                "UPDATE v7_migration_runs SET status='failed', last_error_json=?, updated_at_us=? WHERE migration_run_id=?",
                (canonical_json_bytes(error).decode("utf-8"), now, run_id),
            )
            connection.commit()
            connection.close()
        except Exception:
            return

    def _restore_pointer(self, storage: Path, previous) -> None:
        pointer_path = storage / "active-db.json"
        if previous.pointer is None:
            if pointer_path.is_file() and not pointer_path.is_symlink():
                pointer_path.unlink()
            return
        write_active_pointer(storage, previous.pointer)

    def rollback(
        self,
        selector: str | os.PathLike[str] | None = None,
        migration_run_id: str | None = "latest",
    ) -> MigrationResult:
        workspace, storage = self._storage(selector)
        with DatabaseFileLock(storage, "exclusive"):
            resolved = resolve_active_database(storage)
            requested = (
                None if migration_run_id in (None, "latest") else migration_run_id
            )
            if resolved.format_version == TARGET_FORMAT_VERSION:
                from .v7_schema_upgrade import V7SchemaUpgradeService

                upgrade_service = V7SchemaUpgradeService(
                    fault_injector=self._fault_injector,
                    clock_us=self._clock_us,
                )
                recovered_rollback = upgrade_service.prepare_rollback(
                    storage, workspace.workspace_id, resolved
                )
                if recovered_rollback is not None:
                    if (
                        requested is None
                        or requested == recovered_rollback.migration_run_id
                    ):
                        return recovered_rollback
                    raise MigrationV7Error(
                        "ROLLBACK_NOT_ACTIVE", "migration run is not active"
                    )
            if resolved.format_version != TARGET_FORMAT_VERSION:
                run_id = resolved.migration_run_id
                expected_candidate = (
                    f"migrations/v7/{run_id}/candidate.db"
                    if run_id is not None
                    else None
                )
                if (
                    resolved.format_version == 6
                    and run_id is not None
                    and resolved.previous_db == expected_candidate
                    and (requested is None or requested == run_id)
                ):
                    candidate = storage.joinpath(
                        "migrations", "v7", run_id, "candidate.db"
                    )
                    status, source_format, inventory, _validation = (
                        self._load_published_run(
                            candidate,
                            run_id,
                            workspace.workspace_id,
                            error_code="ROLLBACK_STATE_INVALID",
                        )
                    )
                    if status == "active":
                        self._mark_candidate_rolled_back(candidate, run_id)
                        return MigrationResult(
                            status="rolled_back",
                            action="rollback",
                            workspace_id=workspace.workspace_id,
                            source_format=source_format,
                            migration_run_id=run_id,
                            active_generation=resolved.generation,
                            inventory=inventory,
                        )
                if requested is None or requested == resolved.migration_run_id:
                    return MigrationResult(
                        status="already_rolled_back",
                        action="already_rolled_back",
                        workspace_id=workspace.workspace_id,
                        source_format=resolved.format_version,
                        migration_run_id=resolved.migration_run_id,
                        active_generation=resolved.generation,
                    )
                raise MigrationV7Error(
                    "ROLLBACK_NOT_ACTIVE", "migration run is not active"
                )
            self._validate_active_v7(resolved, workspace.workspace_id)
            run_id = resolved.migration_run_id
            if run_id is None or (requested is not None and requested != run_id):
                raise MigrationV7Error(
                    "ROLLBACK_NOT_ACTIVE", "migration run is not active"
                )
            if resolved.previous_db is None:
                raise MigrationV7Error(
                    "ROLLBACK_UNAVAILABLE", "active database has no predecessor"
                )
            current_relative = resolved.relative_path
            _status, source_format, _inventory, _validation = self._load_published_run(
                resolved.path,
                run_id,
                workspace.workspace_id,
                error_code="ROLLBACK_STATE_INVALID",
            )
            pointer = ActiveDatabasePointer(
                format_version=source_format,
                generation=resolved.generation + 1,
                active_db=resolved.previous_db,
                previous_db=current_relative,
                migration_run_id=run_id,
            )
            write_active_pointer(storage, pointer)
            self._fault("after_rollback_pointer", migration_run_id=run_id)
            self._mark_candidate_rolled_back(resolved.path, run_id)
            return MigrationResult(
                status="rolled_back",
                action="rollback",
                workspace_id=workspace.workspace_id,
                source_format=TARGET_FORMAT_VERSION,
                migration_run_id=run_id,
                active_generation=pointer.generation,
            )


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "MigrationResult",
    "MigrationInterrupted",
    "MigrationV7Error",
    "MigrationV7Service",
    "TARGET_FORMAT_VERSION",
    "inventory_database",
]
