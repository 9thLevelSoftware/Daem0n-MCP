from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from importlib.util import find_spec
from pathlib import Path
from types import MappingProxyType
from unittest.mock import ANY, AsyncMock, patch

import httpx

from daem0nmcp import pinned_http
from daem0nmcp.api.v7.application import AdmittedRequest

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
PREFLIGHT_TOKEN = "t" * 32


def _apply_v7_schema(connection: sqlite3.Connection) -> None:
    from daem0nmcp.migrations.schema import MIGRATIONS
    from daem0nmcp.schema_version import CURRENT_SCHEMA_VERSION

    connection.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
    for version in range(16, CURRENT_SCHEMA_VERSION + 1):
        migration = next(item for item in MIGRATIONS if item[0] == version)
        for statement in migration[2]:
            connection.execute(statement)
        connection.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))
    connection.commit()


def _request(tool_name: str, **arguments: object) -> AdmittedRequest:
    from daem0nmcp.api.v7.tools import TOOL_INPUT_MODELS

    model = TOOL_INPUT_MODELS[tool_name].model_validate(arguments)
    effective = model.model_dump(mode="json")
    effective.pop("preflight_token", None)
    return AdmittedRequest(tool_name, MappingProxyType(effective))


class DocumentFetchTests(unittest.IsolatedAsyncioTestCase):
    def test_document_text_normalizes_page_breaks(self) -> None:
        from daem0nmcp.api.v7.external_operations import _document_text

        self.assertEqual(
            _document_text(b"page one\fpage two\vline two", "text/plain"),
            "page one\npage two\nline two",
        )

    def test_document_text_rejects_unsafe_control_characters(self) -> None:
        from daem0nmcp.api.v7.external_operations import (
            ExternalOperationError,
            _document_text,
        )

        with self.assertRaises(ExternalOperationError) as caught:
            _document_text(b"before\x00after", "text/plain")

        self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")

    async def _fetch(
        self,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        content: bytes = b"bounded document",
        url: str = "https://docs.example.test/guide?version=7&format=raw",
    ) -> tuple[bytes, str]:
        from daem0nmcp.api.v7 import external_operations

        observed: list[httpx.Request] = []

        class BodyStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield content

        def respond(request: httpx.Request) -> httpx.Response:
            observed.append(request)
            return httpx.Response(
                status,
                headers=headers or {"content-type": "text/plain"},
                stream=BodyStream(),
            )

        transport = httpx.MockTransport(respond)
        with (
            patch.object(
                pinned_http,
                "validate_public_url",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                pinned_http,
                "PinnedAsyncHTTPTransport",
                return_value=transport,
            ),
        ):
            result = await external_operations._fetch_document(url)

        self.assertEqual(len(observed), 1)
        self.assertEqual(
            observed[0].url.raw_path,
            b"/guide?version=7&format=raw",
        )
        self.assertEqual(observed[0].headers["accept-encoding"], "identity")
        return result

    async def test_fetch_streams_identity_body_and_preserves_query_target(self) -> None:
        body, content_type = await self._fetch()

        self.assertEqual(body, b"bounded document")
        self.assertEqual(content_type, "text/plain")

    async def test_fetch_rejects_redirect_without_following_it(self) -> None:
        from daem0nmcp.api.v7.external_operations import ExternalOperationError

        with self.assertRaises(ExternalOperationError) as caught:
            await self._fetch(
                status=302,
                headers={
                    "content-type": "text/plain",
                    "location": "http://127.0.0.1/private",
                },
            )

        self.assertEqual(caught.exception.code, "NOT_FOUND")

    async def test_fetch_rejects_unreviewed_media_type_before_ingestion(self) -> None:
        from daem0nmcp.api.v7.external_operations import ExternalOperationError

        with self.assertRaises(ExternalOperationError) as caught:
            await self._fetch(headers={"content-type": "application/octet-stream"})

        self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")

    async def test_fetch_rejects_declared_oversize_before_reading_body(self) -> None:
        from daem0nmcp.api.v7.external_operations import ExternalOperationError

        with self.assertRaises(ExternalOperationError) as caught:
            await self._fetch(
                headers={
                    "content-type": "text/plain",
                    "content-length": "1000001",
                },
            )

        self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")

    async def test_fetch_rejects_streamed_oversize_at_exact_byte_boundary(self) -> None:
        from daem0nmcp.api.v7.external_operations import ExternalOperationError

        with self.assertRaises(ExternalOperationError) as caught:
            await self._fetch(content=b"x" * 1_000_001)

        self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")


class ExternalDocumentOperationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from daem0nmcp.retrieval.runtime import await_projection_job_drains
        from daem0nmcp.storage_activation import (
            ActiveDatabasePointer,
            write_active_pointer,
        )
        from daem0nmcp.workspace import WorkspaceRegistry

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        storage = self.root / ".daem0nmcp" / "storage"
        storage.mkdir(parents=True)
        self.database = storage / "daem0nmcp.db"
        self.addAsyncCleanup(await_projection_job_drains, (self.database,))
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            _apply_v7_schema(connection)
        write_active_pointer(
            storage,
            ActiveDatabasePointer(7, 1, self.database.name, None, None),
        )
        self.workspace = WorkspaceRegistry([self.root], default_root=self.root).default

    def _operations(self):
        from daem0nmcp.api.v7.external_operations import (
            ExternalOperationDependencies,
            build_external_operations,
        )
        from daem0nmcp.api.v7.record_operations import RecordOperationDependencies

        return build_external_operations(
            ExternalOperationDependencies(
                record_dependencies=RecordOperationDependencies(clock=lambda: NOW),
                environment={},
            )
        )

    def _document_request(self, *, key: str = "document-ingest-0001"):
        return _request(
            "document_ingest_url",
            workspace_id=self.workspace.workspace_id,
            url="https://docs.example.test/v7",
            topic="v7 operations",
            chunk_size=256,
            idempotency_key=key,
            preflight_token=PREFLIGHT_TOKEN,
        )

    async def test_ingest_commits_all_chunks_with_provenance_and_replays_receipt(
        self,
    ) -> None:
        from daem0nmcp.api.v7 import external_operations

        raw = ("alpha " * 100).encode()
        operation = self._operations()["document_ingest_url"]
        with patch.object(
            external_operations,
            "_fetch_document",
            new=AsyncMock(return_value=(raw, "text/plain")),
        ):
            first = await operation(
                workspace=self.workspace,
                request=self._document_request(),
            )
            replay = await operation(
                workspace=self.workspace,
                request=self._document_request(),
            )

        self.assertEqual(first.event_ids, replay.event_ids)
        self.assertEqual(3, len(first.event_ids))
        with closing(sqlite3.connect(self.database)) as connection:
            rows = connection.execute(
                "SELECT stream_id,correlation_id,payload_json FROM memory_events "
                "ORDER BY rowid"
            ).fetchall()
        self.assertEqual(3, len(rows))
        for index, row in enumerate(rows):
            import json

            payload = json.loads(row[2])
            self.assertEqual(payload["semantic_namespace"], "document-ingest-url")
            self.assertEqual(payload["batch_index"], index)
            self.assertEqual(payload["batch_size"], 3)
            self.assertEqual(
                payload["provenance"],
                {
                    "chunk_size": 256,
                    "content_hash": (
                        "a57d5aaa71de2ff28b00f7530e4a3936"
                        "abd519e7bcf77da11bf03b7b056b9ea1"
                    ),
                    "content_type": "text/plain",
                    "topic": "v7 operations",
                    "url": "https://docs.example.test/v7",
                },
            )
            self.assertEqual(
                payload["record"]["context"]["document"],
                payload["provenance"],
            )
            self.assertEqual(payload["record"]["context"]["chunk_index"], index)

    async def test_ingest_key_rebinding_is_atomic_conflict(self) -> None:
        from daem0nmcp.api.v7 import external_operations
        from daem0nmcp.api.v7.external_operations import ExternalOperationError

        operation = self._operations()["document_ingest_url"]
        fetch = AsyncMock(return_value=(b"original", "text/plain"))
        with patch.object(external_operations, "_fetch_document", new=fetch):
            original = await operation(
                workspace=self.workspace,
                request=self._document_request(),
            )
            fetch.return_value = (b"rebound", "text/plain")
            with self.assertRaises(ExternalOperationError) as caught:
                await operation(
                    workspace=self.workspace,
                    request=self._document_request(),
                )

        self.assertEqual(caught.exception.code, "IDEMPOTENCY_CONFLICT")
        with closing(sqlite3.connect(self.database)) as connection:
            event_ids = [
                row[0]
                for row in connection.execute(
                    "SELECT event_id FROM memory_events ORDER BY rowid"
                )
            ]
        self.assertEqual(event_ids, original.event_ids)


@unittest.skipIf(
    find_spec("e2b_code_interpreter") is None,
    "e2b_code_interpreter ([agency-e2b] extra) is not installed",
)
class ModernE2BProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_sdk_result_shape_is_bounded_and_sandbox_is_isolated(
        self,
    ) -> None:
        from e2b_code_interpreter import AsyncSandbox
        from e2b_code_interpreter.models import Execution, Logs, Result

        from daem0nmcp.api.v7.external_operations import _ModernE2BProvider

        execution = Execution(
            results=[Result(text="interactive-result")],
            logs=Logs(stdout=["printed\n"], stderr=["warning\n"]),
        )
        sandbox = AsyncMock()
        sandbox.run_code.return_value = execution
        sandbox.kill.return_value = True

        with patch.object(
            AsyncSandbox,
            "create",
            new=AsyncMock(return_value=sandbox),
        ) as create:
            result = await _ModernE2BProvider("e2b_test_key").execute(
                code="print('printed')",
                timeout_seconds=7,
            )

        self.assertEqual(result["stdout"], "printed\n")
        self.assertEqual(result["stderr"], "warning\n")
        self.assertTrue(result["success"])
        self.assertEqual(result["exit_status"], 0)
        create.assert_awaited_once_with(
            timeout=12,
            secure=True,
            allow_internet_access=False,
            envs={},
            lifecycle={"on_timeout": "kill"},
            api_key="e2b_test_key",
            request_timeout=10.0,
            retries=0,
        )
        sandbox.run_code.assert_awaited_once_with(
            "print('printed')",
            on_stdout=ANY,
            on_stderr=ANY,
            on_result=ANY,
            on_error=ANY,
            timeout=7.0,
            request_timeout=10.0,
            envs={},
        )
        sandbox.kill.assert_awaited_once_with(request_timeout=5.0, retries=0)

    async def test_sdk_execution_error_uses_logs_and_omits_traceback_from_sanitized_logs(
        self,
    ) -> None:
        from e2b_code_interpreter import AsyncSandbox
        from e2b_code_interpreter.models import Execution, ExecutionError, Logs

        from daem0nmcp.api.v7.external_operations import _ModernE2BProvider

        execution = Execution(
            logs=Logs(stdout=[], stderr=["user stderr\n"]),
            error=ExecutionError(
                "ValueError",
                "bad input",
                "private traceback must not be returned",
            ),
        )
        sandbox = AsyncMock()
        sandbox.run_code.return_value = execution
        sandbox.kill.return_value = True
        with patch.object(
            AsyncSandbox,
            "create",
            new=AsyncMock(return_value=sandbox),
        ):
            result = await _ModernE2BProvider("e2b_test_key").execute(
                code="raise ValueError('bad input')",
                timeout_seconds=2,
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["exit_status"], 1)
        self.assertIn("user stderr", result["stderr"])
        self.assertIn("ValueError: bad input", result["stderr"])
        self.assertNotIn("private traceback", "\n".join(result["logs"]))

    async def test_cancellation_waits_for_cleanup_even_when_cancelled_again(
        self,
    ) -> None:
        from e2b_code_interpreter import AsyncSandbox

        from daem0nmcp.api.v7.external_operations import _ModernE2BProvider

        run_started = asyncio.Event()
        kill_started = asyncio.Event()
        release_kill = asyncio.Event()

        async def run_code(*_args, **_kwargs):
            run_started.set()
            await asyncio.Event().wait()

        async def kill(**_kwargs):
            kill_started.set()
            await release_kill.wait()
            return True

        sandbox = AsyncMock()
        sandbox.run_code.side_effect = run_code
        sandbox.kill.side_effect = kill
        with patch.object(
            AsyncSandbox,
            "create",
            new=AsyncMock(return_value=sandbox),
        ):
            task = asyncio.create_task(
                _ModernE2BProvider("e2b_test_key").execute(
                    code="while True: pass",
                    timeout_seconds=30,
                )
            )
            await asyncio.wait_for(run_started.wait(), timeout=1)
            task.cancel()
            await asyncio.wait_for(kill_started.wait(), timeout=1)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release_kill.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

        sandbox.kill.assert_awaited_once()

    async def test_cleanup_failure_prevents_false_success(self) -> None:
        from e2b_code_interpreter import AsyncSandbox
        from e2b_code_interpreter.models import Execution, Logs

        from daem0nmcp.api.v7.external_operations import (
            ExternalOperationError,
            _ModernE2BProvider,
        )

        sandbox = AsyncMock()
        sandbox.run_code.return_value = Execution(logs=Logs(stdout=["ok\n"]))
        sandbox.kill.side_effect = RuntimeError("kill failed")
        with (
            patch.object(
                AsyncSandbox,
                "create",
                new=AsyncMock(return_value=sandbox),
            ),
            self.assertRaises(ExternalOperationError) as caught,
        ):
            await _ModernE2BProvider("e2b_test_key").execute(
                code="print('ok')",
                timeout_seconds=2,
            )

        self.assertEqual(caught.exception.code, "CAPABILITY_DEGRADED")

    async def test_cleanup_timeout_cancels_kill_task_and_prevents_false_success(
        self,
    ) -> None:
        from e2b_code_interpreter import AsyncSandbox
        from e2b_code_interpreter.models import Execution, Logs

        from daem0nmcp.api.v7 import external_operations
        from daem0nmcp.api.v7.external_operations import (
            ExternalOperationError,
            _ModernE2BProvider,
        )

        kill_cancelled = asyncio.Event()

        async def never_kill(**_kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                kill_cancelled.set()

        sandbox = AsyncMock()
        sandbox.run_code.return_value = Execution(logs=Logs(stdout=["ok\n"]))
        sandbox.kill.side_effect = never_kill
        with (
            patch.object(
                AsyncSandbox,
                "create",
                new=AsyncMock(return_value=sandbox),
            ),
            patch.object(external_operations, "_E2B_CLEANUP_TIMEOUT_SECONDS", 0.01),
            self.assertRaises(ExternalOperationError) as caught,
        ):
            await asyncio.wait_for(
                _ModernE2BProvider("e2b_test_key").execute(
                    code="print('ok')",
                    timeout_seconds=2,
                ),
                timeout=0.5,
            )

        self.assertEqual(caught.exception.code, "CAPABILITY_DEGRADED")
        self.assertTrue(kill_cancelled.is_set())

    async def test_execution_deadline_is_bounded_and_cleanup_still_runs(self) -> None:
        from e2b_code_interpreter import AsyncSandbox

        from daem0nmcp.api.v7 import external_operations
        from daem0nmcp.api.v7.external_operations import (
            ExternalOperationError,
            _ModernE2BProvider,
        )

        async def never_execute(*_args, **_kwargs):
            await asyncio.Event().wait()

        sandbox = AsyncMock()
        sandbox.run_code.side_effect = never_execute
        sandbox.kill.return_value = True
        with (
            patch.object(
                AsyncSandbox,
                "create",
                new=AsyncMock(return_value=sandbox),
            ),
            patch.object(external_operations, "_E2B_EXECUTION_GRACE_SECONDS", 0.01),
            self.assertRaises(ExternalOperationError) as caught,
        ):
            await asyncio.wait_for(
                _ModernE2BProvider("e2b_test_key").execute(
                    code="while True: pass",
                    timeout_seconds=0,
                ),
                timeout=0.5,
            )

        self.assertEqual(caught.exception.code, "DEADLINE_EXCEEDED")
        self.assertEqual(sandbox.run_code.await_count, 1)
        sandbox.kill.assert_awaited_once()

    async def test_stdout_stderr_and_log_collections_are_bounded(self) -> None:
        from e2b_code_interpreter import AsyncSandbox
        from e2b_code_interpreter.models import Execution, Logs

        from daem0nmcp.api.v7.external_operations import _ModernE2BProvider

        sandbox = AsyncMock()
        sandbox.run_code.return_value = Execution(
            logs=Logs(
                stdout=["x" * 120_000],
                stderr=["y" * 120_000],
            )
        )
        sandbox.kill.return_value = True
        with patch.object(
            AsyncSandbox,
            "create",
            new=AsyncMock(return_value=sandbox),
        ):
            result = await _ModernE2BProvider("e2b_test_key").execute(
                code="print('large')",
                timeout_seconds=2,
            )

        self.assertEqual(len(result["stdout"]), 100_000)
        self.assertEqual(len(result["stderr"]), 100_000)
        self.assertEqual([len(item) for item in result["logs"]], [4096, 4096])

    async def test_streaming_output_limit_aborts_sdk_accumulation_and_cleans_up(
        self,
    ) -> None:
        from e2b_code_interpreter import AsyncSandbox, OutputMessage

        from daem0nmcp.api.v7.external_operations import (
            ExternalOperationError,
            _ModernE2BProvider,
        )

        emitted = 0

        async def emit_unbounded_output(
            _code: str,
            *,
            on_stdout,
            on_stderr,
            **_kwargs,
        ):
            del on_stderr
            nonlocal emitted
            for emitted in range(1, 10_001):
                on_stdout(OutputMessage("x" * 1_000, emitted))
            self.fail("the SDK stream callback did not stop unbounded output")

        sandbox = AsyncMock()
        sandbox.run_code.side_effect = emit_unbounded_output
        sandbox.kill.return_value = True
        with (
            patch.object(
                AsyncSandbox,
                "create",
                new=AsyncMock(return_value=sandbox),
            ),
            self.assertRaises(ExternalOperationError) as caught,
        ):
            await _ModernE2BProvider("e2b_test_key").execute(
                code="while True: print('x' * 1000)",
                timeout_seconds=2,
            )

        self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")
        self.assertEqual(emitted, 101)
        sandbox.kill.assert_awaited_once_with(request_timeout=5.0, retries=0)

    async def test_rich_results_and_error_tracebacks_stop_sdk_accumulation_and_cleanup(
        self,
    ) -> None:
        from e2b_code_interpreter import AsyncSandbox
        from e2b_code_interpreter.models import Execution, async_parse_output

        from daem0nmcp.api.v7.external_operations import (
            ExternalOperationError,
            _ModernE2BProvider,
        )

        for frame in (
            {"type": "result", "html": "x" * 2_000},
            {"type": "result", "text": ""},
            {
                "type": "error",
                "name": "Error",
                "value": "failed",
                "traceback": "x" * 100_001,
            },
        ):
            with self.subTest(frame_type=frame["type"], keys=list(frame)):
                execution = Execution()

                async def emit(_code, execution=execution, frame=frame, **kwargs):
                    for _ in range(10_000):
                        await async_parse_output(
                            execution,
                            json.dumps(frame),
                            on_result=kwargs["on_result"],
                            on_error=kwargs["on_error"],
                        )
                    self.fail("unbounded SDK frames were accepted")

                sandbox = AsyncMock()
                sandbox.run_code.side_effect = emit
                sandbox.kill.return_value = True
                with (
                    patch.object(
                        AsyncSandbox, "create", new=AsyncMock(return_value=sandbox)
                    ),
                    self.assertRaises(ExternalOperationError) as caught,
                ):
                    await _ModernE2BProvider("e2b_test_key").execute(
                        code="display('x')", timeout_seconds=2
                    )
                self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")
                self.assertLess(len(execution.results), 1_000)
                sandbox.kill.assert_awaited_once_with(request_timeout=5.0, retries=0)

    async def test_create_deadline_is_bounded_and_not_retried(self) -> None:
        from e2b_code_interpreter import AsyncSandbox

        from daem0nmcp.api.v7 import external_operations
        from daem0nmcp.api.v7.external_operations import (
            ExternalOperationError,
            _ModernE2BProvider,
        )

        async def never_create(**_kwargs):
            await asyncio.Event().wait()

        create = AsyncMock(side_effect=never_create)
        with (
            patch.object(AsyncSandbox, "create", new=create),
            patch.object(external_operations, "_E2B_CREATE_TIMEOUT_SECONDS", 0.01),
            self.assertRaises(ExternalOperationError) as caught,
        ):
            await asyncio.wait_for(
                _ModernE2BProvider("e2b_test_key").execute(
                    code="print('never')",
                    timeout_seconds=2,
                ),
                timeout=0.5,
            )

        self.assertEqual(caught.exception.code, "DEADLINE_EXCEEDED")
        self.assertEqual(create.await_count, 1)


class SandboxOperationAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_api_key_keeps_operation_registered_but_disabled(
        self,
    ) -> None:
        from daem0nmcp.api.v7.external_operations import (
            ExternalOperationDependencies,
            ExternalOperationError,
            build_external_operations,
        )
        from daem0nmcp.api.v7.record_operations import RecordOperationDependencies
        from daem0nmcp.workspace import WorkspaceRegistry

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            workspace = WorkspaceRegistry([root], default_root=root).default
            operations = build_external_operations(
                ExternalOperationDependencies(
                    record_dependencies=RecordOperationDependencies(),
                    environment={},
                )
            )
            request = _request(
                "sandbox_execute_python",
                workspace_id=workspace.workspace_id,
                code="print(1)",
                timeout_seconds=1,
                preflight_token=PREFLIGHT_TOKEN,
            )

            self.assertIn("sandbox_execute_python", operations)
            with self.assertRaises(ExternalOperationError) as caught:
                await operations["sandbox_execute_python"](
                    workspace=workspace,
                    request=request,
                )

        self.assertEqual(caught.exception.code, "CAPABILITY_DISABLED")
        self.assertEqual(len(caught.exception.capability_states), 1)
        state = caught.exception.capability_states[0]
        self.assertEqual(state.name, "agency-e2b")
        self.assertEqual(state.reason_code, "E2B_API_KEY_MISSING")
        self.assertEqual(
            state.remediation,
            "Set E2B_API_KEY to enable isolated Python execution.",
        )


if __name__ == "__main__":
    unittest.main()
