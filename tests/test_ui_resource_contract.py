"""Compatibility URI, resource-registration, and package-asset tests."""

import json
import os
import re
import unittest
from importlib import resources
from unittest.mock import patch
from urllib.parse import unquote, urlsplit


class _FakeMCP:
    def __init__(self):
        self.registrations = []

    def resource(self, **metadata):
        def decorate(function):
            self.registrations.append((metadata, function))
            return function

        return decorate


class UIResourceContractTests(unittest.TestCase):
    def setUp(self):
        from daem0nmcp.ui import rendering
        from daem0nmcp.ui import resources as ui_resources

        self.rendering = rendering
        self.ui_resources = ui_resources

    def test_registers_six_base_resources_and_five_compat_templates(self):
        mcp = _FakeMCP()
        self.ui_resources.register_ui_resources(mcp)
        by_uri = {
            metadata["uri"]: (metadata, handler)
            for metadata, handler in mcp.registrations
        }
        expected = {f"ui://daem0n/{app_id}" for app_id in self.rendering.APP_SPECS}
        expected |= {
            f"ui://daem0n/{app_id}/{{data}}"
            for app_id in self.rendering.APP_SPECS
            if app_id != "test"
        }
        self.assertEqual(set(by_uri), expected)
        for uri, (metadata, handler) in by_uri.items():
            self.assertEqual(metadata["mime_type"], self.rendering.MCP_APPS_MIME)
            rendered = handler("{}") if "{data}" in uri else handler()
            self.assertIn('id="app-data"', rendered)

    def test_compat_handlers_neutralize_unhashable_values_at_every_enum_path(self):
        cases = (
            (
                "briefing.status",
                "briefing",
                lambda value: {"status": value},
                ("status",),
                "neutral",
            ),
            (
                "briefing.active_warnings.severity",
                "briefing",
                lambda value: {"active_warnings": [{"severity": value}]},
                ("active_warnings", 0, "severity"),
                "neutral",
            ),
            (
                "briefing.git_changes.files.status",
                "briefing",
                lambda value: {"git_changes": {"files": [{"status": value}]}},
                ("git_changes", "files", 0, "status"),
                "?",
            ),
            (
                "briefing.git_changes.uncommitted_changes.status",
                "briefing",
                lambda value: {
                    "git_changes": {"uncommitted_changes": [{"status": value}]}
                },
                ("git_changes", "files", 0, "status"),
                "?",
            ),
            (
                "covenant.phase",
                "covenant",
                lambda value: {"phase": value},
                ("phase",),
                "unknown",
            ),
            (
                "covenant.preflight.status",
                "covenant",
                lambda value: {"preflight": {"status": value}},
                ("preflight", "status"),
                "unknown",
            ),
            (
                "graph.nodes.category",
                "graph",
                lambda value: {"nodes": [{"id": 1, "category": value}]},
                ("nodes", 0, "category"),
                "default",
            ),
            (
                "graph.edges.relationship",
                "graph",
                lambda value: {
                    "nodes": [{"id": 1}, {"id": 2}],
                    "edges": [{"source": 1, "target": 2, "relationship": value}],
                },
                ("edges", 0, "relationship"),
                "relates_to",
            ),
        )
        hostile_values = ([], {"unexpected": "object"})

        for path_name, app_id, payload_for, result_path, expected in cases:
            handler = self.ui_resources._compat_handler(app_id)
            for hostile_value in hostile_values:
                with self.subTest(
                    path=path_name, value_type=type(hostile_value).__name__
                ):
                    document = handler(json.dumps(payload_for(hostile_value)))
                    match = re.search(
                        r'<script id="app-data" type="application/json">(.*?)</script>',
                        document,
                        re.DOTALL,
                    )
                    self.assertIsNotNone(match)
                    projected = json.loads(match.group(1))
                    actual = projected
                    for segment in result_path:
                        actual = actual[segment]
                    self.assertEqual(actual, expected)

    def test_parser_accepts_one_decoded_object_and_rejects_malformed_inputs(self):
        good = '{"topic":"café / path\\\\name"}'
        self.assertEqual(
            self.rendering.parse_compat_payload(good), {"topic": "café / path\\name"}
        )
        bad = (
            "",
            "{",
            "[]",
            '"scalar"',
            '{"a":1,"a":2}',
            '{"a":NaN}',
            '{"a":Infinity}',
            '{"a":1e309}',
            '{"topic":"\\ud800"}',
            "%7B%22topic%22%3A%22double%22%7D",
        )
        for value in bad:
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    self.rendering.UIResourcePayloadError,
                    "^invalid UI resource payload$",
                ),
            ):
                self.rendering.parse_compat_payload(value)

    def test_parser_enforces_depth_members_and_decoded_utf8_limits(self):
        depth_seventeen = "{}"
        for _ in range(17):
            depth_seventeen = '{"x":' + depth_seventeen + "}"
        too_many_members = json.dumps({"items": [0] * 10_001}, separators=(",", ":"))
        too_many_bytes = '{"x":"' + ("x" * 65_530) + '"}'
        for value in (depth_seventeen, too_many_members, too_many_bytes):
            with (
                self.subTest(size=len(value)),
                self.assertRaisesRegex(
                    self.rendering.UIResourcePayloadError,
                    "^invalid UI resource payload$",
                ),
            ):
                self.rendering.parse_compat_payload(value)

    def test_builder_normalizes_then_percent_encodes_one_uri_segment(self):
        data = {"topic": 'café / "quoted" \\ path', "token": "secret"}
        uri = self.rendering.build_compat_ui_uri("search", data)
        self.assertIsNotNone(uri)
        encoded = urlsplit(uri).path.rsplit("/", 1)[-1]
        self.assertNotIn("/", encoded)
        self.assertIn("%2F", encoded.upper())
        decoded = unquote(encoded)
        self.assertEqual(json.loads(decoded)["topic"], data["topic"])
        self.assertNotIn("token", decoded)
        self.assertEqual(
            self.rendering.parse_compat_payload(decoded)["topic"], data["topic"]
        )

    def test_builder_returns_none_for_payload_or_uri_limit(self):
        huge = {
            "nodes": [{"id": index + 1, "content": "x" * 1000} for index in range(100)]
        }
        self.assertIsNone(self.rendering.build_compat_ui_uri("graph", huge))
        with patch.object(self.rendering, "MAX_COMPAT_URI_CHARS", 20):
            self.assertIsNone(
                self.rendering.build_compat_ui_uri("search", {"topic": "small"})
            )
        self.assertIsNone(self.rendering.build_compat_ui_uri("unknown", {}))

    def test_all_declared_assets_are_package_readable_and_nonempty(self):
        root = resources.files("daem0nmcp.ui")
        names = {"templates/app.html"}
        for spec in self.rendering.APP_SPECS.values():
            names.update(f"static/{name}" for name in spec.scripts)
            names.update(f"static/{name}" for name in spec.styles)
        for name in sorted(names):
            with self.subTest(name=name):
                asset = root.joinpath(*name.split("/"))
                self.assertTrue(asset.is_file())
                self.assertTrue(asset.read_bytes())

    def test_missing_runtime_asset_fails_closed(self):
        self.rendering._load_package_text.cache_clear()
        with self.assertRaisesRegex(RuntimeError, "required UI asset is unavailable"):
            self.rendering._load_package_text("static", "missing-runtime.js")

    def test_trusted_asset_with_a_raw_closing_element_fails_closed(self):
        class FakeAsset:
            def read_text(self, encoding):
                self.encoding = encoding
                return "safe();</script><script>unsafe();"

        class FakeRoot:
            def joinpath(self, *parts):
                self.parts = parts
                return FakeAsset()

        self.rendering._load_package_text.cache_clear()
        try:
            with (
                patch.object(
                    self.rendering.resources, "files", return_value=FakeRoot()
                ),
                self.assertRaisesRegex(RuntimeError, "required UI asset is unsafe"),
            ):
                self.rendering._load_package_text("static", "runtime.js")
        finally:
            self.rendering._load_package_text.cache_clear()

    def test_only_the_single_secure_html_shell_remains(self):
        template_root = resources.files("daem0nmcp.ui").joinpath("templates")
        names = sorted(
            item.name for item in template_root.iterdir() if item.name.endswith(".html")
        )
        self.assertEqual(names, ["app.html"])

    def test_environment_can_enable_text_only_mode(self):
        from daem0nmcp.config import Settings

        with patch.dict(os.environ, {"DAEM0NMCP_UI_RENDERING_ENABLED": "false"}):
            configured = Settings(_env_file=None)
        self.assertFalse(configured.ui_rendering_enabled)

    def test_text_only_mode_omits_hint_without_changing_data_or_text(self):
        from daem0nmcp.ui import fallback

        data = {
            "status": "ready",
            "nested": {"value": 7},
            "ui_resource": "ui://daem0n/stale",
        }
        with patch.object(fallback.settings, "ui_rendering_enabled", True):
            visual = fallback.format_with_ui_hint(
                data, "ui://daem0n/briefing", "exact text"
            )
        with patch.object(fallback.settings, "ui_rendering_enabled", False):
            text_only = fallback.format_with_ui_hint(
                data, "ui://daem0n/briefing", "exact text"
            )
        self.assertEqual(visual["text"], text_only["text"])
        self.assertEqual(
            text_only,
            {"status": "ready", "nested": {"value": 7}, "text": "exact text"},
        )
        self.assertEqual(
            data,
            {
                "status": "ready",
                "nested": {"value": 7},
                "ui_resource": "ui://daem0n/stale",
            },
        )


if __name__ == "__main__":
    unittest.main()
