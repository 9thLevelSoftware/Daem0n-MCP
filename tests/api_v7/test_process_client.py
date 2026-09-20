"""Production transport acceptance; no injected handlers or in-process server."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import urlsplit

import httpx
import pytest

from daem0nmcp.database import DatabaseManager
from daem0nmcp.workspace import WorkspaceRegistry
from tests.api_v7.process_client import call, process_client, succeed


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


async def test_production_http_host_origin_and_json_boundaries(initialized_workspace):
    async def probe(url):
        async with httpx.AsyncClient(timeout=5) as client:
            invalid_host = await client.post(
                url,
                content=b"not-json",
                headers={
                    "host": "attacker.example",
                    "content-type": "application/json",
                },
            )
            assert invalid_host.status_code == 403
            parsed = urlsplit(url)
            reader, writer = await asyncio.open_connection(parsed.hostname, parsed.port)
            try:
                writer.write(
                    b"POST /mcp HTTP/1.1\r\nHost: attacker.example\r\nHost: localhost\r\nContent-Length: 0\r\n\r\n"
                )
                await writer.drain()
                status_line = await asyncio.wait_for(reader.readline(), 5)
                assert status_line.split()[1] in {b"400", b"403"}
            finally:
                writer.close()
                await writer.wait_closed()
            bad_origin = await client.post(
                url,
                content=b"{}",
                headers={
                    "origin": "https://attacker.example",
                    "content-type": "application/json",
                },
            )
            assert bad_origin.status_code == 403
            duplicate_json = await client.post(
                url,
                content=b'{"jsonrpc":"2.0","jsonrpc":"2.0"}',
                headers={"content-type": "application/json"},
            )
            assert duplicate_json.status_code == 400

    async with process_client(
        initialized_workspace.root, "streamable-http", http_probe=probe
    ) as session:
        await succeed(
            session,
            "session_brief",
            {"workspace_id": initialized_workspace.workspace_id},
        )


@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
async def test_production_ritual_and_restart(initialized_workspace, transport):
    workspace = initialized_workspace
    scope = {"workspace_id": workspace.workspace_id}
    recall_arguments = {**scope, "query": "durable protocol acceptance", "limit": 5}
    async with process_client(workspace.root, transport) as session:
        tools = await session.list_tools()
        names = {tool.name for tool in tools.tools}
        assert {
            "session_brief",
            "memory_recall",
            "memory_preflight",
            "memory_store",
            "memory_record_outcome",
            "system_health",
        } <= names
        denied = await call(session, "memory_recall", recall_arguments)
        assert not denied["ok"]
        assert denied["error"]["code"] == "COMMUNION_REQUIRED"
        await succeed(session, "session_brief", scope)
        health = await succeed(
            session, "system_health", {**scope, "include_components": True}
        )
        runtime_components = {
            row["component"]: row for row in health["runtime_diagnostics"]
        }
        assert {
            "tasks",
            "edit_bridge",
            "lexical",
            "migration",
        } <= runtime_components.keys()
        assert runtime_components["migration"]["status"] == "not_verified"
        compact_health = await succeed(
            session, "system_health", {**scope, "include_components": False}
        )
        assert compact_health["runtime_diagnostics"] == []
        empty = await succeed(session, "memory_recall", recall_arguments)
        assert empty["items"] == []
        arguments = {
            "record_type": "decision",
            "content": "Use durable protocol acceptance tests",
            "idempotency_key": "process-acceptance-store-0001",
        }
        preflight = await succeed(
            session,
            "memory_preflight",
            {
                **scope,
                "target_tool": "memory_store",
                "target_arguments": arguments,
                "description": "Verify the production MCP write path",
            },
        )
        stored = await succeed(
            session,
            "memory_store",
            {
                **scope,
                **arguments,
                "preflight_token": preflight["preflight_token"],
            },
        )
        record_id = stored["record"]["record_id"]
        await succeed(
            session,
            "memory_record_outcome",
            {
                **scope,
                "record_id": record_id,
                "outcome_text": "Real MCP transport succeeded",
                "worked": True,
                "idempotency_key": "process-acceptance-outcome-0001",
            },
        )
        visibility_deadline = asyncio.get_running_loop().time() + 5
        while True:
            recalled = await succeed(session, "memory_recall", recall_arguments)
            if record_id in {item["record"]["record_id"] for item in recalled["items"]}:
                break
            assert asyncio.get_running_loop().time() < visibility_deadline, recalled
            await asyncio.sleep(0.1)
        templates = await session.list_resource_templates()
        assert len(templates.resourceTemplates) >= 4
        resource = await session.read_resource(
            f"memory://workspaces/{workspace.workspace_id}/warnings"
        )
        assert resource.contents
        resource_payload = json.loads(resource.contents[0].text)
        assert resource_payload["items"] == []
        health = await succeed(session, "system_health", scope)
        assert health["storage_format_version"] == 7
    async with process_client(workspace.root, transport) as restarted:
        denied = await call(restarted, "memory_recall", recall_arguments)
        assert denied["error"]["code"] == "COMMUNION_REQUIRED"
        await succeed(restarted, "session_brief", scope)
        recalled = await succeed(restarted, "memory_recall", recall_arguments)
        assert record_id in {item["record"]["record_id"] for item in recalled["items"]}


async def test_federated_recall_process_requires_each_briefing_and_keeps_origin(
    tmp_path,
):
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

    async def store(session, workspace, content, key):
        scope = {"workspace_id": workspace.workspace_id}
        arguments = {
            "record_type": "decision",
            "content": content,
            "idempotency_key": key,
        }
        preflight = await succeed(
            session,
            "memory_preflight",
            {
                **scope,
                "target_tool": "memory_store",
                "target_arguments": arguments,
            },
        )
        result = await succeed(
            session,
            "memory_store",
            {**scope, **arguments, "preflight_token": preflight["preflight_token"]},
        )
        return result["record"]["record_id"]

    client_options = {"workspace_roots": (linked.root,)}
    async with process_client(origin.root, "stdio", **client_options) as session:
        await succeed(session, "session_brief", {"workspace_id": origin.workspace_id})
        await succeed(session, "session_brief", {"workspace_id": linked.workspace_id})
        origin_record = await store(
            session,
            origin,
            "Federated protocol decision from origin",
            "federated-origin-store-0001",
        )
        linked_record = await store(
            session,
            linked,
            "Federated protocol decision from linked workspace",
            "federated-linked-store-0001",
        )
        link_arguments = {
            "linked_workspace_id": linked.workspace_id,
            "relationship": "related",
            "label": "process federation",
        }
        preflight = await succeed(
            session,
            "memory_preflight",
            {
                "workspace_id": origin.workspace_id,
                "target_tool": "workspace_link",
                "target_arguments": link_arguments,
            },
        )
        await succeed(
            session,
            "workspace_link",
            {
                "workspace_id": origin.workspace_id,
                **link_arguments,
                "preflight_token": preflight["preflight_token"],
            },
        )

    recall_arguments = {
        "workspace_id": origin.workspace_id,
        "query": "federated protocol decision",
        "limit": 6,
        "candidate_limit": 12,
        "linked_workspace_ids": [linked.workspace_id],
    }
    async with process_client(origin.root, "stdio", **client_options) as session:
        await succeed(session, "session_brief", {"workspace_id": origin.workspace_id})
        denied = await call(session, "memory_recall", recall_arguments)
        assert not denied["ok"]
        assert denied["error"]["code"] == "COMMUNION_REQUIRED"

        await succeed(session, "session_brief", {"workspace_id": linked.workspace_id})
        visibility_deadline = asyncio.get_running_loop().time() + 5
        while True:
            recalled = await succeed(session, "memory_recall", recall_arguments)
            returned = {
                item["record"]["record_id"]: {
                    ref["origin_workspace_id"] for ref in item["evidence_refs"]
                }
                for item in recalled["items"]
            }
            if origin_record in returned and linked_record in returned:
                break
            assert asyncio.get_running_loop().time() < visibility_deadline, recalled
            await asyncio.sleep(0.1)

        assert returned[origin_record] == {origin.workspace_id}
        assert returned[linked_record] == {linked.workspace_id}
        citation_origins = {
            ref["origin_workspace_id"]
            for citation in recalled["citation_manifest"]
            for ref in citation["evidence_refs"]
        }
        assert citation_origins == {origin.workspace_id, linked.workspace_id}

        linked_only = await succeed(
            session,
            "memory_recall",
            {
                "workspace_id": origin.workspace_id,
                "query": "linked workspace",
                "limit": 1,
                "candidate_limit": 2,
                "linked_workspace_ids": [linked.workspace_id],
            },
        )
        assert [item["record"]["record_id"] for item in linked_only["items"]] == [
            linked_record
        ]
        assert {
            ref["origin_workspace_id"]
            for ref in linked_only["items"][0]["evidence_refs"]
        } == {linked.workspace_id}


@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
async def test_consolidation_preview_copy_and_canonical_archive_over_real_mcp(
    tmp_path, transport
):
    roots = (tmp_path / "target", tmp_path / "source-a", tmp_path / "source-b")
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
    target, *sources = workspaces

    async with process_client(
        target.root, transport, workspace_roots=tuple(source.root for source in sources)
    ) as session:
        for workspace in workspaces:
            await succeed(
                session, "session_brief", {"workspace_id": workspace.workspace_id}
            )

        for index, source in enumerate(sources):
            arguments = {
                "record_type": "learning",
                "content": f"consolidation process source {index}",
                "idempotency_key": f"consolidation-source-{index:04d}",
            }
            preflight = await succeed(
                session,
                "memory_preflight",
                {
                    "workspace_id": source.workspace_id,
                    "target_tool": "memory_store",
                    "target_arguments": arguments,
                },
            )
            await succeed(
                session,
                "memory_store",
                {
                    "workspace_id": source.workspace_id,
                    **arguments,
                    "preflight_token": preflight["preflight_token"],
                },
            )
            link_arguments = {
                "linked_workspace_id": source.workspace_id,
                "relationship": "related",
            }
            link_preflight = await succeed(
                session,
                "memory_preflight",
                {
                    "workspace_id": target.workspace_id,
                    "target_tool": "workspace_link",
                    "target_arguments": link_arguments,
                },
            )
            await succeed(
                session,
                "workspace_link",
                {
                    "workspace_id": target.workspace_id,
                    **link_arguments,
                    "preflight_token": link_preflight["preflight_token"],
                },
            )

        source_ids = [source.workspace_id for source in sources]
        preview = await succeed(
            session,
            "workspace_consolidation_preview",
            {
                "workspace_id": target.workspace_id,
                "source_workspace_ids": source_ids,
            },
        )
        assert preview["selected"] == 2
        consolidate_arguments = {
            "source_workspace_ids": source_ids,
            "idempotency_key": "process-consolidate-0001",
            "selection_token": preview["selection_token"],
        }
        preflight = await succeed(
            session,
            "memory_preflight",
            {
                "workspace_id": target.workspace_id,
                "target_tool": "workspace_consolidate",
                "target_arguments": consolidate_arguments,
            },
        )
        copied = await succeed(
            session,
            "workspace_consolidate",
            {
                "workspace_id": target.workspace_id,
                **consolidate_arguments,
                "preflight_token": preflight["preflight_token"],
            },
        )
        assert copied["imported"] == 2

        archive_preview = await succeed(
            session,
            "workspace_consolidation_preview",
            {
                "workspace_id": target.workspace_id,
                "source_workspace_ids": source_ids,
            },
        )
        archive_arguments = {
            "source_workspace_ids": source_ids,
            "idempotency_key": "process-consolidate-archive-0001",
            "selection_token": archive_preview["selection_token"],
        }
        archive_preflight = await succeed(
            session,
            "memory_preflight",
            {
                "workspace_id": target.workspace_id,
                "target_tool": "workspace_consolidate_and_archive_sources",
                "target_arguments": archive_arguments,
            },
        )
        archived = await succeed(
            session,
            "workspace_consolidate_and_archive_sources",
            {
                "workspace_id": target.workspace_id,
                **archive_arguments,
                "preflight_token": archive_preflight["preflight_token"],
            },
        )
        assert archived["archived"] == 2
        assert all(source.root.is_dir() for source in sources)


async def test_an_installed_extra_is_enabled_without_any_variable(
    initialized_workspace,
):
    """UD-7: installing the extra is the whole opt-in."""
    from daem0nmcp.capabilities import CapabilityRegistry

    if CapabilityRegistry(environ={}).get("graph")["status"] != "ready":
        pytest.skip("the graph extra is not installed")

    workspace = initialized_workspace
    scope = {"workspace_id": workspace.workspace_id}
    async with process_client(workspace.root, "stdio") as session:
        await succeed(session, "session_brief", scope)
        health = await succeed(session, "system_health", scope)

    states = {state["name"]: state["status"] for state in health["capability_states"]}
    assert states["graph"] == "ready", states
