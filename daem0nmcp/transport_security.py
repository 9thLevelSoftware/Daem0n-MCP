"""Transport authentication configuration and bind-host security policy."""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from starlette.middleware import Middleware

JWT_VERIFIER = "fastmcp.server.auth.providers.jwt.JWTVerifier"
_PRODUCTION_PROVIDER_CLASSES = frozenset({JWT_VERIFIER})
_ALLOWED_CORS_METHODS = ("DELETE", "GET", "POST")
_ALLOWED_CORS_HEADERS = frozenset(
    {
        "accept",
        "authorization",
        "content-type",
        "last-event-id",
        "mcp-protocol-version",
        "mcp-session-id",
    }
)
_MAX_ORIGINS = 32
_MAX_ORIGIN_CONFIG_BYTES = 8192
_MAX_HTTP_JSON_BYTES = 2 * 1024 * 1024
_MAX_STDIO_JSON_BYTES = 2 * 1024 * 1024


class TransportSecurityError(RuntimeError):
    """Stable configuration error raised before starting an unsafe listener."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class AuthConfiguration:
    """Validated arguments for a supported FastMCP authentication provider."""

    provider: str
    kwargs: dict[str, str]


def _normalized_origin(value: str) -> str:
    if (
        not value
        or value != value.strip()
        or value == "*"
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise TransportSecurityError(
            "INVALID_ORIGIN_CONFIGURATION",
            "allowed origins must be exact HTTP origins",
        )
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise TransportSecurityError(
            "INVALID_ORIGIN_CONFIGURATION",
            "allowed origins must have a valid port",
        ) from exc
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise TransportSecurityError(
            "INVALID_ORIGIN_CONFIGURATION",
            "allowed origins must be exact HTTP origins",
        )
    scheme = parsed.scheme.casefold()
    try:
        hostname = parsed.hostname.encode("idna").decode("ascii").casefold()
    except UnicodeError as exc:
        raise TransportSecurityError(
            "INVALID_ORIGIN_CONFIGURATION",
            "allowed origins must have a valid host",
        ) from exc
    if ":" in hostname:
        hostname = f"[{hostname}]"
    default_port = 80 if scheme == "http" else 443
    port_suffix = "" if port in {None, default_port} else f":{port}"
    return f"{scheme}://{hostname}{port_suffix}"


def _configured_origins(environ: Mapping[str, str]) -> tuple[str, ...]:
    raw = environ.get("DAEM0NMCP_ALLOWED_ORIGINS", "")
    try:
        raw_size = len(raw.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise TransportSecurityError(
            "INVALID_ORIGIN_CONFIGURATION",
            "allowed origins must be valid text",
        ) from exc
    if raw_size > _MAX_ORIGIN_CONFIG_BYTES:
        raise TransportSecurityError(
            "INVALID_ORIGIN_CONFIGURATION",
            "allowed origin configuration is too large",
        )
    if not raw:
        return ()
    values = raw.split(",")
    if len(values) > _MAX_ORIGINS or any(not value for value in values):
        raise TransportSecurityError(
            "INVALID_ORIGIN_CONFIGURATION",
            "allowed origin configuration is invalid",
        )
    return tuple(sorted({_normalized_origin(value) for value in values}))


class OriginPolicyMiddleware:
    """Strict ASGI browser-origin boundary for the MCP HTTP endpoint."""

    def __init__(self, app: Any, *, allowed_origins: tuple[str, ...]) -> None:
        self._app = app
        self._allowed_origins = frozenset(allowed_origins)

    @staticmethod
    async def _reject(send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [(b"content-length", b"0")],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    @staticmethod
    def _header_values(scope: Mapping[str, Any], name: bytes) -> list[bytes]:
        headers = scope.get("headers", ())
        if not isinstance(headers, (list, tuple)):
            return []
        return [
            value
            for key, value in headers
            if isinstance(key, bytes)
            and isinstance(value, bytes)
            and key.lower() == name
        ]

    @staticmethod
    def _cors_headers(origin: str) -> list[tuple[bytes, bytes]]:
        return [
            (b"access-control-allow-origin", origin.encode("ascii")),
            (b"access-control-expose-headers", b"Mcp-Session-Id"),
            (b"vary", b"Origin"),
        ]

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if not isinstance(scope, Mapping) or scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        origin_values = self._header_values(scope, b"origin")
        if not origin_values:
            await self._app(scope, receive, send)
            return
        if len(origin_values) != 1:
            await self._reject(send)
            return
        try:
            origin = _normalized_origin(origin_values[0].decode("ascii"))
        except (UnicodeDecodeError, TransportSecurityError):
            await self._reject(send)
            return
        if origin not in self._allowed_origins:
            await self._reject(send)
            return

        method = str(scope.get("method", "")).upper()
        requested_methods = self._header_values(scope, b"access-control-request-method")
        if method == "OPTIONS" and requested_methods:
            if len(requested_methods) != 1:
                await self._reject(send)
                return
            try:
                requested_method = requested_methods[0].decode("ascii").upper()
            except UnicodeDecodeError:
                await self._reject(send)
                return
            requested_headers = self._header_values(
                scope, b"access-control-request-headers"
            )
            if len(requested_headers) > 1:
                await self._reject(send)
                return
            header_names: set[str] = set()
            if requested_headers:
                try:
                    header_names = {
                        value.strip().casefold()
                        for value in requested_headers[0].decode("ascii").split(",")
                        if value.strip()
                    }
                except UnicodeDecodeError:
                    await self._reject(send)
                    return
            if (
                requested_method not in _ALLOWED_CORS_METHODS
                or not header_names <= _ALLOWED_CORS_HEADERS
            ):
                await self._reject(send)
                return
            headers = self._cors_headers(origin)
            headers.extend(
                [
                    (
                        b"access-control-allow-methods",
                        b", ".join(
                            value.encode("ascii") for value in _ALLOWED_CORS_METHODS
                        ),
                    ),
                    (
                        b"access-control-allow-headers",
                        b", ".join(
                            value.encode("ascii")
                            for value in sorted(_ALLOWED_CORS_HEADERS)
                        ),
                    ),
                    (b"access-control-max-age", b"600"),
                    (b"content-length", b"0"),
                ]
            )
            await send(
                {
                    "type": "http.response.start",
                    "status": 204,
                    "headers": headers,
                }
            )
            await send({"type": "http.response.body", "body": b""})
            return

        async def send_with_origin(message: Any) -> None:
            if (
                isinstance(message, dict)
                and message.get("type") == "http.response.start"
            ):
                headers = list(message.get("headers", ()))
                headers.extend(self._cors_headers(origin))
                message = {**message, "headers": headers}
            await send(message)

        await self._app(scope, receive, send_with_origin)


class _DuplicateJsonKeyError(ValueError):
    pass


def _strict_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKeyError
        value[key] = item
    return value


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _finite_json_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite JSON number")
    return result


def _validate_json_nesting(text: str) -> None:
    # Enforce a transport ceiling before the decoder allocates nested objects;
    # interpreter recursion limits differ across versions and imported packages.
    depth = 0
    quoted = False
    escaped = False
    for character in text:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "[{":
            depth += 1
            if depth > 64:
                raise ValueError("JSON nesting exceeds transport limit")
        elif character in "]}":
            depth -= 1


def _strict_json_value(raw: str | bytes | bytearray, *, max_bytes: int) -> Any:
    if isinstance(raw, str):
        encoded = raw.encode("utf-8")
        text = raw
    elif isinstance(raw, (bytes, bytearray)):
        encoded = bytes(raw)
        text = encoded.decode("utf-8")
    else:
        raise TypeError("JSON input must be text or bytes")
    if len(encoded) > max_bytes:
        raise ValueError("JSON input is too large")
    _validate_json_nesting(text)
    return json.loads(
        text,
        object_pairs_hook=_strict_json_pairs,
        parse_constant=_reject_json_constant,
        parse_float=_finite_json_float,
    )


@contextmanager
def strict_stdio_json_boundary(message_model: Any) -> Iterator[None]:
    """Make the pinned SDK's stdio model parser duplicate-key strict.

    The pinned MCP SDK invokes ``JSONRPCMessage.model_validate_json`` directly for
    every line.  The Pydantic parser otherwise accepts the last duplicate key,
    so the reviewed stdio launcher temporarily replaces that one parse seam
    for the lifetime of the single stdio server.
    """

    model_class = isinstance(message_model, type)
    parser_name = "model_validate_json" if model_class else "validate_json"
    if not hasattr(message_model, parser_name):
        raise TypeError("message_model must expose the SDK JSON parser")
    namespace = vars(message_model)
    had_override = parser_name in namespace
    original_descriptor = namespace.get(parser_name)
    original_parser = getattr(message_model, parser_name)

    def validate_json(
        raw: str | bytes | bytearray,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        value = _strict_json_value(raw, max_bytes=_MAX_STDIO_JSON_BYTES)
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        return original_parser(canonical, *args, **kwargs)

    if model_class:

        def model_validate_json(
            cls: type[Any],
            raw: str | bytes | bytearray,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            del cls
            return validate_json(raw, *args, **kwargs)

        replacement: Any = classmethod(model_validate_json)
    else:
        replacement = validate_json
    setattr(message_model, parser_name, replacement)
    try:
        yield
    finally:
        if had_override:
            setattr(message_model, parser_name, original_descriptor)
        else:
            delattr(message_model, parser_name)


class StrictJsonBodyMiddleware:
    """Reject ambiguous JSON objects before the MCP SDK parses request bodies."""

    def __init__(self, app: Any) -> None:
        self._app = app

    @staticmethod
    async def _reject(send: Any, status: int) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-length", b"0")],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if (
            not isinstance(scope, Mapping)
            or scope.get("type") != "http"
            or str(scope.get("method", "")).upper() != "POST"
        ):
            await self._app(scope, receive, send)
            return

        body = bytearray()
        while True:
            message = await receive()
            if not isinstance(message, Mapping):
                await self._reject(send, 400)
                return
            message_type = message.get("type")
            if message_type == "http.disconnect":
                await self._reject(send, 400)
                return
            if message_type != "http.request":
                await self._reject(send, 400)
                return
            chunk = message.get("body", b"")
            if not isinstance(chunk, bytes):
                await self._reject(send, 400)
                return
            body.extend(chunk)
            if len(body) > _MAX_HTTP_JSON_BYTES:
                await self._reject(send, 413)
                return
            if message.get("more_body") is not True:
                break

        try:
            _strict_json_value(
                body,
                max_bytes=_MAX_HTTP_JSON_BYTES,
            )
        except (RecursionError, TypeError, UnicodeError, ValueError):
            await self._reject(send, 400)
            return

        delivered = False

        async def replay() -> dict[str, Any]:
            nonlocal delivered
            if delivered:
                # Exhausting the request body does not disconnect the client.
                # Streaming responses keep listening here until the transport
                # reports a real disconnect; fabricating one truncates SSE.
                return await receive()
            delivered = True
            return {
                "type": "http.request",
                "body": bytes(body),
                "more_body": False,
            }

        await self._app(scope, replay, send)


def build_http_origin_middleware(
    host: str,
    port: int,
    *,
    environ: Mapping[str, str] | None = None,
) -> list[Middleware]:
    """Build one explicit origin policy for the reviewed HTTP listener."""

    env = os.environ if environ is None else environ
    allowed = set(_configured_origins(env))
    if _is_loopback_host(host):
        normalized_host = host.strip()
        if normalized_host.startswith("[") and normalized_host.endswith("]"):
            normalized_host = normalized_host[1:-1]
        if ":" in normalized_host:
            normalized_host = f"[{normalized_host}]"
        allowed.add(_normalized_origin(f"http://{normalized_host}:{port}"))
    return [
        Middleware(
            OriginPolicyMiddleware,
            allowed_origins=tuple(sorted(allowed)),
        )
    ]


def build_http_transport_middleware(
    host: str,
    port: int,
    *,
    environ: Mapping[str, str] | None = None,
) -> list[Middleware]:
    """Build the complete reviewed Streamable HTTP parsing/origin boundary."""

    return [
        Middleware(
            HostPolicyMiddleware, allowed_hosts=_allowed_http_hosts(host, port, environ)
        ),
        Middleware(StrictJsonBodyMiddleware),
        *build_http_origin_middleware(host, port, environ=environ),
    ]


def _normalized_authority(value: str) -> str:
    if (
        not value
        or len(value) > 300
        or value != value.strip()
        or "%" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("invalid host authority")
    parsed = urlsplit("http://" + value)
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid host authority")
    hostname = parsed.hostname
    if not hostname or any(character.isspace() for character in value):
        raise ValueError("invalid host authority")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        hostname = hostname.encode("idna").decode("ascii").lower()
        if len(hostname) > 253 or any(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is None
            for label in hostname.split(".")
        ):
            raise ValueError("invalid host authority") from None
    else:
        hostname = (
            f"[{address.compressed}]" if address.version == 6 else address.compressed
        )
    port = parsed.port
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("invalid host authority")
    if value.endswith(":"):
        raise ValueError("invalid host authority")
    return hostname + (f":{port}" if port is not None else "")


def _allowed_http_hosts(
    host: str, port: int, environ: Mapping[str, str] | None
) -> tuple[str, ...]:
    env = os.environ if environ is None else environ
    raw = env.get("DAEM0NMCP_ALLOWED_HOSTS", "")
    try:
        if len(raw.encode("utf-8")) > 8192:
            raise ValueError("host configuration too large")
        values = raw.split(",") if raw else []
        if len(values) > 32:
            raise ValueError("too many hosts")
        allowed = {_normalized_authority(value) for value in values}
        if _is_loopback_host(host):
            normalized = host.strip("[]")
            authority = f"[{normalized}]" if ":" in normalized else normalized
            allowed.add(_normalized_authority(f"{authority}:{port}"))
            allowed.add(_normalized_authority(f"localhost:{port}"))
            if port == 80:
                allowed.update({_normalized_authority(authority), "localhost"})
        if not allowed:
            raise ValueError("remote listener requires explicit hosts")
    except (UnicodeError, ValueError) as exc:
        raise TransportSecurityError(
            "INVALID_HOST_CONFIGURATION",
            "configure exact DAEM0NMCP_ALLOWED_HOSTS authorities for this listener",
        ) from exc
    return tuple(sorted(allowed))


class HostPolicyMiddleware:
    """Reject ambiguous or unconfigured Host headers before reading bodies."""

    def __init__(self, app: Any, *, allowed_hosts: tuple[str, ...]) -> None:
        self._app = app
        self._allowed_hosts = frozenset(allowed_hosts)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        hosts = OriginPolicyMiddleware._header_values(scope, b"host")
        try:
            if len(hosts) != 1:
                raise ValueError("ambiguous host")
            authority = _normalized_authority(hosts[0].decode("ascii"))
            if authority not in self._allowed_hosts:
                raise ValueError("unconfigured host")
        except (UnicodeError, ValueError):
            await OriginPolicyMiddleware._reject(send)
            return
        await self._app(scope, receive, send)


def build_uvicorn_security_config(
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Forwarding is disabled unless specific reverse-proxy IPs are configured."""
    env = os.environ if environ is None else environ
    raw = env.get("DAEM0NMCP_TRUSTED_PROXY_IPS", "")
    if not raw:
        return {"proxy_headers": False, "forwarded_allow_ips": ""}
    try:
        values = raw.split(",")
        if len(raw) > 2048 or len(values) > 32:
            raise ValueError("proxy configuration too large")
        addresses = sorted({str(ipaddress.ip_address(value)) for value in values})
    except ValueError as exc:
        raise TransportSecurityError(
            "INVALID_PROXY_CONFIGURATION",
            "trusted proxies must be explicit IP addresses",
        ) from exc
    return {"proxy_headers": True, "forwarded_allow_ips": ",".join(addresses)}


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip()
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    if normalized.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def get_auth_configuration(
    environ: Mapping[str, str] | None = None,
) -> AuthConfiguration | None:
    """Validate the supported FastMCP production provider environment."""
    env = os.environ if environ is None else environ
    provider = env.get("FASTMCP_SERVER_AUTH", "").strip()
    if not provider:
        return None
    if provider not in _PRODUCTION_PROVIDER_CLASSES:
        raise TransportSecurityError(
            "INVALID_AUTH_CONFIGURATION",
            "a supported production FastMCP token verifier is required",
        )

    required = {
        "jwks_uri": "FASTMCP_SERVER_AUTH_JWT_JWKS_URI",
        "issuer": "FASTMCP_SERVER_AUTH_JWT_ISSUER",
        "audience": "FASTMCP_SERVER_AUTH_JWT_AUDIENCE",
    }
    kwargs = {name: env.get(key, "").strip() for name, key in required.items()}
    if any(not value for value in kwargs.values()):
        raise TransportSecurityError(
            "INVALID_AUTH_CONFIGURATION",
            "JWTVerifier requires JWKS URI, issuer, and audience",
        )
    return AuthConfiguration(provider=provider, kwargs=kwargs)


def build_fastmcp_auth(
    environ: Mapping[str, str] | None = None,
) -> Any | None:
    """Construct the configured FastMCP provider without custom middleware."""
    configuration = get_auth_configuration(environ)
    if configuration is None:
        return None

    from fastmcp.server.auth.providers.jwt import JWTVerifier

    return JWTVerifier(
        jwks_uri=configuration.kwargs["jwks_uri"],
        issuer=configuration.kwargs["issuer"],
        audience=configuration.kwargs["audience"],
    )


def _provider_class_path(auth_provider: object) -> str:
    provider_type = type(auth_provider)
    return f"{provider_type.__module__}.{provider_type.__qualname__}"


def validate_transport_security(
    host: str,
    *,
    auth_provider: object | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Reject non-loopback HTTP binds unless production auth is configured."""
    if _is_loopback_host(host):
        return

    if auth_provider is not None:
        if _provider_class_path(auth_provider) in _PRODUCTION_PROVIDER_CLASSES:
            return
        raise TransportSecurityError(
            "INVALID_AUTH_CONFIGURATION",
            "development and unsupported authentication providers are not permitted",
        )

    configuration = get_auth_configuration(environ)
    if configuration is not None:
        return

    raise TransportSecurityError(
        "REMOTE_BIND_REQUIRES_AUTH",
        "non-loopback listeners require a production FastMCP token verifier",
    )
