from __future__ import annotations

import types
import unittest
from types import MappingProxyType
from typing import Any, get_args, get_origin

from pydantic import ValidationError

from daem0nmcp.api.v7.mapping import V6_TO_V7_MAPPINGS
from daem0nmcp.api.v7.policy import V7_TOOL_LEVELS
from daem0nmcp.api.v7.registry import PINNED_TOOL_NAMES, ManifestError
from daem0nmcp.api.v7.tools import V7_NATIVE_TOOL_NAMES

OPTIONAL_TOOLS = frozenset(
    {
        "memory_recall",
        "memory_recall_hierarchical",
        "context_compress",
        "memory_store_batch",
        "document_ingest_url",
        "memory_verify",
        "sandbox_execute_python",
        "code_index",
        "code_impact_analyze",
        "code_todos_scan",
        "code_todos_scan_and_store",
        "code_refactor_propose",
        "memory_related",
        "memory_chain_trace",
        "knowledge_graph_get",
        "knowledge_graph_render",
        "community_rebuild",
        "entity_backfill",
        "entity_evolution_trace",
        "memory_prune_preview",
        "memory_prune",
        "memory_duplicates_preview",
        "memory_duplicates_cleanup",
        "memory_compaction_preview",
        "memory_compact",
        "projection_rebuild",
        "workspace_export",
        "workspace_import",
        "workspace_consolidate",
        "workspace_consolidation_preview",
        "workspace_consolidate_and_archive_sources",
        "dream_duplicates_preview",
        "dream_duplicates_purge",
        "decision_simulate",
        "rule_evolution_analyze",
        "decision_debate",
    }
)

READ_ONLY_TOOLS = frozenset(
    {
        "session_brief",
        "memory_capture_list",
        "memory_preflight",
        "memory_recall",
        "system_health",
        "active_context_list",
        "context_triggers_match",
        "session_updates_get",
        "covenant_status",
        "memory_recall_file",
        "memory_recall_entity",
        "memory_recall_hierarchical",
        "memory_search_text",
        "rule_check",
        "context_compress",
        "memory_verify",
        "code_search",
        "code_impact_analyze",
        "code_todos_scan",
        "code_refactor_propose",
        "rule_list",
        "context_trigger_list",
        "memory_related",
        "memory_chain_trace",
        "knowledge_graph_get",
        "knowledge_graph_render",
        "knowledge_graph_stats",
        "community_list",
        "community_get",
        "entity_list",
        "entity_evolution_trace",
        "memory_versions_list",
        "memory_at_time_get",
        "memory_prune_preview",
        "memory_duplicates_preview",
        "memory_compaction_preview",
        "workspace_export",
        "workspace_links_list",
        "workspace_consolidation_preview",
        "dream_duplicates_preview",
        "decision_simulate",
        "rule_evolution_analyze",
    }
)

DESTRUCTIVE_TOOLS = frozenset(
    {
        "memory_unlink",
        "active_context_remove",
        "active_context_clear",
        "sandbox_execute_python",
        "context_trigger_delete",
        "memory_prune",
        "memory_archive_set",
        "memory_duplicates_cleanup",
        "memory_compact",
        "workspace_import",
        "workspace_unlink",
        "workspace_consolidate_and_archive_sources",
        "dream_duplicates_purge",
    }
)

OPEN_WORLD_TOOLS = frozenset({"document_ingest_url", "sandbox_execute_python"})

PINNED_INPUT_FIELDS = {
    "session_brief": {
        "workspace_id",
        "focus_areas",
        "warning_limit",
        "failure_limit",
    },
    "memory_preflight": {
        "workspace_id",
        "target_tool",
        "target_arguments",
        "description",
    },
    "memory_recall": {
        "workspace_id",
        "query",
        "limit",
        "candidate_limit",
        "categories",
        "tags",
        "record_ids",
        "linked_workspace_ids",
        "as_of_valid_time",
        "as_of_transaction_time",
        "include_invalidated",
        "include_archived",
        "token_budget",
        "rerank",
    },
    "memory_store": {
        "workspace_id",
        "record_type",
        "content",
        "rationale",
        "context",
        "tags",
        "relative_file_path",
        "happened_at",
        "procedure_steps",
        "idempotency_key",
        "preflight_token",
    },
    "memory_record_outcome": {
        "workspace_id",
        "record_id",
        "outcome_text",
        "worked",
        "happened_at",
        "idempotency_key",
    },
    "system_health": {"workspace_id", "include_components"},
}


async def _handler(**arguments: object) -> object:
    return arguments


def _handler_map() -> dict[str, object]:
    return dict.fromkeys(V7_TOOL_LEVELS, _handler)


def _contains_any(annotation: object) -> bool:
    if annotation is Any:
        return True
    origin = get_origin(annotation)
    if origin in {types.UnionType, list, set, frozenset, tuple, dict}:
        return any(_contains_any(argument) for argument in get_args(annotation))
    return any(_contains_any(argument) for argument in get_args(annotation))


def _schema_property_names(schema: dict[str, object]) -> set[str]:
    properties = schema.get("properties")
    assert isinstance(properties, dict)
    return set(properties)


class ToolManifestTests(unittest.TestCase):
    def test_active_context_list_issues_clear_selection_metadata(self) -> None:
        # The destructive clear token must bind the exact listed entry set.
        from daem0nmcp.api.v7.tools import TOOL_DATA_MODELS

        model = TOOL_DATA_MODELS["active_context_list"]
        self.assertIn("selection_token", model.model_fields)
        with self.assertRaises(ValidationError):
            model(items=[], next_cursor=None, truncated=False)
        page = model(
            items=[],
            next_cursor=None,
            truncated=False,
            selection_token="sel_0123456789abcdef01234567",
        )
        self.assertEqual(
            "sel_0123456789abcdef01234567",
            page.selection_token,
        )

    def test_public_input_model_map_builds_the_covenant_normalizer(self) -> None:
        # Catches preflight implementations reaching into a private model table.
        from daem0nmcp.api.v7.tools import (
            TOOL_INPUT_MODELS,
            build_argument_normalizer,
        )

        self.assertIsInstance(TOOL_INPUT_MODELS, MappingProxyType)
        self.assertEqual(set(TOOL_INPUT_MODELS), set(V7_TOOL_LEVELS))
        normalizer = build_argument_normalizer()
        self.assertEqual(normalizer.operations, frozenset(V7_TOOL_LEVELS))
        with self.assertRaises(TypeError):
            TOOL_INPUT_MODELS["commune"] = TOOL_INPUT_MODELS["session_brief"]

    def test_factory_rejects_missing_and_unexpected_handlers(self) -> None:
        # Catches fail-open startup when a manifest name and handler set drift.
        from daem0nmcp.api.v7.tools import build_tool_specs

        handlers = _handler_map()
        handlers.pop("memory_store")
        with self.assertRaisesRegex(ManifestError, "missing.*memory_store"):
            build_tool_specs(handlers)

        handlers = _handler_map()
        handlers["commune"] = _handler
        with self.assertRaisesRegex(ManifestError, "unexpected.*commune"):
            build_tool_specs(handlers)

    def test_manifest_is_exactly_the_policy_and_mapping_replacement_set(self) -> None:
        # Catches stale, missing, duplicate, and order-randomized tool entries.
        from daem0nmcp.api.v7.tools import build_tool_specs

        specs = build_tool_specs(_handler_map())
        names = [spec.name for spec in specs]
        mapped_names = {name for row in V6_TO_V7_MAPPINGS for name in row.new_tools}

        self.assertEqual(len(names), 75)
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(set(names), set(V7_TOOL_LEVELS))
        self.assertEqual(set(names), mapped_names | V7_NATIVE_TOOL_NAMES)
        self.assertEqual(names, sorted(names))
        self.assertEqual(
            {spec.name for spec in specs if spec.pinned}, PINNED_TOOL_NAMES
        )
        for spec in specs:
            with self.subTest(tool=spec.name):
                self.assertIs(spec.handler, _handler)
                self.assertIs(spec.covenant, V7_TOOL_LEVELS[spec.name])

    def test_task_modes_and_annotations_match_the_static_contract(self) -> None:
        # Catches advisory metadata that understates writes, destruction, or I/O.
        from daem0nmcp.api.v7.tools import build_tool_specs

        by_name = {spec.name: spec for spec in build_tool_specs(_handler_map())}
        for name, spec in by_name.items():
            with self.subTest(tool=name):
                self.assertEqual(
                    spec.task_mode,
                    "optional" if name in OPTIONAL_TOOLS else "forbidden",
                )
                self.assertEqual(
                    spec.annotations["readOnlyHint"], name in READ_ONLY_TOOLS
                )
                self.assertEqual(
                    spec.annotations["destructiveHint"], name in DESTRUCTIVE_TOOLS
                )
                self.assertEqual(
                    spec.annotations["openWorldHint"], name in OPEN_WORLD_TOOLS
                )
                self.assertEqual(
                    spec.annotations["idempotentHint"],
                    name != "sandbox_execute_python",
                )
                self.assertTrue(spec.category)
                self.assertTrue(spec.tags)
                self.assertIn(spec.category, spec.tags)

    def test_all_models_are_closed_typed_and_have_envelope_outputs(self) -> None:
        # Catches broad request bags, Any results, and non-envelope regressions.
        from daem0nmcp.api.v7.models import ApiResponse, WireModel
        from daem0nmcp.api.v7.tools import build_tool_specs

        for spec in build_tool_specs(_handler_map()):
            with self.subTest(tool=spec.name):
                self.assertTrue(issubclass(spec.input_model, WireModel))
                self.assertTrue(issubclass(spec.output_model, ApiResponse))
                self.assertFalse(
                    any(
                        _contains_any(field.annotation)
                        for field in spec.input_model.model_fields.values()
                    )
                )
                data_arguments = spec.output_model.__pydantic_generic_metadata__["args"]
                self.assertEqual(len(data_arguments), 1)
                data_model = data_arguments[0]
                self.assertFalse(
                    any(
                        _contains_any(field.annotation)
                        for field in data_model.model_fields.values()
                    )
                )

                input_schema = spec.input_schema
                output_schema = spec.output_schema
                self.assertIs(input_schema.get("additionalProperties"), False)
                self.assertEqual(output_schema.get("type"), "object")
                self.assertEqual(
                    _schema_property_names(output_schema),
                    {"api_version", "ok", "data", "error", "meta"},
                )
                encoded = str(input_schema).lower()
                self.assertNotIn("project_path", encoded)
                self.assertNotIn("'action'", encoded)

    def test_six_pinned_inputs_have_exact_top_level_parameters(self) -> None:
        # Catches nested request objects and accidental public signature drift.
        from daem0nmcp.api.v7.tools import build_tool_specs

        by_name = {spec.name: spec for spec in build_tool_specs(_handler_map())}
        for name, expected in PINNED_INPUT_FIELDS.items():
            with self.subTest(tool=name):
                self.assertEqual(
                    _schema_property_names(by_name[name].input_schema), expected
                )

    def test_pinned_defaults_bounds_and_cross_field_rules_are_strict(self) -> None:
        # Catches coercion, unsafe paths, unbounded recall, and procedure leakage.
        from daem0nmcp.api.v7.tools import (
            MemoryRecallInput,
            MemoryStoreInput,
            SessionBriefInput,
        )

        workspace_id = "ws_" + "a" * 24
        brief = SessionBriefInput(workspace_id=workspace_id)
        self.assertEqual(brief.focus_areas, [])
        self.assertEqual(brief.warning_limit, 10)
        self.assertEqual(brief.failure_limit, 10)
        with self.assertRaises(ValidationError):
            SessionBriefInput(workspace_id=workspace_id, warning_limit="10")
        with self.assertRaises(ValidationError):
            SessionBriefInput(workspace_id=workspace_id, focus_areas=["x"] * 11)

        with self.assertRaises(ValidationError):
            MemoryRecallInput(
                workspace_id=workspace_id,
                query="find it",
                limit=20,
                candidate_limit=10,
            )
        with self.assertRaises(ValidationError):
            MemoryRecallInput(
                workspace_id=workspace_id,
                query="find it",
                as_of_valid_time="2026-08-08T12:00:00",
            )

        common = {
            "workspace_id": workspace_id,
            "record_type": "decision",
            "content": "Use the event store.",
            "idempotency_key": "decision-0001",
            "preflight_token": "capability-token-0001",
        }
        with self.assertRaises(ValidationError):
            MemoryStoreInput(**common, relative_file_path="../secret")
        with self.assertRaises(ValidationError):
            MemoryStoreInput(**common, procedure_steps=["do it"])
        procedure = MemoryStoreInput(
            **{**common, "record_type": "procedure"},
            procedure_steps=["do it"],
        )
        self.assertEqual(procedure.procedure_steps, ["do it"])
        with self.assertRaises(ValidationError):
            MemoryStoreInput(**common, record_id="mem_" + "1" * 64)

    def test_exactly_one_selector_models_reject_zero_or_two_selectors(self) -> None:
        # Catches ambiguous entity/code lookups that could cross object scopes.
        from daem0nmcp.api.v7.tools import (
            CodeImpactAnalyzeInput,
            EntityEvolutionTraceInput,
            MemoryRecallEntityInput,
        )

        workspace_id = "ws_" + "a" * 24
        pairs = (
            (MemoryRecallEntityInput, "entity_id", "ent_" + "b" * 64, "entity_name"),
            (
                CodeImpactAnalyzeInput,
                "code_entity_id",
                "code_" + "c" * 64,
                "qualified_name",
            ),
            (EntityEvolutionTraceInput, "entity_id", "ent_" + "d" * 64, "entity_name"),
        )
        for model, id_field, identifier, name_field in pairs:
            with self.subTest(model=model.__name__):
                with self.assertRaises(ValidationError):
                    model(workspace_id=workspace_id)
                with self.assertRaises(ValidationError):
                    model(
                        workspace_id=workspace_id,
                        **{id_field: identifier, name_field: "name"},
                    )
                self.assertIsNotNone(
                    model(workspace_id=workspace_id, **{id_field: identifier})
                )

    def test_json_arrays_validate_as_bounded_set_inputs(self) -> None:
        # Catches strict-mode rejection of JSON's only representation of a set.
        from daem0nmcp.api.v7.tools import MemoryRecallInput

        workspace_id = "ws_" + "a" * 24
        parsed = MemoryRecallInput.model_validate_json(
            '{"workspace_id":"'
            + workspace_id
            + '","query":"event store","categories":["decision"]}'
        )
        self.assertEqual(parsed.categories, {"decision"})
        with self.assertRaises(ValidationError):
            MemoryRecallInput.model_validate(
                {
                    "workspace_id": workspace_id,
                    "query": "event store",
                    "categories": ["decision", "decision"],
                }
            )
        categories_schema = MemoryRecallInput.model_json_schema()["properties"][
            "categories"
        ]["anyOf"][0]
        self.assertEqual(categories_schema["maxItems"], 6)
        self.assertNotIn("maxLength", categories_schema)

        first = MemoryRecallInput.model_validate(
            {
                "workspace_id": workspace_id,
                "query": "event store",
                "categories": ["warning", "decision", "pattern"],
            }
        )
        second = MemoryRecallInput.model_validate(
            {
                "workspace_id": workspace_id,
                "query": "event store",
                "categories": ["pattern", "warning", "decision"],
            }
        )
        expected_order = ["decision", "pattern", "warning"]
        self.assertEqual(first.model_dump(mode="json")["categories"], expected_order)
        self.assertEqual(second.model_dump(mode="json")["categories"], expected_order)

    def test_rule_patch_requires_an_effective_change(self) -> None:
        # Catches no-op nested patches that would consume a one-use capability.
        from daem0nmcp.api.v7.tools import RulePatch

        with self.assertRaises(ValidationError):
            RulePatch()
        with self.assertRaises(ValidationError):
            RulePatch(enabled=None)
        self.assertFalse(RulePatch(enabled=False).enabled)

    def test_split_consolidation_results_and_compaction_receipt_are_truthful(
        self,
    ) -> None:
        # Catches optional fields that blur static destructive/non-destructive results.
        from daem0nmcp.api.v7.models import DestructiveMutationReceipt
        from daem0nmcp.api.v7.tools import build_tool_specs

        by_name = {spec.name: spec for spec in build_tool_specs(_handler_map())}
        normal = by_name[
            "workspace_consolidate"
        ].output_model.__pydantic_generic_metadata__["args"][0]
        archival = by_name[
            "workspace_consolidate_and_archive_sources"
        ].output_model.__pydantic_generic_metadata__["args"][0]
        compact = by_name["memory_compact"].output_model.__pydantic_generic_metadata__[
            "args"
        ][0]

        self.assertEqual(set(normal.model_fields), {"sources", "imported", "event_ids"})
        self.assertEqual(
            set(archival.model_fields),
            {"sources", "imported", "archived", "event_ids"},
        )
        self.assertIs(
            compact.model_fields["receipt"].annotation,
            DestructiveMutationReceipt,
        )


if __name__ == "__main__":
    unittest.main()
