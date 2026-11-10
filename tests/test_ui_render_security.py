"""Security tests for the single MCP app document boundary."""

import base64
import hashlib
import json
import re
import unittest
from html.parser import HTMLParser

INJECTION = "</script><img src=x onerror=alert(1)>\"'<svg onload=alert(2)>};color:red&\u2028\u2029"


class _DocumentCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.start_tags = []
        self.end_tags = []
        self.data_by_open_tag = []
        self._open = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        self.start_tags.append((tag, values))
        self._open.append((tag, values, []))

    def handle_endtag(self, tag):
        self.end_tags.append(tag)
        if self._open and self._open[-1][0] == tag:
            opened = self._open.pop()
            self.data_by_open_tag.append((opened[0], opened[1], "".join(opened[2])))

    def handle_data(self, data):
        if self._open:
            self._open[-1][2].append(data)


def _parse_document(document):
    parser = _DocumentCollector()
    parser.feed(document)
    return parser


def _app_data(document):
    parser = _parse_document(document)
    blocks = [
        data
        for tag, attrs, data in parser.data_by_open_tag
        if tag == "script" and attrs.get("id") == "app-data"
    ]
    if len(blocks) != 1:
        raise AssertionError(f"expected one app-data block, got {len(blocks)}")
    return blocks[0], json.loads(blocks[0])


def _fixtures():
    return {
        "test": {},
        "search": {
            "topic": INJECTION,
            "decisions": [
                {
                    "id": INJECTION,
                    "content": INJECTION,
                    "created_at": INJECTION,
                    "tags": [INJECTION],
                    "category": INJECTION,
                }
            ],
            "warnings": [
                {
                    "id": INJECTION,
                    "content": INJECTION,
                    "created_at": INJECTION,
                    "tags": [INJECTION],
                }
            ],
            "patterns": [
                {
                    "id": INJECTION,
                    "content": INJECTION,
                    "created_at": INJECTION,
                    "tags": [INJECTION],
                }
            ],
            "learnings": [
                {
                    "id": INJECTION,
                    "content": INJECTION,
                    "created_at": INJECTION,
                    "tags": [INJECTION],
                }
            ],
        },
        "briefing": {
            "status": INJECTION,
            "message": INJECTION,
            "recent_decisions": [{"content": INJECTION, "created_at": INJECTION}],
            "active_warnings": [{"content": INJECTION, "severity": INJECTION}],
            "failed_approaches": [{"content": INJECTION}],
            "git_changes": {"files": [{"status": INJECTION, "path": INJECTION}]},
            "focus_areas": [{"topic": INJECTION}],
        },
        "covenant": {
            "phase": INJECTION,
            "phase_label": INJECTION,
            "phase_description": INJECTION,
            "message": INJECTION,
            "preflight": {
                "status": INJECTION,
                "expires_at": INJECTION,
                "token": INJECTION,
            },
            "credential": INJECTION,
        },
        "community": {
            "communities": [
                {"id": "root", "name": INJECTION, "summary": INJECTION},
                {
                    "id": "child",
                    "parent_community_id": "root",
                    "name": INJECTION,
                    "summary": INJECTION,
                },
            ],
            "path": [{"id": "breadcrumb", "name": INJECTION}],
        },
        "graph": {
            "topic": INJECTION,
            "nodes": [
                {
                    "id": "source",
                    "content": INJECTION,
                    "full_content": INJECTION,
                    "category": "decision",
                    "tags": [INJECTION],
                    "created_at": "2026-08-08T12:00:00Z",
                    "community_id": "community",
                },
                {
                    "id": "target",
                    "content": INJECTION,
                    "full_content": INJECTION,
                    "category": "warning",
                    "tags": [INJECTION],
                    "created_at": "2026-08-08T13:00:00Z",
                    "community_id": "community",
                },
            ],
            "edges": [
                {
                    "source": "source",
                    "target": "target",
                    "relationship": "relates_to",
                    "description": INJECTION,
                }
            ],
            "path": ["source", "target"],
        },
    }


class UIDocumentSecurityTests(unittest.TestCase):
    def setUp(self):
        from daem0nmcp.ui import rendering, resources

        self.rendering = rendering
        self.builders = {
            "test": lambda data: resources._build_test_ui(),
            "search": resources._build_search_ui,
            "briefing": resources._build_briefing_ui,
            "covenant": resources._build_covenant_ui,
            "community": resources._build_community_ui,
            "graph": resources._build_graph_ui,
        }

    def test_every_app_uses_one_non_executable_escaped_json_boundary(self):
        for app_id, fixture in _fixtures().items():
            with self.subTest(app_id=app_id):
                document = self.builders[app_id](fixture)
                parser = _parse_document(document)
                raw, parsed = _app_data(document)
                outside_data = document.replace(raw, "", 1)
                self.assertIsInstance(parsed, dict)
                self.assertNotIn("<", raw)
                self.assertNotIn(">", raw)
                self.assertNotIn("&", raw)
                self.assertNotIn("\u2028", raw)
                self.assertNotIn("\u2029", raw)
                self.assertIn("\\u003c", raw) if app_id != "test" else None
                for tag, attrs in parser.start_tags:
                    self.assertNotEqual(tag, "img")
                    self.assertFalse(
                        any(name.lower().startswith("on") for name in attrs)
                    )
                    self.assertNotIn("style", attrs)
                scripts = [attrs for tag, attrs in parser.start_tags if tag == "script"]
                app_scripts = [
                    attrs for attrs in scripts if attrs.get("id") == "app-data"
                ]
                self.assertEqual(
                    app_scripts, [{"id": "app-data", "type": "application/json"}]
                )
                self.assertNotIn("credential", parsed)
                self.assertNotIn("token", json.dumps(parsed))
                self.assertNotIn("onerror=alert(1)", outside_data)
                self.assertNotIn("onload=alert(2)", outside_data)
                self.assertNotIn("};color:red", outside_data)

    def test_normal_unicode_round_trips_for_each_data_app(self):
        fixtures = {
            "search": {
                "topic": "café / 東京",
                "decisions": [{"id": 1, "content": "Use PostgreSQL ✓"}],
            },
            "briefing": {
                "git_changes": {"files": [{"status": "M", "path": "src/東京.py"}]},
                "message": "ready ✓",
            },
            "covenant": {
                "phase": "counsel",
                "preflight": {"status": "valid", "expires_at": "2026-08-08T12:00:00Z"},
            },
            "community": {
                "communities": [{"id": 1, "name": "Équipe", "summary": "résumé"}]
            },
            "graph": {
                "topic": "mémoire",
                "nodes": [{"id": 1, "content": "東京", "full_content": "東京 graph"}],
            },
        }
        for app_id, fixture in fixtures.items():
            with self.subTest(app_id=app_id):
                _, parsed = _app_data(self.builders[app_id](fixture))
                self.assertIn(
                    "✓", json.dumps(parsed, ensure_ascii=False)
                ) if app_id in {"search", "briefing"} else None
                self.assertIsInstance(parsed, dict)

    def test_graph_and_community_hostile_display_text_survives_only_escaped(self):
        community_raw, community = _app_data(
            self.builders["community"](_fixtures()["community"])
        )
        self.assertEqual(
            [item["name"] for item in community["communities"]],
            [INJECTION, INJECTION],
        )
        self.assertEqual(
            [item["summary"] for item in community["communities"]],
            [INJECTION, INJECTION],
        )
        self.assertEqual(community["path"], [{"id": "breadcrumb", "name": INJECTION}])

        graph_raw, graph = _app_data(self.builders["graph"](_fixtures()["graph"]))
        self.assertEqual(graph["topic"], INJECTION)
        self.assertEqual(
            [item["content"] for item in graph["nodes"]], [INJECTION, INJECTION]
        )
        self.assertEqual(
            [item["full_content"] for item in graph["nodes"]],
            [INJECTION, INJECTION],
        )
        self.assertEqual(
            [item["tags"] for item in graph["nodes"]], [[INJECTION], [INJECTION]]
        )
        self.assertEqual(graph["edges"][0]["description"], INJECTION)
        self.assertEqual(graph["edges"][0]["source"], "source")
        self.assertEqual(graph["edges"][0]["target"], "target")
        self.assertEqual(graph["path"], ["source", "target"])

        for raw in (community_raw, graph_raw):
            self.assertIn("\\u003c/script\\u003e", raw)
            self.assertIn("\\u003cimg", raw)
            self.assertNotIn("<", raw)
            self.assertNotIn(">", raw)
            self.assertNotIn("&", raw)

    def test_dynamic_data_cannot_change_code_styles_metadata_or_csp(self):
        for app_id in self.builders:
            with self.subTest(app_id=app_id):
                first = self.builders[app_id](_fixtures()[app_id])
                second = self.builders[app_id]({})
                first_parser = _parse_document(first)
                second_parser = _parse_document(second)

                def fixed_parts(parser):
                    scripts = [
                        (attrs, data)
                        for tag, attrs, data in parser.data_by_open_tag
                        if tag == "script" and attrs.get("id") != "app-data"
                    ]
                    styles = [
                        (attrs, data)
                        for tag, attrs, data in parser.data_by_open_tag
                        if tag == "style"
                    ]
                    html_attrs = [
                        attrs for tag, attrs in parser.start_tags if tag == "html"
                    ]
                    metas = [attrs for tag, attrs in parser.start_tags if tag == "meta"]
                    titles = [
                        data
                        for tag, attrs, data in parser.data_by_open_tag
                        if tag == "title"
                    ]
                    return scripts, styles, html_attrs, metas, titles

                self.assertEqual(fixed_parts(first_parser), fixed_parts(second_parser))
                self.assertNotEqual(
                    _app_data(first)[0], _app_data(second)[0]
                ) if app_id != "test" else None

    def test_csp_is_deny_by_default_and_hashes_exact_emitted_assets(self):
        required = (
            "default-src 'none'",
            "base-uri 'none'",
            "object-src 'none'",
            "connect-src 'none'",
            "form-action 'none'",
            "frame-src 'none'",
            "worker-src 'none'",
            "font-src 'none'",
            "media-src 'none'",
            "manifest-src 'none'",
            "img-src data: blob:",
            "script-src-attr 'none'",
            "style-src-attr 'none'",
        )
        for app_id, builder in self.builders.items():
            with self.subTest(app_id=app_id):
                parser = _parse_document(builder(_fixtures()[app_id]))
                policies = [
                    attrs["content"]
                    for tag, attrs in parser.start_tags
                    if tag == "meta"
                    and attrs.get("http-equiv") == "Content-Security-Policy"
                ]
                self.assertEqual(len(policies), 1)
                policy = policies[0]
                for directive in required:
                    self.assertIn(directive, policy)
                self.assertIsNone(
                    re.search(r"unsafe-|https?:|wss?:|nonce-|\bself\b", policy, re.I)
                )
                emitted_scripts = [
                    data
                    for tag, attrs, data in parser.data_by_open_tag
                    if tag == "script" and attrs.get("data-asset")
                ]
                emitted_styles = [
                    data
                    for tag, attrs, data in parser.data_by_open_tag
                    if tag == "style" and attrs.get("data-asset")
                ]
                expected_script_hashes = [
                    "'sha256-"
                    + base64.b64encode(hashlib.sha256(value.encode()).digest()).decode()
                    + "'"
                    for value in emitted_scripts
                ]
                expected_style_hashes = [
                    "'sha256-"
                    + base64.b64encode(hashlib.sha256(value.encode()).digest()).decode()
                    + "'"
                    for value in emitted_styles
                ]
                script_clause = next(
                    part.strip()
                    for part in policy.split(";")
                    if part.strip().startswith("script-src ")
                )
                style_clause = next(
                    part.strip()
                    for part in policy.split(";")
                    if part.strip().startswith("style-src ")
                )
                self.assertEqual(script_clause.split()[1:], expected_script_hashes)
                self.assertEqual(style_clause.split()[1:], expected_style_hashes)

    def test_owned_assets_avoid_dynamic_markup_style_and_code_sinks(self):
        from importlib import resources

        root = resources.files("daem0nmcp.ui")
        forbidden = (
            "innerHTML",
            "insertAdjacentHTML",
            "document.write",
            "new Function",
            "eval(",
            ".style.",
            'setAttribute("style"',
            "setAttribute('style'",
        )
        for app_id, spec in self.rendering.APP_SPECS.items():
            for asset in spec.scripts:
                if asset == "d3.bundle.js":
                    continue
                source = root.joinpath("static", *asset.split("/")).read_text(
                    encoding="utf-8"
                )
                for token in forbidden:
                    with self.subTest(app_id=app_id, asset=asset, token=token):
                        self.assertNotIn(token, source)
                if asset.startswith("renderers/"):
                    self.assertIn("replaceChildren", source)


if __name__ == "__main__":
    unittest.main()
