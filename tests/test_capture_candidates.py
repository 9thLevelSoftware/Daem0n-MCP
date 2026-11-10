from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from daem0nmcp.api.v7.models import RecordType
from daem0nmcp.capture_candidates import (
    CaptureCandidateError,
    CaptureCandidateRequest,
    CaptureCandidateStore,
)
from daem0nmcp.database import DatabaseManager
from daem0nmcp.workspace import WorkspaceRegistry

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
CANONICAL_RECORD_TYPES = (
    "decision",
    "pattern",
    "warning",
    "learning",
    "procedure",
    "observation",
)


@pytest.fixture
async def capture_context(tmp_path):
    storage = tmp_path / ".daem0nmcp" / "storage"
    storage.mkdir(parents=True)
    manager = DatabaseManager(str(storage))
    try:
        await manager.init_db()
    finally:
        await manager.close()
    workspace = WorkspaceRegistry([tmp_path], default_root=tmp_path).default
    return workspace, CaptureCandidateStore(clock=lambda: NOW)


def request(*, key: str = "capture-edit-0001") -> CaptureCandidateRequest:
    return CaptureCandidateRequest(
        source_kind="native_edit",
        record={
            "record_type": "learning",
            "content": "The parser must preserve deterministic ordering.",
            "rationale": "Observed after the reviewed edit",
            "context": {"component": "parser"},
            "tags": ["capture", "parser"],
        },
        provenance={
            "source_operation": "Edit",
            "edit_request_id": "edt_" + "a" * 64,
            "relative_file_paths": ["src/parser.py"],
            "preimage_hashes": ["b" * 64],
        },
        idempotency_key=key,
    )


@pytest.mark.asyncio
async def test_stage_is_bounded_idempotent_and_absent_from_canonical_recall(
    capture_context,
):
    workspace, store = capture_context
    first = await store.stage(workspace, request())
    replay = await store.stage(workspace, request())

    assert first.candidate_id == replay.candidate_id
    assert first.status == "pending"
    database = workspace.root / ".daem0nmcp" / "storage" / "daem0nmcp.db"
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM memory_capture_candidates"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute("SELECT count(*) FROM memory_events").fetchone()[0] == 0
        )
        assert (
            connection.execute("SELECT count(*) FROM memory_records").fetchone()[0] == 0
        )

    changed = request()
    changed = CaptureCandidateRequest(
        source_kind=changed.source_kind,
        record={**changed.record, "content": "A different proposal."},
        provenance=changed.provenance,
        idempotency_key=changed.idempotency_key,
    )
    with pytest.raises(CaptureCandidateError) as conflict:
        await store.stage(workspace, changed)
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"


def test_raw_transcript_and_public_absolute_paths_are_rejected() -> None:
    with pytest.raises(ValueError, match="conversation"):
        CaptureCandidateRequest(
            source_kind="tool_result",
            record={"record_type": "learning", "content": "bounded summary"},
            provenance={"raw_transcript": ["full conversation"]},
            idempotency_key="capture-tool-0001",
        )
    with pytest.raises(ValueError, match="absolute path"):
        CaptureCandidateRequest(
            source_kind="native_edit",
            record={"record_type": "learning", "content": "bounded summary"},
            provenance={"relative_file_paths": ["C:/private/source.py"]},
            idempotency_key="capture-edit-0002",
        )


@pytest.mark.asyncio
async def test_promotion_is_one_atomic_canonical_event_and_replays(capture_context):
    workspace, store = capture_context
    candidate = await store.stage(workspace, request())
    final_record = {
        "record_type": "learning",
        "content": "Reviewed: parser ordering must remain deterministic.",
        "rationale": "Confirmed during review",
        "context": {"component": "parser", "reviewed": True},
        "tags": ["capture", "reviewed"],
    }

    first = await store.promote(
        workspace,
        candidate_id=candidate.candidate_id,
        record=final_record,
        idempotency_key="promote-capture-0001",
    )
    replay = await store.promote(
        workspace,
        candidate_id=candidate.candidate_id,
        record=final_record,
        idempotency_key="promote-capture-0001",
    )

    assert not first.idempotent_replay
    assert replay.idempotent_replay
    assert first.event_id == replay.event_id
    database = workspace.root / ".daem0nmcp" / "storage" / "daem0nmcp.db"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT status,promoted_event_id,promotion_idempotency_key "
            "FROM memory_capture_candidates WHERE candidate_id=?",
            (candidate.candidate_id,),
        ).fetchone()
        events = connection.execute(
            "SELECT event_id,payload_json FROM memory_events"
        ).fetchall()
    assert row == ("promoted", first.event_id, "promote-capture-0001")
    assert len(events) == 1
    payload = json.loads(events[0][1])
    assert payload["capture_candidate_id"] == candidate.candidate_id
    assert payload["record"]["content"] == final_record["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("record_type", CANONICAL_RECORD_TYPES)
async def test_every_canonical_record_type_promotes_and_replays(
    capture_context,
    record_type: RecordType,
):
    workspace, store = capture_context
    staged = await store.stage(
        workspace,
        CaptureCandidateRequest(
            source_kind="system",
            record={"record_type": record_type, "content": "Bounded candidate."},
            provenance={"source_operation": "canonical-type-test"},
            idempotency_key=f"candidate-{record_type}-0001",
        ),
    )
    final_record = {
        "record_type": record_type,
        "content": f"Reviewed canonical {record_type} candidate.",
    }
    first = await store.promote(
        workspace,
        candidate_id=staged.candidate_id,
        record=final_record,
        idempotency_key=f"promote-{record_type}-0001",
    )
    replay = await store.promote(
        workspace,
        candidate_id=staged.candidate_id,
        record=final_record,
        idempotency_key=f"promote-{record_type}-0001",
    )
    assert first.record.record_type == record_type
    assert replay.event_id == first.event_id
    assert replay.idempotent_replay


@pytest.mark.asyncio
@pytest.mark.parametrize("record_type", ("context", "failed_attempt"))
async def test_noncanonical_record_type_rejects_before_persistence(
    capture_context,
    record_type: str,
):
    workspace, _store = capture_context
    with pytest.raises(ValueError, match="record type"):
        CaptureCandidateRequest(
            source_kind="system",
            record={"record_type": record_type, "content": "Invalid candidate."},
            provenance={"source_operation": "invalid-type-test"},
            idempotency_key=f"candidate-{record_type}-0001",
        )
    database = workspace.root / ".daem0nmcp" / "storage" / "daem0nmcp.db"
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM memory_capture_candidates"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.asyncio
async def test_promoted_candidate_rejects_changed_exact_request(capture_context):
    workspace, store = capture_context
    candidate = await store.stage(workspace, request())
    record = {"record_type": "learning", "content": "Reviewed capture."}
    await store.promote(
        workspace,
        candidate_id=candidate.candidate_id,
        record=record,
        idempotency_key="promote-capture-0002",
    )

    with pytest.raises(CaptureCandidateError) as conflict:
        await store.promote(
            workspace,
            candidate_id=candidate.candidate_id,
            record={"record_type": "learning", "content": "Changed review."},
            idempotency_key="promote-capture-0002",
        )
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
