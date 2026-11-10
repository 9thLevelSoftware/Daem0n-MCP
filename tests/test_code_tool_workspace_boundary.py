"""Workspace containment coverage through the MCP index_project wrapper."""

import contextlib
import importlib.util
import shutil
import sys
import types
import unittest
import uuid
from pathlib import Path

from daem0nmcp.covenant import (
    CapabilityAuthority,
    CovenantGate,
    CovenantStateStore,
    InvocationScope,
    installed_invocation,
)
from daem0nmcp.workspace import WorkspaceRegistry


def _module(name, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


class _MCP:
    def tool(self, **kwargs):
        return lambda function: function


class _CodeIndexManager:
    def __init__(self, db=None, qdrant=None):
        self.db = db

    async def index_project(self, project_path, patterns, *, workspace_root):
        return {
            "indexed": 0,
            "files_processed": 0,
            "project": project_path,
            "workspace_root": workspace_root,
            "patterns": patterns,
        }


@contextlib.contextmanager
def _load_code_tools(project_root: Path, helper_result: Path):
    registry = WorkspaceRegistry([project_root])
    workspace = registry.default
    context = types.SimpleNamespace(
        project_path=str(project_root.resolve()),
        workspace_id=workspace.workspace_id,
        storage_path=str(project_root / ".daem0nmcp" / "storage"),
        db_manager=object(),
    )

    async def get_project_context(selector):
        return context

    def resolve_within_project(root, target):
        return helper_result.resolve(), None

    fake_modules = {
        "daem0nmcp.config": _module(
            "daem0nmcp.config", settings=types.SimpleNamespace()
        ),
        "daem0nmcp.context_manager": _module(
            "daem0nmcp.context_manager",
            _default_project_path=str(project_root),
            _missing_project_path_error=lambda: {"error": "missing"},
            _resolve_within_project=resolve_within_project,
            get_project_context=get_project_context,
            workspace_registry=registry,
        ),
        "daem0nmcp.logging_config": _module(
            "daem0nmcp.logging_config", with_request_id=lambda function: function
        ),
        "daem0nmcp.mcp_instance": _module("daem0nmcp.mcp_instance", mcp=_MCP()),
        "daem0nmcp.models": _module("daem0nmcp.models", Memory=object),
        "sqlalchemy": _module("sqlalchemy", select=lambda *args: None),
        "daem0nmcp.code_indexer": _module(
            "daem0nmcp.code_indexer",
            CodeIndexManager=_CodeIndexManager,
            is_available=lambda: True,
        ),
        "daem0nmcp.qdrant_store": _module(
            "daem0nmcp.qdrant_store",
            QdrantVectorStore=lambda **kwargs: (_ for _ in ()).throw(RuntimeError()),
        ),
    }
    originals = {name: sys.modules.get(name) for name in fake_modules}
    sys.modules.update(fake_modules)
    module_name = "daem0nmcp.tools._code_tool_workspace_boundary_test"
    try:
        source = (
            Path(__file__).resolve().parents[1]
            / "daem0nmcp"
            / "tools"
            / "code_tools.py"
        )
        spec = importlib.util.spec_from_file_location(module_name, source)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        yield module
    finally:
        for name, original in originals.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


class CodeToolWorkspaceBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / ".test_tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.temp_root = base / f"code-tool-security-{uuid.uuid4().hex}"
        self.project = self.temp_root / "project"
        self.project.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.temp_root, ignore_errors=True)

    async def test_wrapper_hands_helper_resolved_root_to_index_manager(self):
        requested = self.project / "requested"
        requested.mkdir()
        helper_approved = self.project / "approved-by-helper"
        helper_approved.mkdir()
        registry = WorkspaceRegistry([self.project])
        scope = InvocationScope(
            "test-principal", "test-session", str(self.project.resolve())
        )
        gate = CovenantGate(
            state_store=CovenantStateStore(),
            authority=CapabilityAuthority(
                secret=b"code-tool-boundary-test-secret-32b",
                kid="test",
            ),
        )
        gate.record_briefing(scope)
        with (
            _load_code_tools(self.project, helper_approved) as code_tools,
            installed_invocation(scope, gate, workspace_resolver=registry.resolve),
        ):
            response = await code_tools.index_project(
                path="requested",
                patterns=["**/*.py"],
                project_path=str(self.project),
            )

        result = response["result"]
        self.assertEqual(result["project"], str(helper_approved.resolve()))
        self.assertEqual(result["workspace_root"], str(self.project.resolve()))
        self.assertEqual(result["patterns"], ["**/*.py"])


if __name__ == "__main__":
    unittest.main()
