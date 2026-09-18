from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from daem0nmcp.discovery_projection import (
    CodeEdgeProjectionSeed,
    CodeEntityProjectionSeed,
    DiscoveryProjectionBuilder,
)
from daem0nmcp.event_store import (
    EventCommand,
    EventStore,
    GovernanceEventCommand,
    GovernanceEventStore,
    deterministic_id,
    sha256_json,
)
from daem0nmcp.migrations import MIGRATIONS
from daem0nmcp.retrieval.projections import LexicalProjectionBuilder
from daem0nmcp.retrieval.specialized_projection import SpecializedProjectionBuilder
from daem0nmcp.storage_activation import (
    ActiveDatabasePointer,
    resolve_active_database,
    write_active_pointer,
)
from daem0nmcp.verification_v7 import VerificationV7Error, verify_v7
from daem0nmcp.workspace import WorkspaceRegistry

WORKSPACE_ID = "ws_0123456789abcdef01234567"
MEMORY_ID = "mem_" + "1" * 64


def _state(content: str = "authoritative state") -> dict[str, object]:
    return {
        "record_type": "decision",
        "legacy_type": None,
        "content": content,
        "rationale": None,
        "context": {},
        "tags": ["verification"],
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


def _storage(tmp_path: Path) -> tuple[Path, Path]:
    storage = tmp_path / ".daem0nmcp" / "storage"
    storage.mkdir(parents=True)
    database = storage / "daem0nmcp.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, "
            "applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        for version, _description, statements in MIGRATIONS:
            if version < 16:
                continue
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_version(version) VALUES (?)", (version,)
            )
        connection.commit()
        EventStore(connection).append_and_project(
            EventCommand(
                workspace_id=WORKSPACE_ID,
                stream_id=MEMORY_ID,
                stream_kind="memory",
                event_type="memory.created",
                occurred_at_us=10,
                recorded_at_us=11,
                actor_type="user",
                payload={"record": _state()},
            )
        )
        rule_id = deterministic_id("rel", "rule-test", WORKSPACE_ID)
        # Rule IDs use a five-character prefix; retain the deterministic digest.
        rule_id = "rule_" + rule_id.removeprefix("rel_")
        GovernanceEventStore(connection).append_and_project(
            GovernanceEventCommand(
                workspace_id=WORKSPACE_ID,
                stream_id=rule_id,
                stream_kind="rule",
                event_type="rule.created",
                occurred_at_us=12,
                recorded_at_us=12,
                actor_type="user",
                payload={
                    "rule_id": rule_id,
                    "trigger": "verify stores",
                    "must_do": ["verify"],
                    "must_not": [],
                    "ask_first": [],
                    "warnings": [],
                    "priority": 1,
                    "enabled": True,
                    "created_at_us": 12,
                    "updated_at_us": 12,
                },
            )
        )
        linked = "ws_89abcdef0123456701234567"
        envelope = {
            "workspace_id": WORKSPACE_ID,
            "linked_workspace_id": linked,
            "stream_version": 1,
            "event_type": "workspace.linked",
            "relationship": "related",
            "label": "peer",
            "occurred_at_us": 13,
            "recorded_at_us": 13,
            "previous_event_hash": None,
        }
        event_hash = sha256_json(envelope)
        connection.execute(
            "INSERT INTO workspace_link_events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "evt_" + event_hash,
                WORKSPACE_ID,
                linked,
                1,
                "workspace.linked",
                "related",
                "peer",
                13,
                13,
                None,
                event_hash,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    write_active_pointer(
        storage,
        ActiveDatabasePointer(7, 1, "daem0nmcp.db", None, None),
    )
    return storage, database


def _build_code_projection(database: Path) -> tuple[str, str, str]:
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        result = DiscoveryProjectionBuilder(
            connection, clock_us=lambda: 1000
        ).rebuild_code(
            WORKSPACE_ID,
            entities=(
                CodeEntityProjectionSeed("a", "function", "a.target", "a.py", 1, 1),
                CodeEntityProjectionSeed("b", "function", "b.caller", "b.py", 1, 2),
                CodeEntityProjectionSeed("c", "function", "c.other", "c.py", 1, 2),
            ),
            edges=(CodeEdgeProjectionSeed("b", "a", "call", 2),),
        )
        connection.commit()
    assert len(result.code_entity_ids) == 3
    return (
        result.code_entity_ids[0],
        result.code_entity_ids[1],
        result.code_entity_ids[2],
    )


def test_verify_v7_replays_every_authority_domain(tmp_path: Path) -> None:
    storage, _database = _storage(tmp_path)

    report = verify_v7(storage, WORKSPACE_ID)

    assert report["status"] == "verified"
    assert report["authority"] == {
        "federation_events": 1,
        "governance_events": 1,
        "memory_events": 1,
    }
    assert report["checks"]["canonical_replay"]["ok"] is True
    assert report["checks"]["active_pointer"]["generation"] == 1


def test_verify_v7_accepts_intact_active_code_generation(tmp_path: Path) -> None:
    storage, database = _storage(tmp_path)
    _build_code_projection(database)

    report = verify_v7(storage, WORKSPACE_ID)

    assert report["status"] == "verified"
    assert report["checks"]["projection_manifests"]["ok"] is True


def test_verify_v7_rejects_missing_code_edge_table(tmp_path: Path) -> None:
    storage, database = _storage(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE discovery_code_edges")
        connection.commit()

    report = verify_v7(storage, WORKSPACE_ID)

    assert report["status"] == "invalid"
    assert report["checks"]["schema"]["ok"] is False


@pytest.mark.parametrize(
    "mutation",
    [
        "delete_edge",
        "insert_edge",
        "modify_edge",
        "public_binding",
        "partition",
        "manifest",
    ],
)
def test_verify_v7_rejects_corrupt_code_generation(
    tmp_path: Path, mutation: str
) -> None:
    storage, database = _storage(tmp_path)
    target_id, source_id, other_id = _build_code_projection(database)
    with sqlite3.connect(database) as connection:
        if mutation == "delete_edge":
            connection.execute("DROP TRIGGER discovery_code_edges_no_delete")
            connection.execute("DELETE FROM discovery_code_edges")
        elif mutation == "insert_edge":
            identity = sha256_json(["code-edge", other_id, source_id, "reference", 1])
            connection.execute(
                "INSERT INTO discovery_code_edges(workspace_id,code_generation,"
                "source_code_entity_id,target_code_entity_id,edge_kind,source_line,"
                "identity_hash) VALUES (?,1,?,?,'reference',1,?)",
                (WORKSPACE_ID, other_id, source_id, identity),
            )
        elif mutation == "modify_edge":
            connection.execute("DROP TRIGGER discovery_code_edges_no_update")
            connection.execute("UPDATE discovery_code_edges SET source_line=3")
        elif mutation == "public_binding":
            connection.execute("DROP TRIGGER public_object_ids_no_update")
            connection.execute(
                "UPDATE public_object_ids SET source_key='s:tampered' "
                "WHERE public_id=?",
                (target_id,),
            )
        elif mutation == "partition":
            connection.execute("DROP TRIGGER discovery_projection_partitions_no_update")
            connection.execute(
                "UPDATE discovery_projection_partitions SET content_hash=? "
                "WHERE projection_name='code'",
                ("0" * 64,),
            )
        else:
            connection.execute(
                "UPDATE projection_manifests SET source_event_root_hash=? "
                "WHERE projection_name='code' AND status='active'",
                ("1" * 64,),
            )
        connection.commit()

    report = verify_v7(storage, WORKSPACE_ID)

    assert report["status"] == "invalid"
    assert report["checks"]["projection_manifests"]["ok"] is False


def test_verify_v7_detects_event_corruption_without_repairing(tmp_path: Path) -> None:
    storage, database = _storage(tmp_path)
    connection = sqlite3.connect(database)
    try:
        connection.execute("DROP TRIGGER memory_events_no_update")
        connection.execute("UPDATE memory_events SET payload_hash=?", ("0" * 64,))
        connection.commit()
    finally:
        connection.close()

    report = verify_v7(storage, WORKSPACE_ID)

    assert report["status"] == "invalid"
    assert report["checks"]["memory_events"]["ok"] is False
    with pytest.raises(VerificationV7Error, match="AUTHORITATIVE_STATE_INVALID"):
        verify_v7(storage, WORKSPACE_ID, repair_projections=True)
    assert resolve_active_database(storage).path == database


def test_verify_v7_detects_migration_mapping_provenance_mismatch(
    tmp_path: Path,
) -> None:
    storage, database = _storage(tmp_path)
    run_id = "mig_" + "2" * 64
    connection = sqlite3.connect(database)
    try:
        event_id = connection.execute("SELECT event_id FROM memory_events").fetchone()[
            0
        ]
        connection.execute(
            "INSERT INTO v7_migration_runs (migration_run_id,workspace_id,"
            "source_db_sha256,source_schema_version,source_format_version,"
            "target_format_version,status,snapshot_name,candidate_name,"
            "source_inventory_json,created_at_us,updated_at_us) "
            "VALUES (?,?,?,?,?,7,'active','source.snapshot.db','candidate.db',"
            "'{}',1,1)",
            (run_id, WORKSPACE_ID, "3" * 64, 15, 6),
        )
        connection.execute(
            "INSERT INTO legacy_id_map VALUES (?,?,?,?,?,?,?,?)",
            (
                run_id,
                "memories",
                "1",
                WORKSPACE_ID,
                "memory",
                MEMORY_ID,
                "4" * 64,
                event_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    report = verify_v7(storage, WORKSPACE_ID)

    assert report["status"] == "invalid"
    assert report["checks"]["migration_mappings"]["ok"] is False


@pytest.mark.parametrize("ledger", ["governance", "federation"])
def test_verify_v7_detects_non_memory_authority_corruption(
    tmp_path: Path, ledger: str
) -> None:
    storage, database = _storage(tmp_path)
    with sqlite3.connect(database) as connection:
        if ledger == "governance":
            connection.execute("DROP TRIGGER governance_events_no_update")
            connection.execute(
                "UPDATE governance_events SET payload_hash=?", ("0" * 64,)
            )
        else:
            connection.execute("DROP TRIGGER workspace_link_events_no_update")
            connection.execute(
                "UPDATE workspace_link_events SET event_hash=?", ("0" * 64,)
            )
        connection.commit()

    report = verify_v7(storage, WORKSPACE_ID)

    assert report["status"] == "invalid"
    assert report["checks"][f"{ledger}_events"]["ok"] is False


def test_verify_v7_rejects_undeclared_stale_active_manifest(tmp_path: Path) -> None:
    storage, database = _storage(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO projection_manifests (manifest_id,workspace_id,"
            "projection_name,generation,projection_version,status,source_event_count,"
            "source_event_root_hash,row_count,builder_version,details_json,started_at_us) "
            "VALUES (?,?, 'dense',1,1,'active',1,?,0,'test','{}',1)",
            ("prj_" + "5" * 64, WORKSPACE_ID, "6" * 64),
        )
        connection.commit()

    report = verify_v7(storage, WORKSPACE_ID)

    assert report["status"] == "invalid"
    assert report["checks"]["projection_manifests"]["ok"] is False


def test_verify_v7_rejects_active_manifest_with_false_row_count(tmp_path: Path) -> None:
    storage, database = _storage(tmp_path)
    with sqlite3.connect(database) as connection:
        event = connection.execute(
            "SELECT recorded_at_us,event_id,event_hash FROM memory_events"
        ).fetchone()
        connection.execute(
            "INSERT INTO projection_manifests (manifest_id,workspace_id,"
            "projection_name,generation,projection_version,status,source_event_count,"
            "source_event_root_hash,cursor_recorded_at_us,cursor_event_id,row_count,"
            "builder_version,details_json,started_at_us) VALUES "
            "(?,?,'dense',1,1,'active',1,?,?,?,999,'test',?,1)",
            (
                "prj_" + "7" * 64,
                WORKSPACE_ID,
                sha256_json([event[2]]),
                event[0],
                event[1],
                json.dumps({"provider_key": "test"}, separators=(",", ":")),
            ),
        )
        # Event roots hash the raw event-hash bytes, not their JSON string.
        import hashlib

        root = hashlib.sha256(bytes.fromhex(event[2])).hexdigest()
        connection.execute(
            "UPDATE projection_manifests SET source_event_root_hash=?", (root,)
        )
        connection.commit()

    report = verify_v7(storage, WORKSPACE_ID)

    assert report["status"] == "invalid"
    assert report["checks"]["projection_manifests"]["ok"] is False


def test_verify_v7_rejects_corrupt_local_lexical_projection_and_repairs_it(
    tmp_path: Path,
) -> None:
    storage, database = _storage(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        LexicalProjectionBuilder(connection).rebuild(WORKSPACE_ID)
        connection.execute("UPDATE retrieval_documents SET content='locally corrupted'")
        connection.commit()

    report = verify_v7(storage, WORKSPACE_ID)
    assert report["checks"]["projection_manifests"]["ok"] is False

    repaired = verify_v7(storage, WORKSPACE_ID, repair_projections=True)
    assert repaired["status"] == "repaired"
    assert verify_v7(storage, WORKSPACE_ID)["status"] == "verified"


@pytest.mark.parametrize(
    "projection_name", ["graph", "temporal", "procedure", "outcome"]
)
def test_verify_v7_rejects_each_corrupt_specialized_local_projection(
    tmp_path: Path, projection_name: str
) -> None:
    storage, database = _storage(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        SpecializedProjectionBuilder(connection).rebuild(WORKSPACE_ID, projection_name)
        connection.execute(
            "UPDATE projection_manifests SET row_count=row_count+1 WHERE workspace_id=? "
            "AND projection_name=? AND status='active'",
            (WORKSPACE_ID, projection_name),
        )
        connection.commit()

    assert (
        verify_v7(storage, WORKSPACE_ID)["checks"]["projection_manifests"]["ok"]
        is False
    )
    repaired = verify_v7(storage, WORKSPACE_ID, repair_projections=True)
    assert repaired["status"] == "repaired"
    assert verify_v7(storage, WORKSPACE_ID)["status"] == "verified"


def test_verify_v7_accepts_explicit_external_rebuild_required_manifest(
    tmp_path: Path,
) -> None:
    storage, database = _storage(tmp_path)
    with sqlite3.connect(database) as connection:
        event = connection.execute("SELECT event_hash FROM memory_events").fetchone()
        import hashlib

        root = hashlib.sha256(bytes.fromhex(event[0])).hexdigest()
        connection.execute(
            "INSERT INTO projection_manifests (manifest_id,workspace_id,projection_name,"
            "generation,projection_version,status,source_event_count,source_event_root_hash,"
            "row_count,builder_version,details_json,started_at_us) VALUES "
            "(?,?,'dense',1,1,'rebuild_required',1,?,0,'test',?,1)",
            (
                "prj_" + "e" * 64,
                WORKSPACE_ID,
                root,
                json.dumps(
                    {"rebuild_required_event_id": "evt_external"}, separators=(",", ":")
                ),
            ),
        )
        connection.commit()

    report = verify_v7(storage, WORKSPACE_ID)
    assert report["status"] == "verified"
    assert report["checks"]["projection_manifests"]["rebuild_required"] == 1


def test_verify_v7_requires_mapping_legacy_id_to_match_event_claim(
    tmp_path: Path,
) -> None:
    storage, database = _storage(tmp_path)
    run_id = "mig_" + "8" * 64
    target_id = "mem_" + "8" * 64
    legacy = {"table": "memories", "columns": [["id", 7]]}
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO v7_migration_runs (migration_run_id,workspace_id,"
            "source_db_sha256,source_schema_version,source_format_version,"
            "target_format_version,status,snapshot_name,candidate_name,"
            "source_inventory_json,created_at_us,updated_at_us) VALUES "
            "(?,?,?,?,7,7,'active','source.snapshot.db','candidate.db','{}',1,1)",
            (run_id, WORKSPACE_ID, "8" * 64, 23),
        )
        event = EventStore(connection).append_and_project(
            EventCommand(
                workspace_id=WORKSPACE_ID,
                stream_id=target_id,
                stream_kind="memory",
                event_type="legacy.memory_state_imported",
                occurred_at_us=20,
                recorded_at_us=20,
                actor_type="migration",
                correlation_id=run_id,
                payload={"legacy": legacy, "record": _state("legacy")},
            )
        )
        connection.execute(
            "INSERT INTO legacy_id_map VALUES (?,?,?,?,?,?,?,?)",
            (
                run_id,
                "memories",
                "999",
                WORKSPACE_ID,
                "memory",
                target_id,
                sha256_json(legacy),
                event.event_id,
            ),
        )
        connection.commit()

    report = verify_v7(storage, WORKSPACE_ID)
    assert report["checks"]["migration_mappings"]["ok"] is False


@pytest.mark.parametrize(
    ("source_table", "legacy"),
    [
        ("facts", {"table": "memories", "columns": [["id", 7]]}),
        ("memories", {"table": "memories"}),
    ],
)
def test_verify_v7_rejects_mapping_table_or_source_claim_omission(
    tmp_path: Path, source_table: str, legacy: dict[str, object]
) -> None:
    storage, database = _storage(tmp_path)
    run_id = "mig_" + "a" * 64
    target_id = "mem_" + "a" * 64
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO v7_migration_runs (migration_run_id,workspace_id,"
            "source_db_sha256,source_schema_version,source_format_version,"
            "target_format_version,status,snapshot_name,candidate_name,"
            "source_inventory_json,created_at_us,updated_at_us) VALUES "
            "(?,?,?,?,7,7,'active','source.snapshot.db','candidate.db','{}',1,1)",
            (run_id, WORKSPACE_ID, "a" * 64, 23),
        )
        event = EventStore(connection).append_and_project(
            EventCommand(
                workspace_id=WORKSPACE_ID,
                stream_id=target_id,
                stream_kind="memory",
                event_type="legacy.memory_state_imported",
                occurred_at_us=21,
                recorded_at_us=21,
                actor_type="migration",
                correlation_id=run_id,
                payload={"legacy": legacy, "record": _state("legacy")},
            )
        )
        connection.execute(
            "INSERT INTO legacy_id_map VALUES (?,?,?,?,?,?,?,?)",
            (
                run_id,
                source_table,
                "7",
                WORKSPACE_ID,
                "memory",
                target_id,
                sha256_json(legacy),
                event.event_id,
            ),
        )
        connection.commit()
    assert (
        verify_v7(storage, WORKSPACE_ID)["checks"]["migration_mappings"]["ok"] is False
    )


@pytest.mark.parametrize("run_status", ["active", "ready", "failed", "rolled_back"])
@pytest.mark.parametrize("retire_first", [False, True])
def test_verify_v7_matches_runtime_live_migration_claims_in_every_status(
    tmp_path: Path,
    run_status: str,
    retire_first: bool,
) -> None:
    storage, database = _storage(tmp_path)
    legacy = {"table": "memories", "columns": [["id", 7]]}
    with sqlite3.connect(database) as connection:
        for ordinal, letter in enumerate(("b", "c"), start=1):
            run_id = "mig_" + letter * 64
            target_id = "mem_" + letter * 64
            connection.execute(
                "INSERT INTO v7_migration_runs (migration_run_id,workspace_id,"
                "source_db_sha256,source_schema_version,source_format_version,"
                "target_format_version,status,snapshot_name,candidate_name,"
                "source_inventory_json,created_at_us,updated_at_us) VALUES "
                "(?,?,?,?,7,7,?,'source.snapshot.db','candidate.db','{}',1,1)",
                (run_id, WORKSPACE_ID, letter * 64, 23, run_status),
            )
            event = EventStore(connection).append_and_project(
                EventCommand(
                    workspace_id=WORKSPACE_ID,
                    stream_id=target_id,
                    stream_kind="memory",
                    event_type="legacy.memory_state_imported",
                    occurred_at_us=30 + ordinal,
                    recorded_at_us=30 + ordinal,
                    actor_type="migration",
                    correlation_id=run_id,
                    payload={"legacy": legacy, "record": _state(letter)},
                )
            )
            connection.execute(
                "INSERT INTO legacy_id_map VALUES (?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    "memories",
                    "7",
                    WORKSPACE_ID,
                    "memory",
                    target_id,
                    sha256_json(legacy),
                    event.event_id,
                ),
            )
        if retire_first:
            EventStore(connection).append_and_project(
                EventCommand(
                    workspace_id=WORKSPACE_ID,
                    stream_id="mem_" + "b" * 64,
                    stream_kind="memory",
                    event_type="memory.deleted",
                    occurred_at_us=40,
                    recorded_at_us=40,
                    actor_type="user",
                    payload={"record": {**_state("b"), "deleted_at_us": 40}},
                )
            )
        connection.commit()
    assert (
        verify_v7(storage, WORKSPACE_ID)["checks"]["migration_mappings"]["ok"]
        is retire_first
    )
    from contextlib import closing

    from daem0nmcp.event_store import (
        CompatibilityStreamError,
        resolve_compatibility_stream,
    )

    with closing(sqlite3.connect(database)) as connection:
        if retire_first:
            assert (
                resolve_compatibility_stream(
                    connection, WORKSPACE_ID, "memory", "memories", 7
                )
                == "mem_" + "c" * 64
            )
        else:
            with pytest.raises(CompatibilityStreamError, match="AMBIGUOUS"):
                resolve_compatibility_stream(
                    connection, WORKSPACE_ID, "memory", "memories", 7
                )


@pytest.mark.parametrize(
    "projection_name", ["lexical", "graph", "temporal", "procedure", "outcome"]
)
def test_verify_v7_rejects_renamed_manifest_and_rebuilds_known_projection(
    tmp_path: Path,
    projection_name: str,
) -> None:
    from contextlib import closing

    storage, database = _storage(tmp_path)
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        if projection_name == "lexical":
            LexicalProjectionBuilder(connection).rebuild(WORKSPACE_ID)
        else:
            SpecializedProjectionBuilder(connection).rebuild(
                WORKSPACE_ID, projection_name
            )
        connection.execute(
            "UPDATE projection_manifests SET projection_name='unsupported' WHERE projection_name=?",
            (projection_name,),
        )
        connection.commit()
    assert verify_v7(storage, WORKSPACE_ID)["status"] == "invalid"
    assert (
        verify_v7(storage, WORKSPACE_ID, repair_projections=True)["repair"]["activated"]
        is True
    )
    assert verify_v7(storage, WORKSPACE_ID)["status"] == "verified"


def test_repair_rebuilds_normally_invalidated_local_projection_once(
    tmp_path: Path,
) -> None:
    from contextlib import closing

    storage, database = _storage(tmp_path)
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        LexicalProjectionBuilder(connection).rebuild(WORKSPACE_ID)
        EventStore(connection).append_and_project(
            EventCommand(
                workspace_id=WORKSPACE_ID,
                stream_id="mem_" + "2" * 64,
                stream_kind="memory",
                event_type="memory.created",
                occurred_at_us=30,
                recorded_at_us=31,
                actor_type="user",
                payload={"record": _state("new memory")},
            )
        )
        connection.commit()
    stale = verify_v7(storage, WORKSPACE_ID)["checks"]["projection_manifests"]
    assert stale["local_rebuild_required"] == 1
    assert (
        verify_v7(storage, WORKSPACE_ID, repair_projections=True)["repair"]["activated"]
        is True
    )
    assert (
        verify_v7(storage, WORKSPACE_ID, repair_projections=True)["repair"]["activated"]
        is False
    )


def test_verify_v7_rejects_incomplete_legacy_migration_checkpoints(
    tmp_path: Path,
) -> None:
    storage, database = _storage(tmp_path)
    run_id = "mig_" + "d" * 64
    inventory = {"tables": {"memories": 0, "facts": 0, "memory_relationships": 0}}
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO v7_migration_runs (migration_run_id,workspace_id,"
            "source_db_sha256,source_schema_version,source_format_version,"
            "target_format_version,status,snapshot_name,candidate_name,"
            "source_inventory_json,created_at_us,updated_at_us) VALUES "
            "(?,?,?,?,6,7,'active','source.snapshot.db','candidate.db',?,1,1)",
            (
                run_id,
                WORKSPACE_ID,
                "d" * 64,
                15,
                json.dumps(inventory, separators=(",", ":")),
            ),
        )
        connection.commit()
    assert (
        verify_v7(storage, WORKSPACE_ID)["checks"]["migration_mappings"]["ok"] is False
    )


def test_repair_projections_activates_candidate_and_preserves_original(
    tmp_path: Path,
) -> None:
    storage, original = _storage(tmp_path)
    connection = sqlite3.connect(original)
    try:
        connection.execute("UPDATE memory_records SET content='corrupt projection'")
        connection.commit()
    finally:
        connection.close()

    report = verify_v7(storage, WORKSPACE_ID, repair_projections=True)

    active = resolve_active_database(storage)
    assert report["status"] == "repaired"
    assert report["repair"]["activated"] is True
    assert active.generation == 2
    assert active.path != original
    assert active.previous_db == "daem0nmcp.db"
    assert original.is_file()
    with sqlite3.connect(active.path) as connection:
        assert (
            connection.execute("SELECT content FROM memory_records").fetchone()[0]
            == "authoritative state"
        )
    assert verify_v7(storage, WORKSPACE_ID)["status"] == "verified"


def test_repair_scrubs_transient_portable_lease_and_preserves_staged_pages(
    tmp_path: Path,
) -> None:
    storage, original = _storage(tmp_path)
    session_id = "ipt_" + "7" * 64
    owner_token = "8" * 64
    session_directory = storage / "portable" / "v2" / "imports" / session_id
    page_path = session_directory / "pages" / "00000000.json"
    page_path.parent.mkdir(parents=True)
    page_path.write_bytes(b"{}")
    attempt = session_directory / "attempts" / owner_token
    attempt.mkdir(parents=True)
    (attempt / "validated-events.db").write_bytes(b"transient")
    with sqlite3.connect(original) as connection:
        connection.execute("UPDATE memory_records SET content='corrupt projection'")
        connection.execute(
            "INSERT INTO portable_transfer_sessions(session_id,workspace_id,direction,"
            "status,event_root_hash,manifest_json,manifest_hash,page_count,"
            "staged_page_count,total_bytes,created_at_us,updated_at_us,expires_at_us,"
            "completed_at_us) VALUES (?,?,'import','finalizing',?,?,?,1,1,"
            "4294967296,1,2,1000000000,NULL)",
            (
                session_id,
                WORKSPACE_ID,
                hashlib.sha256().hexdigest(),
                "{}",
                sha256_json({}),
            ),
        )
        connection.execute(
            "INSERT INTO portable_transfer_pages(session_id,page_index,page_kind,"
            "item_count,byte_count,page_hash,relative_path,received_at_us) "
            "VALUES (?,0,'events',0,2,?,'pages/00000000.json',1)",
            (session_id, hashlib.sha256(b"{}").hexdigest()),
        )
        connection.execute(
            "INSERT INTO portable_transfer_finalization_leases(session_id,owner_token,"
            "attempt_relative_path,acquired_at_us,renewed_at_us,expires_at_us) "
            "VALUES (?,?,?,1,2,1000000)",
            (session_id, owner_token, f"attempts/{owner_token}"),
        )
        connection.commit()

    repaired = verify_v7(storage, WORKSPACE_ID, repair_projections=True)

    assert repaired["status"] == "repaired"
    active = resolve_active_database(storage).path
    with sqlite3.connect(active) as connection:
        assert connection.execute(
            "SELECT status,total_bytes,staged_page_count,page_count "
            "FROM portable_transfer_sessions WHERE session_id=?",
            (session_id,),
        ).fetchone() == ("staging", 2, 1, 1)
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM portable_transfer_finalization_leases "
                "WHERE session_id=?",
                (session_id,),
            ).fetchone()[0]
            == 0
        )
        assert connection.execute(
            "SELECT relative_path,page_hash FROM portable_transfer_pages "
            "WHERE session_id=? AND page_index=0",
            (session_id,),
        ).fetchone() == (
            "pages/00000000.json",
            hashlib.sha256(b"{}").hexdigest(),
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    with sqlite3.connect(original) as connection:
        assert connection.execute(
            "SELECT status FROM portable_transfer_sessions WHERE session_id=?",
            (session_id,),
        ).fetchone() == ("finalizing",)
        assert connection.execute(
            "SELECT owner_token FROM portable_transfer_finalization_leases "
            "WHERE session_id=?",
            (session_id,),
        ).fetchone() == (owner_token,)
    assert page_path.read_bytes() == b"{}"
    assert not attempt.exists()


def test_repair_interruption_before_pointer_keeps_original_active(
    tmp_path: Path,
) -> None:
    storage, original = _storage(tmp_path)
    with sqlite3.connect(original) as connection:
        connection.execute("UPDATE memory_records SET content='corrupt projection'")
        connection.commit()

    def interrupt(stage: str, _details: dict[str, object]) -> None:
        if stage == "before_pointer":
            raise RuntimeError("injected interruption")

    with pytest.raises(RuntimeError, match="injected interruption"):
        verify_v7(
            storage,
            WORKSPACE_ID,
            repair_projections=True,
            fault_injector=interrupt,
        )

    active = resolve_active_database(storage)
    assert active.path == original
    assert active.generation == 1
    assert json.loads((storage / "active-db.json").read_text())["active_db"] == (
        "daem0nmcp.db"
    )

    resumed = verify_v7(storage, WORKSPACE_ID, repair_projections=True)
    assert resumed["status"] == "repaired"
    assert resolve_active_database(storage).generation == 2


def test_repair_retry_rebuilds_from_current_store_after_later_write(
    tmp_path: Path,
) -> None:
    storage, original = _storage(tmp_path)
    with sqlite3.connect(original) as connection:
        connection.execute("UPDATE memory_records SET content='corrupt projection'")
        connection.commit()

    def interrupt(stage: str, _details: dict[str, object]) -> None:
        if stage == "before_pointer":
            raise RuntimeError("injected interruption")

    with pytest.raises(RuntimeError, match="injected interruption"):
        verify_v7(
            storage, WORKSPACE_ID, repair_projections=True, fault_injector=interrupt
        )

    job_id = "job_" + "9" * 64
    payload = {"work": "preserve me"}
    with sqlite3.connect(original) as connection:
        connection.execute(
            "INSERT INTO background_jobs (job_id,workspace_id,job_type,idempotency_key,"
            "payload_json,payload_hash,status,available_at_us,created_at_us,updated_at_us) "
            "VALUES (?,?,?,?,?,?,'queued',1,1,1)",
            (
                job_id,
                WORKSPACE_ID,
                "test",
                "late-write",
                json.dumps(payload, separators=(",", ":")),
                sha256_json(payload),
            ),
        )
        connection.commit()

    repaired = verify_v7(storage, WORKSPACE_ID, repair_projections=True)
    assert repaired["status"] == "repaired"
    with sqlite3.connect(resolve_active_database(storage).path) as connection:
        assert connection.execute(
            "SELECT job_id FROM background_jobs WHERE job_id=?", (job_id,)
        ).fetchone() == (job_id,)
    assert len(list((storage / "migrations" / "v7").glob("mig_*"))) >= 2


@pytest.mark.parametrize(
    "stage",
    [
        "after_run_directory",
        "after_snapshot_copy",
        "after_snapshot_publish",
        "after_candidate_copy",
        "after_derived_replay",
        "after_manifest_rebuild",
        "after_run_row",
        "after_candidate_validation",
        "after_candidate_fsync",
        "before_pointer",
    ],
)
def test_repair_is_restartable_at_every_durable_construction_boundary(
    tmp_path: Path, stage: str
) -> None:
    storage, original = _storage(tmp_path)
    with sqlite3.connect(original) as connection:
        connection.execute("UPDATE memory_records SET content='corrupt projection'")
        connection.commit()

    def interrupt(current: str, _details: dict[str, object]) -> None:
        if current == stage:
            raise RuntimeError(stage)

    with pytest.raises(RuntimeError, match=stage):
        verify_v7(
            storage, WORKSPACE_ID, repair_projections=True, fault_injector=interrupt
        )
    assert resolve_active_database(storage).path == original

    repaired = verify_v7(storage, WORKSPACE_ID, repair_projections=True)
    assert repaired["status"] == "repaired"
    assert resolve_active_database(storage).generation == 2


def test_repair_rejects_linked_migration_parent_without_writing_outside(
    tmp_path: Path,
) -> None:
    storage, original = _storage(tmp_path)
    with sqlite3.connect(original) as connection:
        connection.execute("UPDATE memory_records SET content='corrupt projection'")
        connection.commit()
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = storage / "migrations"
    try:
        if os.name == "nt":
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(linked), str(outside)],
                check=False,
                capture_output=True,
                text=True,
            )
            if created.returncode != 0:
                pytest.skip("junction creation is unavailable")
        else:
            linked.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory links are unavailable")

    with pytest.raises(VerificationV7Error, match="UNSAFE_RECOVERY_PATH"):
        verify_v7(storage, WORKSPACE_ID, repair_projections=True)
    assert list(outside.iterdir()) == []
    assert resolve_active_database(storage).path == original


def test_verify_v7_cli_emits_machine_readable_report(tmp_path: Path) -> None:
    _storage(tmp_path)
    workspace_id = WorkspaceRegistry(default_root=tmp_path).default.workspace_id

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "daem0nmcp.cli",
            "--json",
            "--project-path",
            str(tmp_path),
            "verify-v7",
            "--workspace-id",
            workspace_id,
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["status"] == "verified"
    assert payload["checks"]["canonical_replay"]["ok"] is True
