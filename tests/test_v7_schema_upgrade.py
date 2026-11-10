"""Recoverable upgrades for already-active architecture-format 7 stores."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

import daem0nmcp.migrations.v7 as migration_v7_module
from daem0nmcp.database import DatabaseManager
from daem0nmcp.event_store import EventCommand, EventStore
from daem0nmcp.migrations.schema import MIGRATIONS
from daem0nmcp.migrations.v7 import (
    MigrationInterrupted,
    MigrationV7Error,
    MigrationV7Service,
    inventory_database,
)
from daem0nmcp.migrations.v7_schema_upgrade import _check_contract
from daem0nmcp.schema_version import (
    CURRENT_SCHEMA_VERSION,
    REQUIRED_V7_SCHEMA_VERSIONS,
)
from daem0nmcp.storage_activation import (
    ActiveDatabasePointer,
    DatabaseFileLock,
    DatabaseInUseError,
    resolve_active_database,
    write_active_pointer,
)
from daem0nmcp.workspace import WorkspaceRegistry

MEMORY_ID = "mem_" + "1" * 64


def _state(content: str = "retained authority") -> dict[str, object]:
    return {
        "record_type": "decision",
        "legacy_type": None,
        "content": content,
        "rationale": None,
        "context": {},
        "tags": ["schema-upgrade"],
        "file_path": None,
        "file_path_relative": None,
        "keywords": None,
        "is_permanent": False,
        "pinned": False,
        "archived": False,
        "outcome": None,
        "worked": None,
        "recall_count": 0,
        "surprise_score": None,
        "importance_score": None,
        "source_client": "test",
        "source_model": None,
        "deleted_at_us": None,
    }


def _fixture(
    root: Path,
    *,
    maximum_version: int,
    with_event: bool = True,
) -> tuple[Path, Path, WorkspaceRegistry]:
    storage = root / ".daem0nmcp" / "storage"
    storage.mkdir(parents=True)
    database = storage / "daem0nmcp.db"
    registry = WorkspaceRegistry([root], default_root=root)
    workspace_id = registry.default.workspace_id
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE schema_version(version INTEGER PRIMARY KEY, "
            "applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        for version, _description, statements in MIGRATIONS:
            if version not in REQUIRED_V7_SCHEMA_VERSIONS or version > maximum_version:
                continue
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_version(version) VALUES (?)", (version,)
            )
        connection.commit()
        if with_event:
            EventStore(connection).append_and_project(
                EventCommand(
                    workspace_id=workspace_id,
                    stream_id=MEMORY_ID,
                    stream_kind="memory",
                    event_type="memory.created",
                    occurred_at_us=10,
                    recorded_at_us=11,
                    actor_type="user",
                    payload={"record": _state()},
                )
            )
            connection.commit()
    finally:
        connection.close()
    write_active_pointer(
        storage, ActiveDatabasePointer(7, 1, "daem0nmcp.db", None, None)
    )
    return storage, database, registry


def _orphaned_learning_fixture(root: Path, *, deleted: bool = False):
    storage, database, registry = _fixture(root, maximum_version=33, with_event=False)
    expected = {}
    orphan_ids = []
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        store = EventStore(connection)
        for index, workspace_id in enumerate(
            (registry.default.workspace_id, "ws_" + "b" * 24), start=1
        ):
            record_id = "mem_" + str(index) * 64
            source_id = "mem_" + str(index + 2) * 64
            orphan_ids.append((record_id,))
            store.append_and_project(
                EventCommand(
                    workspace_id=workspace_id,
                    stream_id=source_id,
                    stream_kind="memory",
                    event_type="memory.created",
                    occurred_at_us=10 + index,
                    recorded_at_us=10 + index,
                    actor_type="user",
                    payload={"record": _state("preserved source")},
                )
            )
            final = _state("latest canonical state")
            final.update(worked=False, outcome="latest state differs from outcome")
            events = [
                ("memory.created", _state("original state"), 20 + index),
                (
                    "memory.outcome_recorded",
                    {**_state("outcome state"), "worked": True, "outcome": "worked"},
                    40 + index,
                ),
                ("memory.updated", final, 30 + index),
            ]
            if deleted and index == 1:
                events.append(
                    (
                        "memory.deleted",
                        {**final, "deleted_at_us": 50 + index},
                        50 + index,
                    )
                )
            for version, (event_type, record, timestamp) in enumerate(events, start=1):
                payload = {"record": record}
                if event_type in {"memory.created", "memory.outcome_recorded"}:
                    payload["provenance"] = {"informed_by": [source_id]}
                store.append_and_project(
                    EventCommand(
                        workspace_id=workspace_id,
                        stream_id=record_id,
                        stream_kind="memory",
                        event_type=event_type,
                        occurred_at_us=timestamp,
                        recorded_at_us=timestamp,
                        actor_type="user",
                        expected_stream_version=version,
                        payload=payload,
                    )
                )
            connection.commit()
        for table in (
            "memory_records",
            "memory_provenance_edges",
            "memory_outcome_signals",
        ):
            expected[table] = connection.execute(
                f"SELECT * FROM {table} ORDER BY 1,2,3"
            ).fetchall()
        connection.execute("DROP TABLE memory_provenance_edges")
        connection.execute("DROP TABLE memory_outcome_signals")
        connection.execute("DELETE FROM schema_version WHERE version=33")
        connection.executemany(
            "DELETE FROM memory_records WHERE record_id=?", orphan_ids
        )
        connection.commit()
    return storage, database, registry, expected


def _authority_rows(path: Path) -> dict[str, list[tuple[object, ...]]]:
    connection = sqlite3.connect(path)
    try:
        result: dict[str, list[tuple[object, ...]]] = {}
        for table in ("memory_events", "governance_events", "workspace_link_events"):
            columns = [
                str(row[1])
                for row in connection.execute(f'PRAGMA table_info("{table}")')
            ]
            order = ",".join(f'"{column}"' for column in columns)
            result[table] = [
                tuple(row)
                for row in connection.execute(
                    f'SELECT * FROM "{table}" ORDER BY {order}'
                )
            ]
        return result
    finally:
        connection.close()


def _schema_versions(path: Path) -> set[int]:
    with sqlite3.connect(path) as connection:
        return {
            int(row[0])
            for row in connection.execute("SELECT version FROM schema_version")
        }


def _start_manager(storage: Path) -> tuple[int, int]:
    """Open the store the way every legacy command and hook does."""

    async def start() -> tuple[int, int]:
        manager = DatabaseManager(str(storage))
        try:
            await manager.init_db()
            return manager.format_version, manager.active_generation
        finally:
            await manager.close()

    return asyncio.run(start())


def _assert_pointer_names_its_own_database(storage: Path) -> None:
    """The rule every reader of a format-7 pointer enforces."""

    active = resolve_active_database(storage)
    assert active.format_version == 7
    if active.migration_run_id is None:
        assert (active.generation, active.relative_path, active.previous_db) == (
            1,
            "daem0nmcp.db",
            None,
        )
        return
    assert active.relative_path == (
        f"migrations/v7/{active.migration_run_id}/candidate.db"
    )
    assert _run_status(active.path) == "active"


def _run_status(candidate: Path) -> str | None:
    """The status a published candidate records for its own migration run."""

    with closing(sqlite3.connect(candidate)) as connection:
        row = connection.execute(
            "SELECT status FROM v7_migration_runs WHERE migration_run_id=?",
            (candidate.parent.name,),
        ).fetchone()
    return None if row is None else str(row[0])


@contextmanager
def _as_the_next_build():
    """Run the migration service as a build whose schema is one version ahead."""

    import daem0nmcp.migrations.schema as schema_module
    import daem0nmcp.migrations.v7_schema_upgrade as upgrade_module
    import daem0nmcp.verification_v7 as verification_module

    later = CURRENT_SCHEMA_VERSION + 1
    ledger = [
        *MIGRATIONS,
        (
            later,
            "An additive table from a later build",
            ["CREATE TABLE later_build_marker (marker INTEGER PRIMARY KEY)"],
        ),
    ]
    required = frozenset(range(min(REQUIRED_V7_SCHEMA_VERSIONS), later + 1))
    with ExitStack() as patches:
        for module in (schema_module, upgrade_module, verification_module):
            patches.enter_context(mock.patch.object(module, "MIGRATIONS", ledger))
        for module in (migration_v7_module, upgrade_module, verification_module):
            patches.enter_context(
                mock.patch.object(module, "CURRENT_SCHEMA_VERSION", later)
            )
            patches.enter_context(
                mock.patch.object(module, "REQUIRED_V7_SCHEMA_VERSIONS", required)
            )
        try:
            yield later
        finally:
            upgrade_module._reference_schema_contract.cache_clear()


def _rewrite_schema_object(
    database: Path,
    *,
    object_type: str,
    name: str,
    transform,
) -> None:
    connection = sqlite3.connect(database)
    try:
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type=? AND name=?",
            (object_type, name),
        ).fetchone()
        assert sql is not None and isinstance(sql[0], str)
        changed = transform(sql[0])
        assert changed != sql[0]
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute(
            "UPDATE sqlite_master SET sql=? WHERE type=? AND name=?",
            (changed, object_type, name),
        )
        schema_version = int(connection.execute("PRAGMA schema_version").fetchone()[0])
        connection.execute(f"PRAGMA schema_version={schema_version + 1}")
        connection.execute("PRAGMA writable_schema=OFF")
        connection.commit()
    finally:
        connection.close()


def _weaken_schema_version_contract(database: Path) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.execute("ALTER TABLE schema_version RENAME TO old_schema_version")
        connection.execute("CREATE TABLE schema_version(version TEXT, applied_at BLOB)")
        connection.execute(
            "INSERT INTO schema_version(version,applied_at) "
            "SELECT CAST(version AS TEXT),CAST(applied_at AS BLOB) "
            "FROM old_schema_version"
        )
        connection.execute("DROP TABLE old_schema_version")
        connection.commit()
    finally:
        connection.close()


def test_dry_run_and_apply_upgrade_retained_v7_without_changing_source(tmp_path: Path):
    old_version = CURRENT_SCHEMA_VERSION - 1
    storage, source, registry = _fixture(tmp_path, maximum_version=old_version)
    source_bytes = source.read_bytes()
    pointer_bytes = (storage / "active-db.json").read_bytes()
    authority = _authority_rows(source)
    source_hashes = inventory_database(source)["table_hashes"]
    service = MigrationV7Service(registry)

    dry = service.dry_run(tmp_path)
    assert dry.status == "dry_run"
    assert dry.action == "upgrade"
    assert dry.validation["source_schema_version"] == old_version
    assert dry.validation["target_schema_version"] == CURRENT_SCHEMA_VERSION
    assert source.read_bytes() == source_bytes
    assert (storage / "active-db.json").read_bytes() == pointer_bytes

    applied = service.apply(tmp_path)
    active = resolve_active_database(storage)
    assert (applied.status, applied.action, applied.active_generation) == (
        "activated",
        "upgrade",
        2,
    )
    assert applied.checkpoints["schema_from"] == old_version
    assert applied.checkpoints["schema_to"] == CURRENT_SCHEMA_VERSION
    assert _schema_versions(active.path) >= REQUIRED_V7_SCHEMA_VERSIONS
    assert _authority_rows(active.path) == authority
    candidate_hashes = inventory_database(active.path)["table_hashes"]
    for table, digest in source_hashes.items():
        if table not in {"schema_version", "v7_migration_runs"}:
            assert candidate_hashes[table] == digest
    assert source.read_bytes() == source_bytes


def test_upgrade_backfills_native_outcomes_without_inventing_legacy_evidence(
    tmp_path: Path,
):
    import hashlib

    from daem0nmcp.event_store import (
        canonical_json_bytes,
        deterministic_id,
        export_event_bundle,
        import_event_bundle,
        sha256_json,
    )

    storage, source, registry = _fixture(tmp_path, maximum_version=32)
    workspace_id = registry.default.workspace_id
    expected_signals = []
    with sqlite3.connect(source) as connection:
        store = EventStore(connection)
        with mock.patch.object(EventStore, "_project_provenance"):
            for timestamp, worked in enumerate((True, False, None), start=20):
                state = _state()
                state.update(worked=worked, outcome="historical outcome")
                appended = store.append_and_project(
                    EventCommand(
                        workspace_id=workspace_id,
                        stream_id=MEMORY_ID,
                        stream_kind="memory",
                        event_type="memory.outcome_recorded",
                        occurred_at_us=timestamp,
                        recorded_at_us=timestamp,
                        actor_type="user",
                        payload={"record": state},
                    )
                )
                if worked is not None:
                    expected_signals.append(
                        (
                            workspace_id,
                            appended.event_id,
                            MEMORY_ID,
                            float(worked),
                            0.5,
                            timestamp,
                        )
                    )
            run_id = "con_" + "a" * 64
            store.append_and_project(
                EventCommand(
                    workspace_id=workspace_id,
                    stream_id="mem_" + "3" * 64,
                    stream_kind="memory",
                    event_type="memory.created",
                    occurred_at_us=25,
                    recorded_at_us=25,
                    actor_type="import",
                    actor_id="workspace-consolidation",
                    correlation_id=run_id,
                    payload={
                        "record": _state("historical consolidation"),
                        "provenance": {
                            "consolidation_run_id": run_id,
                            "source_workspace_id": "ws_" + "b" * 24,
                            "source_record_id": "mem_" + "b" * 64,
                            "source_event_id": "evt_" + "c" * 64,
                            "source_state_hash": "d" * 64,
                        },
                    },
                )
            )
            store.append_and_project(
                EventCommand(
                    workspace_id=workspace_id,
                    stream_id="mem_" + "4" * 64,
                    stream_kind="memory",
                    event_type="memory.created",
                    occurred_at_us=26,
                    recorded_at_us=26,
                    actor_type="client",
                    payload={
                        "record": _state("historical document"),
                        "semantic_namespace": "document-ingest-url",
                        "provenance": {
                            "url": "https://example.test/document",
                            "content_hash": "a" * 64,
                        },
                    },
                )
            )
        connection.execute(
            "CREATE TABLE memories (id INTEGER PRIMARY KEY,category TEXT,"
            "content TEXT,outcome TEXT,worked INTEGER)"
        )
        connection.execute(
            "INSERT INTO memories VALUES (7,'decision',"
            "'imported historical decision','legacy success',1)"
        )
        connection.row_factory = sqlite3.Row
        retained = connection.execute("SELECT * FROM memories WHERE id=7").fetchone()
        legacy = migration_v7_module._lossless_row(retained, "memories")
        legacy_state = migration_v7_module._memory_state(retained)
        legacy_run_id = "mig_" + "e" * 64
        legacy_record_id = deterministic_id(
            "mem", "memory", workspace_id, "legacy", "memories", "7"
        )
        connection.execute(
            "INSERT INTO v7_migration_runs (migration_run_id,workspace_id,"
            "source_db_sha256,source_schema_version,source_format_version,"
            "target_format_version,status,snapshot_name,candidate_name,"
            "source_inventory_json,created_at_us,updated_at_us) "
            "VALUES (?,?,?,15,6,7,'active','source.snapshot.db',"
            "'legacy.candidate.db',?,30,30)",
            (
                legacy_run_id,
                workspace_id,
                "e" * 64,
                canonical_json_bytes(
                    {"tables": {"memories": 1, "facts": 0, "memory_relationships": 0}}
                ).decode("utf-8"),
            ),
        )
        source_hash = sha256_json(legacy)
        empty_hash = hashlib.sha256(b"").hexdigest()
        for table in ("memories", "facts", "memory_relationships"):
            imported = int(table == "memories")
            connection.execute(
                "INSERT INTO v7_migration_checkpoints VALUES (?,?,?,?,?,1,30)",
                (
                    legacy_run_id,
                    table,
                    "7" if imported else None,
                    imported,
                    sha256_json([empty_hash, source_hash]) if imported else empty_hash,
                ),
            )
        legacy_event = store.append_and_project(
            EventCommand(
                workspace_id=workspace_id,
                stream_id=legacy_record_id,
                stream_kind="memory",
                event_type="legacy.memory_state_imported",
                occurred_at_us=30,
                recorded_at_us=30,
                actor_type="migration",
                correlation_id=legacy_run_id,
                payload={"legacy": legacy, "record": legacy_state},
            )
        )
        connection.execute(
            "INSERT INTO legacy_id_map VALUES (?,?,?,?,?,?,?,?)",
            (
                legacy_run_id,
                "memories",
                "7",
                workspace_id,
                "memory",
                legacy_record_id,
                source_hash,
                legacy_event.event_id,
            ),
        )
    authority = _authority_rows(source)

    MigrationV7Service(registry).apply(tmp_path)
    active = resolve_active_database(storage)
    assert _authority_rows(active.path) == authority
    with sqlite3.connect(active.path) as connection:
        signals = connection.execute(
            "SELECT workspace_id,event_id,record_id,reward,weight,recorded_at_us "
            "FROM memory_outcome_signals ORDER BY recorded_at_us,event_id"
        ).fetchall()
        assert signals == expected_signals
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_provenance_edges"
        ).fetchone() == (0,)
        bundle = export_event_bundle(connection, workspace_id)

    with sqlite3.connect(":memory:") as replay:
        replay.execute("PRAGMA foreign_keys=ON")
        for version, _description, statements in MIGRATIONS:
            if version in REQUIRED_V7_SCHEMA_VERSIONS:
                for statement in statements:
                    replay.execute(statement)
        import_event_bundle(replay, bundle, workspace_id)
        assert (
            replay.execute(
                "SELECT workspace_id,event_id,record_id,reward,weight,recorded_at_us "
                "FROM memory_outcome_signals ORDER BY recorded_at_us,event_id"
            ).fetchall()
            == signals
        )
        assert replay.execute(
            "SELECT COUNT(*) FROM memory_provenance_edges"
        ).fetchone() == (0,)


@pytest.mark.parametrize("payload", [{}, {"record": None}, {"record": []}])
def test_upgrade_rejects_invalid_memory_record_without_publishing_schema(
    tmp_path: Path, payload: dict[str, object]
):
    from daem0nmcp.event_store import canonical_json_bytes, sha256_json

    storage, source, registry = _fixture(tmp_path, maximum_version=32)
    workspace_id = registry.default.workspace_id
    with closing(sqlite3.connect(source)) as connection:
        previous_hash = connection.execute(
            "SELECT event_hash FROM memory_events WHERE stream_id=?", (MEMORY_ID,)
        ).fetchone()[0]
    envelope = {
        "workspace_id": workspace_id,
        "stream_id": MEMORY_ID,
        "stream_kind": "memory",
        "stream_version": 2,
        "event_type": "memory.created",
        "event_schema_version": 1,
        "occurred_at_us": 12,
        "recorded_at_us": 12,
        "actor_type": "user",
        "actor_id": None,
        "causation_event_id": None,
        "correlation_id": None,
        "payload_hash": sha256_json(payload),
        "previous_event_hash": previous_hash,
    }
    event_hash = sha256_json(envelope)
    columns = (
        "event_id",
        *envelope,
        "payload_json",
        "event_hash",
    )
    values = (
        "evt_" + event_hash,
        *envelope.values(),
        canonical_json_bytes(payload).decode("utf-8"),
        event_hash,
    )
    with closing(sqlite3.connect(source)) as connection:
        connection.execute(
            f"INSERT INTO memory_events ({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)})",
            values,
        )
        connection.commit()
    source_bytes = source.read_bytes()
    pointer_bytes = (storage / "active-db.json").read_bytes()

    with pytest.raises(MigrationV7Error) as raised:
        MigrationV7Service(registry).apply(tmp_path)

    assert raised.value.code == "SCHEMA_UPGRADE_FAILED"
    assert isinstance(raised.value.__cause__, ValueError)
    assert source.read_bytes() == source_bytes
    assert (storage / "active-db.json").read_bytes() == pointer_bytes
    candidate = next((storage / "migrations").rglob("candidate.db.partial"))
    assert max(_schema_versions(candidate)) == 32
    with closing(sqlite3.connect(candidate)) as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('memory_provenance_edges','memory_outcome_signals')"
            ).fetchall()
            == []
        )


@pytest.mark.parametrize("deleted", [False, True])
def test_upgrade_restores_missing_learning_parents_from_complete_replay(
    tmp_path: Path, deleted: bool
):
    from daem0nmcp.verification_v7 import verify_v7

    storage, source, registry, expected = _orphaned_learning_fixture(
        tmp_path, deleted=deleted
    )
    source_bytes = source.read_bytes()
    authority = _authority_rows(source)

    MigrationV7Service(registry).apply(tmp_path)

    active = resolve_active_database(storage)
    assert active.generation == 2
    assert source.read_bytes() == source_bytes
    assert _authority_rows(active.path) == authority
    with closing(sqlite3.connect(active.path)) as connection:
        for table, rows in expected.items():
            assert (
                connection.execute(f"SELECT * FROM {table} ORDER BY 1,2,3").fetchall()
                == rows
            )
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
    report = verify_v7(storage, registry.default.workspace_id)
    assert report["status"] == "verified"
    assert report["checks"]["canonical_replay"]["ok"]


@pytest.mark.parametrize(
    "corruption",
    ["memory_hash", "memory_gap", "workspace", "sequence", "governance", "federation"],
)
def test_learning_parent_recovery_requires_complete_valid_authority(
    tmp_path: Path, corruption: str
):
    from daem0nmcp.event_store import GovernanceEventCommand, GovernanceEventStore

    storage, source, registry, _expected = _orphaned_learning_fixture(tmp_path)
    workspace_id = registry.default.workspace_id
    with closing(sqlite3.connect(source)) as connection:
        if corruption == "federation":
            connection.execute(
                "INSERT INTO workspace_link_events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "evt_" + "f" * 64,
                    workspace_id,
                    "ws_" + "c" * 24,
                    1,
                    "workspace.linked",
                    "related",
                    "invalid hash",
                    8,
                    8,
                    None,
                    "f" * 64,
                ),
            )
        else:
            if corruption == "governance":
                rule_id = "rule_" + "a" * 64
                GovernanceEventStore(connection).append_and_project(
                    GovernanceEventCommand(
                        workspace_id=workspace_id,
                        stream_id=rule_id,
                        stream_kind="rule",
                        event_type="rule.created",
                        occurred_at_us=9,
                        recorded_at_us=9,
                        actor_type="user",
                        payload={
                            "rule_id": rule_id,
                            "trigger": "parent recovery",
                            "must_do": [],
                            "must_not": [],
                            "ask_first": [],
                            "warnings": [],
                            "priority": 0,
                            "enabled": True,
                            "created_at_us": 9,
                            "updated_at_us": 9,
                        },
                    )
                )
                trigger = "governance_events_no_update"
                sql = "UPDATE governance_events SET payload_json='{}'"
                parameters = ()
            elif corruption == "sequence":
                trigger = "session_update_sequence_no_delete"
                sql = (
                    "DELETE FROM session_update_sequence WHERE event_id IN "
                    "(SELECT event_id FROM memory_events WHERE stream_id=?)"
                )
                parameters = (MEMORY_ID,)
            elif corruption == "memory_gap":
                trigger = "memory_events_no_delete"
                sql = "DELETE FROM memory_events WHERE stream_id=? AND stream_version=1"
                parameters = (MEMORY_ID,)
            else:
                trigger = "memory_events_no_update"
                assignment = (
                    "payload_json='{}'"
                    if corruption == "memory_hash"
                    else "workspace_id='ws_" + "c" * 24 + "'"
                )
                sql = (
                    f"UPDATE memory_events SET {assignment} "
                    "WHERE stream_id=? AND stream_version=1"
                )
                parameters = (MEMORY_ID,)
            trigger_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                (trigger,),
            ).fetchone()[0]
            connection.execute(f'DROP TRIGGER "{trigger}"')
            connection.execute(sql, parameters)
            connection.execute(trigger_sql)
        connection.commit()
    source_bytes = source.read_bytes()
    pointer_bytes = (storage / "active-db.json").read_bytes()

    with pytest.raises(MigrationV7Error) as raised:
        MigrationV7Service(registry).apply(tmp_path)

    assert raised.value.code == "SCHEMA_UPGRADE_AUTHORITY_INVALID"
    assert source.read_bytes() == source_bytes
    assert (storage / "active-db.json").read_bytes() == pointer_bytes
    candidate = next((storage / "migrations").rglob("candidate.db.partial"))
    assert max(_schema_versions(candidate)) == 32
    with closing(sqlite3.connect(candidate)) as connection:
        assert (
            connection.execute(
                "SELECT record_id FROM memory_records WHERE record_id=?", (MEMORY_ID,)
            ).fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('memory_provenance_edges','memory_outcome_signals')"
            ).fetchall()
            == []
        )


def test_learning_parent_recovery_interruption_rolls_back_and_retries(tmp_path: Path):
    storage, source, registry, expected = _orphaned_learning_fixture(
        tmp_path, deleted=True
    )
    source_bytes = source.read_bytes()
    pointer_bytes = (storage / "active-db.json").read_bytes()

    def interrupt(stage, _details):
        if stage == "schema_upgrade_after_learning_parent_restore":
            raise MigrationInterrupted(stage)

    with pytest.raises(MigrationInterrupted):
        MigrationV7Service(registry, fault_injector=interrupt).apply(tmp_path)

    assert source.read_bytes() == source_bytes
    assert (storage / "active-db.json").read_bytes() == pointer_bytes
    candidate = next((storage / "migrations").rglob("candidate.db.partial"))
    assert max(_schema_versions(candidate)) == 32
    with closing(sqlite3.connect(candidate)) as connection:
        assert (
            connection.execute(
                "SELECT record_id FROM memory_records WHERE record_id=?", (MEMORY_ID,)
            ).fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('memory_provenance_edges','memory_outcome_signals')"
            ).fetchall()
            == []
        )

    MigrationV7Service(registry).apply(tmp_path)

    assert source.read_bytes() == source_bytes
    active = resolve_active_database(storage)
    assert active.generation == 2
    with closing(sqlite3.connect(active.path)) as connection:
        for table, rows in expected.items():
            assert (
                connection.execute(f"SELECT * FROM {table} ORDER BY 1,2,3").fetchall()
                == rows
            )


def test_pointerless_schema_32_retains_canonical_v7_recognition(tmp_path: Path):
    from daem0nmcp.storage_activation import has_canonical_v7_state

    storage, database, _registry = _fixture(tmp_path, maximum_version=32)
    (storage / "active-db.json").unlink()
    assert has_canonical_v7_state(database)


def test_upgrade_from_earliest_supported_v7_schema(tmp_path: Path):
    earliest = min(REQUIRED_V7_SCHEMA_VERSIONS)
    storage, source, registry = _fixture(
        tmp_path, maximum_version=earliest, with_event=False
    )

    result = MigrationV7Service(registry).apply(tmp_path)

    assert result.checkpoints["schema_from"] == earliest
    assert _schema_versions(resolve_active_database(storage).path) >= (
        REQUIRED_V7_SCHEMA_VERSIONS
    )
    assert max(_schema_versions(source)) == earliest


def test_actual_cli_upgrades_active_v7_schema(tmp_path: Path):
    old_version = CURRENT_SCHEMA_VERSION - 1
    storage, source, _registry = _fixture(tmp_path, maximum_version=old_version)
    source_bytes = source.read_bytes()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "daem0nmcp.cli",
            "--json",
            "--project-path",
            str(tmp_path),
            "migrate-v7",
            "--apply",
        ],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "activated"
    assert payload["source_format"] == 7
    assert payload["checkpoints"]["schema_from"] == old_version
    assert source.read_bytes() == source_bytes
    assert resolve_active_database(storage).generation == 2


def test_fresh_database_manager_schema_is_immediately_current(tmp_path: Path):
    storage = tmp_path / ".daem0nmcp" / "storage"

    async def initialize() -> None:
        manager = DatabaseManager(str(storage))
        try:
            await manager.init_db()
        finally:
            await manager.close()

    asyncio.run(initialize())
    registry = WorkspaceRegistry([tmp_path], default_root=tmp_path)

    result = MigrationV7Service(registry).dry_run(tmp_path)

    assert result.action == "already_active"
    assert result.validation["source_schema_version"] == CURRENT_SCHEMA_VERSION


def test_manager_start_leaves_an_older_v7_schema_to_migrate_v7(tmp_path: Path):
    storage, source, registry = _fixture(
        tmp_path, maximum_version=CURRENT_SCHEMA_VERSION - 1
    )
    source_bytes = source.read_bytes()
    pointer_bytes = (storage / "active-db.json").read_bytes()

    with pytest.raises(RuntimeError) as raised:
        _start_manager(storage)

    assert getattr(raised.value, "code", None) == "MIGRATION_REQUIRED"
    assert "migrate-v7 --apply" in str(raised.value)
    assert str(tmp_path) not in str(raised.value)
    assert source.read_bytes() == source_bytes
    assert (storage / "active-db.json").read_bytes() == pointer_bytes
    assert not (storage / "migrations").exists()

    MigrationV7Service(registry).apply(tmp_path)

    assert _start_manager(storage) == (7, 2)
    assert source.read_bytes() == source_bytes


def test_manager_start_does_not_attempt_the_learning_backfill_in_place(
    tmp_path: Path,
):
    storage, source, registry, _expected = _orphaned_learning_fixture(tmp_path)
    source_bytes = source.read_bytes()

    with pytest.raises(RuntimeError) as raised:
        _start_manager(storage)

    assert getattr(raised.value, "code", None) == "MIGRATION_REQUIRED"
    assert source.read_bytes() == source_bytes

    MigrationV7Service(registry).apply(tmp_path)

    assert _start_manager(storage) == (7, 2)


def test_actual_cli_status_does_not_upgrade_an_older_v7_schema(tmp_path: Path):
    storage, source, _registry = _fixture(
        tmp_path, maximum_version=CURRENT_SCHEMA_VERSION - 1
    )
    source_bytes = source.read_bytes()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "daem0nmcp.cli",
            "--json",
            "--project-path",
            str(tmp_path),
            "status",
        ],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        timeout=300,
        check=False,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    error = json.loads(result.stdout)["error"]
    assert error.startswith("MIGRATION_REQUIRED")
    assert "migrate-v7 --apply" in error
    assert source.read_bytes() == source_bytes
    assert resolve_active_database(storage).generation == 1


def test_semantic_physical_contract_accepts_historical_orm_spellings(
    tmp_path: Path,
):
    _storage, database, registry = _fixture(
        tmp_path, maximum_version=CURRENT_SCHEMA_VERSION, with_event=False
    )

    def historical_memory_events(sql: str) -> str:
        return (
            sql.replace(
                "event_id TEXT PRIMARY KEY",
                "event_id VARCHAR(68) NOT NULL PRIMARY KEY",
            )
            .replace(
                "event_schema_version INTEGER NOT NULL DEFAULT 1",
                "event_schema_version INTEGER NOT NULL",
            )
            .replace("payload_hash TEXT NOT NULL", "payload_hash VARCHAR(64) NOT NULL")
        )

    _rewrite_schema_object(
        database,
        object_type="table",
        name="memory_events",
        transform=historical_memory_events,
    )
    _rewrite_schema_object(
        database,
        object_type="table",
        name="retrieval_documents",
        transform=lambda sql: sql.replace(
            "archived INTEGER NOT NULL DEFAULT 0",
            "archived INTEGER NOT NULL DEFAULT '0'",
        ),
    )
    _rewrite_schema_object(
        database,
        object_type="index",
        name="idx_memory_records_briefing_type",
        transform=lambda _sql: (
            'CREATE INDEX "idx_memory_records_briefing_type" '
            'ON "memory_records"('
            '"workspace_id","record_type","updated_at_us" DESC,"record_id" DESC)'
        ),
    )
    _rewrite_schema_object(
        database,
        object_type="index",
        name="uq_projection_active",
        transform=lambda sql: sql.replace(
            "WHERE status='active'", "WHERE ((status='active'))"
        ),
    )

    result = MigrationV7Service(registry).dry_run(tmp_path)
    assert result.action == "already_active"


def test_check_contract_ignores_only_redundant_outer_parentheses():
    plain = "CREATE TABLE sample(value INTEGER CHECK(value=1 OR value=2))"
    wrapped = "CREATE TABLE sample(value INTEGER CHECK(((value=1 OR value=2))))"
    weakened = "CREATE TABLE sample(value INTEGER CHECK(value=1))"

    assert _check_contract(plain) == _check_contract(wrapped)
    assert _check_contract(plain) != _check_contract(weakened)


def test_missing_nonhistorical_default_is_rejected(tmp_path: Path):
    storage, database, registry = _fixture(
        tmp_path, maximum_version=CURRENT_SCHEMA_VERSION, with_event=False
    )
    pointer = (storage / "active-db.json").read_bytes()
    _rewrite_schema_object(
        database,
        object_type="table",
        name="retrieval_documents",
        transform=lambda sql: sql.replace(
            "visibility TEXT NOT NULL DEFAULT 'workspace'",
            "visibility TEXT NOT NULL",
        ),
    )

    with pytest.raises(MigrationV7Error) as raised:
        MigrationV7Service(registry).dry_run(tmp_path)
    assert raised.value.code == "V7_PHYSICAL_SCHEMA_INVALID"
    assert (storage / "active-db.json").read_bytes() == pointer


def test_index_direction_tamper_is_rejected_semantically(tmp_path: Path):
    storage, database, registry = _fixture(
        tmp_path, maximum_version=CURRENT_SCHEMA_VERSION, with_event=False
    )
    pointer = (storage / "active-db.json").read_bytes()
    _rewrite_schema_object(
        database,
        object_type="index",
        name="idx_memory_records_briefing_type",
        transform=lambda sql: sql.replace("updated_at_us DESC", "updated_at_us ASC"),
    )

    with pytest.raises(MigrationV7Error) as raised:
        MigrationV7Service(registry).apply(tmp_path)
    assert raised.value.code == "V7_PHYSICAL_SCHEMA_INVALID"
    assert (storage / "active-db.json").read_bytes() == pointer


def test_partial_index_predicate_tamper_is_rejected(tmp_path: Path):
    storage, database, registry = _fixture(
        tmp_path, maximum_version=CURRENT_SCHEMA_VERSION, with_event=False
    )
    pointer = (storage / "active-db.json").read_bytes()
    _rewrite_schema_object(
        database,
        object_type="index",
        name="uq_projection_active",
        transform=lambda sql: sql.replace("status='active'", "status='stale'"),
    )

    with pytest.raises(MigrationV7Error) as raised:
        MigrationV7Service(registry).dry_run(tmp_path)
    assert raised.value.code == "V7_PHYSICAL_SCHEMA_INVALID"
    assert (storage / "active-db.json").read_bytes() == pointer


def test_current_store_with_textual_unkeyed_schema_ledger_is_rejected(
    tmp_path: Path,
):
    storage, database, registry = _fixture(
        tmp_path, maximum_version=CURRENT_SCHEMA_VERSION, with_event=False
    )
    pointer = (storage / "active-db.json").read_bytes()
    _weaken_schema_version_contract(database)

    service = MigrationV7Service(registry)
    with pytest.raises(MigrationV7Error) as dry_error:
        service.dry_run(tmp_path)
    assert dry_error.value.code == "V7_PHYSICAL_SCHEMA_INVALID"
    with pytest.raises(MigrationV7Error) as apply_error:
        service.apply(tmp_path)
    assert apply_error.value.code == "V7_PHYSICAL_SCHEMA_INVALID"
    assert resolve_active_database(storage).path == database
    assert (storage / "active-db.json").read_bytes() == pointer


def test_published_candidate_with_textual_unkeyed_schema_ledger_is_rejected(
    tmp_path: Path,
):
    storage, source, registry = _fixture(
        tmp_path, maximum_version=CURRENT_SCHEMA_VERSION - 1, with_event=False
    )
    pointer = (storage / "active-db.json").read_bytes()

    def interrupt(stage: str, _details: dict[str, object]) -> None:
        if stage == "schema_upgrade_after_candidate_publish":
            raise MigrationInterrupted(stage)

    with pytest.raises(MigrationInterrupted):
        MigrationV7Service(registry, fault_injector=interrupt).apply(tmp_path)
    candidate = next((storage / "migrations" / "v7").glob("mig_*/candidate.db"))
    _weaken_schema_version_contract(candidate)

    with pytest.raises(MigrationV7Error) as raised:
        MigrationV7Service(registry).apply(tmp_path)
    assert raised.value.code == "ACTIVE_V7_INVALID"
    assert resolve_active_database(storage).path == source
    assert (storage / "active-db.json").read_bytes() == pointer


@pytest.mark.parametrize(
    "stage",
    [
        "schema_upgrade_after_run_directory",
        "schema_upgrade_after_snapshot",
        "schema_upgrade_after_candidate_copy",
        "schema_upgrade_after_schema",
        "schema_upgrade_after_verification",
        "schema_upgrade_after_candidate_publish",
        "schema_upgrade_before_pointer",
        "schema_upgrade_after_pointer",
    ],
)
def test_every_durable_interruption_retries_without_source_loss(
    tmp_path: Path, stage: str
):
    storage, source, registry = _fixture(
        tmp_path, maximum_version=CURRENT_SCHEMA_VERSION - 1
    )
    source_bytes = source.read_bytes()

    def interrupt(current: str, _details: dict[str, object]) -> None:
        if current == stage:
            raise MigrationInterrupted(stage)

    with pytest.raises(MigrationInterrupted):
        MigrationV7Service(registry, fault_injector=interrupt).apply(tmp_path)
    assert source.read_bytes() == source_bytes

    resumed = MigrationV7Service(registry).apply(tmp_path)
    active = resolve_active_database(storage)
    assert resumed.status in {"activated", "already_active"}
    assert active.format_version == 7
    assert active.generation == 2
    assert source.read_bytes() == source_bytes


def test_rollback_interruption_is_recovered_and_candidate_reactivates(tmp_path: Path):
    storage, source, registry = _fixture(
        tmp_path, maximum_version=CURRENT_SCHEMA_VERSION - 1
    )
    source_pointer = (storage / "active-db.json").read_bytes()
    service = MigrationV7Service(registry)
    activated = service.apply(tmp_path)
    candidate_pointer = (storage / "active-db.json").read_bytes()
    candidate = resolve_active_database(storage).path

    def interrupt(stage: str, _details: dict[str, object]) -> None:
        if stage == "after_rollback_pointer":
            raise MigrationInterrupted(stage)

    with pytest.raises(MigrationInterrupted):
        MigrationV7Service(registry, fault_injector=interrupt).rollback(tmp_path)
    rolled_pointer = resolve_active_database(storage)
    assert rolled_pointer.path == source
    assert rolled_pointer.format_version == 7
    assert (storage / "active-db.json").read_bytes() == source_pointer
    assert _run_status(candidate) == "active"

    recovered = service.rollback(tmp_path, activated.migration_run_id)
    assert recovered.action == "already_rolled_back"
    assert _run_status(candidate) == "rolled_back"
    reactivated = service.apply(tmp_path)
    assert reactivated.action == "reactivate"
    # Rolling back republishes the source's own generation-one pointer, so the
    # candidate returns under the pointer it was first activated with.
    assert (storage / "active-db.json").read_bytes() == candidate_pointer
    assert resolve_active_database(storage).generation == 2


def test_apply_after_an_interrupted_rollback_reactivates_the_candidate(tmp_path: Path):
    storage, _source, registry = _fixture(
        tmp_path, maximum_version=CURRENT_SCHEMA_VERSION - 1
    )
    activated = MigrationV7Service(registry).apply(tmp_path)
    candidate_pointer = (storage / "active-db.json").read_bytes()
    candidate = resolve_active_database(storage).path

    def interrupt(stage: str, _details: dict[str, object]) -> None:
        if stage == "after_rollback_pointer":
            raise MigrationInterrupted(stage)

    with pytest.raises(MigrationInterrupted):
        MigrationV7Service(registry, fault_injector=interrupt).rollback(tmp_path)
    assert _run_status(candidate) == "active"

    reactivated = MigrationV7Service(registry).apply(tmp_path)

    assert (reactivated.action, reactivated.migration_run_id) == (
        "reactivate",
        activated.migration_run_id,
    )
    assert (storage / "active-db.json").read_bytes() == candidate_pointer
    _assert_pointer_names_its_own_database(storage)


def test_an_interrupted_reactivation_is_completed_by_apply_or_rollback(
    tmp_path: Path,
):
    """Reactivation writes the pointer first, like a first activation."""

    def interrupt(stage: str, _details: dict[str, object]) -> None:
        if stage == "schema_upgrade_after_pointer":
            raise MigrationInterrupted(stage)

    for name, finish in (("apply", "apply"), ("rollback", "rollback")):
        root = tmp_path / name
        root.mkdir()
        storage, source, registry = _fixture(
            root, maximum_version=CURRENT_SCHEMA_VERSION - 1
        )
        source_pointer = (storage / "active-db.json").read_bytes()
        service = MigrationV7Service(registry)
        upgraded = service.apply(root)
        candidate = resolve_active_database(storage).path
        candidate_pointer = (storage / "active-db.json").read_bytes()
        service.rollback(root)

        with pytest.raises(MigrationInterrupted):
            MigrationV7Service(registry, fault_injector=interrupt).apply(root)
        assert (storage / "active-db.json").read_bytes() == candidate_pointer
        assert _run_status(candidate) == "rolled_back"

        if finish == "apply":
            resumed = service.apply(root)
            assert (resumed.status, resumed.migration_run_id) == (
                "activated",
                upgraded.migration_run_id,
            )
            assert (storage / "active-db.json").read_bytes() == candidate_pointer
            _assert_pointer_names_its_own_database(storage)
            assert _start_manager(storage) == (7, 2)
            assert service.rollback(root).status == "rolled_back"
        else:
            rolled = service.rollback(root)
            assert (rolled.status, rolled.migration_run_id) == (
                "rolled_back",
                upgraded.migration_run_id,
            )
            assert (storage / "active-db.json").read_bytes() == source_pointer
            assert _run_status(candidate) == "rolled_back"
            assert service.apply(root).action == "reactivate"
            assert _start_manager(storage) == (7, 2)


def test_a_rolled_back_run_under_a_foreign_pointer_is_not_completed(tmp_path: Path):
    """Only the pointer activation itself publishes proves an interruption."""

    storage, _source, registry = _fixture(
        tmp_path, maximum_version=CURRENT_SCHEMA_VERSION - 1
    )
    service = MigrationV7Service(registry)
    upgraded = service.apply(tmp_path)
    candidate = resolve_active_database(storage)
    service.rollback(tmp_path)
    write_active_pointer(
        storage,
        ActiveDatabasePointer(
            7,
            candidate.generation + 5,
            candidate.relative_path,
            candidate.previous_db,
            upgraded.migration_run_id,
        ),
    )

    with pytest.raises(MigrationV7Error) as refused:
        service.apply(tmp_path)

    assert refused.value.code == "ACTIVE_V7_INVALID"
    assert _run_status(candidate.path) == "rolled_back"


def test_the_run_under_a_rolled_back_schema_upgrade_can_be_rolled_back_too(
    tmp_path: Path,
):
    """An older schema is no reason to keep a store from its predecessor."""

    storage, source, registry = _fixture(
        tmp_path, maximum_version=CURRENT_SCHEMA_VERSION - 1
    )
    source_bytes = source.read_bytes()
    source_pointer = (storage / "active-db.json").read_bytes()
    first = MigrationV7Service(registry).apply(tmp_path)
    first_pointer = (storage / "active-db.json").read_bytes()
    first_candidate = resolve_active_database(storage).path

    with _as_the_next_build():
        service = MigrationV7Service(registry)
        second = service.apply(tmp_path)
        assert second.checkpoints["schema_from"] == CURRENT_SCHEMA_VERSION
        second_candidate = resolve_active_database(storage).path
        assert service.rollback(tmp_path).migration_run_id == second.migration_run_id
        assert (storage / "active-db.json").read_bytes() == first_pointer
        assert service.rollback(tmp_path).action == "already_rolled_back"

        under = service.rollback(tmp_path, first.migration_run_id)

        assert (under.status, under.migration_run_id, under.active_generation) == (
            "rolled_back",
            first.migration_run_id,
            1,
        )
        assert (storage / "active-db.json").read_bytes() == source_pointer
        assert source.read_bytes() == source_bytes
        assert _run_status(first_candidate) == "rolled_back"
        assert _run_status(second_candidate) == "rolled_back"
        assert service.rollback(tmp_path).action == "already_rolled_back"
        # A damaged predecessor is still refused, whatever its schema.
        again = service.apply(tmp_path)
        assert again.status == "activated"
        with closing(sqlite3.connect(resolve_active_database(storage).path)) as raw:
            raw.execute("DROP TABLE later_build_marker")
            raw.commit()
        with pytest.raises(MigrationV7Error) as damaged:
            service.rollback(tmp_path)
        assert damaged.value.code == "ACTIVE_V7_INVALID"


def test_a_v6_migration_under_a_rolled_back_schema_upgrade_returns_to_v6(
    tmp_path: Path,
):
    from tests.test_migrate_v7_end_to_end import _registry, _v6_database

    storage = _v6_database(tmp_path)
    source = storage / "daem0nmcp.db"
    source_bytes = source.read_bytes()
    registry = _registry(tmp_path)
    migrated = MigrationV7Service(registry).apply(tmp_path)
    migrated_candidate = resolve_active_database(storage).path

    with _as_the_next_build():
        service = MigrationV7Service(registry)
        upgraded = service.apply(tmp_path)
        assert (upgraded.action, upgraded.active_generation) == ("upgrade", 2)
        assert service.rollback(tmp_path).migration_run_id == (
            upgraded.migration_run_id
        )
        assert resolve_active_database(storage).path == migrated_candidate

        under = service.rollback(tmp_path, migrated.migration_run_id)

    assert (under.status, under.migration_run_id) == (
        "rolled_back",
        migrated.migration_run_id,
    )
    active = resolve_active_database(storage)
    assert (active.format_version, active.path) == (6, source)
    assert source.read_bytes() == source_bytes
    assert _run_status(migrated_candidate) == "rolled_back"
    # The build that migrated it takes the store forward again.
    assert MigrationV7Service(registry).apply(tmp_path).action == "reactivate"
    assert resolve_active_database(storage).path == migrated_candidate
    assert _start_manager(storage) == (7, 3)


def test_schema_upgrade_rollback_leaves_a_store_that_starts_and_upgrades_again(
    tmp_path: Path,
):
    from daem0nmcp.verification_v7 import verify_v7

    storage, source, registry = _fixture(
        tmp_path, maximum_version=CURRENT_SCHEMA_VERSION - 1
    )
    source_bytes = source.read_bytes()
    pointer_bytes = (storage / "active-db.json").read_bytes()
    service = MigrationV7Service(registry)
    upgraded = service.apply(tmp_path)
    candidate = resolve_active_database(storage).path
    assert _start_manager(storage) == (7, 2)

    rolled = service.rollback(tmp_path)

    assert (rolled.status, rolled.migration_run_id) == (
        "rolled_back",
        upgraded.migration_run_id,
    )
    _assert_pointer_names_its_own_database(storage)
    assert (storage / "active-db.json").read_bytes() == pointer_bytes
    assert rolled.active_generation == 1
    assert source.read_bytes() == source_bytes
    assert _run_status(candidate) == "rolled_back"

    # This build serves only the newer schema; it must say so, not rewrite
    # the database the rollback returned to.
    with pytest.raises(RuntimeError) as raised:
        _start_manager(storage)
    assert getattr(raised.value, "code", None) == "MIGRATION_REQUIRED"
    assert source.read_bytes() == source_bytes
    assert service.rollback(tmp_path).action == "already_rolled_back"
    assert service.dry_run(tmp_path).action == "reactivate"

    reactivated = service.apply(tmp_path)

    assert (reactivated.status, reactivated.action, reactivated.migration_run_id) == (
        "activated",
        "reactivate",
        upgraded.migration_run_id,
    )
    active = resolve_active_database(storage)
    assert (active.path, active.generation) == (candidate, 2)
    _assert_pointer_names_its_own_database(storage)
    assert verify_v7(storage, registry.default.workspace_id)["status"] == "verified"
    assert _start_manager(storage) == (7, 2)
    assert source.read_bytes() == source_bytes


def test_the_build_rolled_back_to_serves_the_store_again(tmp_path: Path):
    from daem0nmcp.verification_v7 import verify_v7

    storage, source, registry = _fixture(
        tmp_path, maximum_version=CURRENT_SCHEMA_VERSION
    )
    workspace_id = registry.default.workspace_id
    assert _start_manager(storage) == (7, 1)
    source_pointer = (storage / "active-db.json").read_bytes()
    events = _authority_rows(source)

    with _as_the_next_build() as later:
        service = MigrationV7Service(registry)
        upgraded = service.apply(tmp_path)
        assert upgraded.checkpoints["schema_to"] == later
        assert max(_schema_versions(resolve_active_database(storage).path)) == later
        assert service.rollback(tmp_path).status == "rolled_back"

    # This build is the one the rollback returned to.
    assert max(_schema_versions(source)) == CURRENT_SCHEMA_VERSION
    assert _start_manager(storage) == (7, 1)
    assert (storage / "active-db.json").read_bytes() == source_pointer
    assert _authority_rows(source) == events
    assert verify_v7(storage, workspace_id)["checks"]["active_pointer"]["ok"] is True
    assert MigrationV7Service(registry).apply(tmp_path).action == "already_active"


def test_rollback_refuses_a_predecessor_pointer_it_cannot_reproduce(tmp_path: Path):
    from daem0nmcp.event_store import canonical_json_bytes

    storage, _source, registry = _fixture(
        tmp_path, maximum_version=CURRENT_SCHEMA_VERSION - 1
    )
    service = MigrationV7Service(registry)
    upgraded = service.apply(tmp_path)
    candidate = resolve_active_database(storage).path
    pointer_bytes = (storage / "active-db.json").read_bytes()
    with closing(sqlite3.connect(candidate)) as connection:
        identity = json.loads(
            connection.execute(
                "SELECT source_inventory_json FROM v7_migration_runs "
                "WHERE migration_run_id=?",
                (upgraded.migration_run_id,),
            ).fetchone()[0]
        )
        identity["active_pointer"]["sha256"] = "0" * 64
        connection.execute(
            "UPDATE v7_migration_runs SET source_inventory_json=? "
            "WHERE migration_run_id=?",
            (
                canonical_json_bytes(identity).decode("utf-8"),
                upgraded.migration_run_id,
            ),
        )
        connection.commit()

    with pytest.raises(MigrationV7Error) as raised:
        service.rollback(tmp_path)

    assert raised.value.code == "ROLLBACK_STATE_INVALID"
    assert (storage / "active-db.json").read_bytes() == pointer_bytes
    assert _run_status(candidate) == "active"
    _assert_pointer_names_its_own_database(storage)


def _activate_a_projection_repair(storage: Path, database: Path, workspace_id: str):
    """Corrupt the lexical projection and let `verify-v7` activate its repair."""

    from daem0nmcp.retrieval.projections import LexicalProjectionBuilder
    from daem0nmcp.verification_v7 import verify_v7

    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        LexicalProjectionBuilder(connection).rebuild(workspace_id)
        connection.execute("UPDATE retrieval_documents SET content='locally corrupted'")
        connection.commit()
    repaired = verify_v7(storage, workspace_id, repair_projections=True)
    assert repaired["repair"]["activated"] is True
    return resolve_active_database(storage).migration_run_id


def test_projection_repair_rollback_leaves_a_store_every_reader_accepts(
    tmp_path: Path,
):
    from daem0nmcp.verification_v7 import verify_v7

    storage, source, registry = _fixture(
        tmp_path, maximum_version=CURRENT_SCHEMA_VERSION
    )
    workspace_id = registry.default.workspace_id
    pointer_bytes = (storage / "active-db.json").read_bytes()
    repair_run = _activate_a_projection_repair(storage, source, workspace_id)
    service = MigrationV7Service(registry)

    rolled = service.rollback(tmp_path)

    assert (rolled.status, rolled.migration_run_id) == ("rolled_back", repair_run)
    _assert_pointer_names_its_own_database(storage)
    assert (storage / "active-db.json").read_bytes() == pointer_bytes
    assert verify_v7(storage, workspace_id)["checks"]["active_pointer"]["ok"] is True
    assert service.rollback(tmp_path).action == "already_rolled_back"
    assert service.apply(tmp_path).action == "already_active"
    assert _start_manager(storage) == (7, 1)


def test_a_rolled_back_projection_repair_can_be_activated_again(tmp_path: Path):
    from daem0nmcp.verification_v7 import verify_v7

    storage, source, registry = _fixture(
        tmp_path, maximum_version=CURRENT_SCHEMA_VERSION
    )
    workspace_id = registry.default.workspace_id
    repair_run = _activate_a_projection_repair(storage, source, workspace_id)
    MigrationV7Service(registry).rollback(tmp_path)

    again = verify_v7(storage, workspace_id, repair_projections=True)

    # The restored pointer reproduces the repair's own identity, so the same
    # run is rebuilt rather than a second one beside it.
    assert again["repair"]["activated"] is True
    active = resolve_active_database(storage)
    assert (active.migration_run_id, active.generation) == (repair_run, 2)
    _assert_pointer_names_its_own_database(storage)
    assert verify_v7(storage, workspace_id)["status"] == "verified"


def test_rollback_unwinds_a_repair_stacked_on_a_schema_upgrade_one_run_at_a_time(
    tmp_path: Path,
):
    storage, source, registry = _fixture(
        tmp_path, maximum_version=CURRENT_SCHEMA_VERSION - 1
    )
    workspace_id = registry.default.workspace_id
    source_bytes = source.read_bytes()
    source_pointer = (storage / "active-db.json").read_bytes()
    service = MigrationV7Service(registry)
    upgraded = service.apply(tmp_path)
    upgrade_pointer = (storage / "active-db.json").read_bytes()
    repair_run = _activate_a_projection_repair(
        storage, resolve_active_database(storage).path, workspace_id
    )
    assert repair_run != upgraded.migration_run_id

    first = service.rollback(tmp_path)

    # The repair recorded only the digest of the pointer it replaced, which
    # named the upgrade run and its own predecessor.
    assert (first.status, first.migration_run_id) == ("rolled_back", repair_run)
    assert (storage / "active-db.json").read_bytes() == upgrade_pointer
    _assert_pointer_names_its_own_database(storage)
    assert _start_manager(storage) == (7, 2)
    # Repeating "latest" never unwinds a second run by accident.
    assert service.rollback(tmp_path).migration_run_id == repair_run
    assert (storage / "active-db.json").read_bytes() == upgrade_pointer
    with pytest.raises(MigrationV7Error) as unknown:
        service.rollback(tmp_path, "mig_" + "f" * 64)
    assert unknown.value.code == "ROLLBACK_NOT_ACTIVE"

    second = service.rollback(tmp_path, upgraded.migration_run_id)

    assert (second.status, second.migration_run_id) == (
        "rolled_back",
        upgraded.migration_run_id,
    )
    assert (storage / "active-db.json").read_bytes() == source_pointer
    assert source.read_bytes() == source_bytes
    assert service.rollback(tmp_path).migration_run_id == upgraded.migration_run_id


def test_a_source_changed_after_rollback_is_upgraded_into_a_new_candidate(
    tmp_path: Path,
):
    storage, source, registry = _fixture(
        tmp_path, maximum_version=CURRENT_SCHEMA_VERSION - 1
    )
    service = MigrationV7Service(registry)
    first = service.apply(tmp_path)
    first_candidate = resolve_active_database(storage).path
    service.rollback(tmp_path)
    with closing(sqlite3.connect(source)) as connection:
        EventStore(connection).append_and_project(
            EventCommand(
                workspace_id=registry.default.workspace_id,
                stream_id="mem_" + "2" * 64,
                stream_kind="memory",
                event_type="memory.created",
                occurred_at_us=20,
                recorded_at_us=21,
                actor_type="user",
                payload={"record": _state("written after the rollback")},
            )
        )
        connection.commit()

    assert service.dry_run(tmp_path).action == "upgrade"
    second = service.apply(tmp_path)

    assert second.action == "upgrade"
    assert second.migration_run_id != first.migration_run_id
    active = resolve_active_database(storage)
    assert (active.generation, active.previous_db) == (2, "daem0nmcp.db")
    _assert_pointer_names_its_own_database(storage)
    with closing(sqlite3.connect(active.path)) as connection:
        assert connection.execute("SELECT count(*) FROM memory_events").fetchone() == (
            2,
        )
    assert _run_status(first_candidate) == "rolled_back"


def _roll_back_the_way_earlier_builds_did(storage: Path, *, status: str) -> Path:
    """Leave the pointer that named the rolled-back run over its predecessor."""

    active = resolve_active_database(storage)
    assert active.previous_db is not None
    write_active_pointer(
        storage,
        ActiveDatabasePointer(
            7,
            active.generation + 1,
            active.previous_db,
            active.relative_path,
            active.migration_run_id,
        ),
    )
    with closing(sqlite3.connect(active.path)) as connection:
        connection.execute(
            "UPDATE v7_migration_runs SET status=? WHERE migration_run_id=?",
            (status, active.migration_run_id),
        )
        connection.commit()
    return active.path


@pytest.mark.parametrize("status", ["rolled_back", "active"])
def test_a_rollback_pointer_from_an_earlier_build_is_replaced_and_reactivates(
    tmp_path: Path, status: str
):
    storage, source, registry = _fixture(
        tmp_path, maximum_version=CURRENT_SCHEMA_VERSION - 1
    )
    source_bytes = source.read_bytes()
    source_pointer = (storage / "active-db.json").read_bytes()
    service = MigrationV7Service(registry)
    upgraded = service.apply(tmp_path)
    candidate_pointer = (storage / "active-db.json").read_bytes()
    candidate = _roll_back_the_way_earlier_builds_did(storage, status=status)

    again = service.rollback(tmp_path)

    assert (again.action, again.migration_run_id) == (
        "already_rolled_back",
        upgraded.migration_run_id,
    )
    assert (storage / "active-db.json").read_bytes() == source_pointer
    assert _run_status(candidate) == "rolled_back"
    assert source.read_bytes() == source_bytes

    reactivated = service.apply(tmp_path)

    assert reactivated.action == "reactivate"
    assert (storage / "active-db.json").read_bytes() == candidate_pointer


@pytest.mark.parametrize("status", ["rolled_back", "active"])
def test_apply_reactivates_straight_from_an_earlier_builds_rollback_pointer(
    tmp_path: Path, status: str
):
    storage, source, registry = _fixture(
        tmp_path, maximum_version=CURRENT_SCHEMA_VERSION - 1
    )
    source_bytes = source.read_bytes()
    service = MigrationV7Service(registry)
    upgraded = service.apply(tmp_path)
    candidate_pointer = (storage / "active-db.json").read_bytes()
    _roll_back_the_way_earlier_builds_did(storage, status=status)

    reactivated = service.apply(tmp_path)

    assert (reactivated.action, reactivated.migration_run_id) == (
        "reactivate",
        upgraded.migration_run_id,
    )
    assert (storage / "active-db.json").read_bytes() == candidate_pointer
    _assert_pointer_names_its_own_database(storage)
    assert source.read_bytes() == source_bytes


def test_a_workspace_an_earlier_build_left_unusable_is_recovered_by_apply(
    tmp_path: Path,
):
    from daem0nmcp.migrations.schema import run_migrations

    storage, source, registry = _fixture(
        tmp_path, maximum_version=CURRENT_SCHEMA_VERSION - 1
    )
    source_pointer = (storage / "active-db.json").read_bytes()
    service = MigrationV7Service(registry)
    service.apply(tmp_path)
    _roll_back_the_way_earlier_builds_did(storage, status="rolled_back")
    # The earlier build's next start then upgraded the rolled-back source in
    # place and stopped on a pointer naming a run that source never recorded.
    run_migrations(str(source), workspace_id=registry.default.workspace_id)
    with pytest.raises(RuntimeError, match="V7_ACTIVATION_INCOMPLETE"):
        _start_manager(storage)

    recovered = service.apply(tmp_path)

    assert recovered.action == "already_active"
    assert (storage / "active-db.json").read_bytes() == source_pointer
    _assert_pointer_names_its_own_database(storage)
    assert _start_manager(storage) == (7, 1)


def test_rolling_back_a_run_stacked_on_an_earlier_builds_rollback_pointer(
    tmp_path: Path,
):
    from daem0nmcp.migrations.v7_schema_upgrade import V7SchemaUpgradeService

    storage, source, registry = _fixture(
        tmp_path, maximum_version=CURRENT_SCHEMA_VERSION - 1
    )
    source_bytes = source.read_bytes()
    source_pointer = (storage / "active-db.json").read_bytes()
    service = MigrationV7Service(registry)
    first = service.apply(tmp_path)
    _roll_back_the_way_earlier_builds_did(storage, status="rolled_back")
    # The earlier build upgraded again from that pointer, so the second run
    # recorded it as the pointer to return to.
    with DatabaseFileLock(storage, "exclusive"):
        second = V7SchemaUpgradeService().apply(
            storage, registry.default.workspace_id, resolve_active_database(storage)
        )
    assert second is not None and second.migration_run_id != first.migration_run_id
    assert resolve_active_database(storage).generation == 4

    rolled = service.rollback(tmp_path)

    assert (rolled.status, rolled.migration_run_id, rolled.active_generation) == (
        "rolled_back",
        second.migration_run_id,
        1,
    )
    assert (storage / "active-db.json").read_bytes() == source_pointer
    _assert_pointer_names_its_own_database(storage)
    assert source.read_bytes() == source_bytes


def test_current_schema_is_database_and_pointer_byte_invariant(tmp_path: Path):
    storage, database, registry = _fixture(
        tmp_path, maximum_version=CURRENT_SCHEMA_VERSION
    )
    before_database = database.read_bytes()
    before_pointer = (storage / "active-db.json").read_bytes()
    service = MigrationV7Service(registry)

    assert service.dry_run(tmp_path).action == "already_active"
    assert service.apply(tmp_path).action == "already_active"
    assert database.read_bytes() == before_database
    assert (storage / "active-db.json").read_bytes() == before_pointer


def test_future_incomplete_and_corrupt_authority_fail_closed(tmp_path: Path):
    storage, source, registry = _fixture(
        tmp_path, maximum_version=CURRENT_SCHEMA_VERSION - 1
    )
    pointer = (storage / "active-db.json").read_bytes()
    source_bytes = source.read_bytes()
    with sqlite3.connect(source) as connection:
        connection.execute(
            "INSERT INTO memory_events(event_id,workspace_id,stream_id,stream_kind,"
            "stream_version,event_type,event_schema_version,occurred_at_us,"
            "recorded_at_us,actor_type,actor_id,causation_event_id,correlation_id,"
            "payload_json,payload_hash,previous_event_hash,event_hash) "
            "SELECT ?,workspace_id,stream_id,stream_kind,2,'memory.updated',1,12,12,"
            "'user',NULL,NULL,NULL,'{}',?,?,? FROM memory_events LIMIT 1",
            ("evt_" + "2" * 64, "3" * 64, "4" * 64, "5" * 64),
        )
        connection.commit()
    corrupt_bytes = source.read_bytes()
    with pytest.raises(MigrationV7Error) as corrupt:
        MigrationV7Service(registry).apply(tmp_path)
    assert corrupt.value.code == "SCHEMA_UPGRADE_AUTHORITY_INVALID"
    assert source.read_bytes() == corrupt_bytes
    assert (storage / "active-db.json").read_bytes() == pointer
    assert corrupt_bytes != source_bytes

    with sqlite3.connect(source) as connection:
        connection.execute(
            "INSERT INTO schema_version(version) VALUES (?)",
            (CURRENT_SCHEMA_VERSION + 1,),
        )
        connection.commit()
    with pytest.raises(MigrationV7Error) as future:
        MigrationV7Service(registry).dry_run(tmp_path)
    assert future.value.code == "FUTURE_V7_SCHEMA"


def test_incomplete_known_ledger_is_rejected(tmp_path: Path):
    _storage, source, registry = _fixture(
        tmp_path, maximum_version=CURRENT_SCHEMA_VERSION - 1, with_event=False
    )
    missing = CURRENT_SCHEMA_VERSION - 2
    with sqlite3.connect(source) as connection:
        connection.execute("DELETE FROM schema_version WHERE version=?", (missing,))
        connection.commit()

    with pytest.raises(MigrationV7Error) as raised:
        MigrationV7Service(registry).apply(tmp_path)
    assert raised.value.code == "ACTIVE_V7_INVALID"


def test_exclusive_lock_rejects_concurrent_upgrade(tmp_path: Path):
    storage, _source, registry = _fixture(
        tmp_path, maximum_version=CURRENT_SCHEMA_VERSION - 1, with_event=False
    )
    with DatabaseFileLock(storage, "exclusive"), pytest.raises(DatabaseInUseError):
        MigrationV7Service(registry).apply(tmp_path)


def test_disk_preflight_fails_before_creating_a_candidate(tmp_path: Path):
    storage, source, registry = _fixture(
        tmp_path, maximum_version=CURRENT_SCHEMA_VERSION - 1, with_event=False
    )
    pointer = (storage / "active-db.json").read_bytes()
    with (
        mock.patch(
            "daem0nmcp.migrations.v7_schema_upgrade.shutil.disk_usage",
            return_value=SimpleNamespace(free=0),
        ),
        pytest.raises(MigrationV7Error) as raised,
    ):
        MigrationV7Service(registry).apply(tmp_path)

    assert raised.value.code == "INSUFFICIENT_DISK_SPACE"
    assert resolve_active_database(storage).path == source
    assert (storage / "active-db.json").read_bytes() == pointer
    assert not (storage / "migrations").exists()


def test_malformed_published_candidate_is_never_activated(tmp_path: Path):
    storage, source, registry = _fixture(
        tmp_path, maximum_version=CURRENT_SCHEMA_VERSION - 1, with_event=False
    )

    def interrupt(stage: str, _details: dict[str, object]) -> None:
        if stage == "schema_upgrade_after_candidate_publish":
            raise MigrationInterrupted(stage)

    with pytest.raises(MigrationInterrupted):
        MigrationV7Service(registry, fault_injector=interrupt).apply(tmp_path)
    candidate = next((storage / "migrations" / "v7").glob("mig_*/candidate.db"))
    candidate.write_bytes(b"not sqlite")

    with pytest.raises(MigrationV7Error) as raised:
        MigrationV7Service(registry).apply(tmp_path)
    assert raised.value.code == "INVALID_SCHEMA_UPGRADE_CANDIDATE"
    assert resolve_active_database(storage).path == source


@pytest.mark.parametrize(
    "tamper",
    [
        "DROP INDEX idx_memory_records_briefing_type",
        "ALTER TABLE dense_projection_refs DROP COLUMN vector_sha256",
    ],
)
def test_published_candidate_with_incomplete_physical_schema_is_rejected(
    tmp_path: Path, tamper: str
):
    storage, source, registry = _fixture(
        tmp_path, maximum_version=CURRENT_SCHEMA_VERSION - 1, with_event=False
    )
    pointer = (storage / "active-db.json").read_bytes()

    def interrupt(stage: str, _details: dict[str, object]) -> None:
        if stage == "schema_upgrade_after_candidate_publish":
            raise MigrationInterrupted(stage)

    with pytest.raises(MigrationInterrupted):
        MigrationV7Service(registry, fault_injector=interrupt).apply(tmp_path)
    candidate = next((storage / "migrations" / "v7").glob("mig_*/candidate.db"))
    with sqlite3.connect(candidate) as connection:
        connection.execute(tamper)
        connection.commit()

    with pytest.raises(MigrationV7Error) as raised:
        MigrationV7Service(registry).apply(tmp_path)
    assert raised.value.code == "ACTIVE_V7_INVALID"
    assert resolve_active_database(storage).path == source
    assert (storage / "active-db.json").read_bytes() == pointer


@pytest.mark.parametrize(
    "tamper",
    [
        "DROP INDEX idx_memory_records_briefing_type",
        "ALTER TABLE dense_projection_refs DROP COLUMN vector_sha256",
    ],
)
def test_current_store_with_incomplete_physical_schema_is_not_already_active(
    tmp_path: Path, tamper: str
):
    storage, database, registry = _fixture(
        tmp_path, maximum_version=CURRENT_SCHEMA_VERSION, with_event=False
    )
    pointer = (storage / "active-db.json").read_bytes()
    with sqlite3.connect(database) as connection:
        connection.execute(tamper)
        connection.commit()

    service = MigrationV7Service(registry)
    with pytest.raises(MigrationV7Error) as dry_error:
        service.dry_run(tmp_path)
    assert dry_error.value.code == "V7_PHYSICAL_SCHEMA_INVALID"
    with pytest.raises(MigrationV7Error) as apply_error:
        service.apply(tmp_path)
    assert apply_error.value.code == "V7_PHYSICAL_SCHEMA_INVALID"
    assert resolve_active_database(storage).path == database
    assert (storage / "active-db.json").read_bytes() == pointer


def test_current_store_with_weakened_column_check_is_rejected(tmp_path: Path):
    storage, database, registry = _fixture(
        tmp_path, maximum_version=CURRENT_SCHEMA_VERSION, with_event=False
    )
    pointer = (storage / "active-db.json").read_bytes()
    connection = sqlite3.connect(database)
    try:
        sql = str(
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='dense_projection_refs'"
            ).fetchone()[0]
        )
        required = (
            "CHECK(vector_sha256 IS NULL OR (length(vector_sha256)=64 AND "
            "vector_sha256 NOT GLOB '*[^0-9a-f]*'))"
        )
        assert required in sql
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute(
            "UPDATE sqlite_master SET sql=? WHERE type='table' "
            "AND name='dense_projection_refs'",
            (sql.replace(required, "CHECK(1)"),),
        )
        schema_version = int(connection.execute("PRAGMA schema_version").fetchone()[0])
        connection.execute(f"PRAGMA schema_version={schema_version + 1}")
        connection.execute("PRAGMA writable_schema=OFF")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(MigrationV7Error) as raised:
        MigrationV7Service(registry).apply(tmp_path)
    assert raised.value.code == "V7_PHYSICAL_SCHEMA_INVALID"
    assert resolve_active_database(storage).path == database
    assert (storage / "active-db.json").read_bytes() == pointer


def test_dry_run_holds_shared_lock_before_resolving_pointer(tmp_path: Path):
    storage, _source, registry = _fixture(
        tmp_path, maximum_version=CURRENT_SCHEMA_VERSION - 1, with_event=False
    )
    service = MigrationV7Service(registry)
    with DatabaseFileLock(storage, "exclusive"), pytest.raises(DatabaseInUseError):
        service.dry_run(tmp_path)

    real_resolve = migration_v7_module.resolve_active_database
    observed_inside_lock = False

    def resolve_while_asserting_lock(path: Path):
        nonlocal observed_inside_lock
        with pytest.raises(DatabaseInUseError):
            DatabaseFileLock(storage, "exclusive").acquire()
        observed_inside_lock = True
        return real_resolve(path)

    with mock.patch.object(
        migration_v7_module,
        "resolve_active_database",
        side_effect=resolve_while_asserting_lock,
    ):
        assert service.dry_run(tmp_path).action == "upgrade"
    assert observed_inside_lock
