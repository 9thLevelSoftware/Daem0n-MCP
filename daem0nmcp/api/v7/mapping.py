"""Authoritative, immutable mapping from the v6 wire surface to v7 tools.

This module deliberately has no FastMCP or database dependency.  It is used by
the server conformance tests and by the documentation generator, so drift in a
legacy dispatcher causes generation to fail instead of silently publishing an
incomplete migration guide.
"""

from __future__ import annotations

import importlib
import json
import unicodedata
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

_WORKFLOW_NAMES = (
    "commune",
    "consult",
    "inscribe",
    "reflect",
    "understand",
    "govern",
    "explore",
    "maintain",
)
_COGNITIVE_OPERATIONS = frozenset(
    {"simulate_decision", "evolve_rule", "debate_internal"}
)
_POLICY_LEVELS = frozenset({"EXEMPT", "COMMUNION", "COUNSEL", "DESTRUCTIVE"})
_POLICY_NOTE = (
    "Authorization is keyed to the exact v7 tool name; v6 capabilities are "
    "never translated."
)

_WS = "ws_0123456789abcdef01234567"
_MEM = "mem_" + ("0" * 64)
_REL = "rel_" + ("1" * 64)
_RULE = "rule_" + ("2" * 64)
_TRIGGER = "trg_" + ("3" * 64)
_ACTIVE = "act_" + ("4" * 64)
_ENTITY = "ent_" + ("5" * 64)
_COMMUNITY = "com_" + ("6" * 64)
_CODE = "code_" + ("7" * 64)


class MappingCoverageError(ValueError):
    """Raised when the migration mapping and live v6 surface disagree."""


@dataclass(frozen=True, slots=True)
class PolicyChange:
    """One policy transition, optionally scoped to a branch condition."""

    v6_policy: str
    v7_policy: str
    condition: tuple[str, bool | str] | None = None
    note: str = _POLICY_NOTE


@dataclass(frozen=True, slots=True)
class ConditionalBranch:
    """One exact argument-sensitive v6 route and its static v7 replacement."""

    condition: tuple[str, bool | str]
    new_tool: str
    v6_policy: str
    v7_policy: str
    replacement_example: str


@dataclass(frozen=True, slots=True)
class ToolMapping:
    """Migration guidance for one v6 workflow action or cognitive tool."""

    old_operation: str
    new_tools: tuple[str, ...]
    removed_parameters: tuple[str, ...]
    policy_change: tuple[PolicyChange, ...]
    replacement_examples: tuple[str, ...]
    conditional_branches: tuple[ConditionalBranch, ...] = ()


def _removed(operation: str, *parameters: str) -> tuple[str, ...]:
    common = ("project_path",) if "." not in operation else ("action", "project_path")
    return tuple(dict.fromkeys((*common, *parameters)))


def _entry(
    old_operation: str,
    new_tool: str,
    policy: str,
    example: str,
    *,
    removed: tuple[str, ...] = (),
    v7_policy: str | None = None,
) -> ToolMapping:
    target_policy = v7_policy or policy
    return ToolMapping(
        old_operation=old_operation,
        new_tools=(new_tool,),
        removed_parameters=_removed(old_operation, *removed),
        policy_change=(PolicyChange(policy, target_policy),),
        replacement_examples=(example,),
    )


def _branch(
    parameter: str,
    value: bool | str,
    new_tool: str,
    policy: str,
    example: str,
    *,
    v7_policy: str | None = None,
) -> ConditionalBranch:
    return ConditionalBranch(
        condition=(parameter, value),
        new_tool=new_tool,
        v6_policy=policy,
        v7_policy=v7_policy or policy,
        replacement_example=example,
    )


def _conditional_entry(
    old_operation: str,
    branches: tuple[ConditionalBranch, ConditionalBranch],
    *,
    removed: tuple[str, ...] = (),
) -> ToolMapping:
    branch_parameter = branches[0].condition[0]
    return ToolMapping(
        old_operation=old_operation,
        new_tools=tuple(branch.new_tool for branch in branches),
        removed_parameters=_removed(
            old_operation,
            branch_parameter,
            *removed,
        ),
        policy_change=tuple(
            PolicyChange(
                branch.v6_policy,
                branch.v7_policy,
                branch.condition,
            )
            for branch in branches
        ),
        replacement_examples=tuple(branch.replacement_example for branch in branches),
        conditional_branches=branches,
    )


_MAPPINGS = (
    _entry(
        "commune.briefing",
        "session_brief",
        "EXEMPT",
        f'session_brief(workspace_id="{_WS}")',
        removed=("visual",),
    ),
    _entry(
        "commune.active_context",
        "active_context_list",
        "COMMUNION",
        f'active_context_list(workspace_id="{_WS}")',
    ),
    _entry(
        "commune.triggers",
        "context_triggers_match",
        "COMMUNION",
        f'context_triggers_match(workspace_id="{_WS}", relative_file_path="src/app.py")',
        removed=("file_path",),
    ),
    _entry(
        "commune.health",
        "system_health",
        "EXEMPT",
        f'system_health(workspace_id="{_WS}")',
    ),
    _entry(
        "commune.covenant",
        "covenant_status",
        "EXEMPT",
        f'covenant_status(workspace_id="{_WS}")',
        removed=("visual",),
    ),
    _entry(
        "commune.updates",
        "session_updates_get",
        "COMMUNION",
        f'session_updates_get(workspace_id="{_WS}", wait_seconds=10)',
        removed=("interval_seconds",),
    ),
    _entry(
        "consult.preflight",
        "memory_preflight",
        "COMMUNION",
        f'memory_preflight(workspace_id="{_WS}", target_tool="memory_store", target_arguments={{"record_type":"decision","content":"Use v7","idempotency_key":"example-0001"}})',
        removed=("target_operation", "target_args"),
    ),
    _entry(
        "consult.recall",
        "memory_recall",
        "COMMUNION",
        f'memory_recall(workspace_id="{_WS}", query="deployment decision")',
        removed=(
            "topic",
            "file_path",
            "offset",
            "since",
            "until",
            "include_linked",
            "visual",
            "condensed",
            "as_of_time",
        ),
    ),
    _entry(
        "consult.recall_file",
        "memory_recall_file",
        "COMMUNION",
        f'memory_recall_file(workspace_id="{_WS}", relative_file_path="src/app.py")',
        removed=("file_path",),
    ),
    _entry(
        "consult.recall_entity",
        "memory_recall_entity",
        "COMMUNION",
        f'memory_recall_entity(workspace_id="{_WS}", entity_name="Widget")',
    ),
    _entry(
        "consult.recall_hierarchical",
        "memory_recall_hierarchical",
        "COMMUNION",
        f'memory_recall_hierarchical(workspace_id="{_WS}", query="architecture")',
        removed=("topic",),
    ),
    _entry(
        "consult.search",
        "memory_search_text",
        "COMMUNION",
        f'memory_search_text(workspace_id="{_WS}", query="retry policy")',
        removed=("offset", "include_meta", "highlight_start", "highlight_end"),
    ),
    _entry(
        "consult.check_rules",
        "rule_check",
        "COMMUNION",
        f'rule_check(workspace_id="{_WS}", proposed_action="change storage")',
        removed=("action_desc",),
    ),
    _entry(
        "consult.compress",
        "context_compress",
        "COMMUNION",
        f'context_compress(workspace_id="{_WS}", text="bounded context")',
        removed=("compress_text",),
    ),
    _entry(
        "inscribe.remember",
        "memory_store",
        "COUNSEL",
        f'memory_store(workspace_id="{_WS}", record_type="decision", content="Use v7", idempotency_key="example-0001", preflight_token="<token>")',
        removed=("category", "file_path"),
    ),
    _entry(
        "inscribe.remember_batch",
        "memory_store_batch",
        "COUNSEL",
        f'memory_store_batch(workspace_id="{_WS}", records=[{{"record_type":"learning","content":"Bounded"}}], idempotency_key="example-0002", preflight_token="<token>")',
        removed=("memories",),
    ),
    _entry(
        "inscribe.link",
        "memory_link",
        "COUNSEL",
        f'memory_link(workspace_id="{_WS}", source_record_id="{_MEM}", target_record_id="{_MEM}", relationship_type="supports", idempotency_key="example-0003", preflight_token="<token>")',
        removed=("source_id", "target_id", "relationship"),
    ),
    _entry(
        "inscribe.unlink",
        "memory_unlink",
        "DESTRUCTIVE",
        f'memory_unlink(workspace_id="{_WS}", relationship_id="{_REL}", preflight_token="<token>")',
        removed=("source_id", "target_id", "relationship"),
    ),
    _entry(
        "inscribe.pin",
        "memory_pin_set",
        "COUNSEL",
        f'memory_pin_set(workspace_id="{_WS}", record_id="{_MEM}", pinned=true, preflight_token="<token>")',
        removed=("memory_id",),
    ),
    _entry(
        "inscribe.activate",
        "active_context_add",
        "COUNSEL",
        f'active_context_add(workspace_id="{_WS}", record_id="{_MEM}", preflight_token="<token>")',
        removed=("memory_id", "expires_in_hours"),
    ),
    _entry(
        "inscribe.deactivate",
        "active_context_remove",
        "DESTRUCTIVE",
        f'active_context_remove(workspace_id="{_WS}", active_context_id="{_ACTIVE}", preflight_token="<token>")',
        removed=("memory_id",),
    ),
    _entry(
        "inscribe.clear_active",
        "active_context_clear",
        "DESTRUCTIVE",
        f'active_context_clear(workspace_id="{_WS}", selection_token="<selection>", preflight_token="<token>")',
    ),
    _entry(
        "inscribe.ingest",
        "document_ingest_url",
        "COUNSEL",
        f'document_ingest_url(workspace_id="{_WS}", url="https://example.invalid/docs", topic="API", idempotency_key="example-0004", preflight_token="<token>")',
    ),
    _entry(
        "reflect.outcome",
        "memory_record_outcome",
        "COMMUNION",
        f'memory_record_outcome(workspace_id="{_WS}", record_id="{_MEM}", outcome_text="Verified", worked=true, idempotency_key="example-0005")',
        removed=("memory_id",),
    ),
    _entry(
        "reflect.verify",
        "memory_verify",
        "COMMUNION",
        f'memory_verify(workspace_id="{_WS}", text="The migration completed")',
        removed=("as_of_time",),
    ),
    _entry(
        "reflect.execute",
        "sandbox_execute_python",
        "DESTRUCTIVE",
        f'sandbox_execute_python(workspace_id="{_WS}", code="print(1)", preflight_token="<token>")',
    ),
    _entry(
        "understand.index",
        "code_index",
        "COMMUNION",
        f'code_index(workspace_id="{_WS}", relative_root="src")',
        removed=("path",),
    ),
    _entry(
        "understand.find",
        "code_search",
        "COMMUNION",
        f'code_search(workspace_id="{_WS}", query="workspace resolver")',
    ),
    _entry(
        "understand.impact",
        "code_impact_analyze",
        "COMMUNION",
        f'code_impact_analyze(workspace_id="{_WS}", code_entity_id="{_CODE}")',
        removed=("entity_name",),
    ),
    _conditional_entry(
        "understand.todos",
        (
            _branch(
                "auto_remember",
                False,
                "code_todos_scan",
                "COMMUNION",
                f'code_todos_scan(workspace_id="{_WS}")',
            ),
            _branch(
                "auto_remember",
                True,
                "code_todos_scan_and_store",
                "COUNSEL",
                f'code_todos_scan_and_store(workspace_id="{_WS}", idempotency_key="example-0006", preflight_token="<token>")',
            ),
        ),
        removed=("path",),
    ),
    _entry(
        "understand.refactor",
        "code_refactor_propose",
        "COMMUNION",
        f'code_refactor_propose(workspace_id="{_WS}", relative_file_path="src/app.py")',
        removed=("file_path",),
    ),
    _entry(
        "govern.add_rule",
        "rule_create",
        "COUNSEL",
        f'rule_create(workspace_id="{_WS}", trigger="before deploy", idempotency_key="example-0007", preflight_token="<token>")',
    ),
    _entry(
        "govern.update_rule",
        "rule_update",
        "COUNSEL",
        f'rule_update(workspace_id="{_WS}", rule_id="{_RULE}", patch={{"enabled":false}}, preflight_token="<token>")',
        removed=(
            "must_do",
            "must_not",
            "ask_first",
            "warnings",
            "priority",
            "enabled",
        ),
    ),
    _entry(
        "govern.list_rules",
        "rule_list",
        "COMMUNION",
        f'rule_list(workspace_id="{_WS}")',
    ),
    _entry(
        "govern.add_trigger",
        "context_trigger_create",
        "COUNSEL",
        f'context_trigger_create(workspace_id="{_WS}", trigger_type="file", pattern="src/*.py", recall_query="Python rules", idempotency_key="example-0008", preflight_token="<token>")',
        removed=("recall_topic", "recall_categories", "priority"),
    ),
    _entry(
        "govern.list_triggers",
        "context_trigger_list",
        "COMMUNION",
        f'context_trigger_list(workspace_id="{_WS}")',
    ),
    _entry(
        "govern.remove_trigger",
        "context_trigger_delete",
        "DESTRUCTIVE",
        f'context_trigger_delete(workspace_id="{_WS}", trigger_id="{_TRIGGER}", preflight_token="<token>")',
    ),
    _entry(
        "explore.related",
        "memory_related",
        "COMMUNION",
        f'memory_related(workspace_id="{_WS}", record_id="{_MEM}")',
        removed=("memory_id",),
    ),
    _entry(
        "explore.chain",
        "memory_chain_trace",
        "COMMUNION",
        f'memory_chain_trace(workspace_id="{_WS}", start_record_id="{_MEM}", end_record_id="{_MEM}")',
        removed=("start_memory_id", "end_memory_id"),
    ),
    _conditional_entry(
        "explore.graph",
        (
            _branch(
                "format",
                "json",
                "knowledge_graph_get",
                "COMMUNION",
                f'knowledge_graph_get(workspace_id="{_WS}")',
            ),
            _branch(
                "format",
                "mermaid",
                "knowledge_graph_render",
                "COMMUNION",
                f'knowledge_graph_render(workspace_id="{_WS}", format="mermaid")',
            ),
        ),
        removed=("memory_ids", "topic", "visual"),
    ),
    _entry(
        "explore.stats",
        "knowledge_graph_stats",
        "COMMUNION",
        f'knowledge_graph_stats(workspace_id="{_WS}")',
    ),
    _entry(
        "explore.communities",
        "community_list",
        "COMMUNION",
        f'community_list(workspace_id="{_WS}")',
        removed=("visual",),
    ),
    _entry(
        "explore.community_detail",
        "community_get",
        "COMMUNION",
        f'community_get(workspace_id="{_WS}", community_id="{_COMMUNITY}")',
    ),
    _entry(
        "explore.rebuild_communities",
        "community_rebuild",
        "COUNSEL",
        f'community_rebuild(workspace_id="{_WS}", idempotency_key="example-0009", preflight_token="<token>")',
    ),
    _entry(
        "explore.entities",
        "entity_list",
        "COMMUNION",
        f'entity_list(workspace_id="{_WS}")',
    ),
    _entry(
        "explore.backfill_entities",
        "entity_backfill",
        "COUNSEL",
        f'entity_backfill(workspace_id="{_WS}", idempotency_key="example-0010", preflight_token="<token>")',
    ),
    _entry(
        "explore.evolution",
        "entity_evolution_trace",
        "COMMUNION",
        f'entity_evolution_trace(workspace_id="{_WS}", entity_id="{_ENTITY}")',
    ),
    _entry(
        "explore.versions",
        "memory_versions_list",
        "COMMUNION",
        f'memory_versions_list(workspace_id="{_WS}", record_id="{_MEM}")',
        removed=("memory_id",),
    ),
    _entry(
        "explore.at_time",
        "memory_at_time_get",
        "COMMUNION",
        f'memory_at_time_get(workspace_id="{_WS}", record_id="{_MEM}", valid_time="2026-01-01T00:00:00Z")',
        removed=("memory_id", "timestamp"),
    ),
    _conditional_entry(
        "maintain.prune",
        (
            _branch(
                "dry_run",
                True,
                "memory_prune_preview",
                "COMMUNION",
                f'memory_prune_preview(workspace_id="{_WS}")',
            ),
            _branch(
                "dry_run",
                False,
                "memory_prune",
                "DESTRUCTIVE",
                f'memory_prune(workspace_id="{_WS}", selection_token="<selection>", preflight_token="<token>")',
            ),
        ),
    ),
    _entry(
        "maintain.archive",
        "memory_archive_set",
        "DESTRUCTIVE",
        f'memory_archive_set(workspace_id="{_WS}", record_id="{_MEM}", archived=true, preflight_token="<token>")',
        removed=("memory_id",),
    ),
    _conditional_entry(
        "maintain.cleanup",
        (
            _branch(
                "dry_run",
                True,
                "memory_duplicates_preview",
                "COMMUNION",
                f'memory_duplicates_preview(workspace_id="{_WS}")',
            ),
            _branch(
                "dry_run",
                False,
                "memory_duplicates_cleanup",
                "DESTRUCTIVE",
                f'memory_duplicates_cleanup(workspace_id="{_WS}", selection_token="<selection>", preflight_token="<token>")',
            ),
        ),
    ),
    _conditional_entry(
        "maintain.compact",
        (
            _branch(
                "dry_run",
                True,
                "memory_compaction_preview",
                "COMMUNION",
                f'memory_compaction_preview(workspace_id="{_WS}", summary="Weekly summary")',
            ),
            _branch(
                "dry_run",
                False,
                "memory_compact",
                "DESTRUCTIVE",
                f'memory_compact(workspace_id="{_WS}", summary="Weekly summary", selection_token="<selection>", idempotency_key="example-0011", preflight_token="<token>")',
            ),
        ),
        removed=("topic",),
    ),
    _entry(
        "maintain.rebuild_index",
        "projection_rebuild",
        "COMMUNION",
        f'projection_rebuild(workspace_id="{_WS}", projection="lexical")',
    ),
    _entry(
        "maintain.export",
        "workspace_export",
        "COMMUNION",
        f'workspace_export(workspace_id="{_WS}")',
    ),
    _entry(
        "maintain.import_data",
        "workspace_import",
        "DESTRUCTIVE",
        f'workspace_import(workspace_id="{_WS}", bundle={{"api_version":"7"}}, idempotency_key="example-0012", preflight_token="<token>")',
        removed=("data",),
    ),
    _entry(
        "maintain.link_project",
        "workspace_link",
        "COUNSEL",
        f'workspace_link(workspace_id="{_WS}", linked_workspace_id="{_WS}", preflight_token="<token>")',
        removed=("linked_path",),
    ),
    _entry(
        "maintain.unlink_project",
        "workspace_unlink",
        "DESTRUCTIVE",
        f'workspace_unlink(workspace_id="{_WS}", linked_workspace_id="{_WS}", preflight_token="<token>")',
        removed=("linked_path",),
    ),
    _entry(
        "maintain.list_projects",
        "workspace_links_list",
        "COMMUNION",
        f'workspace_links_list(workspace_id="{_WS}")',
    ),
    _conditional_entry(
        "maintain.consolidate",
        (
            _branch(
                "archive_sources",
                False,
                "workspace_consolidate",
                "COUNSEL",
                f'workspace_consolidate(workspace_id="{_WS}", source_workspace_ids=["{_WS}"], idempotency_key="example-0013", selection_token="<preview-token>", preflight_token="<token>")',
            ),
            _branch(
                "archive_sources",
                True,
                "workspace_consolidate_and_archive_sources",
                "DESTRUCTIVE",
                f'workspace_consolidate_and_archive_sources(workspace_id="{_WS}", source_workspace_ids=["{_WS}"], idempotency_key="example-0014", selection_token="<preview-token>", preflight_token="<token>")',
            ),
        ),
    ),
    _conditional_entry(
        "maintain.purge_dream_spam",
        (
            _branch(
                "dry_run",
                True,
                "dream_duplicates_preview",
                "COMMUNION",
                f'dream_duplicates_preview(workspace_id="{_WS}")',
            ),
            _branch(
                "dry_run",
                False,
                "dream_duplicates_purge",
                "DESTRUCTIVE",
                f'dream_duplicates_purge(workspace_id="{_WS}", selection_token="<selection>", preflight_token="<token>")',
            ),
        ),
    ),
    _entry(
        "simulate_decision",
        "decision_simulate",
        "COMMUNION",
        f'decision_simulate(workspace_id="{_WS}", record_id="{_MEM}")',
        removed=("decision_id",),
    ),
    _entry(
        "evolve_rule",
        "rule_evolution_analyze",
        "COMMUNION",
        f'rule_evolution_analyze(workspace_id="{_WS}", rule_id="{_RULE}")',
    ),
    _entry(
        "debate_internal",
        "decision_debate",
        "COUNSEL",
        f'decision_debate(workspace_id="{_WS}", topic="Storage", advocate_position="Use A", challenger_position="Use B", idempotency_key="example-0015", preflight_token="<token>")',
    ),
)


V6_TO_V7_MAPPINGS: tuple[ToolMapping, ...] = tuple(
    sorted(_MAPPINGS, key=lambda item: item.old_operation)
)
V6_TO_V7_BY_OPERATION: Mapping[str, ToolMapping] = MappingProxyType(
    {entry.old_operation: entry for entry in V6_TO_V7_MAPPINGS}
)


_EXPECTED_CONDITIONS: Mapping[str, frozenset[tuple[str, bool | str]]] = (
    MappingProxyType(
        {
            "understand.todos": frozenset(
                {("auto_remember", False), ("auto_remember", True)}
            ),
            "explore.graph": frozenset({("format", "json"), ("format", "mermaid")}),
            "maintain.prune": frozenset({("dry_run", True), ("dry_run", False)}),
            "maintain.cleanup": frozenset({("dry_run", True), ("dry_run", False)}),
            "maintain.compact": frozenset({("dry_run", True), ("dry_run", False)}),
            "maintain.consolidate": frozenset(
                {("archive_sources", False), ("archive_sources", True)}
            ),
            "maintain.purge_dream_spam": frozenset(
                {("dry_run", True), ("dry_run", False)}
            ),
        }
    )
)


def current_v6_operations() -> frozenset[str]:
    """Discover the eight live dispatcher action sets plus cognitive tools."""

    operations: set[str] = set(_COGNITIVE_OPERATIONS)
    for workflow_name in _WORKFLOW_NAMES:
        module = importlib.import_module(f"daem0nmcp.workflows.{workflow_name}")
        actions = getattr(module, "VALID_ACTIONS", None)
        if not isinstance(actions, frozenset) or not all(
            isinstance(action, str) and action for action in actions
        ):
            raise MappingCoverageError(
                f"{workflow_name}.VALID_ACTIONS is not a non-empty string frozenset"
            )
        operations.update(f"{workflow_name}.{action}" for action in actions)
    return frozenset(operations)


def _duplicates(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return sorted(duplicates)


def validate_mapping(
    mappings: Sequence[ToolMapping] = V6_TO_V7_MAPPINGS,
    *,
    current_operations: Collection[str] | None = None,
) -> None:
    """Fail closed when the mapping is missing, stale, or internally ambiguous."""

    duplicate_operations = _duplicates(entry.old_operation for entry in mappings)
    if duplicate_operations:
        raise MappingCoverageError(
            "duplicate mapping operations: " + ", ".join(duplicate_operations)
        )

    mapped_operations = {entry.old_operation for entry in mappings}
    live_operations = set(
        current_v6_operations() if current_operations is None else current_operations
    )
    unmapped = sorted(live_operations - mapped_operations)
    stale = sorted(mapped_operations - live_operations)
    errors: list[str] = []
    if unmapped:
        errors.append("unmapped v6 operations: " + ", ".join(unmapped))
    if stale:
        errors.append("stale mapping operations: " + ", ".join(stale))
    if errors:
        raise MappingCoverageError("; ".join(errors))

    conditional_operations = {
        entry.old_operation for entry in mappings if entry.conditional_branches
    }
    expected_conditional = set(_EXPECTED_CONDITIONS)
    if conditional_operations != expected_conditional:
        missing = sorted(expected_conditional - conditional_operations)
        unexpected = sorted(conditional_operations - expected_conditional)
        details = []
        if missing:
            details.append("missing conditional mappings: " + ", ".join(missing))
        if unexpected:
            details.append("unexpected conditional mappings: " + ", ".join(unexpected))
        raise MappingCoverageError("; ".join(details))

    all_new_tools: list[str] = []
    for entry in mappings:
        if not entry.old_operation or not entry.new_tools:
            raise MappingCoverageError("mapping rows require old and new tool names")
        if len(set(entry.removed_parameters)) != len(entry.removed_parameters):
            raise MappingCoverageError(
                f"duplicate removed parameters for {entry.old_operation}"
            )
        if len(entry.replacement_examples) != len(entry.new_tools):
            raise MappingCoverageError(
                f"replacement example count mismatch for {entry.old_operation}"
            )
        if len(entry.policy_change) != len(entry.new_tools):
            raise MappingCoverageError(
                f"policy change count mismatch for {entry.old_operation}"
            )
        for tool, example, change in zip(
            entry.new_tools,
            entry.replacement_examples,
            entry.policy_change,
            strict=True,
        ):
            if not tool or not tool.replace("_", "").isalnum():
                raise MappingCoverageError(
                    f"invalid v7 tool name for {entry.old_operation}: {tool!r}"
                )
            if not example.startswith(f"{tool}("):
                raise MappingCoverageError(
                    f"replacement example does not call {tool} for "
                    f"{entry.old_operation}"
                )
            if change.v6_policy not in _POLICY_LEVELS:
                raise MappingCoverageError(
                    f"invalid v6 policy for {entry.old_operation}"
                )
            if change.v7_policy not in _POLICY_LEVELS:
                raise MappingCoverageError(
                    f"invalid v7 policy for {entry.old_operation}"
                )
        all_new_tools.extend(entry.new_tools)

        expected_conditions = _EXPECTED_CONDITIONS.get(entry.old_operation)
        actual_conditions = frozenset(
            branch.condition for branch in entry.conditional_branches
        )
        if expected_conditions is not None:
            if actual_conditions != expected_conditions:
                raise MappingCoverageError(
                    f"conditional branch mismatch for {entry.old_operation}"
                )
            branch_tools = tuple(
                branch.new_tool for branch in entry.conditional_branches
            )
            if branch_tools != entry.new_tools:
                raise MappingCoverageError(
                    f"conditional tool order mismatch for {entry.old_operation}"
                )
        elif entry.conditional_branches:
            raise MappingCoverageError(
                f"unexpected conditional branches for {entry.old_operation}"
            )

    duplicate_tools = _duplicates(all_new_tools)
    if duplicate_tools:
        raise MappingCoverageError(
            "v7 tools mapped more than once: " + ", ".join(duplicate_tools)
        )


def _policy_change_document(change: PolicyChange) -> dict[str, object]:
    when = None
    if change.condition is not None:
        when = {change.condition[0]: change.condition[1]}
    return {
        "from": change.v6_policy,
        "note": change.note,
        "to": change.v7_policy,
        "when": when,
    }


def _branch_document(branch: ConditionalBranch) -> dict[str, object]:
    return {
        "new_tool": branch.new_tool,
        "policy_change": {
            "from": branch.v6_policy,
            "to": branch.v7_policy,
        },
        "replacement_example": branch.replacement_example,
        "when": {branch.condition[0]: branch.condition[1]},
    }


def mapping_document(
    mappings: Sequence[ToolMapping] = V6_TO_V7_MAPPINGS,
) -> dict[str, object]:
    """Return the JSON-compatible, deterministic migration document."""

    validate_mapping(mappings)
    rows = []
    for entry in sorted(mappings, key=lambda item: item.old_operation):
        rows.append(
            {
                "conditional_branches": [
                    _branch_document(branch) for branch in entry.conditional_branches
                ],
                "new_tools": list(entry.new_tools),
                "old_operation": entry.old_operation,
                "policy_change": [
                    _policy_change_document(change) for change in entry.policy_change
                ],
                "removed_parameters": list(entry.removed_parameters),
                "replacement_examples": list(entry.replacement_examples),
            }
        )
    return {
        "api_version": "7",
        "mappings": rows,
        "schema_version": 1,
    }


def render_mapping_json(
    mappings: Sequence[ToolMapping] = V6_TO_V7_MAPPINGS,
) -> str:
    """Render exact compact, sorted, UTF-8-safe JSON without a trailing newline."""

    return json.dumps(
        _normalize_json(mapping_document(mappings)),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _normalize_json(value: object) -> object:
    """Apply Task 7's NFC rule to the generated JSON-compatible document."""

    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        normalized_text = unicodedata.normalize("NFC", value)
        normalized_text.encode("utf-8")
        return normalized_text
    if isinstance(value, list):
        return [_normalize_json(item) for item in value]
    if isinstance(value, dict):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            normalized_key = unicodedata.normalize("NFC", key)
            if normalized_key in normalized:
                raise MappingCoverageError(
                    "mapping document keys collide after Unicode normalization"
                )
            normalized[normalized_key] = _normalize_json(item)
        return normalized
    raise MappingCoverageError(
        f"mapping document contains unsupported JSON value: {type(value).__name__}"
    )


__all__ = [
    "ConditionalBranch",
    "MappingCoverageError",
    "PolicyChange",
    "ToolMapping",
    "V6_TO_V7_BY_OPERATION",
    "V6_TO_V7_MAPPINGS",
    "current_v6_operations",
    "mapping_document",
    "render_mapping_json",
    "validate_mapping",
]
