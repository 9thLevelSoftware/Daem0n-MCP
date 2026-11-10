"""Workspace authorization tests through get_project_context()."""

import contextlib
import contextvars
import importlib.util
import shutil
import sys
import time
import types
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from daem0nmcp.workspace import WorkspaceAccessError, WorkspaceRegistry


class _FakeDatabaseManager:
    constructed_paths: list[Path] = []

    def __init__(self, storage_path: str):
        path = Path(storage_path)
        self.constructed_paths.append(path)
        path.mkdir(parents=True, exist_ok=True)
        self.init_count = 0

    async def init_db(self):
        self.init_count += 1

    async def close(self):
        return None


class _FakeRWLock:
    @contextlib.asynccontextmanager
    async def read(self):
        yield

    @contextlib.asynccontextmanager
    async def write(self):
        yield


def _module(name: str, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


def _load_context_manager(settings_override=None):
    """Load the real boundary with only unavailable heavy dependencies replaced."""
    settings = settings_override or types.SimpleNamespace(
        project_root=".",
        workspace_roots=[],
        max_project_contexts=10,
        context_ttl_seconds=3600,
    )
    dependency_modules = {
        "daem0nmcp.config": _module("daem0nmcp.config", settings=settings),
        "daem0nmcp.covenant": _module(
            "daem0nmcp.covenant", set_context_callback=lambda callback: None
        ),
        "daem0nmcp.database": _module(
            "daem0nmcp.database", DatabaseManager=_FakeDatabaseManager
        ),
        "daem0nmcp.logging_config": _module(
            "daem0nmcp.logging_config",
            request_id_var=contextvars.ContextVar("request_id", default=None),
            set_release_callback=lambda callback: None,
        ),
        "daem0nmcp.memory": _module(
            "daem0nmcp.memory", MemoryManager=lambda database: object()
        ),
        "daem0nmcp.rules": _module(
            "daem0nmcp.rules", RulesEngine=lambda database: object()
        ),
        "daem0nmcp.rwlock": _module("daem0nmcp.rwlock", RWLock=_FakeRWLock),
    }
    original_modules = {name: sys.modules.get(name) for name in dependency_modules}
    sys.modules.update(dependency_modules)
    try:
        source = (
            Path(__file__).resolve().parents[1] / "daem0nmcp" / "context_manager.py"
        )
        module_name = "daem0nmcp._context_workspace_boundary_test"
        spec = importlib.util.spec_from_file_location(module_name, source)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        module._last_eviction = time.time()
        return module
    finally:
        for name, original in original_modules.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


class ProjectContextWorkspaceBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / ".test_tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.temp_root = base / f"context-workspace-{uuid.uuid4().hex}"
        self.temp_root.mkdir()
        self.registered = self.temp_root / "registered"
        self.registered.mkdir()
        _FakeDatabaseManager.constructed_paths.clear()

    def tearDown(self):
        shutil.rmtree(self.temp_root, ignore_errors=True)

    async def test_unknown_selector_is_rejected_before_storage_construction(self):
        context_manager = _load_context_manager()
        context_manager.workspace_registry = WorkspaceRegistry([self.registered])
        unknown = self.temp_root / "unknown"

        with self.assertRaises(WorkspaceAccessError):
            await context_manager.get_project_context(str(unknown))

        self.assertEqual(_FakeDatabaseManager.constructed_paths, [])
        self.assertFalse((unknown / ".daem0nmcp").exists())

    async def test_workspace_id_and_legacy_path_share_one_context(self):
        context_manager = _load_context_manager()
        registry = WorkspaceRegistry([self.registered])
        context_manager.workspace_registry = registry
        workspace = registry.resolve(str(self.registered.resolve()))

        by_path = await context_manager.get_project_context(
            str(self.registered.resolve())
        )
        by_id = await context_manager.get_project_context(workspace.workspace_id)

        self.assertIs(by_path, by_id)
        self.assertEqual(by_path.project_path, str(self.registered.resolve()))
        self.assertEqual(len(_FakeDatabaseManager.constructed_paths), 1)
        await context_manager.cleanup_all_contexts()

    async def test_default_stdio_uses_settings_project_root(self):
        settings = types.SimpleNamespace(
            project_root=str(self.registered),
            workspace_roots=[],
            max_project_contexts=10,
            context_ttl_seconds=3600,
        )
        with patch.dict("os.environ", {}, clear=True):
            context_manager = _load_context_manager(settings)

        try:
            context = await context_manager.get_project_context()
            actual = context.project_path
        except WorkspaceAccessError:
            actual = None

        self.assertEqual(actual, str(self.registered.resolve()))
        await context_manager.cleanup_all_contexts()

    async def test_settings_workspace_roots_are_registered(self):
        settings_root = self.temp_root / "settings-root"
        settings_root.mkdir()
        settings = types.SimpleNamespace(
            project_root=str(self.registered),
            workspace_roots=[str(settings_root)],
            max_project_contexts=10,
            context_ttl_seconds=3600,
        )
        with patch.dict("os.environ", {}, clear=True):
            context_manager = _load_context_manager(settings)

        try:
            context = await context_manager.get_project_context(str(settings_root))
            actual = context.project_path
        except WorkspaceAccessError:
            actual = None

        self.assertEqual(actual, str(settings_root.resolve()))
        await context_manager.cleanup_all_contexts()

    async def test_derived_storage_escape_fails_before_database_construction(self):
        class DerivedPathEscapeError(ValueError):
            pass

        context_manager = _load_context_manager()
        context_manager.workspace_registry = WorkspaceRegistry([self.registered])

        def reject_derived_path(*args, **kwargs):
            raise DerivedPathEscapeError("derived storage escaped")

        context_manager.resolve_derived_path = reject_derived_path
        with self.assertRaises(DerivedPathEscapeError):
            await context_manager.get_project_context(str(self.registered))

        self.assertEqual(_FakeDatabaseManager.constructed_paths, [])


if __name__ == "__main__":
    unittest.main()
