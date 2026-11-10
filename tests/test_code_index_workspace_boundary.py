"""Indexing containment tests through CodeIndexManager."""

import importlib.util
import os
import shutil
import sys
import types
import unittest
import uuid
from pathlib import Path

from daem0nmcp.workspace import IndexPathError


class _ReadingIndexer:
    available = True

    def __init__(self):
        self.read_files = []

    def index_file(self, file_path: Path, project_root: Path):
        self.read_files.append(file_path)
        file_path.read_text(encoding="utf-8")
        return []


def _load_code_indexer():
    config = types.ModuleType("daem0nmcp.config")
    config.settings = types.SimpleNamespace(
        parse_tree_cache_maxsize=10,
        index_languages=[],
    )
    original = sys.modules.get("daem0nmcp.config")
    sys.modules["daem0nmcp.config"] = config
    try:
        source = Path(__file__).resolve().parents[1] / "daem0nmcp" / "code_indexer.py"
        module_name = "daem0nmcp._code_index_workspace_boundary_test"
        spec = importlib.util.spec_from_file_location(module_name, source)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        if original is None:
            sys.modules.pop("daem0nmcp.config", None)
        else:
            sys.modules["daem0nmcp.config"] = original


class CodeIndexWorkspaceBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / ".test_tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.temp_root = base / f"code-index-security-{uuid.uuid4().hex}"
        self.temp_root.mkdir()
        self.workspace = self.temp_root / "workspace"
        self.outside = self.temp_root / "outside"
        self.workspace.mkdir()
        self.outside.mkdir()
        (self.workspace / "inside.py").write_text("INSIDE = True\n", encoding="utf-8")
        (self.outside / "outside.py").write_text("SECRET = True\n", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.temp_root, ignore_errors=True)

    def _manager(self):
        module = _load_code_indexer()
        manager = module.CodeIndexManager()
        manager.indexer = _ReadingIndexer()
        return manager

    async def test_valid_relative_pattern_reads_only_workspace_file(self):
        manager = self._manager()

        result = await manager.index_project(
            str(self.workspace), ["*.py"], workspace_root=str(self.workspace)
        )

        self.assertEqual(result["files_processed"], 1)
        self.assertEqual(
            manager.indexer.read_files, [(self.workspace / "inside.py").resolve()]
        )

    async def test_absolute_and_parent_patterns_are_rejected(self):
        for pattern in (str((self.outside / "*.py").resolve()), "../outside/*.py"):
            manager = self._manager()
            with self.subTest(pattern=pattern):
                with self.assertRaises(IndexPathError):
                    await manager.index_project(
                        str(self.workspace),
                        [pattern],
                        workspace_root=str(self.workspace),
                    )
                self.assertEqual(manager.indexer.read_files, [])

    async def test_index_root_outside_selected_workspace_is_rejected(self):
        manager = self._manager()

        with self.assertRaises(IndexPathError):
            await manager.index_project(
                str(self.outside), ["*.py"], workspace_root=str(self.workspace)
            )

        self.assertEqual(manager.indexer.read_files, [])

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    async def test_symlink_escape_is_rejected_before_indexer_reads(self):
        link = self.workspace / "escaped.py"
        try:
            link.symlink_to(self.outside / "outside.py")
        except OSError as exc:
            self.skipTest(f"symlink creation is unavailable: {exc}")
        manager = self._manager()

        with self.assertRaises(IndexPathError):
            await manager.index_project(
                str(self.workspace), ["escaped.py"], workspace_root=str(self.workspace)
            )

        self.assertEqual(manager.indexer.read_files, [])


if __name__ == "__main__":
    unittest.main()
