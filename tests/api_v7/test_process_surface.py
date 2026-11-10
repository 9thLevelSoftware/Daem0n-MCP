"""Real subprocess coverage for stateful v7 MCP tool surfaces.

These tests deliberately use the production process client.  They do not mount
FastMCP in-process or inject operation handlers, so the assertions cover the
wire schema, covenant/preflight admission, and the selected transport together.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone

import pytest

from daem0nmcp.database import DatabaseManager
from daem0nmcp.workspace import WorkspaceRegistry
from tests.api_v7.process_client import process_client, succeed


@pytest.fixture
async def initialized_workspace(tmp_path):
    storage = tmp_path / ".daem0nmcp" / "storage"
    storage.mkdir(parents=True)
    manager = DatabaseManager(str(storage))
    try:
        await manager.init_db()
    finally:
        await manager.close()
    return WorkspaceRegistry([tmp_path], default_root=tmp_path).default


async def _preflight(session, scope, tool_name, target_arguments):
    result = await succeed(
        session,
        "memory_preflight",
        {
            **scope,
            "target_tool": tool_name,
            "target_arguments": target_arguments,
            "description": f"Exercise {tool_name} through the production transport",
        },
    )
    return result["preflight_token"]


async def _store(session, scope, *, content, key, record_type="decision"):
    arguments = {
        "record_type": record_type,
        "content": content,
        "idempotency_key": key,
    }
    result = await succeed(
        session,
        "memory_store",
        {
            **scope,
            **arguments,
            "preflight_token": await _preflight(
                session, scope, "memory_store", arguments
            ),
        },
    )
    return result["record"]


@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
async def test_production_stateful_core_surface(initialized_workspace, transport):
    """Exercise 24 stateful core tools with exact preflight-bound arguments."""

    workspace = initialized_workspace
    scope = {"workspace_id": workspace.workspace_id}
    async with process_client(workspace.root, transport) as session:
        await succeed(session, "session_brief", scope)
        source = await _store(
            session,
            scope,
            content="Source decision for process surface coverage.",
            key="process-surface-store-source-0001",
        )
        target = await _store(
            session,
            scope,
            content="Target decision for process surface coverage.",
            key="process-surface-store-target-0001",
        )
        source_id = source["record_id"]
        target_id = target["record_id"]

        # Active context: add, list, remove, and clear each operate on state.
        add_arguments = {
            "record_id": source_id,
            "reason": "Keep the current decision visible",
            "priority": 7,
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }
        active = await succeed(
            session,
            "active_context_add",
            {
                **scope,
                **add_arguments,
                "preflight_token": await _preflight(
                    session, scope, "active_context_add", add_arguments
                ),
            },
        )
        page = await succeed(session, "active_context_list", scope)
        assert [item["active_context_id"] for item in page["items"]] == [
            active["active_context_id"]
        ]
        remove_arguments = {"active_context_id": active["active_context_id"]}
        removed = await succeed(
            session,
            "active_context_remove",
            {
                **scope,
                **remove_arguments,
                "preflight_token": await _preflight(
                    session, scope, "active_context_remove", remove_arguments
                ),
            },
        )
        assert removed["affected_ids"] == [active["active_context_id"]]
        second_arguments = {"record_id": target_id, "priority": 3}
        await succeed(
            session,
            "active_context_add",
            {
                **scope,
                **second_arguments,
                "preflight_token": await _preflight(
                    session, scope, "active_context_add", second_arguments
                ),
            },
        )
        clear_page = await succeed(session, "active_context_list", scope)
        clear_arguments = {"selection_token": clear_page["selection_token"]}
        cleared = await succeed(
            session,
            "active_context_clear",
            {
                **scope,
                **clear_arguments,
                "preflight_token": await _preflight(
                    session, scope, "active_context_clear", clear_arguments
                ),
            },
        )
        assert cleared["affected_ids"]
        assert (await succeed(session, "active_context_list", scope))["items"] == []

        # Rules have no registered delete operation; update disables the created rule.
        rule_arguments = {
            "trigger": "changing process surface fixtures",
            "must_do": ["Run the process test"],
            "warnings": ["Preserve exact preflight arguments"],
            "priority": 4,
            "idempotency_key": "process-surface-rule-create-0001",
        }
        rule = await succeed(
            session,
            "rule_create",
            {
                **scope,
                **rule_arguments,
                "preflight_token": await _preflight(
                    session, scope, "rule_create", rule_arguments
                ),
            },
        )
        listed_rules = await succeed(session, "rule_list", scope)
        assert [item["rule_id"] for item in listed_rules["items"]] == [rule["rule_id"]]
        checked = await succeed(
            session,
            "rule_check",
            {
                **scope,
                "proposed_action": "changing process surface fixtures",
            },
        )
        assert [item["rule_id"] for item in checked["matched_rules"]] == [
            rule["rule_id"]
        ]
        update_arguments = {"rule_id": rule["rule_id"], "patch": {"enabled": False}}
        updated = await succeed(
            session,
            "rule_update",
            {
                **scope,
                **update_arguments,
                "preflight_token": await _preflight(
                    session, scope, "rule_update", update_arguments
                ),
            },
        )
        assert updated["enabled"] is False
        assert (await succeed(session, "rule_list", scope))["items"] == []

        trigger_arguments = {
            "trigger_type": "file",
            "pattern": "src/**/*.py",
            "recall_query": "process surface guidance",
            "categories": ["decision"],
            "idempotency_key": "process-surface-trigger-create-0001",
        }
        trigger = await succeed(
            session,
            "context_trigger_create",
            {
                **scope,
                **trigger_arguments,
                "preflight_token": await _preflight(
                    session, scope, "context_trigger_create", trigger_arguments
                ),
            },
        )
        listed_triggers = await succeed(session, "context_trigger_list", scope)
        assert [item["trigger_id"] for item in listed_triggers["items"]] == [
            trigger["trigger_id"]
        ]
        matches = await succeed(
            session,
            "context_triggers_match",
            {**scope, "relative_file_path": "src/service.py"},
        )
        assert [item["trigger"]["trigger_id"] for item in matches["matches"]] == [
            trigger["trigger_id"]
        ]
        delete_arguments = {"trigger_id": trigger["trigger_id"]}
        deleted = await succeed(
            session,
            "context_trigger_delete",
            {
                **scope,
                **delete_arguments,
                "preflight_token": await _preflight(
                    session, scope, "context_trigger_delete", delete_arguments
                ),
            },
        )
        assert deleted["affected_ids"] == [trigger["trigger_id"]]
        assert (await succeed(session, "context_trigger_list", scope))["items"] == []

        versions = await succeed(
            session, "memory_versions_list", {**scope, "record_id": source_id}
        )
        assert versions["items"][0]["record"]["record_id"] == source_id
        link_arguments = {
            "source_record_id": source_id,
            "target_record_id": target_id,
            "relationship_type": "related_to",
            "idempotency_key": "process-surface-link-0001",
        }
        linked = await succeed(
            session,
            "memory_link",
            {
                **scope,
                **link_arguments,
                "preflight_token": await _preflight(
                    session, scope, "memory_link", link_arguments
                ),
            },
        )
        relationship_id = next(
            value for value in linked["affected_ids"] if value.startswith("rel_")
        )
        related = await succeed(
            session,
            "memory_related",
            {**scope, "record_id": source_id, "max_depth": 1},
        )
        assert [record["record_id"] for record in related["records"]] == [target_id]
        chain = await succeed(
            session,
            "memory_chain_trace",
            {**scope, "start_record_id": source_id, "end_record_id": target_id},
        )
        assert chain["paths"][0]["record_ids"] == [source_id, target_id]
        unlink_arguments = {"relationship_id": relationship_id}
        unlinked = await succeed(
            session,
            "memory_unlink",
            {
                **scope,
                **unlink_arguments,
                "preflight_token": await _preflight(
                    session, scope, "memory_unlink", unlink_arguments
                ),
            },
        )
        assert relationship_id in unlinked["affected_ids"]
        assert (
            await succeed(
                session,
                "memory_chain_trace",
                {**scope, "start_record_id": source_id, "end_record_id": target_id},
            )
        )["paths"] == []

        pin_arguments = {"record_id": source_id, "pinned": True}
        pinned = await succeed(
            session,
            "memory_pin_set",
            {
                **scope,
                **pin_arguments,
                "preflight_token": await _preflight(
                    session, scope, "memory_pin_set", pin_arguments
                ),
            },
        )
        assert pinned["affected_ids"] == [source_id]
        archive_arguments = {"record_id": target_id, "archived": True}
        archived = await succeed(
            session,
            "memory_archive_set",
            {
                **scope,
                **archive_arguments,
                "preflight_token": await _preflight(
                    session, scope, "memory_archive_set", archive_arguments
                ),
            },
        )
        assert archived["affected_ids"] == [target_id]
        assert (await succeed(session, "memory_prune_preview", scope))[
            "selection_token"
        ]
        assert (await succeed(session, "memory_duplicates_preview", scope))[
            "selection_token"
        ]
        assert (
            await succeed(
                session,
                "memory_compaction_preview",
                {**scope, "summary": "Compact the process coverage decisions."},
            )
        )["selection_token"]


@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
async def test_production_core_read_batch_and_projection_surface(
    initialized_workspace, transport
):
    """Cover independent core reads, batch mutation, and lexical rebuild in one process."""

    workspace = initialized_workspace
    (workspace.root / "src").mkdir()
    (workspace.root / "src" / "refactor.py").write_text(
        "def old_name():\n    return 1\n", encoding="utf-8"
    )
    scope = {"workspace_id": workspace.workspace_id}
    async with process_client(workspace.root, transport) as session:
        await succeed(session, "session_brief", scope)
        covenant = await succeed(session, "covenant_status", scope)
        assert covenant["briefed"] is True
        compressed = await succeed(
            session,
            "context_compress",
            {
                **scope,
                "text": "The refactor keeps the public contract stable. " * 20,
                "rate": 0.5,
            },
        )
        assert compressed["rendered_tokens"] <= compressed["original_tokens"]
        proposal = await succeed(
            session,
            "code_refactor_propose",
            {
                **scope,
                "relative_file_path": "src/refactor.py",
                "objective": "Rename old_name.",
            },
        )
        assert proposal["proposal"]

        since = datetime.now(timezone.utc).isoformat()
        before = await succeed(
            session, "session_updates_get", {**scope, "since": since}
        )
        batch_arguments = {
            "records": [
                {
                    "record_type": "decision",
                    "content": "Batch process evidence is searchable.",
                    "relative_file_path": "src/refactor.py",
                    "tags": ["process", "batch"],
                },
                {
                    "record_type": "learning",
                    "content": "Batch process evidence retains exact file attribution.",
                    "relative_file_path": "src/refactor.py",
                },
            ],
            "idempotency_key": "process-surface-batch-store-0001",
        }
        batch = await succeed(
            session,
            "memory_store_batch",
            {
                **scope,
                **batch_arguments,
                "preflight_token": await _preflight(
                    session, scope, "memory_store_batch", batch_arguments
                ),
            },
        )
        assert len(batch["records"]) == 2
        file_records = await succeed(
            session,
            "memory_recall_file",
            {**scope, "relative_file_path": "src/refactor.py"},
        )
        assert {item["record_id"] for item in file_records["items"]} == {
            record["record_id"] for record in batch["records"]
        }
        searched = await succeed(
            session,
            "memory_search_text",
            {**scope, "query": "searchable", "highlight": True},
        )
        assert (
            searched["items"][0]["record"]["record_id"]
            == batch["records"][0]["record_id"]
        )
        updates = await succeed(
            session,
            "session_updates_get",
            {**scope, "after_cursor": before["cursor"], "since": since},
        )
        assert updates["changed"] is True
        assert {event["object_id"] for event in updates["events"]} >= {
            record["record_id"] for record in batch["records"]
        }
        rebuilt = await succeed(
            session,
            "projection_rebuild",
            {**scope, "projection": "lexical"},
        )
        assert rebuilt["manifest"]["projection"] == "lexical"


@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
async def test_production_workspace_link_lifecycle_surface(tmp_path, transport):
    """Link listing and unlinking use two registered workspaces through one process."""

    roots = (tmp_path / "origin", tmp_path / "linked")
    workspaces = []
    for root in roots:
        storage = root / ".daem0nmcp" / "storage"
        storage.mkdir(parents=True)
        manager = DatabaseManager(str(storage))
        try:
            await manager.init_db()
        finally:
            await manager.close()
        workspaces.append(WorkspaceRegistry([root], default_root=root).default)
    origin, linked = workspaces
    scope = {"workspace_id": origin.workspace_id}
    async with process_client(
        origin.root, transport, workspace_roots=(linked.root,)
    ) as session:
        await succeed(session, "session_brief", scope)
        await succeed(session, "session_brief", {"workspace_id": linked.workspace_id})
        link_arguments = {
            "linked_workspace_id": linked.workspace_id,
            "relationship": "related",
            "label": "process lifecycle",
        }
        await succeed(
            session,
            "workspace_link",
            {
                **scope,
                **link_arguments,
                "preflight_token": await _preflight(
                    session, scope, "workspace_link", link_arguments
                ),
            },
        )
        listed = await succeed(session, "workspace_links_list", scope)
        assert [item["linked_workspace_id"] for item in listed["items"]] == [
            linked.workspace_id
        ]
        unlink_arguments = {"linked_workspace_id": linked.workspace_id}
        receipt = await succeed(
            session,
            "workspace_unlink",
            {
                **scope,
                **unlink_arguments,
                "preflight_token": await _preflight(
                    session, scope, "workspace_unlink", unlink_arguments
                ),
            },
        )
        assert linked.workspace_id in receipt["affected_ids"]
        assert (await succeed(session, "workspace_links_list", scope))["items"] == []


@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
async def test_production_maintenance_apply_from_fresh_previews(
    initialized_workspace, transport
):
    """Every destructive maintenance mutation consumes its own current preview token."""

    workspace = initialized_workspace
    scope = {"workspace_id": workspace.workspace_id}
    async with process_client(workspace.root, transport) as session:
        await succeed(session, "session_brief", scope)
        duplicate_one = await _store(
            session,
            scope,
            content="Duplicate process maintenance evidence.",
            key="process-surface-duplicate-one-0001",
        )
        duplicate_two = await _store(
            session,
            scope,
            content="Duplicate process maintenance evidence.",
            key="process-surface-duplicate-two-0001",
        )

        duplicate_preview_args = {"merge_duplicates": True}
        duplicate_preview = await succeed(
            session, "memory_duplicates_preview", {**scope, **duplicate_preview_args}
        )
        assert set(duplicate_preview["sample_ids"]) & {
            duplicate_one["record_id"],
            duplicate_two["record_id"],
        }
        cleanup_args = {
            **duplicate_preview_args,
            "selection_token": duplicate_preview["selection_token"],
        }
        cleaned = await succeed(
            session,
            "memory_duplicates_cleanup",
            {
                **scope,
                **cleanup_args,
                "preflight_token": await _preflight(
                    session, scope, "memory_duplicates_cleanup", cleanup_args
                ),
            },
        )
        assert cleaned["changed_count"] >= 1

        compact_source = await _store(
            session,
            scope,
            content="Unique process compaction maintenance evidence.",
            key="process-surface-compact-source-0001",
            record_type="learning",
        )

        compact_preview_args = {
            "summary": "Compact the remaining process maintenance evidence.",
            "query": "Unique process compaction",
        }
        compact_preview = await succeed(
            session,
            "memory_compaction_preview",
            {**scope, **compact_preview_args},
        )
        assert compact_source["record_id"] in compact_preview["sample_ids"]
        compact_args = {
            **compact_preview_args,
            "selection_token": compact_preview["selection_token"],
            "idempotency_key": "process-surface-compact-0001",
        }
        compacted = await succeed(
            session,
            "memory_compact",
            {
                **scope,
                **compact_args,
                "preflight_token": await _preflight(
                    session, scope, "memory_compact", compact_args
                ),
            },
        )
        assert compacted["summary_record"]["record_type"] == "learning"
        assert compacted["source_event_ids"]


@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
async def test_production_intelligence_time_filter_surface(
    initialized_workspace, transport
):
    """Use wire RFC3339 dates for intelligence reads and rule evolution."""

    workspace = initialized_workspace
    scope = {"workspace_id": workspace.workspace_id}
    async with process_client(workspace.root, transport) as session:
        await succeed(session, "session_brief", scope)
        decision = await _store(
            session,
            scope,
            content="Use explicit process evidence for deployment decisions.",
            key="process-surface-intelligence-decision-0001",
        )
        now = datetime.now(timezone.utc).isoformat()
        verified = await succeed(
            session,
            "memory_verify",
            {
                **scope,
                "text": "Use explicit process evidence for deployment decisions.",
                "as_of_valid_time": now,
                "as_of_transaction_time": now,
            },
        )
        assert verified["overall_status"] == "supported"
        simulated = await succeed(
            session,
            "decision_simulate",
            {
                **scope,
                "record_id": decision["record_id"],
                "as_of_transaction_time": now,
            },
        )
        assert simulated["decision"]["record_id"] == decision["record_id"]
        rule_arguments = {
            "trigger": "reviewing intelligence process coverage",
            "must_do": ["Preserve point-in-time arguments"],
            "idempotency_key": "process-surface-intelligence-rule-0001",
        }
        rule = await succeed(
            session,
            "rule_create",
            {
                **scope,
                **rule_arguments,
                "preflight_token": await _preflight(
                    session, scope, "rule_create", rule_arguments
                ),
            },
        )
        evolution = await succeed(
            session,
            "rule_evolution_analyze",
            {**scope, "rule_id": rule["rule_id"]},
        )
        assert evolution["analyzed"] == 1


@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
async def test_production_memory_at_time_accepts_its_version_timestamp(
    initialized_workspace, transport
):
    """Regression: a version timestamp must be accepted by temporal lookup."""

    workspace = initialized_workspace
    scope = {"workspace_id": workspace.workspace_id}
    async with process_client(workspace.root, transport) as session:
        await succeed(session, "session_brief", scope)
        record = await _store(
            session,
            scope,
            content="Temporal process regression record.",
            key="process-surface-temporal-store-0001",
        )
        versions = await succeed(
            session,
            "memory_versions_list",
            {**scope, "record_id": record["record_id"]},
        )
        at_time = await succeed(
            session,
            "memory_at_time_get",
            {
                **scope,
                "record_id": record["record_id"],
                "valid_time": versions["items"][0]["valid_from"],
            },
        )
        assert at_time["record"]["record_id"] == record["record_id"]


@pytest.mark.skipif(
    importlib.util.find_spec("tree_sitter_language_pack") is None,
    reason="apps profile requires tree-sitter-language-pack",
)
@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
async def test_production_code_todos_apps_profile(initialized_workspace, transport):
    """The apps-only TODO operation runs through a process with only that profile enabled."""

    workspace = initialized_workspace
    (workspace.root / "src").mkdir()
    (workspace.root / "src" / "todo.py").write_text(
        "# TODO: exercise production code_todos_scan\n", encoding="utf-8"
    )
    scope = {"workspace_id": workspace.workspace_id}
    async with process_client(
        workspace.root,
        transport,
        environment_overrides={"DAEM0NMCP_APPS_ENABLED": "true"},
    ) as session:
        await succeed(session, "session_brief", scope)
        todos = await succeed(
            session,
            "code_todos_scan",
            {**scope, "relative_root": "src", "types": ["todo"]},
        )
        assert [
            (item["relative_file_path"], item["todo_type"]) for item in todos["items"]
        ] == [("src/todo.py", "todo")]
