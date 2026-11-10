"""Pinned SDK parsing must never see an oversized interpreter wire frame."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from daem0nmcp.api.v7.e2b_transport import (
    MAX_EXECUTION_WIRE_BYTES,
    BoundedExecutionTransport,
    bounded_sandbox_type,
)
from daem0nmcp.api.v7.external_operations import (
    ExternalOperationError,
    _ModernE2BProvider,
)


class GeneratedStream(httpx.AsyncByteStream):
    def __init__(self, *, oversized=True):
        self.oversized = oversized
        self.sent = 0
        self.closed = False

    async def __aiter__(self):
        if not self.oversized:
            yield (
                json.dumps({"type": "stdout", "text": "ok\n", "timestamp": 0}) + "\n"
            ).encode()
            return
        # One syntactically unfinished JSON frame, generated with constant space.
        yield b'{"type":"result","html":"'
        for _ in range(10_000):
            self.sent += 8192
            yield b"x" * 8192
        yield b'"}\n'

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,headers",
    [
        (200, {}),
        (500, {}),
        (200, {"content-encoding": "gzip"}),
        (200, {"content-length": str(MAX_EXECUTION_WIRE_BYTES + 1)}),
    ],
)
async def test_sdk_wire_bound_precedes_parser_and_always_kills(status, headers):
    sdk = pytest.importorskip("e2b_code_interpreter")
    from e2b.connection_config import ConnectionConfig
    from e2b.sandbox.main import SandboxBase
    from packaging.version import Version

    cls = bounded_sandbox_type(sdk.AsyncSandbox)
    sandbox = object.__new__(cls)
    SandboxBase.__init__(
        sandbox,
        sandbox_id="synthetic",
        envd_version=Version("0.4.0"),
        envd_access_token="synthetic-host-token",
        sandbox_domain="example.invalid",
        connection_config=ConnectionConfig(api_key="synthetic-key"),
    )
    stream = GeneratedStream()
    requests = []

    async def respond(request):
        requests.append(request)
        return httpx.Response(status, headers=headers, stream=stream)

    client = httpx.AsyncClient(
        transport=BoundedExecutionTransport(httpx.MockTransport(respond))
    )
    sandbox.__dict__["_v7_execution_client"] = client
    sandbox.kill = AsyncMock(return_value=True)
    with (
        patch.object(sdk.AsyncSandbox, "create", new=AsyncMock(return_value=sandbox)),
        patch(
            "e2b_code_interpreter.code_interpreter_async.async_parse_output",
            new=AsyncMock(),
        ) as parse,
        pytest.raises(ExternalOperationError) as caught,
    ):
        await _ModernE2BProvider("synthetic-key").execute(
            code="display('synthetic')", timeout_seconds=2
        )
    assert caught.value.code == "INVALID_ARGUMENT"
    parse.assert_not_awaited()
    assert stream.sent <= MAX_EXECUTION_WIRE_BYTES + 8192
    assert stream.closed and client.is_closed
    sandbox.kill.assert_awaited_once_with(request_timeout=5.0, retries=0)
    assert requests[0].headers["accept-encoding"] == "identity"
    assert requests[0].headers["X-Access-Token"] == "synthetic-host-token"


@pytest.mark.asyncio
async def test_small_sdk_frame_preserves_execution_contract():
    sdk = pytest.importorskip("e2b_code_interpreter")
    from e2b.connection_config import ConnectionConfig
    from e2b.sandbox.main import SandboxBase
    from packaging.version import Version

    cls = bounded_sandbox_type(sdk.AsyncSandbox)
    sandbox = object.__new__(cls)
    SandboxBase.__init__(
        sandbox,
        sandbox_id="synthetic",
        envd_version=Version("0.4.0"),
        envd_access_token=None,
        sandbox_domain="example.invalid",
        connection_config=ConnectionConfig(api_key="synthetic-key"),
    )
    stream = GeneratedStream(oversized=False)
    client = httpx.AsyncClient(
        transport=BoundedExecutionTransport(
            httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
        )
    )
    sandbox.__dict__["_v7_execution_client"] = client
    sandbox.kill = AsyncMock(return_value=True)
    with patch.object(sdk.AsyncSandbox, "create", new=AsyncMock(return_value=sandbox)):
        result = await _ModernE2BProvider("synthetic-key").execute(
            code="print('ok')", timeout_seconds=2
        )
    assert result["success"] and result["stdout"] == "ok\n"
    assert stream.closed and client.is_closed
    sandbox.kill.assert_awaited_once()


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_owned_transport_cleanup():
    sdk = pytest.importorskip("e2b_code_interpreter")
    executing = asyncio.Event()
    closing = asyncio.Event()
    release = asyncio.Event()
    closed = asyncio.Event()

    class Transport(httpx.AsyncBaseTransport):
        async def aclose(self):
            closing.set()
            await release.wait()
            closed.set()

    async def execute(*args, **kwargs):
        executing.set()
        await asyncio.Event().wait()

    sandbox = AsyncMock()
    sandbox.run_code.side_effect = execute
    sandbox.kill.return_value = True
    sandbox.__dict__["_v7_execution_client"] = httpx.AsyncClient(transport=Transport())
    with patch.object(sdk.AsyncSandbox, "create", new=AsyncMock(return_value=sandbox)):
        task = asyncio.create_task(
            _ModernE2BProvider("synthetic-key").execute(code="pass", timeout_seconds=2)
        )
        await executing.wait()
        task.cancel()
        await closing.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert closed.is_set()
    sandbox.kill.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("hang", [False, True])
async def test_owned_transport_close_failure_is_bounded_and_stable(hang):
    sdk = pytest.importorskip("e2b_code_interpreter")
    from e2b_code_interpreter.models import Execution, Logs

    class Transport(httpx.AsyncBaseTransport):
        async def aclose(self):
            if hang:
                await asyncio.Event().wait()
            raise RuntimeError("sensitive transport failure")

    sandbox = AsyncMock()
    sandbox.run_code.return_value = Execution(logs=Logs(stdout=["ok\n"]))
    sandbox.kill.return_value = True
    sandbox.__dict__["_v7_execution_client"] = httpx.AsyncClient(transport=Transport())
    with (
        patch.object(sdk.AsyncSandbox, "create", new=AsyncMock(return_value=sandbox)),
        patch(
            "daem0nmcp.api.v7.external_operations._E2B_CLEANUP_TIMEOUT_SECONDS", 0.01
        ),
        pytest.raises(ExternalOperationError) as caught,
    ):
        await asyncio.wait_for(
            _ModernE2BProvider("synthetic-key").execute(code="pass", timeout_seconds=2),
            timeout=0.5,
        )
    assert caught.value.code == "CAPABILITY_DEGRADED"
    assert "sensitive" not in str(caught.value)
    sandbox.kill.assert_awaited_once()
