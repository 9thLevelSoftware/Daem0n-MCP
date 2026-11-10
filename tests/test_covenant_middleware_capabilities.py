"""Middleware-level Covenant capability tests without a network transport."""

from __future__ import annotations

import importlib.util
import inspect
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import daem0nmcp.covenant as covenant
from daem0nmcp.covenant import (
    ACTION_ARGUMENT_DEFAULTS,
    CapabilityAuthority,
    CovenantGate,
    CovenantStateStore,
    InvocationScope,
    admitted_call_var,
    authorize_legacy_call,
    covenant_gate_var,
    installed_invocation,
    invocation_scope_var,
    issue_current_preflight,
    workspace_resolver_var,
)
from daem0nmcp.transforms.covenant import CovenantMiddleware, client_meta_var


class FakeContext:
    def __init__(
        self,
        name: str,
        arguments: dict | None = None,
        *,
        fastmcp_context: object | None = None,
    ) -> None:
        self.message = SimpleNamespace(name=name, arguments=arguments or {})
        self.fastmcp_context = fastmcp_context


class CovenantMiddlewareCapabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = str(Path(self.temp.name).resolve())
        self.workspace_b = str(Path(self.temp.name, "workspace-b").resolve())
        Path(self.workspace_b).mkdir()
        self.clock = lambda: 1_000
        self.store = CovenantStateStore(clock=self.clock)
        self.gate = CovenantGate(
            state_store=self.store,
            authority=CapabilityAuthority(
                secret=b"middleware-test-key-is-at-least-32-bytes",
                kid="test",
                clock=self.clock,
            ),
        )
        self.scope = InvocationScope("principal", "session", self.workspace)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _code(result: object) -> str | None:
        structured = getattr(result, "structured_content", None)
        return structured.get("violation") if isinstance(structured, dict) else None

    async def test_unknown_and_unbriefed_consolidated_actions_block_before_dispatch(
        self,
    ) -> None:
        middleware = CovenantMiddleware(
            gate=self.gate,
            scope_provider=lambda _context, _workspace: self.scope,
            workspace_resolver=lambda _selector: self.workspace,
        )
        dispatched = 0

        async def call_next(_context: FakeContext) -> dict:
            nonlocal dispatched
            dispatched += 1
            return {"ok": True}

        cases = (
            (
                "inscribe",
                {"action": "remember", "category": "decision", "content": "x"},
                "COMMUNION_REQUIRED",
            ),
            ("govern", {"action": "add_rule", "trigger": "auth"}, "COMMUNION_REQUIRED"),
            ("maintain", {"action": "import_data", "data": {}}, "COMMUNION_REQUIRED"),
            (
                "reflect",
                {"action": "execute", "code": "print(1)"},
                "COMMUNION_REQUIRED",
            ),
            ("inscribe", {"action": "future"}, "UNKNOWN_COVENANT_OPERATION"),
        )
        for workflow, arguments, expected in cases:
            result = await middleware.on_call_tool(
                FakeContext(workflow, {**arguments, "project_path": self.workspace}),
                call_next,
            )
            self.assertEqual(expected, self._code(result))
        self.assertEqual(0, dispatched)

    async def test_standalone_cognitive_tools_are_classified_before_dispatch(
        self,
    ) -> None:
        middleware = CovenantMiddleware(
            gate=self.gate,
            scope_provider=lambda _context, _workspace: self.scope,
            workspace_resolver=lambda _selector: self.workspace,
        )
        dispatched = 0

        async def call_next(_context: FakeContext) -> dict:
            nonlocal dispatched
            dispatched += 1
            return {"ok": True}

        for tool_name, arguments in (
            ("simulate_decision", {"decision_id": 7}),
            ("evolve_rule", {"rule_id": 3}),
            (
                "debate_internal",
                {
                    "topic": "auth",
                    "advocate_position": "keep",
                    "challenger_position": "change",
                },
            ),
        ):
            with self.subTest(tool_name=tool_name):
                result = await middleware.on_call_tool(
                    FakeContext(
                        tool_name,
                        {**arguments, "project_path": self.workspace},
                    ),
                    call_next,
                )
                self.assertEqual("COMMUNION_REQUIRED", self._code(result))
        self.assertEqual(0, dispatched)

    async def test_middleware_copies_filtered_arguments_and_resets_contextvars(
        self,
    ) -> None:
        middleware = CovenantMiddleware(
            gate=self.gate,
            scope_provider=lambda _context, _workspace: self.scope,
            workspace_resolver=lambda _selector: self.workspace,
        )
        original = {
            "action": "health",
            "project_path": self.workspace,
            "_client_meta": {"client": "test-client"},
        }

        async def call_next(context: FakeContext) -> dict:
            self.assertEqual(self.scope, invocation_scope_var.get())
            self.assertEqual({"client": "test-client"}, client_meta_var.get())
            self.assertNotIn("_client_meta", context.message.arguments)
            return {"ok": True}

        result = await middleware.on_call_tool(
            FakeContext("commune", original), call_next
        )
        self.assertEqual({"ok": True}, result)
        self.assertIn("_client_meta", original)
        self.assertIsNone(invocation_scope_var.get())
        self.assertIsNone(client_meta_var.get())

    async def test_stdio_full_capability_flow_and_exempt_health(self) -> None:
        middleware = CovenantMiddleware(
            gate=self.gate,
            transport_mode="stdio",
            workspace_resolver=lambda _selector: self.workspace,
        )
        health_calls = 0

        async def health_next(_context: FakeContext) -> dict:
            nonlocal health_calls
            health_calls += 1
            return {"status": "healthy"}

        health = await middleware.on_call_tool(
            FakeContext("commune", {"action": "health"}), health_next
        )
        self.assertEqual({"status": "healthy"}, health)
        self.assertEqual(1, health_calls)

        async def initialize_next(_context: FakeContext) -> dict:
            return {"initialized": True}

        await middleware.on_initialize(FakeContext("initialize"), initialize_next)

        async def success(_context: FakeContext) -> dict:
            return {"status": "ready"}

        await middleware.on_call_tool(
            FakeContext(
                "commune",
                {"action": "briefing", "project_path": self.workspace},
            ),
            success,
        )
        recall = await middleware.on_call_tool(
            FakeContext(
                "consult",
                {
                    "action": "recall",
                    "topic": "auth",
                    "project_path": self.workspace,
                },
            ),
            success,
        )
        self.assertEqual({"status": "ready"}, recall)

        target_args = {"category": "decision", "content": "bound"}

        async def preflight_next(_context: FakeContext) -> dict:
            return {
                "preflight_token": issue_current_preflight(
                    "inscribe.remember", target_args
                )
            }

        preflight = await middleware.on_call_tool(
            FakeContext(
                "consult",
                {
                    "action": "preflight",
                    "target_operation": "inscribe.remember",
                    "target_args": target_args,
                    "project_path": self.workspace,
                },
            ),
            preflight_next,
        )
        token = preflight["preflight_token"]
        protected = await middleware.on_call_tool(
            FakeContext(
                "inscribe",
                {
                    "action": "remember",
                    **target_args,
                    "project_path": self.workspace,
                    "preflight_token": token,
                },
            ),
            success,
        )
        self.assertEqual({"status": "ready"}, protected)

    async def test_remote_protected_call_fails_closed_without_proven_identity(
        self,
    ) -> None:
        middleware = CovenantMiddleware(
            gate=self.gate,
            transport_mode="remote",
            workspace_resolver=lambda _selector: self.workspace,
        )
        dispatched = False

        async def call_next(_context: FakeContext) -> dict:
            nonlocal dispatched
            dispatched = True
            return {"ok": True}

        result = await middleware.on_call_tool(
            FakeContext(
                "consult",
                {
                    "action": "recall",
                    "topic": "auth",
                    "project_path": self.workspace,
                },
            ),
            call_next,
        )
        self.assertEqual("IDENTITY_UNAVAILABLE", self._code(result))
        self.assertFalse(dispatched)

    async def test_remote_identity_uses_authenticated_subject_and_mcp_session(
        self,
    ) -> None:
        fastmcp_context = SimpleNamespace(
            request_context=object(), session_id="remote-session"
        )
        access_token = SimpleNamespace(
            claims={"sub": "alice"}, client_id="client-fallback"
        )
        middleware = CovenantMiddleware(
            gate=self.gate,
            transport_mode="remote",
            access_token_provider=lambda: access_token,
            workspace_resolver=lambda _selector: self.workspace,
        )

        async def success(_context: FakeContext) -> dict:
            return {"status": "ready"}

        await middleware.on_call_tool(
            FakeContext(
                "commune",
                {"action": "briefing", "project_path": self.workspace},
                fastmcp_context=fastmcp_context,
            ),
            success,
        )
        scope = InvocationScope(
            "oauth-sub:alice", "mcp-session:remote-session", self.workspace
        )
        self.assertTrue(self.store.is_briefed(scope))
        recall = await middleware.on_call_tool(
            FakeContext(
                "consult",
                {
                    "action": "recall",
                    "topic": "auth",
                    "project_path": self.workspace,
                },
                fastmcp_context=fastmcp_context,
            ),
            success,
        )
        self.assertEqual({"status": "ready"}, recall)

    async def test_remote_identity_falls_back_to_namespaced_client_id(self) -> None:
        context = FakeContext(
            "commune",
            {"action": "briefing", "project_path": self.workspace},
            fastmcp_context=SimpleNamespace(
                request_context=object(), session_id="remote-session"
            ),
        )
        middleware = CovenantMiddleware(
            gate=self.gate,
            transport_mode="remote",
            access_token_provider=lambda: SimpleNamespace(
                claims={}, client_id="service-client"
            ),
            workspace_resolver=lambda _selector: self.workspace,
        )

        async def success(_context: FakeContext) -> dict:
            return {"status": "ready"}

        await middleware.on_call_tool(context, success)
        self.assertTrue(
            self.store.is_briefed(
                InvocationScope(
                    "oauth-client:service-client",
                    "mcp-session:remote-session",
                    self.workspace,
                )
            )
        )

    async def test_remote_identity_rejects_malformed_or_unestablished_context(
        self,
    ) -> None:
        cases = (
            (
                SimpleNamespace(claims={"sub": 7}, client_id=None),
                SimpleNamespace(request_context=object(), session_id="session"),
            ),
            (
                SimpleNamespace(claims={}, client_id=7),
                SimpleNamespace(request_context=object(), session_id="session"),
            ),
            (
                SimpleNamespace(claims={"sub": "alice"}, client_id=None),
                SimpleNamespace(request_context=None, session_id="session"),
            ),
            (
                SimpleNamespace(claims={"sub": "alice"}, client_id=None),
                SimpleNamespace(request_context=object(), session_id=""),
            ),
        )
        for access_token, fastmcp_context in cases:
            with self.subTest(access_token=access_token):
                middleware = CovenantMiddleware(
                    gate=self.gate,
                    transport_mode="remote",
                    access_token_provider=lambda token=access_token: token,
                    workspace_resolver=lambda _selector: self.workspace,
                )
                result = await middleware.on_call_tool(
                    FakeContext(
                        "consult",
                        {
                            "action": "recall",
                            "topic": "auth",
                            "project_path": self.workspace,
                        },
                        fastmcp_context=fastmcp_context,
                    ),
                    lambda _context: None,
                )
                self.assertEqual("IDENTITY_UNAVAILABLE", self._code(result))

    async def test_remote_identity_fails_closed_on_session_access_error(self) -> None:
        class BrokenFastMcpContext:
            request_context = object()

            @property
            def session_id(self):
                raise KeyError("session state unavailable")

        middleware = CovenantMiddleware(
            gate=self.gate,
            transport_mode="remote",
            access_token_provider=lambda: SimpleNamespace(
                claims={"sub": "alice"}, client_id=None
            ),
            workspace_resolver=lambda _selector: self.workspace,
        )
        result = await middleware.on_call_tool(
            FakeContext(
                "consult",
                {"action": "recall", "topic": "auth"},
                fastmcp_context=BrokenFastMcpContext(),
            ),
            lambda _context: None,
        )
        self.assertEqual("IDENTITY_UNAVAILABLE", self._code(result))

    async def test_fingerprint_error_resets_every_contextvar(self) -> None:
        middleware = CovenantMiddleware(
            gate=self.gate,
            scope_provider=lambda _context, _workspace: self.scope,
            workspace_resolver=lambda _selector: self.workspace,
        )
        with (
            patch.object(
                self.gate, "fingerprint", side_effect=RuntimeError("fingerprint failed")
            ),
            self.assertRaisesRegex(RuntimeError, "fingerprint failed"),
        ):
            await middleware.on_call_tool(
                FakeContext(
                    "commune",
                    {
                        "action": "health",
                        "project_path": self.workspace,
                        "_client_meta": {"client": "test"},
                    },
                ),
                lambda _context: None,
            )
        self.assertIsNone(client_meta_var.get())
        self.assertIsNone(invocation_scope_var.get())
        self.assertIsNone(covenant_gate_var.get())
        self.assertIsNone(admitted_call_var.get())

    async def test_malformed_fixed_operations_return_stable_argument_violation(
        self,
    ) -> None:
        middleware = CovenantMiddleware(
            gate=self.gate,
            scope_provider=lambda _context, _workspace: self.scope,
            workspace_resolver=lambda _selector: self.workspace,
        )
        self.gate.record_briefing(self.scope)
        dispatched = 0

        async def call_next(_context: FakeContext) -> dict:
            nonlocal dispatched
            dispatched += 1
            return {"unexpected": "dispatch"}

        cases = (
            ("commune", {"action": "health"}),
            ("consult", {"action": "recall", "topic": "auth"}),
        )
        for tool_name, arguments in cases:
            with self.subTest(tool_name=tool_name):
                try:
                    result = await middleware.on_call_tool(
                        FakeContext(
                            tool_name,
                            {
                                **arguments,
                                "unknown_argument": "rejected",
                                "_client_meta": {"client": "test"},
                            },
                        ),
                        call_next,
                    )
                except Exception as exc:
                    self.fail(f"malformed operation escaped as {type(exc).__name__}")
                self.assertEqual("TOKEN_ARGUMENT_MISMATCH", self._code(result))
                self.assertIsNone(client_meta_var.get())
                self.assertIsNone(invocation_scope_var.get())
                self.assertIsNone(covenant_gate_var.get())
                self.assertIsNone(admitted_call_var.get())
                self.assertIsNone(workspace_resolver_var.get())
        self.assertEqual(0, dispatched)

    def _load_workflow_wrappers(
        self,
        dispatch,
        *,
        default_project_path: str | None = None,
    ) -> types.ModuleType:
        class FakeMcp:
            @staticmethod
            def tool(**_kwargs):
                return lambda function: function

        context_module = types.ModuleType("daem0nmcp.context_manager")
        context_module._default_project_path = (
            self.workspace if default_project_path is None else default_project_path
        )
        context_module._missing_project_path_error = lambda: {"error": "missing"}

        class FakeRegistry:
            @staticmethod
            def resolve(selector):
                roots = {
                    "ws_a": self.workspace,
                    "ws_b": self.workspace_b,
                    self.workspace: self.workspace,
                    self.workspace_b: self.workspace_b,
                }
                root = roots.get(selector)
                if root is None:
                    raise ValueError("unknown workspace")
                return SimpleNamespace(root=Path(root))

        context_module.workspace_registry = FakeRegistry()
        logging_module = types.ModuleType("daem0nmcp.logging_config")
        logging_module.with_request_id = lambda function: function
        mcp_module = types.ModuleType("daem0nmcp.mcp_instance")
        mcp_module.mcp = FakeMcp()
        errors_module = types.ModuleType("daem0nmcp.workflows.errors")

        class WorkflowError(Exception):
            recovery_hint = ""

        errors_module.WorkflowError = WorkflowError
        workflows_package = types.ModuleType("daem0nmcp.workflows")
        workflows_package.__path__ = []
        workflows_package.errors = errors_module
        workflow_modules = {}
        for workflow in (
            "commune",
            "consult",
            "inscribe",
            "reflect",
            "understand",
            "govern",
            "explore",
            "maintain",
        ):
            workflow_module = types.ModuleType(f"daem0nmcp.workflows.{workflow}")
            workflow_module.dispatch = dispatch
            workflow_modules[workflow] = workflow_module
            setattr(workflows_package, workflow, workflow_module)
        stubs = {
            "daem0nmcp.context_manager": context_module,
            "daem0nmcp.logging_config": logging_module,
            "daem0nmcp.mcp_instance": mcp_module,
            "daem0nmcp.workflows": workflows_package,
            "daem0nmcp.workflows.errors": errors_module,
            **{
                f"daem0nmcp.workflows.{workflow}": module
                for workflow, module in workflow_modules.items()
            },
        }
        module_path = Path(__file__).parents[1] / "daem0nmcp" / "tools" / "workflows.py"
        spec = importlib.util.spec_from_file_location(
            "daem0nmcp.tools._covenant_wrapper_test", module_path
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, stubs):
            spec.loader.exec_module(module)
        module._test_stubs = stubs
        return module

    def _load_cognitive_tools(
        self,
        *,
        default_project_path: str | None = None,
    ) -> tuple[types.ModuleType, list[str | None], list[tuple]]:
        class FakeMcp:
            @staticmethod
            def tool(**_kwargs):
                return lambda function: function

        context_calls: list[str | None] = []
        cognitive_calls: list[tuple] = []
        context_module = types.ModuleType("daem0nmcp.context_manager")
        context_module._default_project_path = (
            self.workspace if default_project_path is None else default_project_path
        )
        context_module._missing_project_path_error = lambda: {"error": "missing"}

        class FakeRegistry:
            @staticmethod
            def resolve(selector):
                roots = {
                    "ws_a": self.workspace,
                    "ws_b": self.workspace_b,
                    self.workspace: self.workspace,
                    self.workspace_b: self.workspace_b,
                }
                root = roots.get(selector)
                if root is None:
                    raise ValueError("unknown workspace")
                return SimpleNamespace(root=Path(root))

        context_module.workspace_registry = FakeRegistry()

        async def get_project_context(selector):
            context_calls.append(selector)
            return SimpleNamespace(project_path=selector)

        context_module.get_project_context = get_project_context
        logging_module = types.ModuleType("daem0nmcp.logging_config")
        logging_module.with_request_id = lambda function: function
        mcp_module = types.ModuleType("daem0nmcp.mcp_instance")
        mcp_module.mcp = FakeMcp()

        cognitive_package = types.ModuleType("daem0nmcp.cognitive")
        cognitive_package.__path__ = []
        cognitive_modules = {}

        class FakeResult:
            def __init__(self, payload):
                self.payload = payload

            def to_dict(self):
                return self.payload

        async def run_simulation(decision_id, ctx):
            cognitive_calls.append(("simulate", decision_id, ctx.project_path))
            return FakeResult({"simulated": decision_id})

        async def run_evolution(rule_id, ctx):
            cognitive_calls.append(("evolve", rule_id, ctx.project_path))
            return [FakeResult({"evolved": rule_id})]

        async def run_debate(topic, advocate, challenger, ctx):
            cognitive_calls.append(
                ("debate", topic, advocate, challenger, ctx.project_path)
            )
            return FakeResult({"consensus": topic})

        for name, function_name, function in (
            ("simulate", "run_simulation", run_simulation),
            ("evolve", "run_evolution", run_evolution),
            ("debate", "run_debate", run_debate),
        ):
            cognitive_module = types.ModuleType(f"daem0nmcp.cognitive.{name}")
            setattr(cognitive_module, function_name, function)
            cognitive_modules[name] = cognitive_module
            setattr(cognitive_package, name, cognitive_module)

        stubs = {
            "daem0nmcp.context_manager": context_module,
            "daem0nmcp.logging_config": logging_module,
            "daem0nmcp.mcp_instance": mcp_module,
            "daem0nmcp.cognitive": cognitive_package,
            **{
                f"daem0nmcp.cognitive.{name}": module
                for name, module in cognitive_modules.items()
            },
        }
        module_path = (
            Path(__file__).parents[1] / "daem0nmcp" / "tools" / "cognitive_tools.py"
        )
        spec = importlib.util.spec_from_file_location(
            "daem0nmcp.tools._covenant_cognitive_test", module_path
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, stubs):
            spec.loader.exec_module(module)
        module._test_stubs = stubs
        return module, context_calls, cognitive_calls

    def _load_memory_leaf_tools(
        self,
    ) -> tuple[types.ModuleType, list[tuple]]:
        class FakeMcp:
            @staticmethod
            def tool(**_kwargs):
                return lambda function: function

        manager_calls: list[tuple] = []

        class FakeMemoryManager:
            async def remember_batch(self, *, memories, project_path):
                manager_calls.append(("remember_batch", memories, project_path))
                return {
                    "created_count": len(memories),
                    "error_count": 0,
                    "ids": [1],
                    "errors": [],
                }

            async def recall(self, **kwargs):
                manager_calls.append(("recall", kwargs))
                return {"results": [{"id": 1, "content": "recalled"}]}

        context_module = types.ModuleType("daem0nmcp.context_manager")
        context_module._default_project_path = self.workspace
        context_module._missing_project_path_error = lambda: {"error": "missing"}
        context_module._check_covenant_counsel = lambda tool_name, project_path: (
            authorize_legacy_call(tool_name, {"project_path": project_path})
        )
        context_module._check_covenant_communion = (
            lambda project_path, tool_name="recall": authorize_legacy_call(
                tool_name, {"project_path": project_path}
            )
        )

        async def get_project_context(project_path):
            return SimpleNamespace(
                project_path=project_path,
                memory_manager=FakeMemoryManager(),
            )

        context_module.get_project_context = get_project_context
        logging_module = types.ModuleType("daem0nmcp.logging_config")
        logging_module.with_request_id = lambda function: function
        mcp_module = types.ModuleType("daem0nmcp.mcp_instance")
        mcp_module.mcp = FakeMcp()
        models_module = types.ModuleType("daem0nmcp.models")
        models_module.Memory = type("Memory", (), {})
        models_module.MemoryVersion = type("MemoryVersion", (), {})
        sqlalchemy_module = types.ModuleType("sqlalchemy")
        sqlalchemy_module.func = SimpleNamespace(max=lambda *_args: None)
        sqlalchemy_module.select = lambda *_args, **_kwargs: None
        stubs = {
            "daem0nmcp.context_manager": context_module,
            "daem0nmcp.logging_config": logging_module,
            "daem0nmcp.mcp_instance": mcp_module,
            "daem0nmcp.models": models_module,
            "sqlalchemy": sqlalchemy_module,
        }
        module_path = Path(__file__).parents[1] / "daem0nmcp" / "tools" / "memory.py"
        spec = importlib.util.spec_from_file_location(
            "daem0nmcp.tools._covenant_memory_leaf_test", module_path
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, stubs):
            spec.loader.exec_module(module)
        return module, manager_calls

    async def test_actual_batch_and_recall_leaf_dispatch_preserve_admission(
        self,
    ) -> None:
        from daem0nmcp.tools._deprecation import WorkflowCall
        from daem0nmcp.workflows import consult as consult_workflow
        from daem0nmcp.workflows import inscribe as inscribe_workflow

        leaf_tools, manager_calls = self._load_memory_leaf_tools()
        server_module = types.ModuleType("daem0nmcp.server")
        server_module.remember_batch = leaf_tools.remember_batch
        server_module.recall = leaf_tools.recall
        server_module.recall_visual = leaf_tools.recall
        middleware = CovenantMiddleware(
            gate=self.gate,
            scope_provider=lambda _context, _workspace: self.scope,
            workspace_resolver=lambda selector: (
                self.workspace if selector == self.workspace else self.workspace_b
            ),
        )
        self.gate.record_briefing(self.scope)
        memories = [{"category": "decision", "content": "batch"}]
        token = self.gate.issue_preflight(
            self.scope,
            "inscribe.remember_batch",
            {"memories": memories},
        )

        async def dispatch_batch(context: FakeContext) -> dict:
            with WorkflowCall():
                return await inscribe_workflow.dispatch(**context.message.arguments)

        async def dispatch_recall(context: FakeContext) -> dict:
            with WorkflowCall():
                return await consult_workflow.dispatch(**context.message.arguments)

        with patch.dict(sys.modules, {"daem0nmcp.server": server_module}):
            batch = await middleware.on_call_tool(
                FakeContext(
                    "inscribe",
                    {
                        "action": "remember_batch",
                        "memories": memories,
                        "project_path": self.workspace,
                        "preflight_token": token,
                    },
                ),
                dispatch_batch,
            )
            recall = await middleware.on_call_tool(
                FakeContext(
                    "consult",
                    {
                        "action": "recall",
                        "topic": "auth",
                        "project_path": self.workspace,
                    },
                ),
                dispatch_recall,
            )

        self.assertNotIn("violation", batch, batch)
        self.assertEqual(1, batch["created_count"])
        self.assertEqual("Stored 1 memories", batch["message"])
        self.assertEqual({"results": [{"id": 1, "content": "recalled"}]}, recall)
        self.assertEqual("remember_batch", manager_calls[0][0])
        self.assertEqual("recall", manager_calls[1][0])

    def test_argument_schemas_cover_registered_tool_signatures_and_defaults(
        self,
    ) -> None:
        async def dispatch(**_kwargs):
            return {}

        wrappers = self._load_workflow_wrappers(dispatch)
        for workflow in (
            "commune",
            "consult",
            "inscribe",
            "reflect",
            "understand",
            "govern",
            "explore",
            "maintain",
        ):
            signature = inspect.signature(getattr(wrappers, workflow))
            controls = {"action", "project_path"}
            if workflow in {
                "inscribe",
                "reflect",
                "understand",
                "govern",
                "explore",
                "maintain",
            }:
                controls.add("preflight_token")
            schema_fields = set().union(
                *(
                    set(defaults)
                    for operation, defaults in ACTION_ARGUMENT_DEFAULTS.items()
                    if operation.startswith(f"{workflow}.")
                )
            )
            expected_fields = schema_fields | controls
            if workflow == "consult":
                expected_fields |= {
                    "description",
                    "target_operation",
                    "target_args",
                }
            self.assertEqual(
                expected_fields,
                set(signature.parameters),
                workflow,
            )
            for operation, defaults in ACTION_ARGUMENT_DEFAULTS.items():
                if not operation.startswith(f"{workflow}."):
                    continue
                for name, default in defaults.items():
                    parameter = signature.parameters[name]
                    if parameter.default is not inspect.Parameter.empty:
                        self.assertEqual(default, parameter.default, operation)

        cognitive_tools, _, _ = self._load_cognitive_tools()
        for operation in (
            "simulate_decision",
            "evolve_rule",
            "debate_internal",
        ):
            signature = inspect.signature(getattr(cognitive_tools, operation))
            expected_fields = set(ACTION_ARGUMENT_DEFAULTS[operation]) | {
                "project_path"
            }
            if operation == "debate_internal":
                expected_fields.add("preflight_token")
            self.assertEqual(expected_fields, set(signature.parameters), operation)
            required_parameters = frozenset(
                name
                for name, parameter in signature.parameters.items()
                if parameter.default is inspect.Parameter.empty
            )
            self.assertEqual(
                getattr(covenant, "ACTION_REQUIRED_ARGUMENTS", {}).get(operation),
                required_parameters,
                operation,
            )
            for name, default in ACTION_ARGUMENT_DEFAULTS[operation].items():
                parameter = signature.parameters[name]
                if parameter.default is not inspect.Parameter.empty:
                    self.assertEqual(default, parameter.default, operation)

    async def test_direct_cognitive_tools_gate_identity_and_workspace_before_context(
        self,
    ) -> None:
        tools, context_calls, cognitive_calls = self._load_cognitive_tools()
        with patch.dict(sys.modules, tools._test_stubs):
            blocked = await tools.simulate_decision(7, self.workspace)
        self.assertEqual("IDENTITY_UNAVAILABLE", blocked["violation"])
        self.assertEqual([], context_calls)

        self.gate.record_briefing(self.scope)
        registry = tools._test_stubs["daem0nmcp.context_manager"].workspace_registry
        with (
            patch.dict(sys.modules, tools._test_stubs),
            installed_invocation(
                self.scope, self.gate, workspace_resolver=registry.resolve
            ),
        ):
            wrong_workspace = await tools.simulate_decision(7, self.workspace_b)
            allowed = await tools.simulate_decision(7, "ws_a")
        self.assertEqual("TOKEN_SCOPE_MISMATCH", wrong_workspace["violation"])
        self.assertEqual({"simulated": 7}, allowed)
        self.assertEqual(["ws_a"], context_calls)
        self.assertEqual([("simulate", 7, "ws_a")], cognitive_calls)

    async def test_direct_debate_cannot_write_without_exact_capability(self) -> None:
        tools, context_calls, cognitive_calls = self._load_cognitive_tools()
        self.gate.record_briefing(self.scope)
        registry = tools._test_stubs["daem0nmcp.context_manager"].workspace_registry
        arguments = {
            "topic": "auth",
            "advocate_position": "keep",
            "challenger_position": "change",
        }
        with (
            patch.dict(sys.modules, tools._test_stubs),
            installed_invocation(
                self.scope, self.gate, workspace_resolver=registry.resolve
            ),
        ):
            blocked = await tools.debate_internal(
                project_path=self.workspace, **arguments
            )
        self.assertEqual("COUNSEL_REQUIRED", blocked["violation"])
        self.assertEqual([], context_calls)
        self.assertEqual([], cognitive_calls)

        token = self.gate.issue_preflight(self.scope, "debate_internal", arguments)
        with (
            patch.dict(sys.modules, tools._test_stubs),
            installed_invocation(
                self.scope, self.gate, workspace_resolver=registry.resolve
            ),
        ):
            allowed = await tools.debate_internal(
                project_path=self.workspace,
                preflight_token=token,
                **arguments,
            )
        self.assertEqual({"consensus": "auth"}, allowed)
        self.assertEqual([self.workspace], context_calls)
        self.assertEqual(
            [("debate", "auth", "keep", "change", self.workspace)],
            cognitive_calls,
        )

    async def test_direct_consolidated_wrapper_uses_same_gate_before_dispatch(
        self,
    ) -> None:
        dispatch_calls: list[dict] = []

        async def dispatch(**kwargs):
            dispatch_calls.append(kwargs)
            return {"stored": True}

        wrappers = self._load_workflow_wrappers(dispatch)
        target_args = {"category": "decision", "content": "bound"}
        with patch.dict(sys.modules, wrappers._test_stubs):
            blocked = await wrappers.inscribe(
                action="remember", project_path=self.workspace, **target_args
            )
        self.assertEqual("IDENTITY_UNAVAILABLE", blocked["violation"])
        self.assertEqual([], dispatch_calls)

        self.gate.record_briefing(self.scope)
        token = self.gate.issue_preflight(self.scope, "inscribe.remember", target_args)
        with (
            patch.dict(sys.modules, wrappers._test_stubs),
            installed_invocation(self.scope, self.gate),
        ):
            allowed = await wrappers.inscribe(
                action="remember",
                project_path=self.workspace,
                preflight_token=token,
                **target_args,
            )
        self.assertEqual({"stored": True}, allowed)
        self.assertEqual(1, len(dispatch_calls))
        self.assertNotIn("preflight_token", dispatch_calls[0])

    async def test_direct_wrapper_installs_exact_admission_for_real_leaf_dispatch(
        self,
    ) -> None:
        from daem0nmcp.workflows import inscribe as inscribe_workflow

        leaf_tools, manager_calls = self._load_memory_leaf_tools()
        wrappers = self._load_workflow_wrappers(inscribe_workflow.dispatch)
        registry = wrappers._test_stubs["daem0nmcp.context_manager"].workspace_registry
        server_module = types.ModuleType("daem0nmcp.server")
        server_module.remember_batch = leaf_tools.remember_batch
        memories = [{"category": "decision", "content": "direct batch"}]
        self.gate.record_briefing(self.scope)

        token = self.gate.issue_preflight(
            self.scope, "inscribe.remember_batch", {"memories": memories}
        )
        call_modules = {
            **wrappers._test_stubs,
            "daem0nmcp.server": server_module,
        }
        with (
            patch.dict(sys.modules, call_modules),
            installed_invocation(
                self.scope,
                self.gate,
                workspace_resolver=registry.resolve,
            ),
        ):
            allowed = await wrappers.inscribe(
                action="remember_batch",
                memories=memories,
                project_path=self.workspace,
                preflight_token=token,
            )
            self.assertIsNone(admitted_call_var.get())
            replay = await wrappers.inscribe(
                action="remember_batch",
                memories=memories,
                project_path=self.workspace,
                preflight_token=token,
            )
            self.assertIsNone(admitted_call_var.get())

        self.assertNotIn("violation", allowed, allowed)
        self.assertEqual(1, allowed["created_count"])
        self.assertEqual("TOKEN_REPLAYED", replay["violation"])
        self.assertEqual([("remember_batch", memories, self.workspace)], manager_calls)

        mismatch_token = self.gate.issue_preflight(
            self.scope, "inscribe.remember_batch", {"memories": memories}
        )
        different_memories = [{"category": "decision", "content": "other"}]
        workspace_token = self.gate.issue_preflight(
            self.scope, "inscribe.remember_batch", {"memories": memories}
        )
        with (
            patch.dict(sys.modules, call_modules),
            installed_invocation(
                self.scope,
                self.gate,
                workspace_resolver=registry.resolve,
            ),
        ):
            mismatch = await wrappers.inscribe(
                action="remember_batch",
                memories=different_memories,
                project_path=self.workspace,
                preflight_token=mismatch_token,
            )
            wrong_workspace = await wrappers.inscribe(
                action="remember_batch",
                memories=memories,
                project_path=self.workspace_b,
                preflight_token=workspace_token,
            )
            self.assertIsNone(admitted_call_var.get())
        self.assertEqual("TOKEN_ARGUMENT_MISMATCH", mismatch["violation"])
        self.assertEqual("TOKEN_SCOPE_MISMATCH", wrong_workspace["violation"])
        self.assertEqual(1, len(manager_calls))

        @covenant.legacy_entrypoint("remember_batch")
        async def failing_leaf(memories, project_path=None):
            raise RuntimeError("leaf failed")

        server_module.remember_batch = failing_leaf
        failure_token = self.gate.issue_preflight(
            self.scope, "inscribe.remember_batch", {"memories": memories}
        )
        with (
            patch.dict(sys.modules, call_modules),
            installed_invocation(
                self.scope,
                self.gate,
                workspace_resolver=registry.resolve,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "leaf failed"):
                await wrappers.inscribe(
                    action="remember_batch",
                    memories=memories,
                    project_path=self.workspace,
                    preflight_token=failure_token,
                )
            self.assertIsNone(admitted_call_var.get())

    async def test_direct_wrappers_validate_all_levels_before_dispatch(self) -> None:
        dispatch_calls = []

        async def dispatch(**kwargs):
            dispatch_calls.append(kwargs)
            return {"unexpected": True}

        wrappers = self._load_workflow_wrappers(dispatch)
        registry = wrappers._test_stubs["daem0nmcp.context_manager"].workspace_registry
        self.gate.record_briefing(self.scope)
        with (
            patch.dict(sys.modules, wrappers._test_stubs),
            installed_invocation(
                self.scope,
                self.gate,
                workspace_resolver=registry.resolve,
            ),
        ):
            results = (
                await wrappers.commune(
                    action="briefing",
                    focus_areas=[{"invalid"}],
                    project_path=self.workspace,
                ),
                await wrappers.consult(
                    action="recall",
                    topic={"invalid"},
                    project_path=self.workspace,
                ),
                await wrappers.inscribe(
                    action="remember",
                    category={"invalid"},
                    content="blocked",
                    project_path=self.workspace,
                ),
                await wrappers.maintain(
                    action="import_data",
                    data={"invalid": {1}},
                    project_path=self.workspace,
                ),
            )
            unknown = await wrappers.consult(
                action="not_registered", project_path=self.workspace
            )
            self.assertIsNone(admitted_call_var.get())

        self.assertEqual(
            ["TOKEN_ARGUMENT_MISMATCH"] * 4,
            [result["violation"] for result in results],
        )
        self.assertEqual("UNKNOWN_COVENANT_OPERATION", unknown["violation"])
        self.assertEqual([], dispatch_calls)

    async def test_direct_legacy_leaves_validate_all_levels_before_handler(
        self,
    ) -> None:
        reached = []

        @covenant.legacy_entrypoint("get_briefing")
        async def briefing_leaf(focus_areas=None, project_path=None):
            reached.append("exempt")

        @covenant.legacy_entrypoint("recall")
        async def recall_leaf(topic, project_path=None):
            reached.append("communion")

        @covenant.legacy_entrypoint("add_rule")
        async def add_rule_leaf(trigger, project_path=None):
            reached.append("counsel")

        @covenant.legacy_entrypoint("import_data")
        async def import_leaf(data, merge=True, project_path=None):
            reached.append("destructive")

        self.gate.record_briefing(self.scope)

        def resolver(selector):
            return {
                self.workspace: self.workspace,
                self.workspace_b: self.workspace_b,
            }[selector]

        with installed_invocation(self.scope, self.gate, workspace_resolver=resolver):
            results = (
                await briefing_leaf([{"invalid"}], project_path=self.workspace),
                await recall_leaf({"invalid"}, project_path=self.workspace),
                await add_rule_leaf({"invalid"}, project_path=self.workspace),
                await import_leaf({"invalid": {1}}, project_path=self.workspace),
            )

        self.assertEqual(
            ["TOKEN_ARGUMENT_MISMATCH"] * 4,
            [result["violation"] for result in results],
        )
        self.assertEqual([], reached)

    async def test_nested_legacy_admission_is_operation_and_workspace_bound(
        self,
    ) -> None:
        middleware = CovenantMiddleware(
            gate=self.gate,
            scope_provider=lambda _context, _workspace: self.scope,
            workspace_resolver=lambda selector: {
                self.workspace: self.workspace,
                self.workspace_b: self.workspace_b,
            }[selector],
        )
        self.gate.record_briefing(self.scope)
        memories = [{"category": "decision", "content": "batch"}]
        token = self.gate.issue_preflight(
            self.scope,
            "inscribe.remember_batch",
            {"memories": memories},
        )

        async def nested_rechecks(_context: FakeContext) -> dict:
            return {
                "same": authorize_legacy_call(
                    "remember_batch", {"project_path": self.workspace}
                ),
                "other_workspace": authorize_legacy_call(
                    "remember_batch", {"project_path": self.workspace_b}
                ),
                "other_operation": authorize_legacy_call(
                    "remember", {"project_path": self.workspace}
                ),
            }

        result = await middleware.on_call_tool(
            FakeContext(
                "inscribe",
                {
                    "action": "remember_batch",
                    "memories": memories,
                    "project_path": self.workspace,
                    "preflight_token": token,
                },
            ),
            nested_rechecks,
        )
        self.assertEqual("COUNSEL_REQUIRED", result["same"]["violation"])
        self.assertEqual(
            "TOKEN_SCOPE_MISMATCH",
            result["other_workspace"]["violation"],
        )
        self.assertEqual("COUNSEL_REQUIRED", result["other_operation"]["violation"])

    async def test_scope_for_workspace_a_cannot_dispatch_workspace_b_in_any_wrapper(
        self,
    ) -> None:
        dispatch_calls: list[dict] = []

        async def dispatch(**kwargs):
            dispatch_calls.append(kwargs)
            return {"dispatched": True}

        wrappers = self._load_workflow_wrappers(dispatch)
        self.gate.record_briefing(self.scope)
        cases = (
            ("commune", "active_context", {}, None),
            ("consult", "recall", {"topic": "auth"}, None),
            (
                "inscribe",
                "remember",
                {"category": "decision", "content": "x"},
                "inscribe.remember",
            ),
            ("reflect", "execute", {"code": "print(1)"}, "reflect.execute"),
            (
                "understand",
                "todos",
                {"auto_remember": True},
                "understand.todos",
            ),
            ("govern", "add_rule", {"trigger": "auth"}, "govern.add_rule"),
            (
                "explore",
                "rebuild_communities",
                {},
                "explore.rebuild_communities",
            ),
            (
                "maintain",
                "import_data",
                {"data": {}},
                "maintain.import_data",
            ),
        )
        for workflow, action, action_args, protected_operation in cases:
            token = None
            if protected_operation:
                token = self.gate.issue_preflight(
                    self.scope, protected_operation, action_args
                )
            with self.subTest(workflow=workflow):
                with (
                    patch.dict(sys.modules, wrappers._test_stubs),
                    installed_invocation(
                        self.scope,
                        self.gate,
                        workspace_resolver=wrappers._test_stubs[
                            "daem0nmcp.context_manager"
                        ].workspace_registry.resolve,
                    ),
                ):
                    result = (
                        await getattr(wrappers, workflow)(
                            action=action,
                            project_path=self.workspace_b,
                            preflight_token=token,
                            **action_args,
                        )
                        if workflow not in {"commune", "consult"}
                        else await getattr(wrappers, workflow)(
                            action=action,
                            project_path=self.workspace_b,
                            **action_args,
                        )
                    )
                self.assertEqual("TOKEN_SCOPE_MISMATCH", result["violation"])
        self.assertEqual([], dispatch_calls)

    async def test_default_workspace_mismatch_blocks_but_same_and_opaque_selectors_dispatch(
        self,
    ) -> None:
        dispatch_calls: list[dict] = []

        async def dispatch(**kwargs):
            dispatch_calls.append(kwargs)
            return {"dispatched": True}

        wrappers = self._load_workflow_wrappers(
            dispatch, default_project_path=self.workspace_b
        )
        registry = wrappers._test_stubs["daem0nmcp.context_manager"].workspace_registry
        self.gate.record_briefing(self.scope)
        with (
            patch.dict(sys.modules, wrappers._test_stubs),
            installed_invocation(
                self.scope, self.gate, workspace_resolver=registry.resolve
            ),
        ):
            defaulted = await wrappers.consult(action="recall", topic="auth")
            same = await wrappers.consult(
                action="recall", topic="auth", project_path=self.workspace
            )
            opaque = await wrappers.consult(
                action="recall", topic="auth", project_path="ws_a"
            )
        self.assertEqual("TOKEN_SCOPE_MISMATCH", defaulted["violation"])
        self.assertEqual({"dispatched": True}, same)
        self.assertEqual({"dispatched": True}, opaque)
        self.assertEqual(2, len(dispatch_calls))

    @unittest.skipUnless(
        importlib.util.find_spec("fastmcp") is not None,
        "FastMCP dependency unavailable",
    )
    def test_real_fastmcp_remote_identity_contract_is_importable(self) -> None:
        from fastmcp.server.dependencies import get_access_token
        from fastmcp.server.middleware import MiddlewareContext

        self.assertTrue(callable(get_access_token))
        self.assertIsNotNone(MiddlewareContext)


if __name__ == "__main__":
    unittest.main()
