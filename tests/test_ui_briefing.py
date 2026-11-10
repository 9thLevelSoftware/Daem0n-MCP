"""Briefing app document and presentation-model compatibility tests."""

import json
import unittest
from html.parser import HTMLParser

from daem0nmcp.ui.fallback import format_briefing_text, format_with_ui_hint
from daem0nmcp.ui.resources import _build_briefing_ui


class _DataBlock(HTMLParser):
    def __init__(self):
        super().__init__()
        self.capture = False
        self.value = ""

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("id") == "app-data":
            self.capture = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.capture = False

    def handle_data(self, data):
        if self.capture:
            self.value += data


def app_data(document):
    parser = _DataBlock()
    parser.feed(document)
    return json.loads(parser.value)


def sample_briefing():
    return {
        "status": "ready",
        "statistics": {
            "total_memories": 42,
            "by_category": {"decision": 15, "warning": 8, "pattern": 12, "learning": 7},
            "outcome_rates": {"success_rate": 0.73},
        },
        "recent_decisions": [
            {
                "content": "Use PostgreSQL",
                "worked": True,
                "created_at": "2026-01-15T10:30:00Z",
            },
            {"content": "Try Redis", "worked": False},
        ],
        "active_warnings": [{"content": "Rate limit risk", "severity": "high"}],
        "failed_approaches": [{"content": "SQLite concurrency"}],
        "git_changes": {
            "total": 2,
            "files": [{"status": "M", "path": "src/config.py"}],
        },
        "focus_areas": [{"topic": "authentication"}],
        "message": "One warning requires attention.",
    }


class BriefingUIContractTests(unittest.TestCase):
    def test_document_contains_closed_normalized_briefing_model(self):
        document = _build_briefing_ui(sample_briefing())
        model = app_data(document)
        self.assertEqual(model["status"], "ready")
        self.assertEqual(model["statistics"]["total_memories"], 42)
        self.assertEqual(model["statistics"]["outcome_rates"]["success_rate"], 0.73)
        self.assertEqual(model["recent_decisions"][0]["content"], "Use PostgreSQL")
        self.assertEqual(model["active_warnings"][0]["severity"], "high")
        self.assertEqual(model["git_changes"]["files"][0]["path"], "src/config.py")
        self.assertNotIn("project_path", model)

    def test_real_briefing_aliases_are_projected_without_extra_fields(self):
        model = app_data(
            _build_briefing_ui(
                {
                    "recent_decisions": [{"summary": "Lean decision", "id": 9}],
                    "active_warnings": [{"summary": "Lean warning", "id": 10}],
                    "failed_approaches": [
                        {"summary": "Lean failure", "outcome": "bad"}
                    ],
                    "git_changes": {
                        "uncommitted_changes": [{"status": "M", "file": "src/a.py"}]
                    },
                    "focus_areas": {"security": {"found": 2}},
                    "bootstrap": {"project_path": "private"},
                }
            )
        )
        self.assertEqual(model["recent_decisions"][0]["content"], "Lean decision")
        self.assertEqual(model["active_warnings"][0]["content"], "Lean warning")
        self.assertEqual(model["failed_approaches"][0]["content"], "Lean failure")
        self.assertEqual(
            model["git_changes"]["files"], [{"path": "src/a.py", "status": "M"}]
        )
        self.assertEqual(model["focus_areas"], [{"topic": "security"}])
        self.assertNotIn("bootstrap", model)

    def test_fixed_document_structure_and_assets_are_present(self):
        document = _build_briefing_ui({})
        self.assertIn('data-daem0n-app="briefing"', document)
        self.assertIn("<title>Daem0n Session Briefing</title>", document)
        self.assertIn('data-asset="messenger.js"', document)
        self.assertIn('data-asset="renderers/briefing.js"', document)
        self.assertIn("Content-Security-Policy", document)

    def test_text_fallback_and_wrapper_semantics_are_preserved(self):
        payload = sample_briefing()
        text = format_briefing_text(payload)
        wrapped = format_with_ui_hint(payload, "ui://daem0n/briefing", text)
        self.assertEqual(wrapped["text"], text)
        self.assertIn("Session Briefing", text)
        self.assertIn("Total Memories: 42", text)
        self.assertEqual(wrapped["status"], "ready")


if __name__ == "__main__":
    unittest.main()
