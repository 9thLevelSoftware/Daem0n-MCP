"""Format-2 portable projection export/import regressions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from daem0nmcp.api.v7.operations import (
    CoreOperationDependencies,
    CoreOperationError,
    build_core_operations,
)
from daem0nmcp.api.v7.portable_projections import (
    PortableTransferError,
    PreparedImport,
    _merkle_levels,
    _merkle_proof,
    _verify_page_proof,
    claim_import_finalization,
    complete_import_session,
    create_export_session,
    prepare_vector_candidate,
    release_import_finalization,
    renew_import_finalization,
)
from daem0nmcp.covenant import (
    CapabilityAuthority,
    CovenantGate,
    CovenantStateStore,
    InvocationScope,
)
from daem0nmcp.event_store import (
    EventCommand,
    EventStore,
    canonical_json_bytes,
    sha256_json,
)
from daem0nmcp.migrations.v7 import MigrationV7Service
from daem0nmcp.retrieval.providers import (
    DENSE_BUILDER_VERSION,
    build_dense_point_payload,
    dense_builder_contract,
    dense_manifest_details,
)
from daem0nmcp.retrieval.runtime import ConfiguredEmbeddingEncoder
from daem0nmcp.storage_activation import resolve_active_database
from daem0nmcp.verification_v7 import verify_v7
from daem0nmcp.workspace import Workspace, WorkspaceRegistry

from .test_operations import NOW, WORKSPACE_ID, _Fixture, _record, _request


def _fixture(path: Path) -> _Fixture:
    path.mkdir()
    return _Fixture(path)


def _dependencies(
    fixture: _Fixture,
    config: object | None = None,
    *,
    local_status: str | None = None,
):
    scope = InvocationScope("principal", "session", fixture.root)
    gate = CovenantGate(
        state_store=CovenantStateStore(clock=lambda: 1_767_323_045),
        authority=CapabilityAuthority(
            secret=b"x" * 32,
            kid="test",
            clock=lambda: 1_767_323_045,
            ttl_seconds=300,
        ),
    )
    return CoreOperationDependencies(
        covenant_gate=gate,
        scope_provider=lambda: scope,
        storage_path_resolver=lambda _workspace: fixture.storage,
        clock=lambda: NOW,
        projection_config=config,
        projection_capability_statuses=(
            {"local": local_status or "ready"} if config else None
        ),
        cursor_secret=b"c" * 32,
    )


async def _all_pages(operation, fixture: _Fixture, **options):
    page = await operation(
        workspace=fixture.workspace,
        request=_request(
            "workspace_export",
            workspace_id=WORKSPACE_ID,
            page_byte_limit=65_536,
            **options,
        ),
    )
    pages = [page]
    while page.next_cursor is not None:
        page = await operation(
            workspace=fixture.workspace,
            request=_request(
                "workspace_export",
                workspace_id=WORKSPACE_ID,
                include_legacy_projection=options.get(
                    "include_legacy_projection", True
                ),
                include_vectors=options.get("include_vectors", False),
                export_session_id=page.export_session_id,
                page_index=page.page_index + 1,
                cursor=page.next_cursor,
                page_byte_limit=65_536,
            ),
        )
        pages.append(page)
    return pages


async def _import_pages(importer, fixture: _Fixture, pages):
    session_id = None
    for index, page in enumerate(pages):
        staged = await importer(
            workspace=fixture.workspace,
            request=_request(
                "workspace_import",
                workspace_id=WORKSPACE_ID,
                bundle=page.model_dump(mode="python"),
                import_session_id=session_id,
                finalize=False,
                idempotency_key=f"stage-portable-page-{index:04d}",
                preflight_token="token_value_for_test",
            ),
        )
        session_id = staged.import_session_id
    return await importer(
        workspace=fixture.workspace,
        request=_request(
            "workspace_import",
            workspace_id=WORKSPACE_ID,
            bundle=None,
            import_session_id=session_id,
            finalize=True,
            idempotency_key="finalize-portable-import-0001",
            preflight_token="token_value_for_test",
        ),
    )


async def test_paged_snapshot_is_frozen_and_legacy_is_replay_validated(tmp_path: Path):
    source = _fixture(tmp_path / "source")
    target = _fixture(tmp_path / "target")
    for index in range(6):
        source.append(
            f"portable-{index}-" + "x" * 18_000,
            occurred_at_us=100 + index,
            recorded_at_us=200 + index,
        )
    exporter = build_core_operations(_dependencies(source))["workspace_export"]
    first = await exporter(
        workspace=source.workspace,
        request=_request(
            "workspace_export",
            workspace_id=WORKSPACE_ID,
            page_byte_limit=65_536,
        ),
    )
    source.append("after snapshot", occurred_at_us=999, recorded_at_us=999)
    pages = [first]
    page = first
    while page.next_cursor is not None:
        page = await exporter(
            workspace=source.workspace,
            request=_request(
                "workspace_export",
                workspace_id=WORKSPACE_ID,
                export_session_id=page.export_session_id,
                page_index=page.page_index + 1,
                cursor=page.next_cursor,
                page_byte_limit=65_536,
            ),
        )
        pages.append(page)

    assert len(pages) >= 3
    assert {page.manifest_hash for page in pages} == {first.manifest_hash}
    assert first.manifest["event_count"] == 6
    assert any(page.page_kind == "legacy" for page in pages)
    assert all(len(page.model_dump_json().encode()) < 1_048_576 for page in pages)

    result = await _import_pages(
        build_core_operations(_dependencies(target))["workspace_import"],
        target,
        pages,
    )
    assert result.imported == 6
    assert result.status == "succeeded"
    connection = sqlite3.connect(target.database)
    try:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM memory_events WHERE workspace_id=?",
                (WORKSPACE_ID,),
            ).fetchone()[0]
            == 6
        )
        assert (
            connection.execute(
                "SELECT status FROM portable_transfer_sessions WHERE session_id=?",
                (result.import_session_id,),
            ).fetchone()[0]
            == "succeeded"
        )
    finally:
        connection.close()


async def test_tampered_page_never_changes_canonical_state(tmp_path: Path):
    source = _fixture(tmp_path / "source")
    target = _fixture(tmp_path / "target")
    source.append("portable", occurred_at_us=100, recorded_at_us=200)
    page = (
        await _all_pages(
            build_core_operations(_dependencies(source))["workspace_export"],
            source,
            include_legacy_projection=False,
        )
    )[0]
    tampered = page.model_dump(mode="python")
    tampered["events"][0]["payload"]["data"]["record"]["content"] = "changed"

    importer = build_core_operations(_dependencies(target))["workspace_import"]
    try:
        await importer(
            workspace=target.workspace,
            request=_request(
                "workspace_import",
                workspace_id=WORKSPACE_ID,
                bundle=tampered,
                finalize=True,
                idempotency_key="tampered-portable-page-0001",
                preflight_token="token_value_for_test",
            ),
        )
    except Exception as exc:
        assert getattr(exc, "code", "IMPORT_INVALID") == "IMPORT_INVALID"
    else:  # pragma: no cover - security invariant
        raise AssertionError("tampered page was accepted")
    connection = sqlite3.connect(target.database)
    try:
        assert (
            connection.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0] == 0
        )
    finally:
        connection.close()


async def test_legacy_identity_mapping_is_preserved(tmp_path: Path):
    source = _fixture(tmp_path / "source")
    target = _fixture(tmp_path / "target")
    run_id = "mig_" + "5" * 64
    legacy = {
        "table": "memories",
        "columns": [["id", 42], ["content", "mapped"]],
    }
    connection = sqlite3.connect(source.database)
    try:
        event = EventStore(connection).append_and_project(
            EventCommand(
                workspace_id=WORKSPACE_ID,
                stream_id="mem_" + "a" * 64,
                stream_kind="memory",
                event_type="legacy.memory_state_imported",
                occurred_at_us=100,
                recorded_at_us=200,
                actor_type="migration",
                correlation_id=run_id,
                expected_stream_version=1,
                payload={"legacy": legacy, "record": _record("mapped")},
            )
        )
        connection.commit()
    finally:
        connection.close()

    pages = await _all_pages(
        build_core_operations(_dependencies(source))["workspace_export"], source
    )
    result = await _import_pages(
        build_core_operations(_dependencies(target))["workspace_import"],
        target,
        pages,
    )
    assert result.imported == 1
    connection = sqlite3.connect(target.database)
    try:
        assert connection.execute(
            "SELECT target_id,source_row_hash,imported_event_id FROM legacy_id_map WHERE workspace_id=? "
            "AND source_table='memories' AND legacy_id='42'",
            (WORKSPACE_ID,),
        ).fetchone() == ("mem_" + "a" * 64, sha256_json(legacy), event.event_id)
        assert connection.execute(
            "SELECT status,source_format_version FROM v7_migration_runs "
            "WHERE migration_run_id=?",
            (run_id,),
        ).fetchone() == ("ready", 7)
    finally:
        connection.close()
    report = verify_v7(target.storage, WORKSPACE_ID)
    assert report["checks"]["migration_mappings"]["ok"] is True
    assert report["status"] == "verified"


async def test_event_only_v1_import_reconstructs_authoritative_migration_map(
    tmp_path: Path,
):
    source = _fixture(tmp_path / "source")
    target = _fixture(tmp_path / "target")
    run_id = "mig_" + "6" * 64
    legacy = {
        "table": "memories",
        "columns": [["id", "v1-7"], ["content", "event only"]],
    }
    connection = sqlite3.connect(source.database)
    try:
        event = EventStore(connection).append_and_project(
            EventCommand(
                workspace_id=WORKSPACE_ID,
                stream_id="mem_" + "b" * 64,
                stream_kind="memory",
                event_type="legacy.memory_state_imported",
                occurred_at_us=300,
                recorded_at_us=400,
                actor_type="migration",
                correlation_id=run_id,
                expected_stream_version=1,
                payload={"legacy": legacy, "record": _record("event only")},
            )
        )
        connection.commit()
    finally:
        connection.close()
    page = (
        await _all_pages(
            build_core_operations(_dependencies(source))["workspace_export"],
            source,
            include_legacy_projection=False,
        )
    )[0]
    v1_bundle = {
        "api_version": "7",
        "workspace_id": WORKSPACE_ID,
        "exported_at": page.exported_at,
        "root_hash": page.root_hash,
        "events": [item.model_dump(mode="python") for item in page.events],
        "legacy_projection_included": False,
        "vectors_included": False,
    }
    result = await build_core_operations(_dependencies(target))["workspace_import"](
        workspace=target.workspace,
        request=_request(
            "workspace_import",
            workspace_id=WORKSPACE_ID,
            bundle=v1_bundle,
            idempotency_key="portable-v1-event-only-0001",
            preflight_token="token_value_for_test",
        ),
    )
    assert result.imported == 1
    connection = sqlite3.connect(target.database)
    try:
        assert connection.execute(
            "SELECT target_id,source_row_hash,imported_event_id FROM legacy_id_map "
            "WHERE migration_run_id=? AND source_table='memories' AND legacy_id='v1-7'",
            (run_id,),
        ).fetchone() == ("mem_" + "b" * 64, sha256_json(legacy), event.event_id)
    finally:
        connection.close()
    report = verify_v7(target.storage, WORKSPACE_ID)
    assert report["status"] == "verified", report


async def test_event_replay_reconstructs_fact_relationship_and_placeholder_maps(
    tmp_path: Path,
):
    source = _fixture(tmp_path / "source")
    target = _fixture(tmp_path / "target")
    run_id = "mig_" + "7" * 64
    memory_id = "mem_" + "c" * 64
    placeholder_id = "mem_" + "d" * 64
    fact_id = "fact_" + "e" * 64
    relationship_id = "rel_" + "f" * 64
    commands = [
        EventCommand(
            workspace_id=WORKSPACE_ID,
            stream_id=memory_id,
            stream_kind="memory",
            event_type="legacy.memory_state_imported",
            occurred_at_us=1,
            recorded_at_us=1,
            actor_type="migration",
            correlation_id=run_id,
            expected_stream_version=1,
            payload={
                "legacy": {"table": "memories", "columns": [["id", 1]]},
                "record": _record("mapped memory"),
            },
        ),
        EventCommand(
            workspace_id=WORKSPACE_ID,
            stream_id=placeholder_id,
            stream_kind="memory",
            event_type="legacy.placeholder_created",
            occurred_at_us=1,
            recorded_at_us=1,
            actor_type="migration",
            correlation_id=run_id,
            expected_stream_version=1,
            payload={
                "legacy": {"table": "memories", "id": "99", "missing": True},
                "record": _record("missing memory"),
            },
        ),
        EventCommand(
            workspace_id=WORKSPACE_ID,
            stream_id=fact_id,
            stream_kind="fact",
            event_type="fact.asserted",
            occurred_at_us=1,
            recorded_at_us=1,
            actor_type="migration",
            correlation_id=run_id,
            expected_stream_version=1,
            payload={
                "legacy": {"table": "facts", "columns": [["id", 2]]},
                "fact": {
                    "subject_record_id": memory_id,
                    "predicate": "legacy.fact",
                    "object_kind": "text",
                    "object": "fact",
                    "legacy_type": None,
                    "confidence": 1.0,
                    "verification_count": 0,
                    "is_verified": False,
                    "evidence": [],
                    "metadata": {},
                    "valid_from_us": 1,
                    "valid_to_us": None,
                },
            },
        ),
        EventCommand(
            workspace_id=WORKSPACE_ID,
            stream_id=relationship_id,
            stream_kind="relationship",
            event_type="relationship.created",
            occurred_at_us=1,
            recorded_at_us=1,
            actor_type="migration",
            correlation_id=run_id,
            expected_stream_version=1,
            payload={
                "legacy": {
                    "table": "memory_relationships",
                    "columns": [["id", 3]],
                },
                "relationship": {
                    "source_record_id": memory_id,
                    "target_record_id": placeholder_id,
                    "relationship_type": "related_to",
                    "legacy_type": None,
                    "description": None,
                    "confidence": 1.0,
                    "metadata": {},
                    "valid_from_us": 1,
                    "valid_to_us": None,
                },
            },
        ),
    ]
    connection = sqlite3.connect(source.database)
    try:
        store = EventStore(connection)
        for command in commands:
            store.append_and_project(command)
        connection.commit()
    finally:
        connection.close()
    pages = await _all_pages(
        build_core_operations(_dependencies(source))["workspace_export"],
        source,
        include_legacy_projection=False,
    )
    await _import_pages(
        build_core_operations(_dependencies(target))["workspace_import"], target, pages
    )
    connection = sqlite3.connect(target.database)
    try:
        assert connection.execute(
            "SELECT source_table,legacy_id,target_kind,target_id FROM legacy_id_map "
            "WHERE migration_run_id=? ORDER BY source_table,legacy_id",
            (run_id,),
        ).fetchall() == [
            ("facts", "2", "fact", fact_id),
            ("memories", "1", "memory", memory_id),
            ("memory_relationships", "3", "relationship", relationship_id),
            (
                "memory_relationships.orphan",
                "memories:99",
                "placeholder",
                placeholder_id,
            ),
        ]
    finally:
        connection.close()
    report = verify_v7(target.storage, WORKSPACE_ID)
    assert report["status"] == "verified", report


async def test_real_v6_migration_roundtrips_without_database_internal_rows(
    tmp_path: Path,
):
    from tests.test_migrate_v7 import _create_legacy_database

    source_root = tmp_path / "legacy-source"
    source_storage = source_root / ".daem0nmcp" / "storage"
    source_storage.mkdir(parents=True)
    _create_legacy_database(source_storage / "daem0nmcp.db").close()
    registry = WorkspaceRegistry([source_root], default_root=source_root)
    MigrationV7Service(registry).apply(source_root, batch_size=1)
    workspace = registry.default
    active = resolve_active_database(source_storage)
    source = SimpleNamespace(
        root=source_root,
        storage=source_storage,
        workspace=workspace,
        database=active.path,
    )
    target = _fixture(tmp_path / "target")
    target.workspace = Workspace(workspace.workspace_id, target.root)

    exporter = build_core_operations(_dependencies(source))["workspace_export"]
    first = await exporter(
        workspace=source.workspace,
        request=_request(
            "workspace_export",
            workspace_id=workspace.workspace_id,
            page_byte_limit=65_536,
        ),
    )
    pages = [first]
    while pages[-1].next_cursor is not None:
        prior = pages[-1]
        pages.append(
            await exporter(
                workspace=source.workspace,
                request=_request(
                    "workspace_export",
                    workspace_id=workspace.workspace_id,
                    export_session_id=prior.export_session_id,
                    page_index=prior.page_index + 1,
                    cursor=prior.next_cursor,
                    page_byte_limit=65_536,
                ),
            )
        )
    importer = build_core_operations(_dependencies(target))["workspace_import"]
    session_id = None
    for index, page in enumerate(pages):
        staged = await importer(
            workspace=target.workspace,
            request=_request(
                "workspace_import",
                workspace_id=workspace.workspace_id,
                bundle=page.model_dump(mode="python"),
                import_session_id=session_id,
                finalize=False,
                idempotency_key=f"real-v6-page-{index:04d}",
                preflight_token="token_value_for_test",
            ),
        )
        session_id = staged.import_session_id
    result = await importer(
        workspace=target.workspace,
        request=_request(
            "workspace_import",
            workspace_id=workspace.workspace_id,
            import_session_id=session_id,
            finalize=True,
            idempotency_key="real-v6-finalize-0001",
            preflight_token="token_value_for_test",
        ),
    )
    assert result.imported == 6
    connection = sqlite3.connect(target.database)
    try:
        assert (
            connection.execute("SELECT COUNT(*) FROM legacy_id_map").fetchone()[0] == 5
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM v7_migration_runs WHERE source_format_version=7 "
                "AND status='ready'"
            ).fetchone()[0]
            == 1
        )
    finally:
        connection.close()
    assert verify_v7(target.storage, workspace.workspace_id)["status"] == "verified"


class _QdrantBackend:
    def __init__(self) -> None:
        self.collections: dict[str, list[object]] = {}


def _config(model: str = "portable-model") -> SimpleNamespace:
    return SimpleNamespace(
        qdrant_url="http://qdrant.invalid",
        qdrant_api_key="secret",
        qdrant_path=None,
        qdrant_timeout_seconds=10.0,
        qdrant_collection_prefix="test",
        embedding_model=model,
        embedding_dimension=3,
        embedding_backend="hash",
        embedding_query_prefix="search_query: ",
        embedding_document_prefix="search_document: ",
    )


def _vector_metadata(model: str = "source-model") -> dict[str, object]:
    encoder = ConfiguredEmbeddingEncoder(
        model_id=model,
        dimension=3,
        prefix="search_document: ",
        backend="hash",
    )
    details = dense_manifest_details(
        workspace_id=WORKSPACE_ID,
        provider_key="qdrant",
        generation=1,
        model_id=model,
        dimension=3,
        collection_prefix="test",
    )
    details.update(
        dense_builder_contract(
            build_config_hash=str(details["build_config_hash"]),
            encoder=encoder,
            model_id=model,
            dimension=3,
            query_prefix="search_query: ",
        )
    )
    return {
        **details,
        "projection_version": 1,
        "builder_version": DENSE_BUILDER_VERSION,
        "generation": 1,
        "source_event_count": 0,
        "source_event_root_hash": hashlib.sha256().hexdigest(),
        "row_count": 0,
        "payload_hash": hashlib.sha256().hexdigest(),
    }


def test_incompatible_vector_contract_requires_rebuild_without_external_write(
    tmp_path: Path,
):
    vectors = tmp_path / "vectors.jsonl"
    vectors.write_bytes(b"")
    prepared = PreparedImport(
        session_id="ipt_" + "1" * 64,
        database_path=tmp_path / "events.db",
        manifest={
            "event_count": 0,
            "event_root_hash": hashlib.sha256().hexdigest(),
            "vectors": _vector_metadata(),
        },
        legacy_path=None,
        vectors_path=vectors,
        page_count=1,
    )
    connection = sqlite3.connect(":memory:")
    try:
        candidate, diagnostic = prepare_vector_candidate(
            prepared,
            connection,
            WORKSPACE_ID,
            _config("different-model"),
            local_capability_ready=True,
        )
    finally:
        connection.close()
    assert candidate is None
    assert diagnostic == "VECTOR_REBUILD_REQUIRED"


def test_vector_client_boundary_rejects_incomplete_provider(tmp_path: Path):
    vectors = tmp_path / "vectors.jsonl"
    vectors.write_bytes(b"")
    prepared = PreparedImport(
        session_id="ipt_" + "2" * 64,
        database_path=tmp_path / "events.db",
        manifest={
            "event_count": 0,
            "event_root_hash": hashlib.sha256().hexdigest(),
            "vectors": _vector_metadata("portable-model"),
        },
        legacy_path=None,
        vectors_path=vectors,
        page_count=1,
    )

    class IncompleteClient:
        closed = False

        def close(self):
            self.closed = True

    client = IncompleteClient()
    connection = sqlite3.connect(":memory:")
    try:
        with (
            patch(
                "daem0nmcp.api.v7.portable_projections.create_qdrant_client",
                return_value=client,
            ),
            pytest.raises(PortableTransferError, match="CAPABILITY_DEGRADED"),
        ):
            prepare_vector_candidate(
                prepared,
                connection,
                WORKSPACE_ID,
                _config(),
                local_capability_ready=True,
            )
    finally:
        connection.close()
    assert client.closed


class _QdrantClient:
    def __init__(self, backend: _QdrantBackend) -> None:
        self.backend = backend

    def collection_exists(self, name):
        return name in self.backend.collections

    def create_collection(self, *, collection_name, vectors_config):
        assert vectors_config is not None
        self.backend.collections[collection_name] = []

    def delete_collection(self, name):
        self.backend.collections.pop(name, None)

    def upsert(self, *, collection_name, points, wait):
        assert wait is True
        for point in points:
            vector = [float(value) for value in _point_field(point, "vector")]
            norm = sum(value * value for value in vector) ** 0.5
            normalized = [value / norm for value in vector]
            if isinstance(point, dict):
                point["vector"] = normalized
            else:
                point.vector = normalized
        existing = {
            str(_point_field(point, "id")): point
            for point in self.backend.collections[collection_name]
        }
        existing.update({str(_point_field(point, "id")): point for point in points})
        self.backend.collections[collection_name] = list(existing.values())

    def count(self, *, collection_name, exact):
        assert exact is True
        return {"count": len(self.backend.collections[collection_name])}

    def scroll(
        self,
        *,
        collection_name,
        limit,
        offset=None,
        with_payload,
        with_vectors,
    ):
        values = self.backend.collections[collection_name]
        start = 0 if offset is None else int(offset)
        batch = values[start : start + limit]
        next_offset = start + len(batch)
        return batch, (None if next_offset >= len(values) else next_offset)

    def query_points(self, *, collection_name, **_kwargs):
        return SimpleNamespace(
            points=[
                SimpleNamespace(
                    id=_point_field(point, "id"),
                    payload=_point_field(point, "payload"),
                    score=1.0,
                )
                for point in self.backend.collections[collection_name]
            ]
        )

    def close(self):
        return None


def test_vector_provider_checkpoint_cleans_candidate_after_cancellation(
    tmp_path: Path,
):
    fixture = _fixture(tmp_path / "target")
    vectors = tmp_path / "vectors.jsonl"
    vectors.write_bytes(b"")
    prepared = PreparedImport(
        session_id="ipt_" + "3" * 64,
        database_path=tmp_path / "events.db",
        manifest={
            "event_count": 0,
            "event_root_hash": hashlib.sha256().hexdigest(),
            "vectors": _vector_metadata("portable-model"),
        },
        legacy_path=None,
        vectors_path=vectors,
        page_count=1,
    )
    backend = _QdrantBackend()

    class TrackingClient(_QdrantClient):
        closed = False
        deleted: list[str] = []

        def delete_collection(self, name):
            self.deleted.append(name)
            super().delete_collection(name)

        def close(self):
            self.closed = True

    client = TrackingClient(backend)
    checkpoints = 0

    def checkpoint() -> None:
        nonlocal checkpoints
        checkpoints += 1
        if checkpoints == 4:
            raise PortableTransferError("CANCELLED")

    connection = sqlite3.connect(fixture.database)
    try:
        with (
            patch(
                "daem0nmcp.api.v7.portable_projections.create_qdrant_client",
                return_value=client,
            ),
            pytest.raises(PortableTransferError, match="CANCELLED"),
        ):
            prepare_vector_candidate(
                prepared,
                connection,
                WORKSPACE_ID,
                _config(),
                local_capability_ready=True,
                checkpoint=checkpoint,
            )
    finally:
        connection.close()
    assert checkpoints == 4
    assert client.deleted
    assert client.closed
    assert backend.collections == {}


def _point_field(point: object, name: str):
    if isinstance(point, dict):
        return point.get(name)
    return getattr(point, name)


def _install_dense_source(
    fixture: _Fixture,
    backend: _QdrantBackend,
    config: SimpleNamespace,
    *,
    artifact_fingerprint: str | None = None,
    legacy_manifest: bool = False,
) -> None:
    connection = sqlite3.connect(fixture.database)
    try:
        event_rows = connection.execute(
            "SELECT event_hash FROM memory_events WHERE workspace_id=? ORDER BY event_id",
            (WORKSPACE_ID,),
        ).fetchall()
        record = connection.execute(
            "SELECT record_id,content_hash,source_event_id FROM memory_records "
            "WHERE workspace_id=? AND deleted_at_us IS NULL ORDER BY record_id LIMIT 1",
            (WORKSPACE_ID,),
        ).fetchone()
        assert record is not None
        encoder = ConfiguredEmbeddingEncoder(
            model_id=config.embedding_model,
            dimension=config.embedding_dimension,
            prefix=config.embedding_document_prefix,
            backend=config.embedding_backend,
        )
        encoder.artifact_fingerprint = artifact_fingerprint
        details = dense_manifest_details(
            workspace_id=WORKSPACE_ID,
            provider_key="qdrant",
            generation=1,
            model_id=config.embedding_model,
            dimension=config.embedding_dimension,
            collection_prefix=config.qdrant_collection_prefix,
        )
        details.update(
            dense_builder_contract(
                build_config_hash=str(details["build_config_hash"]),
                encoder=encoder,
                model_id=config.embedding_model,
                dimension=config.embedding_dimension,
                query_prefix=config.embedding_query_prefix,
            )
        )
        if legacy_manifest:
            details.pop("vector_format")
            details.pop("vector_space_hash")
        details["projection"] = "dense"
        digest = hashlib.sha256()
        for (event_hash,) in event_rows:
            digest.update(bytes.fromhex(event_hash))
        connection.execute(
            "INSERT INTO projection_manifests VALUES (?,?, 'dense',1,1,'active',?,"
            "?,NULL,NULL,1,?, ?,1,1,1)",
            (
                "prj_" + "1" * 64,
                WORKSPACE_ID,
                len(event_rows),
                digest.hexdigest(),
                DENSE_BUILDER_VERSION,
                canonical_json_bytes(details).decode(),
            ),
        )
        connection.execute(
            "INSERT INTO dense_projection_refs(workspace_id,provider_key,"
            "projection_generation,record_id,content_hash,model_id,dimension,state,"
            "updated_event_id,failure_code,updated_at_us) "
            "VALUES (?,?,1,?,?,?,?, 'ready',?,NULL,1)",
            (
                WORKSPACE_ID,
                "qdrant",
                record[0],
                record[1],
                config.embedding_model,
                config.embedding_dimension,
                record[2],
            ),
        )
        point_id, payload = build_dense_point_payload(
            workspace_id=WORKSPACE_ID,
            record_id=record[0],
            content_hash=record[1],
            projection_generation=1,
            model_id=config.embedding_model,
        )
        backend.collections[str(details["collection_name"])] = [
            {"id": point_id, "payload": payload, "vector": [0.1, 0.2, 0.3]}
        ]
        connection.commit()
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("legacy_manifest", "artifact_fingerprint"),
    ((True, None), (False, "a" * 64)),
)
async def test_vectors_capture_metadata_activate_and_query_candidate(
    tmp_path: Path,
    legacy_manifest: bool,
    artifact_fingerprint: str | None,
):
    source = _fixture(tmp_path / "source")
    target = _fixture(tmp_path / "target")
    source.append("vector source", occurred_at_us=100, recorded_at_us=200)
    config = _config()
    backend = _QdrantBackend()
    _install_dense_source(
        source,
        backend,
        config,
        artifact_fingerprint=artifact_fingerprint,
        legacy_manifest=legacy_manifest,
    )

    def encoder(purpose: str) -> ConfiguredEmbeddingEncoder:
        selected = ConfiguredEmbeddingEncoder(
            model_id=config.embedding_model,
            dimension=config.embedding_dimension,
            prefix=(
                config.embedding_query_prefix
                if purpose == "query"
                else config.embedding_document_prefix
            ),
            backend=config.embedding_backend,
        )
        selected.artifact_fingerprint = artifact_fingerprint
        selected._model = SimpleNamespace(
            encode=lambda texts, **_kwargs: [[0.1, 0.2, 0.3] for _ in texts]
        )
        return selected

    with (
        patch(
            "daem0nmcp.api.v7.portable_projections.create_qdrant_client",
            side_effect=lambda **_kwargs: _QdrantClient(backend),
        ),
        patch(
            "daem0nmcp.retrieval.runtime._embedding_encoder",
            side_effect=lambda _config, purpose: encoder(purpose),
        ),
    ):
        pages = await _all_pages(
            build_core_operations(_dependencies(source, config))["workspace_export"],
            source,
            include_legacy_projection=False,
            include_vectors=True,
        )
        assert any(page.page_kind == "vectors" for page in pages)
        vector_manifest = pages[0].manifest["vectors"]
        assert vector_manifest["model_id"] == "portable-model"
        assert vector_manifest["dimension"] == 3
        assert vector_manifest["generation"] == 1
        assert vector_manifest["vector_format"] == "qdrant-cosine-f32-le-v1"
        assert (vector_manifest["vector_space_hash"] is None) == (
            artifact_fingerprint is None
        )
        result = await _import_pages(
            build_core_operations(_dependencies(target, config))["workspace_import"],
            target,
            pages,
        )
    assert result.diagnostics == []
    connection = sqlite3.connect(target.database)
    try:
        active = connection.execute(
            "SELECT generation,row_count,details_json FROM projection_manifests "
            "WHERE workspace_id=? AND projection_name='dense' AND status='active'",
            (WORKSPACE_ID,),
        ).fetchone()
        assert active[:2] == (1, 1)
        assert json.loads(active[2])["portable_manifest_hash"] == pages[0].manifest_hash
        attestations = connection.execute(
            "SELECT vector_format,vector_sha256 FROM dense_projection_refs "
            "WHERE workspace_id=? AND projection_generation=?",
            (WORKSPACE_ID, active[0]),
        ).fetchall()
        assert attestations == [(None, None)]
    finally:
        connection.close()

    from daem0nmcp.retrieval.providers import DenseProvider
    from daem0nmcp.retrieval.types import RetrievalQuery

    query_encoder = encoder("query")
    document_encoder = encoder("document")
    provider = DenseProvider(
        connection_factory=lambda: sqlite3.connect(target.database),
        provider_key="qdrant",
        model_id=config.embedding_model,
        dimension=config.embedding_dimension,
        encoder=query_encoder,
        document_encoder=document_encoder,
        query_prefix=config.embedding_query_prefix,
        client=_QdrantClient(backend),
        collection_prefix=config.qdrant_collection_prefix,
    )
    result = await provider.search(
        RetrievalQuery(
            workspace_id=WORKSPACE_ID,
            text="vector source",
            limit=5,
            candidate_limit=5,
        ),
        5,
    )
    assert result.status == "ready"
    assert len(result.candidates) == 1


def test_compact_manifest_proofs_remain_transport_bounded_at_maximum_page_count():
    pages = [
        {
            "page_index": index,
            "page_kind": "events",
            "item_count": 1,
            "byte_count": 65_536,
            "page_hash": hashlib.sha256(str(index).encode()).hexdigest(),
        }
        for index in range(65_536)
    ]
    levels = _merkle_levels(pages)
    proof = _merkle_proof(levels, len(pages) - 1)
    manifest = {
        "bundle_version": 2,
        "workspace_id": WORKSPACE_ID,
        "event_root_hash": hashlib.sha256().hexdigest(),
        "event_count": len(pages),
        "legacy_projection": None,
        "vectors": None,
        "page_count": len(pages),
        "page_table_root": levels[-1][0],
    }
    assert len(canonical_json_bytes(manifest)) < 1_024
    assert len(proof) == 16
    assert _verify_page_proof(pages[-1], proof, manifest["page_table_root"])


async def test_export_accounts_physical_files_and_recovers_orphan_directory(
    tmp_path: Path,
):
    source = _fixture(tmp_path / "source")
    source.append("physical accounting", occurred_at_us=100, recorded_at_us=200)
    orphan = source.storage / "portable" / "v2" / "exports" / ("xpt_" + "f" * 64)
    orphan.mkdir(parents=True)
    (orphan / "orphan.bin").write_bytes(b"orphan")
    page = await build_core_operations(_dependencies(source))["workspace_export"](
        workspace=source.workspace,
        request=_request(
            "workspace_export",
            workspace_id=WORKSPACE_ID,
            include_legacy_projection=False,
        ),
    )
    assert not orphan.exists()
    directory = source.storage / "portable" / "v2" / "exports" / page.export_session_id
    physical = sum(
        path.stat().st_size for path in directory.rglob("*") if path.is_file()
    )
    assert not (directory / "source.snapshot.db").exists()
    connection = sqlite3.connect(source.database)
    try:
        tracked = connection.execute(
            "SELECT total_bytes,status FROM portable_transfer_sessions WHERE session_id=?",
            (page.export_session_id,),
        ).fetchone()
    finally:
        connection.close()
    assert tracked == (physical, "ready")


async def test_export_rejects_source_before_oversized_backup_is_created(
    tmp_path: Path,
):
    from daem0nmcp.api.v7 import portable_projections as portable

    source = _fixture(tmp_path / "source")
    source.append("pre-backup quota", occurred_at_us=100, recorded_at_us=200)
    allowance = 128 * 1024
    with (
        patch.object(portable, "MAX_SESSION_BYTES", allowance),
        patch.object(
            portable,
            "_sqlite_logical_size",
            return_value=(allowance, 4096),
        ),
        pytest.raises(CoreOperationError, match="TASK_REQUIRED"),
    ):
        await build_core_operations(_dependencies(source))["workspace_export"](
            workspace=source.workspace,
            request=_request(
                "workspace_export",
                workspace_id=WORKSPACE_ID,
                include_legacy_projection=False,
            ),
        )

    connection = sqlite3.connect(source.database)
    try:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM portable_transfer_sessions"
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()
    exports = source.storage / "portable" / "v2" / "exports"
    assert not exports.exists() or not any(exports.iterdir())


def test_export_cancellation_during_backup_removes_partial_reservation_and_files(
    tmp_path: Path,
):
    source = _fixture(tmp_path / "source")
    source.append("backup cancellation", occurred_at_us=100, recorded_at_us=200)
    calls = 0

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 2

    connection = sqlite3.connect(source.database)
    connection.row_factory = sqlite3.Row
    try:
        with pytest.raises(PortableTransferError, match="CANCELLED"):
            create_export_session(
                connection,
                source.storage,
                WORKSPACE_ID,
                include_legacy_projection=False,
                include_vectors=False,
                config=None,
                clock=NOW,
                cursor_secret=b"c" * 32,
                public_event=lambda event: event,
                page_byte_limit=65_536,
                cancelled=cancelled,
            )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM portable_transfer_sessions"
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()
    exports = source.storage / "portable" / "v2" / "exports"
    assert calls >= 2
    assert not exports.exists() or not any(exports.iterdir())


async def test_export_cancellation_after_publication_returns_resumable_receipt(
    tmp_path: Path,
):
    from daem0nmcp.api.v7 import operations as operations_module

    source = _fixture(tmp_path / "source")
    source.append("late cancellation", occurred_at_us=100, recorded_at_us=200)
    original = operations_module.create_export_session
    published = threading.Event()
    release = threading.Event()

    def block_after_publication(*args, **kwargs):
        result = original(*args, **kwargs)
        published.set()
        release.wait(2)
        return result

    with patch.object(
        operations_module, "create_export_session", side_effect=block_after_publication
    ):
        task = asyncio.create_task(
            build_core_operations(_dependencies(source))["workspace_export"](
                workspace=source.workspace,
                request=_request(
                    "workspace_export",
                    workspace_id=WORKSPACE_ID,
                    include_legacy_projection=False,
                ),
            )
        )
        assert await asyncio.to_thread(published.wait, 2)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        release.set()
        receipt = await task
    assert receipt.export_session_id is not None
    connection = sqlite3.connect(source.database)
    try:
        assert (
            connection.execute(
                "SELECT status FROM portable_transfer_sessions WHERE session_id=?",
                (receipt.export_session_id,),
            ).fetchone()[0]
            == "ready"
        )
    finally:
        connection.close()


async def test_disabled_local_profile_never_constructs_qdrant_client(tmp_path: Path):
    source = _fixture(tmp_path / "source")
    target = _fixture(tmp_path / "target")
    source.append("disabled source", occurred_at_us=100, recorded_at_us=200)
    backend = _QdrantBackend()
    config = _config()
    _install_dense_source(source, backend, config)
    with patch("daem0nmcp.api.v7.portable_projections.create_qdrant_client") as factory:
        with pytest.raises(CoreOperationError, match="CAPABILITY_DEGRADED"):
            await build_core_operations(
                _dependencies(source, config, local_status="disabled")
            )["workspace_export"](
                workspace=source.workspace,
                request=_request(
                    "workspace_export",
                    workspace_id=WORKSPACE_ID,
                    include_legacy_projection=False,
                    include_vectors=True,
                ),
            )
        factory.assert_not_called()
    with patch(
        "daem0nmcp.api.v7.portable_projections.create_qdrant_client",
        side_effect=lambda **_kwargs: _QdrantClient(backend),
    ):
        pages = await _all_pages(
            build_core_operations(_dependencies(source, config))["workspace_export"],
            source,
            include_legacy_projection=False,
            include_vectors=True,
        )
    with patch("daem0nmcp.api.v7.portable_projections.create_qdrant_client") as factory:
        result = await _import_pages(
            build_core_operations(
                _dependencies(target, config, local_status="disabled")
            )["workspace_import"],
            target,
            pages,
        )
        factory.assert_not_called()
    assert [item.code for item in result.diagnostics] == ["VECTOR_REBUILD_REQUIRED"]


async def test_vector_export_aborts_on_generation_lease_loss_and_releases_state(
    tmp_path: Path,
):
    from daem0nmcp.retrieval.dense_generation_gc import (
        DenseGenerationLifecycleError,
    )

    source = _fixture(tmp_path / "source")
    source.append("lease guarded vector", occurred_at_us=100, recorded_at_us=200)
    backend = _QdrantBackend()
    config = _config()
    _install_dense_source(source, backend, config)
    exporter = build_core_operations(_dependencies(source, config))["workspace_export"]

    with (
        patch(
            "daem0nmcp.api.v7.portable_projections.create_qdrant_client",
            side_effect=lambda **_kwargs: _QdrantClient(backend),
        ),
        patch(
            "daem0nmcp.api.v7.portable_projections.renew_generation_lease",
            side_effect=DenseGenerationLifecycleError("DENSE_GENERATION_LEASE_LOST"),
        ),
        pytest.raises(CoreOperationError, match="CAPABILITY_DEGRADED"),
    ):
        await exporter(
            workspace=source.workspace,
            request=_request(
                "workspace_export",
                workspace_id=WORKSPACE_ID,
                include_legacy_projection=False,
                include_vectors=True,
            ),
        )

    connection = sqlite3.connect(source.database)
    try:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM dense_generation_read_leases"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM portable_transfer_sessions WHERE direction='export'"
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def test_unsupported_vector_semantics_require_rebuild_before_external_write(
    tmp_path: Path,
):
    vectors = tmp_path / "vectors.jsonl"
    vectors.write_bytes(b"")
    for field, value in (("schema_version", 999), ("provider_key", "foreign")):
        metadata = _vector_metadata("portable-model")
        metadata[field] = value
        prepared = PreparedImport(
            session_id="ipt_" + "1" * 64,
            database_path=tmp_path / "events.db",
            manifest={
                "event_count": 0,
                "event_root_hash": hashlib.sha256().hexdigest(),
                "vectors": metadata,
            },
            legacy_path=None,
            vectors_path=vectors,
            page_count=1,
        )
        with patch(
            "daem0nmcp.api.v7.portable_projections.create_qdrant_client"
        ) as factory:
            candidate, diagnostic = prepare_vector_candidate(
                prepared,
                sqlite3.connect(":memory:"),
                WORKSPACE_ID,
                _config(),
                local_capability_ready=True,
            )
        factory.assert_not_called()
        assert candidate is None
        assert diagnostic == "VECTOR_REBUILD_REQUIRED"
    malformed = _vector_metadata("portable-model")
    malformed["builder_contract_hash"] = "not-a-hash"
    prepared.manifest["vectors"] = malformed
    with patch("daem0nmcp.api.v7.portable_projections.create_qdrant_client") as factory:
        with pytest.raises(PortableTransferError, match="IMPORT_INVALID"):
            prepare_vector_candidate(
                prepared,
                sqlite3.connect(":memory:"),
                WORKSPACE_ID,
                _config(),
                local_capability_ready=True,
            )
        factory.assert_not_called()


async def test_vector_merge_does_not_activate_partial_source_projection(tmp_path: Path):
    source = _fixture(tmp_path / "source")
    target = _fixture(tmp_path / "target")
    source.append("source vector", occurred_at_us=100, recorded_at_us=200)
    connection = sqlite3.connect(target.database)
    try:
        EventStore(connection).append_and_project(
            EventCommand(
                workspace_id=WORKSPACE_ID,
                stream_id="mem_" + "b" * 64,
                stream_kind="memory",
                event_type="memory.created",
                occurred_at_us=101,
                recorded_at_us=201,
                actor_type="client",
                payload={"record": _record("target extra")},
            )
        )
        connection.commit()
    finally:
        connection.close()
    backend = _QdrantBackend()
    config = _config()
    _install_dense_source(source, backend, config)
    with patch(
        "daem0nmcp.api.v7.portable_projections.create_qdrant_client",
        side_effect=lambda **_kwargs: _QdrantClient(backend),
    ):
        pages = await _all_pages(
            build_core_operations(_dependencies(source, config))["workspace_export"],
            source,
            include_legacy_projection=False,
            include_vectors=True,
        )
        result = await _import_pages(
            build_core_operations(_dependencies(target, config))["workspace_import"],
            target,
            pages,
        )
    assert [item.code for item in result.diagnostics] == ["VECTOR_REBUILD_REQUIRED"]
    connection = sqlite3.connect(target.database)
    try:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM projection_manifests WHERE workspace_id=? "
                "AND projection_name='dense' AND status='active'",
                (WORKSPACE_ID,),
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


async def test_import_validation_reserves_capacity_before_attempt_database(
    tmp_path: Path,
):
    from daem0nmcp.api.v7 import portable_projections as portable

    source = _fixture(tmp_path / "source")
    target = _fixture(tmp_path / "target")
    source.append("bounded validation", occurred_at_us=100, recorded_at_us=200)
    pages = await _all_pages(
        build_core_operations(_dependencies(source))["workspace_export"],
        source,
        include_legacy_projection=False,
    )
    importer = build_core_operations(_dependencies(target))["workspace_import"]
    session_id = None
    for index, page in enumerate(pages):
        staged = await importer(
            workspace=target.workspace,
            request=_request(
                "workspace_import",
                workspace_id=WORKSPACE_ID,
                bundle=page.model_dump(mode="python"),
                import_session_id=session_id,
                finalize=False,
                idempotency_key=f"quota-stage-{index:04d}",
                preflight_token="token_value_for_test",
            ),
        )
        session_id = staged.import_session_id
    assert session_id is not None
    directory = target.storage / "portable" / "v2" / "imports" / session_id
    physical = sum(
        path.stat().st_size for path in directory.rglob("*") if path.is_file()
    )
    with (
        patch.object(portable, "MAX_SESSION_BYTES", physical + 32 * 1024),
        pytest.raises(CoreOperationError, match="TASK_REQUIRED"),
    ):
        await importer(
            workspace=target.workspace,
            request=_request(
                "workspace_import",
                workspace_id=WORKSPACE_ID,
                bundle=None,
                import_session_id=session_id,
                finalize=True,
                idempotency_key="quota-finalize-0001",
                preflight_token="token_value_for_test",
            ),
        )
    connection = sqlite3.connect(target.database)
    try:
        assert (
            connection.execute(
                "SELECT status FROM portable_transfer_sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()[0]
            == "staging"
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM portable_transfer_finalization_leases"
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()
    attempts = directory / "attempts"
    assert not attempts.exists() or not any(attempts.iterdir())


@pytest.mark.parametrize("stable_error", [False, True])
@pytest.mark.parametrize("other_workspace_active", [False, True])
def test_failed_candidate_cleanup_survives_for_successor(
    tmp_path: Path, stable_error: bool, other_workspace_active: bool
):
    from daem0nmcp.api.v7.portable_projections import _reclaim_stale_vector_artifacts

    target = _fixture(tmp_path / "target")
    session_id = "ipt_" + "8" * 64
    now_us = int(NOW.timestamp() * 1_000_000)
    manifest = {
        "event_count": 0,
        "event_root_hash": hashlib.sha256().hexdigest(),
        "vectors": _vector_metadata("portable-model"),
    }
    backend = _QdrantBackend()

    class FailingClient(_QdrantClient):
        closed = False

        def count(self, **kwargs):
            if stable_error:
                raise PortableTransferError("CANCELLED")
            raise RuntimeError("provider validation failed")

        def delete_collection(self, name):
            raise RuntimeError("provider cleanup failed")

        def close(self):
            self.closed = True

    failed_client = FailingClient(backend)
    connection = sqlite3.connect(target.database)
    try:
        connection.execute(
            "INSERT INTO portable_transfer_sessions(session_id,workspace_id,direction,"
            "status,event_root_hash,manifest_json,manifest_hash,page_count,"
            "staged_page_count,total_bytes,created_at_us,updated_at_us,expires_at_us,"
            "completed_at_us) VALUES (?,?,'import','staging',?,?,?,1,1,0,?,?,?,NULL)",
            (
                session_id,
                WORKSPACE_ID,
                manifest["event_root_hash"],
                "{}",
                sha256_json({}),
                now_us,
                now_us,
                now_us + 86_400_000_000,
            ),
        )
        connection.commit()
        directory = target.storage / "portable" / "v2" / "imports" / session_id
        directory.mkdir(parents=True)
        first = claim_import_finalization(
            connection, target.storage, WORKSPACE_ID, session_id, now=NOW
        )
        first_path = directory / first.attempt_relative_path
        first_path.mkdir(parents=True)
        vectors = first_path / "vectors.jsonl"
        vectors.write_bytes(b"")
        prepared = PreparedImport(
            session_id=session_id,
            database_path=target.database,
            manifest=manifest,
            legacy_path=None,
            vectors_path=vectors,
            page_count=1,
            lease=first,
        )
        with (
            patch(
                "daem0nmcp.api.v7.portable_projections.create_qdrant_client",
                return_value=failed_client,
            ),
            pytest.raises(
                PortableTransferError,
                match="CANCELLED" if stable_error else "CAPABILITY_DEGRADED",
            ),
        ):
            prepare_vector_candidate(
                prepared,
                connection,
                WORKSPACE_ID,
                _config("portable-model"),
                local_capability_ready=True,
            )
        rows = connection.execute(
            "SELECT owner_token,artifact_name FROM portable_transfer_attempt_artifacts "
            "WHERE session_id=?",
            (session_id,),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == first.owner_token
        assert rows[0][1] in backend.collections
        assert failed_client.closed
        if other_workspace_active:
            connection.execute(
                "INSERT INTO projection_manifests(manifest_id,workspace_id,projection_name,"
                "generation,projection_version,status,source_event_count,"
                "source_event_root_hash,row_count,builder_version,details_json,started_at_us) "
                "VALUES (?,'ws_other','dense',1,1,'active',0,?,0,'test',?,?)",
                (
                    "prj_" + "9" * 64,
                    manifest["event_root_hash"],
                    json.dumps({"collection_name": rows[0][1]}),
                    now_us,
                ),
            )
            connection.commit()

        second = claim_import_finalization(
            connection,
            target.storage,
            WORKSPACE_ID,
            session_id,
            now=NOW + timedelta(minutes=16),
        )
        second_path = directory / second.attempt_relative_path
        second_path.mkdir(parents=True)
        vectors = second_path / "vectors.jsonl"
        vectors.write_bytes(b"")
        successor = PreparedImport(
            session_id=session_id,
            database_path=target.database,
            manifest=manifest,
            legacy_path=None,
            vectors_path=vectors,
            page_count=1,
            lease=second,
        )
        with patch(
            "daem0nmcp.api.v7.portable_projections.create_qdrant_client",
            return_value=_QdrantClient(backend),
        ):
            candidate, diagnostic = prepare_vector_candidate(
                successor,
                connection,
                WORKSPACE_ID,
                _config("portable-model"),
                local_capability_ready=True,
            )
        assert candidate is not None and diagnostic is None
        if other_workspace_active:
            assert rows[0][1] in backend.collections
            assert connection.execute(
                "SELECT COUNT(*) FROM portable_transfer_attempt_artifacts WHERE owner_token=?",
                (first.owner_token,),
            ).fetchone() == (1,)
            connection.execute(
                "UPDATE projection_manifests SET status='ready' WHERE workspace_id='ws_other'"
            )
            connection.commit()
            _reclaim_stale_vector_artifacts(
                _QdrantClient(backend), connection, successor, WORKSPACE_ID, None
            )
        assert rows[0][1] not in backend.collections
        assert connection.execute(
            "SELECT COUNT(*) FROM portable_transfer_attempt_artifacts WHERE owner_token=?",
            (first.owner_token,),
        ).fetchone() == (0,)
        candidate.discard(connection, WORKSPACE_ID)
        connection.commit()
        assert not backend.collections
    finally:
        connection.close()


def test_expired_finalizer_is_fenced_from_release_cleanup_and_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from daem0nmcp.api.v7 import portable_projections as portable

    monkeypatch.setattr(portable, "MAX_SESSION_BYTES", 256 * 1024)
    target = _fixture(tmp_path / "target")
    session_id = "ipt_" + "7" * 64
    now_us = int(NOW.timestamp() * 1_000_000)
    connection = sqlite3.connect(target.database)
    try:
        connection.execute(
            "INSERT INTO portable_transfer_sessions(session_id,workspace_id,direction,"
            "status,event_root_hash,manifest_json,manifest_hash,page_count,"
            "staged_page_count,total_bytes,created_at_us,updated_at_us,expires_at_us,"
            "completed_at_us) VALUES (?,?,'import','staging',?,?,?,1,1,0,?,?,?,NULL)",
            (
                session_id,
                WORKSPACE_ID,
                hashlib.sha256().hexdigest(),
                "{}",
                sha256_json({}),
                now_us,
                now_us,
                now_us + 24 * 60 * 60 * 1_000_000,
            ),
        )
        connection.commit()
        directory = target.storage / "portable" / "v2" / "imports" / session_id
        staged_page = directory / "pages" / "00000000.json"
        staged_page.parent.mkdir(parents=True)
        staged_page.write_bytes(b"p" * 4096)
        first = claim_import_finalization(
            connection, target.storage, WORKSPACE_ID, session_id, now=NOW
        )
        first = renew_import_finalization(
            connection,
            WORKSPACE_ID,
            first,
            now=NOW + timedelta(minutes=10),
        )
        with pytest.raises(PortableTransferError, match="TASK_REQUIRED"):
            claim_import_finalization(
                connection,
                target.storage,
                WORKSPACE_ID,
                session_id,
                now=NOW + timedelta(minutes=16),
            )
        first_attempt = directory / first.attempt_relative_path
        first_attempt.mkdir(parents=True)
        (first_attempt / "validated-events.db").write_bytes(b"x" * (200 * 1024))
        takeover_at = NOW + timedelta(minutes=26)
        second = claim_import_finalization(
            connection, target.storage, WORKSPACE_ID, session_id, now=takeover_at
        )
        second_attempt = directory / second.attempt_relative_path
        second_attempt.mkdir(parents=True)
        (second_attempt / "owner.txt").write_text("winner", encoding="utf-8")
        assert not first_attempt.exists()
        assert staged_page.read_bytes() == b"p" * 4096
        assert connection.execute(
            "SELECT total_bytes FROM portable_transfer_sessions WHERE session_id=?",
            (session_id,),
        ).fetchone() == (256 * 1024,)

        assert not release_import_finalization(
            connection,
            target.storage,
            first,
            now=takeover_at + timedelta(seconds=1),
        )
        row = connection.execute(
            "SELECT session.status,lease.owner_token FROM portable_transfer_sessions "
            "AS session JOIN portable_transfer_finalization_leases AS lease "
            "ON lease.session_id=session.session_id WHERE session.session_id=?",
            (session_id,),
        ).fetchone()
        assert row == ("finalizing", second.owner_token)
        assert not first_attempt.exists()
        assert (second_attempt / "owner.txt").read_text(encoding="utf-8") == "winner"

        vector_manifest = {
            "event_count": 0,
            "event_root_hash": hashlib.sha256().hexdigest(),
            "vectors": _vector_metadata("portable-model"),
        }
        stale_collection = (
            f"test-{WORKSPACE_ID}-portable-g1-"
            f"{sha256_json(vector_manifest)[:8]}-{first.owner_token[:16]}"
        )
        connection.execute(
            "INSERT INTO portable_transfer_attempt_artifacts("
            "session_id,owner_token,artifact_kind,artifact_name,created_at_us) "
            "VALUES (?,?,'qdrant_collection',?,?)",
            (session_id, first.owner_token, stale_collection, now_us),
        )
        connection.commit()
        backend = _QdrantBackend()
        backend.collections[stale_collection] = []
        vectors = second_attempt / "vectors.jsonl"
        vectors.write_bytes(b"")
        prepared = PreparedImport(
            session_id=session_id,
            database_path=target.database,
            manifest=vector_manifest,
            legacy_path=None,
            vectors_path=vectors,
            page_count=1,
            lease=second,
        )
        with patch(
            "daem0nmcp.api.v7.portable_projections.create_qdrant_client",
            return_value=_QdrantClient(backend),
        ):
            candidate, diagnostic = prepare_vector_candidate(
                prepared,
                connection,
                WORKSPACE_ID,
                _config("portable-model"),
                local_capability_ready=True,
            )
        assert candidate is not None
        assert diagnostic is None
        assert stale_collection not in backend.collections
        assert candidate.collection_name.endswith(second.owner_token[:16])
        candidate.discard(connection, WORKSPACE_ID)
        connection.commit()
        assert connection.execute(
            "SELECT COUNT(*) FROM portable_transfer_attempt_artifacts "
            "WHERE session_id=?",
            (session_id,),
        ).fetchone() == (0,)

        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(PortableTransferError, match="TASK_REQUIRED"):
            complete_import_session(
                connection,
                WORKSPACE_ID,
                first,
                now=takeover_at + timedelta(seconds=2),
            )
        connection.rollback()
        connection.execute("BEGIN IMMEDIATE")
        complete_import_session(
            connection,
            WORKSPACE_ID,
            second,
            now=takeover_at + timedelta(seconds=2),
        )
        connection.commit()
        assert (
            connection.execute(
                "SELECT status FROM portable_transfer_sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()[0]
            == "succeeded"
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM portable_transfer_finalization_leases"
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


async def test_concurrent_finalizer_is_lease_blocked_and_cannot_delete_winner(
    tmp_path: Path,
):
    from daem0nmcp.api.v7 import operations as operations_module

    source = _fixture(tmp_path / "source")
    target = _fixture(tmp_path / "target")
    source.append("concurrent vector", occurred_at_us=100, recorded_at_us=200)
    backend = _QdrantBackend()
    config = _config()
    _install_dense_source(source, backend, config)
    with patch(
        "daem0nmcp.api.v7.portable_projections.create_qdrant_client",
        side_effect=lambda **_kwargs: _QdrantClient(backend),
    ):
        pages = await _all_pages(
            build_core_operations(_dependencies(source, config))["workspace_export"],
            source,
            include_legacy_projection=False,
            include_vectors=True,
        )
        importer = build_core_operations(_dependencies(target, config))[
            "workspace_import"
        ]
        session_id = None
        for index, page in enumerate(pages):
            staged = await importer(
                workspace=target.workspace,
                request=_request(
                    "workspace_import",
                    workspace_id=WORKSPACE_ID,
                    bundle=page.model_dump(mode="python"),
                    import_session_id=session_id,
                    finalize=False,
                    idempotency_key=f"concurrent-stage-{index:04d}",
                    preflight_token="token_value_for_test",
                ),
            )
            session_id = staged.import_session_id

        original_prepare = operations_module.prepare_vector_candidate
        entered = threading.Event()
        release = threading.Event()

        def blocking_prepare(*args, **kwargs):
            entered.set()
            release.wait(2)
            return original_prepare(*args, **kwargs)

        finalize_request = _request(
            "workspace_import",
            workspace_id=WORKSPACE_ID,
            bundle=None,
            import_session_id=session_id,
            finalize=True,
            idempotency_key="concurrent-finalize-0001",
            preflight_token="token_value_for_test",
        )
        with patch.object(
            operations_module,
            "prepare_vector_candidate",
            side_effect=blocking_prepare,
        ):
            winner = asyncio.create_task(
                importer(workspace=target.workspace, request=finalize_request)
            )
            assert await asyncio.to_thread(entered.wait, 2)
            with pytest.raises(CoreOperationError, match="TASK_REQUIRED"):
                await importer(workspace=target.workspace, request=finalize_request)
            release.set()
            result = await winner
    assert result.status == "succeeded"
    connection = sqlite3.connect(target.database)
    try:
        details = json.loads(
            connection.execute(
                "SELECT details_json FROM projection_manifests WHERE workspace_id=? "
                "AND projection_name='dense' AND status='active'",
                (WORKSPACE_ID,),
            ).fetchone()[0]
        )
        tracked = connection.execute(
            "SELECT total_bytes,status FROM portable_transfer_sessions "
            "WHERE session_id=?",
            (session_id,),
        ).fetchone()
        artifacts = connection.execute(
            "SELECT COUNT(*) FROM portable_transfer_attempt_artifacts "
            "WHERE session_id=?",
            (session_id,),
        ).fetchone()[0]
    finally:
        connection.close()
    assert details["collection_name"] in backend.collections
    directory = target.storage / "portable" / "v2" / "imports" / session_id
    physical = sum(
        path.stat().st_size for path in directory.rglob("*") if path.is_file()
    )
    assert tracked == (physical, "succeeded")
    assert artifacts == 0
    attempts = directory / "attempts"
    assert not attempts.exists() or not any(attempts.iterdir())
