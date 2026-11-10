"""Dependency-free source checks for runtime semantic-write routing."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


class MemoryEventRoutingSourceTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(__file__).resolve().parents[1] / "daem0nmcp" / "memory.py"
        self.source = self.path.read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)

    def _method_source(self, name: str) -> str:
        node = next(
            node
            for node in ast.walk(self.tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        )
        return ast.get_source_segment(self.source, node) or ""

    def test_core_mutators_append_exact_v7_event_types(self):
        expected = {
            "remember": "memory.created",
            "remember_batch": "memory.created",
            "record_outcome": "memory.outcome_recorded",
            "compact_memories": "memory.archived_set",
            "link_memories": "relationship.created",
            "unlink_memories": "relationship.removed",
        }
        for method, event_type in expected.items():
            with self.subTest(method=method):
                body = self._method_source(method)
                self.assertIn("_append_v7", body)
                self.assertIn(event_type, body)

    def test_batch_and_compaction_create_compatibility_version_one(self):
        for method in ("remember_batch", "compact_memories"):
            with self.subTest(method=method):
                self.assertIn("MemoryVersion(", self._method_source(method))

    def test_maintenance_mutations_and_exports_route_through_event_store(self):
        root = Path(__file__).resolve().parents[1]
        maintenance = (root / "daem0nmcp" / "tools" / "maintenance.py").read_text(
            encoding="utf-8"
        )
        workflow = (root / "daem0nmcp" / "workflows" / "maintain.py").read_text(
            encoding="utf-8"
        )
        memory_tools = (root / "daem0nmcp" / "tools" / "memory.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("export_event_bundle_async", maintenance)
        self.assertIn("import_event_bundle_async", maintenance)
        self.assertIn('"event_bundle"', maintenance)
        self.assertIn('"typed_id"', maintenance)
        self.assertNotIn("delete(Memory)", maintenance)
        self.assertNotIn("session.delete(memory)", maintenance)
        self.assertNotIn("delete(Memory)", workflow)
        self.assertIn("delete_compatibility_memory", workflow)
        for source in (maintenance, workflow, memory_tools):
            self.assertIn('"memory.deleted"', source)

    def test_temporal_invalidation_is_event_backed_for_format_seven(self):
        root = Path(__file__).resolve().parents[1]
        temporal = (root / "daem0nmcp" / "graph" / "temporal.py").read_text(
            encoding="utf-8"
        )
        contradiction = (root / "daem0nmcp" / "graph" / "contradiction.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('"memory.version_invalidated"', temporal)
        self.assertIn("format_version", temporal)
        self.assertNotIn("version.valid_to =", contradiction)
        self.assertNotIn("version.invalidated_by_version_id =", contradiction)

    def test_retained_v6_ids_are_resolved_before_post_migration_events(self):
        """Every compatibility mutator/export must reuse migrated canonical IDs."""
        root = Path(__file__).resolve().parents[1]
        memory = self.source
        temporal = (root / "daem0nmcp" / "graph" / "temporal.py").read_text(
            encoding="utf-8"
        )
        maintenance = (root / "daem0nmcp" / "tools" / "maintenance.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("resolve_compatibility_stream_async", memory)
        self.assertIn("await self._resolve_typed_memory_id", memory)
        self.assertIn("await self._resolve_typed_relationship_id", memory)
        self.assertIn("resolve_compatibility_stream_async", temporal)
        self.assertIn("resolve_compatibility_stream_async", maintenance)
        self.assertNotIn("select(LegacyIdMap", temporal)
        self.assertNotIn("select(LegacyIdMap", maintenance)

    def test_new_runtime_streams_use_nonreusable_internal_idempotency_keys(self):
        """Deleting the highest v6 row ID must not recycle a canonical identity."""
        memory_append = self._method_source("_append_v7_memory_event")
        relation_append = self._method_source("_append_v7_relationship_event")
        memory_resolve = self._method_source("_resolve_typed_memory_id")
        relation_resolve = self._method_source("_resolve_typed_relationship_id")

        self.assertIn("secrets.token_hex", self.source)
        self.assertIn("_new_typed_memory_id", memory_append)
        self.assertIn("_new_typed_relationship_id", relation_append)
        self.assertNotIn("or self._typed_memory_id", memory_resolve)
        self.assertNotIn("or self._typed_relationship_id", relation_resolve)

    def test_format_seven_never_writes_legacy_vectors_or_qdrant_payloads(self):
        """The v7 event/projection path must not dual-write legacy vectors."""

        initializer = self._method_source("__init__")
        helper = self._method_source("_legacy_vector_writes_enabled")
        self.assertIn("format_version != 7", helper)
        self.assertIn("_legacy_vector_writes_enabled", initializer)

        for method in ("remember", "remember_batch", "compact_memories"):
            with self.subTest(method=method):
                body = self._method_source(method)
                self.assertIn("_legacy_vector_writes_enabled", body)
                self.assertNotIn(
                    "if self._vectors_enabled else None",
                    body,
                )

    def test_remote_legacy_qdrant_does_not_create_a_local_store_directory(self):
        initializer = self._method_source("__init__")
        remote_branch = initializer.index("if settings.qdrant_url")
        local_directory = initializer.index("Path(qdrant_path).mkdir")
        self.assertGreater(local_directory, remote_branch)

    def test_format_seven_import_export_never_carries_legacy_vector_blobs(self):
        root = Path(__file__).resolve().parents[1]
        maintenance = (root / "daem0nmcp" / "tools" / "maintenance.py").read_text(
            encoding="utf-8"
        )
        normalized = " ".join(maintenance.split())

        self.assertIn(
            "include_vectors and ctx.db_manager.format_version != 7",
            normalized,
        )
        self.assertIn(
            "vector_bytes if ctx.db_manager.format_version != 7 else None",
            normalized,
        )

    def test_committed_v7_events_trigger_the_durable_lexical_job_runner(self):
        root = Path(__file__).resolve().parents[1] / "daem0nmcp"
        event_store = (root / "event_store.py").read_text(encoding="utf-8")
        database = (root / "database.py").read_text(encoding="utf-8")

        self.assertIn('session.info["daem0nmcp_v7_event_appended"] = True', event_store)
        self.assertIn("drain_projection_jobs", database)
        self.assertTrue(
            any(
                isinstance(node, ast.Call)
                and ast.unparse(node.func) == "session.info.pop"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "daem0nmcp_v7_event_appended"
                for node in ast.walk(ast.parse(database))
            )
        )
        self.assertIn("schedule_projection_job_drain", database)

    def test_format_seven_recall_uses_only_the_retrieval_service_facade(self):
        recall = self._method_source("recall")
        v7_recall = self._method_source("_recall_v7")
        factory = self._method_source("_get_v7_retrieval_service")

        self.assertIn("self.db.format_version == 7", recall)
        self.assertLess(
            recall.index("self.db.format_version == 7"),
            recall.index("get_recall_cache"),
        )
        self.assertIn("RetrievalQuery", v7_recall)
        self.assertIn("service.retrieve", v7_recall)
        self.assertNotIn("_hybrid_search", v7_recall)
        self.assertNotIn("TFIDF", v7_recall)
        self.assertIn("create_retrieval_service", factory)

    def test_v6_hybrid_weight_adapter_warns_before_reading_legacy_setting(self):
        hybrid = self._method_source("_hybrid_search")

        self.assertIn("warn_legacy_hybrid_weight", hybrid)
        self.assertLess(
            hybrid.index("warn_legacy_hybrid_weight"),
            hybrid.index("settings.hybrid_vector_weight"),
        )

    def test_repo_wide_semantic_memory_bypass_scan(self):
        """Only event_store compatibility projectors may mutate semantic fields."""
        package = Path(__file__).resolve().parents[1] / "daem0nmcp"
        semantic_fields = {
            "category",
            "content",
            "rationale",
            "context",
            "tags",
            "file_path",
            "file_path_relative",
            "keywords",
            "is_permanent",
            "outcome",
            "worked",
            "pinned",
            "archived",
            "source_client",
            "source_model",
            "valid_to",
            "invalidated_by_version_id",
        }
        violations = []
        for path in package.rglob("*.py"):
            relative = path.relative_to(package).as_posix()
            if relative == "event_store.py" or relative.startswith("migrations/"):
                continue
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    targets = node.targets
                elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                    targets = [node.target]
                else:
                    targets = []
                for target in targets:
                    for item in ast.walk(target):
                        if (
                            isinstance(item, ast.Attribute)
                            and item.attr in semantic_fields
                        ):
                            violations.append(f"{relative}:{node.lineno}:{item.attr}")
                if not isinstance(node, ast.Call):
                    continue
                if (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "delete"
                    and node.args
                    and isinstance(node.args[0], ast.Name)
                    and node.args[0].id == "Memory"
                ):
                    violations.append(f"{relative}:{node.lineno}:delete(Memory)")
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "delete"
                    and node.args
                    and isinstance(node.args[0], ast.Name)
                    and node.args[0].id == "memory"
                ):
                    violations.append(
                        f"{relative}:{node.lineno}:session.delete(memory)"
                    )
        self.assertEqual([], violations)


if __name__ == "__main__":
    unittest.main()
