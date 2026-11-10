"""Security contract for deprecated public Python tool entry points."""

from __future__ import annotations

import ast
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import daem0nmcp.covenant as covenant

EXPECTED_LEGACY_ENTRYPOINTS = {
    "remember": "inscribe.remember",
    "remember_batch": "inscribe.remember_batch",
    "recall": "consult.recall",
    "recall_visual": "consult.recall",
    "record_outcome": "reflect.outcome",
    "recall_for_file": "consult.recall_file",
    "recall_by_entity": "consult.recall_entity",
    "recall_hierarchical": "consult.recall_hierarchical",
    "search_memories": "consult.search",
    "find_related": "explore.related",
    "get_related_memories": "explore.related",
    "get_memory_versions": "explore.versions",
    "get_memory_at_time": "explore.at_time",
    "compact_memories": "maintain.compact",
    "cleanup_memories": "maintain.cleanup",
    "archive_memory": "maintain.archive",
    "pin_memory": "inscribe.pin",
    "add_rule": "govern.add_rule",
    "check_rules": "consult.check_rules",
    "list_rules": "govern.list_rules",
    "update_rule": "govern.update_rule",
    "get_briefing": "commune.briefing",
    "get_briefing_visual": "commune.briefing",
    "get_covenant_status": "commune.covenant",
    "get_covenant_status_visual": "commune.covenant",
    "context_check": "consult.preflight",
    "check_for_updates": "commune.updates",
    "health": "commune.health",
    "verify_facts": "reflect.verify",
    "scan_todos": "understand.todos",
    "index_project": "understand.index",
    "find_code": "understand.find",
    "analyze_impact": "understand.impact",
    "propose_refactor": "understand.refactor",
    "rebuild_index": "maintain.rebuild_index",
    "export_data": "maintain.export",
    "import_data": "maintain.import_data",
    "prune_memories": "maintain.prune",
    "link_memories": "inscribe.link",
    "unlink_memories": "inscribe.unlink",
    "trace_chain": "explore.related",
    "get_graph": "explore.graph",
    "get_graph_visual": "explore.graph",
    "get_graph_stats": "explore.stats",
    "rebuild_communities": "explore.rebuild_communities",
    "list_communities": "explore.communities",
    "list_communities_visual": "explore.communities",
    "get_community_details": "explore.community_detail",
    "set_active_context": "inscribe.activate",
    "get_active_context": "commune.active_context",
    "remove_from_active_context": "inscribe.deactivate",
    "clear_active_context": "inscribe.clear_active",
    "add_context_trigger": "govern.add_trigger",
    "list_context_triggers": "govern.list_triggers",
    "remove_context_trigger": "govern.remove_trigger",
    "check_context_triggers": "commune.triggers",
    "link_projects": "maintain.link_project",
    "unlink_projects": "maintain.unlink_project",
    "list_linked_projects": "maintain.list_projects",
    "consolidate_linked_databases": "maintain.consolidate",
    "compress_context": "consult.compress",
    "execute_python": "reflect.execute",
    "ingest_doc": "inscribe.ingest",
    "trace_causal_path": "explore.chain",
    "trace_evolution": "explore.evolution",
    "list_entities": "explore.entities",
    "backfill_entities": "explore.backfill_entities",
}

EXEMPT_LEGACY_ENTRYPOINTS = frozenset(
    {
        "get_briefing",
        "get_briefing_visual",
        "get_covenant_status",
        "get_covenant_status_visual",
        "health",
    }
)

SECURITY_RELEVANT_LEGACY_ENTRYPOINTS = frozenset(
    {
        "remember",
        "remember_batch",
        "pin_memory",
        "add_rule",
        "update_rule",
        "link_memories",
        "rebuild_communities",
        "set_active_context",
        "add_context_trigger",
        "link_projects",
        "ingest_doc",
        "backfill_entities",
        "archive_memory",
        "import_data",
        "unlink_memories",
        "remove_from_active_context",
        "clear_active_context",
        "remove_context_trigger",
        "unlink_projects",
        "execute_python",
        "scan_todos",
        "prune_memories",
        "cleanup_memories",
        "compact_memories",
        "consolidate_linked_databases",
    }
)

LEGACY_MODULES = {
    **dict.fromkeys(
        (
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
        "memory",
    ),
    **dict.fromkeys(("add_rule", "check_rules", "list_rules", "update_rule"), "rules"),
    **dict.fromkeys(
        (
            "get_briefing",
            "get_briefing_visual",
            "get_covenant_status",
            "get_covenant_status_visual",
            "context_check",
            "check_for_updates",
            "health",
        ),
        "briefing",
    ),
    "verify_facts": "verification",
    **dict.fromkeys(
        (
            "scan_todos",
            "index_project",
            "find_code",
            "analyze_impact",
            "propose_refactor",
        ),
        "code_tools",
    ),
    **dict.fromkeys(
        ("rebuild_index", "export_data", "import_data", "prune_memories"), "maintenance"
    ),
    **dict.fromkeys(
        (
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
        "graph_tools",
    ),
    **dict.fromkeys(
        (
            "set_active_context",
            "get_active_context",
            "remove_from_active_context",
            "clear_active_context",
            "add_context_trigger",
            "list_context_triggers",
            "remove_context_trigger",
            "check_context_triggers",
        ),
        "context_tools",
    ),
    **dict.fromkeys(
        (
            "link_projects",
            "unlink_projects",
            "list_linked_projects",
            "consolidate_linked_databases",
        ),
        "federation",
    ),
    **dict.fromkeys(
        ("compress_context", "execute_python", "ingest_doc"), "agency_tools"
    ),
    "trace_causal_path": "temporal",
    "trace_evolution": "temporal",
    "list_entities": "entity_tools",
    "backfill_entities": "entity_tools",
}


class LegacyEntrypointInventoryTests(unittest.TestCase):
    def test_inventory_equals_reviewed_mapping_and_server_public_list(self) -> None:
        self.assertEqual(67, len(EXPECTED_LEGACY_ENTRYPOINTS))
        actual = getattr(covenant, "LEGACY_ENTRYPOINTS", None)
        self.assertIsNotNone(actual)
        self.assertEqual(EXPECTED_LEGACY_ENTRYPOINTS, dict(actual))

        server_path = Path(__file__).parents[1] / "daem0nmcp" / "server.py"
        module = ast.parse(server_path.read_text(encoding="utf-8"))
        lazy_exports = None
        for node in module.body:
            if (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "_LEGACY_EXPORTS"
            ):
                lazy_exports = ast.literal_eval(node.value)
                break
        self.assertIsNotNone(lazy_exports)
        assert lazy_exports is not None
        self.assertTrue(set(EXPECTED_LEGACY_ENTRYPOINTS) <= set(lazy_exports))
        for public_name, module_name in LEGACY_MODULES.items():
            self.assertEqual(
                (f"daem0nmcp.tools.{module_name}", public_name),
                lazy_exports[public_name],
            )
        self.assertNotIn("_DEPRECATED_TOOLS", server_path.read_text("utf-8"))

    def test_every_public_leaf_explicitly_uses_dedicated_decorator(self) -> None:
        tools_root = Path(__file__).parents[1] / "daem0nmcp" / "tools"
        decorated = {}
        for module_path in tools_root.glob("*.py"):
            module = ast.parse(module_path.read_text(encoding="utf-8"))
            for node in module.body:
                if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                    continue
                if node.name not in EXPECTED_LEGACY_ENTRYPOINTS:
                    continue
                decorator_names = []
                legacy_argument = None
                for decorator in node.decorator_list:
                    expression = (
                        decorator.func if isinstance(decorator, ast.Call) else decorator
                    )
                    if isinstance(expression, ast.Name):
                        decorator_names.append(expression.id)
                    elif isinstance(expression, ast.Attribute):
                        decorator_names.append(expression.attr)
                    if (
                        isinstance(decorator, ast.Call)
                        and isinstance(decorator.func, ast.Name)
                        and decorator.func.id == "legacy_entrypoint"
                    ):
                        legacy_argument = ast.literal_eval(decorator.args[0])
                decorated[node.name] = (decorator_names, legacy_argument)

        self.assertEqual(set(EXPECTED_LEGACY_ENTRYPOINTS), set(decorated))
        for name, (decorators, legacy_argument) in decorated.items():
            with self.subTest(name=name):
                self.assertIn("with_request_id", decorators)
                self.assertIn("legacy_entrypoint", decorators)
                self.assertLess(
                    decorators.index("with_request_id"),
                    decorators.index("legacy_entrypoint"),
                )
                self.assertEqual(name, legacy_argument)

    def test_recall_temporal_view_is_part_of_canonical_digest_schema(self) -> None:
        defaults = covenant.ACTION_ARGUMENT_DEFAULTS["consult.recall"]
        self.assertIn("as_of_time", defaults)
        self.assertIsNone(defaults["as_of_time"])

        wrapper_path = (
            Path(__file__).parents[1] / "daem0nmcp" / "tools" / "workflows.py"
        )
        module = ast.parse(wrapper_path.read_text(encoding="utf-8"))
        consult = next(
            node
            for node in module.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "consult"
        )
        defaults = dict(
            zip(
                [
                    argument.arg
                    for argument in consult.args.args[-len(consult.args.defaults) :]
                ],
                consult.args.defaults,
                strict=True,
            )
        )
        self.assertIn("as_of_time", defaults)
        self.assertIsInstance(defaults["as_of_time"], ast.Constant)
        self.assertIsNone(defaults["as_of_time"].value)

    def test_every_effective_leaf_parameter_has_an_explicit_digest_mapping(
        self,
    ) -> None:
        self.assertEqual(set(EXPECTED_LEGACY_ENTRYPOINTS), set(LEGACY_MODULES))
        tools_root = Path(__file__).parents[1] / "daem0nmcp" / "tools"
        for name, module_name in LEGACY_MODULES.items():
            module = ast.parse(
                (tools_root / f"{module_name}.py").read_text(encoding="utf-8")
            )
            function = next(
                node
                for node in module.body
                if isinstance(node, ast.AsyncFunctionDef) and node.name == name
            )
            operation = EXPECTED_LEGACY_ENTRYPOINTS[name]
            canonical_fields = set(covenant.ACTION_ARGUMENT_DEFAULTS[operation])
            adapter = covenant.LEGACY_ARGUMENT_ADAPTERS[name]
            parameters = [
                argument.arg
                for argument in (*function.args.args, *function.args.kwonlyargs)
                if argument.arg != "project_path"
            ]
            with self.subTest(name=name):
                self.assertTrue(set(adapter.fixed) <= canonical_fields)
                self.assertTrue(set(adapter.renames.values()) <= canonical_fields)
                unmapped = {
                    parameter
                    for parameter in parameters
                    if parameter not in adapter.excluded
                    and adapter.renames.get(parameter, parameter)
                    not in canonical_fields
                }
                self.assertEqual(set(), unmapped)
                for parameter in adapter.excluded:
                    self.assertIn(parameter, parameters)
                    self.assertTrue(adapter.excluded[parameter].strip())

    def test_mode_specific_leaves_bind_fixed_canonical_arguments(self) -> None:
        expected = {
            "recall": {"visual": False},
            "recall_visual": {"visual": True},
            "get_briefing": {"visual": False},
            "get_briefing_visual": {"visual": True},
            "get_covenant_status": {"visual": False},
            "get_covenant_status_visual": {"visual": True},
            "get_graph": {"visual": False, "include_orphans": False},
            "get_graph_visual": {"visual": True, "format": "json"},
            "list_communities": {
                "parent_community_id": None,
                "visual": False,
            },
            "list_communities_visual": {"visual": True},
        }
        self.assertEqual(
            expected,
            {
                name: dict(covenant.LEGACY_ARGUMENT_ADAPTERS[name].fixed)
                for name in expected
            },
        )


class LegacyEntrypointBehaviorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = str(Path(self.temp.name, "workspace-a").resolve())
        self.workspace_b = str(Path(self.temp.name, "workspace-b").resolve())
        Path(self.workspace).mkdir()
        Path(self.workspace_b).mkdir()
        self.clock = lambda: 1_000
        self.scope = covenant.InvocationScope("principal", "session", self.workspace)
        self.gate = covenant.CovenantGate(
            state_store=covenant.CovenantStateStore(clock=self.clock),
            authority=covenant.CapabilityAuthority(
                secret=b"legacy-entrypoint-test-key-is-32-bytes",
                kid="test",
                clock=self.clock,
            ),
        )
        self.resolver = lambda selector: {
            None: self.workspace,
            "ws_a": self.workspace,
            "ws_b": self.workspace_b,
            self.workspace: self.workspace,
            self.workspace_b: self.workspace_b,
            self.scope.canonical_workspace: self.scope.canonical_workspace,
        }[selector]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _load_real_leaf_module(
        self, module_name: str
    ) -> tuple[types.ModuleType, list[str]]:
        class FakeMcp:
            @staticmethod
            def tool(**_kwargs):
                return lambda function: function

        reached: list[str] = []
        context_module = types.ModuleType("daem0nmcp.context_manager")
        context_module._default_project_path = self.workspace
        context_module._missing_project_path_error = lambda: {"error": "missing"}

        async def get_project_context(_project_path):
            reached.append("get_project_context")
            raise AssertionError("authorization guard reached the context sink")

        context_module.get_project_context = get_project_context
        logging_module = types.ModuleType("daem0nmcp.logging_config")
        logging_module.with_request_id = lambda function: function
        mcp_module = types.ModuleType("daem0nmcp.mcp_instance")
        mcp_module.mcp = FakeMcp()
        models_module = types.ModuleType("daem0nmcp.models")
        models_module.Memory = type("Memory", (), {})
        models_module.MemoryRecord = type("MemoryRecord", (), {})
        models_module.MemoryVersion = type("MemoryVersion", (), {})
        models_module.Rule = type("Rule", (), {})
        sqlalchemy_module = types.ModuleType("sqlalchemy")
        sqlalchemy_module.delete = lambda *_args, **_kwargs: None
        sqlalchemy_module.or_ = lambda *_args, **_kwargs: None
        sqlalchemy_module.select = lambda *_args, **_kwargs: None
        stubs = {
            "daem0nmcp.context_manager": context_module,
            "daem0nmcp.logging_config": logging_module,
            "daem0nmcp.mcp_instance": mcp_module,
            "daem0nmcp.models": models_module,
            "sqlalchemy": sqlalchemy_module,
        }
        module_path = (
            Path(__file__).parents[1] / "daem0nmcp" / "tools" / f"{module_name}.py"
        )
        qualified_name = f"daem0nmcp.tools._legacy_{module_name}_security_test"
        spec = importlib.util.spec_from_file_location(qualified_name, module_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, stubs):
            spec.loader.exec_module(module)
        return module, reached

    async def test_every_protected_inventory_entry_fails_before_unscoped_sink(
        self,
    ) -> None:
        decorator = getattr(covenant, "legacy_entrypoint", None)
        self.assertIsNotNone(decorator)
        if decorator is None:
            return
        protected = set(EXPECTED_LEGACY_ENTRYPOINTS) - set(EXEMPT_LEGACY_ENTRYPOINTS)
        reached = []
        for name in sorted(protected):

            async def sink(project_path=None, *, _name=name):
                reached.append(_name)
                return {"reached": _name}

            guarded = decorator(name)(sink)
            with self.subTest(name=name):
                result = await guarded(project_path=self.workspace)
                self.assertEqual("IDENTITY_UNAVAILABLE", result["violation"])
        self.assertEqual([], reached)

    async def test_real_add_rule_and_import_data_stop_before_context_sink(self) -> None:
        rules, rules_reached = self._load_real_leaf_module("rules")
        maintenance, maintenance_reached = self._load_real_leaf_module("maintenance")

        add_result = await rules.add_rule("unscoped-write", project_path=self.workspace)
        import_result = await maintenance.import_data(
            {"memories": [], "rules": []}, project_path=self.workspace
        )

        self.assertEqual("IDENTITY_UNAVAILABLE", add_result["violation"])
        self.assertEqual("IDENTITY_UNAVAILABLE", import_result["violation"])
        self.assertEqual([], rules_reached)
        self.assertEqual([], maintenance_reached)

    async def test_exempt_entries_are_workspace_bound_when_scope_is_installed(
        self,
    ) -> None:
        decorator = getattr(covenant, "legacy_entrypoint", None)
        self.assertIsNotNone(decorator)
        if decorator is None:
            return

        def make_sink(reached):
            async def sink(project_path=None):
                reached.append(project_path)
                return {"reached": project_path}

            return sink

        for name in sorted(EXEMPT_LEGACY_ENTRYPOINTS):
            reached = []
            guarded = decorator(name)(make_sink(reached))
            with self.subTest(name=name):
                with covenant.installed_invocation(
                    self.scope,
                    self.gate,
                    workspace_resolver=self.resolver,
                ):
                    allowed = await guarded(project_path="ws_a")
                    blocked = await guarded(project_path="ws_b")
                self.assertEqual({"reached": "ws_a"}, allowed)
                self.assertEqual("TOKEN_SCOPE_MISMATCH", blocked["violation"])
                self.assertEqual(["ws_a"], reached)

    async def test_positional_arguments_exact_token_and_token_stripping(self) -> None:
        decorator = getattr(covenant, "legacy_entrypoint", None)
        self.assertIsNotNone(decorator)
        if decorator is None:
            return
        reached = []

        @decorator("add_rule")
        async def add_rule_leaf(
            trigger,
            must_do=None,
            must_not=None,
            ask_first=None,
            warnings=None,
            priority=0,
            project_path=None,
        ):
            reached.append((trigger, priority, project_path))
            return {"stored": trigger}

        self.gate.record_briefing(self.scope)
        token = self.gate.issue_preflight(
            self.scope,
            "govern.add_rule",
            {"trigger": "auth", "priority": 0},
        )
        with covenant.installed_invocation(
            self.scope, self.gate, workspace_resolver=self.resolver
        ):
            allowed = await add_rule_leaf(
                "auth", project_path="ws_a", preflight_token=token
            )
        self.assertEqual({"stored": "auth"}, allowed)
        self.assertEqual([("auth", 0, "ws_a")], reached)

        mismatch_token = self.gate.issue_preflight(
            self.scope,
            "govern.add_rule",
            {"trigger": "auth", "priority": 0},
        )
        with covenant.installed_invocation(
            self.scope, self.gate, workspace_resolver=self.resolver
        ):
            mismatch = await add_rule_leaf(
                "auth",
                priority=5,
                project_path="ws_a",
                preflight_token=mismatch_token,
            )
        self.assertEqual("TOKEN_ARGUMENT_MISMATCH", mismatch["violation"])
        self.assertEqual(1, len(reached))

    async def test_default_sensitive_leaf_binds_omitted_defaults_into_token(
        self,
    ) -> None:
        reached = []

        @covenant.legacy_entrypoint("prune_memories")
        async def prune_leaf(
            older_than_days=90,
            categories=None,
            min_recall_count=5,
            protect_successful=True,
            dry_run=True,
            project_path=None,
        ):
            reached.append((older_than_days, dry_run, project_path))
            return {"pruned": True}

        self.gate.record_briefing(self.scope)
        token = self.gate.issue_preflight(
            self.scope, "maintain.prune", {"dry_run": False}
        )
        with covenant.installed_invocation(
            self.scope, self.gate, workspace_resolver=self.resolver
        ):
            allowed = await prune_leaf(
                dry_run=False,
                project_path="ws_a",
                preflight_token=token,
            )
        self.assertEqual({"pruned": True}, allowed)

        mismatch_token = self.gate.issue_preflight(
            self.scope, "maintain.prune", {"dry_run": False}
        )
        with covenant.installed_invocation(
            self.scope, self.gate, workspace_resolver=self.resolver
        ):
            mismatch = await prune_leaf(
                older_than_days=30,
                dry_run=False,
                project_path="ws_a",
                preflight_token=mismatch_token,
            )
        self.assertEqual("TOKEN_ARGUMENT_MISMATCH", mismatch["violation"])
        self.assertEqual([(90, False, "ws_a")], reached)

    async def test_no_selector_compress_leaf_uses_installed_scope_workspace(
        self,
    ) -> None:
        reached = []

        @covenant.legacy_entrypoint("compress_context")
        async def compress_leaf(
            context,
            rate=None,
            content_type=None,
            preserve_code=True,
        ):
            reached.append(context)
            return {"compressed": context}

        self.gate.record_briefing(self.scope)
        with covenant.installed_invocation(
            self.scope, self.gate, workspace_resolver=self.resolver
        ):
            result = await compress_leaf("security context")
        self.assertEqual({"compressed": "security context"}, result)
        self.assertEqual(["security context"], reached)


if __name__ == "__main__":
    unittest.main()
