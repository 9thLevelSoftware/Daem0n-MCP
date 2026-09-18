"""Opt-in network certification through the production MCP transports.

An absent opt-in is an open certification gate, not successful service evidence.
"""

from __future__ import annotations

import os

import pytest

from daem0nmcp.workspace import WorkspaceRegistry
from tests.api_v7.process_client import process_client, succeed
from tests.api_v7.test_process_surface import _preflight


@pytest.mark.skipif(
    os.environ.get("DAEM0NMCP_CERTIFY_PUBLIC_INGEST") != "1",
    reason="External URL certification is open without an explicit network run",
)
@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
async def test_actual_public_document_ingestion(tmp_path, transport):
    workspace = WorkspaceRegistry([tmp_path], default_root=tmp_path).default
    scope = {"workspace_id": workspace.workspace_id}
    async with process_client(
        tmp_path,
        transport,
        environment_overrides={"DAEM0NMCP_APPS_ENABLED": "true"},
    ) as session:
        await succeed(session, "session_brief", scope)
        arguments = {
            "url": "https://example.com/",
            "topic": "Public example domain certification",
            "chunk_size": 256,
            "idempotency_key": "public-ingest-certification-0001",
        }
        result = await succeed(
            session,
            "document_ingest_url",
            {
                **scope,
                **arguments,
                "preflight_token": await _preflight(
                    session, scope, "document_ingest_url", arguments
                ),
            },
        )
        assert result["source"]["url"] == arguments["url"]
        assert len(result["source"]["content_hash"]) == 64
        assert result["records"] and len(result["records"]) == len(result["event_ids"])
        replay = await succeed(
            session,
            "document_ingest_url",
            {
                **scope,
                **arguments,
                "preflight_token": await _preflight(
                    session, scope, "document_ingest_url", arguments
                ),
            },
        )
        assert replay["event_ids"] == result["event_ids"]
        bundle = await succeed(
            session,
            "workspace_export",
            {
                **scope,
                "include_legacy_projection": False,
            },
        )
        assert len(bundle["events"]) == len(result["event_ids"])
        for event in bundle["events"]:
            state = event["payload"]["data"]["record"]
            assert state["context"]["document"]["url"] == arguments["url"]
            assert (
                state["context"]["document"]["content_hash"]
                == result["source"]["content_hash"]
            )
        assert "Example Domain" in " ".join(
            event["payload"]["data"]["record"]["content"] for event in bundle["events"]
        )
