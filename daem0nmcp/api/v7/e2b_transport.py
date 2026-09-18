"""Bound the pinned E2B interpreter's wire stream before its JSON parser.

The SDK 2.10 ``_client`` property is the sole private integration seam. The
subclass keeps SDK request/authentication construction and owns an HTTP/1.1
client whose response stream cannot accumulate an unbounded NDJSON frame.
"""

from __future__ import annotations

from typing import Any

import httpx

MAX_EXECUTION_WIRE_BYTES = 512_000


class ExecutionOutputLimitError(ValueError):
    """An interpreter response exceeded the bounded wire contract."""


class _BoundedStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream) -> None:
        self._stream = stream

    async def __aiter__(self):
        remaining = MAX_EXECUTION_WIRE_BYTES
        async for chunk in self._stream:
            if len(chunk) > remaining:
                raise ExecutionOutputLimitError("Interpreter output exceeds byte limit")
            remaining -= len(chunk)
            yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()


class BoundedExecutionTransport(httpx.AsyncBaseTransport):
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport or httpx.AsyncHTTPTransport(
            http2=False,
            retries=0,
            trust_env=False,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request.headers["accept-encoding"] = "identity"
        response = await self._transport.handle_async_request(request)
        # Reject compression before HTTPX can expand it, including error bodies.
        encoding = response.headers.get("content-encoding", "identity").strip().lower()
        length = response.headers.get("content-length")
        if encoding != "identity" or (
            length is not None
            and (
                not length.isascii()
                or not length.isdecimal()
                or len(length) > 10
                or int(length) > MAX_EXECUTION_WIRE_BYTES
            )
        ):
            await response.aclose()
            raise ExecutionOutputLimitError("Interpreter response violates wire bounds")
        stream = response.stream
        if not isinstance(stream, httpx.AsyncByteStream):
            raise TypeError("Interpreter transport returned a non-async stream")
        response.stream = _BoundedStream(stream)
        return response

    async def aclose(self) -> None:
        await self._transport.aclose()


def bounded_sandbox_type(base: type[Any]) -> type[Any]:
    """Adapt the explicitly pinned SDK without changing its global clients."""

    class BoundedSandbox(base):
        @property
        def _client(self) -> httpx.AsyncClient:
            client = self.__dict__.get("_v7_execution_client")
            if client is None:
                client = httpx.AsyncClient(
                    transport=BoundedExecutionTransport(),
                    follow_redirects=False,
                    trust_env=False,
                )
                self.__dict__["_v7_execution_client"] = client
            return client

    return BoundedSandbox


async def close_execution_client(sandbox: Any) -> None:
    client = vars(sandbox).pop("_v7_execution_client", None)
    if client is not None:
        await client.aclose()
