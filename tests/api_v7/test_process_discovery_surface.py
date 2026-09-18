"""Real subprocess acceptance for v7 discovery and graph-capable tools."""

from __future__ import annotations

import importlib.util

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
            "description": f"Exercise {tool_name} through a production MCP process",
        },
    )
    return result["preflight_token"]


async def _store(session, scope, *, content, key, record_type="decision", tags=None):
    arguments = {
        "record_type": record_type,
        "content": content,
        "idempotency_key": key,
    }
    if tags is not None:
        arguments["tags"] = tags
    stored = await succeed(
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
    return stored["record"]


@pytest.mark.skipif(
    any(
        importlib.util.find_spec(name) is None
        for name in ("igraph", "leidenalg", "networkx")
    ),
    reason="Graph process certification requires the installed graph profile",
)
@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
async def test_production_graph_discovery_surface(initialized_workspace, transport):
    """Seed canonical records, then exercise graph and entity discovery state."""

    workspace = initialized_workspace
    scope = {"workspace_id": workspace.workspace_id}
    environment = {"DAEM0NMCP_GRAPH_ENABLED": "true"}
    async with process_client(
        workspace.root, transport, environment_overrides=environment
    ) as session:
        await succeed(session, "session_brief", scope)
        first = await _store(
            session,
            scope,
            content="AuthenticationService signs session cookies for the Portal.",
            key="process-discovery-first-0001",
        )
        second = await _store(
            session,
            scope,
            content="AuthenticationService rotates Portal session keys every week.",
            key="process-discovery-second-0001",
        )
        link_arguments = {
            "source_record_id": first["record_id"],
            "target_record_id": second["record_id"],
            "relationship_type": "related_to",
            "idempotency_key": "process-discovery-link-0001",
        }
        await succeed(
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
        backfill_arguments = {
            "force": True,
            "idempotency_key": "process-discovery-backfill-0001",
        }
        backfilled = await succeed(
            session,
            "entity_backfill",
            {
                **scope,
                **backfill_arguments,
                "preflight_token": await _preflight(
                    session, scope, "entity_backfill", backfill_arguments
                ),
            },
        )
        assert backfilled["scanned"] == 2
        assert backfilled["extracted"] >= 1
        entities = await succeed(session, "entity_list", scope)
        assert entities["items"]
        entity = entities["items"][0]
        traced = await succeed(
            session,
            "entity_evolution_trace",
            {**scope, "entity_id": entity["entity_id"]},
        )
        assert traced["entity"]["entity_id"] == entity["entity_id"]
        recalled = await succeed(
            session,
            "memory_recall_entity",
            {**scope, "entity_name": entity["name"]},
        )
        assert {item["record_id"] for item in recalled["items"]} & {
            first["record_id"],
            second["record_id"],
        }
        graph = await succeed(
            session,
            "knowledge_graph_get",
            {
                **scope,
                "record_ids": [first["record_id"], second["record_id"]],
                "include_orphans": True,
                "max_nodes": 2,
            },
        )
        assert {node["record"]["record_id"] for node in graph["nodes"]} >= {
            first["record_id"],
            second["record_id"],
        }
        rendered = await succeed(
            session,
            "knowledge_graph_render",
            {
                **scope,
                "record_ids": [first["record_id"], second["record_id"]],
                "include_orphans": True,
                "max_nodes": 2,
            },
        )
        assert rendered["text"].startswith("flowchart TD")
        stats = await succeed(session, "knowledge_graph_stats", scope)
        assert stats["edge_count"] >= 1

        rebuild_arguments = {
            "min_community_size": 2,
            "resolution": 1.0,
            "idempotency_key": "process-discovery-community-0001",
        }
        rebuilt = await succeed(
            session,
            "community_rebuild",
            {
                **scope,
                **rebuild_arguments,
                "preflight_token": await _preflight(
                    session, scope, "community_rebuild", rebuild_arguments
                ),
            },
        )
        assert rebuilt["manifest"]["projection"] == "graph"
        communities = await succeed(session, "community_list", scope)
        assert communities["items"]
        community = await succeed(
            session,
            "community_get",
            {**scope, "community_id": communities["items"][0]["community_id"]},
        )
        assert (
            community["community"]["community_id"]
            == communities["items"][0]["community_id"]
        )
        hierarchical = await succeed(
            session,
            "memory_recall_hierarchical",
            {**scope, "query": entity["name"], "include_members": True},
        )
        assert hierarchical["communities"]


@pytest.mark.skipif(
    importlib.util.find_spec("tree_sitter_language_pack") is None,
    reason="apps profile requires tree-sitter-language-pack",
)
@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
async def test_production_code_todos_store_surface(initialized_workspace, transport):
    """The apps-profile TODO write is preflight-bound and persists canonical records."""

    workspace = initialized_workspace
    source = workspace.root / "src"
    source.mkdir()
    (source / "debt.py").write_text(
        "# TODO: persist process evidence\n", encoding="utf-8"
    )
    scope = {"workspace_id": workspace.workspace_id}
    async with process_client(
        workspace.root,
        transport,
        environment_overrides={"DAEM0NMCP_APPS_ENABLED": "true"},
    ) as session:
        await succeed(session, "session_brief", scope)
        arguments = {
            "relative_root": "src",
            "types": ["todo"],
            "idempotency_key": "process-discovery-todos-store-0001",
        }
        stored = await succeed(
            session,
            "code_todos_scan_and_store",
            {
                **scope,
                **arguments,
                "preflight_token": await _preflight(
                    session, scope, "code_todos_scan_and_store", arguments
                ),
            },
        )
        assert [
            (item["relative_file_path"], item["todo_type"])
            for item in stored["findings"]
        ] == [("src/debt.py", "todo")]
        assert len(stored["stored_records"]) == 1


@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
async def test_production_debate_and_dream_cleanup_surface(
    initialized_workspace, transport
):
    """Decision debate and dream purge persist evidence through a production process."""

    workspace = initialized_workspace
    scope = {"workspace_id": workspace.workspace_id}
    async with process_client(workspace.root, transport) as session:
        await succeed(session, "session_brief", scope)
        await _store(
            session,
            scope,
            content="The release should retain canonical process evidence.",
            key="process-discovery-debate-evidence-0001",
        )
        debate_arguments = {
            "topic": "retaining process evidence",
            "advocate_position": "Keep canonical evidence for release review.",
            "challenger_position": "Avoid unnecessary retained data.",
            "max_rounds": 2,
            "idempotency_key": "process-discovery-debate-0001",
        }
        debated = await succeed(
            session,
            "decision_debate",
            {
                **scope,
                **debate_arguments,
                "preflight_token": await _preflight(
                    session, scope, "decision_debate", debate_arguments
                ),
            },
        )
        assert len(debated["rounds"]) == 2
        assert debated["consensus_record_id"].startswith("mem_")

        dream_tags = [
            "dream",
            "re-evaluation",
            "source-decision:release-review",
        ]
        first = await _store(
            session,
            scope,
            content="Dream re-evaluation one.",
            key="process-discovery-dream-one-0001",
            record_type="learning",
            tags=dream_tags,
        )
        second = await _store(
            session,
            scope,
            content="Dream re-evaluation two.",
            key="process-discovery-dream-two-0001",
            record_type="learning",
            tags=dream_tags,
        )
        preview = await succeed(session, "dream_duplicates_preview", scope)
        assert set(preview["sample_ids"]) & {first["record_id"], second["record_id"]}
        purge_arguments = {"selection_token": preview["selection_token"]}
        purged = await succeed(
            session,
            "dream_duplicates_purge",
            {
                **scope,
                **purge_arguments,
                "preflight_token": await _preflight(
                    session, scope, "dream_duplicates_purge", purge_arguments
                ),
            },
        )
        assert purged["changed_count"] == 1
