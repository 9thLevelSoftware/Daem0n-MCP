from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from daem0nmcp.api.v7.discovery_operations import default_code_indexer_factory
from daem0nmcp.api.v7.pinned import (
    IdempotencyConflict,
    MemoryOutcomeCommand,
    MemoryStoreCommand,
)
from daem0nmcp.api.v7.runtime_services import (
    RuntimeServiceError,
    SQLiteMemoryEventWriter,
    Task8RecallService,
)
from daem0nmcp.api.v7.tools import (
    CodeRef,
    MemoryCapturePromoteInput,
    MemoryCreate,
    MemoryRecordOutcomeInput,
    MemoryStoreInput,
    OutcomeVerification,
)
from daem0nmcp.config import Settings
from daem0nmcp.event_store import (
    EventCommand,
    EventStore,
    deterministic_id,
    export_event_bundle,
    import_event_bundle,
)
from daem0nmcp.retrieval.runtime import drain_projection_jobs
from daem0nmcp.retrieval.types import RetrievalQuery

from .test_local_state_operations import NOW, _apply_v7_schema, _Fixture

LEGACY_STORE_HASH = "57a7c1ddd4407236b7f99754d9530754b7ca5b0a206ac0b6c4f7c9994f83b808"
LEGACY_OUTCOME_HASH = "9e727cd277356ecc6e381f8eb2ab3b539dc57e4eef0ee12870b55c185e372549"


def _store_command(**changes: object) -> MemoryStoreCommand:
    values = {
        "record_type": "decision",
        "content": "Use canonical events.",
        "rationale": None,
        "context": {},
        "tags": (),
        "relative_file_path": None,
        "happened_at": None,
        "procedure_steps": (),
        "idempotency_key": "learning-store-0001",
    }
    values.update(changes)
    return MemoryStoreCommand(**values)


def _outcome_command(record_id: str, **changes: object) -> MemoryOutcomeCommand:
    values = {
        "record_id": record_id,
        "outcome_text": "The change worked.",
        "worked": True,
        "happened_at": None,
        "idempotency_key": "learning-outcome-0001",
    }
    values.update(changes)
    return MemoryOutcomeCommand(**values)


class LearningInputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = {
            "workspace_id": "ws_" + "a" * 24,
            "record_type": "decision",
            "content": "Keep useful evidence.",
            "idempotency_key": "learning-input-0001",
            "preflight_token": "t" * 32,
        }
        self.outcome = {
            "workspace_id": self.store["workspace_id"],
            "record_id": "mem_" + "1" * 64,
            "outcome_text": "Verified the change.",
            "worked": True,
            "idempotency_key": "learning-input-0002",
        }

    def test_informed_by_requires_unique_bounded_record_ids(self) -> None:
        for model, arguments in (
            (MemoryStoreInput, self.store),
            (MemoryRecordOutcomeInput, self.outcome),
        ):
            for ids in (
                ["mem_" + "2" * 64] * 2,
                ["mem_" + "A" * 64],
                ["not-a-record"],
                [f"mem_{index:064x}" for index in range(33)],
            ):
                with (
                    self.subTest(model=model.__name__, ids=ids),
                    self.assertRaises(ValidationError),
                ):
                    model.model_validate({**arguments, "informed_by": ids})
            request = model.model_validate(
                {**arguments, "informed_by": ["mem_" + "2" * 64]}
            )
            self.assertEqual(["mem_" + "2" * 64], request.informed_by)

    def test_code_bindings_context_is_server_managed_on_every_creation_input(
        self,
    ) -> None:
        cases = (
            (MemoryStoreInput, self.store),
            (MemoryCreate, {"record_type": "decision", "content": "Imported note."}),
            (
                MemoryCapturePromoteInput,
                {
                    **self.store,
                    "candidate_id": "cap_" + "1" * 64,
                },
            ),
        )
        for model, arguments in cases:
            with (
                self.subTest(model=model.__name__),
                self.assertRaisesRegex(
                    ValidationError, "context.code_bindings is server-managed"
                ),
            ):
                model.model_validate({**arguments, "context": {"code_bindings": []}})

    def test_verification_rejects_unrecognized_or_inconsistent_evidence(self) -> None:
        invalid = (
            {"kind": "unknown"},
            {"kind": "test", "extra": True},
            {"kind": "test", "exit_code": 2_147_483_648},
            {"kind": "command", "exit_code": -2_147_483_649},
            {"kind": "review", "command": "pytest"},
            {"kind": "review", "exit_code": 0},
            {"kind": "self_report", "command": "pytest"},
            {"kind": "self_report", "exit_code": 1},
        )
        for evidence in invalid:
            with self.subTest(evidence=evidence), self.assertRaises(ValidationError):
                OutcomeVerification.model_validate(evidence)
        for kind in ("test", "command", "review", "self_report"):
            evidence = OutcomeVerification(kind=kind)
            self.assertEqual({"kind": kind}, evidence.model_dump(exclude_none=True))
        for exit_code in (-2_147_483_648, 2_147_483_647):
            OutcomeVerification(kind="command", command="pytest", exit_code=exit_code)

    def test_code_refs_follow_relative_path_policy_and_limit(self) -> None:
        for path in ("../outside.py", "/outside.py", "C:\\outside.py"):
            with self.subTest(path=path), self.assertRaises(ValidationError):
                CodeRef(relative_file_path=path)
        ref = {
            "relative_file_path": "src/runtime.py",
            "qualified_name": "Runtime.start",
        }
        request = MemoryStoreInput.model_validate({**self.store, "code_refs": [ref]})
        self.assertEqual("Runtime.start", request.code_refs[0].qualified_name)
        with self.assertRaises(ValidationError):
            MemoryStoreInput.model_validate({**self.store, "code_refs": [ref] * 17})


class MemoryLearningWriteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = _Fixture(Path(self.temporary.name))
        self.writer = SQLiteMemoryEventWriter(
            clock=lambda: NOW,
            projection_scheduler=lambda _path: None,
            max_workers=1,
        )
        self.addCleanup(self.writer.close)

    def _payloads(self) -> list[dict[str, object]]:
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            return [
                json.loads(row[0])
                for row in connection.execute(
                    "SELECT payload_json FROM memory_events ORDER BY stream_id,stream_version"
                )
            ]

    async def test_unknown_and_foreign_provenance_are_rejected_without_events(
        self,
    ) -> None:
        target_id = self.fixture.add_record(1)
        foreign_id = "mem_" + "2" * 64
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            record = json.loads(
                connection.execute("SELECT payload_json FROM memory_events").fetchone()[
                    0
                ]
            )["record"]
            EventStore(connection).append_and_project(
                EventCommand(
                    workspace_id="ws_" + "f" * 24,
                    stream_id=foreign_id,
                    stream_kind="memory",
                    event_type="memory.created",
                    occurred_at_us=1,
                    recorded_at_us=1,
                    actor_type="client",
                    payload={"record": record},
                )
            )
            connection.commit()
        for source in ("mem_" + "9" * 64, foreign_id):
            with self.subTest(source=source):
                with self.assertRaisesRegex(RuntimeServiceError, "INVALID_ARGUMENT"):
                    await self.writer.store(
                        self.fixture.workspace, _store_command(informed_by=(source,))
                    )
                with self.assertRaisesRegex(RuntimeServiceError, "INVALID_ARGUMENT"):
                    await self.writer.record_outcome(
                        self.fixture.workspace,
                        _outcome_command(target_id, informed_by=(source,)),
                    )
        self.assertEqual(2, len(self._payloads()))

    async def test_self_provenance_is_rejected_for_store_and_outcome(self) -> None:
        record_id = self.fixture.add_record(1)
        with self.assertRaisesRegex(RuntimeServiceError, "INVALID_ARGUMENT"):
            await self.writer.record_outcome(
                self.fixture.workspace,
                _outcome_command(record_id, informed_by=(record_id,)),
            )
        command = _store_command()
        self_id = deterministic_id(
            "mem",
            "memory-store",
            self.fixture.workspace.workspace_id,
            command.idempotency_key,
        )
        with self.assertRaisesRegex(RuntimeServiceError, "INVALID_ARGUMENT"):
            await self.writer.store(
                self.fixture.workspace, replace(command, informed_by=(self_id,))
            )
        self.assertEqual(1, len(self._payloads()))

    async def test_missing_code_ref_and_invalid_rebind_are_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeServiceError, "INVALID_ARGUMENT"):
            await self.writer.store(
                self.fixture.workspace,
                _store_command(code_refs=(("src/missing.py", None),)),
            )
        record_id = self.fixture.add_record(1)
        for worked in (False, True):
            with (
                self.subTest(worked=worked),
                self.assertRaisesRegex(RuntimeServiceError, "INVALID_ARGUMENT"),
            ):
                await self.writer.record_outcome(
                    self.fixture.workspace,
                    _outcome_command(record_id, worked=worked, rebind_code=True),
                )
        self.assertEqual(1, len(self._payloads()))

    async def test_environmental_code_ref_failure_is_atomic_and_restorable(
        self,
    ) -> None:
        first = self.fixture.root / "first.txt"
        blocked = self.fixture.root / "blocked.txt"
        first.write_bytes(b"first")
        blocked.write_bytes(b"unchanged")
        command = _store_command(code_refs=(("first.txt", None), ("blocked.txt", None)))
        original_open = type(blocked).open

        def refuse(path, *args, **kwargs):
            if path == blocked:
                raise PermissionError("temporary sharing violation")
            return original_open(path, *args, **kwargs)

        with (
            patch.object(type(blocked), "open", refuse),
            self.assertRaisesRegex(RuntimeServiceError, "CAPABILITY_DEGRADED"),
        ):
            await self.writer.store(self.fixture.workspace, command)
        self.assertEqual([], self._payloads())
        stored = await self.writer.store(self.fixture.workspace, command)
        before = self._payloads()
        with (
            patch.object(type(blocked), "open", refuse),
            self.assertRaisesRegex(RuntimeServiceError, "CAPABILITY_DEGRADED"),
        ):
            await self.writer.record_outcome(
                self.fixture.workspace,
                _outcome_command(stored.record.record_id, rebind_code=True),
            )
        self.assertEqual(before, self._payloads())
        await self.writer.record_outcome(
            self.fixture.workspace,
            _outcome_command(stored.record.record_id, rebind_code=True),
        )

    async def test_oversized_code_ref_capture_is_invalid_and_atomic(self) -> None:
        first = self.fixture.root / "first.txt"
        large = self.fixture.root / "large.txt"
        first.write_bytes(b"ok")
        large.write_bytes(b"x\r\n" * 4)
        with (
            patch("daem0nmcp.code_bindings._MAX_BINDING_FILE_BYTES", 8),
            self.assertRaisesRegex(RuntimeServiceError, "INVALID_ARGUMENT"),
        ):
            await self.writer.store(
                self.fixture.workspace,
                _store_command(code_refs=(("first.txt", None), ("large.txt", None))),
            )
        self.assertEqual([], self._payloads())

    async def test_later_missing_shared_source_symbol_is_atomic_and_restorable(
        self,
    ) -> None:
        if not default_code_indexer_factory().available:
            self.skipTest("code parser is unavailable")
        source = self.fixture.root / "handler.py"
        first = "def first():\n    return 1\n"
        both = first + "\ndef second():\n    return 2\n"
        command = _store_command(
            code_refs=(("handler.py", "first"), ("./handler.py", "second"))
        )

        def records():
            with closing(sqlite3.connect(self.fixture.database)) as connection:
                return connection.execute(
                    "SELECT * FROM memory_records ORDER BY record_id"
                ).fetchall()

        source.write_text(first, encoding="utf-8")
        before_records = records()
        with self.assertRaisesRegex(RuntimeServiceError, "INVALID_ARGUMENT"):
            await self.writer.store(self.fixture.workspace, command)
        self.assertEqual([], self._payloads())
        self.assertEqual(before_records, records())
        source.write_text(both, encoding="utf-8")
        stored = await self.writer.store(self.fixture.workspace, command)
        before_events = self._payloads()
        before_records = records()
        source.write_text(first, encoding="utf-8")
        outcome = _outcome_command(stored.record.record_id, rebind_code=True)
        with self.assertRaisesRegex(RuntimeServiceError, "INVALID_ARGUMENT"):
            await self.writer.record_outcome(self.fixture.workspace, outcome)
        self.assertEqual(before_events, self._payloads())
        self.assertEqual(before_records, records())
        source.write_text(both.replace("return 2", "return 3"), encoding="utf-8")
        await self.writer.record_outcome(self.fixture.workspace, outcome)
        self.assertEqual(2, len(self._payloads()))

    async def _assert_missing_parser_writes(self) -> None:
        self.assertFalse(default_code_indexer_factory().available)
        source = self.fixture.root / "handler.py"
        source.write_text("def handler():\n    return 1\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeServiceError, "CAPABILITY_DEGRADED"):
            await self.writer.store(
                self.fixture.workspace,
                _store_command(code_refs=(("handler.py", "handler"),)),
            )
        self.assertEqual([], self._payloads())
        stored = await self.writer.store(
            self.fixture.workspace,
            _store_command(
                context={
                    "code_bindings": [
                        {
                            "relative_file_path": "handler.py",
                            "qualified_name": "handler",
                            "fingerprint": "0" * 64,
                            "head_commit": None,
                        }
                    ]
                }
            ),
        )
        before = self._payloads()
        with self.assertRaisesRegex(RuntimeServiceError, "CAPABILITY_DEGRADED"):
            await self.writer.record_outcome(
                self.fixture.workspace,
                _outcome_command(stored.record.record_id, rebind_code=True),
            )
        self.assertEqual(before, self._payloads())
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            self.assertEqual(
                (None, None),
                connection.execute(
                    "SELECT outcome, worked FROM memory_records WHERE record_id=?",
                    (stored.record.record_id,),
                ).fetchone(),
            )

    def test_package_absence_capture_and_rebind_fail_without_events(self) -> None:
        script = (
            "import sys; sys.modules['tree_sitter_language_pack'] = None\n"
            "import unittest\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, str(Path.cwd() / 'tests'))\n"
            "from api_v7.test_memory_learning_writes import "
            "MemoryLearningWriteTests\n"
            "suite = unittest.TestSuite([MemoryLearningWriteTests("
            "'_assert_missing_parser_writes')])\n"
            "result = unittest.TextTestRunner().run(suite)\n"
            "sys.exit(0 if result.wasSuccessful() else 1)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    async def test_real_parser_failure_and_reference_errors_are_atomic(self) -> None:
        if not default_code_indexer_factory().available:
            self.skipTest("code parser is unavailable")
        source = self.fixture.root / "handler.py"
        source.write_text("def handler():\n    return 1\n", encoding="utf-8")
        stored = await self.writer.store(
            self.fixture.workspace,
            _store_command(code_refs=(("handler.py", "handler"),)),
        )
        before = self._payloads()
        cases = (
            ("handler.py", "def handler(:\n", "CAPABILITY_DEGRADED"),
            ("handler.py", "def other():\n    return 1\n", "INVALID_ARGUMENT"),
            ("handler.md", "# handler\n", "INVALID_ARGUMENT"),
            (
                "handler.py",
                "class A:\n    def handler(self):\n        return 1\n\n"
                "class B:\n    def handler(self):\n        return 2\n",
                "INVALID_ARGUMENT",
            ),
        )
        for index, (relative, content, code) in enumerate(cases):
            with self.subTest(relative=relative, content=content):
                target = self.fixture.root / relative
                target.write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(RuntimeServiceError, code):
                    await self.writer.store(
                        self.fixture.workspace,
                        _store_command(
                            idempotency_key=f"capture-failure-{index:04d}",
                            code_refs=(("handler.py", None), (relative, "handler")),
                        ),
                    )
                self.assertEqual(before, self._payloads())
                if relative == "handler.py":
                    with self.assertRaisesRegex(RuntimeServiceError, code):
                        await self.writer.record_outcome(
                            self.fixture.workspace,
                            _outcome_command(stored.record.record_id, rebind_code=True),
                        )
                    self.assertEqual(before, self._payloads())
        source.write_text("def handler():\n    return 2\n", encoding="utf-8")
        await self.writer.record_outcome(
            self.fixture.workspace,
            _outcome_command(stored.record.record_id, rebind_code=True),
        )
        self.assertEqual(2, len(self._payloads()))

    async def test_unsupported_symbol_rebind_is_a_client_error(self) -> None:
        if not default_code_indexer_factory().available:
            self.skipTest("code parser is unavailable")
        source = self.fixture.root / "handler.md"
        source.write_text("# handler\n", encoding="utf-8")
        stored = await self.writer.store(
            self.fixture.workspace,
            _store_command(
                context={
                    "code_bindings": [
                        {
                            "relative_file_path": "handler.md",
                            "qualified_name": "handler",
                            "fingerprint": "0" * 64,
                            "head_commit": None,
                        }
                    ]
                }
            ),
        )
        before = self._payloads()
        with self.assertRaisesRegex(RuntimeServiceError, "INVALID_ARGUMENT"):
            await self.writer.record_outcome(
                self.fixture.workspace,
                _outcome_command(stored.record.record_id, rebind_code=True),
            )
        self.assertEqual(before, self._payloads())

    async def _recall_binding(self, mode: str):
        config = Settings(
            memory_validity_mode=mode,
            retrieval_utility_mode="off",
            retrieval_rerank_enabled=False,
        )
        statuses = {
            "local": "disabled",
            "models-local": "disabled",
            "graph": "disabled",
        }
        while await drain_projection_jobs(
            self.fixture.database,
            config=config,
            max_jobs=100,
            include_optional=False,
            capability_statuses=statuses,
        ):
            pass
        adapter = Task8RecallService(config=config, capability_statuses=statuses)
        try:
            return await adapter.retrieve(
                self.fixture.workspace,
                RetrievalQuery(
                    self.fixture.workspace.workspace_id, "bound handler guidance"
                ),
                frozenset(),
            )
        finally:
            adapter.close()

    async def _assert_binding_rebind(self, qualified_name: str | None) -> None:
        source = self.fixture.root / "handler.py"
        source.write_text(
            "def handler(value):\n    return value + 1\n", encoding="utf-8"
        )
        stored = await self.writer.store(
            self.fixture.workspace,
            _store_command(
                content="bound handler guidance",
                code_refs=(("handler.py", qualified_name),),
            ),
        )
        bindings = self._payloads()[0]["record"]["context"]["code_bindings"]
        self.assertEqual("handler.py", bindings[0]["relative_file_path"])
        original = await self._recall_binding("shadow")
        self.assertEqual("current", original.items[0].applicability)
        source.write_text(
            "def handler(value):\n    return value - 22\n", encoding="utf-8"
        )
        shadow = await self._recall_binding("shadow")
        expected = "handler.py" + (f"::{qualified_name}" if qualified_name else "")
        self.assertEqual("needs_revalidation", shadow.items[0].applicability)
        self.assertEqual([expected], shadow.items[0].changed_bindings)
        self.assertEqual(original.rendered_context, shadow.rendered_context)
        self.assertIn(
            "VALIDITY_SHADOW", [d.reason for d in shadow.provider_diagnostics]
        )
        disabled = await self._recall_binding("off")
        self.assertIsNone(disabled.items[0].applicability)
        self.assertEqual(original.rendered_context, disabled.rendered_context)
        with patch(
            "daem0nmcp.retrieval.service.BindingEvaluator.evaluate",
            side_effect=RuntimeError("binding evaluation failed"),
        ):
            degraded = await self._recall_binding("apply")
        self.assertIsNone(degraded.items[0].applicability)
        self.assertEqual(original.rendered_context, degraded.rendered_context)
        diagnostic = next(
            d for d in degraded.provider_diagnostics if d.provider == "validity"
        )
        self.assertEqual("degraded", diagnostic.status)
        self.assertEqual("VALIDITY_FAILED", diagnostic.reason)
        self.assertEqual(0, diagnostic.returned_count)
        applied = await self._recall_binding("apply")
        self.assertIn("Needs revalidation: " + expected, applied.rendered_context)
        self.assertIn(
            "VALIDITY_APPLIED", [d.reason for d in applied.provider_diagnostics]
        )
        with self.assertRaisesRegex(RuntimeServiceError, "INVALID_ARGUMENT"):
            await self.writer.record_outcome(
                self.fixture.workspace,
                _outcome_command(
                    stored.record.record_id, worked=False, rebind_code=True
                ),
            )
        await self.writer.record_outcome(
            self.fixture.workspace,
            _outcome_command(stored.record.record_id, rebind_code=True),
        )
        refreshed = await self._recall_binding("apply")
        self.assertEqual("current", refreshed.items[0].applicability)
        self.assertEqual([], refreshed.items[0].changed_bindings)
        self.assertNotIn("Needs revalidation:", refreshed.rendered_context)
        self.assertNotEqual(
            bindings[0]["fingerprint"],
            self._payloads()[-1]["record"]["context"]["code_bindings"][0][
                "fingerprint"
            ],
        )

    async def test_file_binding_recall_and_rebind(self) -> None:
        await self._assert_binding_rebind(None)

    async def test_symbol_binding_recall_and_rebind(self) -> None:
        if not default_code_indexer_factory().available:
            self.skipTest("code parser is unavailable")
        await self._assert_binding_rebind("handler.handler")

    async def test_bare_symbol_binding_recall_and_rebind(self) -> None:
        if not default_code_indexer_factory().available:
            self.skipTest("code parser is unavailable")
        await self._assert_binding_rebind("handler")

    async def test_preexisting_store_hash_replays_with_default_new_fields(self) -> None:
        source_id = self.fixture.add_record(1)
        command = _store_command()
        workspace_id = self.fixture.workspace.workspace_id
        record_id = deterministic_id(
            "mem", "memory-store", workspace_id, command.idempotency_key
        )
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            original = json.loads(
                connection.execute(
                    "SELECT payload_json FROM memory_events WHERE stream_id=?",
                    (source_id,),
                ).fetchone()[0]
            )["record"]
            original.update(
                content=command.content,
                rationale=None,
                context={},
                tags=[],
                file_path_relative=None,
            )
            event = EventStore(connection).append_and_project(
                EventCommand(
                    workspace_id=workspace_id,
                    stream_id=record_id,
                    stream_kind="memory",
                    event_type="memory.created",
                    occurred_at_us=1,
                    recorded_at_us=1,
                    actor_type="client",
                    payload={
                        "record": original,
                        "idempotency_request_hash": LEGACY_STORE_HASH,
                    },
                    correlation_id=deterministic_id(
                        "job",
                        "memory-store-idempotency",
                        workspace_id,
                        command.idempotency_key,
                    ),
                )
            )
            connection.commit()
        replay = await self.writer.store(self.fixture.workspace, command)
        self.assertTrue(replay.idempotent_replay)
        self.assertEqual(event, replay.event)
        self.assertEqual(2, len(self._payloads()))

    async def test_preexisting_outcome_hash_replays_with_default_new_fields(
        self,
    ) -> None:
        record_id = self.fixture.add_record(1)
        command = _outcome_command(record_id)
        workspace_id = self.fixture.workspace.workspace_id
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            record = json.loads(
                connection.execute("SELECT payload_json FROM memory_events").fetchone()[
                    0
                ]
            )["record"]
            record.update(outcome=command.outcome_text, worked=command.worked)
            event = EventStore(connection).append_and_project(
                EventCommand(
                    workspace_id=workspace_id,
                    stream_id=record_id,
                    stream_kind="memory",
                    event_type="memory.outcome_recorded",
                    occurred_at_us=2,
                    recorded_at_us=2,
                    actor_type="client",
                    payload={
                        "record": record,
                        "idempotency_request_hash": LEGACY_OUTCOME_HASH,
                    },
                    correlation_id=deterministic_id(
                        "job",
                        "memory-record-outcome-idempotency",
                        workspace_id,
                        command.idempotency_key,
                    ),
                )
            )
            connection.commit()
        replay = await self.writer.record_outcome(self.fixture.workspace, command)
        self.assertTrue(replay.idempotent_replay)
        self.assertEqual(event, replay.event)
        self.assertEqual(2, len(self._payloads()))

    async def test_provenance_and_verification_are_canonical_and_hash_bound(
        self,
    ) -> None:
        sources = (self.fixture.add_record(2), self.fixture.add_record(1))
        command = _store_command(informed_by=sources)
        stored = await self.writer.store(self.fixture.workspace, command)
        replay = await self.writer.store(
            self.fixture.workspace,
            replace(command, informed_by=tuple(reversed(sources))),
        )
        self.assertTrue(replay.idempotent_replay)
        self.assertEqual(stored.event, replay.event)
        with self.assertRaises(IdempotencyConflict):
            await self.writer.store(
                self.fixture.workspace, replace(command, informed_by=sources[:1])
            )
        outcome = _outcome_command(
            stored.record.record_id,
            informed_by=sources,
            verification={"kind": "test", "command": "pytest", "exit_code": 0},
        )
        recorded = await self.writer.record_outcome(self.fixture.workspace, outcome)
        replayed = await self.writer.record_outcome(
            self.fixture.workspace,
            replace(outcome, informed_by=tuple(reversed(sources))),
        )
        self.assertTrue(replayed.idempotent_replay)
        self.assertEqual(recorded.event, replayed.event)
        for changed in (
            replace(outcome, informed_by=sources[:1]),
            replace(outcome, verification=None),
            replace(outcome, verification={"kind": "review"}),
        ):
            with self.assertRaises(IdempotencyConflict):
                await self.writer.record_outcome(self.fixture.workspace, changed)
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            for event in (stored.event, recorded.event):
                payload = json.loads(
                    connection.execute(
                        "SELECT payload_json FROM memory_events WHERE event_id=?",
                        (event.event_id,),
                    ).fetchone()[0]
                )
                self.assertEqual(
                    {"informed_by": sorted(sources)}, payload["provenance"]
                )
            self.assertEqual(dict(outcome.verification), payload["verification"])
            signal = connection.execute(
                "SELECT reward,weight FROM memory_outcome_signals WHERE event_id=?",
                (recorded.event.event_id,),
            ).fetchone()
            self.assertEqual((1.0, 1.0), signal)

    async def test_bundle_replay_reproduces_learning_projections_exactly(self) -> None:
        source_id = self.fixture.add_record(1)
        child = await self.writer.store(
            self.fixture.workspace, _store_command(informed_by=(source_id,))
        )
        for index, (worked, evidence) in enumerate(
            ((True, {"kind": "test", "exit_code": 0}), (False, {"kind": "review"})), 1
        ):
            await self.writer.record_outcome(
                self.fixture.workspace,
                _outcome_command(
                    child.record.record_id,
                    worked=worked,
                    informed_by=(source_id,),
                    verification=evidence,
                    idempotency_key=f"learning-outcome-{index:04d}",
                    happened_at=NOW + timedelta(seconds=index),
                ),
            )
        with (
            closing(sqlite3.connect(self.fixture.database)) as source,
            closing(sqlite3.connect(":memory:")) as target,
        ):
            source.row_factory = sqlite3.Row
            target.row_factory = sqlite3.Row
            target.execute("PRAGMA foreign_keys=ON")
            _apply_v7_schema(target)
            bundle = export_event_bundle(source, self.fixture.workspace.workspace_id)
            imported = import_event_bundle(
                target, bundle, self.fixture.workspace.workspace_id
            )
            target.commit()
            self.assertEqual(4, imported.events_imported)
            for table, order in (
                ("memory_provenance_edges", "event_id,informed_by_record_id"),
                ("memory_outcome_signals", "event_id"),
            ):
                query = f"SELECT * FROM {table} ORDER BY {order}"
                expected = [tuple(row) for row in source.execute(query)]
                self.assertTrue(expected)
                self.assertEqual(
                    expected, [tuple(row) for row in target.execute(query)]
                )
            self.assertIsNone(target.execute("PRAGMA foreign_key_check").fetchone())
            replay = import_event_bundle(
                target, bundle, self.fixture.workspace.workspace_id
            )
            self.assertEqual(0, replay.events_imported)
            self.assertEqual(4, replay.events_existing)
