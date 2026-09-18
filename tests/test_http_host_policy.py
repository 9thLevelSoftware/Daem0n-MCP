"""Host authority and reverse-proxy trust are explicit deployment boundaries."""

import pytest

from daem0nmcp.transport_security import (
    HostPolicyMiddleware,
    TransportSecurityError,
    _allowed_http_hosts,
    _strict_json_value,
    build_uvicorn_security_config,
)


@pytest.mark.parametrize(
    "hosts",
    [
        [],
        [b"evil.example"],
        [b"127.0.0.1:8765", b"evil.example"],
        [b"127.0.0.1:8765@evil.example"],
        [b"127.0.0.1:8765/path"],
        [b"127.0.0.1:8765\t"],
        [b"127.0.0.1:8765\x00"],
    ],
)
async def test_untrusted_or_ambiguous_host_rejected_before_body(hosts):
    messages = []

    async def forbidden(*_):
        raise AssertionError("host rejection must precede body reads and dispatch")

    async def send(message):
        messages.append(message)

    await HostPolicyMiddleware(forbidden, allowed_hosts=("127.0.0.1:8765",))(
        {"type": "http", "headers": [(b"host", value) for value in hosts]},
        forbidden,
        send,
    )
    assert messages[0]["status"] == 403


@pytest.mark.parametrize("host", [b"mcp.example:443", b"MCP.EXAMPLE:443"])
async def test_exact_configured_host_admitted(host):
    calls = []

    async def downstream(*_):
        calls.append(True)

    await HostPolicyMiddleware(downstream, allowed_hosts=("mcp.example:443",))(
        {"type": "http", "headers": [(b"host", host)]}, None, None
    )
    assert calls == [True]


def test_local_and_remote_host_configuration():
    assert _allowed_http_hosts("127.0.0.1", 8765, {}) == (
        "127.0.0.1:8765",
        "localhost:8765",
    )
    assert _allowed_http_hosts("::1", 8765, {}) == ("[::1]:8765", "localhost:8765")
    assert _allowed_http_hosts(
        "0.0.0.0", 8765, {"DAEM0NMCP_ALLOWED_HOSTS": "mcp.example,mcp.example:443"}
    ) == ("mcp.example", "mcp.example:443")
    for raw in (
        "",
        "*",
        "*.example",
        "mcp.example,",
        "mcp.example:0",
        "http://mcp.example",
        "a b",
        "[::1%eth0]",
    ):
        with pytest.raises(TransportSecurityError, match="INVALID_HOST_CONFIGURATION"):
            _allowed_http_hosts("0.0.0.0", 8765, {"DAEM0NMCP_ALLOWED_HOSTS": raw})


def test_forwarded_headers_disabled_by_default_and_only_exact_proxy_ips_allowed():
    assert build_uvicorn_security_config({"FORWARDED_ALLOW_IPS": "*"}) == {
        "proxy_headers": False,
        "forwarded_allow_ips": "",
    }
    assert build_uvicorn_security_config(
        {"DAEM0NMCP_TRUSTED_PROXY_IPS": "::1,127.0.0.1"}
    ) == {"proxy_headers": True, "forwarded_allow_ips": "127.0.0.1,::1"}
    for value in ("*", "127.0.0.0/8", "localhost", "127.0.0.1,", " 127.0.0.1"):
        with pytest.raises(TransportSecurityError, match="INVALID_PROXY_CONFIGURATION"):
            build_uvicorn_security_config({"DAEM0NMCP_TRUSTED_PROXY_IPS": value})


def test_json_nesting_is_explicit_and_ignores_quoted_delimiters():
    import json

    value = {"text": '[{\\"' * 1000}
    assert _strict_json_value(json.dumps(value), max_bytes=100_000) == value
    assert _strict_json_value("[" * 64 + "0" + "]" * 64, max_bytes=1000)
    with pytest.raises(ValueError, match="nesting"):
        _strict_json_value("[" * 65 + "0" + "]" * 65, max_bytes=1000)
    for raw in ("1e9999", "-1e9999", "NaN", "Infinity"):
        with pytest.raises(ValueError, match="non-finite"):
            _strict_json_value(raw, max_bytes=1000)
