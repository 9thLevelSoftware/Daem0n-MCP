from __future__ import annotations

import importlib
import unittest

from daem0nmcp.api.v7.policy import V7_TOOL_LEVELS


async def _unused(**arguments: object) -> object:
    return arguments


class _Resources:
    async def warnings(self, workspace_id: str) -> object:
        return workspace_id

    async def failures(self, workspace_id: str) -> object:
        return workspace_id

    async def rules(self, workspace_id: str) -> object:
        return workspace_id

    async def active_context(self, workspace_id: str) -> object:
        return workspace_id


class V7FactoryTests(unittest.TestCase):
    def _handlers(self) -> dict[str, object]:
        return dict.fromkeys(V7_TOOL_LEVELS, _unused)

    def test_factory_builds_one_exact_immutable_manifest_and_search_index(self) -> None:
        from daem0nmcp.api.v7.factory import (
            build_tool_search_index,
            build_v7_manifest,
        )

        manifest = build_v7_manifest(self._handlers(), _Resources())
        self.assertEqual({tool.name for tool in manifest.tools}, set(V7_TOOL_LEVELS))
        self.assertEqual(sum(tool.pinned for tool in manifest.tools), 6)
        self.assertEqual(len(manifest.resources), 10)

        index = build_tool_search_index(manifest)
        self.assertEqual(len(index), 75)
        self.assertEqual(index.search("store durable memory")[0].name, "memory_store")
        self.assertEqual(set(index.document_ids), set(V7_TOOL_LEVELS))

        before = tuple(tool.name for tool in manifest.tools)
        importlib.import_module("daem0nmcp.workflows.errors")
        self.assertEqual(tuple(tool.name for tool in manifest.tools), before)

    def test_inspectable_factory_rejects_missing_handler_without_partial_server(
        self,
    ) -> None:
        from daem0nmcp.api.v7.factory import build_inspectable_v7_server
        from daem0nmcp.api.v7.registry import ManifestError

        handlers = self._handlers()
        del handlers["memory_store"]
        with self.assertRaisesRegex(ManifestError, "missing handlers"):
            build_inspectable_v7_server(handlers, _Resources())

    def test_handler_maps_merge_exactly_and_never_override_duplicates(self) -> None:
        from daem0nmcp.api.v7.factory import combine_handler_maps
        from daem0nmcp.api.v7.registry import ManifestError

        names = sorted(V7_TOOL_LEVELS)
        combined = combine_handler_maps(
            dict.fromkeys(names[:30], _unused),
            dict.fromkeys(names[30:], _unused),
        )
        self.assertEqual(set(combined), set(V7_TOOL_LEVELS))
        with self.assertRaisesRegex(ManifestError, "duplicate handler"):
            combine_handler_maps(
                {names[0]: _unused},
                {names[0]: _unused},
            )


if __name__ == "__main__":
    unittest.main()
