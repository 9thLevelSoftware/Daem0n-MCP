"""Search app document and presentation-model compatibility tests."""

import json
import unittest
from html.parser import HTMLParser

from daem0nmcp.ui.fallback import format_search_results, format_with_ui_hint
from daem0nmcp.ui.resources import _build_search_ui


class _DataBlock(HTMLParser):
    def __init__(self):
        super().__init__()
        self.capture = False
        self.value = ""

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "script" and values.get("id") == "app-data":
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


class SearchUIContractTests(unittest.TestCase):
    def test_document_contains_normalized_search_model_not_server_cards(self):
        payload = {
            "topic": "python",
            "decisions": [
                {
                    "id": 123,
                    "content": "Use Python 3.11",
                    "relevance": 0.95,
                    "semantic_match": 0.9,
                    "recency_weight": 0.8,
                    "created_at": "2026-01-28T00:00:00Z",
                    "tags": ["python"],
                    "worked": None,
                }
            ],
            "warnings": [{"id": 2, "content": "Avoid Python 2", "relevance": 75}],
            "patterns": [],
            "learnings": [],
            "total_count": 8,
            "offset": 0,
            "limit": 10,
            "has_more": True,
        }
        document = _build_search_ui(payload)
        model = app_data(document)
        self.assertEqual(model["topic"], "python")
        self.assertEqual(model["decisions"][0]["id"], 123)
        self.assertEqual(model["decisions"][0]["relevance"], 0.95)
        self.assertEqual(model["warnings"][0]["relevance"], 0.75)
        self.assertEqual(model["total_count"], 8)
        self.assertTrue(model["has_more"])
        self.assertNotIn('data-memory-id="123"', document)
        self.assertNotIn(
            "Use Python 3.11", document.replace(parser_value(document), "")
        )

    def test_missing_fields_produce_a_bounded_empty_view_model(self):
        model = app_data(_build_search_ui({"topic": "minimal"}))
        self.assertEqual(model["topic"], "minimal")
        self.assertEqual(model["decisions"], [])
        self.assertEqual(model["warnings"], [])
        self.assertEqual(model["patterns"], [])
        self.assertEqual(model["learnings"], [])
        self.assertEqual(model["limit"], 10)

    def test_fixed_document_structure_and_assets_are_present(self):
        document = _build_search_ui({})
        self.assertTrue(document.startswith("<!doctype html>"))
        self.assertIn('<html lang="en" data-daem0n-app="search">', document)
        self.assertIn("<title>Daem0n Search Results</title>", document)
        self.assertIn('data-asset="daemon.css"', document)
        self.assertIn('data-asset="runtime.js"', document)
        self.assertIn('data-asset="renderers/search.js"', document)
        self.assertIn("default-src 'none'", document)

    def test_text_fallback_and_wrapper_semantics_are_preserved(self):
        results = [
            {"category": "decision", "content": "Use React", "score": 0.95},
            {"category": "warning", "content": "Avoid jQuery", "score": 0.82},
        ]
        text = format_search_results("framework", results, total_count=2)
        wrapped = format_with_ui_hint({"count": 2}, "ui://daem0n/search", text)
        self.assertIn("Search Results for: framework", wrapped["text"])
        self.assertIn("Use React", wrapped["text"])
        self.assertEqual(wrapped["count"], 2)


def parser_value(document):
    parser = _DataBlock()
    parser.feed(document)
    return parser.value


if __name__ == "__main__":
    unittest.main()
