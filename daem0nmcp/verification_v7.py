"""Whole-store format-7 verification and offline projection recovery.

The immutable event ledgers are always validated before derived state is
considered repairable.  Recovery works on SQLite backup files, replays events
in a separate database, and changes the activation pointer only after the
candidate passes the same verifier.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal

from .discovery_projection import verify_code_projection
from .event_store import (
    GovernanceEventCommand,
    GovernanceEventStore,
    build_live_compatibility_claim_index,
    canonical_json_bytes,
    deterministic_id,
    export_event_bundle,
    import_event_bundle,
    parse_canonical_json,
    sha256_json,
)
from .migrations import MIGRATIONS
from .migrations.v7 import _source_row_hash, inventory_database
from .retrieval.lexical_config import GENERATION_TABLES
from .schema_version import CURRENT_SCHEMA_VERSION, REQUIRED_V7_SCHEMA_VERSIONS
from .storage_activation import (
    ActiveDatabasePointer,
    DatabaseFileLock,
    resolve_active_database,
    write_active_pointer,
)

_DERIVED_TABLES = (
    "memory_records",
    "memory_fact_versions",
    "memory_relationship_versions",
    "governance_rules",
    "governance_context_triggers",
    "active_context_entries",
)
_REQUIRED_TABLES = frozenset(
    {
        "active_context_entries",
        "governance_context_triggers",
        "governance_events",
        "governance_rules",
        "discovery_code_edges",
        "discovery_code_entities",
        "discovery_projection_partitions",
        "dreaming_strategy_state",
        "legacy_id_map",
        "memory_events",
        "memory_fact_versions",
        "memory_records",
        "memory_relationship_versions",
        "projection_manifests",
        "public_object_ids",
        "schema_version",
        "session_update_sequence",
        "v7_migration_runs",
        "workspace_link_events",
    }
)
_LOCAL_PROJECTIONS = frozenset(
    {
        "memory_records",
        "memory_fact_versions",
        "memory_relationship_versions",
        "lexical",
        "graph",
        "temporal",
        "procedure",
        "outcome",
    }
)
_SUPPORTED_PROJECTIONS = _LOCAL_PROJECTIONS | {
    "dense",
    "code",
    "entities",
    "communities",
}


class VerificationV7Error(RuntimeError):
    """Fail-closed operator error for verification or recovery."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(code if not detail else f"{code}: {detail}")


def _check(ok: bool, **details: Any) -> dict[str, Any]:
    return {"ok": bool(ok), **details}


def _database_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
        destination_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        destination_connection.close()
        source_connection.close()
    with destination.open("r+b") as handle:
        os.fsync(handle.fileno())


def _is_link_or_reparse(path: Path) -> bool:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return False
    attributes = getattr(details, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(details.st_mode) or bool(attributes & reparse_flag)


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        return False
    return True


def _safe_recovery_root(storage: Path) -> Path:
    if _is_link_or_reparse(storage) or not storage.is_dir():
        raise VerificationV7Error("UNSAFE_RECOVERY_PATH", "storage")
    root = storage.resolve(strict=True)
    current = storage
    for name in ("migrations", "v7"):
        current = current / name
        if current.exists():
            if _is_link_or_reparse(current) or not current.is_dir():
                raise VerificationV7Error("UNSAFE_RECOVERY_PATH", name)
        else:
            current.mkdir(mode=0o700)
        if not _within(current, root):
            raise VerificationV7Error("UNSAFE_RECOVERY_PATH", name)
    return current


def _safe_child(parent: Path, name: str) -> Path:
    if "/" in name or "\\" in name or name in {"", ".", ".."}:
        raise VerificationV7Error("UNSAFE_RECOVERY_PATH", name)
    if _is_link_or_reparse(parent) or not parent.is_dir():
        raise VerificationV7Error("UNSAFE_RECOVERY_PATH", str(parent))
    child = parent / name
    if not _within(child, parent):
        raise VerificationV7Error("UNSAFE_RECOVERY_PATH", name)
    return child


def _copy_exclusive(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise VerificationV7Error("RECOVERY_FILE_EXISTS", destination.name)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(destination, flags, 0o600)
    try:
        with source.open("rb") as input_handle, os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            shutil.copyfileobj(input_handle, output, 1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _require_owned_regular(path: Path, parent: Path) -> None:
    if (
        _is_link_or_reparse(parent)
        or not parent.is_dir()
        or _is_link_or_reparse(path)
        or not path.is_file()
        or not _within(path, parent)
    ):
        raise VerificationV7Error("UNSAFE_RECOVERY_PATH", path.name)


def _publish_partial(partial: Path, final: Path, parent: Path) -> None:
    _require_owned_regular(partial, parent)
    if final.exists() or final.is_symlink():
        raise VerificationV7Error("RECOVERY_FILE_EXISTS", final.name)
    os.replace(partial, final)
    _require_owned_regular(final, parent)
    _fsync_directory(parent)


def _fault(
    injector: Callable[[str, dict[str, object]], None] | None,
    stage: str,
    run_id: str,
) -> None:
    if injector is not None:
        injector(stage, {"migration_run_id": run_id})


def _physical_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as handle:
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _table_digest(connection: sqlite3.Connection, table: str) -> tuple[int, str]:
    columns = [
        str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')
    ]
    quoted = ",".join(f'"{column}"' for column in columns)
    rows = [
        list(row)
        for row in connection.execute(
            f'SELECT {quoted} FROM "{table}" ORDER BY {quoted}'
        )
    ]
    return len(rows), sha256_json(rows)


def _event_snapshot(
    connection: sqlite3.Connection, workspace_id: str
) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    for row in connection.execute(
        "SELECT event_hash FROM memory_events WHERE workspace_id=? ORDER BY event_id",
        (workspace_id,),
    ):
        digest.update(bytes.fromhex(str(row[0])))
        count += 1
    return count, digest.hexdigest()


def _ledger_root(connection: sqlite3.Connection, table: str) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    for row in connection.execute(
        f'SELECT event_hash FROM "{table}" ORDER BY event_id'
    ):
        digest.update(bytes.fromhex(str(row[0])))
        count += 1
    return count, digest.hexdigest()


def _initialize_replay(path: Path, workspace_id: str) -> sqlite3.Connection:
    path.touch()
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(
        "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, "
        "applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    for version, _description, statements in MIGRATIONS:
        if version < min(REQUIRED_V7_SCHEMA_VERSIONS):
            continue
        for statement in statements:
            connection.execute(statement)
        connection.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))
    connection.commit()
    return connection


def _memory_replay(
    source: sqlite3.Connection, replay: sqlite3.Connection
) -> tuple[int, dict[str, tuple[int, str]]]:
    workspace_ids = sorted(
        str(row[0])
        for row in source.execute("SELECT DISTINCT workspace_id FROM memory_events")
    )
    roots: dict[str, tuple[int, str]] = {}
    total = 0
    for workspace_id in workspace_ids:
        bundle = export_event_bundle(source, workspace_id)
        result = import_event_bundle(
            replay, bundle, workspace_id, assume_transaction=True
        )
        total += result.events_imported + result.events_existing
        roots[workspace_id] = (len(bundle["events"]), str(bundle["root_hash"]))
    replay.commit()
    return total, roots


def _governance_rows(source: sqlite3.Connection) -> list[dict[str, Any]]:
    cursor = source.execute(
        "SELECT event_id,workspace_id,stream_id,stream_kind,stream_version,"
        "event_type,event_schema_version,occurred_at_us,recorded_at_us,actor_type,"
        "actor_id,causation_event_id,correlation_id,payload_json,payload_hash,"
        "previous_event_hash,event_hash FROM governance_events"
    )
    names = [str(item[0]) for item in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def _apply_active_context(
    replay: sqlite3.Connection, row: Mapping[str, Any], payload: Mapping[str, Any]
) -> None:
    required = {
        "active_context_id",
        "record_id",
        "priority",
        "reason",
        "added_at_us",
        "expires_at_us",
        "removed_at_us",
    }
    if set(payload) != required or payload["active_context_id"] != row["stream_id"]:
        raise ValueError("active-context event state is invalid")
    values = (
        payload["active_context_id"],
        row["workspace_id"],
        payload["record_id"],
        payload["priority"],
        payload["reason"],
        payload["added_at_us"],
        payload["expires_at_us"],
        payload["removed_at_us"],
    )
    existing = replay.execute(
        "SELECT 1 FROM active_context_entries WHERE active_context_id=?",
        (payload["active_context_id"],),
    ).fetchone()
    if existing is None:
        replay.execute(
            "INSERT INTO active_context_entries VALUES (?,?,?,?,?,?,?,?)", values
        )
    else:
        replay.execute(
            "UPDATE active_context_entries SET priority=?,reason=?,added_at_us=?,"
            "expires_at_us=?,removed_at_us=? WHERE active_context_id=? "
            "AND workspace_id=? AND record_id=?",
            (
                payload["priority"],
                payload["reason"],
                payload["added_at_us"],
                payload["expires_at_us"],
                payload["removed_at_us"],
                payload["active_context_id"],
                row["workspace_id"],
                payload["record_id"],
            ),
        )


def _governance_replay(source: sqlite3.Connection, replay: sqlite3.Connection) -> int:
    rows = _governance_rows(source)
    memory_ids = {
        str(row[0]) for row in source.execute("SELECT event_id FROM memory_events")
    }
    by_id = {str(row["event_id"]): row for row in rows}
    if len(by_id) != len(rows):
        raise ValueError("governance event identity is duplicated")
    dependencies: dict[str, set[str]] = {}
    hash_to_id = {str(row["event_hash"]): str(row["event_id"]) for row in rows}
    for row in rows:
        required: set[str] = set()
        previous = row["previous_event_hash"]
        if previous is not None:
            previous_id = hash_to_id.get(str(previous))
            if previous_id is None:
                raise ValueError("governance stream predecessor is missing")
            required.add(previous_id)
        cause = row["causation_event_id"]
        if cause is not None and cause not in memory_ids:
            if cause not in by_id:
                raise ValueError("governance causation event is missing")
            required.add(str(cause))
        dependencies[str(row["event_id"])] = required

    completed: set[str] = set()
    store = GovernanceEventStore(replay, assume_transaction=True)
    while len(completed) < len(rows):
        ready = sorted(
            event_id
            for event_id, required in dependencies.items()
            if event_id not in completed and required <= completed
        )
        if not ready:
            raise ValueError("governance dependencies are cyclic")
        for event_id in ready:
            row = by_id[event_id]
            payload = parse_canonical_json(str(row["payload_json"]))
            command = GovernanceEventCommand(
                workspace_id=str(row["workspace_id"]),
                stream_id=str(row["stream_id"]),
                stream_kind=str(row["stream_kind"]),
                event_type=str(row["event_type"]),
                occurred_at_us=int(row["occurred_at_us"]),
                recorded_at_us=int(row["recorded_at_us"]),
                actor_type=str(row["actor_type"]),
                actor_id=row["actor_id"],
                causation_event_id=row["causation_event_id"],
                correlation_id=row["correlation_id"],
                event_schema_version=int(row["event_schema_version"]),
                expected_stream_version=int(row["stream_version"]),
                payload=payload,
            )
            appended = store.append_and_project(command)
            if (
                appended.event_id != event_id
                or appended.event_hash != row["event_hash"]
                or appended.payload_hash != row["payload_hash"]
                or appended.previous_event_hash != row["previous_event_hash"]
            ):
                raise ValueError("governance event envelope differs")
            if row["stream_kind"] == "active_context":
                _apply_active_context(replay, row, payload)
            completed.add(event_id)
    replay.commit()
    return len(rows)


def _verify_federation(connection: sqlite3.Connection) -> int:
    count = 0
    previous_by_stream: dict[tuple[str, str], tuple[int, str, str]] = {}
    for row in connection.execute(
        "SELECT event_id,workspace_id,linked_workspace_id,stream_version,event_type,"
        "relationship,label,occurred_at_us,recorded_at_us,previous_event_hash,event_hash "
        "FROM workspace_link_events ORDER BY workspace_id,linked_workspace_id,stream_version"
    ):
        stream = (str(row[1]), str(row[2]))
        previous = previous_by_stream.get(stream)
        version = 1 if previous is None else previous[0] + 1
        previous_hash = None if previous is None else previous[1]
        previous_type = None if previous is None else previous[2]
        envelope = {
            "workspace_id": row[1],
            "linked_workspace_id": row[2],
            "stream_version": row[3],
            "event_type": row[4],
            "relationship": row[5],
            "label": row[6],
            "occurred_at_us": row[7],
            "recorded_at_us": row[8],
            "previous_event_hash": row[9],
        }
        event_hash = sha256_json(envelope)
        if (
            int(row[3]) != version
            or row[8] != row[7]
            or row[9] != previous_hash
            or row[10] != event_hash
            or row[0] != "evt_" + event_hash
            or (version == 1 and row[4] != "workspace.linked")
            or (
                previous_type == "workspace.unlinked" and row[4] == "workspace.unlinked"
            )
        ):
            raise ValueError("workspace-link event stream is invalid")
        previous_by_stream[stream] = (version, event_hash, str(row[4]))
        count += 1
    return count


def _verify_sequence(connection: sqlite3.Connection) -> int:
    expected = {
        (str(row[0]), str(row[1]), "memory")
        for row in connection.execute(
            "SELECT workspace_id,event_id FROM memory_events WHERE stream_kind='memory'"
        )
    }
    expected.update(
        (str(row[0]), str(row[1]), "governance")
        for row in connection.execute(
            "SELECT workspace_id,event_id FROM governance_events"
        )
    )
    actual = {
        (str(row[0]), str(row[1]), str(row[2]))
        for row in connection.execute(
            "SELECT workspace_id,event_id,event_source FROM session_update_sequence"
        )
    }
    if actual != expected:
        raise ValueError("session update sequence does not cover authority events")
    return len(actual)


def _verify_mappings(connection: sqlite3.Connection, replay: sqlite3.Connection) -> int:
    runs: dict[str, tuple[int, str, dict[str, Any]]] = {}
    for row in connection.execute(
        "SELECT migration_run_id,source_format_version,status,source_inventory_json,"
        "validation_json,last_error_json FROM v7_migration_runs"
    ):
        metadata: list[dict[str, Any] | None] = []
        for value in row[3:]:
            parsed = None if value is None else parse_canonical_json(str(value))
            if parsed is not None and not isinstance(parsed, dict):
                raise ValueError("migration run metadata is invalid")
            metadata.append(parsed)
        runs[str(row[0])] = (int(row[1]), str(row[2]), metadata[0] or {})

    mapped_events: set[str] = set()
    count = 0
    for row in connection.execute(
        "SELECT map.migration_run_id,map.source_table,map.legacy_id,map.workspace_id,"
        "map.target_kind,map.target_id,map.source_row_hash,map.imported_event_id,"
        "event.workspace_id,event.stream_id,event.stream_kind,event.actor_type,"
        "event.correlation_id,event.payload_json,event.event_type "
        "FROM legacy_id_map map LEFT JOIN memory_events event "
        "ON event.event_id=map.imported_event_id"
    ):
        payload = parse_canonical_json(str(row[13])) if row[13] is not None else {}
        legacy = payload.get("legacy") if isinstance(payload, dict) else None
        if not isinstance(legacy, dict):
            raise ValueError("legacy migration source table differs")
        if row[1] == "memory_relationships.orphan":
            expected_id = str(row[2]).removeprefix("memories:")
            if legacy != {"table": "memories", "id": expected_id, "missing": True}:
                raise ValueError("legacy placeholder source provenance differs")
        else:
            if legacy.get("table") != row[1]:
                raise ValueError("legacy migration source table differs")
            columns = legacy.get("columns")
            if not isinstance(columns, list):
                raise ValueError("legacy migration source columns are missing")
            column_map = {
                str(item[0]): item[1]
                for item in columns
                if isinstance(item, list) and len(item) == 2
            }
            if "id" not in column_map or str(column_map["id"]) != str(row[2]):
                raise ValueError("legacy migration source id differs")
        expected_kind = "memory" if row[4] == "placeholder" else row[4]
        expected_event_type = {
            "memory": "legacy.memory_state_imported",
            "fact": "fact.asserted",
            "relationship": "relationship.created",
            "placeholder": "legacy.placeholder_created",
        }.get(str(row[4]))
        if (
            row[8] != row[3]
            or row[9] != row[5]
            or row[10] != expected_kind
            or row[11] != "migration"
            or row[12] != row[0]
            or sha256_json(payload.get("legacy")) != row[6]
            or row[14] != expected_event_type
        ):
            raise ValueError("legacy migration mapping provenance differs")
        run = runs.get(str(row[0]))
        if run is None:
            raise ValueError("legacy migration run is missing")
        mapped_events.add(str(row[7]))
        count += 1

    for event in connection.execute(
        "SELECT event_id,event_type FROM memory_events WHERE actor_type='migration' "
        "AND event_type IN ('legacy.memory_state_imported','legacy.placeholder_created',"
        "'fact.asserted','relationship.created')"
    ):
        if str(event[0]) not in mapped_events:
            raise ValueError("legacy migration event has no mapping claim")

    # Active legacy migrations must cover their retained source rows exactly and
    # advertise completed checkpoints consistent with the snapshotted inventory.
    target_kind = {
        "memories": "memory",
        "facts": "fact",
        "memory_relationships": "relationship",
    }
    tables = _table_names(connection)
    for run_id, (source_format, status_value, inventory) in runs.items():
        if source_format >= 7 or status_value != "active":
            continue
        inventory_tables = inventory.get("tables")
        if not isinstance(inventory_tables, dict):
            raise ValueError("legacy migration inventory is incomplete")
        for source_table, kind in target_kind.items():
            expected_count = int(inventory_tables.get(source_table, 0))
            checkpoint = connection.execute(
                "SELECT rows_imported,completed FROM v7_migration_checkpoints "
                "WHERE migration_run_id=? AND source_table=?",
                (run_id, source_table),
            ).fetchone()
            if (
                checkpoint is None
                or int(checkpoint[1]) != 1
                or int(checkpoint[0]) != expected_count
            ):
                raise ValueError("legacy migration checkpoint is incomplete")
            mapped_count = int(
                connection.execute(
                    "SELECT count(*) FROM legacy_id_map WHERE migration_run_id=? "
                    "AND source_table=? AND target_kind=?",
                    (run_id, source_table, kind),
                ).fetchone()[0]
            )
            if mapped_count != expected_count:
                raise ValueError("legacy migration mapping coverage differs")
            if source_table not in tables:
                if expected_count:
                    raise ValueError("retained legacy source table is missing")
                continue
            rows = connection.execute(
                f'SELECT * FROM "{source_table}" ORDER BY id'
            ).fetchall()
            if len(rows) != expected_count:
                raise ValueError("retained legacy source count differs")
            for source_row in rows:
                mapping = connection.execute(
                    "SELECT source_row_hash FROM legacy_id_map WHERE migration_run_id=? "
                    "AND source_table=? AND legacy_id=? AND target_kind=?",
                    (run_id, source_table, str(source_row["id"]), kind),
                ).fetchone()
                if mapping is None or str(mapping[0]) != _source_row_hash(
                    source_row, source_table
                ):
                    raise ValueError("retained legacy source provenance differs")
    # Every map above is bound to its event provenance. Apply the runtime's
    # exact live-claim semantics to the verified replay, including all run
    # statuses. This permits retired identities without trusting corrupt
    # current projections or declaring their repair impossible.
    for row in replay.execute("SELECT DISTINCT workspace_id FROM memory_events"):
        build_live_compatibility_claim_index(replay, str(row[0]))
    return count


def _verify_manifests(connection: sqlite3.Connection) -> tuple[int, int, int]:
    from .retrieval.projections import LexicalProjectionBuilder
    from .retrieval.specialized_projection import SpecializedProjectionBuilder

    roots = {
        str(row[0]): _event_snapshot(connection, str(row[0]))
        for row in connection.execute("SELECT DISTINCT workspace_id FROM memory_events")
    }
    total = 0
    stale = 0
    manifest_rows = connection.execute(
        "SELECT workspace_id,projection_name,status,source_event_count,"
        "source_event_root_hash,details_json,generation,row_count,"
        "cursor_recorded_at_us,cursor_event_id FROM projection_manifests"
    ).fetchall()
    for name, generation_table in GENERATION_TABLES.items():
        if connection.execute(
            f'SELECT 1 FROM "{generation_table}" rows WHERE NOT EXISTS '
            "(SELECT 1 FROM projection_manifests manifest WHERE "
            "manifest.workspace_id=rows.workspace_id AND manifest.projection_name=? "
            "AND manifest.generation=rows.projection_generation) LIMIT 1",
            (name,),
        ).fetchone():
            raise ValueError("projection generation has no matching manifest")
    latest_local: dict[tuple[str, str], tuple[int, bool]] = {}
    for row in manifest_rows:
        if row[1] not in _SUPPORTED_PROJECTIONS:
            raise ValueError("unsupported projection manifest name")
        details = parse_canonical_json(str(row[5]))
        if not isinstance(details, dict):
            raise ValueError("projection manifest details are invalid")
        expected = roots.get(str(row[0]), (0, hashlib.sha256().hexdigest()))
        projection_name = str(row[1])
        current = projection_name == "code" or (
            int(row[3]) == expected[0] and str(row[4]) == expected[1]
        )
        declared_stale = (
            row[2] == "rebuild_required"
            or details.get("rebuild_required_event_id") is not None
        )
        if row[1] in _LOCAL_PROJECTIONS:
            key = (str(row[0]), str(row[1]))
            previous = latest_local.get(key)
            if previous is None or int(row[6]) > previous[0]:
                latest_local[key] = (int(row[6]), not current or declared_stale)
        if row[2] == "active" and not declared_stale:
            if projection_name == "code":
                if (row[8], row[9]) != (None, None):
                    raise ValueError("code projection has a memory-event cursor")
            else:
                cursor = connection.execute(
                    "SELECT recorded_at_us,event_id FROM memory_events "
                    "WHERE workspace_id=? ORDER BY recorded_at_us DESC,event_id DESC "
                    "LIMIT 1",
                    (row[0],),
                ).fetchone()
                expected_cursor = (
                    (None, None) if cursor is None else (cursor[0], cursor[1])
                )
                if not current or (row[8], row[9]) != expected_cursor:
                    raise ValueError("active projection manifest source cursor differs")
            name = str(row[1])
            workspace = str(row[0])
            generation = int(row[6])
            expected_rows: int | None = None
            table_by_name = {
                "memory_records": "memory_records",
                "memory_fact_versions": "memory_fact_versions",
                "memory_relationship_versions": "memory_relationship_versions",
                "lexical": "retrieval_documents",
                "procedure": "record_procedures",
                "outcome": "record_outcome_view",
            }
            table = table_by_name.get(name)
            if table is not None:
                if name.startswith("memory_"):
                    expected_rows = int(
                        connection.execute(
                            f'SELECT count(*) FROM "{table}" WHERE workspace_id=?',
                            (workspace,),
                        ).fetchone()[0]
                    )
                else:
                    expected_rows = int(
                        connection.execute(
                            f'SELECT count(*) FROM "{table}" WHERE workspace_id=? '
                            "AND projection_generation=?",
                            (workspace, generation),
                        ).fetchone()[0]
                    )
            elif name == "dense":
                provider_key = details.get("provider_key")
                if not isinstance(provider_key, str) or not provider_key:
                    raise ValueError("dense projection provider provenance is missing")
                expected_rows = int(
                    connection.execute(
                        "SELECT count(*) FROM dense_projection_refs WHERE workspace_id=? "
                        "AND provider_key=? AND projection_generation=?",
                        (workspace, provider_key, generation),
                    ).fetchone()[0]
                )
            elif name == "code":
                expected_rows, _edge_count = verify_code_projection(
                    connection,
                    workspace,
                    generation,
                    expected_row_count=int(row[7]),
                    expected_root_hash=str(row[4]),
                )
            elif name in {"entities", "communities"}:
                discovery_table = {
                    "entities": "discovery_entities",
                    "communities": "discovery_communities",
                }[name]
                generation_column = "graph_generation"
                expected_rows = int(
                    connection.execute(
                        f'SELECT count(*) FROM "{discovery_table}" WHERE workspace_id=? '
                        f'AND "{generation_column}"=?',
                        (workspace, generation),
                    ).fetchone()[0]
                )
            if expected_rows is not None and int(row[7]) != expected_rows:
                raise ValueError("active projection manifest row count differs")
            if name == "lexical":
                canonical_rows = connection.execute(
                    "SELECT record_id,content,COALESCE(rationale,''),tags_json,record_type,"
                    "context_json,archived,content_hash,source_event_id FROM memory_records "
                    "WHERE workspace_id=? AND deleted_at_us IS NULL ORDER BY record_id",
                    (workspace,),
                ).fetchall()
                projected_rows = connection.execute(
                    "SELECT record_id,content,rationale,tags_text,category,visibility,"
                    "archived,content_hash,source_event_id FROM retrieval_documents "
                    "WHERE workspace_id=? AND projection_generation=? ORDER BY record_id",
                    (workspace, generation),
                ).fetchall()
                lexical_expected: list[tuple[Any, ...]] = []
                for canonical in canonical_rows:
                    tags = parse_canonical_json(str(canonical[3]))
                    context = parse_canonical_json(str(canonical[5]))
                    if not isinstance(tags, list) or not isinstance(context, dict):
                        raise ValueError("canonical lexical metadata is invalid")
                    lexical_expected.append(
                        (
                            canonical[0],
                            canonical[1],
                            canonical[2],
                            "\n".join(
                                value
                                if isinstance(value, str)
                                else canonical_json_bytes(value).decode("utf-8")
                                for value in tags
                            ),
                            canonical[4],
                            context.get("visibility", "workspace"),
                            canonical[6],
                            canonical[7],
                            canonical[8],
                        )
                    )
                if [
                    tuple(item) for item in projected_rows
                ] != lexical_expected or not LexicalProjectionBuilder(
                    connection
                ).active_is_current(workspace):
                    raise ValueError("lexical projection content differs")
            if name in {
                "graph",
                "temporal",
                "procedure",
                "outcome",
            } and not SpecializedProjectionBuilder(connection).active_is_current(
                workspace, name
            ):
                raise ValueError(f"{name} projection content differs")
        stale += int(not current or declared_stale)
        total += 1
    return total, stale, sum(int(value[1]) for value in latest_local.values())


def _verify_database(
    path: Path, workspace_id: str, replay_path: Path
) -> dict[str, Any]:
    checks: dict[str, dict[str, Any]] = {}
    authority = {"memory_events": 0, "governance_events": 0, "federation_events": 0}
    source = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    replay: sqlite3.Connection | None = None
    roots: dict[str, tuple[int, str]] = {}
    authority_roots: dict[str, tuple[int, str]] = {}
    try:
        tables = _table_names(source)
        versions = (
            {
                int(row[0])
                for row in source.execute("SELECT version FROM schema_version")
            }
            if "schema_version" in tables
            else set()
        )
        schema_ok = (
            tables >= _REQUIRED_TABLES and versions >= REQUIRED_V7_SCHEMA_VERSIONS
        )
        checks["schema"] = _check(
            schema_ok,
            current=max(versions, default=0),
            required=CURRENT_SCHEMA_VERSION,
        )
        if not schema_ok:
            return {
                "checks": checks,
                "authority": authority,
                "roots": roots,
                "authority_roots": authority_roots,
            }
        integrity = [str(row[0]) for row in source.execute("PRAGMA integrity_check")]
        foreign = [list(row) for row in source.execute("PRAGMA foreign_key_check")]
        checks["sqlite_integrity"] = _check(
            integrity == ["ok"] and not foreign,
            foreign_key_violations=len(foreign),
        )
        replay = _initialize_replay(replay_path, workspace_id)
        try:
            authority["memory_events"], roots = _memory_replay(source, replay)
            checks["memory_events"] = _check(True, count=authority["memory_events"])
        except Exception as exc:
            checks["memory_events"] = _check(False, error=type(exc).__name__)
        try:
            authority["governance_events"] = _governance_replay(source, replay)
            checks["governance_events"] = _check(
                True, count=authority["governance_events"]
            )
        except Exception as exc:
            checks["governance_events"] = _check(False, error=type(exc).__name__)
        try:
            authority["federation_events"] = _verify_federation(source)
            checks["federation_events"] = _check(
                True, count=authority["federation_events"]
            )
        except Exception as exc:
            checks["federation_events"] = _check(False, error=type(exc).__name__)
        try:
            authority_roots = {
                "memory_events": _ledger_root(source, "memory_events"),
                "governance_events": _ledger_root(source, "governance_events"),
                "federation_events": _ledger_root(source, "workspace_link_events"),
            }
        except (TypeError, ValueError):
            authority_roots = {}
        try:
            count = _verify_sequence(source)
            checks["session_update_sequence"] = _check(True, count=count)
        except Exception as exc:
            checks["session_update_sequence"] = _check(False, error=type(exc).__name__)
        try:
            count = _verify_mappings(source, replay)
            checks["migration_mappings"] = _check(True, count=count)
        except Exception as exc:
            checks["migration_mappings"] = _check(False, error=type(exc).__name__)
        try:
            manifest_count, stale, local_stale = _verify_manifests(source)
            checks["projection_manifests"] = _check(
                True,
                count=manifest_count,
                rebuild_required=stale,
                local_rebuild_required=local_stale,
            )
        except Exception as exc:
            checks["projection_manifests"] = _check(False, error=type(exc).__name__)
        try:
            invalid = int(
                source.execute(
                    "SELECT COUNT(*) FROM dreaming_strategy_state WHERE "
                    "strategy NOT IN ('failed_decision','pending_outcome','connection_discovery','community_refresh') "
                    "OR status NOT IN ('idle','running','disabled','degraded') "
                    "OR pending_count NOT BETWEEN 0 AND 10000 "
                    "OR yielded NOT IN (0,1)"
                ).fetchone()[0]
            )
            count = int(
                source.execute(
                    "SELECT COUNT(*) FROM dreaming_strategy_state"
                ).fetchone()[0]
            )
            duplicate = source.execute(
                "SELECT 1 FROM dreaming_strategy_state GROUP BY workspace_id,strategy "
                "HAVING COUNT(*)>1 LIMIT 1"
            ).fetchone()
            checks["dreaming_strategy_state"] = _check(
                invalid == 0 and duplicate is None,
                count=count,
                invalid=invalid,
            )
        except Exception as exc:
            checks["dreaming_strategy_state"] = _check(False, error=type(exc).__name__)

        replayable = checks.get("memory_events", {}).get("ok") and checks.get(
            "governance_events", {}
        ).get("ok")
        if replayable:
            try:
                differences: list[str] = []
                for table in _DERIVED_TABLES:
                    if _table_digest(source, table) != _table_digest(replay, table):
                        differences.append(table)
                checks["canonical_replay"] = _check(
                    not differences, differences=differences
                )
            except Exception as exc:
                checks["canonical_replay"] = _check(False, error=type(exc).__name__)
        else:
            checks["canonical_replay"] = _check(False, error="AUTHORITY_REPLAY_FAILED")
    finally:
        if replay is not None:
            replay.close()
        source.close()
    return {
        "checks": checks,
        "authority": authority,
        "roots": roots,
        "authority_roots": authority_roots,
    }


def _replace_derived_tables(candidate: Path, replay_path: Path) -> None:
    connection = sqlite3.connect(candidate)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("ATTACH DATABASE ? AS replay", (str(replay_path),))
        connection.execute("BEGIN IMMEDIATE")
        for table in _DERIVED_TABLES:
            triggers = [
                (str(row[0]), str(row[1]))
                for row in connection.execute(
                    "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
                    "AND tbl_name=? AND sql IS NOT NULL",
                    (table,),
                )
            ]
            for name, _sql in triggers:
                connection.execute(f'DROP TRIGGER "{name}"')
            connection.execute(f'DELETE FROM "{table}"')
            columns = [
                str(row[1])
                for row in connection.execute(f'PRAGMA table_info("{table}")')
            ]
            quoted = ",".join(f'"{column}"' for column in columns)
            connection.execute(
                f'INSERT INTO "{table}" ({quoted}) SELECT {quoted} FROM replay."{table}"'
            )
            for _name, sql in triggers:
                connection.execute(sql)
        # Dreaming progress is bounded derived metadata, not canonical history.
        # Recovery restarts it from authoritative records and candidates.
        connection.execute("DELETE FROM dreaming_strategy_state")
        # A finalization lease belongs to the process and exact active database
        # that acquired it. Never reactivate it through a recovered candidate.
        # Keep durable pages/session identity, and retain the conservative byte
        # reservation until post-activation orphan cleanup measures the files.
        connection.execute("DELETE FROM portable_transfer_finalization_leases")
        connection.execute(
            "UPDATE portable_transfer_sessions SET status='staging',"
            "completed_at_us=NULL WHERE direction='import' AND status='finalizing'"
        )
        connection.commit()
        connection.execute("DETACH DATABASE replay")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _cleanup_portable_transfer_attempts(storage: Path, candidate: Path) -> None:
    """Remove orphan attempt files after activation and restore exact accounting."""

    imports_root = storage / "portable" / "v2" / "imports"
    connection = sqlite3.connect(candidate)
    try:
        rows = connection.execute(
            "SELECT session_id FROM portable_transfer_sessions AS session "
            "WHERE direction='import' AND status='staging' AND NOT EXISTS("
            "SELECT 1 FROM portable_transfer_finalization_leases AS lease "
            "WHERE lease.session_id=session.session_id)"
        ).fetchall()
        connection.execute("BEGIN IMMEDIATE")
        for (raw_session_id,) in rows:
            session_id = str(raw_session_id)
            if re.fullmatch(r"ipt_[0-9a-f]{64}", session_id) is None:
                continue
            session_directory = (imports_root / session_id).resolve()
            if not _within(session_directory, imports_root.resolve()):
                continue
            attempts = session_directory / "attempts"
            if attempts.exists():
                if _is_link_or_reparse(attempts) or not _within(
                    attempts.resolve(), session_directory
                ):
                    continue
                unsafe = any(
                    _is_link_or_reparse(path)
                    or not _within(path.resolve(), attempts.resolve())
                    for path in attempts.rglob("*")
                )
                if unsafe:
                    continue
                shutil.rmtree(attempts)
            total_bytes = 0
            safe = True
            if session_directory.exists():
                for path in session_directory.rglob("*"):
                    if _is_link_or_reparse(path) or not _within(
                        path.resolve(), session_directory
                    ):
                        safe = False
                        break
                    if path.is_file():
                        total_bytes += path.stat().st_size
            if safe and total_bytes <= 4 * 1024 * 1024 * 1024:
                connection.execute(
                    "UPDATE portable_transfer_sessions SET total_bytes=? "
                    "WHERE session_id=? AND direction='import' AND status='staging'",
                    (total_bytes, session_id),
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _refresh_manifests(candidate: Path, workspace_ids: list[str]) -> None:
    from .retrieval.projections import LexicalProjectionBuilder
    from .retrieval.specialized_projection import SpecializedProjectionBuilder

    connection = sqlite3.connect(candidate)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        # Unrecognized manifests are corrupt derived metadata. The original
        # remains in the retained source; only the separate candidate is reset.
        supported = tuple(sorted(_SUPPORTED_PROJECTIONS))
        placeholders = ",".join("?" for _ in supported)
        connection.execute(
            f"DELETE FROM projection_manifests WHERE projection_name NOT IN ({placeholders})",
            supported,
        )
        # Remove only orphaned local generations in the candidate. Otherwise a
        # renamed/deleted manifest can leave an FTS table occupying the next ID.
        for name, table in GENERATION_TABLES.items():
            connection.execute(
                f'DELETE FROM "{table}" AS rows WHERE NOT EXISTS '
                "(SELECT 1 FROM projection_manifests manifest WHERE "
                "manifest.workspace_id=rows.workspace_id AND manifest.projection_name=? "
                "AND manifest.generation=rows.projection_generation)",
                (name,),
            )
        for table_row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall():
            name = str(table_row[0])
            match = re.fullmatch(
                r"retrieval_(procedure_)?fts_([0-9a-f]{24})_g([0-9]+)", name
            )
            if match is None:
                continue
            projection = "procedure" if match[1] else "lexical"
            if (
                connection.execute(
                    "SELECT 1 FROM projection_manifests WHERE workspace_id=? "
                    "AND projection_name=? AND generation=?",
                    ("ws_" + match[2], projection, int(match[3])),
                ).fetchone()
                is None
            ):
                connection.execute(f'DROP TABLE "{name}"')
        now = time.time_ns() // 1_000
        connection.execute(
            "UPDATE projection_manifests SET status='rebuild_required' "
            "WHERE status='active'"
        )
        for workspace_id in workspace_ids:
            event_count, root = _event_snapshot(connection, workspace_id)
            cursor = connection.execute(
                "SELECT recorded_at_us,event_id FROM memory_events WHERE workspace_id=? "
                "ORDER BY recorded_at_us DESC,event_id DESC LIMIT 1",
                (workspace_id,),
            ).fetchone()
            for name, table in (
                ("memory_records", "memory_records"),
                ("memory_fact_versions", "memory_fact_versions"),
                ("memory_relationship_versions", "memory_relationship_versions"),
            ):
                generation = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(generation),0)+1 FROM projection_manifests "
                        "WHERE workspace_id=? AND projection_name=?",
                        (workspace_id, name),
                    ).fetchone()[0]
                )
                row_count = int(
                    connection.execute(
                        f'SELECT count(*) FROM "{table}" WHERE workspace_id=?',
                        (workspace_id,),
                    ).fetchone()[0]
                )
                manifest_id = deterministic_id(
                    "prj", "projection", workspace_id, name, generation, root
                )
                connection.execute(
                    "INSERT INTO projection_manifests (manifest_id,workspace_id,"
                    "projection_name,generation,projection_version,status,"
                    "source_event_count,source_event_root_hash,cursor_recorded_at_us,"
                    "cursor_event_id,row_count,builder_version,details_json,started_at_us,"
                    "completed_at_us,activated_at_us) VALUES (?,?,?,?,1,'active',?,?,?,?,?,"
                    "'verify-v7-replay-1','{}',?,?,?)",
                    (
                        manifest_id,
                        workspace_id,
                        name,
                        generation,
                        event_count,
                        root,
                        None if cursor is None else cursor[0],
                        None if cursor is None else cursor[1],
                        row_count,
                        now,
                        now,
                        now,
                    ),
                )
            LexicalProjectionBuilder(connection).rebuild(workspace_id)
            specialized = SpecializedProjectionBuilder(connection)
            for name in ("graph", "temporal", "procedure", "outcome"):
                specialized.rebuild(workspace_id, name)
        connection.execute(
            "UPDATE projection_manifests SET status='rebuild_required' "
            "WHERE projection_name IN ('dense','code','communities','entities') "
            "AND status='active'"
        )
        connection.commit()
    finally:
        connection.close()


def _record_recovery_run(
    candidate: Path,
    run_id: str,
    workspace_id: str,
    snapshot_hash: str,
    source_inventory: Mapping[str, Any],
) -> None:
    now = time.time_ns() // 1_000
    connection = sqlite3.connect(candidate)
    try:
        connection.execute(
            "INSERT INTO v7_migration_runs (migration_run_id,workspace_id,"
            "source_db_sha256,source_schema_version,source_format_version,"
            "target_format_version,status,snapshot_name,candidate_name,"
            "source_inventory_json,validation_json,created_at_us,updated_at_us,"
            "validated_at_us,activated_at_us) VALUES (?,?,?,?,7,7,'active',"
            "'source.snapshot.db','candidate.db',?,?,?,?,?,?)",
            (
                run_id,
                workspace_id,
                snapshot_hash,
                CURRENT_SCHEMA_VERSION,
                canonical_json_bytes(dict(source_inventory)).decode("utf-8"),
                canonical_json_bytes({"authority_verified": True}).decode("utf-8"),
                now,
                now,
                now,
                now,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _recovery_source_inventory(
    snapshot: Path, storage: Path, active: Any
) -> dict[str, Any]:
    inventory = inventory_database(snapshot)
    pointer_path = storage / "active-db.json"
    pointer_bytes = pointer_path.read_bytes() if pointer_path.is_file() else b""
    return {
        "recovery": "verify-v7",
        "logical_sha256": inventory["logical_sha256"],
        "tables": inventory["tables"],
        "table_hashes": inventory["table_hashes"],
        "active_pointer": {
            "sha256": hashlib.sha256(pointer_bytes).hexdigest(),
            "format_version": active.format_version,
            "generation": active.generation,
            "active_db": active.relative_path,
            "migration_run_id": active.migration_run_id,
        },
    }


def _archive_incomplete_run(run_dir: Path, root: Path) -> None:
    if (
        _is_link_or_reparse(run_dir)
        or not run_dir.is_dir()
        or not _within(run_dir, root)
    ):
        raise VerificationV7Error("UNSAFE_RECOVERY_PATH", run_dir.name)
    for child in run_dir.iterdir():
        if _is_link_or_reparse(child):
            raise VerificationV7Error("UNSAFE_RECOVERY_PATH", child.name)
    suffix = 1
    while True:
        archived = _safe_child(root, f"{run_dir.name}.incomplete-{suffix}")
        if not archived.exists():
            os.replace(run_dir, archived)
            _fsync_directory(root)
            return
        suffix += 1


def _resumable_candidate(
    run_dir: Path,
    run_id: str,
    workspace_id: str,
    source_inventory: Mapping[str, Any],
) -> tuple[Path, Path] | None:
    if _is_link_or_reparse(run_dir) or not run_dir.is_dir():
        raise VerificationV7Error("UNSAFE_RECOVERY_PATH", run_dir.name)
    snapshot = _safe_child(run_dir, "source.snapshot.db")
    candidate = _safe_child(run_dir, "candidate.db")
    if not snapshot.is_file() or not candidate.is_file():
        return None
    _require_owned_regular(snapshot, run_dir)
    _require_owned_regular(candidate, run_dir)
    try:
        connection = sqlite3.connect(f"file:{candidate.as_posix()}?mode=ro", uri=True)
        try:
            row = connection.execute(
                "SELECT workspace_id,source_db_sha256,status,source_inventory_json "
                "FROM v7_migration_runs WHERE migration_run_id=?",
                (run_id,),
            ).fetchone()
        finally:
            connection.close()
        parsed = None if row is None else parse_canonical_json(str(row[3]))
        if (
            row is None
            or row[0] != workspace_id
            or row[1] != _physical_sha256(snapshot)
            or row[2] != "active"
            or parsed != dict(source_inventory)
        ):
            return None
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None
    return snapshot, candidate


def _all_ok(checks: Mapping[str, Mapping[str, Any]]) -> bool:
    return bool(checks) and all(bool(value.get("ok")) for value in checks.values())


def _authority_ok(checks: Mapping[str, Mapping[str, Any]]) -> bool:
    required = (
        "schema",
        "sqlite_integrity",
        "memory_events",
        "governance_events",
        "federation_events",
        "session_update_sequence",
        "migration_mappings",
        "active_pointer",
    )
    return all(bool(checks.get(name, {}).get("ok")) for name in required)


def _active_pointer_check(active: Any, workspace_id: str) -> dict[str, Any]:
    if active.migration_run_id is None:
        return _check(
            active.pointer is not None
            and active.generation == 1
            and active.relative_path == "daem0nmcp.db",
            format_version=active.format_version,
            generation=active.generation,
            retained_previous=False,
        )
    expected = f"migrations/v7/{active.migration_run_id}/candidate.db"
    try:
        connection = sqlite3.connect(f"file:{active.path.as_posix()}?mode=ro", uri=True)
        try:
            row = connection.execute(
                "SELECT workspace_id,source_db_sha256,status,target_format_version,"
                "snapshot_name,candidate_name FROM v7_migration_runs "
                "WHERE migration_run_id=?",
                (active.migration_run_id,),
            ).fetchone()
        finally:
            connection.close()
        snapshot = active.path.parent / "source.snapshot.db"
        ok = (
            active.relative_path == expected
            and row is not None
            and row[0] == workspace_id
            and row[2] == "active"
            and int(row[3]) == 7
            and row[4] == "source.snapshot.db"
            and row[5] == "candidate.db"
            and snapshot.is_file()
            and not snapshot.is_symlink()
            and _physical_sha256(snapshot) == row[1]
        )
    except (OSError, sqlite3.Error, TypeError, ValueError):
        ok = False
    return _check(
        ok,
        format_version=active.format_version,
        generation=active.generation,
        retained_previous=active.previous_db is not None,
    )


def verify_v7(
    storage_path: str | os.PathLike[str],
    workspace_id: str,
    *,
    repair_projections: bool = False,
    fault_injector: Callable[[str, dict[str, object]], None] | None = None,
) -> dict[str, Any]:
    """Verify one active format-7 store and optionally recover derived state."""

    storage = Path(storage_path)
    mode: Literal["shared", "exclusive"] = (
        "exclusive" if repair_projections else "shared"
    )
    with DatabaseFileLock(storage, mode):
        active = resolve_active_database(storage)
        if active.format_version != 7:
            raise VerificationV7Error("FORMAT_7_REQUIRED")
        with tempfile.TemporaryDirectory(prefix="daem0nmcp-verify-v7-") as raw_temp:
            temporary = Path(raw_temp)
            snapshot = temporary / "snapshot.db"
            replay = temporary / "replay.db"
            _database_backup(active.path, snapshot)
            verified = _verify_database(snapshot, workspace_id, replay)
            checks = verified["checks"]
            checks["active_pointer"] = _active_pointer_check(active, workspace_id)
            report = {
                "status": "verified" if _all_ok(checks) else "invalid",
                "workspace_id": workspace_id,
                "format_version": active.format_version,
                "active_generation": active.generation,
                "authority": verified["authority"],
                "authority_roots": verified["authority_roots"],
                "checks": checks,
                "repair": {"requested": repair_projections, "activated": False},
            }
            if not repair_projections:
                return report
            if not _authority_ok(checks):
                raise VerificationV7Error("AUTHORITATIVE_STATE_INVALID")
            manifests = checks.get("projection_manifests", {})
            if (
                checks.get("canonical_replay", {}).get("ok")
                and manifests.get("ok")
                and not manifests.get("local_rebuild_required")
            ):
                report["repair"] = {"requested": True, "activated": False}
                return report

            event_roots = verified["roots"]
            authority_roots = verified["authority_roots"]
            source_inventory = _recovery_source_inventory(snapshot, storage, active)
            recovery_identity = sha256_json(
                [
                    "verify-v7-recovery",
                    workspace_id,
                    source_inventory,
                ]
            )
            run_id = "mig_" + recovery_identity
            recovery_root = _safe_recovery_root(storage)
            run_dir = _safe_child(recovery_root, run_id)
            retained_snapshot: Path
            candidate: Path
            if run_dir.exists():
                resumed = _resumable_candidate(
                    run_dir, run_id, workspace_id, source_inventory
                )
                if resumed is None:
                    _archive_incomplete_run(run_dir, recovery_root)
                else:
                    retained_snapshot, candidate = resumed
            if not run_dir.exists():
                run_dir.mkdir(mode=0o700)
                _fault(fault_injector, "after_run_directory", run_id)
                retained_partial = _safe_child(run_dir, "source.snapshot.db.partial")
                retained_snapshot = _safe_child(run_dir, "source.snapshot.db")
                candidate_partial = _safe_child(run_dir, "candidate.db.partial")
                candidate = _safe_child(run_dir, "candidate.db")
                _copy_exclusive(snapshot, retained_partial)
                _fault(fault_injector, "after_snapshot_copy", run_id)
                _publish_partial(retained_partial, retained_snapshot, run_dir)
                _fault(fault_injector, "after_snapshot_publish", run_id)
                _copy_exclusive(snapshot, candidate_partial)
                _fault(fault_injector, "after_candidate_copy", run_id)
                _require_owned_regular(candidate_partial, run_dir)
                _replace_derived_tables(candidate_partial, replay)
                _fault(fault_injector, "after_derived_replay", run_id)
                workspace_ids = sorted(event_roots)
                _require_owned_regular(candidate_partial, run_dir)
                _refresh_manifests(candidate_partial, workspace_ids)
                _fault(fault_injector, "after_manifest_rebuild", run_id)
                snapshot_hash = _physical_sha256(retained_snapshot)
                _require_owned_regular(candidate_partial, run_dir)
                _record_recovery_run(
                    candidate_partial,
                    run_id,
                    workspace_id,
                    snapshot_hash,
                    source_inventory,
                )
                _fault(fault_injector, "after_run_row", run_id)
                construction_replay = temporary / "construction-replay.db"
                _require_owned_regular(candidate_partial, run_dir)
                construction_verification = _verify_database(
                    candidate_partial, workspace_id, construction_replay
                )
                if (
                    not _all_ok(construction_verification["checks"])
                    or construction_verification["authority_roots"] != authority_roots
                ):
                    raise VerificationV7Error("RECOVERY_CANDIDATE_INVALID")
                _fault(fault_injector, "after_candidate_validation", run_id)
                _fsync_file(candidate_partial)
                _fault(fault_injector, "after_candidate_fsync", run_id)
                _publish_partial(candidate_partial, candidate, run_dir)
            validation_replay = temporary / "candidate-replay.db"
            _require_owned_regular(retained_snapshot, run_dir)
            _require_owned_regular(candidate, run_dir)
            candidate_verification = _verify_database(
                candidate, workspace_id, validation_replay
            )
            if (
                not _all_ok(candidate_verification["checks"])
                or candidate_verification["authority_roots"] != authority_roots
            ):
                raise VerificationV7Error("RECOVERY_CANDIDATE_INVALID")
            _fsync_file(retained_snapshot)
            _fsync_file(candidate)
            _fsync_directory(run_dir)
            # Re-read active metadata immediately before publication. The source
            # identity includes every table and the pointer bytes, so an
            # interrupted run cannot discard later writes on retry.
            current = resolve_active_database(storage)
            current_snapshot = temporary / "current-source.db"
            _database_backup(current.path, current_snapshot)
            if (
                _recovery_source_inventory(current_snapshot, storage, current)
                != source_inventory
            ):
                raise VerificationV7Error("RECOVERY_SOURCE_CHANGED")
            _require_owned_regular(retained_snapshot, run_dir)
            _require_owned_regular(candidate, run_dir)
            _fault(fault_injector, "before_pointer", run_id)
            pointer = ActiveDatabasePointer(
                format_version=7,
                generation=active.generation + 1,
                active_db=candidate.relative_to(storage).as_posix(),
                previous_db=active.relative_path,
                migration_run_id=run_id,
            )
            write_active_pointer(storage, pointer)
            # Pointer publication is the durable boundary. Cleanup affects only
            # transient attempt files; if it cannot run, the conservative
            # reservation remains and the next portable operation reconciles it.
            with suppress(OSError, sqlite3.Error):
                _cleanup_portable_transfer_attempts(storage, candidate)
            report.update(
                {
                    "status": "repaired",
                    "active_generation": pointer.generation,
                    "checks": candidate_verification["checks"]
                    | {
                        "active_pointer": _check(
                            True,
                            format_version=7,
                            generation=pointer.generation,
                            retained_previous=True,
                        )
                    },
                    "repair": {"requested": True, "activated": True},
                }
            )
            return report


__all__ = ["VerificationV7Error", "verify_v7"]
