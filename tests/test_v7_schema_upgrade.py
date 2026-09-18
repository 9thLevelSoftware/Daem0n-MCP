"""Recoverable upgrades for already-active architecture-format 7 stores."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
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
    service = MigrationV7Service(registry)
    activated = service.apply(tmp_path)

    def interrupt(stage: str, _details: dict[str, object]) -> None:
        if stage == "after_rollback_pointer":
            raise MigrationInterrupted(stage)

    with pytest.raises(MigrationInterrupted):
        MigrationV7Service(registry, fault_injector=interrupt).rollback(tmp_path)
    rolled_pointer = resolve_active_database(storage)
    assert rolled_pointer.path == source
    assert rolled_pointer.format_version == 7

    recovered = service.rollback(tmp_path, activated.migration_run_id)
    assert recovered.action == "already_rolled_back"
    reactivated = service.apply(tmp_path)
    assert reactivated.action == "reactivate"
    assert resolve_active_database(storage).generation == 4


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
