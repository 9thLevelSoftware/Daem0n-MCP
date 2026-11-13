"""Own a loopback native V2 service and await its project MCP connection.

This uses the pinned public status API without prompts, tools/call, resource
reads, server-connect overrides or fixed startup sleeps. The eventual client
must use this same service URL; a different standalone service is unqualified.
"""

from __future__ import annotations

import base64
import json
import math
import os
import queue
import re
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import suppress
from pathlib import Path

if __package__:
    from .native_acceptance_drivers import _close_owned_group, _spawn, redact
else:
    from native_acceptance_drivers import _close_owned_group, _spawn, redact

MAX_STATUS_BYTES = 1024 * 1024
MAX_LOG_BYTES = 1024 * 1024


def validate_service_url(url):
    value = urllib.parse.urlsplit(url)
    if (
        value.scheme != "http"
        or value.hostname != "127.0.0.1"
        or value.port is None
        or value.port <= 0
        or value.username is not None
        or value.password is not None
        or value.path not in ("", "/")
        or value.query
        or value.fragment
    ):
        raise ValueError("Native service must announce a credential-free loopback URL")
    return url.rstrip("/")


def is_mcp_ready(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return False
    servers = payload["data"]
    if not all(isinstance(server, dict) for server in servers):
        return False
    daemon = [server for server in servers if server.get("name") == "daem0nmcp"]
    sentinel = [
        server for server in servers if server.get("name") == "acceptance_sentinel"
    ]
    return (
        len(daemon) == len(sentinel) == 1
        and daemon[0].get("status") == {"status": "connected"}
        and sentinel[0].get("status") == {"status": "disabled"}
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Native readiness endpoint unexpectedly redirected")


def _request_json(url, timeout, *, password=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    request = url
    if password:
        encoded = base64.b64encode(("opencode:" + password).encode()).decode()
        request = urllib.request.Request(
            url, headers={"Authorization": "Basic " + encoded}
        )
    with opener.open(request, timeout=timeout) as response:
        data = response.read(MAX_STATUS_BYTES + 1)
    if len(data) > MAX_STATUS_BYTES:
        raise ValueError("Native status response exceeded the evidence bound")
    return json.loads(data)


def wait_for_mcp(
    url,
    project,
    *,
    deadline,
    request=_request_json,
    monotonic=time.monotonic,
    sleep=time.sleep,
    alive=lambda: True,
):
    """Poll actual state under one deadline; never infer readiness from time."""
    url = validate_service_url(url)
    query = urllib.parse.urlencode({"directory": str(Path(project).resolve())})
    endpoint = url + "/api/mcp?" + query
    observations = []
    while monotonic() < deadline:
        if not alive():
            observations.append({"error": "OwnedNativeServiceExited"})
            return {"ready": False, "endpoint": endpoint, "observations": observations}
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        try:
            payload = request(endpoint, min(2.0, remaining))
            observations.append({"status": payload})
            if is_mcp_ready(payload) and payload.get("location", {}).get(
                "directory"
            ) == str(Path(project).resolve()):
                return {
                    "ready": True,
                    "endpoint": endpoint,
                    "observations": observations,
                }
        except (OSError, ValueError, TimeoutError) as error:
            observations.append(
                {"error": type(error).__name__, "message": str(error)[:500]}
            )
        remaining = deadline - monotonic()
        if remaining > 0:
            sleep(min(0.1, remaining))
    return {"ready": False, "endpoint": endpoint, "observations": observations}


class NativeService:
    """A single owned native service, retained until its one client finishes."""

    def __init__(
        self, client, project, evidence, environment, *, timeout=45, sensitive_values=()
    ):
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not 0 < timeout <= 120
        ):
            raise ValueError(
                "Native readiness timeout must be finite and within (0, 120] seconds"
            )
        self.client = Path(client)
        self.project = Path(project)
        self.evidence = Path(evidence)
        self.environment = dict(environment)
        self.timeout = timeout
        self._password = secrets.token_urlsafe(32)
        encoded = base64.b64encode(("opencode:" + self._password).encode()).decode()
        self.sensitive_values = (*sensitive_values, self._password, encoded)
        self.process = None
        self.url = None
        self.readiness = {"ready": False}
        self.cleanup = None
        self._closed = False
        self._readers = []
        self._stdout = queue.Queue(maxsize=128)

    def client_environment(self):
        """Use native V2's supported ephemeral authentication, without CLI secrets."""
        return {**self.environment, "OPENCODE_PASSWORD": self._password}

    def _write(self):
        result = {
            **self.readiness,
            "server_url": self.url,
            "cleanup": self.cleanup,
            "same_service_required": True,
            "native_model_calls_during_readiness": 0,
        }
        (self.evidence / "readiness.json").write_text(
            redact(json.dumps(result, indent=2), self.sensitive_values) + "\n"
        )

    def _reader(self, stream, name):
        total = 0
        with (self.evidence / (name + ".log")).open("x", encoding="utf-8") as log:
            while True:
                line = stream.readline(16_384)
                if not line:
                    break
                safe = redact(line, self.sensitive_values)
                safe = re.sub(r"(?im)(server password )\S+", r"\1<REDACTED>", safe)
                if total < MAX_LOG_BYTES:
                    log.write(safe[: MAX_LOG_BYTES - total])
                    log.flush()
                    total += len(safe)
                if name == "stdout":
                    with suppress(queue.Full):
                        self._stdout.put_nowait(safe)

    def start(self):
        self.evidence.mkdir(parents=True, exist_ok=False)
        deadline = time.monotonic() + self.timeout
        command = [
            str(self.client),
            "serve",
            "--hostname",
            "127.0.0.1",
            "--port",
            "0",
            "--stdio",
        ]
        (self.evidence / "command.json").write_text(
            json.dumps({"command": command, "cwd": str(self.project)}) + "\n"
        )
        try:
            self.process = _spawn(
                command,
                cwd=self.project,
                env=self.client_environment(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            owned = {"pid": self.process.pid, "owned_native_service": True}
            if os.name != "nt":
                with suppress(ProcessLookupError):
                    owned.update(
                        pgid=os.getpgid(self.process.pid),
                        session_id=os.getsid(self.process.pid),
                        uid=os.getuid(),
                    )
            (self.evidence / "owned-process.json").write_text(
                json.dumps(owned, indent=2) + "\n"
            )
            for stream, name in (
                (self.process.stdout, "stdout"),
                (self.process.stderr, "stderr"),
            ):
                thread = threading.Thread(
                    target=self._reader, args=(stream, name), daemon=True
                )
                thread.start()
                self._readers.append(thread)
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError("Owned native service exited before readiness")
                try:
                    line = self._stdout.get(
                        timeout=min(0.1, max(0, deadline - time.monotonic()))
                    )
                except queue.Empty:
                    continue
                try:
                    announcement = json.loads(line)
                except ValueError:
                    continue
                if isinstance(announcement, dict) and isinstance(
                    announcement.get("url"), str
                ):
                    self.url = validate_service_url(announcement["url"])
                    break
            if self.url is None:
                raise RuntimeError("Native service did not announce its loopback URL")
            self.readiness = wait_for_mcp(
                self.url,
                self.project,
                deadline=deadline,
                alive=lambda: self.process.poll() is None,
                request=lambda url, timeout: _request_json(
                    url, timeout, password=self._password
                ),
            )
            self._write()
            if not self.readiness["ready"]:
                raise RuntimeError(
                    "Native project MCP was not ready before the deadline"
                )
            return self.url
        except BaseException as error:
            self.readiness["startup_error"] = str(error)
            self.close()
            raise

    def close(self):
        if self._closed:
            return self.cleanup
        self._closed = True
        if self.process is not None:
            try:
                self.cleanup = _close_owned_group(self.process)
            except OSError as error:
                self.cleanup = {
                    "process_group_closed": None,
                    "cleanup_error": type(error).__name__,
                }
            finally:
                with suppress(OSError, ValueError):
                    self.process.stdin.close()
                for thread in self._readers:
                    thread.join(timeout=1)
        self._write()
        return self.cleanup

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.close()
