"""Deterministic tests for the pinned URL-ingestion HTTP adapter."""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import ipaddress
import logging
import socket
import ssl
import subprocess
import sys
import threading
import time
import types
import unittest
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib

try:
    import httpcore
    import httpx

    if importlib.util.find_spec("packaging") is None:
        raise ModuleNotFoundError("packaging")
except ModuleNotFoundError as error:  # pragma: no cover - dependency profile gate
    raise unittest.SkipTest("the apps dependency profile is unavailable") from error


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SPECIAL_ADDRESS_CONTROLS = (
    "192.0.0.8",
    "192.0.0.11",
    "192.88.99.2",
    "3fff::1",
    "100:0:0:1::1",
    "5f00::1",
    "::ffff:5db8:d822",
    "64:ff9b::5db8:d822",
    "64:ff9b:1::5db8:d822",
    "2001::1",
    "2002:5db8:d822::",
)


@contextmanager
def _loaded_agency_tools_module():
    """Load the real ingestion helpers with unavailable server deps stubbed."""

    module_name = "daem0nmcp.tools._agency_task5_test"
    agency = types.ModuleType("daem0nmcp.agency")

    class _CapabilityManager:
        pass

    class _CapabilityScope:
        EXECUTE_CODE = "execute_code"

    class _SandboxExecutor:
        def __init__(self, *args, **kwargs):
            self.available = False

    agency.CapabilityManager = _CapabilityManager
    agency.CapabilityScope = _CapabilityScope
    agency.SandboxExecutor = _SandboxExecutor
    agency.check_capability = lambda *args, **kwargs: None

    config = types.ModuleType("daem0nmcp.config")
    config.settings = types.SimpleNamespace(
        max_content_size=1_000_000,
        max_chunks=100,
        ingest_timeout=0.02,
        allowed_url_schemes=("http", "https"),
    )
    covenant = types.ModuleType("daem0nmcp.covenant")
    covenant.legacy_entrypoint = lambda *args, **kwargs: lambda function: function
    context_manager = types.ModuleType("daem0nmcp.context_manager")
    context_manager._default_project_path = None
    context_manager._missing_project_path_error = lambda: {"error": "missing"}

    async def get_project_context(project_path):
        return types.SimpleNamespace(project_path=project_path, memory_manager=object())

    context_manager.get_project_context = get_project_context
    logging_config = types.ModuleType("daem0nmcp.logging_config")
    logging_config.with_request_id = lambda function: function
    mcp_instance = types.ModuleType("daem0nmcp.mcp_instance")
    mcp_instance.mcp = types.SimpleNamespace(
        tool=lambda *args, **kwargs: lambda function: function
    )
    stubs = {
        "daem0nmcp.agency": agency,
        "daem0nmcp.config": config,
        "daem0nmcp.covenant": covenant,
        "daem0nmcp.context_manager": context_manager,
        "daem0nmcp.logging_config": logging_config,
        "daem0nmcp.mcp_instance": mcp_instance,
    }
    previous = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    spec = importlib.util.spec_from_file_location(
        module_name, PROJECT_ROOT / "daem0nmcp" / "tools" / "agency_tools.py"
    )
    if spec is None or spec.loader is None:
        raise AssertionError("could not load agency ingestion helpers")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(module_name, None)
        for name, value in previous.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


class SequenceResolver:
    """Return one scripted DNS answer set per awaited resolution."""

    def __init__(self, *answers: tuple[str, ...]) -> None:
        self.answers = deque(answers)
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, host: str, port: int) -> tuple[str, ...]:
        self.calls.append((host, port))
        if not self.answers:
            raise AssertionError("unexpected resolver call")
        return self.answers.popleft()


class ScriptedNetworkStream(httpcore.AsyncNetworkStream):
    """In-memory httpcore stream with an explicit connected peer."""

    def __init__(
        self,
        *,
        server_addr: object = ("93.184.216.34", 443),
        response: bytes = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n",
    ) -> None:
        self.server_addr = server_addr
        self.response = deque([response])
        self.writes: list[bytes] = []
        self.tls_calls: list[tuple[ssl.SSLContext, str | None, float | None]] = []
        self.closed = False

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        if not self.response:
            return b""
        return self.response.popleft()

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.writes.append(buffer)

    async def aclose(self) -> None:
        self.closed = True

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> ScriptedNetworkStream:
        self.tls_calls.append((ssl_context, server_hostname, timeout))
        return self

    def get_extra_info(self, info: str) -> object:
        if info == "server_addr":
            return self.server_addr
        return None


class RecordingBackend(httpcore.AsyncNetworkBackend):
    """Record literal TCP dials and return scripted in-memory streams."""

    def __init__(self, *streams: ScriptedNetworkStream) -> None:
        self.streams = deque(streams)
        self.connect_calls: list[dict[str, object]] = []
        self.sleep_calls: list[float] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: object = None,
    ) -> ScriptedNetworkStream:
        self.connect_calls.append(
            {
                "host": host,
                "port": port,
                "timeout": timeout,
                "local_address": local_address,
                "socket_options": socket_options,
            }
        )
        if not self.streams:
            raise AssertionError("unexpected delegate dial")
        return self.streams.popleft()

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: object = None,
    ) -> ScriptedNetworkStream:
        raise AssertionError("Unix socket delegation is forbidden")

    async def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)


class TestPinnedDependencyContract(unittest.TestCase):
    """Catch dependency drift outside the adapter versions we validate."""

    def test_apps_extra_pins_supported_httpx_and_httpcore_versions(self):
        with (PROJECT_ROOT / "pyproject.toml").open("rb") as project_file:
            project = tomllib.load(project_file)

        apps = project["project"]["optional-dependencies"]["apps"]

        self.assertIn("httpx>=0.28.1,<0.29", apps)
        self.assertIn("httpcore>=1.0.9,<1.1", apps)
        self.assertIn("packaging>=24.0", apps)

    def test_live_httpcore_interfaces_match_the_adapter_contract(self):
        import httpcore

        pool_parameters = inspect.signature(httpcore.AsyncConnectionPool).parameters
        connect_parameters = tuple(
            inspect.signature(httpcore.AsyncNetworkBackend.connect_tcp).parameters
        )
        tls_parameters = tuple(
            inspect.signature(httpcore.AsyncNetworkStream.start_tls).parameters
        )

        self.assertIn("network_backend", pool_parameters)
        self.assertEqual(
            connect_parameters,
            (
                "self",
                "host",
                "port",
                "timeout",
                "local_address",
                "socket_options",
            ),
        )
        self.assertEqual(
            tls_parameters,
            ("self", "ssl_context", "server_hostname", "timeout"),
        )

    def test_adapter_accepts_the_supported_installed_runtime(self):
        from daem0nmcp.pinned_http import ensure_runtime_compatibility

        versions = ensure_runtime_compatibility()

        self.assertEqual(versions, (httpx.__version__, httpcore.__version__))

    def test_adapter_rejects_an_unvalidated_runtime_with_remediation(self):
        from daem0nmcp.pinned_http import (
            PinnedTransportCompatibilityError,
            ensure_runtime_compatibility,
        )

        with (
            patch.object(httpx, "__version__", "0.29.0"),
            self.assertRaises(PinnedTransportCompatibilityError) as raised,
        ):
            ensure_runtime_compatibility()

        self.assertIn("httpx 0.29.0", str(raised.exception))
        self.assertIn("pip install 'daem0nmcp[apps]'", str(raised.exception))

    def test_missing_transport_interfaces_reach_owned_remediation(self):
        script = f"""
import runpy
import sys
import types

httpx = types.ModuleType('httpx')
httpx.__version__ = '0.28.1'
httpcore = types.ModuleType('httpcore')
httpcore.__version__ = '1.0.9'
sys.modules['httpx'] = httpx
sys.modules['httpcore'] = httpcore
namespace = runpy.run_path({str(PROJECT_ROOT / "daem0nmcp" / "pinned_http.py")!r})
try:
    namespace['ensure_runtime_compatibility']()
except namespace['PinnedTransportCompatibilityError'] as error:
    assert "pip install 'daem0nmcp[apps]'" in str(error)
else:
    raise AssertionError('incompatible interfaces were accepted')
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_malformed_present_class_bases_reach_owned_remediation(self):
        script = f"""
import runpy
import sys
import types

httpx = types.ModuleType('httpx')
httpx.__version__ = '0.28.1'
httpx.AsyncByteStream = None
httpx.AsyncBaseTransport = object()
httpcore = types.ModuleType('httpcore')
httpcore.__version__ = '1.0.9'
httpcore.ConnectError = object()
httpcore.AsyncNetworkBackend = object()
sys.modules['httpx'] = httpx
sys.modules['httpcore'] = httpcore
namespace = runpy.run_path({str(PROJECT_ROOT / "daem0nmcp" / "pinned_http.py")!r})
try:
    namespace['ensure_runtime_compatibility']()
except namespace['PinnedTransportCompatibilityError']:
    pass
else:
    raise AssertionError('malformed present interfaces were accepted')
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_uninspectable_transport_interfaces_reach_owned_remediation(self):
        from daem0nmcp.pinned_http import (
            PinnedTransportCompatibilityError,
            ensure_runtime_compatibility,
        )

        invalid_interfaces = (
            ("AsyncNetworkBackend", object),
            ("AsyncConnectionPool", object()),
        )
        for attribute, replacement in invalid_interfaces:
            with (
                self.subTest(attribute=attribute),
                patch.object(httpcore, attribute, replacement),
                self.assertRaises(PinnedTransportCompatibilityError),
            ):
                ensure_runtime_compatibility()

        invalid_httpx_interfaces = (
            ("URL", None),
            ("InvalidURL", None),
            ("InvalidURL", object),
        )
        for attribute, replacement in invalid_httpx_interfaces:
            with (
                self.subTest(attribute=f"httpx.{attribute}"),
                patch.object(httpx, attribute, replacement),
                self.assertRaises(PinnedTransportCompatibilityError),
            ):
                ensure_runtime_compatibility()

    def test_version_gate_accepts_post_local_and_rejects_prereleases(self):
        from daem0nmcp.pinned_http import (
            PinnedTransportCompatibilityError,
            ensure_runtime_compatibility,
        )

        valid_versions = (
            ("0.28.1.post1", "1.0.9.post2"),
            ("0.28.1+vendor.1", "1.0.9+vendor.2"),
            ("v0.28.1", "v1.0.9"),
        )
        for httpx_version, httpcore_version in valid_versions:
            with (
                self.subTest(httpx=httpx_version, httpcore=httpcore_version),
                patch.object(httpx, "__version__", httpx_version),
                patch.object(httpcore, "__version__", httpcore_version),
            ):
                self.assertEqual(
                    ensure_runtime_compatibility(),
                    (httpx_version, httpcore_version),
                )

        for prerelease in ("0.28.2rc1", "0.28.2.dev1"):
            with (
                self.subTest(version=prerelease),
                patch.object(httpx, "__version__", prerelease),
                self.assertRaises(PinnedTransportCompatibilityError),
            ):
                ensure_runtime_compatibility()


class TestPublicAddressSelection(unittest.TestCase):
    """Reject every unsafe DNS answer before a socket delegate can see it."""

    def test_explicit_registry_special_range_boundaries(self):
        from daem0nmcp.pinned_http import _is_disallowed_special_address

        disallowed = (
            "192.0.0.0",
            "192.0.0.8",
            "192.0.0.11",
            "192.0.0.255",
            "192.88.99.0",
            "192.88.99.2",
            "192.88.99.255",
            "3fff::",
            "3fff:fff:ffff:ffff:ffff:ffff:ffff:ffff",
            "100:0:0:1::",
            "100:0:0:1:ffff:ffff:ffff:ffff",
            "5f00::",
            "5f00:ffff:ffff:ffff:ffff:ffff:ffff:ffff",
            "::ffff:0:0",
            "::ffff:ffff:ffff",
            "64:ff9b::",
            "64:ff9b::ffff:ffff",
            "64:ff9b:1::",
            "64:ff9b:1:ffff:ffff:ffff:ffff:ffff",
            "2001::",
            "2001:0:ffff:ffff:ffff:ffff:ffff:ffff",
            "2002::",
            "2002:ffff:ffff:ffff:ffff:ffff:ffff:ffff",
        )
        adjacent_or_exempt = (
            "191.255.255.255",
            "192.0.0.9",
            "192.0.0.10",
            "192.0.1.0",
            "192.88.98.255",
            "192.88.100.0",
            "3ffe:ffff:ffff:ffff:ffff:ffff:ffff:ffff",
            "3fff:1000::",
            "100:0:0:0:ffff:ffff:ffff:ffff",
            "100:0:0:2::",
            "5eff:ffff:ffff:ffff:ffff:ffff:ffff:ffff",
            "5f01::",
        )

        for address in disallowed:
            with self.subTest(address=address):
                self.assertTrue(
                    _is_disallowed_special_address(ipaddress.ip_address(address))
                )
        for address in adjacent_or_exempt:
            with self.subTest(address=address):
                self.assertFalse(
                    _is_disallowed_special_address(ipaddress.ip_address(address))
                )

    def test_accepts_public_controls_and_registry_anycast_exceptions(self):
        from daem0nmcp.pinned_http import select_public_address

        controls = (
            "93.184.216.34",
            "2606:4700:4700::1111",
            "192.0.0.9",
            "192.0.0.10",
        )
        for address in controls:
            with self.subTest(address=address):
                self.assertEqual(select_public_address((address,)), address)

    def test_rejects_empty_invalid_or_non_global_answer_sets(self):
        from daem0nmcp.pinned_http import PinnedAddressError, select_public_address

        unsafe_sets = (
            (),
            ("not-an-address",),
            ("127.0.0.1",),
            ("10.1.2.3",),
            ("169.254.169.254",),
            ("224.0.0.1",),
            ("192.0.2.1",),
            ("::1",),
            ("fe80::1",),
            ("fec0::1",),
            ("100:0:0:1::1",),
            ("5f00::1",),
            ("64:ff9b::a00:1",),
            ("93.184.216.34", "127.0.0.1"),
        )

        for addresses in unsafe_sets:
            with (
                self.subTest(addresses=addresses),
                self.assertRaises(PinnedAddressError),
            ):
                select_public_address(addresses)

    def test_selects_a_deterministic_canonical_literal_from_all_public_answers(self):
        from daem0nmcp.pinned_http import select_public_address

        selected = select_public_address(
            ("2606:4700:4700::1111", "93.184.216.34", "93.184.216.34")
        )

        self.assertEqual(selected, "93.184.216.34")


class TestURLAdmission(unittest.IsolatedAsyncioTestCase):
    async def test_incompatible_runtime_fails_with_remediation_before_dns(self):
        from daem0nmcp.pinned_http import validate_public_url

        resolver = SequenceResolver(("93.184.216.34",))
        with patch.object(httpx, "__version__", "0.29.0"):
            error = await validate_public_url(
                "https://docs.example.test/",
                allowed_schemes=("https",),
                resolver=resolver,
            )

        self.assertIn("Unsupported pinned HTTP runtime", error)
        self.assertIn("daem0nmcp[apps]", error)
        self.assertEqual(resolver.calls, [])

    async def test_never_allows_a_configured_non_http_scheme(self):
        from daem0nmcp.pinned_http import validate_public_url

        resolver = SequenceResolver(("93.184.216.34",))

        error = await validate_public_url(
            "ftp://docs.example.test/resource",
            allowed_schemes=("http", "https", "ftp"),
            resolver=resolver,
        )

        self.assertIsNotNone(error)
        self.assertEqual(resolver.calls, [])

    async def test_rejects_malformed_credentials_ports_and_local_authorities(self):
        from daem0nmcp.pinned_http import validate_public_url

        invalid_urls = (
            "file:///etc/passwd",
            "ftp://docs.example.test/file",
            "https:///missing-host",
            "https://user@docs.example.test/",
            "https://:secret@docs.example.test/",
            "https://docs.example.test:not-a-port/",
            "https://docs.example.test:/",
            "https://docs.example.test:+80/",
            "https://docs.example.test: 80/",
            "https://docs.example.test:8_0/",
            "https://docs.example.test:８０/",
            "https://docs.example.test:0/",
            "https://docs.example.test:65536/",
            "https://docs.example.test:" + "9" * 5000 + "/",
            "https://docs.example.test\\alternate/path",
            "https://docs.example.test\t/path",
            "https://docs.example.test\x00/path",
            "http://localhost/",
            "http://localhost./",
            "http://LOCALHOST.localdomain/",
            "http://service.localhost/",
        )

        for url in invalid_urls:
            with self.subTest(url=url):
                resolver = SequenceResolver()
                error = await validate_public_url(
                    url,
                    allowed_schemes=("http", "https"),
                    resolver=resolver,
                )
                self.assertIsNotNone(error)
                self.assertEqual(resolver.calls, [])

    async def test_admission_resolves_the_exact_idna_authority_httpx_will_dial(self):
        from daem0nmcp.pinned_http import validate_public_url

        resolver = SequenceResolver(("93.184.216.34",))

        error = await validate_public_url(
            "https://bücher.example:8443/resource",
            allowed_schemes=("http", "https"),
            resolver=resolver,
        )

        self.assertIsNone(error)
        self.assertEqual(resolver.calls, [("xn--bcher-kva.example", 8443)])

    async def test_rejects_non_global_ip_literals_without_resolving(self):
        from daem0nmcp.pinned_http import validate_public_url

        unsafe_urls = (
            "http://127.0.0.1/",
            "http://10.0.0.1/",
            "http://169.254.169.254/",
            "http://192.0.2.1/",
            "http://224.0.0.1/",
            "http://[::1]/",
            "http://[fe80::1]/",
        )

        for url in unsafe_urls:
            with self.subTest(url=url):
                resolver = SequenceResolver()
                error = await validate_public_url(
                    url,
                    allowed_schemes=("http", "https"),
                    resolver=resolver,
                )
                self.assertIsNotNone(error)
                self.assertEqual(resolver.calls, [])

    async def test_rejects_registry_special_literals_without_resolving(self):
        from daem0nmcp.pinned_http import validate_public_url

        for address in SPECIAL_ADDRESS_CONTROLS:
            url = f"http://[{address}]/" if ":" in address else f"http://{address}/"
            with self.subTest(address=address):
                resolver = SequenceResolver()
                error = await validate_public_url(
                    url,
                    allowed_schemes=("http", "https"),
                    resolver=resolver,
                )
                self.assertIsNotNone(error)
                self.assertEqual(resolver.calls, [])

    async def test_rejects_empty_or_mixed_hostname_resolution(self):
        from daem0nmcp.pinned_http import validate_public_url

        for answers in ((), ("93.184.216.34", "127.0.0.1")):
            with self.subTest(answers=answers):
                resolver = SequenceResolver(answers)
                error = await validate_public_url(
                    "https://docs.example.test/",
                    allowed_schemes=("http", "https"),
                    resolver=resolver,
                )
                self.assertIsNotNone(error)
                self.assertEqual(resolver.calls, [("docs.example.test", 443)])

    async def test_rejects_mixed_registry_special_hostname_answers(self):
        from daem0nmcp.pinned_http import validate_public_url

        for special_address in SPECIAL_ADDRESS_CONTROLS:
            answers = ("93.184.216.34", special_address)
            with self.subTest(answers=answers):
                resolver = SequenceResolver(answers)
                error = await validate_public_url(
                    "https://docs.example.test/",
                    allowed_schemes=("http", "https"),
                    resolver=resolver,
                )
                self.assertIsNotNone(error)
                self.assertEqual(resolver.calls, [("docs.example.test", 443)])

    async def test_accepts_public_hostname_and_uses_effective_port(self):
        from daem0nmcp.pinned_http import validate_public_url

        controls = (
            ("http://docs.example.test/", 80),
            ("https://docs.example.test/", 443),
            ("https://docs.example.test:8443/", 8443),
            ("https://[2606:4700:4700::1111]/", None),
            ("https://[2606:4700:4700::1111]:8443/", None),
            ("http://192.0.0.9/", None),
            ("http://192.0.0.10/", None),
        )
        for url, port in controls:
            with self.subTest(url=url):
                resolver = SequenceResolver(("93.184.216.34",))
                error = await validate_public_url(
                    url,
                    allowed_schemes=("http", "https"),
                    resolver=resolver,
                )
                self.assertIsNone(error)
                expected_calls = [] if port is None else [("docs.example.test", port)]
                self.assertEqual(resolver.calls, expected_calls)


class TestPinnedBackend(unittest.IsolatedAsyncioTestCase):
    async def test_connect_timeout_covers_a_never_resolving_dns_call(self):
        from daem0nmcp.pinned_http import PinnedPublicNetworkBackend

        class NeverResolver:
            async def __call__(self, host, port):
                await asyncio.Event().wait()

        delegate = RecordingBackend()
        backend = PinnedPublicNetworkBackend(
            resolver=NeverResolver(),
            delegate=delegate,
        )
        started = time.monotonic()

        with self.assertRaises(httpcore.ConnectTimeout):
            await asyncio.wait_for(
                backend.connect_tcp("docs.example.test", 443, timeout=0.01),
                timeout=0.10,
            )

        self.assertLess(time.monotonic() - started, 0.08)
        self.assertEqual(delegate.connect_calls, [])

    async def test_rejects_connect_time_rebind_before_delegate_dial(self):
        from daem0nmcp.pinned_http import PinnedAddressError, PinnedPublicNetworkBackend

        resolver = SequenceResolver(("93.184.216.34",), ("127.0.0.1",))
        admission_answers = await resolver("docs.example.test", 443)
        self.assertEqual(admission_answers, ("93.184.216.34",))
        stream = ScriptedNetworkStream()
        delegate = RecordingBackend(stream)
        backend = PinnedPublicNetworkBackend(resolver=resolver, delegate=delegate)

        with self.assertRaises(PinnedAddressError):
            await backend.connect_tcp("docs.example.test", 443)

        self.assertEqual(
            resolver.calls,
            [("docs.example.test", 443), ("docs.example.test", 443)],
        )
        self.assertEqual(delegate.connect_calls, [])
        self.assertEqual(stream.writes, [])

    async def test_registry_special_connect_answers_fail_before_delegate_or_bytes(self):
        from daem0nmcp.pinned_http import PinnedAddressError, PinnedPublicNetworkBackend

        for special_address in SPECIAL_ADDRESS_CONTROLS:
            with self.subTest(address=special_address):
                stream = ScriptedNetworkStream(server_addr=("93.184.216.34", 443))
                delegate = RecordingBackend(stream)
                backend = PinnedPublicNetworkBackend(
                    resolver=SequenceResolver(("93.184.216.34", special_address)),
                    delegate=delegate,
                )

                with self.assertRaises(PinnedAddressError):
                    await backend.connect_tcp("docs.example.test", 443)

                self.assertEqual(delegate.connect_calls, [])
                self.assertEqual(stream.writes, [])
                self.assertEqual(stream.tls_calls, [])

    async def test_resolver_uses_a_dedicated_bounded_pool(self):
        from daem0nmcp import pinned_http
        from daem0nmcp.bounded_workers import BoundedWorkerPool

        calls: list[tuple[object, ...]] = []
        worker_names: list[str] = []
        pool = BoundedWorkerPool(max_workers=1, thread_name_prefix="test-dns")

        def fake_getaddrinfo(host, port, *, type, proto):
            calls.append((host, port, type, proto))
            worker_names.append(threading.current_thread().name)
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
                (
                    socket.AF_INET6,
                    socket.SOCK_STREAM,
                    6,
                    "",
                    ("2606:4700:4700::1111", 443, 0, 0),
                ),
            ]

        loop = asyncio.get_running_loop()
        try:
            with (
                patch.object(
                    loop,
                    "getaddrinfo",
                    side_effect=AssertionError("default executor DNS is forbidden"),
                ),
                patch.object(pinned_http, "_DNS_WORKER_POOL", pool, create=True),
                patch.object(
                    pinned_http.socket,
                    "getaddrinfo",
                    side_effect=fake_getaddrinfo,
                ),
            ):
                addresses = await pinned_http.resolve_host_addresses(
                    "docs.example.test", 443
                )
        finally:
            pool.shutdown()

        self.assertEqual(addresses, ("93.184.216.34", "2606:4700:4700::1111"))
        self.assertEqual(
            calls,
            [
                (
                    "docs.example.test",
                    443,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                )
            ],
        )
        self.assertEqual(len(worker_names), 1)
        self.assertTrue(worker_names[0].startswith("test-dns"))

    async def test_cancelled_dns_retains_capacity_until_getaddrinfo_finishes(self):
        from daem0nmcp import pinned_http
        from daem0nmcp.bounded_workers import BoundedWorkerPool

        started = threading.Event()
        release = threading.Event()
        calls = 0
        pool = BoundedWorkerPool(max_workers=1, thread_name_prefix="test-dns")

        def blocking_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
            nonlocal calls
            calls += 1
            started.set()
            release.wait(timeout=1.0)
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))
            ]

        try:
            with (
                patch.object(pinned_http, "_DNS_WORKER_POOL", pool, create=True),
                patch.object(
                    pinned_http.socket,
                    "getaddrinfo",
                    side_effect=blocking_getaddrinfo,
                ),
            ):
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        pinned_http.resolve_host_addresses("docs.example.test", 443),
                        timeout=0.02,
                    )

                self.assertTrue(started.is_set())
                self.assertEqual(pool.in_flight, 1)
                before = time.monotonic()
                with self.assertRaises(pinned_http.PinnedAddressError) as captured:
                    await pinned_http.resolve_host_addresses(
                        "another.example.test", 443
                    )
                self.assertLess(time.monotonic() - before, 0.05)
                self.assertEqual(
                    str(captured.exception),
                    "Host resolution capacity is unavailable",
                )
                self.assertEqual(calls, 1)

                release.set()
                for _ in range(100):
                    if pool.in_flight == 0:
                        break
                    await asyncio.sleep(0.001)
                self.assertEqual(pool.in_flight, 0)
                self.assertEqual(
                    await pinned_http.resolve_host_addresses(
                        "recovered.example.test", 443
                    ),
                    ("93.184.216.34",),
                )
        finally:
            release.set()
            pool.shutdown()

    async def test_closes_stream_when_connected_peer_is_not_exact(self):
        from daem0nmcp.pinned_http import PinnedAddressError, PinnedPublicNetworkBackend

        invalid_peers = (
            ("93.184.216.35", 443),
            None,
            "93.184.216.34:443",
            ("not-an-address", 443),
            ("93.184.216.34", 80),
        )

        for peer in invalid_peers:
            with self.subTest(peer=peer):
                stream = ScriptedNetworkStream(server_addr=peer)
                delegate = RecordingBackend(stream)
                backend = PinnedPublicNetworkBackend(
                    resolver=SequenceResolver(("93.184.216.34",)),
                    delegate=delegate,
                )

                with self.assertRaises(PinnedAddressError):
                    await backend.connect_tcp("docs.example.test", 443)

                self.assertTrue(stream.closed)
                self.assertEqual(stream.writes, [])

    async def test_exact_peer_is_returned_after_literal_only_dial(self):
        from daem0nmcp.pinned_http import PinnedPublicNetworkBackend

        stream = ScriptedNetworkStream(server_addr=("93.184.216.34", 443))
        delegate = RecordingBackend(stream)
        resolver = SequenceResolver(("93.184.216.34",))
        backend = PinnedPublicNetworkBackend(resolver=resolver, delegate=delegate)
        socket_options = ((socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),)

        result = await backend.connect_tcp(
            "docs.example.test",
            443,
            timeout=2.5,
            local_address="0.0.0.0",
            socket_options=socket_options,
        )

        self.assertIs(result, stream)
        self.assertFalse(stream.closed)
        self.assertEqual(len(delegate.connect_calls), 1)
        connect_call = delegate.connect_calls[0]
        self.assertEqual(connect_call["host"], "93.184.216.34")
        self.assertEqual(connect_call["port"], 443)
        self.assertGreater(connect_call["timeout"], 0)
        self.assertLessEqual(connect_call["timeout"], 2.5)
        self.assertEqual(connect_call["local_address"], "0.0.0.0")
        self.assertEqual(connect_call["socket_options"], socket_options)

    async def test_unix_socket_path_is_rejected_without_delegation(self):
        from daem0nmcp.pinned_http import PinnedPublicNetworkBackend

        delegate = RecordingBackend()
        backend = PinnedPublicNetworkBackend(
            resolver=SequenceResolver(),
            delegate=delegate,
        )

        with self.assertRaises(httpcore.UnsupportedProtocol):
            await backend.connect_unix_socket("/tmp/forbidden.sock")

        self.assertEqual(delegate.connect_calls, [])


class TestPinnedHTTPAdapter(unittest.IsolatedAsyncioTestCase):
    async def test_real_adapter_preserves_authority_sni_tls_and_request_target(self):
        from daem0nmcp.pinned_http import PinnedAsyncHTTPTransport

        stream = ScriptedNetworkStream(
            server_addr=("93.184.216.34", 443),
            response=(
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK"
            ),
        )
        delegate = RecordingBackend(stream)
        resolver = SequenceResolver(("93.184.216.34",))
        transport = PinnedAsyncHTTPTransport(resolver=resolver, delegate=delegate)

        async with httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            headers={"Accept-Encoding": "identity"},
        ) as client:
            response = await client.get("https://docs.example.test/path?x=1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "OK")
        self.assertEqual(resolver.calls, [("docs.example.test", 443)])
        self.assertEqual(delegate.connect_calls[0]["host"], "93.184.216.34")
        self.assertEqual(delegate.connect_calls[0]["port"], 443)
        self.assertEqual(stream.server_addr, ("93.184.216.34", 443))
        self.assertEqual(len(stream.tls_calls), 1)
        tls_context, server_hostname, _ = stream.tls_calls[0]
        self.assertEqual(server_hostname, "docs.example.test")
        self.assertEqual(tls_context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(tls_context.check_hostname)
        request_bytes = b"".join(stream.writes)
        self.assertIn(b"GET /path?x=1 HTTP/1.1\r\n", request_bytes)
        self.assertIn(b"Host: docs.example.test\r\n", request_bytes)
        self.assertIn(b"Accept-Encoding: identity\r\n", request_bytes)

    async def test_conflicting_sni_extension_is_rejected_before_resolution(self):
        from daem0nmcp.pinned_http import PinnedAsyncHTTPTransport

        resolver = SequenceResolver(("93.184.216.34",))
        delegate = RecordingBackend(ScriptedNetworkStream())
        transport = PinnedAsyncHTTPTransport(resolver=resolver, delegate=delegate)
        request = httpx.Request(
            "GET",
            "https://docs.example.test/",
            extensions={"sni_hostname": "attacker.example"},
        )

        with self.assertRaises(httpcore.LocalProtocolError):
            await transport.handle_async_request(request)
        await transport.aclose()

        self.assertEqual(resolver.calls, [])
        self.assertEqual(delegate.connect_calls, [])

    async def test_redirect_response_does_not_trigger_a_second_dial(self):
        from daem0nmcp.pinned_http import PinnedAsyncHTTPTransport

        stream = ScriptedNetworkStream(
            server_addr=("93.184.216.34", 443),
            response=(
                b"HTTP/1.1 302 Found\r\n"
                b"Location: http://127.0.0.1/private\r\n"
                b"Content-Length: 0\r\n"
                b"Connection: close\r\n\r\n"
            ),
        )
        resolver = SequenceResolver(("93.184.216.34",))
        delegate = RecordingBackend(stream)

        async with httpx.AsyncClient(
            transport=PinnedAsyncHTTPTransport(resolver=resolver, delegate=delegate),
            trust_env=False,
            follow_redirects=False,
        ) as client:
            response = await client.get("https://docs.example.test/start")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(resolver.calls), 1)
        self.assertEqual(len(delegate.connect_calls), 1)

    async def test_dependency_logs_do_not_emit_the_raw_request_url(self):
        from daem0nmcp.pinned_http import (
            PinnedAsyncHTTPTransport,
            pinned_dependency_log_scope,
        )

        messages: list[str] = []

        class RecordingHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                messages.append(record.getMessage())

        dependency_loggers = [
            logging.getLogger("httpx"),
            logging.getLogger("httpcore"),
        ]
        previous_levels = [logger.level for logger in dependency_loggers]
        handler = RecordingHandler()
        for dependency_logger in dependency_loggers:
            dependency_logger.addHandler(handler)
            dependency_logger.setLevel(logging.DEBUG)
        raw_url = "https://docs.example.test/path?token=do-not-log#fragment"
        observed_levels: list[int] = []
        try:
            stream = ScriptedNetworkStream(
                server_addr=("93.184.216.34", 443),
                response=(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                ),
            )
            with pinned_dependency_log_scope():
                async with httpx.AsyncClient(
                    transport=PinnedAsyncHTTPTransport(
                        resolver=SequenceResolver(("93.184.216.34",)),
                        delegate=RecordingBackend(stream),
                    ),
                    trust_env=False,
                    follow_redirects=False,
                ) as client:
                    await client.get(raw_url)

            for dependency_logger in dependency_loggers:
                dependency_logger.info("unrelated-client-log")
            observed_levels = [logger.level for logger in dependency_loggers]
        finally:
            for dependency_logger, previous_level in zip(
                dependency_loggers, previous_levels, strict=True
            ):
                dependency_logger.removeHandler(handler)
                dependency_logger.setLevel(previous_level)

        rendered = "\n".join(messages)
        self.assertNotIn(raw_url, rendered)
        self.assertNotIn("do-not-log", rendered)
        self.assertNotIn("93.184.216.34", rendered)
        self.assertEqual(rendered.count("unrelated-client-log"), 2)
        self.assertEqual(observed_levels, [logging.DEBUG, logging.DEBUG])

    async def test_dependency_log_scope_does_not_suppress_a_concurrent_client(self):
        from daem0nmcp.pinned_http import pinned_dependency_log_scope

        dependency_logger = logging.getLogger("httpx")
        previous_level = dependency_logger.level
        messages: list[str] = []

        class RecordingHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                messages.append(record.getMessage())

        handler = RecordingHandler()
        initial_logger_filters = list(dependency_logger.filters)
        initial_handler_filters = list(handler.filters)
        dependency_logger.addHandler(handler)
        dependency_logger.setLevel(logging.INFO)
        scope_entered = asyncio.Event()
        unrelated_logged = asyncio.Event()

        async def pinned_request() -> None:
            with pinned_dependency_log_scope():
                dependency_logger.info("pinned-secret-url")
                scope_entered.set()
                await unrelated_logged.wait()

        async def unrelated_request() -> None:
            await scope_entered.wait()
            dependency_logger.info("unrelated-concurrent-request")
            unrelated_logged.set()

        try:
            await asyncio.gather(pinned_request(), unrelated_request())
        finally:
            dependency_logger.removeHandler(handler)
            dependency_logger.setLevel(previous_level)

        self.assertNotIn("pinned-secret-url", messages)
        self.assertEqual(messages, ["unrelated-concurrent-request"])
        self.assertEqual(dependency_logger.filters, initial_logger_filters)
        self.assertEqual(handler.filters, initial_handler_filters)


class TestPinnedResponseBounds(unittest.IsolatedAsyncioTestCase):
    async def test_compressed_content_is_rejected_without_reading_body(self):
        from daem0nmcp.pinned_http import (
            PinnedResponseError,
            read_bounded_identity_body,
        )

        class Response:
            def __init__(self, encoding):
                self.headers = {"content-encoding": encoding, "content-length": "1973"}
                self.iterated = False

            async def aiter_raw(self, chunk_size=None):
                self.iterated = True
                yield b"compressed-bomb"

        for encoding in ("gzip", "deflate", "br"):
            with self.subTest(encoding=encoding):
                response = Response(encoding)
                with self.assertRaises(PinnedResponseError):
                    await read_bounded_identity_body(response, max_bytes=1_000_000)
                self.assertFalse(response.iterated)

    async def test_raw_identity_body_is_bounded_before_accumulation(self):
        from daem0nmcp.pinned_http import (
            PinnedResponseError,
            read_bounded_identity_body,
        )

        class Response:
            headers = {"content-encoding": "identity"}

            async def aiter_raw(self, chunk_size=None):
                yield b"a" * 8
                yield b"b" * 8

        with self.assertRaises(PinnedResponseError):
            await read_bounded_identity_body(Response(), max_bytes=10)


class TestAgencyTotalDeadline(unittest.IsolatedAsyncioTestCase):
    async def test_admission_and_fetch_share_one_total_deadline(self):
        with _loaded_agency_tools_module() as module:
            fetch_called = False

            async def never_admit(url):
                await asyncio.Event().wait()

            async def fetch(url):
                nonlocal fetch_called
                fetch_called = True
                return "unexpected"

            module._validate_url = never_admit
            module._fetch_and_extract = fetch
            started = time.monotonic()
            error, content = await module._validate_and_fetch_with_deadline(
                "https://docs.example.test/",
                timeout_seconds=0.01,
            )

        self.assertIsNone(error)
        self.assertIsNone(content)
        self.assertFalse(fetch_called)
        self.assertLess(time.monotonic() - started, 0.10)

    async def test_slow_trickle_fetch_cannot_extend_the_total_deadline(self):
        with _loaded_agency_tools_module() as module:
            fetch_cancelled = False

            async def admit(url):
                return None

            async def slow_trickle(url):
                nonlocal fetch_cancelled
                try:
                    for _ in range(20):
                        await asyncio.sleep(0.004)
                    return "unexpected"
                except asyncio.CancelledError:
                    fetch_cancelled = True
                    raise

            module._validate_url = admit
            module._fetch_and_extract = slow_trickle
            started = time.monotonic()
            error, content = await module._validate_and_fetch_with_deadline(
                "https://docs.example.test/",
                timeout_seconds=0.02,
            )

        self.assertIsNone(error)
        self.assertIsNone(content)
        self.assertTrue(fetch_cancelled)
        self.assertLess(time.monotonic() - started, 0.10)

    async def test_html_extraction_cannot_block_or_overrun_total_deadline(self):
        from daem0nmcp import pinned_http
        from daem0nmcp.bounded_workers import BoundedWorkerPool

        body = b"<b>x</b>" * 125_000
        self.assertEqual(len(body), 1_000_000)
        parser_started = threading.Event()
        parser_release = threading.Event()
        parser_threads: list[str] = []
        parser_calls = 0
        pool = BoundedWorkerPool(max_workers=1, thread_name_prefix="test-html")

        class BlockingSoup:
            def __init__(self, text, parser):
                nonlocal parser_calls
                parser_calls += 1
                parser_threads.append(threading.current_thread().name)
                parser_started.set()
                parser_release.wait(timeout=1.0)

            def __call__(self, names):
                return []

            def get_text(self, *, separator, strip):
                return "extracted"

        fake_bs4 = types.ModuleType("bs4")
        fake_bs4.BeautifulSoup = BlockingSoup

        class FakeResponse:
            headers = {
                "content-encoding": "identity",
                "content-length": str(len(body)),
            }
            encoding = "utf-8"

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            def raise_for_status(self):
                return None

            async def aiter_raw(self, chunk_size=None):
                yield body

        class FakeAsyncClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            def stream(self, method, url):
                return FakeResponse()

        timer = threading.Timer(0.15, parser_release.set)
        timer.start()
        try:
            with (
                _loaded_agency_tools_module() as module,
                patch.dict(sys.modules, {"bs4": fake_bs4}),
                patch.object(httpx, "AsyncClient", FakeAsyncClient),
                patch.object(
                    pinned_http,
                    "PinnedAsyncHTTPTransport",
                    side_effect=lambda **kwargs: object(),
                ),
            ):

                async def admit(url):
                    return None

                module._validate_url = admit
                module._HTML_WORKER_POOL = pool
                started = time.monotonic()
                error, content = await module._validate_and_fetch_with_deadline(
                    "https://docs.example.test/",
                    timeout_seconds=0.02,
                )
                elapsed = time.monotonic() - started

                self.assertEqual((error, content), (None, None))
                self.assertLess(elapsed, 0.10)
                self.assertTrue(parser_started.is_set())
                self.assertEqual(pool.in_flight, 1)

                overflow_url = "https://another.example.test/?token=do-not-log"
                overflow_started = time.monotonic()
                with self.assertLogs(module.logger, level="ERROR") as captured_logs:
                    self.assertIsNone(await module._fetch_and_extract(overflow_url))
                self.assertLess(time.monotonic() - overflow_started, 0.05)
                self.assertEqual(parser_calls, 1)
                self.assertNotIn(overflow_url, "\n".join(captured_logs.output))
                self.assertIn("BoundedWorkerBusyError", captured_logs.output[0])

                parser_release.set()
                for _ in range(100):
                    if pool.in_flight == 0:
                        break
                    await asyncio.sleep(0.001)
                self.assertEqual(pool.in_flight, 0)
                self.assertEqual(len(parser_threads), 1)
                self.assertTrue(parser_threads[0].startswith("test-html"))
        finally:
            parser_release.set()
            timer.cancel()
            timer.join(timeout=0.25)
            pool.shutdown()


class TestPinnedHTTPAdapterAdditional(unittest.IsolatedAsyncioTestCase):
    async def test_connect_failure_is_not_retried_or_reresolved(self):
        from daem0nmcp.pinned_http import PinnedAsyncHTTPTransport

        class FailingBackend(RecordingBackend):
            async def connect_tcp(self, host, port, **kwargs):
                self.connect_calls.append({"host": host, "port": port, **kwargs})
                raise httpcore.ConnectError("scripted dial failure")

        resolver = SequenceResolver(("93.184.216.34",))
        delegate = FailingBackend()

        async with httpx.AsyncClient(
            transport=PinnedAsyncHTTPTransport(resolver=resolver, delegate=delegate),
            trust_env=False,
            follow_redirects=False,
        ) as client:
            with self.assertRaises(httpcore.ConnectError):
                await client.get("http://docs.example.test/resource")

        self.assertEqual(resolver.calls, [("docs.example.test", 80)])
        self.assertEqual(len(delegate.connect_calls), 1)
        self.assertEqual(delegate.connect_calls[0]["host"], "93.184.216.34")

    async def test_completed_connection_is_not_retained_for_a_second_request(self):
        from daem0nmcp.pinned_http import PinnedAsyncHTTPTransport

        response_bytes = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"
        first_stream = ScriptedNetworkStream(
            server_addr=("93.184.216.34", 80),
            response=response_bytes,
        )
        second_stream = ScriptedNetworkStream(
            server_addr=("93.184.216.34", 80),
            response=response_bytes,
        )
        resolver = SequenceResolver(
            ("93.184.216.34",),
            ("93.184.216.34",),
        )
        delegate = RecordingBackend(first_stream, second_stream)

        async with httpx.AsyncClient(
            transport=PinnedAsyncHTTPTransport(resolver=resolver, delegate=delegate),
            trust_env=False,
            follow_redirects=False,
        ) as client:
            first = await client.get("http://docs.example.test/one")
            second = await client.get("http://docs.example.test/two")

        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(len(resolver.calls), 2)
        self.assertEqual(len(delegate.connect_calls), 2)


if __name__ == "__main__":
    unittest.main()
