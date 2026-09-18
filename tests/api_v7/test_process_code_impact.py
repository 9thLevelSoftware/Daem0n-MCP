"""Real production MCP coverage for generation-scoped code impact."""

from __future__ import annotations

import pytest

from daem0nmcp.api.v7.public_ids import (
    PublicObjectKind,
    derive_public_object_id,
)
from daem0nmcp.database import DatabaseManager
from daem0nmcp.workspace import WorkspaceRegistry
from tests.api_v7.process_client import call, process_client, succeed


@pytest.fixture
async def code_workspace(tmp_path):
    storage = tmp_path / ".daem0nmcp" / "storage"
    storage.mkdir(parents=True)
    manager = DatabaseManager(str(storage))
    try:
        await manager.init_db()
    finally:
        await manager.close()
    source = tmp_path / "src"
    source.mkdir()
    (source / "core.py").write_text("def target():\n    return 1\n", encoding="utf-8")
    (source / "service.py").write_text(
        "from .core import target as chosen\n\ndef middle():\n    return chosen()\n",
        encoding="utf-8",
    )
    (source / "api.py").write_text(
        "from service import middle\n\ndef entry():\n    return middle()\n"
        "\ndef unrelated(unknown):\n    return unknown.target()\n"
        "\ndef dynamic():\n    return factory().target()\n",
        encoding="utf-8",
    )
    return WorkspaceRegistry([tmp_path], default_root=tmp_path).default


@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
async def test_production_code_index_search_and_impact(code_workspace, transport):
    workspace = code_workspace
    scope = {"workspace_id": workspace.workspace_id}
    environment = {"DAEM0NMCP_APPS_ENABLED": "true"}
    async with process_client(
        workspace.root,
        transport,
        environment_overrides=environment,
    ) as session:
        await succeed(session, "session_brief", scope)
        indexed = await succeed(
            session,
            "code_index",
            {
                **scope,
                "relative_root": "src",
                "patterns": ["**/*.py"],
            },
        )
        assert indexed["files_indexed"] == 3
        searched = await succeed(
            session,
            "code_search",
            {**scope, "query": "core.target"},
        )
        assert len(searched["items"]) == 1
        target_id = searched["items"][0]["code_entity_id"]
        impact = await succeed(
            session,
            "code_impact_analyze",
            {**scope, "code_entity_id": target_id, "max_depth": 2},
        )
        assert impact["subject"]["qualified_name"] == "core.target"
        assert [item["qualified_name"] for item in impact["affected"]] == [
            "service.middle",
            "api.entry",
        ]
        assert len(impact["paths"][-1]["entities"]) == 3

        malformed = await session.call_tool(
            "code_impact_analyze",
            {
                **scope,
                "code_entity_id": target_id,
                "qualified_name": "core.target",
            },
        )
        assert malformed.isError

        foreign_id = derive_public_object_id(
            "ws_0123456789abcdef01234567",
            PublicObjectKind.CODE,
            "foreign-target",
            1,
        )
        foreign = await call(
            session,
            "code_impact_analyze",
            {**scope, "code_entity_id": foreign_id},
        )
        assert not foreign["ok"]
        assert foreign["error"]["code"] == "NOT_FOUND"

        escaped = await session.call_tool(
            "code_index",
            {**scope, "relative_root": "../other", "patterns": ["**/*.py"]},
        )
        assert escaped.isError


async def test_production_code_index_disabled_profile_has_remedy(code_workspace):
    scope = {"workspace_id": code_workspace.workspace_id}
    async with process_client(code_workspace.root, "stdio") as session:
        await succeed(session, "session_brief", scope)
        disabled = await call(
            session,
            "code_index",
            {**scope, "relative_root": "src", "patterns": ["**/*.py"]},
        )
        assert not disabled["ok"]
        assert disabled["error"]["code"] == "CAPABILITY_DISABLED"
        states = disabled["meta"]["capability_states"]
        assert states[0]["name"] == "apps"
        assert "DAEM0NMCP_APPS_ENABLED=true" in states[0]["remediation"]
