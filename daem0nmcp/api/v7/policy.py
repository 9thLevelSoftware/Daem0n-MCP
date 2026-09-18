"""Immutable Task 9 Covenant policy and typed argument normalization."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ValidationError

from ...covenant import (
    ArgumentNormalizationError,
    CovenantLevel,
    UnknownCovenantOperation,
)


def _levels(level: CovenantLevel, *tool_names: str) -> dict[str, CovenantLevel]:
    return dict.fromkeys(tool_names, level)


V7_TOOL_LEVELS: Mapping[str, CovenantLevel] = MappingProxyType(
    {
        **_levels(
            CovenantLevel.EXEMPT,
            "session_brief",
            "system_health",
            "covenant_status",
        ),
        **_levels(
            CovenantLevel.COMMUNION,
            "memory_preflight",
            "memory_recall",
            "memory_record_outcome",
            "active_context_list",
            "context_triggers_match",
            "session_updates_get",
            "memory_recall_file",
            "memory_recall_entity",
            "memory_recall_hierarchical",
            "memory_search_text",
            "rule_check",
            "context_compress",
            "memory_verify",
            "code_index",
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
            "projection_rebuild",
            "workspace_export",
            "workspace_links_list",
            "workspace_consolidation_preview",
            "dream_duplicates_preview",
            "decision_simulate",
            "rule_evolution_analyze",
            "edit_preflight",
            "memory_capture_list",
        ),
        **_levels(
            CovenantLevel.COUNSEL,
            "memory_store",
            "memory_store_batch",
            "memory_link",
            "memory_pin_set",
            "active_context_add",
            "document_ingest_url",
            "code_todos_scan_and_store",
            "rule_create",
            "rule_update",
            "context_trigger_create",
            "community_rebuild",
            "entity_backfill",
            "workspace_link",
            "workspace_consolidate",
            "decision_debate",
            "memory_capture_promote",
        ),
        **_levels(
            CovenantLevel.DESTRUCTIVE,
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
        ),
    }
)


class V7CovenantPolicy:
    """One immutable, argument-insensitive policy keyed by v7 tool name."""

    def __init__(self, levels: Mapping[str, CovenantLevel] = V7_TOOL_LEVELS) -> None:
        copied = dict(levels)
        if not copied or any(
            not isinstance(name, str) or not isinstance(level, CovenantLevel)
            for name, level in copied.items()
        ):
            raise ValueError("v7 policy entries must be named Covenant levels")
        self._levels = MappingProxyType(copied)
        self.operations = frozenset(copied)

    @property
    def levels(self) -> Mapping[str, CovenantLevel]:
        return self._levels

    def resolve(
        self, operation: str, arguments: Mapping[str, Any] | None = None
    ) -> CovenantLevel:
        del arguments
        try:
            return self._levels[operation]
        except KeyError as exc:
            raise UnknownCovenantOperation(operation) from exc


V7_COVENANT_POLICY = V7CovenantPolicy()
V7_RESOURCE_LEVEL = CovenantLevel.COMMUNION


class V7ArgumentNormalizer:
    """Validate one v7 input model and return capability-bound arguments."""

    _EXCLUDED = frozenset({"workspace_id", "preflight_token"})
    _PREFLIGHT_PLACEHOLDER = "capability-validation-placeholder"

    def __init__(self, input_models: Mapping[str, type[BaseModel]]) -> None:
        copied = dict(input_models)
        if not copied or any(
            not isinstance(name, str)
            or not isinstance(model, type)
            or not issubclass(model, BaseModel)
            for name, model in copied.items()
        ):
            raise ValueError("v7 input models must be Pydantic model classes")
        self._input_models = MappingProxyType(copied)

    @property
    def operations(self) -> frozenset[str]:
        return frozenset(self._input_models)

    def __call__(
        self,
        operation: str,
        arguments: Mapping[str, Any] | None,
        workspace: str,
    ) -> dict[str, Any]:
        del workspace
        try:
            model = self._input_models[operation]
        except KeyError as exc:
            raise UnknownCovenantOperation(operation) from exc
        supplied = dict(arguments or {})
        fields = model.model_fields
        if "preflight_token" in fields and "preflight_token" not in supplied:
            supplied["preflight_token"] = self._PREFLIGHT_PLACEHOLDER
        try:
            validated = model.model_validate(supplied)
            normalized = validated.model_dump(mode="json", exclude=set(self._EXCLUDED))
        except (TypeError, ValueError, ValidationError) as exc:
            raise ArgumentNormalizationError(
                "v7 arguments do not match the target tool schema"
            ) from exc
        return normalized


__all__ = [
    "V7ArgumentNormalizer",
    "V7CovenantPolicy",
    "V7_COVENANT_POLICY",
    "V7_RESOURCE_LEVEL",
    "V7_TOOL_LEVELS",
]
