from __future__ import annotations

import unittest


class DashboardV7AdapterTests(unittest.TestCase):
    def test_v7_tool_metadata_references_only_registered_shells(self) -> None:
        from daem0nmcp.api.v7.policy import V7_TOOL_LEVELS
        from daem0nmcp.api.v7.tools import build_tool_specs

        specs = build_tool_specs(
            {name: lambda **_args: None for name in V7_TOOL_LEVELS}
        )
        metadata = {
            spec.name: spec.meta["ui"]["resourceUri"]
            for spec in specs
            if "ui" in spec.meta
        }
        self.assertEqual(
            metadata,
            {
                "memory_recall": "ui://daem0n/search",
                "session_brief": "ui://daem0n/briefing",
                "covenant_status": "ui://daem0n/covenant",
                "community_list": "ui://daem0n/community",
                "knowledge_graph_get": "ui://daem0n/graph",
            },
        )


if __name__ == "__main__":
    unittest.main()
