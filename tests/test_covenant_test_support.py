"""Contracts for the explicit, scoped Covenant test harness."""

from __future__ import annotations

import ast
import asyncio
import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from daem0nmcp import covenant

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
LEGACY_TEST_FILES = tuple(
    path.name for path in sorted((REPOSITORY_ROOT / "tests").glob("test_*.py"))
)


def _protected_legacy_calls(source: str, filename: str) -> list[str]:
    protected = set(covenant.LEGACY_ENTRYPOINTS) - set(covenant.COVENANT_EXEMPT_TOOLS)
    tree = ast.parse(source, filename=filename)
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for imported in node.names:
                if imported.asname:
                    aliases[imported.asname] = imported.name
                else:
                    root_name = imported.name.split(".")[0]
                    aliases[root_name] = root_name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for imported in node.names:
                aliases[imported.asname or imported.name] = f"{module}.{imported.name}"

    def dotted_name(node: ast.expr) -> str:
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return f"{dotted_name(node.value)}.{node.attr}"
        return ""

    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        qualified = dotted_name(node.func)
        leaf_name = qualified.rsplit(".", 1)[-1]
        if leaf_name not in protected:
            continue
        if not qualified.startswith(("daem0nmcp.server.", "daem0nmcp.tools.")):
            continue
        violations.append(f"{filename}:{node.lineno}:{qualified}")
    return violations


def _support_module():
    try:
        return importlib.import_module("tests.covenant_test_support")
    except ModuleNotFoundError as exc:
        raise AssertionError(
            "the scoped Covenant test support module has not been implemented"
        ) from exc


class CovenantTestWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace_path = str(Path(self.temp_dir.name).resolve())

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_unsealed_call_installs_scope_without_leaking_it(self) -> None:
        support = _support_module()
        calls: list[dict[str, object]] = []

        async def remember(
            category: str,
            content: str,
            rationale: str | None = None,
            context: dict | None = None,
            file_path: str | None = None,
            tags: list[str] | None = None,
            happened_at: str | None = None,
            project_path: str | None = None,
        ) -> dict:
            calls.append({"category": category, "content": content})
            return {"id": 1}

        guarded = covenant.legacy_entrypoint("remember")(remember)
        workspace = support.CovenantTestWorkspace(self.workspace_path)

        result = await workspace.call_unsealed(
            guarded,
            category="decision",
            content="Use scoped test authorization",
            project_path=workspace,
        )

        self.assertEqual("COMMUNION_REQUIRED", result["violation"])
        self.assertEqual([], calls)
        self.assertIsNone(covenant.invocation_scope_var.get())
        self.assertIsNone(covenant.covenant_gate_var.get())

    async def test_call_issues_exact_one_use_capability_to_real_guard(self) -> None:
        support = _support_module()
        calls: list[str] = []

        async def remember(
            category: str,
            content: str,
            rationale: str | None = None,
            context: dict | None = None,
            file_path: str | None = None,
            tags: list[str] | None = None,
            happened_at: str | None = None,
            project_path: str | None = None,
        ) -> dict:
            calls.append(content)
            return {"id": 7, "content": content}

        guarded = covenant.legacy_entrypoint("remember")(remember)
        workspace = support.CovenantTestWorkspace(self.workspace_path)
        workspace.gate.record_briefing(workspace.scope)

        result = await workspace.call(
            guarded,
            category="decision",
            content="Bind the exact arguments",
            project_path=workspace,
        )

        self.assertEqual(7, result["id"])
        self.assertEqual(["Bind the exact arguments"], calls)
        self.assertEqual(
            0, workspace.gate.state_store.status(workspace.scope)["active_capabilities"]
        )
        self.assertIsNone(covenant.invocation_scope_var.get())

    async def test_explicit_token_rejects_changed_arguments_and_replay(self) -> None:
        support = _support_module()

        async def remember(
            category: str,
            content: str,
            rationale: str | None = None,
            context: dict | None = None,
            file_path: str | None = None,
            tags: list[str] | None = None,
            happened_at: str | None = None,
            project_path: str | None = None,
        ) -> dict:
            return {"id": 9}

        guarded = covenant.legacy_entrypoint("remember")(remember)
        workspace = support.CovenantTestWorkspace(self.workspace_path)
        workspace.gate.record_briefing(workspace.scope)
        arguments = {
            "category": "decision",
            "content": "Original",
            "project_path": workspace,
        }
        token = workspace.issue(guarded, **arguments)

        mismatch = await workspace.call_unsealed(
            guarded,
            category="decision",
            content="Changed",
            project_path=workspace,
            preflight_token=token,
        )
        accepted = await workspace.call_unsealed(
            guarded, **arguments, preflight_token=token
        )
        replay = await workspace.call_unsealed(
            guarded, **arguments, preflight_token=token
        )

        self.assertEqual("TOKEN_ARGUMENT_MISMATCH", mismatch["violation"])
        self.assertEqual(9, accepted["id"])
        self.assertEqual("TOKEN_REPLAYED", replay["violation"])

    async def test_brief_uses_real_exempt_leaf_inside_scope(self) -> None:
        support = _support_module()

        async def get_briefing(project_path: str | None = None) -> dict:
            covenant.record_current_briefing()
            return {"status": "ready", "project_path": project_path}

        guarded = covenant.legacy_entrypoint("get_briefing")(get_briefing)
        fake_server = types.ModuleType("daem0nmcp.server")
        fake_server.get_briefing = guarded
        workspace = support.CovenantTestWorkspace(self.workspace_path)

        with patch.dict(sys.modules, {"daem0nmcp.server": fake_server}):
            result = await workspace.brief()

        self.assertEqual("ready", result["status"])
        self.assertTrue(workspace.gate.state_store.is_briefed(workspace.scope))
        self.assertIsNone(covenant.invocation_scope_var.get())

    def test_installed_scope_registers_only_explicit_test_roots_and_restores(
        self,
    ) -> None:
        support = _support_module()
        other_root = str((Path(self.workspace_path) / "linked").resolve())
        Path(other_root).mkdir()
        workspace = support.CovenantTestWorkspace(
            self.workspace_path, additional_roots=[other_root]
        )
        original_registry = object()
        fake_context_manager = types.ModuleType("daem0nmcp.context_manager")
        fake_context_manager.workspace_registry = original_registry

        with (
            patch.dict(
                sys.modules, {"daem0nmcp.context_manager": fake_context_manager}
            ),
            workspace.installed(),
        ):
            self.assertEqual(
                Path(self.workspace_path),
                fake_context_manager.workspace_registry.resolve(workspace).root,
            )
            self.assertEqual(
                Path(other_root),
                fake_context_manager.workspace_registry.resolve(other_root).root,
            )
            from daem0nmcp.workspace import WorkspaceAccessError

            with self.assertRaises(WorkspaceAccessError):
                fake_context_manager.workspace_registry.resolve(
                    str(Path(self.workspace_path).parent / "not-registered")
                )

        self.assertIs(original_registry, fake_context_manager.workspace_registry)

    async def test_overlapping_same_workspace_scopes_restore_registry_once(
        self,
    ) -> None:
        support = _support_module()
        workspace = support.CovenantTestWorkspace(self.workspace_path)
        original_registry = object()
        fake_context_manager = types.ModuleType("daem0nmcp.context_manager")
        fake_context_manager.workspace_registry = original_registry

        first_entered = asyncio.Event()
        second_entered = asyncio.Event()
        release_first = asyncio.Event()
        first_exited = asyncio.Event()
        release_second = asyncio.Event()

        async def first_call() -> None:
            with workspace.installed():
                first_entered.set()
                await release_first.wait()
            first_exited.set()

        async def second_call() -> None:
            await first_entered.wait()
            with workspace.installed():
                second_entered.set()
                await release_second.wait()

        with patch.dict(
            sys.modules, {"daem0nmcp.context_manager": fake_context_manager}
        ):
            first_task = asyncio.create_task(first_call())
            second_task = asyncio.create_task(second_call())
            await second_entered.wait()
            release_first.set()
            await first_exited.wait()
            self.assertEqual(
                Path(self.workspace_path),
                fake_context_manager.workspace_registry.resolve(workspace).root,
            )
            release_second.set()
            await asyncio.gather(first_task, second_task)

        self.assertIs(original_registry, fake_context_manager.workspace_registry)


class LegacyTestInvocationShapeTests(unittest.TestCase):
    def test_protected_call_detector_resolves_direct_and_module_aliases(self) -> None:
        source = """
from daem0nmcp.server import remember as save
from daem0nmcp import server as api
import daem0nmcp.tools.memory as memory_tools

save(category="decision", content="direct alias")
api.remember(category="decision", content="package alias")
memory_tools.remember(category="decision", content="module alias")
"""

        self.assertEqual(
            [
                "synthetic.py:6:daem0nmcp.server.remember",
                "synthetic.py:7:daem0nmcp.server.remember",
                "synthetic.py:8:daem0nmcp.tools.memory.remember",
            ],
            _protected_legacy_calls(source, "synthetic.py"),
        )

    def test_protected_legacy_leaves_use_the_scoped_test_harness(self) -> None:
        violations: list[str] = []

        for filename in LEGACY_TEST_FILES:
            path = REPOSITORY_ROOT / "tests" / filename
            source = path.read_text(encoding="utf-8")
            violations.extend(_protected_legacy_calls(source, filename))

        self.assertEqual([], sorted(violations))
