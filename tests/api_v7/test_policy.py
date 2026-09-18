from __future__ import annotations

import unittest
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field

from daem0nmcp.covenant import (
    CapabilityAuthority,
    CovenantGate,
    CovenantLevel,
    CovenantStateStore,
    InvocationScope,
)

PINNED_TOOLS = frozenset(
    {
        "session_brief",
        "memory_preflight",
        "memory_recall",
        "memory_store",
        "memory_record_outcome",
        "system_health",
    }
)

GRANULAR_TOOLS = frozenset(
    {
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
        "memory_store_batch",
        "memory_link",
        "memory_unlink",
        "memory_pin_set",
        "active_context_add",
        "active_context_remove",
        "active_context_clear",
        "document_ingest_url",
        "memory_verify",
        "sandbox_execute_python",
        "code_index",
        "code_search",
        "code_impact_analyze",
        "code_todos_scan",
        "code_todos_scan_and_store",
        "code_refactor_propose",
        "rule_create",
        "rule_update",
        "rule_list",
        "context_trigger_create",
        "context_trigger_list",
        "context_trigger_delete",
        "memory_related",
        "memory_chain_trace",
        "knowledge_graph_get",
        "knowledge_graph_render",
        "knowledge_graph_stats",
        "community_list",
        "community_get",
        "community_rebuild",
        "entity_list",
        "entity_backfill",
        "entity_evolution_trace",
        "memory_versions_list",
        "memory_at_time_get",
        "memory_prune_preview",
        "memory_prune",
        "memory_archive_set",
        "memory_duplicates_preview",
        "memory_duplicates_cleanup",
        "memory_compaction_preview",
        "memory_compact",
        "projection_rebuild",
        "workspace_export",
        "workspace_import",
        "workspace_link",
        "workspace_unlink",
        "workspace_links_list",
        "workspace_consolidate",
        "workspace_consolidation_preview",
        "workspace_consolidate_and_archive_sources",
        "edit_preflight",
        "memory_capture_list",
        "memory_capture_promote",
        "dream_duplicates_preview",
        "dream_duplicates_purge",
        "decision_simulate",
        "rule_evolution_analyze",
        "decision_debate",
    }
)


class _StoreArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    workspace_id: str
    content: str
    tags: list[str] = Field(default_factory=list)
    preflight_token: str | None = None


class V7PolicyTests(unittest.TestCase):
    def test_policy_is_fixed_complete_and_has_no_v6_names(self) -> None:
        from daem0nmcp.api.v7.policy import V7_COVENANT_POLICY, V7_TOOL_LEVELS

        self.assertIsInstance(V7_TOOL_LEVELS, MappingProxyType)
        self.assertEqual(set(V7_TOOL_LEVELS), PINNED_TOOLS | GRANULAR_TOOLS)
        self.assertFalse(
            {
                "commune",
                "consult",
                "inscribe",
                "reflect",
                "understand",
                "govern",
                "explore",
                "maintain",
                "simulate_decision",
                "evolve_rule",
                "debate_internal",
            }
            & set(V7_TOOL_LEVELS)
        )
        self.assertEqual(
            V7_COVENANT_POLICY.resolve("session_brief"), CovenantLevel.EXEMPT
        )
        self.assertEqual(
            V7_COVENANT_POLICY.resolve("memory_record_outcome"),
            CovenantLevel.COMMUNION,
        )
        self.assertEqual(
            V7_COVENANT_POLICY.resolve("memory_store"), CovenantLevel.COUNSEL
        )
        self.assertEqual(
            V7_COVENANT_POLICY.resolve("memory_prune"), CovenantLevel.DESTRUCTIVE
        )

    def test_split_operations_have_static_policy(self) -> None:
        from daem0nmcp.api.v7.policy import V7_COVENANT_POLICY

        fixed_pairs = (
            ("code_todos_scan", CovenantLevel.COMMUNION),
            ("code_todos_scan_and_store", CovenantLevel.COUNSEL),
            ("memory_prune_preview", CovenantLevel.COMMUNION),
            ("memory_prune", CovenantLevel.DESTRUCTIVE),
            ("workspace_consolidate", CovenantLevel.COUNSEL),
            (
                "workspace_consolidate_and_archive_sources",
                CovenantLevel.DESTRUCTIVE,
            ),
        )
        for name, expected in fixed_pairs:
            with self.subTest(name=name):
                self.assertEqual(V7_COVENANT_POLICY.resolve(name, {}), expected)
                self.assertEqual(
                    V7_COVENANT_POLICY.resolve(name, {"ignored": True}), expected
                )

    def test_gate_accepts_model_normalizer_and_hashes_effective_arguments(self) -> None:
        from daem0nmcp.api.v7.policy import (
            V7ArgumentNormalizer,
            V7CovenantPolicy,
        )

        policy = V7CovenantPolicy({"memory_store": CovenantLevel.COUNSEL})
        normalizer = V7ArgumentNormalizer({"memory_store": _StoreArguments})

        def clock():
            return 1_000

        gate = CovenantGate(
            state_store=CovenantStateStore(clock=clock),
            authority=CapabilityAuthority(secret=b"s" * 32, kid="test", clock=clock),
            policy=policy,
            argument_normalizer=normalizer,
        )
        scope = InvocationScope("principal", "session", ".")
        gate.record_briefing(scope)
        token = gate.issue_preflight(
            scope,
            "memory_store",
            {"workspace_id": "ws_" + "a" * 24, "content": "x"},
        )

        # The default list and the raw capability are not part of the digest.
        self.assertIsNone(
            gate.authorize(
                "memory_store",
                {
                    "workspace_id": "ws_" + "a" * 24,
                    "content": "x",
                    "tags": [],
                    "preflight_token": token,
                },
                scope,
                preflight_token=token,
            )
        )

    def test_model_normalizer_rejects_unknown_fields_and_legacy_operation(self) -> None:
        from daem0nmcp.api.v7.policy import V7ArgumentNormalizer

        normalizer = V7ArgumentNormalizer({"memory_store": _StoreArguments})
        with self.assertRaises(ValueError):
            normalizer(
                "memory_store",
                {
                    "workspace_id": "ws_" + "a" * 24,
                    "content": "x",
                    "project_path": "C:/secret",
                },
                ".",
            )
        with self.assertRaises(ValueError):
            normalizer("inscribe.remember", {}, ".")

    def test_live_grant_capacity_fails_without_evicting_or_orphaning(self) -> None:
        from daem0nmcp.api.v7.opaque_capabilities import (
            OpaqueCapabilityAuthority,
        )
        from daem0nmcp.api.v7.policy import (
            V7ArgumentNormalizer,
            V7CovenantPolicy,
        )
        from daem0nmcp.covenant import (
            CovenantStateCapacityError,
            TokenValidationError,
        )

        def clock():
            return 1_000

        handles = iter(("cap_" + "a" * 20, "cap_" + "b" * 20))
        authority = OpaqueCapabilityAuthority(
            CapabilityAuthority(secret=b"s" * 32, kid="test", clock=clock),
            token_factory=lambda: next(handles),
            clock=clock,
        )
        gate = CovenantGate(
            state_store=CovenantStateStore(
                clock=clock,
                max_capabilities_per_scope=1,
            ),
            authority=authority,
            policy=V7CovenantPolicy({"memory_store": CovenantLevel.COUNSEL}),
            argument_normalizer=V7ArgumentNormalizer({"memory_store": _StoreArguments}),
        )
        scope = InvocationScope("principal", "session", ".")
        arguments = {
            "workspace_id": "ws_" + "a" * 24,
            "content": "first",
        }
        gate.record_briefing(scope)
        first = gate.issue_preflight(scope, "memory_store", arguments)

        with self.assertRaises(CovenantStateCapacityError):
            gate.issue_preflight(
                scope,
                "memory_store",
                {**arguments, "content": "second"},
            )

        self.assertIsNone(
            gate.authorize(
                "memory_store",
                arguments,
                scope,
                preflight_token=first,
                consume_capability=False,
            )
        )
        with self.assertRaises(TokenValidationError):
            authority.verify("cap_" + "b" * 20)


if __name__ == "__main__":
    unittest.main()
