"""Readiness must use the same owned native service without model requests."""

from __future__ import annotations

import importlib
import io
import json
from pathlib import Path

import pytest


@pytest.fixture
def readiness(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("native_opencode_v2_readiness")


def status(state="connected", sentinel="disabled"):
    return {
        "data": [
            {"name": "acceptance_sentinel", "status": {"status": sentinel}},
            {"name": "daem0nmcp", "status": {"status": state}},
        ]
    }


def test_exact_native_status_schema_and_disabled_server_are_required(readiness):
    assert readiness.is_mcp_ready(status()) is True
    assert readiness.is_mcp_ready(status("pending")) is False
    assert readiness.is_mcp_ready(status(sentinel="connected")) is False


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"data": {}},
        {"data": []},
        {"data": [{"name": "daem0nmcp", "status": "connected"}]},
        {
            "data": [
                {"name": "daem0nmcp", "status": {"status": "connected"}},
                {"name": "daem0nmcp", "status": {"status": "pending"}},
            ]
        },
    ],
)
def test_malformed_or_incomplete_status_cannot_pass(readiness, payload):
    assert readiness.is_mcp_ready(payload) is False


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:1234",
        "http://example.org:1234",
        "http://0.0.0.0:1234",
        "http://127.0.0.1",
        "http://user:password@127.0.0.1:1234",
        "http://127.0.0.1:1234/extra",
        "http://127.0.0.1:1234?query=1",
    ],
)
def test_only_credential_free_owned_loopback_url_is_accepted(readiness, url):
    with pytest.raises(ValueError):
        readiness.validate_service_url(url)


def test_status_poll_queries_exact_project_until_observed_ready(readiness, tmp_path):
    clock = [0.0]
    observations = iter([status("pending"), status()])
    requested = []

    def request(url, timeout):
        requested.append((url, timeout))
        payload = next(observations)
        payload["location"] = {"directory": str(tmp_path.resolve())}
        return payload

    def sleep(delay):
        clock[0] += delay

    result = readiness.wait_for_mcp(
        "http://127.0.0.1:1234",
        tmp_path,
        deadline=3,
        request=request,
        monotonic=lambda: clock[0],
        sleep=sleep,
    )
    assert result["ready"] is True
    assert len(result["observations"]) == 2
    assert all("/api/mcp?directory=" in url for url, _ in requested)
    assert all("session" not in url and "resource" not in url for url, _ in requested)


def test_readiness_deadline_retains_pending_observations(readiness, tmp_path):
    clock = [0.0]

    def sleep(delay):
        clock[0] += delay

    result = readiness.wait_for_mcp(
        "http://127.0.0.1:1234",
        tmp_path,
        deadline=0.2,
        request=lambda *_: status("pending"),
        monotonic=lambda: clock[0],
        sleep=sleep,
    )
    assert result["ready"] is False
    assert result["observations"]
    assert clock[0] <= 0.2


def test_service_uses_kernel_port_and_closes_owned_group_on_failed_readiness(
    readiness, monkeypatch, tmp_path
):
    class Process:
        pid = 123
        stdin = io.StringIO()
        stdout = io.StringIO(json.dumps({"url": "http://127.0.0.1:1234"}) + "\n")
        stderr = io.StringIO("")

        def poll(self):
            return None

    spawned = []
    closed = []
    process = Process()
    monkeypatch.setattr(
        readiness,
        "_spawn",
        lambda command, **kwargs: spawned.append((command, kwargs)) or process,
    )
    monkeypatch.setattr(
        readiness,
        "_close_owned_group",
        lambda actual: closed.append(actual.pid) or {"process_group_closed": True},
    )
    monkeypatch.setattr(
        readiness,
        "wait_for_mcp",
        lambda *_args, **_kwargs: {"ready": False, "observations": []},
    )
    service = readiness.NativeService(
        Path("client"),
        tmp_path,
        tmp_path / "service",
        {"HOME": str(tmp_path)},
        timeout=1,
    )
    with pytest.raises(RuntimeError, match="not ready"):
        service.start()
    assert spawned[0][0] == [
        "client",
        "serve",
        "--hostname",
        "127.0.0.1",
        "--port",
        "0",
        "--stdio",
    ]
    assert spawned[0][1]["cwd"] == tmp_path
    assert closed == [123]
    service.close()
    assert closed == [123]
    assert (
        json.loads((tmp_path / "service/readiness.json").read_text())["ready"] is False
    )


def test_native_server_selection_changes_only_transport_arguments(
    readiness, monkeypatch, tmp_path
):
    import native_opencode_v2_acceptance as driver
    from native_acceptance_fixtures import PROJECTS

    lane = tmp_path / "lane"
    (lane / "project").mkdir(parents=True)
    (lane / "scenario.json").write_text(
        json.dumps(
            {
                "mode": "direct",
                "workspace_id": "ws_fixture",
                "source": {"project": "legion", "scenario": PROJECTS["legion"]},
            }
        )
    )
    monkeypatch.setattr(driver, "selected_key", lambda: "test-private-key")
    monkeypatch.setenv("OPENCODE_PASSWORD", "test-service-password")
    observation = lane / "ready-service-service/host-events.jsonl"
    observation.parent.mkdir()
    observation.write_text('{"event":"setup","version":"2.2.7"}\n')
    calls = []
    environments = []

    class InterceptedError(Exception):
        pass

    def spawn(command, **kwargs):
        calls.append(command)
        environments.append(kwargs["env"])
        raise InterceptedError

    monkeypatch.setattr(driver, "_spawn", spawn)
    for attempt, extra in [
        ("standalone", []),
        (
            "ready-service",
            [
                "--server",
                "http://127.0.0.1:1234",
                "--observation-file",
                str(observation),
            ],
        ),
    ]:
        monkeypatch.setattr(
            driver.sys,
            "argv",
            [
                "native_opencode_v2_acceptance.py",
                "run",
                "--output",
                str(lane),
                "--client",
                str(tmp_path / "client"),
                "--attempt",
                attempt,
                "--scenario",
                "restart",
                "--ordinary-workflow",
                *extra,
            ],
        )
        with pytest.raises(InterceptedError):
            driver.main()
    assert calls[0][2:3] == ["--standalone"]
    assert calls[1][2:4] == ["--server", "http://127.0.0.1:1234"]
    assert calls[0][-1] == calls[1][-1]
    assert calls[0][3:-3] == calls[1][4:-3]
    assert "OPENCODE_PASSWORD" not in environments[0]
    assert environments[1]["OPENCODE_PASSWORD"] == "test-service-password"
    assert environments[1]["DAEM0N_V2_OBSERVATION"] == str(observation)
    assert environments[0]["DAEM0N_V2_OBSERVATION"] == str(
        lane / "standalone/host-events.jsonl"
    )
    assert observation.read_text() == '{"event":"setup","version":"2.2.7"}\n'


def test_dead_service_stops_before_another_status_request(readiness, tmp_path):
    def unexpected_request(*_):
        pytest.fail("Exited service must not be polled again")

    result = readiness.wait_for_mcp(
        "http://127.0.0.1:1234",
        tmp_path,
        deadline=100,
        monotonic=lambda: 0,
        alive=lambda: False,
        request=unexpected_request,
    )
    assert result["ready"] is False
    assert result["observations"] == [{"error": "OwnedNativeServiceExited"}]


def test_cleanup_os_error_retains_unknown_closure_and_closes_stdin(
    readiness, monkeypatch, tmp_path
):
    class Process:
        stdin = io.StringIO()

    process = Process()

    def failed_close(_):
        raise OSError("owned cleanup failure")

    monkeypatch.setattr(readiness, "_close_owned_group", failed_close)
    service = readiness.NativeService(
        Path("client"), tmp_path, tmp_path / "evidence", {}
    )
    service.evidence.mkdir()
    service.process = process
    result = service.close()
    assert result["process_group_closed"] is None
    assert process.stdin.closed
    assert (
        json.loads((service.evidence / "readiness.json").read_text())["cleanup"][
            "cleanup_error"
        ]
        == "OSError"
    )


def test_status_fetch_refuses_redirects(readiness):
    with pytest.raises(ValueError, match="redirected"):
        readiness._NoRedirect().redirect_request(
            None, None, 302, "redirect", {}, "https://example.org"
        )


def test_status_fetch_disables_proxy_and_bounds_response(readiness, monkeypatch):
    handlers = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, limit):
            assert limit == readiness.MAX_STATUS_BYTES + 1
            return b"x" * limit

    class Opener:
        def open(self, url, timeout):
            assert url == "http://127.0.0.1:1234/api/mcp?directory=fixture"
            assert timeout == 1
            return Response()

    def build(*args):
        handlers.extend(args)
        return Opener()

    monkeypatch.setattr(readiness.urllib.request, "build_opener", build)
    with pytest.raises(ValueError, match="bound"):
        readiness._request_json("http://127.0.0.1:1234/api/mcp?directory=fixture", 1)
    assert handlers[0].proxies == {}
    assert isinstance(handlers[1], readiness._NoRedirect)


def test_readiness_evidence_redacts_selected_credential(readiness, tmp_path):
    service = readiness.NativeService(
        Path("client"),
        tmp_path,
        tmp_path / "evidence",
        {},
        sensitive_values=("private-selected-key",),
    )
    service.evidence.mkdir()
    service.readiness = {
        "ready": False,
        "error": "native error includes private-selected-key",
    }
    service._write()
    assert (
        "private-selected-key" not in (service.evidence / "readiness.json").read_text()
    )


def test_start_preserves_original_readiness_exception_after_owned_cleanup(
    readiness, monkeypatch, tmp_path
):
    class Process:
        pid = 123
        stdin = io.StringIO()
        stdout = io.StringIO('{"url":"http://127.0.0.1:1234"}\n')
        stderr = io.StringIO("")

        def poll(self):
            return None

    original = ValueError("exact native status error")
    closed = []

    def broken_status(*_args, **_kwargs):
        raise original

    monkeypatch.setattr(readiness, "_spawn", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(readiness, "wait_for_mcp", broken_status)
    monkeypatch.setattr(
        readiness,
        "_close_owned_group",
        lambda actual: closed.append(actual.pid) or {"process_group_closed": True},
    )
    service = readiness.NativeService(
        Path("client"), tmp_path, tmp_path / "service", {}
    )
    with pytest.raises(ValueError) as observed:
        service.start()
    assert observed.value is original
    assert closed == [123]
    assert json.loads((service.evidence / "readiness.json").read_text())[
        "startup_error"
    ] == str(original)


@pytest.mark.parametrize(
    "timeout", [float("inf"), float("nan"), 0, -1, 121, True, "45"]
)
def test_invalid_readiness_timeout_rejected_before_spawn(readiness, tmp_path, timeout):
    with pytest.raises(ValueError, match="finite"):
        readiness.NativeService(
            Path("client"), tmp_path, tmp_path / "service", {}, timeout=timeout
        )
    assert not (tmp_path / "service").exists()


def test_owned_service_auth_is_ephemeral_and_matches_basic_header(
    readiness, monkeypatch, tmp_path
):
    import base64

    received = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, _limit):
            return json.dumps(status()).encode()

    class Opener:
        def open(self, request, timeout):
            received.append(request)
            return Response()

    monkeypatch.setattr(readiness.urllib.request, "build_opener", lambda *_: Opener())
    service = readiness.NativeService(
        Path("client"), tmp_path, tmp_path / "service", {}
    )
    environment = service.client_environment()
    password = environment["OPENCODE_PASSWORD"]
    assert isinstance(password, str) and len(password) >= 32
    readiness._request_json("http://127.0.0.1:1234/api/mcp", 1, password=password)
    encoded = base64.b64encode(("opencode:" + password).encode()).decode()
    assert received[0].get_header("Authorization") == "Basic " + encoded
    assert received[0].full_url == "http://127.0.0.1:1234/api/mcp"
    service.evidence.mkdir()
    service.readiness = {"ready": False, "error": password + " Basic " + encoded}
    service._write()
    retained = (service.evidence / "readiness.json").read_text()
    assert password not in retained and encoded not in retained
    assert not list(tmp_path.rglob("*credential*"))


def test_native_server_without_owned_auth_rejected_before_credentials(
    readiness, monkeypatch, tmp_path
):
    import native_opencode_v2_acceptance as driver

    monkeypatch.delenv("OPENCODE_PASSWORD", raising=False)

    def unexpected():
        pytest.fail("Missing startup auth must fail before account access")

    monkeypatch.setattr(driver, "selected_key", unexpected)
    with pytest.raises(ValueError, match="OPENCODE_PASSWORD"):
        driver.execute(
            __import__("argparse").Namespace(
                ordinary_workflow=True,
                index_argument_policy="off",
                indexing_only=False,
                restart_code_only=False,
                server="http://127.0.0.1:1234",
            )
        )


def test_external_observation_requires_server_mode_before_account_access(
    readiness, monkeypatch, tmp_path
):
    import argparse

    import native_opencode_v2_acceptance as driver

    monkeypatch.setattr(
        driver, "selected_key", lambda: pytest.fail("No account access allowed")
    )
    with pytest.raises(ValueError, match="observation"):
        driver.execute(
            argparse.Namespace(
                ordinary_workflow=True,
                index_argument_policy="off",
                indexing_only=False,
                restart_code_only=False,
                observation_file=tmp_path / "host-events.jsonl",
            )
        )


def test_external_observation_cannot_read_unowned_file(
    readiness, monkeypatch, tmp_path
):
    import argparse

    import native_opencode_v2_acceptance as driver

    monkeypatch.setenv("OPENCODE_PASSWORD", "test-service-password")
    monkeypatch.setattr(
        driver, "selected_key", lambda: pytest.fail("No account access allowed")
    )
    lane = tmp_path / "lane"
    lane.mkdir()
    outside = tmp_path / "host-events.jsonl"
    outside.write_text('{"event":"setup"}\n')
    with pytest.raises(ValueError, match="observation"):
        driver.execute(
            argparse.Namespace(
                ordinary_workflow=True,
                index_argument_policy="off",
                indexing_only=False,
                restart_code_only=False,
                server="http://127.0.0.1:1234",
                observation_file=outside,
                output=lane,
            )
        )
    assert outside.read_text() == '{"event":"setup"}\n'


@pytest.mark.parametrize("location", [None, {"directory": "/another-project"}])
def test_ready_status_for_wrong_or_missing_project_is_not_qualified(
    readiness, tmp_path, location
):
    clock = [0.0]
    payload = status()
    if location is not None:
        payload["location"] = location

    def sleep(delay):
        clock[0] += delay

    result = readiness.wait_for_mcp(
        "http://127.0.0.1:1234",
        tmp_path,
        deadline=0.1,
        request=lambda *_: payload,
        monotonic=lambda: clock[0],
        sleep=sleep,
    )
    assert result["ready"] is False


def test_external_observation_is_copied_without_modifying_service_log(
    readiness, monkeypatch, tmp_path
):
    import native_opencode_v2_acceptance as driver
    from native_acceptance_fixtures import PROJECTS

    lane = tmp_path / "lane"
    (lane / "project").mkdir(parents=True)
    (lane / "scenario.json").write_text(
        json.dumps(
            {
                "mode": "direct",
                "workspace_id": "ws_fixture",
                "source": {"project": "legion", "scenario": PROJECTS["legion"]},
            }
        )
    )
    source = lane / "actual-service/host-events.jsonl"
    source.parent.mkdir()
    raw = '{"event":"setup","note":"test-private-key"}\n'
    source.write_text(raw)
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Existing unchanged prompt fixture")
    monkeypatch.setenv("OPENCODE_PASSWORD", "test-service-password")
    monkeypatch.setattr(driver, "selected_key", lambda: "test-private-key")

    class Process:
        pid = 987654321

        def communicate(self, timeout):
            return ("", "")

        def poll(self):
            return 0

        returncode = 0

    monkeypatch.setattr(driver, "_spawn", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(
        driver, "_close_owned_group", lambda _: {"process_group_closed": True}
    )
    monkeypatch.setattr(
        driver.sys,
        "argv",
        [
            "native_opencode_v2_acceptance.py",
            "run",
            "--output",
            str(lane),
            "--client",
            str(tmp_path / "client"),
            "--attempt",
            "actual",
            "--prompt",
            str(prompt),
            "--server",
            "http://127.0.0.1:1234",
            "--observation-file",
            str(source),
        ],
    )
    driver.main()
    assert source.read_text() == raw
    captured = (lane / "actual/host-events.jsonl").read_text()
    assert "test-private-key" not in captured
    assert json.loads(captured)["event"] == "setup"
    request = json.loads((lane / "actual/request.json").read_text())
    assert request["observation_source"] == str(source)
    assert "test-service-password" not in json.dumps(request)
