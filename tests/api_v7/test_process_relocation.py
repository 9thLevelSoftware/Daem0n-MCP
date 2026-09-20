"""Memories follow a moved project, and a copy of one never steals its rows.

Each case drives the real production server against real storage: a folder is
renamed, ``.daem0nmcp/`` is copied into a new clone, or the copy is registered
beside the original.  The rules themselves are pinned in
``tests/test_workspace_identity.py``; these tests prove the server, the CLI
and the SessionStart hook agree with them end to end.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from daem0nmcp import cli
from daem0nmcp.api.v7.policy import V7_TOOL_LEVELS
from daem0nmcp.api.v7.production import create_v7_server
from daem0nmcp.claude_hooks import session_start
from daem0nmcp.config import Settings
from daem0nmcp.covenant import CovenantLevel
from daem0nmcp.storage_activation import resolve_active_database
from daem0nmcp.workspace import (
    WorkspaceRegistry,
    path_hash_workspace_id,
    reset_workspace_identity_cache,
)

CONTENT = "Relocation keeps the workspace's memories reachable."


@pytest.fixture(autouse=True)
def _fresh_identity_cache():
    reset_workspace_identity_cache()
    yield
    reset_workspace_identity_cache()


def _server(*roots: Path):
    return create_v7_server(
        "stdio",
        settings=Settings(
            project_root=str(roots[0]),
            workspace_roots=[str(root) for root in roots],
            dream_enabled=False,
        ),
        environ={"DAEM0NMCP_PROFILE": "core"},
    )


async def _call(client: Client, tool: str, arguments: dict[str, Any]) -> dict:
    result = await client.call_tool(tool, arguments, raise_on_error=False)
    assert isinstance(result.structured_content, dict), tool
    return result.structured_content


async def _protected(client: Client, workspace_id: str, tool: str, arguments: dict):
    if V7_TOOL_LEVELS[tool] not in {CovenantLevel.COUNSEL, CovenantLevel.DESTRUCTIVE}:
        return await _call(client, tool, {"workspace_id": workspace_id, **arguments})
    grant = await _call(
        client,
        "memory_preflight",
        {
            "workspace_id": workspace_id,
            "target_tool": tool,
            "target_arguments": arguments,
        },
    )
    assert grant["ok"], grant
    return await _call(
        client,
        tool,
        {
            "workspace_id": workspace_id,
            **arguments,
            "preflight_token": grant["data"]["preflight_token"],
        },
    )


async def _seed(root: Path) -> tuple[str, str]:
    """Brief, store one record and record its outcome; return the IDs."""

    async with Client(_server(root)) as client:
        workspace_id = WorkspaceRegistry([root], default_root=root).default.workspace_id
        assert (await _call(client, "session_brief", {"workspace_id": workspace_id}))[
            "ok"
        ]
        stored = await _protected(
            client,
            workspace_id,
            "memory_store",
            {
                "record_type": "decision",
                "content": CONTENT,
                "idempotency_key": "relocation-seed-0001",
            },
        )
        assert stored["ok"], stored
        record_id = stored["data"]["record"]["record_id"]
        outcome = await _protected(
            client,
            workspace_id,
            "memory_record_outcome",
            {
                "record_id": record_id,
                "outcome_text": "It worked after the move.",
                "worked": True,
                "idempotency_key": "relocation-outcome-0001",
            },
        )
        assert outcome["ok"], outcome
    return workspace_id, record_id


def _identity(root: Path) -> dict:
    return json.loads((root / ".daem0nmcp" / "workspace-id").read_text("utf-8"))


def _database_digest(root: Path) -> str:
    active = resolve_active_database(root / ".daem0nmcp" / "storage").path
    return hashlib.sha256(active.read_bytes()).hexdigest()


@contextmanager
def _cli(root: Path, *arguments: str):
    """Run one CLI command against ``root`` and yield the parsed exit code."""

    import sys

    previous = os.environ.get("DAEM0NMCP_PROJECT_ROOT")
    argv = sys.argv
    sys.argv = ["daem0nmcp", "--project-path", str(root), *arguments]
    try:
        with pytest.raises(SystemExit) as raised:
            cli.main()
        yield raised.value.code
    finally:
        sys.argv = argv
        if previous is None:
            os.environ.pop("DAEM0NMCP_PROJECT_ROOT", None)
        else:
            os.environ["DAEM0NMCP_PROJECT_ROOT"] = previous
        reset_workspace_identity_cache()


def _workspace_id_json(root: Path, *arguments: str, capsys) -> tuple[int, dict]:
    with _cli(root, "--json", "workspace-id", *arguments) as code:
        pass
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


async def _recalls_the_record(
    client: Client, workspace_id: str, record_id: str
) -> bool:
    """Poll recall until the record shows up, or a bounded deadline passes.

    The lexical projection drains in the background after a write, so a server
    that has only just started can answer from one that is still catching up.
    The deadline is bounded and there is never a fixed sleep.
    """

    deadline = time.monotonic() + 60.0
    while True:
        recall = await _call(
            client, "memory_recall", {"workspace_id": workspace_id, "query": CONTENT}
        )
        assert recall["ok"], recall
        if any(
            item["record"]["record_id"] == record_id for item in recall["data"]["items"]
        ):
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.25)


async def _reads_the_record(root: Path, workspace_id: str, record_id: str) -> bool:
    async with Client(_server(root)) as client:
        brief = await _call(client, "session_brief", {"workspace_id": workspace_id})
        assert brief["ok"], brief
        found = await _recalls_the_record(client, workspace_id, record_id)
        outcome = await _protected(
            client,
            workspace_id,
            "memory_record_outcome",
            {
                "record_id": record_id,
                "outcome_text": "Still reachable here.",
                "worked": True,
                "idempotency_key": "relocation-after-0002",
            },
        )
        assert outcome["ok"], outcome
    return found


async def test_memories_follow_a_renamed_folder(tmp_path):
    before = tmp_path / "before"
    before.mkdir()
    workspace_id, record_id = await _seed(before)
    recorded = _identity(before)
    assert recorded["id"] == workspace_id

    after = tmp_path / "after"
    before.rename(after)
    reset_workspace_identity_cache()

    moved = WorkspaceRegistry([after], default_root=after).default
    assert moved.workspace_id == workspace_id
    assert moved.identity_block is None
    assert await _reads_the_record(after, workspace_id, record_id)
    # A same-filesystem rename keeps the folder's inode, so nothing was
    # rewritten: the recorded identity is byte-for-byte the original.
    assert _identity(after) == recorded


async def test_a_re_clone_copy_serves_the_memories_in_its_own_process(tmp_path):
    original = tmp_path / "original-clone"
    original.mkdir()
    workspace_id, record_id = await _seed(original)
    clone = tmp_path / "new-clone"
    clone.mkdir()
    shutil.copytree(original / ".daem0nmcp", clone / ".daem0nmcp")
    reset_workspace_identity_cache()

    # The old clone still exists; only the new one is registered here.
    assert await _reads_the_record(clone, workspace_id, record_id)
    assert _identity(clone)["id"] == workspace_id
    assert _identity(original)["id"] == workspace_id


@pytest.mark.parametrize("reversed_order", [False, True])
async def test_a_copy_registered_beside_the_original_is_blocked(
    tmp_path, capsys, reversed_order
):
    original = tmp_path / "origin"
    original.mkdir()
    workspace_id, record_id = await _seed(original)
    copy = tmp_path / "copy"
    copy.mkdir()
    shutil.copytree(original / ".daem0nmcp", copy / ".daem0nmcp")
    reset_workspace_identity_cache()

    copy_id = path_hash_workspace_id(copy)
    roots = (copy, original) if reversed_order else (original, copy)
    async with Client(_server(*roots)) as client:
        assert (await _call(client, "session_brief", {"workspace_id": workspace_id}))[
            "ok"
        ]
        blocked = await _call(client, "session_brief", {"workspace_id": copy_id})
        assert blocked["error"]["code"] == "WORKSPACE_IDENTITY_DUPLICATE", blocked
        assert blocked["error"]["retryable"] is False
        recall = await _call(
            client, "memory_recall", {"workspace_id": copy_id, "query": CONTENT}
        )
        assert recall["error"]["code"] == "WORKSPACE_IDENTITY_DUPLICATE", recall

    # A recorded fork settles the copy without touching a single stored row.
    digest = _database_digest(copy)
    code, payload = _workspace_id_json(copy, "--new", capsys=capsys)
    assert code == 0, payload
    assert payload["workspace_id"] == copy_id
    assert payload["forked_from"] == workspace_id
    assert payload["hidden_event_count"] > 0
    assert _database_digest(copy) == digest

    async with Client(_server(*roots)) as client:
        forked = await _call(client, "session_brief", {"workspace_id": copy_id})
        assert forked["ok"], forked
        assert forked["data"]["workspace_statistics"].get("warnings", 0) == 0
        assert not forked["data"]["recent_decisions"]
        empty = await _call(
            client, "memory_recall", {"workspace_id": copy_id, "query": CONTENT}
        )
        assert empty["ok"], empty
        assert not empty["data"]["items"]
        # The original is untouched throughout.
        assert (await _call(client, "session_brief", {"workspace_id": workspace_id}))[
            "ok"
        ]
        assert await _recalls_the_record(client, workspace_id, record_id)

    # Adopting the original's ID again restores the duplicate block.
    code, payload = _workspace_id_json(copy, "--adopt", workspace_id, capsys=capsys)
    assert payload["workspace_id"] == workspace_id
    async with Client(_server(*roots)) as client:
        blocked = await _call(client, "session_brief", {"workspace_id": copy_id})
        assert blocked["error"]["code"] == "WORKSPACE_IDENTITY_DUPLICATE", blocked


async def test_workspace_id_output_equals_the_session_start_hook_id(tmp_path, capsys):
    root = tmp_path / "hooked"
    root.mkdir()
    workspace_id, _record_id = await _seed(root)
    reset_workspace_identity_cache()

    code, payload = _workspace_id_json(root, capsys=capsys)
    assert code == 0
    assert payload["workspace_id"] == workspace_id
    assert payload["recorded"] is True
    assert payload["root_key_matches"] is True

    message = session_start._hook_message(session_start.workspace_identity(str(root)))
    assert f'workspace_id="{workspace_id}"' in message
    assert "moved or copied" not in message


async def test_the_hook_reports_a_copied_folder_and_a_blocked_one(tmp_path, capsys):
    original = tmp_path / "hook-origin"
    original.mkdir()
    workspace_id, _record_id = await _seed(original)
    copy = tmp_path / "hook-copy"
    copy.mkdir()
    shutil.copytree(original / ".daem0nmcp", copy / ".daem0nmcp")
    reset_workspace_identity_cache()

    message = session_start._hook_message(session_start.workspace_identity(str(copy)))
    assert f'workspace_id="{workspace_id}"' in message
    assert "moved or copied" in message
    assert "workspace-id --new" in message

    # The hook sees one root at a time, so the duplicate block only shows when
    # both are registered; an unreadable identity file blocks on its own.
    (copy / ".daem0nmcp" / "workspace-id").write_text("{", encoding="utf-8")
    reset_workspace_identity_cache()
    blocked = session_start._hook_message(session_start.workspace_identity(str(copy)))
    assert "WORKSPACE_IDENTITY_AMBIGUOUS" in blocked
    code, payload = _workspace_id_json(copy, capsys=capsys)
    assert code == 1
    assert payload["blocked"] == "WORKSPACE_IDENTITY_AMBIGUOUS"
    assert payload["events_by_workspace_id"][workspace_id] > 0


def test_a_blocked_root_never_reaches_the_path_free_wire(tmp_path):
    """The identity remedy names no host path (the wire model enforces it)."""

    from daem0nmcp.api.v7.errors import ErrorCode, identity_block_failure

    code, message = identity_block_failure("WORKSPACE_IDENTITY_DUPLICATE")
    assert code is ErrorCode.WORKSPACE_IDENTITY_DUPLICATE
    assert "workspace-id --new" in message
    assert str(tmp_path) not in message
    assert identity_block_failure("NOT_FOUND") is None
