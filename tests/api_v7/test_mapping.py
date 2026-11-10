"""Contract tests for the authoritative v6-to-v7 tool mapping."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

from daem0nmcp.api.v7.mapping import (
    V6_TO_V7_MAPPINGS,
    MappingCoverageError,
    current_v6_operations,
    mapping_document,
    render_mapping_json,
    validate_mapping,
)

_ROOT = Path(__file__).resolve().parents[2]
_GENERATED_PATH = _ROOT / "docs" / "v6-to-v7-tools.json"
_GENERATOR_PATH = _ROOT / "scripts" / "generate_v7_tool_mapping.py"


class MappingCoverageTests(unittest.TestCase):
    """Catch missing, stale, duplicated, or ambiguous migration routes."""

    def test_mapping_covers_every_live_v6_operation_without_stale_rows(self) -> None:
        validate_mapping()

        mapped = {entry.old_operation for entry in V6_TO_V7_MAPPINGS}
        self.assertEqual(mapped, current_v6_operations())
        self.assertEqual(len(mapped), 64)

    def test_conditional_operations_have_the_exact_two_static_branches(self) -> None:
        expected = {
            "understand.todos": {
                (("auto_remember", False), "code_todos_scan", "COMMUNION"),
                (("auto_remember", True), "code_todos_scan_and_store", "COUNSEL"),
            },
            "explore.graph": {
                (("format", "json"), "knowledge_graph_get", "COMMUNION"),
                (("format", "mermaid"), "knowledge_graph_render", "COMMUNION"),
            },
            "maintain.prune": {
                (("dry_run", True), "memory_prune_preview", "COMMUNION"),
                (("dry_run", False), "memory_prune", "DESTRUCTIVE"),
            },
            "maintain.cleanup": {
                (("dry_run", True), "memory_duplicates_preview", "COMMUNION"),
                (
                    ("dry_run", False),
                    "memory_duplicates_cleanup",
                    "DESTRUCTIVE",
                ),
            },
            "maintain.compact": {
                (("dry_run", True), "memory_compaction_preview", "COMMUNION"),
                (("dry_run", False), "memory_compact", "DESTRUCTIVE"),
            },
            "maintain.consolidate": {
                (
                    ("archive_sources", False),
                    "workspace_consolidate",
                    "COUNSEL",
                ),
                (
                    ("archive_sources", True),
                    "workspace_consolidate_and_archive_sources",
                    "DESTRUCTIVE",
                ),
            },
            "maintain.purge_dream_spam": {
                (("dry_run", True), "dream_duplicates_preview", "COMMUNION"),
                (("dry_run", False), "dream_duplicates_purge", "DESTRUCTIVE"),
            },
        }

        actual: dict[str, set[tuple[tuple[str, object], str, str]]] = {}
        for entry in V6_TO_V7_MAPPINGS:
            if not entry.conditional_branches:
                continue
            actual[entry.old_operation] = {
                (
                    branch.condition,
                    branch.new_tool,
                    branch.v7_policy,
                )
                for branch in entry.conditional_branches
            }

        self.assertEqual(actual, expected)

    def test_validation_reports_unmapped_and_stale_live_actions(self) -> None:
        live = current_v6_operations()

        with self.assertRaisesRegex(
            MappingCoverageError, r"unmapped v6 operations: consult\.new_action"
        ):
            validate_mapping(current_operations=live | {"consult.new_action"})

        with self.assertRaisesRegex(
            MappingCoverageError, r"stale mapping operations: consult\.recall"
        ):
            validate_mapping(current_operations=live - {"consult.recall"})

    def test_mapping_is_immutable_and_each_row_has_migration_guidance(self) -> None:
        entry = V6_TO_V7_MAPPINGS[0]
        with self.assertRaises(FrozenInstanceError):
            entry.old_operation = "changed"  # type: ignore[misc]

        for entry in V6_TO_V7_MAPPINGS:
            self.assertTrue(entry.new_tools)
            self.assertIsInstance(entry.new_tools, tuple)
            self.assertIsInstance(entry.removed_parameters, tuple)
            self.assertTrue(entry.policy_change)
            self.assertEqual(len(entry.replacement_examples), len(entry.new_tools))
            self.assertTrue(
                all(example.strip() for example in entry.replacement_examples)
            )

    def test_debate_preflight_field_is_reissued_not_removed(self) -> None:
        debate = next(
            entry
            for entry in V6_TO_V7_MAPPINGS
            if entry.old_operation == "debate_internal"
        )

        self.assertNotIn("preflight_token", debate.removed_parameters)


class MappingDocumentTests(unittest.TestCase):
    """Catch nondeterministic or out-of-date generated migration docs."""

    def test_document_has_the_published_machine_readable_shape(self) -> None:
        document = mapping_document()

        self.assertEqual(document["api_version"], "7")
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(len(document["mappings"]), 64)
        required = {
            "old_operation",
            "new_tools",
            "removed_parameters",
            "policy_change",
            "replacement_examples",
            "conditional_branches",
        }
        for row in document["mappings"]:
            self.assertEqual(set(row), required)

    def test_renderer_is_sorted_compact_canonical_json(self) -> None:
        first = render_mapping_json()
        second = render_mapping_json()
        parsed = json.loads(first)
        expected = json.dumps(
            parsed,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )

        self.assertEqual(first, second)
        self.assertEqual(first, expected)
        self.assertFalse(first.endswith("\n"))

    def test_renderer_normalizes_unicode_like_the_v7_canonical_codec(self) -> None:
        mappings = list(V6_TO_V7_MAPPINGS)
        original = mappings[0]
        mappings[0] = replace(
            original,
            replacement_examples=(
                original.replacement_examples[0] + " # caf\u0065\u0301",
            ),
        )

        rendered = render_mapping_json(mappings)

        self.assertIn("café", rendered)
        self.assertNotIn("cafe\u0301", rendered)

    def test_checked_in_document_matches_the_authoritative_mapping(self) -> None:
        self.assertEqual(
            _GENERATED_PATH.read_text(encoding="utf-8"), render_mapping_json()
        )

    def test_generator_is_idempotent_and_check_detects_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "mapping.json"
            generate = subprocess.run(
                [sys.executable, str(_GENERATOR_PATH), "--output", str(output)],
                cwd=_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(generate.returncode, 0, generate.stderr)
            first = output.read_bytes()
            self.assertEqual(first, render_mapping_json().encode("utf-8"))

            generate_again = subprocess.run(
                [sys.executable, str(_GENERATOR_PATH), "--output", str(output)],
                cwd=_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(generate_again.returncode, 0, generate_again.stderr)
            self.assertEqual(output.read_bytes(), first)

            output.write_text("{}", encoding="utf-8")
            check = subprocess.run(
                [
                    sys.executable,
                    str(_GENERATOR_PATH),
                    "--check",
                    "--output",
                    str(output),
                ],
                cwd=_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(check.returncode, 1)
            self.assertIn("out of date", check.stderr)


if __name__ == "__main__":
    unittest.main()
