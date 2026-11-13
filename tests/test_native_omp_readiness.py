"""OMP must expose its actual MCP catalog before a paid prompt is submitted."""

import io
import json
import queue
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import native_acceptance_drivers as driver

CORE = (
    "session_brief",
    "memory_preflight",
    "memory_recall",
    "memory_store",
    "memory_record_outcome",
    "system_health",
    "edit_preflight",
    "daem0n_tools_search",
    "daem0n_tool_call",
)


def _catalog(names=CORE):
    return (
        "## MCP Tool Routes\n\nExecute each mounted tool: write JSON arguments to its path.\n"
        + "\n".join(
            f'- "{name}" → `xd://mcp__daem0nmcp_{name}` — Registered MCP tool.'
            for name in names
        )
    )


def _state(*, ready=False, project_text=""):
    return {
        "systemPrompt": [
            "Native system policy",
            "<project-context>\n<repo-rules>"
            + project_text
            + "</repo-rules>\n</project-context>\n"
            + (_catalog() if ready else ""),
        ],
        "dumpTools": [
            {"name": "write", "parameters": {"type": "object", "properties": {}}}
        ],
    }


class _Process:
    def __init__(self, callback):
        self.incoming = queue.Queue()
        self.sent = []
        self.callback = callback
        self.stdin = self

    def write(self, text):
        request = json.loads(text)
        self.sent.append(request)
        self.callback(self, request)

    def flush(self):
        pass

    def poll(self):
        return None

    def reply(self, request, **changes):
        self.incoming.put(
            {
                "id": request["id"],
                "type": "response",
                "command": "get_state",
                "success": True,
                "data": _state(ready=True),
                **changes,
            }
        )


def test_inventory_excludes_project_text_and_partial_or_other_server_routes():
    assert driver._omp_core_inventory(_state(project_text=_catalog())) is None
    partial = _state()
    partial["systemPrompt"][-1] += _catalog(CORE[:-1])
    assert driver._omp_core_inventory(partial) is None
    foreign = _state(ready=True)
    foreign["systemPrompt"][-1] = foreign["systemPrompt"][-1].replace(
        "mcp__daem0nmcp_", "mcp__foreign_"
    )
    assert driver._omp_core_inventory(foreign) is None
    assert driver._omp_core_inventory(_state(ready=True))["tools"] == sorted(CORE)


def test_direct_inventory_requires_exact_core_names_and_parameter_schemas():
    state = _state()
    state["dumpTools"] = [{"name": "mcp__daem0nmcp_" + name} for name in CORE]
    assert driver._omp_core_inventory(state) is None
    for tool in state["dumpTools"]:
        tool["parameters"] = {"type": "object", "properties": {}}
    assert driver._omp_core_inventory(state)["source"] == "dumpTools"


def test_readiness_polls_matching_fresh_states_without_prompt_or_memory_calls(tmp_path):
    recorded = []

    def callback(process, request):
        process.incoming.put({"type": "ready"})
        process.reply(request, id="stale-unrelated-response")
        process.reply(request, data=_state(ready=len(process.sent) == 2))

    process = _Process(callback)
    result = driver._wait_omp_mcp_ready(
        process, process.incoming, recorded.append, tmp_path, timeout=2
    )
    assert result["status"] == "passed"
    assert result["requests"] == 2
    assert result["ignored_responses"] == 2
    assert result["provider_prompts_submitted"] == result["memory_calls"] == 0
    assert all(request["type"] == "get_state" for request in process.sent)
    assert len({request["id"] for request in process.sent}) == 2
    assert len(recorded) == 2


@pytest.mark.parametrize(
    "failure,reason",
    [
        ({"success": False}, "ERROR"),
        ({"data": []}, "MALFORMED"),
        ({"data": {"systemPrompt": [None], "dumpTools": []}}, "MALFORMED"),
        ({"command": "other_command"}, "MALFORMED"),
    ],
)
def test_matching_error_or_malformed_response_fails_closed(tmp_path, failure, reason):
    process = _Process(lambda process, request: process.reply(request, **failure))
    with pytest.raises(
        driver._OMPReadinessError, match="NATIVE_MCP_READINESS_" + reason
    ):
        driver._wait_omp_mcp_ready(
            process, process.incoming, lambda _: None, tmp_path, timeout=1
        )
    retained = json.loads((tmp_path / "omp-readiness.json").read_text())
    assert retained["status"] == "failed"
    assert retained["provider_prompts_submitted"] == 0


@pytest.mark.parametrize(
    "event,reason",
    [
        ({"process_exit": True}, "PROCESS_EXIT"),
        ({"type": "extension_ui_request", "method": "select"}, "UNEXPECTED_REQUEST"),
        ({"type": "error", "message": "native startup failed"}, "ERROR"),
    ],
)
def test_exit_and_unexpected_startup_decisions_do_not_get_approval(
    tmp_path, event, reason
):
    process = _Process(lambda process, _: process.incoming.put(event))
    with pytest.raises(
        driver._OMPReadinessError, match="NATIVE_MCP_READINESS_" + reason
    ):
        driver._wait_omp_mcp_ready(
            process, process.incoming, lambda _: None, tmp_path, timeout=1
        )
    assert [request["type"] for request in process.sent] == ["get_state"]


def test_stale_ready_reply_cannot_satisfy_current_request_or_escape_deadline(tmp_path):
    process = _Process(lambda process, request: process.reply(request, id="old-ready"))
    with pytest.raises(driver._OMPReadinessError, match="NATIVE_MCP_READINESS_TIMEOUT"):
        driver._wait_omp_mcp_ready(
            process, process.incoming, lambda _: None, tmp_path, timeout=0.02
        )
    assert all(request["type"] == "get_state" for request in process.sent)


def test_unsolicited_null_id_ready_state_cannot_satisfy_gap_between_polls(tmp_path):
    def callback(process, request):
        process.reply(request, data=_state())
        process.reply(request, id=None)

    process = _Process(callback)
    with pytest.raises(driver._OMPReadinessError, match="NATIVE_MCP_READINESS_TIMEOUT"):
        driver._wait_omp_mcp_ready(
            process, process.incoming, lambda _: None, tmp_path, timeout=0.02
        )


def test_broken_rpc_input_records_failure_without_prompt(tmp_path):
    def fail(_process, _request):
        raise BrokenPipeError()

    process = _Process(fail)
    with pytest.raises(
        driver._OMPReadinessError, match="NATIVE_MCP_READINESS_IO_ERROR"
    ):
        driver._wait_omp_mcp_ready(
            process, process.incoming, lambda _: None, tmp_path, timeout=1
        )


class _StreamProcess(_Process):
    pid = 12345

    def __init__(self, *, ready):
        super().__init__(self.respond)
        self.stdout_queue = queue.Queue()
        self.stdout = self.lines()
        self.stderr = io.StringIO("")
        self.ready = ready
        self.returncode = None

    def lines(self):
        while True:
            item = self.stdout_queue.get()
            if item is None:
                return
            yield json.dumps(item) + "\n"

    def respond(self, _process, request):
        if request["type"] == "get_state":
            self.stdout_queue.put(
                {
                    "id": request["id"],
                    "type": "response",
                    "command": "get_state",
                    "success": True,
                    "data": _state(ready=self.ready),
                }
            )
        else:
            assert request["type"] == "prompt"
            self.stdout_queue.put({"type": "agent_end"})
            self.returncode = 0
            self.stdout_queue.put(None)

    def poll(self):
        return self.returncode

    def close(self):
        pass

    def wait(self, timeout=None):
        return self.returncode


@pytest.mark.parametrize("ready", [True, False])
def test_stream_submits_original_prompt_only_after_ready_inventory(
    tmp_path, monkeypatch, ready
):
    process = _StreamProcess(ready=ready)
    monkeypatch.setattr(driver, "_spawn", lambda *_args, **_kwargs: process)

    def cleanup(_process):
        process.returncode = 0
        process.stdout_queue.put(None)
        return {"owned_job_closed": True, "native_root_exited": True}

    monkeypatch.setattr(driver, "_close_owned_group", cleanup)
    monkeypatch.setattr(driver, "_finish", lambda *args: args[6])
    metadata = driver.run_stream(
        host="omp",
        argv=["fake-native"],
        cwd=tmp_path,
        env={},
        workspace_id="ws_" + "a" * 24,
        manifest={},
        evidence=tmp_path / "evidence",
        stage="mutate",
        timeout=1 if ready else 0.02,
        omp_rpc=True,
        prompt_text="Exact unchanged stage prompt",
    )
    prompts = [request for request in process.sent if request["type"] == "prompt"]
    if ready:
        assert prompts == [
            {
                "id": "native-acceptance-prompt",
                "type": "prompt",
                "message": "Exact unchanged stage prompt",
            }
        ]
        assert process.sent[0]["type"] == "get_state"
    else:
        assert prompts == []
        assert metadata["terminal_capability_error"] == "NATIVE_MCP_READINESS_TIMEOUT"
