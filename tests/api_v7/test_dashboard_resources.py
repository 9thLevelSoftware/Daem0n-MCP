from __future__ import annotations

import inspect
import unittest


class DashboardResourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_static_shells_are_exact_data_free_html(self) -> None:
        from daem0nmcp.api.v7.dashboard_resources import (
            DASHBOARD_RESOURCE_URIS,
            build_dashboard_resource_specs,
        )

        specs = build_dashboard_resource_specs()

        self.assertEqual({spec.uri_template for spec in specs}, DASHBOARD_RESOURCE_URIS)
        self.assertEqual(len(specs), 6)
        for spec in specs:
            self.assertFalse(spec.requires_workspace)
            self.assertEqual(spec.mime_type, "text/html;profile=mcp-app")
            document = spec.handler()
            self.assertIn("Content-Security-Policy", document)
            self.assertIn("default-src 'none'", document)
            self.assertIn('<script id="app-data" type="application/json">', document)
            self.assertNotIn('"workspace_id":"ws_', document)
            self.assertNotIn("memory://workspaces/", document)

    async def test_fastmcp_adapter_exposes_shells_without_workspace_arguments(
        self,
    ) -> None:
        from daem0nmcp.api.v7.dashboard_resources import (
            build_dashboard_resource_specs,
        )
        from daem0nmcp.api.v7.fastmcp import _resource_adapter

        for spec in build_dashboard_resource_specs():
            adapter = _resource_adapter(spec)
            self.assertEqual(inspect.signature(adapter).parameters, {})
            document = await adapter()
            self.assertIn("Content-Security-Policy", document)

    def test_manifest_allows_only_the_registered_static_uris(self) -> None:
        from daem0nmcp.api.v7.registry import ManifestError, ResourceSpec

        with self.assertRaisesRegex(ManifestError, "static resource URI"):
            ResourceSpec(
                uri_template="ui://daem0n/not-registered",
                name="invalid_dashboard",
                description="Invalid dashboard.",
                handler=lambda: "",
                output_model=None,
                mime_type="text/html;profile=mcp-app",
                requires_workspace=False,
            )


if __name__ == "__main__":
    unittest.main()
