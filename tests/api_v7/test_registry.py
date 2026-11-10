from __future__ import annotations

import importlib
import unittest

from pydantic import BaseModel, ConfigDict

from daem0nmcp import __version__
from daem0nmcp.covenant import CovenantLevel


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    workspace_id: str


class _Output(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    value: str


async def _handler(workspace_id: str) -> _Output:
    return _Output(value=workspace_id)


class RegistryTests(unittest.TestCase):
    def _tool(self, name: str = "session_brief", *, pinned: bool = True):
        from daem0nmcp.api.v7.registry import ToolSpec

        return ToolSpec(
            name=name,
            description="Return a bounded session response.",
            handler=_handler,
            input_model=_Input,
            output_model=_Output,
            category="session",
            tags=("session",),
            covenant=CovenantLevel.EXEMPT,
            task_mode="forbidden",
            annotations={
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
            pinned=pinned,
        )

    def test_spec_metadata_is_complete_and_immutable(self) -> None:
        tool = self._tool()

        self.assertEqual(tool.version, __version__)
        self.assertEqual(tool.meta["daem0nmcp/apiVersion"], "7")
        self.assertTrue(tool.meta["daem0nmcp/pinned"])
        self.assertEqual(tool.meta["daem0nmcp/covenant"], "exempt")
        self.assertEqual(tool.meta["daem0nmcp/taskMode"], "forbidden")
        with self.assertRaises(TypeError):
            tool.annotations["readOnlyHint"] = False

    def test_manifest_rejects_duplicate_policy_gap_and_schema_leak(self) -> None:
        from daem0nmcp.api.v7.registry import ManifestError, V7Manifest

        tool = self._tool()
        with self.assertRaisesRegex(ManifestError, "duplicate"):
            V7Manifest(
                tools=(tool, tool),
                resources=(),
                policy={"session_brief": CovenantLevel.EXEMPT},
                require_full_surface=False,
            )
        with self.assertRaisesRegex(ManifestError, "policy"):
            V7Manifest(
                tools=(tool,),
                resources=(),
                policy={"other": CovenantLevel.EXEMPT},
                require_full_surface=False,
            )

        class _LeakyInput(BaseModel):
            project_path: str

        with self.assertRaisesRegex(ManifestError, "project_path"):
            V7Manifest(
                tools=(tool.replace(input_model=_LeakyInput),),
                resources=(),
                policy={"session_brief": CovenantLevel.EXEMPT},
                require_full_surface=False,
            )

        class _IntegerIdInput(BaseModel):
            memory_id: int

        with self.assertRaisesRegex(ManifestError, "integer public ID"):
            V7Manifest(
                tools=(tool.replace(input_model=_IntegerIdInput),),
                resources=(),
                policy={"session_brief": CovenantLevel.EXEMPT},
                require_full_surface=False,
            )

    def test_full_manifest_requires_six_pins_and_no_legacy_names(self) -> None:
        from daem0nmcp.api.v7.registry import ManifestError, V7Manifest

        with self.assertRaisesRegex(ManifestError, "six pinned"):
            V7Manifest(
                tools=(self._tool(),),
                resources=(),
                policy={"session_brief": CovenantLevel.EXEMPT},
            )
        with self.assertRaisesRegex(ManifestError, "legacy"):
            V7Manifest(
                tools=(self._tool("commune", pinned=False),),
                resources=(),
                policy={"commune": CovenantLevel.EXEMPT},
                require_full_surface=False,
            )

    def test_inspectable_server_is_fresh_and_legacy_import_cannot_mutate_it(
        self,
    ) -> None:
        from daem0nmcp.api.v7.registry import InspectableV7Server, V7Manifest

        manifest = V7Manifest(
            tools=(self._tool(),),
            resources=(),
            policy={"session_brief": CovenantLevel.EXEMPT},
            require_full_surface=False,
        )
        first = InspectableV7Server(manifest)
        second = InspectableV7Server(manifest)
        self.assertIsNot(first, second)
        before = first.tool_names

        # Importing a decorator-heavy compatibility module is incapable of
        # changing a v7 registry assembled from its immutable manifest.
        importlib.import_module("daem0nmcp.workflows.commune")
        self.assertEqual(first.tool_names, before)
        self.assertEqual(first.list_tools()[0]["name"], "session_brief")
        self.assertNotIn("commune", first.tool_names)


if __name__ == "__main__":
    unittest.main()
