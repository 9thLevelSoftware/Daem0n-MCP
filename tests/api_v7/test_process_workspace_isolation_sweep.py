"""Workspace isolation and host-path leak sweep across every v7 tool and resource.

Pass 1 drives the real production server in-process as the default loopback
principal, which may use every registered workspace.  Every manifest tool and
workspace resource is called against a target workspace with workspace A's
IDs, cursors, tokens and idempotency keys, then again with the target's own
arguments where A's would only be refused.  Two targets are swept:

* B, an ordinary second workspace with its own database;
* C, a registered root whose storage is a byte copy of A's (a re-cloned or
  copied project).  C's database physically holds A's rows, so only the
  ``workspace_id`` predicates keep them out of C's responses.

No target response may carry A's text, A's canary token, A's IDs or A's
workspace ID; A's database may not change; a rejected call may not change the
target; and no string or key anywhere may carry a registered root or the home
directory.  Every call's outcome is pinned.

Pass 2 relaunches A and B over HTTP with a JWT principal granted only A.
Every call that targets B, directly or as a consolidation or federation
source, must return UNAUTHORIZED_WORKSPACE and write nothing.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
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
from daem0nmcp.workspace import WorkspaceRegistry
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
APPS_AVAILABLE = importlib.util.find_spec("tree_sitter_language_pack") is not None
PROFILE_ENVIRONMENT = {
    "DAEM0NMCP_PROFILE": "core",
    "DAEM0NMCP_APPS_ENABLED": "true",
    "DAEM0NMCP_GRAPH_ENABLED": "true",
}

# Tools whose purpose is to read or move data between workspaces.  Each needs a
# directional link (or a preview token that required one) plus authorization
# for every workspace involved; see the cross checks below.
CROSS_WORKSPACE_TOOLS = (
    "workspace_consolidation_preview",
    "workspace_consolidate",
    "workspace_consolidate_and_archive_sources",
    "workspace_link",
    "workspace_unlink",
)

# Responses that stop at a gate before any workspace data is read.  Tools whose
# every response is one of these are reported separately from the swept list.
GATE_CODES = frozenset({"TASK_REQUIRED", "CAPABILITY_DEGRADED", "CAPABILITY_DISABLED"})

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


def _strings(value: Any):
    """Every string in a JSON value, dictionary keys included."""
    if isinstance(value, dict):
        for child_key, child in value.items():
            yield str(child_key)
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)
    elif isinstance(value, str):
        yield value


def _invoker(client: Client) -> Invoke:
    async def invoke(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await client.call_tool(tool, arguments, raise_on_error=False)
        assert isinstance(result.structured_content, dict), tool
        return result.structured_content

    return invoke


def _code(response: dict[str, Any]) -> str:
    return "ok" if response["ok"] else response["error"]["code"]


class Seed:
    """Everything workspace A owns that another workspace must never see."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.ids: dict[str, str] = {}
        self.tokens: dict[str, str] = {}
        self.placeholders: set[str] = set()

    def text(self, label: str) -> str:
        value = f"{CANARY} {label} owned by workspace A"
        self.texts.append(value)
        return value

    def id(self, name: str, prefix: str) -> str:
        if name not in self.ids:
            self.placeholders.add(name)
            return _placeholder(prefix)
        return self.ids[name]

    def token(self, name: str) -> str:
        if name not in self.tokens:
            self.placeholders.add(name)
            return f"{name}-{CANARY}-placeholder"
        return self.tokens[name]

    @staticmethod
    def key(name: str) -> str:
        """A's idempotency keys, replayed against the target."""
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


async def _target_call(invoke: Invoke, workspace_id: str, tool: str, arguments: dict):
    if _is_protected(tool):
        return await _protected(invoke, workspace_id, tool, arguments)
    return await invoke(tool, {"workspace_id": workspace_id, **arguments})


async def _seed_workspace_a(invoke: Invoke, a: str, b: str) -> Seed:
    """Write A's objects (and one B record so a consolidation preview exists)."""
    seed = Seed()

    async def ok(tool, arguments):
        result = await _target_call(invoke, a, tool, arguments)
        assert result["ok"], f"seed {tool}: {result['error']}"
        return result["data"]

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
    )
    seed.ids["trigger"] = trigger["trigger_id"]
    active = await ok(
        "active_context_add",
        {"record_id": seed.ids["record"], "reason": seed.text("active reason")},
    )
    seed.ids["active_context"] = active["active_context_id"]
    if APPS_AVAILABLE:
        await ok("code_index", {"relative_root": "src"})
        found = await ok("code_search", {"query": CANARY})
        seed.ids["code_entity"] = found["items"][0]["code_entity_id"]
    # The core graph profile extracts no named entities, so entity and
    # community IDs stay placeholders unless an extractor is installed.
    await ok("entity_backfill", {"idempotency_key": seed.key("entity_backfill")})
    entities = await ok("entity_list", {})
    if entities["items"]:
        seed.ids["entity"] = entities["items"][0]["entity_id"]
    communities = await ok("community_list", {})
    if communities["items"]:
        seed.ids["community"] = communities["items"][0]["community_id"]
    # A consolidation preview needs a linked source with at least one record.
    assert (await invoke("session_brief", {"workspace_id": b}))["ok"]
    b_record = {
        "record_type": "decision",
        "content": "Workspace B owns this decision.",
        "idempotency_key": "sweep-b-own-record-0001",
    }
    assert (await _protected(invoke, b, "memory_store", b_record))["ok"]
    await ok("workspace_link", {"linked_workspace_id": b})
    return seed


async def _mint_tokens(invoke: Invoke, a: str, b: str, seed: Seed) -> None:
    """Cursors and tokens are bound to one server process; mint them per run."""

    async def ok(tool, arguments):
        result = await invoke(tool, {"workspace_id": a, **arguments})
        assert result["ok"], f"mint {tool}: {result['error']}"
        return result["data"]

    await ok("session_brief", {})
    seed.tokens["active_context_selection"] = (await ok("active_context_list", {}))[
        "selection_token"
    ]
    seed.tokens["cursor"] = (
        await ok("memory_search_text", {"query": CANARY, "limit": 1})
    )["next_cursor"]
    seed.tokens["session_cursor"] = (await ok("session_updates_get", {}))["cursor"]
    for tool, arguments in (
        ("memory_prune_preview", {}),
        ("memory_duplicates_preview", {}),
        ("memory_compaction_preview", {"summary": "sweep", "query": CANARY}),
        ("dream_duplicates_preview", {}),
    ):
        seed.tokens[tool] = (await ok(tool, arguments))["selection_token"]
    export = await ok("workspace_export", {})
    seed.ids["export_session"] = export["export_session_id"]
    preview = await ok("workspace_consolidation_preview", {"source_workspace_ids": [b]})
    seed.tokens["consolidation"] = preview["selection_token"]


def _sweep_arguments(seed: Seed) -> dict[str, dict[str, Any]]:
    """Arguments for every non-cross tool, naming A's objects."""

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
        "entity_backfill": {"idempotency_key": seed.key("entity_backfill")},
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
            "content": "Target sweep decision.",
            "idempotency_key": seed.key("record"),
        },
        "memory_store_batch": {
            "records": [{"record_type": "learning", "content": "Target batch sweep."}],
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


# The target's own arguments for tools whose A-argument call is only refused,
# so their success responses are scanned too.  They run first, so the target
# is indexed before A's code entity is looked up in it.
OWN_VARIANTS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("code_index", {"relative_root": "src"}),
    ("code_search", {"query": "neutral"}),
    ("code_todos_scan", {"relative_root": "src"}),
    ("code_refactor_propose", {"relative_file_path": "src/neutral.py"}),
    ("workspace_export", {}),
)

# Pinned outcomes per tool, in call order: A's arguments, then the variant
# without A's cursor or session (when there is one), then the target's own
# arguments (OWN_VARIANTS).  B and the copied-storage C behave identically.
EXPECTED: dict[str, tuple[str, ...]] = {
    "active_context_add": ("NOT_FOUND",),
    "active_context_clear": ("TOKEN_TAMPERED",),
    "active_context_list": (
        "INVALID_ARGUMENT",
        "ok",
    ),
    "active_context_remove": ("NOT_FOUND",),
    "code_impact_analyze": ("NOT_FOUND",),
    "code_index": (
        "ok",
        "ok",
    ),
    "code_refactor_propose": (
        "WORKSPACE_PATH_ESCAPE",
        "ok",
    ),
    "code_search": (
        "INVALID_ARGUMENT",
        "ok",
        "ok",
    ),
    "code_todos_scan": (
        "INVALID_ARGUMENT",
        "ok",
        "ok",
    ),
    "code_todos_scan_and_store": ("ok",),
    "community_get": ("CAPABILITY_DEGRADED",),
    "community_list": (
        "CAPABILITY_DEGRADED",
        "CAPABILITY_DEGRADED",
    ),
    "community_rebuild": ("ok",),
    "context_compress": ("ok",),
    "context_trigger_create": ("ok",),
    "context_trigger_delete": ("NOT_FOUND",),
    "context_trigger_list": (
        "INVALID_ARGUMENT",
        "ok",
    ),
    "context_triggers_match": ("ok",),
    "covenant_status": ("ok",),
    "decision_debate": ("ok",),
    "decision_simulate": ("NOT_FOUND",),
    "document_ingest_url": ("INVALID_ARGUMENT",),
    "dream_duplicates_preview": ("ok",),
    "dream_duplicates_purge": ("TOKEN_SCOPE_MISMATCH",),
    "edit_preflight": ("NOT_FOUND",),
    "entity_backfill": ("ok",),
    "entity_evolution_trace": ("NOT_FOUND",),
    "entity_list": (
        "INVALID_ARGUMENT",
        "ok",
    ),
    "knowledge_graph_get": ("TASK_REQUIRED",),
    "knowledge_graph_render": ("TASK_REQUIRED",),
    "knowledge_graph_stats": ("ok",),
    "memory_archive_set": ("NOT_FOUND",),
    "memory_at_time_get": ("NOT_FOUND",),
    "memory_capture_list": (
        "INVALID_ARGUMENT",
        "ok",
    ),
    "memory_capture_promote": ("NOT_FOUND",),
    "memory_chain_trace": ("NOT_FOUND",),
    "memory_compact": ("TOKEN_SCOPE_MISMATCH",),
    "memory_compaction_preview": ("ok",),
    "memory_duplicates_cleanup": ("TOKEN_SCOPE_MISMATCH",),
    "memory_duplicates_preview": ("ok",),
    "memory_link": ("NOT_FOUND",),
    "memory_pin_set": ("NOT_FOUND",),
    "memory_preflight": ("ok",),
    "memory_prune": ("TOKEN_SCOPE_MISMATCH",),
    "memory_prune_preview": ("ok",),
    "memory_recall": ("ok",),
    "memory_recall_entity": ("NOT_FOUND",),
    "memory_recall_file": (
        "INVALID_ARGUMENT",
        "ok",
    ),
    "memory_recall_hierarchical": ("ok",),
    "memory_record_outcome": ("NOT_FOUND",),
    "memory_related": ("NOT_FOUND",),
    "memory_search_text": (
        "INVALID_ARGUMENT",
        "ok",
    ),
    "memory_store": ("ok",),
    "memory_store_batch": ("ok",),
    "memory_unlink": ("NOT_FOUND",),
    "memory_verify": ("ok",),
    "memory_versions_list": (
        "NOT_FOUND",
        "NOT_FOUND",
    ),
    "projection_rebuild": ("ok",),
    "rule_check": ("ok",),
    "rule_create": ("ok",),
    "rule_evolution_analyze": ("NOT_FOUND",),
    "rule_list": (
        "INVALID_ARGUMENT",
        "ok",
    ),
    "rule_update": ("NOT_FOUND",),
    "sandbox_execute_python": ("TASK_REQUIRED",),
    "session_brief": ("ok",),
    "session_updates_get": (
        "INVALID_ARGUMENT",
        "ok",
    ),
    "system_health": ("ok",),
    "workspace_export": (
        "IMPORT_INVALID",
        "ok",
        "ok",
    ),
    "workspace_import": ("IMPORT_INVALID",),
    "workspace_links_list": (
        "INVALID_ARGUMENT",
        "ok",
    ),
}


def _cross_arguments(token: str, source: str) -> dict[str, dict[str, Any]]:
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


class Sweep:
    """Pass-1 assertions shared by every target workspace."""

    def __init__(self, roots: dict[str, Path], a_root: Path, seed: Seed, a: str):
        self.a_root = a_root
        self.seed = seed
        self.forbidden = [*seed.texts, *seed.ids.values(), a]
        self.a_ledger = _ledger_counts(a_root)
        needles = {
            str(path).lower()
            for base in (*roots.values(), Path.home())
            for path in (base, base.resolve())
        }
        self.needles = needles | {Path(n).as_posix().lower() for n in needles}
        self.observed: dict[str, dict[str, list[str]]] = {}

    def check(self, label: str, response: Any, request: Any) -> None:
        echoed = set(_strings(request))
        for value in _strings(response):
            for secret in self.forbidden:
                assert secret not in value, f"{label}: A's {secret!r} leaked"
            # A bare canary may appear only as an exact echo of the request
            # (memory_verify returns its claim).
            assert CANARY not in value or value in echoed, f"{label}: {value!r}"
            lowered = value.lower()
            for needle in self.needles:
                assert needle not in lowered, f"{label}: host path in {value!r}"
        assert _ledger_counts(self.a_root) == self.a_ledger, f"{label} changed A"

    async def run(self, client, name: str, target: str, root: Path) -> None:
        invoke = _invoker(client)
        observed = self.observed.setdefault(name, {})
        assert (await invoke("session_brief", {"workspace_id": target}))["ok"]
        own: dict[str, str] = {}
        for tool, arguments in OWN_VARIANTS:
            response = await _target_call(invoke, target, tool, arguments)
            self.check(f"{name}:{tool} (own)", response, arguments)
            own[tool] = _code(response)
        for tool, arguments in sorted(_sweep_arguments(self.seed).items()):
            variants = [arguments]
            if {"cursor", "after_cursor", "export_session_id"} & set(arguments):
                # A's cursor or session is refused; also read the target's own.
                variants.append(
                    {
                        key: value
                        for key, value in arguments.items()
                        if key not in {"cursor", "after_cursor", "export_session_id"}
                    }
                )
            for variant in variants:
                before = _ledger_counts(root)
                response = await _target_call(invoke, target, tool, variant)
                self.check(f"{name}:{tool}", response, variant)
                if not response["ok"]:
                    assert _ledger_counts(root) == before, f"{name}:{tool} wrote"
                observed.setdefault(tool, []).append(_code(response))
        for tool, code in own.items():
            observed[tool].append(code)
        templates = [
            f"memory://workspaces/{target}/{kind}"
            for kind in ("warnings", "failures", "rules", "active-context")
        ]
        for uri in templates:
            contents = await client.read_resource(uri)
            self.check(uri, [json.loads(item.text) for item in contents], uri)

    def assert_pinned(self, name: str) -> None:
        for tool, codes in sorted(self.observed[name].items()):
            if APPS_AVAILABLE or not tool.startswith("code_"):
                assert tuple(codes) == EXPECTED[tool], (name, tool, codes)

    def gated(self, name: str) -> list[str]:
        return sorted(
            tool
            for tool, codes in self.observed[name].items()
            if all(code in GATE_CODES for code in codes)
        )


async def test_workspace_isolation_and_path_leak_sweep(tmp_path, capsys):
    a_root, b_root, c_root = (tmp_path / name for name in ("alpha", "beta", "gamma"))
    alpha, beta = await initialize_workspaces((a_root, b_root))
    a, b = alpha.workspace_id, beta.workspace_id
    c = WorkspaceRegistry([c_root], default_root=c_root).default.workspace_id
    for root, text in (
        (a_root, f"def {CANARY}_owned_function():\n    return 1  # TODO: {CANARY}\n"),
        (b_root, "def neutral_function():\n    return 1  # TODO: neutral\n"),
    ):
        (root / "src").mkdir()
        (root / "src" / f"{'owned' if root == a_root else 'neutral'}.py").write_text(
            text, encoding="utf-8"
        )

    def server(roots):
        return create_v7_server(
            "stdio",
            settings=Settings(
                project_root=str(a_root),
                workspace_roots=[str(root) for root in roots],
                dream_enabled=False,
            ),
            environ=PROFILE_ENVIRONMENT,
        )

    # Pass 1, target B.
    async with Client(server((a_root, b_root))) as client:
        invoke = _invoker(client)
        assert {tool.name for tool in await client.list_tools()} == set(V7_TOOL_LEVELS)
        seed = await _seed_workspace_a(invoke, a, b)
        await _mint_tokens(invoke, a, b, seed)
        required = {"record", "other_record", "relationship", "rule", "trigger"}
        required |= {"active_context"}
        required |= {"export_session"} | ({"code_entity"} if APPS_AVAILABLE else set())
        assert required <= set(seed.ids), required - set(seed.ids)
        roots = {"A": a_root, "B": b_root, "C": c_root}
        sweep = Sweep(roots, a_root, seed, a)
        await sweep.run(client, "B", b, b_root)
        cross = await _cross_workspace_checks(invoke, sweep, a, b, b_root, seed)

    # Pass 1, target C: a registered root holding a byte copy of A's storage.
    shutil.copytree(a_root / ".daem0nmcp", c_root / ".daem0nmcp")
    (c_root / "src").mkdir()
    (c_root / "src" / "neutral.py").write_text(
        "def neutral_function():\n    return 1  # TODO: neutral\n", encoding="utf-8"
    )
    async with Client(server((a_root, b_root, c_root))) as client:
        invoke = _invoker(client)
        await _mint_tokens(invoke, a, b, seed)
        sweep.a_ledger = _ledger_counts(a_root)
        await sweep.run(client, "C", c, c_root)
    for name in ("B", "C"):
        sweep.assert_pinned(name)

    await _jwt_pass(tmp_path, a_root, b_root, a, b, seed)

    with capsys.disabled():
        swept = sorted(sweep.observed["B"])
        print(f"\nswept ({len(swept)} tools + 4 resources, targets B and C)")
        print(f"gate-only on B (no data path reached): {sweep.gated('B')}")
        print(f"placeholder seeds (no real A object): {sorted(seed.placeholders)}")
        print(f"cross-workspace ({len(cross)}): {', '.join(cross)}")


async def _cross_workspace_checks(invoke, sweep, a, b, b_root, seed) -> list[str]:
    """B may read or move A only through a B -> A link it created itself."""

    async def refused(tool, arguments, code):
        before = _ledger_counts(b_root)
        response = await _target_call(invoke, b, tool, arguments)
        sweep.check(f"B:{tool}", response, arguments)
        assert _code(response) == code, (tool, response)
        assert _ledger_counts(b_root) == before, tool

    # Only A -> B exists, so every B-side read or move of A is refused.
    await refused(
        "memory_recall",
        {"query": CANARY, "linked_workspace_ids": [a]},
        "UNAUTHORIZED_WORKSPACE",
    )
    for tool, arguments in _cross_arguments(seed.token("consolidation"), a).items():
        if tool in {"workspace_link", "workspace_unlink"}:
            continue
        await refused(tool, arguments, "UNAUTHORIZED_WORKSPACE")
    # Linking needs a preflight bound to exactly that link.
    unbound = await invoke(
        "workspace_link",
        {
            "workspace_id": b,
            "linked_workspace_id": a,
            "preflight_token": seed.token("consolidation"),
        },
    )
    assert _code(unbound) == "TOKEN_TAMPERED", unbound
    link = {"linked_workspace_id": a}
    assert (await _protected(invoke, b, "workspace_link", link))["ok"]
    preview = await invoke(
        "workspace_consolidation_preview",
        {"workspace_id": b, "source_workspace_ids": [a]},
    )
    assert preview["ok"], preview["error"]
    token = preview["data"]["selection_token"]
    assert (await _protected(invoke, b, "workspace_unlink", link))["ok"]
    # With B's own valid preview token, only the removed link refuses the move.
    for tool, arguments in _cross_arguments(token, a).items():
        if tool in {"workspace_link", "workspace_unlink"}:
            continue
        await refused(tool, arguments, "UNAUTHORIZED_WORKSPACE")
    return list(CROSS_WORKSPACE_TOOLS)


async def _jwt_pass(tmp_path, a_root, b_root, a, b, seed) -> None:
    """Pass 2: a JWT principal granted only A is refused B everywhere."""
    policy_path = tmp_path / "protected" / "access.json"
    write_workspace_grants(policy_path, [a])
    placeholder_token = f"preflight-{CANARY}-placeholder"
    with jwt_issuer() as (issuer_url, token):
        async with process_client(
            a_root,
            "streamable-http",
            workspace_roots=(a_root, b_root),
            environment_overrides=jwt_environment(issuer_url, policy_path),
            http_headers={"Authorization": "Bearer " + token()},
        ) as session:
            await succeed(session, "session_brief", {"workspace_id": a})
            ledgers = {root: _ledger_counts(root) for root in (a_root, b_root)}

            def assert_denied(tool, denied):
                assert not denied["ok"], (tool, "succeeded for an ungranted workspace")
                assert denied["error"]["code"] == "UNAUTHORIZED_WORKSPACE", (
                    tool,
                    denied,
                )

            targets = {
                **_sweep_arguments(seed),
                **_cross_arguments(seed.token("consolidation"), a),
            }
            for tool, arguments in sorted(targets.items()):
                if _is_protected(tool):
                    arguments = {**arguments, "preflight_token": placeholder_token}
                denied = await call(session, tool, {"workspace_id": b, **arguments})
                assert_denied(tool, denied)
            for kind in ("warnings", "failures", "rules", "active-context"):
                # The server masks the reason, so the same read succeeding for
                # A is what shows B's failure is the authorization refusal.
                await session.read_resource(f"memory://workspaces/{a}/{kind}")
                uri = f"memory://workspaces/{b}/{kind}"
                with pytest.raises(
                    McpError, match=f"^Error reading resource '{re.escape(uri)}'$"
                ):
                    await session.read_resource(uri)
            # B as the source of an A-side federated read or consolidation.
            denied = await call(
                session,
                "memory_recall",
                {"workspace_id": a, "query": CANARY, "linked_workspace_ids": [b]},
            )
            assert_denied("memory_recall linked", denied)

            async def remote(tool, arguments):
                return await call(session, tool, arguments)

            for tool, arguments in _cross_arguments(
                seed.token("consolidation"), b
            ).items():
                denied = await _target_call(remote, a, tool, arguments)
                assert_denied(f"{tool} from B", denied)
    for root, counts in ledgers.items():
        assert _ledger_counts(root) == counts, f"denied calls wrote {root.name}"
