"""Security regression tests for workspace and indexing isolation."""

import json
import os
import shutil
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

import daem0nmcp.workspace as workspace_module
from daem0nmcp.workspace import (
    IndexPathError,
    WorkspaceAccessError,
    WorkspaceRegistry,
    normalize_resolved_path,
    resolve_index_file,
    resolve_index_target,
    validate_index_patterns,
)


class WorkspaceSecurityTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / ".test_tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.temp_root = base / f"workspace-security-{uuid.uuid4().hex}"
        self.temp_root.mkdir()
        self.primary = self.temp_root / "primary"
        self.secondary = self.temp_root / "secondary"
        self.primary.mkdir()
        self.secondary.mkdir()

    def tearDown(self):
        shutil.rmtree(self.temp_root, ignore_errors=True)

    def test_registered_id_and_legacy_canonical_path_resolve_same_workspace(self):
        registry = WorkspaceRegistry([self.primary, self.secondary])
        primary = registry.resolve(str(self.primary.resolve()))

        self.assertTrue(primary.workspace_id.startswith("ws_"))
        self.assertEqual(registry.resolve(primary.workspace_id), primary)
        self.assertEqual(registry.resolve(str(self.primary.resolve())), primary)

    def test_workspace_ids_are_stable_and_opaque(self):
        first = WorkspaceRegistry([self.primary, self.secondary])
        second = WorkspaceRegistry([self.secondary, self.primary])

        first_id = first.resolve(str(self.primary.resolve())).workspace_id
        second_id = second.resolve(str(self.primary.resolve())).workspace_id
        self.assertEqual(first_id, second_id)
        self.assertNotIn(self.primary.name, first_id)

    def test_unknown_selector_is_rejected_without_creating_storage(self):
        registry = WorkspaceRegistry([self.primary])
        unknown = self.temp_root / "attacker-selected"

        with self.assertRaisesRegex(WorkspaceAccessError, "UNAUTHORIZED_WORKSPACE"):
            registry.resolve(str(unknown))

        self.assertFalse(unknown.exists())
        self.assertFalse((unknown / ".daem0nmcp").exists())

    def test_unauthorized_error_does_not_disclose_registered_roots(self):
        registry = WorkspaceRegistry([self.primary])

        with self.assertRaises(WorkspaceAccessError) as raised:
            registry.resolve(str(self.secondary))

        self.assertNotIn(str(self.primary), str(raised.exception))

    def test_registry_loads_project_root_and_explicit_workspace_roots(self):
        registry = WorkspaceRegistry.from_environment(
            {
                "DAEM0NMCP_PROJECT_ROOT": str(self.primary),
                "DAEM0NMCP_WORKSPACE_ROOTS": json.dumps([str(self.secondary)]),
            }
        )

        self.assertEqual(registry.default.root, self.primary.resolve())
        self.assertEqual(
            registry.resolve(str(self.secondary)).root, self.secondary.resolve()
        )

    def test_registry_loads_default_cwd_and_roots_from_settings(self):
        factory = getattr(WorkspaceRegistry, "from_settings", None)
        self.assertIsNotNone(factory)
        if factory is None:
            return

        registry = factory(
            SimpleNamespace(project_root=".", workspace_roots=[str(self.secondary)])
        )

        self.assertEqual(registry.default.root, Path.cwd().resolve())
        self.assertEqual(
            registry.resolve(str(self.secondary)).root, self.secondary.resolve()
        )

    def test_derived_path_resolver_rejects_parent_escape_without_symlinks(self):
        resolver = getattr(workspace_module, "resolve_derived_path", None)
        self.assertIsNotNone(resolver)
        if resolver is None:
            return

        with self.assertRaises(ValueError):
            resolver(self.primary, "..", "secondary", "storage")

    def test_derived_path_resolver_accepts_nested_in_workspace_path(self):
        resolver = getattr(workspace_module, "resolve_derived_path", None)
        self.assertIsNotNone(resolver)
        if resolver is None:
            return

        resolved = resolver(self.primary, ".daem0nmcp", "storage")
        self.assertEqual(resolved, self.primary / ".daem0nmcp" / "storage")

    @unittest.skipUnless(os.name == "nt", "Windows path spelling regression")
    def test_resolved_path_normalizes_drive_and_unc_extended_spellings(self):
        drive = Path(r"D:\workspace\storage")
        extended_drive = Path(r"\\?\D:\workspace\storage")
        unc = Path(r"\\server\share\workspace")
        extended_unc = Path(r"\\?\UNC\server\share\workspace")

        self.assertEqual(normalize_resolved_path(extended_drive), drive)
        self.assertEqual(normalize_resolved_path(extended_unc), unc)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_derived_path_resolver_rejects_nested_storage_symlink(self):
        resolver = getattr(workspace_module, "resolve_derived_path", None)
        self.assertIsNotNone(resolver)
        if resolver is None:
            return

        metadata = self.primary / ".daem0nmcp"
        metadata.mkdir()
        linked_storage = metadata / "storage"
        try:
            linked_storage.symlink_to(self.secondary, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink creation is unavailable: {exc}")

        with self.assertRaises(ValueError):
            resolver(self.primary, ".daem0nmcp", "storage")

    def test_index_target_must_be_relative_and_inside_workspace(self):
        workspace = WorkspaceRegistry([self.primary]).default
        nested = self.primary / "src"
        nested.mkdir()

        self.assertEqual(resolve_index_target(workspace, "src"), nested.resolve())

        for target in (str(nested.resolve()), "../secondary", "src/../../secondary"):
            with self.subTest(target=target), self.assertRaises(IndexPathError):
                resolve_index_target(workspace, target)

    def test_index_patterns_reject_absolute_and_parent_components(self):
        good = ["**/*.py", "src/*.ts"]
        self.assertEqual(validate_index_patterns(good), good)

        for pattern in (
            str((self.secondary / "*.py").resolve()),
            "../*.py",
            "src/../*.py",
            "**/../../*.py",
        ):
            with self.subTest(pattern=pattern), self.assertRaises(IndexPathError):
                validate_index_patterns([pattern])

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_resolved_index_file_cannot_escape_workspace_through_symlink(self):
        outside_file = self.secondary / "secret.py"
        outside_file.write_text("SECRET = True\n", encoding="utf-8")
        link = self.primary / "linked.py"
        try:
            link.symlink_to(outside_file)
        except OSError as exc:
            self.skipTest(f"symlink creation is unavailable: {exc}")

        workspace = WorkspaceRegistry([self.primary]).default
        with self.assertRaises(IndexPathError):
            resolve_index_file(workspace.root, link)


if __name__ == "__main__":
    unittest.main()
