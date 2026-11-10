"""Dependency-light regressions for legacy server exports and dispatch projections."""

import ast
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from daem0nmcp.workflows import commune as commune_workflow
from daem0nmcp.workflows import consult as consult_workflow
from daem0nmcp.workflows import explore as explore_workflow

REPO_ROOT = Path(__file__).resolve().parents[1]

LEGACY_EXPORTS = {
    "agency_tools": (
        "compress_context",
        "execute_python",
        "ingest_doc",
    ),
    "briefing": (
        "get_briefing",
        "get_briefing_visual",
        "get_covenant_status",
        "get_covenant_status_visual",
        "context_check",
        "check_for_updates",
        "health",
    ),
    "code_tools": (
        "scan_todos",
        "index_project",
        "find_code",
        "analyze_impact",
        "propose_refactor",
    ),
    "context_tools": (
        "set_active_context",
        "get_active_context",
        "remove_from_active_context",
        "clear_active_context",
        "add_context_trigger",
        "list_context_triggers",
        "remove_context_trigger",
        "check_context_triggers",
    ),
    "entity_tools": ("list_entities", "backfill_entities"),
    "federation": (
        "link_projects",
        "unlink_projects",
        "list_linked_projects",
        "consolidate_linked_databases",
    ),
    "graph_tools": (
        "link_memories",
        "unlink_memories",
        "trace_chain",
        "get_graph",
        "get_graph_visual",
        "get_graph_stats",
        "rebuild_communities",
        "list_communities",
        "list_communities_visual",
        "get_community_details",
    ),
    "maintenance": (
        "rebuild_index",
        "export_data",
        "import_data",
        "prune_memories",
    ),
    "memory": (
        "remember",
        "remember_batch",
        "recall",
        "recall_visual",
        "record_outcome",
        "recall_for_file",
        "recall_by_entity",
        "recall_hierarchical",
        "search_memories",
        "find_related",
        "get_related_memories",
        "get_memory_versions",
        "get_memory_at_time",
        "compact_memories",
        "cleanup_memories",
        "archive_memory",
        "pin_memory",
    ),
    "rules": ("add_rule", "check_rules", "list_rules", "update_rule"),
    "temporal": ("trace_causal_path", "trace_evolution"),
    "verification": ("verify_facts",),
}

CONSOLIDATED_TOOLS = {
    "commune",
    "consult",
    "inscribe",
    "reflect",
    "understand",
    "govern",
    "explore",
    "maintain",
}
COGNITIVE_TOOLS = {"simulate_decision", "evolve_rule", "debate_internal"}


def _module(name: str, **attributes):
    module = types.ModuleType(name)
    for attribute, value in attributes.items():
        setattr(module, attribute, value)
    return module


def _named_function(name: str):
    def exported(*args, **kwargs):
        return None

    exported.__name__ = name
    return exported


def _export_module(name: str, exports) -> types.ModuleType:
    return _module(
        name,
        **{export: _named_function(export) for export in exports},
    )


def _lazy_export_inventory() -> dict[str, tuple[str, str]]:
    tree = ast.parse((REPO_ROOT / "daem0nmcp" / "server.py").read_text("utf-8"))
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "_LEGACY_EXPORTS"
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("server.py does not declare _LEGACY_EXPORTS")


class _Registry:
    def __init__(self, names):
        self.tools = {name: object() for name in names}
        self.removed = []
        self.auth = None

    def remove_tool(self, name):
        self.removed.append(name)
        self.tools.pop(name, None)

    def add_middleware(self, middleware):
        return None

    def run(self, **kwargs):
        return None


def _load_stubbed_server(registry: _Registry):
    context_symbols = (
        "ProjectContext",
        "get_project_context",
        "evict_stale_contexts",
        "cleanup_all_contexts",
        "_project_contexts",
        "_context_locks",
        "_normalize_path",
        "_default_project_path",
        "MAX_PROJECT_CONTEXTS",
        "CONTEXT_TTL_SECONDS",
        "_missing_project_path_error",
        "_check_covenant_communion",
        "_check_covenant_counsel",
        "_get_context_for_covenant",
        "_get_context_state_for_middleware",
        "_resolve_within_project",
        "_track_task_context",
        "_release_current_task_contexts",
        "_contexts_lock",
        "_task_contexts",
        "_task_contexts_lock",
        "_EVICTION_INTERVAL_SECONDS",
        "_maybe_schedule_eviction",
        "_get_storage_for_project",
        "workspace_registry",
        "hold_context",
    )
    context_manager = _module("daem0nmcp.context_manager")
    for name in context_symbols:
        setattr(context_manager, name, {} if name == "_project_contexts" else None)

    settings = types.SimpleNamespace(
        get_storage_path=lambda: str(REPO_ROOT / ".test_tmp" / "server-storage"),
        dream_enabled=False,
    )
    stubs = {
        "daem0nmcp.vectors": _module("daem0nmcp.vectors"),
        "daem0nmcp.context_manager": context_manager,
        "daem0nmcp.agency": _export_module("daem0nmcp.agency", ("check_capability",)),
        "daem0nmcp.config": _module("daem0nmcp.config", settings=settings),
        "daem0nmcp.covenant": _export_module(
            "daem0nmcp.covenant", ("set_context_callback",)
        ),
        "daem0nmcp.database": _module(
            "daem0nmcp.database",
            DatabaseManager=lambda path: types.SimpleNamespace(_engine=None),
        ),
        "daem0nmcp.logging_config": _export_module(
            "daem0nmcp.logging_config", ("with_request_id",)
        ),
        "daem0nmcp.mcp_instance": _module("daem0nmcp.mcp_instance", mcp=registry),
        "daem0nmcp.memory": _module(
            "daem0nmcp.memory", MemoryManager=lambda database: object()
        ),
        "daem0nmcp.models": _module(
            "daem0nmcp.models", CodeEntity=object, Memory=object, Rule=object
        ),
        "daem0nmcp.rules": _module(
            "daem0nmcp.rules", RulesEngine=lambda database: object()
        ),
        "daem0nmcp.rwlock": _module("daem0nmcp.rwlock", RWLock=object),
        "daem0nmcp.transforms.covenant": _module(
            "daem0nmcp.transforms.covenant",
            _FASTMCP_MIDDLEWARE_AVAILABLE=False,
            CovenantMiddleware=object,
        ),
        "daem0nmcp.transport_security": _export_module(
            "daem0nmcp.transport_security", ("validate_transport_security",)
        ),
        "daem0nmcp.ui.resources": _export_module(
            "daem0nmcp.ui.resources", ("register_ui_resources",)
        ),
        "daem0nmcp.tools.cognitive_tools": _export_module(
            "daem0nmcp.tools.cognitive_tools", COGNITIVE_TOOLS
        ),
        "daem0nmcp.tools.resources": _export_module(
            "daem0nmcp.tools.resources", ("_failed_resource_impl",)
        ),
        "daem0nmcp.tools.workflows": _export_module(
            "daem0nmcp.tools.workflows", CONSOLIDATED_TOOLS
        ),
        "daem0nmcp.workflows.errors": _module(
            "daem0nmcp.workflows.errors", WorkflowError=RuntimeError
        ),
        "daem0nmcp.dreaming": _module(
            "daem0nmcp.dreaming",
            CommunityRefresh=object,
            ConnectionDiscovery=object,
            DreamSession=object,
            DreamStrategy=object,
            FailedDecisionReview=object,
            IdleDreamScheduler=object,
            PendingOutcomeResolver=object,
        ),
        "daem0nmcp.dreaming.persistence": _export_module(
            "daem0nmcp.dreaming.persistence", ("persist_session_summary",)
        ),
    }
    for module_name, exports in LEGACY_EXPORTS.items():
        stubs[f"daem0nmcp.tools.{module_name}"] = _export_module(
            f"daem0nmcp.tools.{module_name}", exports
        )

    module_name = "daem0nmcp._legacy_server_dispatch_test"
    source = REPO_ROOT / "daem0nmcp" / "server.py"
    spec = importlib.util.spec_from_file_location(module_name, source)
    assert spec is not None and spec.loader is not None
    server = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, stubs):
        sys.modules[module_name] = server
        try:
            spec.loader.exec_module(server)
        finally:
            sys.modules.pop(module_name, None)
    return server


def _recording_server(*names):
    calls = []
    server = _module("daem0nmcp.server")

    for name in names:

        async def leaf(_name=name, **kwargs):
            calls.append((_name, kwargs))
            return {"leaf": _name}

        setattr(server, name, leaf)
    return server, calls


class LegacyServerCompositionTests(unittest.TestCase):
    def test_legacy_python_exports_are_lazy_and_never_touch_the_v7_registry(self):
        inventory = _lazy_export_inventory()
        expected = {
            name
            for module_exports in LEGACY_EXPORTS.values()
            for name in module_exports
        }
        self.assertEqual(67, len(expected))
        self.assertTrue(expected <= set(inventory))
        for module_name, public_names in LEGACY_EXPORTS.items():
            for public_name in public_names:
                self.assertEqual(
                    (f"daem0nmcp.tools.{module_name}", public_name),
                    inventory[public_name],
                )

        server_source = (REPO_ROOT / "daem0nmcp" / "server.py").read_text("utf-8")
        self.assertNotIn("_DEPRECATED_TOOLS", server_source)
        self.assertNotIn("remove_tool(", server_source)


class LegacyDispatcherProjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_explore_chain_invokes_causal_leaf_with_nondefault_arguments(self):
        server, calls = _recording_server("trace_chain", "trace_causal_path")
        with patch.dict(sys.modules, {"daem0nmcp.server": server}):
            result = await explore_workflow.dispatch(
                "chain",
                "workspace-a",
                start_memory_id=17,
                end_memory_id=42,
                max_depth=9,
            )

        self.assertEqual({"leaf": "trace_causal_path"}, result)
        self.assertEqual(
            [
                (
                    "trace_causal_path",
                    {
                        "start_memory_id": 17,
                        "end_memory_id": 42,
                        "max_depth": 9,
                        "project_path": "workspace-a",
                    },
                )
            ],
            calls,
        )

    async def test_consult_recall_preserves_temporal_and_mode_arguments(self):
        common = {
            "topic": "authorization history",
            "categories": ["decision"],
            "tags": ["security"],
            "file_path": "daem0nmcp/covenant.py",
            "offset": 3,
            "limit": 7,
            "since": "2026-01-01T00:00:00Z",
            "until": "2026-07-31T23:59:59Z",
            "include_linked": True,
            "condensed": True,
            "as_of_time": "2026-06-15T12:00:00Z",
        }
        expected_leaf_args = {**common, "project_path": "workspace-a"}

        server, calls = _recording_server("recall", "recall_visual")
        with patch.dict(sys.modules, {"daem0nmcp.server": server}):
            await consult_workflow.dispatch(
                "recall", "workspace-a", visual=False, **common
            )
            await consult_workflow.dispatch(
                "recall", "workspace-a", visual=True, **common
            )

        self.assertEqual(
            [
                ("recall", expected_leaf_args),
                ("recall_visual", expected_leaf_args),
            ],
            calls,
        )

    async def test_graph_and_community_modes_project_supported_leaf_arguments(self):
        names = (
            "get_graph",
            "get_graph_visual",
            "list_communities",
            "list_communities_visual",
        )
        server, calls = _recording_server(*names)
        with patch.dict(sys.modules, {"daem0nmcp.server": server}):
            await explore_workflow.dispatch(
                "graph",
                "workspace-a",
                memory_ids=[2, 5],
                topic="policy",
                format="mermaid",
                visual=False,
            )
            await explore_workflow.dispatch(
                "graph",
                "workspace-a",
                memory_ids=[3, 8],
                topic="tokens",
                visual=True,
                include_orphans=True,
            )
            await explore_workflow.dispatch(
                "communities", "workspace-a", level=2, visual=False
            )
            await explore_workflow.dispatch(
                "communities",
                "workspace-a",
                level=3,
                parent_community_id=19,
                visual=True,
            )

        self.assertEqual(
            [
                (
                    "get_graph",
                    {
                        "memory_ids": [2, 5],
                        "topic": "policy",
                        "format": "mermaid",
                        "project_path": "workspace-a",
                    },
                ),
                (
                    "get_graph_visual",
                    {
                        "memory_ids": [3, 8],
                        "topic": "tokens",
                        "include_orphans": True,
                        "project_path": "workspace-a",
                    },
                ),
                (
                    "list_communities",
                    {"level": 2, "project_path": "workspace-a"},
                ),
                (
                    "list_communities_visual",
                    {
                        "level": 3,
                        "parent_community_id": 19,
                        "project_path": "workspace-a",
                    },
                ),
            ],
            calls,
        )

    async def test_briefing_and_covenant_modes_select_matching_real_leaves(self):
        names = (
            "get_briefing",
            "get_briefing_visual",
            "get_covenant_status",
            "get_covenant_status_visual",
        )
        server, calls = _recording_server(*names)
        with patch.dict(sys.modules, {"daem0nmcp.server": server}):
            await commune_workflow.dispatch(
                "briefing",
                "workspace-a",
                focus_areas=["capabilities"],
                visual=False,
            )
            await commune_workflow.dispatch(
                "briefing",
                "workspace-a",
                focus_areas=["dispatch"],
                visual=True,
            )
            await commune_workflow.dispatch("covenant", "workspace-a", visual=False)
            await commune_workflow.dispatch("covenant", "workspace-a", visual=True)

        self.assertEqual(
            [
                (
                    "get_briefing",
                    {
                        "project_path": "workspace-a",
                        "focus_areas": ["capabilities"],
                    },
                ),
                (
                    "get_briefing_visual",
                    {
                        "project_path": "workspace-a",
                        "focus_areas": ["dispatch"],
                    },
                ),
                ("get_covenant_status", {"project_path": "workspace-a"}),
                ("get_covenant_status_visual", {"project_path": "workspace-a"}),
            ],
            calls,
        )


if __name__ == "__main__":
    unittest.main()
