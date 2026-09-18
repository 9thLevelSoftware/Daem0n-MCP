"""Closed, bounded presentation-schema tests for MCP app payloads."""

import copy
import math
import unittest


class UIPayloadProjectionTests(unittest.TestCase):
    def setUp(self):
        from daem0nmcp.ui import payloads

        self.payloads = payloads

    def test_rejects_unknown_app_and_non_object_roots_without_echoing_input(self):
        with self.assertRaisesRegex(
            self.payloads.InvalidAppPayload, "invalid app payload"
        ):
            self.payloads.normalize_app_payload("secret-app", {"password": "hunter2"})
        for value in (None, [], "object", 1):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    self.payloads.InvalidAppPayload, "invalid app payload"
                ),
            ):
                self.payloads.normalize_app_payload("search", value)

    def test_test_projection_is_empty_and_drops_secrets(self):
        self.assertEqual(
            self.payloads.normalize_app_payload(
                "test", {"token": "secret", "project_path": "C:/private"}
            ),
            {},
        )

    def test_search_projection_is_closed_bounded_and_normalized(self):
        too_long = "x" * (self.payloads.MAX_CONTENT_CHARS + 20)
        data = {
            "topic": "t" * (self.payloads.MAX_LABEL_CHARS + 2),
            "decisions": [
                {
                    "id": 1,
                    "content": too_long,
                    "relevance": 95,
                    "semantic_match": -5,
                    "recency_weight": math.inf,
                    "created_at": "2026-08-08T12:30:00Z",
                    "tags": ["z" * 200] * 40,
                    "worked": True,
                    "token": "secret",
                },
                {"id": True, "content": 7, "relevance": float("nan")},
            ],
            "warnings": [{"id": "safe:id", "content": "warning", "worked": "yes"}],
            "patterns": [],
            "learnings": [],
            "total_count": 10**30,
            "offset": -1,
            "limit": 0,
            "has_more": 1,
            "authorization": "Bearer secret",
        }
        before = copy.deepcopy(data)

        result = self.payloads.normalize_app_payload("search", data)

        self.assertEqual(data, before)
        self.assertEqual(len(result["topic"]), self.payloads.MAX_LABEL_CHARS)
        first = result["decisions"][0]
        self.assertEqual(len(first["content"]), self.payloads.MAX_CONTENT_CHARS)
        self.assertEqual(first["relevance"], 0.95)
        self.assertEqual(first["semantic_match"], 0.0)
        self.assertEqual(first["recency_weight"], 1.0)
        self.assertEqual(first["created_at"], "2026-08-08T12:30:00Z")
        self.assertEqual(len(first["tags"]), self.payloads.MAX_TAGS)
        self.assertEqual(len(first["tags"][0]), self.payloads.MAX_TAG_CHARS)
        self.assertIsNone(result["decisions"][1]["id"])
        self.assertEqual(result["decisions"][1]["content"], "")
        self.assertEqual(result["decisions"][1]["relevance"], 0.0)
        self.assertIsNone(result["warnings"][0]["worked"])
        self.assertEqual(result["total_count"], self.payloads.JS_MAX_SAFE_INTEGER)
        self.assertEqual(result["offset"], 0)
        self.assertEqual(result["limit"], 1)
        self.assertFalse(result["has_more"])
        self.assertNotIn("authorization", result)
        self.assertNotIn("token", first)

    def test_search_slices_each_category_before_projection(self):
        items = [{"id": index + 1, "content": str(index)} for index in range(150)]
        result = self.payloads.normalize_app_payload(
            "search",
            {
                "decisions": items,
                "warnings": items,
                "patterns": items,
                "learnings": items,
            },
        )
        for category in ("decisions", "warnings", "patterns", "learnings"):
            self.assertEqual(len(result[category]), 100)

    def test_briefing_projection_uses_fixed_enums_dates_and_limits(self):
        data = {
            "status": "<script>",
            "statistics": {
                "total_memories": True,
                "by_category": {
                    "decision": 2,
                    "warning": -2,
                    "pattern": float("inf"),
                    "learning": "4",
                },
                "outcome_rates": {"success_rate": 78},
            },
            "recent_decisions": [
                {"content": "ok", "worked": False, "created_at": "not-a-date"}
            ]
            * 30,
            "active_warnings": [{"content": "w", "severity": "critical"}],
            "failed_approaches": [{"content": "f"}],
            "git_changes": {
                "total": 3,
                "files": [
                    {"status": "A", "path": "src/a.py"},
                    {"status": "renamed", "path": "src/b.py"},
                ],
            },
            "focus_areas": [{"topic": "security"}],
            "message": "hello",
            "cookie": "secret",
        }
        result = self.payloads.normalize_app_payload("briefing", data)
        self.assertEqual(result["status"], "neutral")
        self.assertEqual(result["statistics"]["total_memories"], 0)
        self.assertEqual(
            result["statistics"]["by_category"],
            {
                "decision": 2,
                "warning": 0,
                "pattern": 0,
                "learning": 0,
            },
        )
        self.assertEqual(result["statistics"]["outcome_rates"]["success_rate"], 0.78)
        self.assertEqual(len(result["recent_decisions"]), 20)
        self.assertEqual(result["recent_decisions"][0]["created_at"], "")
        self.assertEqual(result["active_warnings"][0]["severity"], "neutral")
        self.assertEqual(result["git_changes"]["files"][1]["status"], "?")
        self.assertNotIn("cookie", result)

    def test_covenant_projection_never_admits_a_capability(self):
        result = self.payloads.normalize_app_payload(
            "covenant",
            {
                "phase": "admin",
                "phase_label": "Label",
                "phase_description": "Description",
                "preflight": {
                    "status": "valid",
                    "expires_at": "2026-08-08T12:30:00+00:00",
                    "remaining_seconds": 99_999,
                    "token": "secret",
                },
                "preflight_token": "secret",
                "can_mutate": True,
                "message": "Message",
            },
        )
        self.assertEqual(result["phase"], "unknown")
        self.assertEqual(result["preflight"]["status"], "valid")
        self.assertEqual(result["preflight"]["remaining_seconds"], 86_400)
        self.assertNotIn("token", result["preflight"])
        self.assertNotIn("preflight_token", result)

    def test_community_projection_preserves_typed_ids_and_breaks_bad_hierarchy(self):
        result = self.payloads.normalize_app_payload(
            "community",
            {
                "count": 99,
                "communities": [
                    {"id": 1, "parent_community_id": None, "name": "int", "level": 0},
                    {"id": "1", "parent_community_id": 1, "name": "string", "level": 1},
                    {"id": 1, "name": "duplicate"},
                    {"id": 2, "parent_community_id": 99, "name": "orphan"},
                    {"id": 3, "parent_community_id": 3, "name": "self"},
                    {"id": 4, "parent_community_id": 5, "name": "cycle-a"},
                    {"id": 5, "parent_community_id": 4, "name": "cycle-b"},
                    {"id": "bad id", "name": "invalid"},
                ],
                "path": [
                    {"id": 1, "name": "root"},
                    {"id": "missing", "name": "not a community"},
                    {"id": "bad id", "name": "unsafe"},
                ],
            },
        )
        self.assertEqual(
            [item["id"] for item in result["communities"]], [1, "1", 2, 3, 4, 5]
        )
        parents = {
            f"{type(item['id']).__name__}:{item['id']}": item["parent_community_id"]
            for item in result["communities"]
        }
        self.assertEqual(parents["str:1"], 1)
        self.assertIsNone(parents["int:2"])
        self.assertIsNone(parents["int:3"])
        self.assertTrue(parents["int:4"] is None or parents["int:5"] is None)
        self.assertEqual(
            result["path"],
            [
                {"id": 1, "name": "root"},
                {"id": "missing", "name": "not a community"},
            ],
        )

    def test_graph_projection_drops_invalid_nodes_edges_and_path_members(self):
        source = {
            "topic": "graph",
            "nodes": [
                {"id": 1, "content": "one", "category": "decision", "x": 999},
                {"id": "1", "content": "string", "category": "evil"},
                {"id": 1, "content": "duplicate"},
                {"id": True, "content": "invalid"},
            ],
            "edges": [
                {
                    "source": 1,
                    "target": "1",
                    "relationship": "led_to",
                    "confidence": 75,
                },
                {"source": "1", "target": 1, "relationship": "evil", "confidence": -1},
                {"source": 1, "target": 99, "relationship": "relates_to"},
            ],
            "path": [1, "1", 99, True],
            "password": "secret",
        }
        before = copy.deepcopy(source)
        result = self.payloads.normalize_app_payload("graph", source)
        self.assertEqual(source, before)
        self.assertEqual([node["id"] for node in result["nodes"]], [1, "1"])
        self.assertNotIn("x", result["nodes"][0])
        self.assertEqual(result["nodes"][1]["category"], "default")
        self.assertEqual(len(result["edges"]), 2)
        self.assertEqual(result["edges"][1]["relationship"], "relates_to")
        self.assertEqual(result["edges"][0]["confidence"], 0.75)
        self.assertEqual(result["edges"][1]["confidence"], 0.0)
        self.assertEqual(result["path"], [1, "1"])
        self.assertNotIn("password", result)

    def test_all_projectors_emit_strict_json_and_drop_sensitive_names(self):
        import json

        sensitive = {
            "token": "x",
            "preflight_token": "x",
            "authorization": "x",
            "cookie": "x",
            "api_key": "x",
            "password": "x",
            "credential": "x",
            "project_path": "x",
        }
        for app_id in self.payloads.APP_IDS:
            with self.subTest(app_id=app_id):
                normalized = self.payloads.normalize_app_payload(app_id, sensitive)
                encoded = json.dumps(normalized, allow_nan=False)
                for name in sensitive:
                    self.assertNotIn(name, encoded)

    def test_lone_surrogates_are_dropped_from_text_fields(self):
        result = self.payloads.normalize_app_payload(
            "search", {"topic": "\ud800", "decisions": [{"content": "\udfff"}]}
        )
        self.assertEqual(result["topic"], "")
        self.assertEqual(result["decisions"][0]["content"], "")

    def test_enormous_finite_ratio_clamps_without_float_overflow(self):
        result = self.payloads.normalize_app_payload(
            "search",
            {
                "decisions": [
                    {"content": "large", "relevance": 10**1000},
                    {"content": "small", "relevance": -(10**1000)},
                ]
            },
        )
        self.assertEqual(result["decisions"][0]["relevance"], 1.0)
        self.assertEqual(result["decisions"][1]["relevance"], 0.0)


if __name__ == "__main__":
    unittest.main()
