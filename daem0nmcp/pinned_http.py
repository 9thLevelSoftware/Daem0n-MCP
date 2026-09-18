"""Pinned, public-address HTTP transport used only by URL ingestion.

This module is the owned compatibility boundary for the optional httpx/httpcore
stack.  Import it lazily so a base Daem0n MCP installation does not require the
``apps`` extra.
"""

from __future__ import annotations

import asyncio
import inspect
import ipaddress
import logging
import socket
import threading
from collections.abc import (
    AsyncIterable,
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Sequence,
)
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from functools import partial

import httpcore
import httpx
from packaging.version import InvalidVersion, Version

try:
    from .bounded_workers import BoundedWorkerBusyError, BoundedWorkerPool
except ImportError:  # pragma: no cover - direct module execution compatibility
    from daem0nmcp.bounded_workers import BoundedWorkerBusyError, BoundedWorkerPool


AddressResolver = Callable[[str, int], Awaitable[Sequence[str]]]
_DNS_WORKER_POOL = BoundedWorkerPool(
    max_workers=4,
    thread_name_prefix="daem0nmcp-dns",
)
_PUBLIC_SPECIAL_ADDRESS_EXCEPTIONS = frozenset(
    (
        ipaddress.IPv4Address("192.0.0.9"),  # PCP anycast
        ipaddress.IPv4Address("192.0.0.10"),  # TURN anycast
    )
)
_DISALLOWED_SPECIAL_NETWORKS: tuple[
    ipaddress.IPv4Network | ipaddress.IPv6Network, ...
] = (
    ipaddress.IPv4Network("192.0.0.0/24"),
    ipaddress.IPv4Network("192.88.99.0/24"),
    ipaddress.IPv6Network("3fff::/20"),
    ipaddress.IPv6Network("100:0:0:1::/64"),
    ipaddress.IPv6Network("5f00::/16"),
    ipaddress.IPv6Network("::ffff:0:0/96"),  # IPv4-mapped
    ipaddress.IPv6Network("64:ff9b::/96"),  # well-known NAT64
    ipaddress.IPv6Network("64:ff9b:1::/48"),  # local-use NAT64
    ipaddress.IPv6Network("2001::/32"),  # Teredo
    ipaddress.IPv6Network("2002::/16"),  # 6to4
)


# Keep module import deterministic when a partial or incompatible optional
# profile is present. Public entry points still call the owned compatibility
# gate before using any HTTP interface.
def _class_base(module, name: str, fallback: type) -> type:
    candidate = getattr(module, name, None)
    return candidate if isinstance(candidate, type) else fallback


def _exception_base(module, name: str) -> type[Exception]:
    candidate = getattr(module, name, None)
    if isinstance(candidate, type) and issubclass(candidate, Exception):
        return candidate
    return RuntimeError


_CONNECT_ERROR_BASE = _exception_base(httpcore, "ConnectError")
_NETWORK_BACKEND_BASE = _class_base(httpcore, "AsyncNetworkBackend", object)
_BYTE_STREAM_BASE = _class_base(httpx, "AsyncByteStream", object)
_TRANSPORT_BASE = _class_base(httpx, "AsyncBaseTransport", object)

_DEPENDENCY_LOG_SCOPE: ContextVar[bool] = ContextVar(
    "daem0nmcp_pinned_http_log_scope", default=False
)
_LOG_FILTER_LOCK = threading.Lock()
_LOG_FILTER_DEPTH = 0
_LOG_FILTER_TARGETS: list[logging.Logger | logging.Handler] = []


class _PinnedDependencyLogFilter(logging.Filter):
    """Suppress dependency records only in the current ingestion context."""

    def filter(self, record: logging.LogRecord) -> bool:
        is_dependency = (
            record.name == "httpx"
            or record.name.startswith(("httpx.", "httpcore."))
            or record.name == "httpcore"
        )
        return not (is_dependency and _DEPENDENCY_LOG_SCOPE.get())


_DEPENDENCY_LOG_FILTER = _PinnedDependencyLogFilter()


def _install_dependency_log_filter() -> None:
    """Attach one context-aware filter while at least one scope is active."""
    global _LOG_FILTER_DEPTH
    with _LOG_FILTER_LOCK:
        _LOG_FILTER_DEPTH += 1
        candidates: list[logging.Logger] = [
            logging.getLogger("httpx"),
            logging.getLogger("httpcore"),
        ]
        for name, candidate in logging.Logger.manager.loggerDict.items():
            if isinstance(candidate, logging.Logger) and (
                name.startswith("httpx.") or name.startswith("httpcore.")
            ):
                candidates.append(candidate)

        handlers = list(logging.getLogger().handlers)
        for candidate in candidates:
            if _DEPENDENCY_LOG_FILTER not in candidate.filters:
                candidate.addFilter(_DEPENDENCY_LOG_FILTER)
                _LOG_FILTER_TARGETS.append(candidate)
            handlers.extend(candidate.handlers)
        for handler in handlers:
            if _DEPENDENCY_LOG_FILTER not in handler.filters:
                handler.addFilter(_DEPENDENCY_LOG_FILTER)
                _LOG_FILTER_TARGETS.append(handler)


def _remove_dependency_log_filter() -> None:
    """Restore operator filter configuration after the last active scope."""
    global _LOG_FILTER_DEPTH
    with _LOG_FILTER_LOCK:
        _LOG_FILTER_DEPTH -= 1
        if _LOG_FILTER_DEPTH > 0:
            return
        _LOG_FILTER_DEPTH = 0
        for target in _LOG_FILTER_TARGETS:
            target.removeFilter(_DEPENDENCY_LOG_FILTER)
        _LOG_FILTER_TARGETS.clear()


@contextmanager
def pinned_dependency_log_scope():
    """Suppress URL-bearing dependency records for this context only."""
    _install_dependency_log_filter()
    token = _DEPENDENCY_LOG_SCOPE.set(True)
    try:
        yield
    finally:
        _DEPENDENCY_LOG_SCOPE.reset(token)
        _remove_dependency_log_filter()


class PinnedTransportCompatibilityError(RuntimeError):
    """Raised when the installed HTTP stack is outside the validated contract."""


class PinnedAddressError(_CONNECT_ERROR_BASE):  # type: ignore[misc,valid-type]
    """Raised when a connect-time address cannot be safely pinned."""


class PinnedResponseError(ValueError):
    """Raised when a response cannot be consumed within ingestion bounds."""


def _release_tuple(version: str) -> tuple[int, ...] | None:
    if not isinstance(version, str):
        return None
    try:
        parsed = Version(version)
    except InvalidVersion:
        return None
    if parsed.epoch != 0 or parsed.is_prerelease or parsed.is_devrelease:
        return None
    release = parsed.release
    return release + (0,) * max(0, 3 - len(release))


def ensure_runtime_compatibility() -> tuple[str, str]:
    """Validate the exact public seams used by the pinned transport."""
    httpx_version = getattr(httpx, "__version__", "unknown")
    httpcore_version = getattr(httpcore, "__version__", "unknown")
    httpx_release = _release_tuple(httpx_version)
    httpcore_release = _release_tuple(httpcore_version)

    compatible = (
        httpx_release is not None
        and httpx_release[:2] == (0, 28)
        and httpx_release >= (0, 28, 1)
        and httpcore_release is not None
        and httpcore_release[:2] == (1, 0)
        and httpcore_release >= (1, 0, 9)
    )

    required_httpx = (
        "AsyncClient",
        "AsyncBaseTransport",
        "AsyncByteStream",
        "InvalidURL",
        "Request",
        "Response",
        "URL",
        "create_ssl_context",
    )
    required_httpcore = (
        "AsyncConnectionPool",
        "AsyncNetworkBackend",
        "AsyncNetworkStream",
        "ConnectTimeout",
        "ConnectError",
        "LocalProtocolError",
        "Request",
        "UnsupportedProtocol",
        "URL",
    )
    compatible = compatible and all(
        callable(getattr(httpx, name, None)) for name in required_httpx
    )
    compatible = compatible and all(
        callable(getattr(httpcore, name, None)) for name in required_httpcore
    )
    httpx_invalid_url = getattr(httpx, "InvalidURL", None)
    compatible = (
        compatible
        and isinstance(httpx_invalid_url, type)
        and issubclass(httpx_invalid_url, Exception)
    )
    for exception_name in (
        "ConnectError",
        "ConnectTimeout",
        "LocalProtocolError",
        "UnsupportedProtocol",
    ):
        exception_type = getattr(httpcore, exception_name, None)
        compatible = (
            compatible
            and isinstance(exception_type, type)
            and issubclass(exception_type, Exception)
        )

    if compatible:
        try:
            pool_parameters = inspect.signature(httpcore.AsyncConnectionPool).parameters
            connect_parameters = tuple(
                inspect.signature(httpcore.AsyncNetworkBackend.connect_tcp).parameters
            )
            tls_parameters = tuple(
                inspect.signature(httpcore.AsyncNetworkStream.start_tls).parameters
            )
            compatible = (
                "network_backend" in pool_parameters
                and connect_parameters
                == (
                    "self",
                    "host",
                    "port",
                    "timeout",
                    "local_address",
                    "socket_options",
                )
                and tls_parameters
                == ("self", "ssl_context", "server_hostname", "timeout")
            )
        except (AttributeError, TypeError, ValueError):
            compatible = False

    if not compatible:
        raise PinnedTransportCompatibilityError(
            "Unsupported pinned HTTP runtime "
            f"(httpx {httpx_version}, httpcore {httpcore_version}). "
            "Install the validated versions with: pip install 'daem0nmcp[apps]'"
        )

    return httpx_version, httpcore_version


async def resolve_host_addresses(host: str, port: int) -> tuple[str, ...]:
    """Resolve a hostname in the dedicated bounded pool for one TCP dial."""
    try:
        addresses = await _DNS_WORKER_POOL.run(
            partial(
                socket.getaddrinfo,
                host,
                port,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        )
    except BoundedWorkerBusyError as error:
        raise PinnedAddressError("Host resolution capacity is unavailable") from error
    except (OSError, UnicodeError) as error:
        raise PinnedAddressError("Host resolution failed") from error

    return tuple(str(address[4][0]) for address in addresses)


def _is_disallowed_special_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    """Apply the owned special-purpose registry policy across Python versions."""
    if address in _PUBLIC_SPECIAL_ADDRESS_EXCEPTIONS:
        return False
    return any(
        address.version == network.version and address in network
        for network in _DISALLOWED_SPECIAL_NETWORKS
    )


def select_public_address(addresses: Iterable[str]) -> str:
    """Validate the complete answer set and select one canonical literal."""
    parsed_addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    try:
        for address in addresses:
            if not isinstance(address, str):
                raise ValueError
            parsed = ipaddress.ip_address(address)
            explicitly_public = parsed in _PUBLIC_SPECIAL_ADDRESS_EXCEPTIONS
            if _is_disallowed_special_address(parsed) or (
                not explicitly_public
                and (
                    not parsed.is_global
                    or parsed.is_multicast
                    or parsed.is_reserved
                    or getattr(parsed, "is_site_local", False)
                )
            ):
                raise ValueError
            parsed_addresses.add(parsed)
    except (TypeError, ValueError) as error:
        raise PinnedAddressError("Host resolution was not entirely public") from error

    if not parsed_addresses:
        raise PinnedAddressError("Host resolution returned no public addresses")

    selected = min(
        parsed_addresses, key=lambda address: (address.version, address.packed)
    )
    return str(selected)


async def validate_public_url(
    url: str,
    *,
    allowed_schemes: Iterable[str],
    resolver: AddressResolver | None = None,
) -> str | None:
    """Validate a URL and its complete admission-time DNS answer set."""
    try:
        ensure_runtime_compatibility()
    except PinnedTransportCompatibilityError as error:
        return str(error)
    if not isinstance(url, str) or any(
        ord(character) < 32 or ord(character) == 127 for character in url
    ):
        return "Invalid URL format"

    authority_marker = url.find("://")
    if authority_marker < 1:
        return "Invalid URL format"
    authority_start = authority_marker + 3
    authority_end = len(url)
    for delimiter in "/?#":
        position = url.find(delimiter, authority_start)
        if position >= 0:
            authority_end = min(authority_end, position)
    raw_authority = url[authority_start:authority_end]
    if (
        not raw_authority
        or "\\" in raw_authority
        or "@" in raw_authority
        or raw_authority.endswith(":")
    ):
        return "URL has an invalid authority"

    raw_port: str | None = None
    if raw_authority.startswith("["):
        closing_bracket = raw_authority.find("]")
        if closing_bracket < 0:
            return "URL has an invalid authority"
        suffix = raw_authority[closing_bracket + 1 :]
        if suffix:
            if not suffix.startswith(":"):
                return "URL has an invalid authority"
            raw_port = suffix[1:]
    elif ":" in raw_authority:
        if raw_authority.count(":") != 1:
            return "URL has an invalid authority"
        _, raw_port = raw_authority.rsplit(":", 1)

    if raw_port is not None:
        if not raw_port or any(character not in "0123456789" for character in raw_port):
            return "URL has an invalid port or authority"
        if len(raw_port) > 5:
            return "URL port must be between 1 and 65535"
        if not 1 <= int(raw_port) <= 65535:
            return "URL port must be between 1 and 65535"

    try:
        parsed = httpx.URL(url)
        scheme = parsed.scheme.casefold()
        hostname = parsed.raw_host.decode("ascii")
        explicit_port = parsed.port
    except (AttributeError, TypeError, UnicodeError, httpx.InvalidURL):
        return "Invalid URL format"

    allowed = {candidate.casefold() for candidate in allowed_schemes}
    if scheme not in {"http", "https"} or scheme not in allowed:
        return "Invalid URL scheme"

    if not hostname:
        return "URL must have a valid hostname"
    if parsed.username or parsed.password:
        return "URL credentials are not allowed"
    if explicit_port is not None and not 1 <= explicit_port <= 65535:
        return "URL port must be between 1 and 65535"

    normalized_hostname = hostname.casefold().rstrip(".")
    if normalized_hostname in {
        "localhost",
        "localhost.localdomain",
    } or normalized_hostname.endswith(".localhost"):
        return "Localhost URLs are not allowed"

    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None

    if literal is not None:
        try:
            select_public_address((str(literal),))
        except PinnedAddressError:
            return "Non-public IP addresses are not allowed"
        return None

    port = (
        explicit_port
        if explicit_port is not None
        else (443 if scheme == "https" else 80)
    )
    resolve = resolver or resolve_host_addresses
    try:
        addresses = await resolve(hostname, port)
        select_public_address(addresses)
    except PinnedAddressError:
        return "Host must resolve only to public IP addresses"
    except (OSError, UnicodeError):
        return "Host could not be resolved"
    return None


class PinnedPublicNetworkBackend(
    _NETWORK_BACKEND_BASE  # type: ignore[misc,valid-type]
):
    """Resolve, validate, pin, and peer-check every TCP connection."""

    def __init__(
        self,
        *,
        resolver: AddressResolver | None = None,
        delegate: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        ensure_runtime_compatibility()
        if delegate is None:
            from httpcore._backends.auto import AutoBackend

            delegate = AutoBackend()
        self._resolver = resolver or resolve_host_addresses
        self._delegate = delegate

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[tuple] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + max(float(timeout), 0.0)
        stream: httpcore.AsyncNetworkStream | None = None
        try:
            if deadline is None:
                addresses = await self._resolver(host, port)
            else:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                addresses = await asyncio.wait_for(
                    self._resolver(host, port), timeout=remaining
                )
            selected = select_public_address(addresses)
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                raise asyncio.TimeoutError
            delegate_call = self._delegate.connect_tcp(
                selected,
                port,
                timeout=remaining,
                local_address=local_address,
                socket_options=socket_options,
            )
            if remaining is None:
                stream = await delegate_call
            else:
                stream = await asyncio.wait_for(delegate_call, timeout=remaining)
        except asyncio.TimeoutError as error:
            if stream is not None:
                with suppress(Exception):
                    await stream.aclose()
            raise httpcore.ConnectTimeout("Pinned connection timed out") from error
        if not self._peer_matches(stream, selected, port):
            with suppress(Exception):
                await stream.aclose()
            raise PinnedAddressError("Connected peer did not match pinned address")
        return stream

    @staticmethod
    def _peer_matches(
        stream: httpcore.AsyncNetworkStream, selected: str, port: int
    ) -> bool:
        get_extra_info = getattr(stream, "get_extra_info", None)
        if not callable(get_extra_info):
            return False
        try:
            peer = get_extra_info("server_addr")
            if not isinstance(peer, (tuple, list)) or len(peer) < 2:
                return False
            peer_ip, peer_port = peer[0], peer[1]
            if (
                not isinstance(peer_ip, str)
                or not isinstance(peer_port, int)
                or isinstance(peer_port, bool)
            ):
                return False
            return str(ipaddress.ip_address(peer_ip)) == selected and peer_port == port
        except Exception:
            return False

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[tuple] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.UnsupportedProtocol("Unix sockets are not supported")

    async def sleep(self, seconds: float) -> None:
        await self._delegate.sleep(seconds)


class _PinnedResponseStream(_BYTE_STREAM_BASE):  # type: ignore[misc,valid-type]
    """Bridge an httpcore async response body into httpx."""

    def __init__(self, stream: AsyncIterable[bytes]) -> None:
        self._stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for part in self._stream:
            yield part

    async def aclose(self) -> None:
        close = getattr(self._stream, "aclose", None)
        if callable(close):
            await close()


async def read_bounded_identity_body(response: object, *, max_bytes: int) -> bytes:
    """Read only an unencoded raw response body within an exact byte cap."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    headers = getattr(response, "headers", None)
    if headers is None or not hasattr(headers, "get"):
        raise PinnedResponseError("Response headers are unavailable")
    content_encoding = headers.get("content-encoding", "")
    if not isinstance(
        content_encoding, str
    ) or content_encoding.strip().casefold() not in {
        "",
        "identity",
    }:
        raise PinnedResponseError("Encoded response bodies are not supported")
    content_length = headers.get("content-length")
    if content_length:
        try:
            declared = int(content_length)
        except (TypeError, ValueError) as error:
            raise PinnedResponseError("Response length is invalid") from error
        if declared < 0 or declared > max_bytes:
            raise PinnedResponseError("Response exceeds the ingestion size limit")

    iterator = getattr(response, "aiter_raw", None)
    if not callable(iterator):
        raise PinnedResponseError("Raw response streaming is unavailable")
    size = 0
    chunks: list[bytes] = []
    async for chunk in iterator(chunk_size=min(65_536, max_bytes + 1)):
        if not isinstance(chunk, bytes):
            raise PinnedResponseError("Response body chunk is invalid")
        size += len(chunk)
        if size > max_bytes:
            raise PinnedResponseError("Response exceeds the ingestion size limit")
        chunks.append(chunk)
    return b"".join(chunks)


class PinnedAsyncHTTPTransport(_TRANSPORT_BASE):  # type: ignore[misc,valid-type]
    """httpx transport retaining URL authority while pinning public TCP peers."""

    def __init__(
        self,
        *,
        resolver: AddressResolver | None = None,
        delegate: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        ensure_runtime_compatibility()
        ssl_context = httpx.create_ssl_context(verify=True, trust_env=False)
        backend = PinnedPublicNetworkBackend(resolver=resolver, delegate=delegate)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            proxy=None,
            max_connections=1,
            max_keepalive_connections=0,
            http1=True,
            http2=False,
            retries=0,
            uds=None,
            network_backend=backend,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        extensions = dict(request.extensions)
        self._validate_sni_extension(request.url.raw_host, extensions)
        response = await self._pool.handle_async_request(
            httpcore.Request(
                method=request.method,
                url=httpcore.URL(
                    scheme=request.url.raw_scheme,
                    host=request.url.raw_host,
                    port=request.url.port,
                    # HTTPX 0.28 exposes the raw origin-form target as
                    # ``raw_path`` (including the unmodified query bytes).
                    # Do not reconstruct it from decoded path/query fields.
                    target=request.url.raw_path,
                ),
                headers=request.headers.raw,
                content=request.stream,
                extensions=extensions,
            )
        )
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_PinnedResponseStream(response.stream),
            extensions=response.extensions,
        )

    @staticmethod
    def _validate_sni_extension(raw_host: bytes, extensions: dict[str, object]) -> None:
        override = extensions.pop("sni_hostname", None)
        if override is None:
            return
        try:
            authority = raw_host.decode("ascii").rstrip(".").casefold()
            if isinstance(override, bytes):
                supplied = override.decode("ascii")
            elif isinstance(override, str):
                supplied = override
            else:
                raise ValueError
            if supplied.rstrip(".").casefold() != authority:
                raise ValueError
        except (UnicodeError, ValueError) as error:
            raise httpcore.LocalProtocolError(
                "Conflicting TLS server name is not permitted"
            ) from error

    async def aclose(self) -> None:
        await self._pool.aclose()
