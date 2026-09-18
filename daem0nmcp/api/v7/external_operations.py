"""Bounded external v7 operations using reviewed transport and event seams."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from html.parser import HTMLParser
from types import MappingProxyType
from typing import Any, Protocol

from ...workspace import Workspace, WorkspaceRegistry
from .application import AdmittedRequest
from .errors import STABLE_ERROR_CODE_SET
from .models import CapabilityState
from .record_operations import (
    CanonicalBatchStoreRequest,
    RecordOperationDependencies,
    store_canonical_batch,
)
from .tools import DocumentIngestData, DocumentSource, SandboxExecutionData

_MAX_DOCUMENT_BYTES = 1_000_000
_MAX_CHUNKS = 100
_MAX_SANDBOX_OUTPUT = 100_000
_DOCUMENT_TYPES = frozenset(
    {"text/plain", "text/markdown", "text/html", "application/json"}
)
_E2B_CREATE_TIMEOUT_SECONDS = 10.0
_E2B_REQUEST_TIMEOUT_SECONDS = 10.0
_E2B_CLEANUP_TIMEOUT_SECONDS = 5.0
_E2B_EXECUTION_GRACE_SECONDS = 2.0
_UNSAFE_DOCUMENT_CONTROL = re.compile(r"[\x00-\x08\x0e-\x1f\x7f]")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.casefold() in {"script", "style", "nav", "footer", "header"}:
            self._ignored += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "nav", "footer", "header"}:
            self._ignored = max(0, self._ignored - 1)

    def handle_data(self, data: str) -> None:
        if not self._ignored and data.strip():
            self.parts.append(data.strip())


def _document_text(body: bytes, content_type: str) -> str:
    try:
        decoded = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ExternalOperationError("INVALID_ARGUMENT") from error
    if content_type == "text/html":
        parser = _TextExtractor()
        parser.feed(decoded)
        parser.close()
        decoded = "\n".join(parser.parts)
    # Form feed and vertical tab are conventional document page/line breaks.
    # Normalize them before enforcing the public wire model's text invariant.
    decoded = decoded.replace("\x0b", "\n").replace("\x0c", "\n")
    if _UNSAFE_DOCUMENT_CONTROL.search(decoded) is not None:
        raise ExternalOperationError("INVALID_ARGUMENT")
    return decoded


class ExternalOperationError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        capability_states: tuple[CapabilityState, ...] = (),
    ) -> None:
        if code not in STABLE_ERROR_CODE_SET:
            raise ValueError("external operation error code is not stable")
        self.code = code
        self.capability_states = capability_states
        super().__init__(code)


class SandboxProvider(Protocol):
    async def execute(
        self, *, code: str, timeout_seconds: int
    ) -> Mapping[str, Any]: ...


def _bounded_e2b_output_handler() -> Callable[[Any], None]:
    """Stop the SDK stream before its in-memory Execution log can grow unbounded."""

    observed = 0

    def guard(message: Any) -> None:
        nonlocal observed
        line = getattr(message, "line", None)
        if not isinstance(line, str):
            raise TypeError("E2B output message has an unexpected shape")
        observed += max(1, len(line.encode("utf-8")))
        if observed > _MAX_SANDBOX_OUTPUT:
            raise ExternalOperationError("INVALID_ARGUMENT")

    return guard


def _bounded_e2b_payload_handler() -> Callable[[Any], None]:
    """Bound SDK-retained rich results and errors, including empty frames."""
    remaining = _MAX_SANDBOX_OUTPUT

    def charge(value: Any, depth: int = 0) -> None:
        nonlocal remaining
        remaining -= 16  # Object/list overhead also bounds zero-length frames.
        if remaining < 0 or depth > 16:
            raise ExternalOperationError("INVALID_ARGUMENT")
        if isinstance(value, str):
            if len(value) > remaining:
                raise ExternalOperationError("INVALID_ARGUMENT")
            remaining -= len(value.encode("utf-8"))
            if remaining < 0:
                raise ExternalOperationError("INVALID_ARGUMENT")
        elif value is None or isinstance(value, (bool, int, float)):
            return
        elif isinstance(value, (list, tuple)):
            for item in value:
                charge(item, depth + 1)
        elif isinstance(value, Mapping):
            for key, item in value.items():
                charge(key, depth + 1)
                charge(item, depth + 1)
        elif is_dataclass(value) and not isinstance(value, type):
            for member in fields(value):
                charge(getattr(value, member.name), depth + 1)
        else:
            # SDK chart values are Pydantic models. Traverse their declared
            # fields without creating another unbounded serialized copy.
            from pydantic import BaseModel

            if not isinstance(value, BaseModel):
                raise ExternalOperationError("INVALID_ARGUMENT")
            for name in type(value).model_fields:
                charge(getattr(value, name), depth + 1)

    return charge


def _bounded_output_text(lines: list[str]) -> str:
    """Join SDK output without exceeding the public UTF-8 byte budget."""

    parts: list[bytes] = []
    remaining = _MAX_SANDBOX_OUTPUT
    for line in lines:
        encoded = line.encode("utf-8")
        if len(encoded) <= remaining:
            parts.append(encoded)
            remaining -= len(encoded)
            continue
        parts.append(encoded[:remaining])
        break
    return b"".join(parts).decode("utf-8", errors="ignore")


@dataclass(frozen=True, slots=True)
class ExternalOperationDependencies:
    record_dependencies: RecordOperationDependencies
    sandbox_provider_factory: Callable[[], SandboxProvider] | None = None
    environment: Mapping[str, str] = field(default_factory=lambda: os.environ)


def _authorize(workspace: Workspace, request: AdmittedRequest, tool: str) -> None:
    if (
        not isinstance(workspace, Workspace)
        or request.tool_name != tool
        or request.workspace_id != workspace.workspace_id
    ):
        raise ExternalOperationError("UNAUTHORIZED_WORKSPACE")
    try:
        root = workspace.root.resolve(strict=True)
        expected = WorkspaceRegistry([root], default_root=root).default
    except (OSError, RuntimeError, TypeError, ValueError):
        raise ExternalOperationError("UNAUTHORIZED_WORKSPACE") from None
    if expected.workspace_id != workspace.workspace_id or os.path.normcase(
        str(workspace.root)
    ) != os.path.normcase(str(root)):
        raise ExternalOperationError("UNAUTHORIZED_WORKSPACE")


def _chunks(text: str, size: int) -> tuple[str, ...]:
    values = tuple(text[index : index + size] for index in range(0, len(text), size))
    if not values or len(values) > _MAX_CHUNKS:
        raise ExternalOperationError("INVALID_ARGUMENT")
    return values


async def _fetch_document(url: str) -> tuple[bytes, str]:
    import httpx

    from ...pinned_http import (
        PinnedAsyncHTTPTransport,
        PinnedResponseError,
        read_bounded_identity_body,
        validate_public_url,
    )

    issue = await validate_public_url(url, allowed_schemes=("https",))
    if issue is not None:
        raise ExternalOperationError("INVALID_ARGUMENT")
    transport = PinnedAsyncHTTPTransport()
    try:
        async with (
            httpx.AsyncClient(
                transport=transport,
                follow_redirects=False,
                trust_env=False,
                timeout=httpx.Timeout(15.0),
            ) as client,
            client.stream(
                "GET",
                url,
                headers={
                    "accept": "text/plain, text/markdown, text/html, application/json",
                    "accept-encoding": "identity",
                },
            ) as response,
        ):
            if 300 <= response.status_code < 400 or response.status_code != 200:
                raise ExternalOperationError("NOT_FOUND")
            content_type = (
                response.headers.get("content-type", "")
                .split(";", 1)[0]
                .strip()
                .casefold()
            )
            if content_type not in _DOCUMENT_TYPES:
                raise ExternalOperationError("INVALID_ARGUMENT")
            return await read_bounded_identity_body(
                response, max_bytes=_MAX_DOCUMENT_BYTES
            ), content_type
    except ExternalOperationError:
        raise
    except PinnedResponseError:
        raise ExternalOperationError("INVALID_ARGUMENT") from None
    except (httpx.HTTPError, UnicodeError, ValueError):
        raise ExternalOperationError("CAPABILITY_DEGRADED") from None
    finally:
        await transport.aclose()


class _ModernE2BProvider:
    """Modern async E2B adapter; no server environment crosses the boundary."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def execute(self, *, code: str, timeout_seconds: int) -> Mapping[str, Any]:
        try:
            from e2b import TimeoutException as E2BTimeoutError
            from e2b_code_interpreter import (
                AsyncSandbox,  # type: ignore[import-untyped]
            )

            from .e2b_transport import (
                ExecutionOutputLimitError,
                bounded_sandbox_type,
                close_execution_client,
            )
        except ImportError as import_error:
            raise ExternalOperationError("CAPABILITY_DISABLED") from import_error
        sandbox: Any | None = None
        result: Mapping[str, Any] | None = None
        failure: BaseException | None = None
        try:
            created = await asyncio.wait_for(
                bounded_sandbox_type(AsyncSandbox).create(
                    timeout=timeout_seconds + 5,
                    secure=True,
                    allow_internet_access=False,
                    envs={},
                    lifecycle={"on_timeout": "kill"},
                    api_key=self._api_key,
                    request_timeout=_E2B_REQUEST_TIMEOUT_SECONDS,
                    retries=0,
                ),
                timeout=_E2B_CREATE_TIMEOUT_SECONDS,
            )
            sandbox = created
            on_stdout = _bounded_e2b_output_handler()
            on_stderr = _bounded_e2b_output_handler()
            on_payload = _bounded_e2b_payload_handler()
            execution = await asyncio.wait_for(
                created.run_code(
                    code,
                    on_stdout=on_stdout,
                    on_stderr=on_stderr,
                    on_result=on_payload,
                    on_error=on_payload,
                    timeout=float(timeout_seconds),
                    request_timeout=_E2B_REQUEST_TIMEOUT_SECONDS,
                    envs={},
                ),
                timeout=float(timeout_seconds) + _E2B_EXECUTION_GRACE_SECONDS,
            )
            execution_error = getattr(execution, "error", None)
            output = getattr(execution, "logs", None)
            stdout_lines = getattr(output, "stdout", None)
            stderr_lines = getattr(output, "stderr", None)
            if not isinstance(stdout_lines, list) or not all(
                isinstance(item, str) for item in stdout_lines
            ):
                raise TypeError("E2B stdout has an unexpected shape")
            if not isinstance(stderr_lines, list) or not all(
                isinstance(item, str) for item in stderr_lines
            ):
                raise TypeError("E2B stderr has an unexpected shape")
            stdout = _bounded_output_text(stdout_lines)
            stderr = _bounded_output_text(stderr_lines)
            sanitized_logs: list[str] = []
            for prefix, lines in (("stdout", stdout_lines), ("stderr", stderr_lines)):
                for line in lines:
                    if line.rstrip():
                        sanitized_logs.append(f"{prefix}: {line.rstrip()}"[:4096])
                    if len(sanitized_logs) == 100:
                        break
                if len(sanitized_logs) == 100:
                    break
            if execution_error is not None:
                name = getattr(execution_error, "name", "ExecutionError")
                value = getattr(execution_error, "value", "Execution failed")
                if not isinstance(name, str) or not isinstance(value, str):
                    raise TypeError("E2B execution error has an unexpected shape")
                summary = f"{name}: {value}"
                stderr = f"{stderr}{'' if not stderr or stderr.endswith(chr(10)) else chr(10)}{summary}"
                if len(sanitized_logs) < 100:
                    sanitized_logs.append(f"error: {summary}"[:4096])
            result = {
                "success": execution_error is None,
                "stdout": stdout,
                "stderr": stderr,
                "exit_status": 0 if execution_error is None else 1,
                "logs": sanitized_logs,
            }
        except asyncio.CancelledError as error:
            failure = error
        except (asyncio.TimeoutError, E2BTimeoutError):
            failure = ExternalOperationError("DEADLINE_EXCEEDED")
        except ExternalOperationError as error:
            failure = error
        except ExecutionOutputLimitError:
            failure = ExternalOperationError("INVALID_ARGUMENT")
        except Exception:
            # Execution may have happened; callers must not retry implicitly.
            failure = ExternalOperationError("CAPABILITY_DEGRADED")

        cleanup_failure: ExternalOperationError | None = None
        if sandbox is not None:
            try:
                await _kill_e2b_sandbox(sandbox)
            except asyncio.CancelledError as error:
                failure = error
            except ExternalOperationError as error:
                cleanup_failure = error
            try:
                await _finish_e2b_cleanup(close_execution_client(sandbox))
            except asyncio.CancelledError as error:
                failure = error
            except ExternalOperationError as error:
                cleanup_failure = error
        if isinstance(failure, asyncio.CancelledError):
            raise failure
        if cleanup_failure is not None:
            raise cleanup_failure from failure
        if failure is not None:
            raise failure
        if result is None:
            raise ExternalOperationError("CAPABILITY_DEGRADED")
        return result


async def _kill_e2b_sandbox(sandbox: Any) -> None:
    """Kill one sandbox within a deadline while deferring caller cancellation."""

    await _finish_e2b_cleanup(
        sandbox.kill(
            request_timeout=_E2B_CLEANUP_TIMEOUT_SECONDS,
            retries=0,
        )
    )


async def _finish_e2b_cleanup(operation: Coroutine[Any, Any, Any]) -> None:
    """Bound cleanup and defer repeated cancellation until its terminal state."""

    cleanup = asyncio.create_task(operation)
    deadline = asyncio.get_running_loop().time() + _E2B_CLEANUP_TIMEOUT_SECONDS
    cancellation: asyncio.CancelledError | None = None
    cleanup_error: BaseException | None = None
    while not cleanup.done():
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            cleanup.cancel()
            try:
                await cleanup
            except asyncio.CancelledError:
                pass
            except Exception as error:
                cleanup_error = error
            if cleanup_error is None:
                cleanup_error = asyncio.TimeoutError()
            break
        try:
            await asyncio.wait_for(asyncio.shield(cleanup), timeout=remaining)
        except asyncio.CancelledError as error:
            cancellation = error
            continue
        except asyncio.TimeoutError:
            cleanup.cancel()
            try:
                await cleanup
            except asyncio.CancelledError:
                pass
            except Exception as error:
                cleanup_error = error
            if cleanup_error is None:
                cleanup_error = asyncio.TimeoutError()
            break
        except Exception as error:
            cleanup_error = error
            break
    if cleanup.done() and not cleanup.cancelled() and cleanup_error is None:
        try:
            cleanup.result()
        except Exception as error:
            cleanup_error = error
    if cancellation is not None:
        raise cancellation from cleanup_error
    if cleanup_error is not None:
        raise ExternalOperationError("CAPABILITY_DEGRADED") from cleanup_error


def build_external_operations(
    dependencies: ExternalOperationDependencies,
) -> Mapping[str, Callable[..., Any]]:
    if not isinstance(dependencies, ExternalOperationDependencies):
        raise TypeError("external dependencies are required")

    async def document_ingest_url(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> DocumentIngestData:
        _authorize(workspace, request, "document_ingest_url")
        raw, content_type = await _fetch_document(request.url)
        text = _document_text(raw, content_type)
        chunks = _chunks(text, request.chunk_size)
        digest = hashlib.sha256(raw).hexdigest()
        provenance = {
            "url": request.url,
            "content_hash": digest,
            "content_type": content_type,
            "topic": request.topic,
            "chunk_size": request.chunk_size,
        }
        records = tuple(
            {
                "record_type": "learning",
                "content": chunk,
                "context": {
                    "document": provenance,
                    "chunk_index": index,
                    "chunk_count": len(chunks),
                },
                "tags": ["document"],
            }
            for index, chunk in enumerate(chunks)
        )
        try:
            receipt = await store_canonical_batch(
                dependencies.record_dependencies,
                workspace=workspace,
                request=CanonicalBatchStoreRequest(
                    records=records,
                    idempotency_key=request.idempotency_key,
                    semantic_namespace="document-ingest-url",
                    provenance=provenance,
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            code = getattr(error, "code", None)
            translated = (
                code
                if isinstance(code, str) and code in STABLE_ERROR_CODE_SET
                else "CAPABILITY_DEGRADED"
            )
            raise ExternalOperationError(translated) from None
        return DocumentIngestData(
            source=DocumentSource(
                url=request.url,
                topic=request.topic,
                content_hash=digest,
            ),
            records=receipt.records,
            event_ids=receipt.event_ids,
            truncated=False,
        )

    async def sandbox_execute_python(
        *, workspace: Workspace, request: AdmittedRequest
    ) -> SandboxExecutionData:
        _authorize(workspace, request, "sandbox_execute_python")
        if not dependencies.environment.get("E2B_API_KEY"):
            raise ExternalOperationError(
                "CAPABILITY_DISABLED",
                capability_states=(
                    CapabilityState(
                        name="agency-e2b",
                        status="disabled",
                        reason_code="E2B_API_KEY_MISSING",
                        remediation=(
                            "Set E2B_API_KEY to enable isolated Python execution."
                        ),
                    ),
                ),
            )
        provider = (
            dependencies.sandbox_provider_factory()
            if dependencies.sandbox_provider_factory
            else _ModernE2BProvider(str(dependencies.environment["E2B_API_KEY"]))
        )
        started = asyncio.get_running_loop().time()
        result = await provider.execute(
            code=request.code, timeout_seconds=request.timeout_seconds
        )
        elapsed = min(
            60_000, max(0, int((asyncio.get_running_loop().time() - started) * 1000))
        )
        return SandboxExecutionData(
            success=bool(result.get("success")),
            stdout=str(result.get("stdout", ""))[:_MAX_SANDBOX_OUTPUT],
            stderr=str(result.get("stderr", ""))[:_MAX_SANDBOX_OUTPUT],
            exit_status=int(result.get("exit_status", -1)),
            execution_time_ms=elapsed,
            sanitized_logs=[
                str(item)[:4096] for item in list(result.get("logs", []))[:100]
            ],
        )

    return MappingProxyType(
        {
            "document_ingest_url": document_ingest_url,
            "sandbox_execute_python": sandbox_execute_python,
        }
    )


__all__ = [
    "ExternalOperationDependencies",
    "ExternalOperationError",
    "SandboxProvider",
    "build_external_operations",
]
