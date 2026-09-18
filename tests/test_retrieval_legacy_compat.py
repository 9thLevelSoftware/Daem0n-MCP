"""Behavioral coverage for the v7-to-legacy recall compatibility envelope."""

from __future__ import annotations

import ast
import json
import logging
import re
import sqlite3
import sys
import tempfile
import types
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from daem0nmcp.retrieval.legacy_compat import build_legacy_recall_categories
from daem0nmcp.retrieval.types import (
    CitationEntry,
    ContextPackage,
    EvidenceItem,
    EvidenceRef,
    RetrievalResult,
)

_WORKSPACE_ID = "ws_0123456789abcdef01234567"


def _opaque(prefix: str, digit: str) -> str:
    return prefix + digit * 64


def _item(
    primary: EvidenceRef,
    *additional: EvidenceRef,
    category: str = "decision",
    excerpt: str = "Canonical selected content",
) -> EvidenceItem:
    refs = (primary, *additional)
    return EvidenceItem(
        citation="[E1]",
        excerpt=excerpt,
        category=category,
        status="current",
        score=0.75,
        channels=frozenset(ref.provider for ref in refs),
        token_count=3,
        evidence_refs=refs,
        outcome="The selected approach worked.",
    )


def _memory_method_subject(*method_names: str) -> type:
    source_path = Path(__file__).resolve().parents[1] / "daem0nmcp" / "memory.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    memory_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "MemoryManager"
    )
    methods = [
        node
        for node in memory_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in method_names
    ]
    subject = ast.ClassDef(
        name="MemorySubject",
        bases=[],
        keywords=[],
        body=methods,
        decorator_list=[],
    )
    module = ast.fix_missing_locations(ast.Module(body=[subject], type_ignores=[]))
    namespace = {
        "__name__": "daem0nmcp._legacy_recall_test_subject",
        "__package__": "daem0nmcp",
        "Any": Any,
        "datetime": datetime,
        "logger": logging.getLogger(__name__),
        "re": re,
        "settings": SimpleNamespace(
            retrieval_candidate_limit=50,
            retrieval_token_budget=512,
            retrieval_rerank_enabled=False,
        ),
        "timezone": timezone,
        "_normalize_file_path": lambda path, _project: (path, path),
    }
    exec(compile(module, str(source_path), "exec"), namespace)
    return namespace["MemorySubject"]


class LegacyRecallCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_corrupt_event_envelope_omits_legacy_metadata(self) -> None:
        from daem0nmcp.database import DatabaseManager
        from daem0nmcp.event_store import canonical_json_bytes
        from daem0nmcp.memory import MemoryManager
        from daem0nmcp.retrieval.legacy_compat import (
            load_legacy_evidence_metadata,
        )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            database = DatabaseManager(str(Path(directory) / "storage"))
            await database.init_db()
            manager = MemoryManager(database)
            created = await manager.remember(
                category="decision",
                content="Authenticated event envelope marker",
                context={"reason": "safe historical context"},
            )
            connection = sqlite3.connect(database.db_path)
            try:
                row = connection.execute(
                    "SELECT event_id,stream_id,payload_json FROM memory_events "
                    "WHERE workspace_id=? AND stream_kind='memory' "
                    "ORDER BY recorded_at_us DESC,event_id DESC LIMIT 1",
                    (database.workspace_id,),
                ).fetchone()
                record_row = connection.execute(
                    "SELECT content_hash FROM memory_records WHERE record_id=?",
                    (row[1],),
                ).fetchone()
                payload = json.loads(row[2])
                payload["compatibility"]["legacy_memory_id"] = created["id"] + 998
                connection.execute("DROP TRIGGER memory_events_no_update")
                connection.execute(
                    "UPDATE memory_events SET payload_json=? WHERE event_id=?",
                    (canonical_json_bytes(payload).decode("utf-8"), row[0]),
                )
                connection.commit()
            finally:
                connection.close()

            reference = EvidenceRef(
                record_id=str(row[1]),
                event_id=str(row[0]),
                content_hash=str(record_row[0]),
                version_id=None,
                provider="lexical",
            )
            item = _item(reference, excerpt="Authenticated event envelope marker")
            metadata = load_legacy_evidence_metadata(
                database.db_path, database.workspace_id, (item,)
            )
            rendered = build_legacy_recall_categories(
                (item,),
                per_category_limit=1,
                condensed=False,
                metadata=metadata,
                origin_workspace_id=database.workspace_id,
            )["decisions"][0]

            self.assertEqual(reference.record_id, rendered["id"])
            self.assertIsNone(rendered["context"])
            self.assertEqual(reference.record_id, rendered["record_id"])
            await database.close()

    async def test_uses_retained_primary_and_only_output_selected_metadata(
        self,
    ) -> None:
        primary = EvidenceRef(
            record_id=_opaque("mem_", "f"),
            event_id=_opaque("evt_", "f"),
            content_hash="f" * 64,
            version_id=None,
            provider="graph",
        )
        lexicographically_first_secondary = EvidenceRef(
            record_id=_opaque("mem_", "0"),
            event_id=_opaque("evt_", "0"),
            content_hash="f" * 64,
            version_id=None,
            provider="lexical",
        )
        per_category_dropped = EvidenceRef(
            record_id=_opaque("mem_", "e"),
            event_id=_opaque("evt_", "e"),
            content_hash="e" * 64,
            version_id=None,
            provider="lexical",
        )
        retained = replace(
            _item(primary, lexicographically_first_secondary),
            rationale="Canonical rationale",
            tags=("v7", "compatibility"),
            worked=True,
        )
        dropped = replace(
            _item(per_category_dropped),
            rationale="DROPPED SECRET",
            tags=("dropped-secret",),
            worked=False,
        )

        categories = build_legacy_recall_categories(
            (retained, dropped),
            per_category_limit=1,
            condensed=False,
        )

        self.assertEqual([], categories["patterns"])
        self.assertEqual([], categories["warnings"])
        self.assertEqual([], categories["learnings"])
        self.assertEqual(1, len(categories["decisions"]))
        rendered = categories["decisions"][0]
        self.assertEqual(primary.record_id, rendered["id"])
        self.assertEqual("Canonical rationale", rendered["rationale"])
        self.assertIsNone(rendered["context"])
        self.assertEqual(["v7", "compatibility"], rendered["tags"])
        self.assertIs(rendered["worked"], True)
        self.assertEqual(
            [primary.record_id, lexicographically_first_secondary.record_id],
            [ref["record_id"] for ref in rendered["evidence_refs"]],
        )
        self.assertNotIn("DROPPED SECRET", json.dumps(categories))

    async def test_rejects_unbounded_post_policy_metadata_reads(self) -> None:
        items = tuple(
            _item(
                EvidenceRef(
                    record_id=f"mem_{index:064x}",
                    event_id=f"evt_{index:064x}",
                    content_hash="a" * 64,
                    version_id=None,
                    provider="lexical",
                )
            )
            for index in range(101)
        )
        with self.assertRaisesRegex(ValueError, "at most 100"):
            build_legacy_recall_categories(
                items,
                per_category_limit=101,
                condensed=False,
            )

    async def test_condensed_output_redacts_verbose_fields_but_keeps_outcome_state(
        self,
    ) -> None:
        primary = EvidenceRef(
            record_id=_opaque("mem_", "c"),
            event_id=_opaque("evt_", "c"),
            content_hash="c" * 64,
            version_id=None,
            provider="lexical",
        )
        item = replace(
            _item(primary, excerpt="x" * 200),
            rationale="Verbose rationale",
            tags=("retained-tag",),
            worked=True,
        )
        categories = build_legacy_recall_categories(
            (item,),
            per_category_limit=1,
            condensed=True,
        )

        rendered = categories["decisions"][0]
        self.assertEqual("x" * 150 + "...", rendered["content"])
        self.assertIsNone(rendered["rationale"])
        self.assertIsNone(rendered["context"])
        self.assertEqual(["retained-tag"], rendered["tags"])
        self.assertIs(rendered["worked"], True)

    async def test_memory_v7_recall_uses_repository_snapshot_metadata(
        self,
    ) -> None:
        primary = EvidenceRef(
            record_id=_opaque("mem_", "b"),
            event_id=_opaque("evt_", "b"),
            content_hash="b" * 64,
            version_id=None,
            provider="lexical",
        )
        item = replace(
            _item(primary),
            rationale="Snapshot rationale",
            tags=("snapshot-tag",),
            worked=True,
        )
        context_text = f"[E1] {item.excerpt}"
        retrieval = RetrievalResult(
            items=(item,),
            context=ContextPackage(
                text=context_text,
                citations=(
                    CitationEntry(
                        marker="[E1]",
                        evidence_refs=(primary,),
                        channels=frozenset({"lexical"}),
                        excerpt_start=5,
                        excerpt_end=5 + len(item.excerpt),
                    ),
                ),
                token_budget=512,
                requested_tokens=3,
                selected_tokens=3,
                rendered_tokens=4,
                dropped_tokens=0,
            ),
        )

        class Service:
            async def retrieve(self, _query):
                return retrieval

        subject_type = _memory_method_subject("_recall_v7")
        subject_type._get_v7_retrieval_service = lambda _self: Service()
        subject_type._truncate_content = staticmethod(
            lambda content, max_length=150: content[:max_length]
        )

        async def merge(_self, result, **_kwargs):
            return result

        subject_type._merge_v7_linked_results = merge

        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "memory.db"
            connection = sqlite3.connect(database_path)
            try:
                connection.execute(
                    """
                    CREATE TABLE memory_records (
                        record_id TEXT PRIMARY KEY,
                        workspace_id TEXT NOT NULL,
                        rationale TEXT,
                        context_json TEXT NOT NULL,
                        tags_json TEXT NOT NULL,
                        worked INTEGER,
                        content_hash TEXT NOT NULL,
                        deleted_at_us INTEGER
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO memory_records VALUES (?,?,?,?,?,?,?,NULL)",
                    (
                        primary.record_id,
                        _WORKSPACE_ID,
                        "POST SNAPSHOT PRIVATE RATIONALE",
                        json.dumps(
                            {
                                "visibility": "private",
                                "secret": "POST SNAPSHOT SECRET",
                            }
                        ),
                        json.dumps(["post-snapshot-tag"]),
                        0,
                        primary.content_hash,
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            subject = subject_type()
            subject.db = SimpleNamespace(
                db_path=database_path,
                workspace_id=_WORKSPACE_ID,
            )
            result = await subject._recall_v7(
                topic="selected evidence",
                categories=None,
                tags=None,
                file_path=None,
                project_path=None,
                offset=0,
                limit=10,
                since=None,
                until=None,
                include_warnings=True,
                include_linked=False,
                condensed=False,
                as_of_time=None,
            )

        rendered = result["decisions"][0]
        self.assertEqual(primary.record_id, rendered["id"])
        self.assertEqual("Snapshot rationale", rendered["rationale"])
        self.assertIsNone(rendered["context"])
        self.assertEqual(["snapshot-tag"], rendered["tags"])
        self.assertIs(rendered["worked"], True)
        self.assertNotIn("POST SNAPSHOT", json.dumps(result))

    async def test_memory_v7_page_does_not_leak_unsliced_context(self) -> None:
        first_ref = EvidenceRef(
            record_id=_opaque("mem_", "1"),
            event_id=_opaque("evt_", "1"),
            content_hash="1" * 64,
            version_id=None,
            provider="lexical",
        )
        second_ref = EvidenceRef(
            record_id=_opaque("mem_", "2"),
            event_id=_opaque("evt_", "2"),
            content_hash="2" * 64,
            version_id=None,
            provider="lexical",
        )
        first = _item(first_ref, excerpt="FIRST PRIVATE EVIDENCE")
        second = replace(
            _item(second_ref, excerpt="selected second evidence"),
            citation="[E2]",
        )
        context_text = f"[E1] {first.excerpt}\n\n[E2] {second.excerpt}"
        second_start = len(f"[E1] {first.excerpt}\n\n[E2] ")
        retrieval = RetrievalResult(
            items=(first, second),
            context=ContextPackage(
                text=context_text,
                citations=(
                    CitationEntry(
                        marker="[E1]",
                        evidence_refs=(first_ref,),
                        channels=frozenset({"lexical"}),
                        excerpt_start=5,
                        excerpt_end=5 + len(first.excerpt),
                    ),
                    CitationEntry(
                        marker="[E2]",
                        evidence_refs=(second_ref,),
                        channels=frozenset({"lexical"}),
                        excerpt_start=second_start,
                        excerpt_end=second_start + len(second.excerpt),
                    ),
                ),
                token_budget=512,
                requested_tokens=7,
                selected_tokens=7,
                rendered_tokens=9,
                dropped_tokens=0,
            ),
        )

        class Service:
            async def retrieve(self, _query):
                return retrieval

        subject_type = _memory_method_subject("_recall_v7")
        subject_type._get_v7_retrieval_service = lambda _self: Service()

        async def merge(_self, result, **_kwargs):
            return result

        subject_type._merge_v7_linked_results = merge
        subject = subject_type()
        subject.db = SimpleNamespace(
            db_path=Path("unused.db"),
            workspace_id=_WORKSPACE_ID,
        )

        result = await subject._recall_v7(
            topic="paged evidence",
            categories=None,
            tags=None,
            file_path=None,
            project_path=None,
            offset=1,
            limit=1,
            since=None,
            until=None,
            include_warnings=True,
            include_linked=False,
            condensed=False,
            as_of_time=None,
        )

        self.assertEqual(second_ref.record_id, result["decisions"][0]["id"])
        self.assertNotIn("context", result["retrieval"])
        self.assertNotIn("citations", result["retrieval"])
        self.assertNotIn("FIRST PRIVATE EVIDENCE", json.dumps(result))

    async def test_v7_linked_recall_degrades_without_leaking_paths_or_v6_ids(
        self,
    ) -> None:
        linked_workspace = "ws_aaaaaaaaaaaaaaaaaaaaaaaa"
        raw_linked_path = r"D:\private\linked-workspace"
        linked_database = SimpleNamespace(
            workspace_id=linked_workspace,
            format_version=6,
        )

        class LinkManager:
            def __init__(self, _database):
                pass

            async def get_linked_db_managers(self, _project_path):
                return [(raw_linked_path, linked_database)]

        class LinkedMemoryManager:
            def __init__(self, _database):
                pass

            async def recall(self, **_kwargs):
                return {
                    "decisions": [
                        {
                            "id": 7,
                            "content": "legacy linked evidence",
                            "citation": "[E1]",
                        }
                    ]
                }

        subject_type = _memory_method_subject("_merge_v7_linked_results")
        subject_type._merge_v7_linked_results.__globals__["MemoryManager"] = (
            LinkedMemoryManager
        )
        fake_links = types.ModuleType("daem0nmcp.links")
        fake_links.LinkManager = LinkManager
        previous_links = sys.modules.get("daem0nmcp.links")
        sys.modules["daem0nmcp.links"] = fake_links
        try:
            subject = subject_type()
            subject.db = SimpleNamespace()
            result = await subject._merge_v7_linked_results(
                {
                    "found": 0,
                    "decisions": [],
                    "patterns": [],
                    "warnings": [],
                    "learnings": [],
                    "retrieval": {"citations": []},
                },
                topic="linked",
                categories=None,
                tags=None,
                file_path=None,
                limit=10,
                since=None,
                until=None,
                project_path=r"D:\private\source-workspace",
                include_warnings=True,
                include_linked=True,
                condensed=False,
                as_of_time=None,
            )
        finally:
            if previous_links is None:
                sys.modules.pop("daem0nmcp.links", None)
            else:
                sys.modules["daem0nmcp.links"] = previous_links

        self.assertEqual([], result["decisions"])
        self.assertEqual(
            [
                {
                    "workspace_id": linked_workspace,
                    "status": "degraded",
                    "reason": "LINKED_EVIDENCE_FEDERATION_REQUIRED",
                }
            ],
            result["retrieval"]["linked"],
        )
        serialized = json.dumps(result, sort_keys=True)
        self.assertNotIn(raw_linked_path, serialized)
        self.assertNotIn('"id": 7', serialized)


if __name__ == "__main__":
    unittest.main()
