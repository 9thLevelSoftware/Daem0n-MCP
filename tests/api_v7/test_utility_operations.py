from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from daem0nmcp.api.v7.application import AdmittedRequest
from daem0nmcp.workspace import WorkspaceRegistry


class UtilityOperationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.workspace = WorkspaceRegistry([self.root], default_root=self.root).default

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _dependencies(self):
        from daem0nmcp.api.v7.utility_operations import (
            UtilityOperationDependencies,
        )

        return UtilityOperationDependencies(cursor_secret=b"u" * 32)

    async def test_context_compress_is_deterministic_bounded_and_extractive(
        self,
    ) -> None:
        from daem0nmcp.api.v7.utility_operations import build_utility_operations

        dependencies = self._dependencies()
        try:
            handler = build_utility_operations(dependencies)["context_compress"]
            self.assertTrue(
                getattr(
                    handler,
                    "__daem0nmcp_sync_fallback_safe__",
                    False,
                )
            )
            request = AdmittedRequest(
                "context_compress",
                {
                    "workspace_id": self.workspace.workspace_id,
                    "text": "alpha beta gamma delta epsilon",
                    "rate": 0.4,
                    "content_type": "plain",
                    "preserve_code": False,
                },
            )
            first = await handler(workspace=self.workspace, request=request)
            second = await handler(workspace=self.workspace, request=request)
        finally:
            dependencies.close()

        self.assertEqual(first, second)
        self.assertEqual(first.text, "alpha beta")
        self.assertEqual(first.original_tokens, 5)
        self.assertEqual(first.rendered_tokens, 2)
        self.assertEqual(first.ratio, 0.4)
        self.assertEqual(first.provider, "deterministic-extractive-v1")

    async def test_todo_scan_is_relative_filtered_and_hmac_paginated(self) -> None:
        from daem0nmcp.api.v7.utility_operations import (
            UtilityOperationError,
            build_utility_operations,
        )

        source = self.root / "src"
        source.mkdir()
        (source / "a.py").write_text(
            "# TODO: first\n# FIXME second\nprint('ok')\n",
            encoding="utf-8",
        )
        (source / "b.py").write_text(
            "// HACK: third\n// NOTE fourth\n",
            encoding="utf-8",
        )
        ignored = self.root / ".git"
        ignored.mkdir()
        (ignored / "ignored.py").write_text("# TODO hidden\n", encoding="utf-8")

        dependencies = self._dependencies()
        try:
            handler = build_utility_operations(dependencies)["code_todos_scan"]
            first_request = AdmittedRequest(
                "code_todos_scan",
                {
                    "workspace_id": self.workspace.workspace_id,
                    "relative_root": ".",
                    "types": {"todo", "fixme", "hack", "note"},
                    "cursor": None,
                    "limit": 2,
                },
            )
            first = await handler(workspace=self.workspace, request=first_request)
            self.assertTrue(first.truncated)
            self.assertIsNotNone(first.next_cursor)
            second = await handler(
                workspace=self.workspace,
                request=AdmittedRequest(
                    "code_todos_scan",
                    {**first_request.model_dump(), "cursor": first.next_cursor},
                ),
            )
            with self.assertRaises(UtilityOperationError) as failure:
                replacement = "0" if first.next_cursor[-1] != "0" else "1"
                await handler(
                    workspace=self.workspace,
                    request=AdmittedRequest(
                        "code_todos_scan",
                        {
                            **first_request.model_dump(),
                            "cursor": first.next_cursor[:-1] + replacement,
                        },
                    ),
                )
        finally:
            dependencies.close()

        findings = first.items + second.items
        self.assertEqual(
            [item.todo_type for item in findings],
            ["todo", "fixme", "hack", "note"],
        )
        self.assertEqual(
            [item.relative_file_path for item in findings],
            ["src/a.py", "src/a.py", "src/b.py", "src/b.py"],
        )
        self.assertFalse(second.truncated)
        self.assertIsNone(second.next_cursor)
        self.assertEqual(failure.exception.code, "INVALID_ARGUMENT")

    async def test_refactor_proposal_reads_only_the_bounded_relative_file(
        self,
    ) -> None:
        from daem0nmcp.api.v7.utility_operations import build_utility_operations

        source = self.root / "src"
        source.mkdir()
        (source / "service.py").write_text(
            "def work():\n"
            "    # TODO: split responsibility\n"
            f"    value = '{'x' * 120}'\n"
            "    return value\n",
            encoding="utf-8",
        )
        dependencies = self._dependencies()
        try:
            handler = build_utility_operations(dependencies)["code_refactor_propose"]
            result = await handler(
                workspace=self.workspace,
                request=AdmittedRequest(
                    "code_refactor_propose",
                    {
                        "workspace_id": self.workspace.workspace_id,
                        "relative_file_path": "src/service.py",
                        "objective": "Reduce coupling",
                    },
                ),
            )
        finally:
            dependencies.close()

        self.assertIn("src/service.py", result.proposal)
        self.assertIn("Reduce coupling", result.proposal)
        self.assertTrue(any("debt" in item.lower() for item in result.warnings))
        self.assertTrue(any("long line" in item.lower() for item in result.warnings))
        self.assertEqual(result.affected_entities, [])
        self.assertEqual(result.evidence_refs, [])
        self.assertNotIn(str(self.root), result.model_dump_json())

    async def test_request_workspace_must_match_exact_registered_root(self) -> None:
        from daem0nmcp.api.v7.utility_operations import (
            UtilityOperationError,
            build_utility_operations,
        )

        dependencies = self._dependencies()
        try:
            handler = build_utility_operations(dependencies)["context_compress"]
            with self.assertRaises(UtilityOperationError) as failure:
                await handler(
                    workspace=self.workspace,
                    request=AdmittedRequest(
                        "context_compress",
                        {
                            "workspace_id": "ws_" + "0" * 24,
                            "text": "bounded",
                            "rate": None,
                            "content_type": None,
                            "preserve_code": True,
                        },
                    ),
                )
        finally:
            dependencies.close()

        self.assertEqual(failure.exception.code, "UNAUTHORIZED_WORKSPACE")


if __name__ == "__main__":
    unittest.main()
