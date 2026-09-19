"""Workspace isolation and host-path leak sweep across every v7 tool and resource.

Pass 1 drives the real production server in-process as the default loopback
principal, which may use both registered workspaces.  Every manifest tool and
workspace resource is called against workspace B with workspace A's IDs,
cursors, tokens and idempotency keys.  No B response may carry A's seeded text
or IDs, A's database may not change, a rejected call may not change B, and no
response field outside user text may carry a registered root, a storage path
or the home directory.

Pass 2 relaunches the same workspaces over HTTP with a JWT principal granted
only A.  Every call that targets B, directly or as a consolidation or
federation source, must return UNAUTHORIZED_WORKSPACE.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from mcp.shared.exceptions import McpError

from daem0nmcp.api.v7.policy import V7_TOOL_LEVELS
from daem0nmcp.api.v7.production import create_v7_server
from daem0nmcp.config import Settings
from daem0nmcp.covenant import CovenantLevel
from daem0nmcp.storage_activation import resolve_active_database
from tests.api_v7.process_client import (
    call,
    initialize_workspaces,
    jwt_environment,
    jwt_issuer,
    process_client,
    succeed,
    write_workspace_grants,
)

CANARY = "canary7f3a9d1e"

# Tools whose purpose is to read or move data between workspaces.  Each is
# gated by a directional link (or a preview token that required one) plus an
# authorization check for every workspace involved; see the cross checks below.
CROSS_WORKSPACE_TOOLS = (
    "workspace_consolidation_preview",
    "workspace_consolidate",
    "workspace_consolidate_and_archive_sources",
    "workspace_link",
    "workspace_unlink",
)

# Fields that echo text the caller wrote.  They are exempt from the host-path
# assertion only (KD-2); A's seeded text and IDs are forbidden in every field.
USER_TEXT_FIELDS = frozenset(
    {
        "content",
        "excerpt",
        "bounded_excerpt",
        "rationale",
        "context",
        "outcome_text",
        "summary",
        "text",
        "description",
        "label",
        "topic",
        "trigger",
        "recall_query",
        "proposed_action",
    }
)

# Derived tables that background projection jobs rewrite after a commit,
# including lazily allocated projection-scoped public IDs.
_DERIVED_TABLE_PREFIXES = (
    "retrieval_",
    "discovery_",
    "dense_",
    "memories_fts",
    "projection_manifests",
    "background_jobs",
    "record_outcome_view",
    "public_object_ids",
    "sqlite_",
)

Invoke = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def _placeholder(prefix: str) -> str:
    return f"{prefix}_{hashlib.sha256(prefix.encode()).hexdigest()}"


def _ledger_counts(root: Path) -> dict[str, int]:
    database = resolve_active_database(root / ".daem0nmcp" / "storage").path
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as db:
        tables = [
            name
            for (name,) in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            if not name.startswith(_DERIVED_TABLE_PREFIXES)
        ]
        return {
            name: db.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
            for name in tables
        }


def _strings(value: Any, key: str | None = None):
    if isinstance(value, dict):
        for child_key, child in value.items():
            yield from _strings(child, child_key)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child, key)
    elif isinstance(value, str):
        yield key, value


class Seed:
    """Everything workspace A owns that B must never see or resolve."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.ids: dict[str, str] = {}
        self.tokens: dict[str, str] = {}

    def text(self, label: str) -> str:
        value = f"{CANARY} {label} owned by workspace A"
        self.texts.append(value)
        return value

    def id(self, name: str, prefix: str) -> str:
        return self.ids.get(name) or _placeholder(prefix)

    def token(self, name: str) -> str:
        return self.tokens.get(name) or f"{name}-{CANARY}-placeholder"

    @staticmethod
    def key(name: str) -> str:
        """A's idempotency keys, replayed against B."""
        return f"sweep-{name}-0001"


async def _protected(invoke: Invoke, workspace_id: str, tool: str, arguments: dict):
    grant = await invoke(
        "memory_preflight",
        {
            "workspace_id": workspace_id,
            "target_tool": tool,
            "target_arguments": arguments,
        },
    )
    if not grant["ok"]:
        return grant
    return await invoke(
        tool,
        {
            "workspace_id": workspace_id,
            **arguments,
            "preflight_token": grant["data"]["preflight_token"],
        },
    )


async def _seed_workspace_a(invoke: Invoke, a: str, b: str) -> Seed:
    seed = Seed()

    async def ok(tool, arguments, protected=False):
        result = await (
            _protected(invoke, a, tool, arguments)
            if protected
            else invoke(tool, {"workspace_id": a, **arguments})
        )
        assert result["ok"], f"seed {tool}: {result['error']}"
        return result["data"]

    async def optional(tool, arguments):
        result = await invoke(tool, {"workspace_id": a, **arguments})
        return result["data"] if result["ok"] else None

    await ok("session_brief", {})
    for name, record_type in (("record", "decision"), ("other_record", "warning")):
        stored = await ok(
            "memory_store",
            {
                "record_type": record_type,
                "content": seed.text(f"{name} content"),
                "rationale": seed.text(f"{name} rationale"),
                "relative_file_path": "src/owned.py",
                "idempotency_key": seed.key(name),
            },
            protected=True,
        )
        seed.ids[name] = stored["record"]["record_id"]
        seed.ids[f"{name}_event"] = stored["event_id"]
    outcome = await ok(
        "memory_record_outcome",
        {
            "record_id": seed.ids["record"],
            "outcome_text": seed.text("outcome"),
            "worked": False,
            "idempotency_key": seed.key("outcome"),
        },
    )
    seed.ids["outcome_event"] = outcome["outcome_event_id"]
    linked = await ok(
        "memory_link",
        {
            "source_record_id": seed.ids["record"],
            "target_record_id": seed.ids["other_record"],
            "relationship_type": "related_to",
            "idempotency_key": seed.key("link"),
        },
        protected=True,
    )
    seed.ids["relationship"] = next(
        value for value in linked["affected_ids"] if value.startswith("rel_")
    )
    rule = await ok(
        "rule_create",
        {
            "trigger": seed.text("rule trigger"),
            "must_do": [seed.text("rule must do")],
            "idempotency_key": seed.key("rule"),
        },
        protected=True,
    )
    seed.ids["rule"] = rule["rule_id"]
    trigger = await ok(
        "context_trigger_create",
        {
            "trigger_type": "file",
            "pattern": "src/**/*.py",
            "recall_query": seed.text("trigger recall query"),
            "idempotency_key": seed.key("trigger"),
        },
        protected=True,
    )
    seed.ids["trigger"] = trigger["trigger_id"]
    active = await ok(
        "active_context_add",
        {"record_id": seed.ids["record"], "reason": seed.text("active reason")},
        protected=True,
    )
    seed.ids["active_context"] = active["active_context_id"]
    page = await ok("active_context_list", {})
    seed.tokens["active_context_selection"] = page["selection_token"]
    search = await ok("memory_search_text", {"query": CANARY, "limit": 1})
    seed.tokens["cursor"] = search["next_cursor"]
    updates = await ok("session_updates_get", {})
    seed.tokens["session_cursor"] = updates["cursor"]
    for tool, arguments in (
        ("memory_prune_preview", {}),
        ("memory_duplicates_preview", {}),
        ("memory_compaction_preview", {"summary": "sweep", "query": CANARY}),
        ("dream_duplicates_preview", {}),
    ):
        data = await optional(tool, arguments)
        if data and data.get("selection_token"):
            seed.tokens[tool] = data["selection_token"]
    export = await ok("workspace_export", {})
    if export.get("export_session_id"):
        seed.ids["export_session"] = export["export_session_id"]
    # A consolidation preview needs a linked source with at least one record.
    assert (await invoke("session_brief", {"workspace_id": b}))["ok"]
    b_record = {
        "record_type": "decision",
        "content": "Workspace B owns this decision.",
        "idempotency_key": "sweep-b-own-record-0001",
    }
    assert (await _protected(invoke, b, "memory_store", b_record))["ok"]
    await ok("workspace_link", {"linked_workspace_id": b}, protected=True)
    preview = await ok("workspace_consolidation_preview", {"source_workspace_ids": [b]})
    seed.tokens["consolidation"] = preview["selection_token"]
    # Optional-profile objects: real IDs when the profile is installed.
    code = await optional("code_index", {"relative_root": "src"})
    if code is not None:
        found = await optional("code_search", {"query": CANARY})
        if found and found["items"]:
            seed.ids["code_entity"] = found["items"][0]["code_entity_id"]
    entities = await optional("entity_list", {})
    if entities and entities["items"]:
        seed.ids["entity"] = entities["items"][0]["entity_id"]
    communities = await optional("community_list", {})
    if communities and communities["items"]:
        seed.ids["community"] = communities["items"][0]["community_id"]
    return seed


def _sweep_arguments(seed: Seed) -> dict[str, dict[str, Any]]:
    """Arguments for every non-cross tool, aimed at B but naming A's objects."""

    record = seed.id("record", "mem")
    cursor = seed.token("cursor")
    now = datetime.now(timezone.utc).isoformat()
    return {
        "active_context_add": {"record_id": record, "reason": "sweep"},
        "active_context_clear": {
            "selection_token": seed.token("active_context_selection")
        },
        "active_context_list": {"cursor": cursor},
        "active_context_remove": {
            "active_context_id": seed.id("active_context", "act")
        },
        "code_impact_analyze": {
            "code_entity_id": seed.id("code_entity", "code"),
        },
        "code_index": {"relative_root": "src"},
        "code_refactor_propose": {"relative_file_path": "src/owned.py"},
        "code_search": {"query": CANARY, "cursor": cursor},
        "code_todos_scan": {"relative_root": "src", "cursor": cursor},
        "code_todos_scan_and_store": {
            "relative_root": "src",
            "idempotency_key": seed.key("record"),
        },
        "community_get": {"community_id": seed.id("community", "com")},
        "community_list": {
            "parent_community_id": seed.id("community", "com"),
            "cursor": cursor,
        },
        "community_rebuild": {"idempotency_key": seed.key("record")},
        "context_compress": {"text": "Isolation sweep neutral text. " * 8},
        "context_trigger_create": {
            "trigger_type": "file",
            "pattern": "src/**/*.py",
            "recall_query": "sweep",
            "idempotency_key": seed.key("trigger"),
        },
        "context_trigger_delete": {"trigger_id": seed.id("trigger", "trg")},
        "context_trigger_list": {"cursor": cursor},
        "context_triggers_match": {"relative_file_path": "src/owned.py"},
        "covenant_status": {},
        "decision_debate": {
            "topic": "sweep",
            "advocate_position": "sweep for",
            "challenger_position": "sweep against",
            "idempotency_key": seed.key("record"),
        },
        "decision_simulate": {"record_id": record},
        "document_ingest_url": {
            "url": "https://127.0.0.1/owned",
            "topic": "sweep",
            "idempotency_key": seed.key("record"),
        },
        "dream_duplicates_preview": {},
        "dream_duplicates_purge": {
            "selection_token": seed.token("dream_duplicates_preview")
        },
        "edit_preflight": {
            "edit_request_id": _placeholder("edt"),
            "description": "sweep",
        },
        "entity_backfill": {"idempotency_key": seed.key("record")},
        "entity_evolution_trace": {"entity_id": seed.id("entity", "ent")},
        "entity_list": {"cursor": cursor},
        "knowledge_graph_get": {"record_ids": [record], "query": CANARY},
        "knowledge_graph_render": {"record_ids": [record], "query": CANARY},
        "knowledge_graph_stats": {},
        "memory_archive_set": {"record_id": record, "archived": True},
        "memory_at_time_get": {"record_id": record, "valid_time": now},
        "memory_capture_list": {"cursor": cursor},
        "memory_capture_promote": {
            "candidate_id": _placeholder("cap"),
            "record_type": "decision",
            "content": "sweep",
            "idempotency_key": seed.key("record"),
        },
        "memory_chain_trace": {
            "start_record_id": record,
            "end_record_id": seed.id("other_record", "mem"),
        },
        "memory_compact": {
            "summary": "sweep",
            "selection_token": seed.token("memory_compaction_preview"),
            "idempotency_key": seed.key("record"),
        },
        "memory_compaction_preview": {"summary": "sweep", "query": CANARY},
        "memory_duplicates_cleanup": {
            "selection_token": seed.token("memory_duplicates_preview")
        },
        "memory_duplicates_preview": {},
        "memory_link": {
            "source_record_id": record,
            "target_record_id": seed.id("other_record", "mem"),
            "relationship_type": "related_to",
            "idempotency_key": seed.key("link"),
        },
        "memory_pin_set": {"record_id": record, "pinned": True},
        "memory_preflight": {
            "target_tool": "memory_archive_set",
            "target_arguments": {"record_id": record, "archived": True},
        },
        "memory_prune": {"selection_token": seed.token("memory_prune_preview")},
        "memory_prune_preview": {},
        "memory_recall": {"query": CANARY, "record_ids": [record]},
        "memory_recall_entity": {"entity_id": seed.id("entity", "ent")},
        "memory_recall_file": {
            "relative_file_path": "src/owned.py",
            "cursor": cursor,
        },
        "memory_recall_hierarchical": {"query": CANARY},
        "memory_record_outcome": {
            "record_id": record,
            "outcome_text": "sweep",
            "worked": True,
            "idempotency_key": seed.key("outcome"),
        },
        "memory_related": {"record_id": record},
        "memory_search_text": {"query": CANARY, "cursor": cursor},
        "memory_store": {
            "record_type": "decision",
            "content": "Workspace B sweep decision.",
            "idempotency_key": seed.key("record"),
        },
        "memory_store_batch": {
            "records": [{"record_type": "learning", "content": "B batch sweep."}],
            "idempotency_key": seed.key("other_record"),
        },
        "memory_unlink": {"relationship_id": seed.id("relationship", "rel")},
        "memory_verify": {"text": CANARY},
        "memory_versions_list": {"record_id": record, "cursor": cursor},
        "projection_rebuild": {"projection": "lexical"},
        "rule_check": {"proposed_action": CANARY},
        "rule_create": {"trigger": "sweep", "idempotency_key": seed.key("rule")},
        "rule_evolution_analyze": {"rule_id": seed.id("rule", "rule")},
        "rule_list": {"cursor": cursor},
        "rule_update": {
            "rule_id": seed.id("rule", "rule"),
            "patch": {"enabled": False},
        },
        "sandbox_execute_python": {"code": "print('sweep')"},
        "session_brief": {},
        "session_updates_get": {"after_cursor": seed.token("session_cursor")},
        "system_health": {"include_components": True},
        "workspace_export": {
            "export_session_id": seed.id("export_session", "xpt"),
            "cursor": cursor,
        },
        "workspace_import": {
            "import_session_id": seed.id("export_session", "xpt"),
            "idempotency_key": seed.key("record"),
        },
        "workspace_links_list": {"cursor": cursor},
    }


def _cross_arguments(seed: Seed, source: str) -> dict[str, dict[str, Any]]:
    token = seed.token("consolidation")
    return {
        "workspace_consolidation_preview": {"source_workspace_ids": [source]},
        "workspace_consolidate": {
            "source_workspace_ids": [source],
            "selection_token": token,
            "idempotency_key": "sweep-consolidate-0001",
        },
        "workspace_consolidate_and_archive_sources": {
            "source_workspace_ids": [source],
            "selection_token": token,
            "idempotency_key": "sweep-consolidate-archive-0001",
        },
        "workspace_link": {"linked_workspace_id": source},
        "workspace_unlink": {"linked_workspace_id": source},
    }


def _is_protected(tool: str) -> bool:
    return V7_TOOL_LEVELS[tool] in {CovenantLevel.COUNSEL, CovenantLevel.DESTRUCTIVE}


def test_every_manifest_tool_is_swept_or_declared_cross_workspace():
    swept = set(_sweep_arguments(Seed()))
    cross = set(CROSS_WORKSPACE_TOOLS)
    assert not swept & cross
    assert swept | cross == set(V7_TOOL_LEVELS), "unswept tools: " + ", ".join(
        sorted(set(V7_TOOL_LEVELS) - swept - cross)
    )


async def test_workspace_isolation_and_path_leak_sweep(tmp_path, capsys):
    roots = (tmp_path / "alpha", tmp_path / "beta")
    alpha, beta = await initialize_workspaces(roots)
    a, b = alpha.workspace_id, beta.workspace_id
    source = roots[0] / "src"
    source.mkdir()
    (source / "owned.py").write_text(
        f"def {CANARY}_owned_function():\n    return 1  # TODO: {CANARY} owned todo\n",
        encoding="utf-8",
    )
    settings = Settings(
        project_root=str(roots[0]),
        workspace_roots=[str(root) for root in roots],
        dream_enabled=False,
    )
    server = create_v7_server(
        "stdio",
        settings=settings,
        environ={
            "DAEM0NMCP_PROFILE": "core",
            "DAEM0NMCP_APPS_ENABLED": "true",
            "DAEM0NMCP_GRAPH_ENABLED": "true",
        },
    )
    path_needles = {
        str(path).lower()
        for base in (*roots, Path.home())
        for path in (base, base.resolve())
    }
    path_needles |= {str(root / ".daem0nmcp" / "storage").lower() for root in roots}
    path_needles |= {Path(needle).as_posix().lower() for needle in path_needles}

    swept: list[str] = []
    async with Client(server) as client:

        async def invoke(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
            result = await client.call_tool(tool, arguments, raise_on_error=False)
            assert isinstance(result.structured_content, dict), tool
            return result.structured_content

        served = {tool.name for tool in await client.list_tools()}
        assert served == set(V7_TOOL_LEVELS)
        seed = await _seed_workspace_a(invoke, a, b)
        forbidden = [*seed.texts, *seed.ids.values(), a]
        a_ledger = _ledger_counts(roots[0])

        def assert_isolated(label: str, response: Any) -> None:
            for key, value in _strings(response):
                for secret in forbidden:
                    assert secret not in value, f"{label}: A's {secret!r} in {key}"
                if key in USER_TEXT_FIELDS:
                    continue
                lowered = value.lower()
                for needle in path_needles:
                    assert needle not in lowered, f"{label}: host path in {key}"
            assert _ledger_counts(roots[0]) == a_ledger, f"{label} changed A"

        assert (await invoke("session_brief", {"workspace_id": b}))["ok"]
        for tool, arguments in sorted(_sweep_arguments(seed).items()):
            variants = [arguments]
            if {"cursor", "after_cursor"} & set(arguments):
                # A's cursor is refused, so also read B's own first page.
                variants.append(
                    {
                        key: value
                        for key, value in arguments.items()
                        if key not in {"cursor", "after_cursor"}
                    }
                )
            for variant in variants:
                before = _ledger_counts(roots[1])
                if _is_protected(tool):
                    response = await _protected(invoke, b, tool, variant)
                else:
                    response = await invoke(tool, {"workspace_id": b, **variant})
                assert_isolated(tool, response)
                if not response["ok"]:
                    assert _ledger_counts(roots[1]) == before, (
                        f"rejected {tool} wrote B"
                    )
            swept.append(tool)

        templates = await client.list_resource_templates()
        workspace_templates = [
            template.uriTemplate
            for template in templates
            if template.uriTemplate.startswith("memory://workspaces/{workspace_id}/")
        ]
        assert workspace_templates
        for template in workspace_templates:
            uri = template.replace("{workspace_id}", b)
            contents = await client.read_resource(uri)
            assert_isolated(uri, [json.loads(content.text) for content in contents])
            swept.append(uri)

        # Cross-workspace tools: B has no link to A (only A -> B exists), so
        # every B-side read or move of A must be refused; a federated recall is
        # refused the same way.
        federated = await invoke(
            "memory_recall",
            {"workspace_id": b, "query": CANARY, "linked_workspace_ids": [a]},
        )
        assert not federated["ok"]
        assert_isolated("memory_recall linked", federated)
        for tool, arguments in _cross_arguments(seed, a).items():
            if tool in {"workspace_link", "workspace_unlink"}:
                continue
            before = _ledger_counts(roots[1])
            if _is_protected(tool):
                response = await _protected(invoke, b, tool, arguments)
            else:
                response = await invoke(tool, {"workspace_id": b, **arguments})
            assert not response["ok"], f"{tool} crossed without a link"
            assert_isolated(tool, response)
            assert _ledger_counts(roots[1]) == before
        # Linking needs a preflight bound to the exact target, then the
        # direction it names is what consolidation honours.
        unbound = await invoke(
            "workspace_link",
            {
                "workspace_id": b,
                "linked_workspace_id": a,
                "preflight_token": seed.token("consolidation"),
            },
        )
        assert not unbound["ok"]
        link = _cross_arguments(seed, a)["workspace_link"]
        assert (await _protected(invoke, b, "workspace_link", link))["ok"]
        preview = await invoke(
            "workspace_consolidation_preview",
            {"workspace_id": b, "source_workspace_ids": [a]},
        )
        assert preview["ok"], preview["error"]
        unlink = _cross_arguments(seed, a)["workspace_unlink"]
        assert (await _protected(invoke, b, "workspace_unlink", unlink))["ok"]
        after_unlink = await invoke(
            "workspace_consolidation_preview",
            {"workspace_id": b, "source_workspace_ids": [a]},
        )
        assert not after_unlink["ok"]

    # Pass 2: a JWT principal granted only A is refused B everywhere.
    policy_path = tmp_path / "protected" / "access.json"
    write_workspace_grants(policy_path, [a])
    with jwt_issuer() as (issuer_url, token):
        async with process_client(
            roots[0],
            "streamable-http",
            workspace_roots=roots,
            environment_overrides=jwt_environment(issuer_url, policy_path),
            http_headers={"Authorization": "Bearer " + token()},
        ) as session:

            async def remote(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
                return await call(session, tool, arguments)

            await succeed(session, "session_brief", {"workspace_id": a})
            placeholder_token = f"preflight-{CANARY}-placeholder"
            targets = {
                **_sweep_arguments(seed),
                **_cross_arguments(seed, a),
            }
            for tool, arguments in sorted(targets.items()):
                if _is_protected(tool):
                    arguments = {**arguments, "preflight_token": placeholder_token}
                denied = await remote(tool, {"workspace_id": b, **arguments})
                assert denied["error"]["code"] == "UNAUTHORIZED_WORKSPACE", tool
            for template in workspace_templates:
                with pytest.raises(McpError):
                    await session.read_resource(template.replace("{workspace_id}", b))
            # B as a source of an A-side federated read or consolidation.
            denied = await remote(
                "memory_recall",
                {"workspace_id": a, "query": CANARY, "linked_workspace_ids": [b]},
            )
            assert denied["error"]["code"] == "UNAUTHORIZED_WORKSPACE"
            for tool, arguments in _cross_arguments(seed, b).items():
                if _is_protected(tool):
                    denied = await _protected(remote, a, tool, arguments)
                else:
                    denied = await remote(tool, {"workspace_id": a, **arguments})
                assert denied["error"]["code"] == "UNAUTHORIZED_WORKSPACE", tool

    with capsys.disabled():
        print(f"\nswept ({len(swept)}): {', '.join(swept)}")
        print(
            f"cross-workspace ({len(CROSS_WORKSPACE_TOOLS)}): "
            + ", ".join(CROSS_WORKSPACE_TOOLS)
        )
