"""Path policy on the v7 wire (KD-2, UD-3).

User text (memory content, rule text, queries, and server text quoting them)
may mention routes and file paths.  Every other string keeps the
absolute-path rule, input and output, through one predicate.  Anything the
server accepts must read back: nothing can be stored and then brick a read.
"""

from __future__ import annotations

import copy
import hashlib
import sqlite3
import time
import types
from contextlib import closing
from pathlib import Path
from typing import Annotated, Any, Literal, Union, get_args, get_origin, get_type_hints

import pytest
from fastmcp import Client
from pydantic import BaseModel, ValidationError, create_model

from daem0nmcp.api.v7.errors import ErrorCode
from daem0nmcp.api.v7.models import (
    Cursor,
    JsonObject,
    RelativePath,
    WireModel,
    _user_text_fields,
    is_host_absolute_path,
)
from daem0nmcp.api.v7.production import create_v7_server
from daem0nmcp.api.v7.resources import (
    ActiveContextResourceDocument,
    FailureResourceDocument,
    RuleResourceDocument,
    WarningResourceDocument,
)
from daem0nmcp.api.v7.responses import ResponseFactory
from daem0nmcp.api.v7.tools import (
    TOOL_DATA_MODELS,
    TOOL_INPUT_MODELS,
    CodeIndexInput,
    DiagnosticSummary,
    DocumentIngestUrlInput,
    ExportEvent,
    HealthData,
    HttpsUrl,
    MediumText,
    MemoryStoreInput,
    PortableVectorPoint,
    SandboxExecutionData,
)
from daem0nmcp.config import Settings
from daem0nmcp.discovery_projection import (
    CommunityProjectionSeed,
    DiscoveryProjectionBuilder,
    EntityProjectionSeed,
    EntityRecordSeed,
)
from daem0nmcp.event_store import (
    EventCommand,
    EventStore,
    GovernanceEventCommand,
    GovernanceEventStore,
)
from daem0nmcp.retrieval.runtime import await_projection_job_drains
from daem0nmcp.retrieval.specialized_projection import SpecializedProjectionBuilder
from daem0nmcp.storage_activation import resolve_active_database
from tests.api_v7.process_client import initialize_workspaces
from tests.api_v7.test_process_workspace_isolation_sweep import (
    CANARY,
    CROSS_WORKSPACE_TOOLS,
    PROFILE_ENVIRONMENT,
    _code,
    _is_protected,
    _mint_tokens,
    _seed_workspace_a,
    _sweep_arguments,
    _target_call,
)

# The UD-3 examples: a regex escape, routes, and a Windows file location.
PATH_CONTENT = ("\\\\d+", "/health", "GET /api/v1/users", "C:\\proj\\src\\app.py")
# Mechanical-coverage seeds (R-26).
SEEDS = ("C:\\proj\\x.py", "/health", "\\d+", "\x1b[0m")
NEUTRAL = "plain neutral words"
# A path in text is the user's business; these outcomes mean it was refused.
# Readers turn a row that fails its output model into CAPABILITY_DEGRADED.
REFUSALS = frozenset(
    {
        "INVALID_ARGUMENT",
        "INTERNAL_ERROR",
        "WORKSPACE_PATH_ESCAPE",
        "SCHEMA_REJECTED",
        "CAPABILITY_DEGRADED",
    }
)
RESOURCE_KINDS = ("warnings", "failures", "rules", "active-context")
TOKEN = "preflight-token-placeholder-0001"
WS = "ws_" + "0" * 24


def _invoker(client: Client):
    """Call a tool; a schema rejection by FastMCP becomes SCHEMA_REJECTED."""

    async def invoke(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await client.call_tool(tool, arguments, raise_on_error=False)
        if result.structured_content is None:
            assert result.is_error, tool
            return {"ok": False, "error": {"code": "SCHEMA_REJECTED"}}
        return result.structured_content

    return invoke


# ---------------------------------------------------------------------------
# Model-level checks.


def _store_input(**overrides: Any) -> dict[str, Any]:
    return {
        "workspace_id": WS,
        "record_type": "decision",
        "content": "text",
        "idempotency_key": "path-policy-0001",
        "preflight_token": TOKEN,
        **overrides,
    }


@pytest.mark.parametrize("path", ["C:\\x", "/etc/x"])
def test_relative_file_path_rejects_host_paths(path):
    with pytest.raises(ValidationError):
        MemoryStoreInput.model_validate(_store_input(relative_file_path=path))


@pytest.mark.parametrize("text", [*PATH_CONTENT, *SEEDS])
def test_user_text_input_fields_accept_paths(text):
    validated = MemoryStoreInput.model_validate(
        _store_input(
            content=text,
            rationale=text,
            tags=[text],
            context={"note": text, text: [text]},
        )
    )
    assert validated.content == text


def test_server_generated_fields_reject_host_paths():
    with pytest.raises(ValidationError):
        DiagnosticSummary(code="CODE_INDEX_SKIPPED", message="C:\\x")
    health = {
        "package_version": "7.0.0",
        "protocol_version": "2025-06-18",
        "supported_transports": {"stdio"},
        "auth_mode": "loopback",
        "task_support": {"name": "tasks", "status": "ready"},
    }
    HealthData.model_validate(health)
    with pytest.raises(ValidationError):
        HealthData.model_validate(
            {
                **health,
                "capability_states": [
                    {
                        "name": "storage",
                        "status": "failed",
                        "remediation": "Repair C:\\x",
                    }
                ],
            }
        )


def test_relative_paths_are_checked_by_their_own_validator():
    """Valid relative names and globs are not mistaken for absolute paths."""
    names = ["src/**/foo.py", "src/__pycache__/x", "docs-/a", "pkg_/a.py"]
    index = CodeIndexInput(workspace_id=WS, patterns=names)
    assert index.patterns == names
    for invalid in ("C:\\x", "/etc/x", "../x", "src/../x", "~/x"):
        with pytest.raises(ValidationError):
            CodeIndexInput(workspace_id=WS, patterns=[invalid])


def test_ingest_topic_accepts_paths():
    for text in SEEDS:
        DocumentIngestUrlInput(
            workspace_id=WS,
            url="https://example.com/doc",
            topic=text,
            idempotency_key="path-policy-0001",
            preflight_token=TOKEN,
        )


def test_error_remedies_echo_path_bearing_arguments():
    """A token error on path-bearing input returns its structured remedy (R-1)."""
    for text in SEEDS:
        failure = (
            ResponseFactory()
            .begin(WS)
            .failure(
                ErrorCode.COUNSEL_REQUIRED,
                "A preflight is required.",
                remedy_tool="memory_preflight",
                remedy_arguments={
                    "workspace_id": WS,
                    "target_tool": "rule_create",
                    "target_arguments": {"trigger": text, "must_do": [text]},
                },
            )
        )
        assert failure.error.remedy.arguments["target_arguments"]["trigger"] == text


def test_sandbox_output_lines_may_mention_paths():
    line = 'File "/tmp/run.py", line 1, in <module>'
    data = SandboxExecutionData(
        success=False,
        stdout="",
        stderr=line,
        exit_status=1,
        execution_time_ms=5,
        sanitized_logs=[f"stderr: {line}", "x" * 4096],
    )
    assert data.sanitized_logs[0].endswith(line)


def test_export_structured_fields_stay_guarded():
    """Only user text in export payloads is exempt (R-3, R-4)."""
    from daem0nmcp.api.v7.operations import CoreOperationError, _reject_raw_paths

    user = {"record": {"content": "/health", "context": {"note": "C:\\x"}}}
    _reject_raw_paths([user])
    for relative in ("../../Users/x/y.py", "d:/other/x.py", "/etc/x"):
        with pytest.raises(CoreOperationError):
            _reject_raw_paths([{"record": {"file_path_relative": relative}}])
    with pytest.raises(CoreOperationError):
        _reject_raw_paths([{"record": {"file_path": "C:\\proj\\x.py"}}])
    event = {
        "event_id": "evt_" + "2" * 64,
        "event_type": "memory.created",
        "happened_at": "2026-08-08T12:00:00Z",
        "content_hash": "a" * 64,
        "payload": {"data": {"content": "C:\\proj\\x.py"}, "actor_id": "agent"},
    }
    ExportEvent.model_validate(event)
    with pytest.raises(ValidationError):
        ExportEvent.model_validate(
            {**event, "payload": {**event["payload"], "actor_id": "C:\\proj"}}
        )
    with pytest.raises(ValidationError):
        PortableVectorPoint(point_id="p", payload={"model_id": "C:\\m"}, vector=[1.0])


def test_every_input_field_shape_is_classified():
    """Discovery walks every input model, bundles included, and knows each shape."""
    paths = {
        f"{tool}.{'.'.join(map(str, path))}"
        for tool, model in TOOL_INPUT_MODELS.items()
        for path, _kind_ in _free_text_paths(model)
    }
    assert {
        "workspace_import.bundle.events.0.payload",
        "workspace_import.bundle.legacy_rows",
        "memory_store_batch.records.0.context",
    } <= paths, sorted(paths)


def _models(root: type[BaseModel], seen: set[type[BaseModel]]) -> None:
    if root in seen:
        return
    seen.add(root)
    for field in root.model_fields.values():
        for model in _nested_models(field.annotation):
            _models(model, seen)


def _nested_models(annotation: object):
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
    for argument in get_args(annotation):
        yield from _nested_models(argument)


def _sample(annotation: object, text: str) -> object:
    """A value of ``annotation``'s shape whose every string is ``text``."""
    origin = get_origin(annotation)
    if origin is Annotated:
        return _sample(get_args(annotation)[0], text)
    if origin in (Union, types.UnionType):
        options = [item for item in get_args(annotation) if item is not type(None)]
        return _sample(options[0], text)
    if origin in (list, set, frozenset):
        return [_sample(get_args(annotation)[0], text)]
    if origin is dict:
        return {text: _sample(get_args(annotation)[1], text)}
    if annotation is JsonObject:
        return {"note": text, text: [text]}
    assert annotation is str, annotation
    return text


def test_user_text_round_trips_into_every_output_field_that_carries_it():
    """Each string a UserText input accepts validates in every UserText output.

    This shows the exemption works for every output field already typed
    ``UserText`` (found from the models, each probe inheriting ``WireModel``'s
    path check).  It cannot show that the right fields carry the type; the
    end-to-end test below does that.
    """
    outputs: set[type[BaseModel]] = set()
    for model in (
        *TOOL_DATA_MODELS.values(),
        WarningResourceDocument,
        FailureResourceDocument,
        RuleResourceDocument,
        ActiveContextResourceDocument,
    ):
        _models(model, outputs)
    carriers = {
        (model.__name__, name): Annotated[
            (field.annotation, *field.metadata)  # type: ignore[valid-type]
        ]
        if field.metadata
        else field.annotation
        for model in outputs
        for name, field in model.model_fields.items()
        if name in _user_text_fields(model)
    }
    assert len(carriers) > 30, sorted(carriers)
    for text in (*PATH_CONTENT, *SEEDS):
        MemoryStoreInput.model_validate(_store_input(content=text))
        for (model_name, name), annotation in carriers.items():
            probe = create_model(
                f"{model_name}_{name}", __base__=WireModel, value=(annotation, ...)
            )
            probe.model_validate({"value": _sample(annotation, text)})
        # The probe is meaningful: a guarded text field refuses the same text.
        guarded = create_model("Guarded", __base__=WireModel, value=(MediumText, ...))
        if is_host_absolute_path(text):
            with pytest.raises(ValidationError):
                guarded.model_validate({"value": text})


# ---------------------------------------------------------------------------
# Rows as a migrated or pre-fix database carries them.


def _hex(*parts: object) -> str:
    return hashlib.sha256(repr(parts).encode()).hexdigest()


def _now_us() -> int:
    return time.time_ns() // 1_000


def _memory_record(record_type: str, text: str) -> dict[str, Any]:
    return {
        "record_type": record_type,
        "legacy_type": None,
        "content": f"Migrated note: {text}",
        "rationale": text,
        "context": {"note": text},
        "tags": [text],
        "file_path": None,
        "file_path_relative": None,
        "keywords": None,
        "is_permanent": False,
        "pinned": False,
        "archived": False,
        "outcome": None,
        "worked": None,
        "recall_count": 0,
        "surprise_score": None,
        "importance_score": None,
        "source_client": "migration",
        "source_model": None,
        "deleted_at_us": None,
    }


def _insert_rows(root: Path, workspace_id: str, texts: tuple[str, ...]) -> list[str]:
    """Write path-bearing records, a rule, a trigger, entities and a community."""
    database = resolve_active_database(root / ".daem0nmcp" / "storage").path
    record_ids: list[str] = []
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        now = _now_us() - 1_000_000
        for index, text in enumerate(texts):
            for record_type in ("decision", "warning"):
                record_id = "mem_" + _hex("row", text, record_type)
                EventStore(connection).append_and_project(
                    EventCommand(
                        workspace_id=workspace_id,
                        stream_id=record_id,
                        stream_kind="memory",
                        event_type="memory.created",
                        occurred_at_us=now + index,
                        recorded_at_us=now + index,
                        actor_type="migration",
                        payload={"record": _memory_record(record_type, text)},
                    )
                )
                record_ids.append(record_id)
            GovernanceEventStore(connection).append_and_project(
                GovernanceEventCommand(
                    workspace_id=workspace_id,
                    stream_id="rule_" + _hex("rule", text),
                    stream_kind="rule",
                    event_type="rule.created",
                    occurred_at_us=now,
                    recorded_at_us=now,
                    actor_type="migration",
                    payload={
                        "rule_id": "rule_" + _hex("rule", text),
                        "trigger": text,
                        "must_do": [text],
                        "must_not": [text],
                        "ask_first": [text],
                        "warnings": [text],
                        "priority": 0,
                        "enabled": True,
                        "created_at_us": now,
                        "updated_at_us": now,
                    },
                )
            )
            GovernanceEventStore(connection).append_and_project(
                GovernanceEventCommand(
                    workspace_id=workspace_id,
                    stream_id="trg_" + _hex("trigger", text),
                    stream_kind="trigger",
                    event_type="context_trigger.created",
                    occurred_at_us=now,
                    recorded_at_us=now,
                    actor_type="migration",
                    payload={
                        "trigger_id": "trg_" + _hex("trigger", text),
                        # A file glob: a tag pattern is a regex, which
                        # "C:\proj\x.py" is not.
                        "trigger_type": "file",
                        "pattern": text,
                        "recall_query": text,
                        "categories": [],
                        "enabled": True,
                        "priority": 0,
                        "created_at_us": now,
                        "updated_at_us": now,
                        "deleted_at_us": None,
                    },
                )
            )
        connection.commit()
        SpecializedProjectionBuilder(connection).rebuild(workspace_id, "graph")
        DiscoveryProjectionBuilder(connection).populate_graph(
            workspace_id,
            entities=tuple(
                EntityProjectionSeed(
                    name=text,
                    entity_type="file",
                    records=(EntityRecordSeed(record_ids[2 * index]),),
                )
                for index, text in enumerate(texts)
                if is_host_absolute_path(text)
            ),
            communities=(
                CommunityProjectionSeed(
                    source_key="paths",
                    label=texts[0],
                    level=0,
                    member_record_ids=tuple(record_ids),
                ),
            ),
        )
        # The graph rebuild queued by the events above just ran here; retire
        # it so a later drain cannot swap the inserted entities for a new
        # generation mid-test.
        connection.execute(
            "UPDATE background_jobs SET status='succeeded',updated_at_us=?,"
            "finished_at_us=? WHERE workspace_id=? AND status='queued' "
            "AND idempotency_key='active-projection:graph'",
            (_now_us(), _now_us(), workspace_id),
        )
        connection.commit()
    return record_ids


def _server(roots: tuple[Path, ...]):
    return create_v7_server(
        "stdio",
        settings=Settings(
            project_root=str(roots[0]),
            workspace_roots=[str(root) for root in roots],
            dream_enabled=False,
        ),
        environ=PROFILE_ENVIRONMENT,
    )


async def test_path_content_is_stored_and_read_back(tmp_path):
    """UD-3 content stores once and every read of it succeeds, pre-fix rows too."""
    root = tmp_path / "project"
    (workspace,) = await initialize_workspaces((root,))
    ws = workspace.workspace_id
    poisoned = _insert_rows(root, ws, ("\\\\d+",))
    async with Client(_server((root,))) as client:
        invoke = _invoker(client)

        async def ok(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
            result = await _target_call(invoke, ws, tool, arguments)
            assert result["ok"], (tool, arguments, result["error"])
            return result["data"]

        brief = await ok("session_brief", {})
        assert poisoned[1] in {item["record_id"] for item in brief["warnings"]}
        for index, text in enumerate(PATH_CONTENT):
            for record_type in ("decision", "warning"):
                stored = await ok(
                    "memory_store",
                    {
                        "record_type": record_type,
                        "content": f"Remember {text}",
                        "idempotency_key": f"path-content-{index}-{record_type}",
                    },
                )
                record_id = stored["record"]["record_id"]
                assert stored["record"]["excerpt"] == f"Remember {text}"
                brief = await ok("session_brief", {})
                if record_type == "warning":
                    assert record_id in {
                        item["record_id"] for item in brief["warnings"]
                    }
                await ok("memory_recall", {"query": text})
                await ok("memory_search_text", {"query": text})
                preflight = await invoke(
                    "memory_preflight",
                    {
                        "workspace_id": ws,
                        "target_tool": "memory_store",
                        "target_arguments": {
                            "record_type": record_type,
                            "content": text,
                            "idempotency_key": f"path-preflight-{index}",
                        },
                        "description": text,
                    },
                )
                assert preflight["ok"], preflight["error"]
                await ok(
                    "memory_record_outcome",
                    {
                        "record_id": record_id,
                        "outcome_text": text,
                        "worked": False,
                        "idempotency_key": f"path-outcome-{index}-{record_type}",
                    },
                )
        stale = await invoke(
            "memory_store",
            {
                "workspace_id": ws,
                "record_type": "decision",
                "content": "GET /health returns 200",
                "idempotency_key": "path-stale-token-0001",
                "preflight_token": TOKEN,
            },
        )
        assert stale["error"]["code"].startswith("TOKEN_"), stale
        remedy = stale["error"]["remedy"]
        assert remedy["tool"] == "memory_preflight"
        assert remedy["arguments"]["target_arguments"]["content"] == (
            "GET /health returns 200"
        )
        await ok("session_brief", {})
        for kind in RESOURCE_KINDS:
            await client.read_resource(f"memory://workspaces/{ws}/{kind}")


# ---------------------------------------------------------------------------
# Mechanical coverage (R-26).

# An import bundle is hash-chained: editing any one string makes it
# IMPORT_INVALID.  Its fields are covered by exporting the seeded workspace
# and importing it back (see the end of the test), not by field seeding.
ROUND_TRIPPED = frozenset({"workspace_import"})
# The ingest URL is fetched before the topic is read, and tests have no
# network, so every call stops at the URL.  The topic's input type is
# covered by test_ingest_topic_accepts_paths.
FETCH_FIRST = frozenset({"document_ingest_url.topic"})


def _mentions_str(annotation: object) -> bool:
    return annotation is str or any(_mentions_str(a) for a in get_args(annotation))


def _kind(annotation: object) -> object:
    """``"str"``, ``"json"``, a model class, ``("list", kind)`` or ``None``.

    Fails on any string-bearing shape it cannot classify, so a new free-text
    shape cannot silently drop out of coverage.
    """
    if annotation in (Cursor, RelativePath, HttpsUrl):
        return None
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        kinds = {_kind(item) for item in get_args(annotation) if item is not type(None)}
        kinds.discard(None)
        assert len(kinds) <= 1, f"mixed union {annotation!r}"
        return kinds.pop() if kinds else None
    if origin is Annotated:
        base, *metadata = get_args(annotation)
        if any(getattr(item, "pattern", None) for item in metadata):
            return None
        return _kind(base)
    if origin in (list, set, frozenset):
        inner = _kind(get_args(annotation)[0])
        assert not isinstance(inner, tuple), f"nested list {annotation!r}"
        return None if inner is None else ("list", inner)
    if origin is Literal:
        return None
    if annotation is str:
        return "str"
    if annotation is JsonObject:
        return "json"
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    assert not _mentions_str(annotation), f"unclassified {annotation!r}"
    return None


def _free_text_paths(model: type[BaseModel], prefix=()):
    """Every free-text field of ``model`` and its nested models, by annotation."""
    hints = get_type_hints(model, include_extras=True)
    for name in model.model_fields:
        kind = _kind(hints[name])
        if kind is None:
            continue
        if isinstance(kind, type):
            yield from _free_text_paths(kind, (*prefix, name))
        elif isinstance(kind, tuple) and isinstance(kind[1], type):
            yield from _free_text_paths(kind[1], (*prefix, name, 0))
        else:
            yield (*prefix, name), kind


def _seed_value(kind: object, text: str) -> object:
    if kind == "str":
        return text
    if kind == "json":
        return {"note": text, text: [text]}
    if kind == ("list", "str"):
        return [text]
    assert kind == ("list", "json"), kind
    return [{"note": text, text: [text]}]


def _seeded(tool: str, base: dict[str, Any], path: tuple, kind: object, text: str):
    """``base`` with the field at ``path`` set to ``text``, and its siblings fit."""
    arguments = copy.deepcopy(base)
    target: Any = arguments
    for step, following in zip(path[:-1], path[1:], strict=True):
        if isinstance(step, int):
            while len(target) <= step:
                target.append({})
        elif step not in target:
            target[step] = [] if isinstance(following, int) else {}
        target = target[step]
    name = path[-1]
    # Exactly one selector is allowed; the seeded name replaces the id.
    target.pop(
        {"entity_name": "entity_id", "qualified_name": "code_entity_id"}.get(name, ""),
        None,
    )
    if name == "procedure_steps":
        target["record_type"] = "procedure"
    if (tool, path) == ("memory_preflight", ("target_arguments",)):
        # Seed the target tool's own argument, not the envelope.
        target["target_tool"] = "memory_store"
        target["target_arguments"] = {
            "record_type": "decision",
            "content": text,
            "idempotency_key": "mech-preflight-target-0001",
        }
        return arguments
    target[name] = _seed_value(kind, text)
    return arguments


def _fresh_keys(arguments: dict[str, Any], label: str) -> dict[str, Any]:
    if "idempotency_key" in arguments:
        digest = hashlib.sha256(label.encode()).hexdigest()[:40]
        return {**arguments, "idempotency_key": f"mech-{digest}"}
    return arguments


def _own_read_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in arguments.items()
        if key not in {"cursor", "after_cursor"}
    }


async def _seed_free_text(invoke, workspace_id: str, tool: str, arguments: dict):
    """Call ``tool`` with each free-text field set to each seed.

    The neutral-text call must not be refused, so a wrongly guarded field
    cannot hide behind bad base arguments.  (It may miss: a lookup by a name
    that does not exist is NOT_FOUND.)  Each seeded call must then get the
    neutral call's outcome or at least not a refusal.
    """
    if tool in ROUND_TRIPPED:
        return []
    base = _own_read_arguments(arguments)
    labels = []
    for path, kind in _free_text_paths(TOOL_INPUT_MODELS[tool]):
        label = f"{tool}.{'.'.join(map(str, path))}"
        if label in FETCH_FIRST:
            continue
        labels.append(label)
        codes = []
        for text in (NEUTRAL, *SEEDS):
            call = _fresh_keys(_seeded(tool, base, path, kind, text), label + text)
            response = await _target_call(invoke, workspace_id, tool, call)
            if _code(response) == "INTERNAL_ERROR":
                # SQLite contention with a background projection drain is still
                # reported as INTERNAL_ERROR on this base (PR 3 makes it
                # retryable).  A refusal caused by the text repeats; retry once.
                call = _fresh_keys(call, label + text + "retry")
                response = await _target_call(invoke, workspace_id, tool, call)
            codes.append(_code(response))
        neutral, *seeded = codes
        assert neutral not in REFUSALS, (label, neutral)
        for text, code in zip(SEEDS, seeded, strict=True):
            assert code == neutral or code not in REFUSALS, (label, text, code, neutral)
    return labels


def _is_write(tool: str) -> bool:
    # memory_record_outcome writes a memory event without a preflight; like
    # every write it marks the graph stale until the next rebuild.
    return _is_protected(tool) or tool == "memory_record_outcome"


async def _read_everything(invoke, workspace_id: str, seed) -> dict[str, str]:
    """Every read tool with the workspace's own objects."""
    codes: dict[str, str] = {}
    for tool, arguments in sorted(_sweep_arguments(seed).items()):
        if _is_write(tool) or tool in CROSS_WORKSPACE_TOOLS:
            continue
        plain = _fresh_keys(_own_read_arguments(arguments), f"{tool}-{len(codes)}")
        codes[tool] = _code(await invoke(tool, {"workspace_id": workspace_id, **plain}))
    return codes


async def _export_pages(invoke, workspace_id: str) -> list[dict[str, Any]]:
    scope = {"workspace_id": workspace_id, "page_byte_limit": 65536}
    page = await invoke("workspace_export", scope)
    assert page["ok"], page["error"]
    pages = [page["data"]]
    while not pages[-1]["complete"]:
        previous = pages[-1]
        page = await invoke(
            "workspace_export",
            {
                **scope,
                "export_session_id": previous["export_session_id"],
                "page_index": previous["page_index"] + 1,
                "cursor": previous["next_cursor"],
            },
        )
        assert page["ok"], page["error"]
        pages.append(page["data"])
    return pages


async def _import_pages(invoke, workspace_id: str, pages) -> dict[str, Any]:
    import_id = None
    key = "mech-import-0001"
    for page in pages:
        arguments = {"bundle": page, "finalize": False, "idempotency_key": key}
        if import_id is not None:
            arguments["import_session_id"] = import_id
        staged = await _target_call(invoke, workspace_id, "workspace_import", arguments)
        assert staged["ok"], staged["error"]
        import_id = staged["data"]["import_session_id"]
    finish = {"import_session_id": import_id, "finalize": True, "idempotency_key": key}
    done = await _target_call(invoke, workspace_id, "workspace_import", finish)
    assert done["ok"], done["error"]
    return done["data"]


def _texts(value: Any) -> set[str]:
    if isinstance(value, dict):
        return {text for item in value.values() for text in _texts(item)}
    if isinstance(value, list):
        return {text for item in value for text in _texts(item)}
    return {value} if isinstance(value, str) else set()


# Reads that stay gated whatever is stored; every other read must be ok.
GATED_READS = {
    # Needs a native edit request, which only the edit hook makes.
    "edit_preflight": "NOT_FOUND",
    # The default selector exceeds the foreground node budget; the same
    # tools are read below with a small selector over the path records.
    "knowledge_graph_get": "TASK_REQUIRED",
    "knowledge_graph_render": "TASK_REQUIRED",
}


async def test_every_free_text_field_accepts_paths_and_every_read_survives(tmp_path):
    """Seed every free-text input field and DB row with paths; read everything.

    An input field typed as free text but not ``UserText`` refuses the seed,
    and an output field carrying it fails or degrades the read; either fails
    this test.  The workspace is then exported and imported back.
    """
    a_root, b_root = tmp_path / "alpha", tmp_path / "beta"
    alpha, beta = await initialize_workspaces((a_root, b_root))
    a, b = alpha.workspace_id, beta.workspace_id
    (a_root / "src").mkdir()
    (a_root / "src" / "owned.py").write_text(
        f"def {CANARY}_function():\n    return 1  # TODO: see C:\\proj\\x.py /health\n",
        encoding="utf-8",
    )
    seeded: list[str] = []
    async with Client(_server((a_root, b_root))) as client:
        invoke = _invoker(client)
        seed = await _seed_workspace_a(invoke, a, b)
        await _mint_tokens(invoke, a, b, seed)
        # Writes first; reads are seeded below, once every kind of row exists
        # (a name lookup before any entity exists stops at the graph gate).
        writes = {
            tool: arguments
            for tool, arguments in _sweep_arguments(seed).items()
            if _is_write(tool)
        }
        writes["workspace_link"] = {"linked_workspace_id": b}
        for tool, arguments in sorted(writes.items()):
            seeded += await _seed_free_text(invoke, a, tool, arguments)

    # Rows as a migrated v6 database or a pre-fix write left them.
    record_ids = _insert_rows(a_root, a, SEEDS)

    async with Client(_server((a_root, b_root))) as client:
        invoke = _invoker(client)
        assert (await invoke("session_brief", {"workspace_id": a}))["ok"]
        await _mint_tokens(invoke, a, b, seed)
        entities = await invoke("entity_list", {"workspace_id": a, "limit": 100})
        path_seeds = {text for text in SEEDS if is_host_absolute_path(text)}
        assert path_seeds <= {item["name"] for item in entities["data"]["items"]}
        seed.ids["entity"] = entities["data"]["items"][0]["entity_id"]
        # The inserted graph generation replaces the seed's community IDs.
        communities = await invoke("community_list", {"workspace_id": a})
        seed.ids["community"] = communities["data"]["items"][0]["community_id"]
        # The stored text comes back verbatim, not dropped or degraded.
        # Before any read that writes (outcomes, rebuilds) makes the graph stale.
        shown: set[str] = set()
        for tool, arguments in (
            ("rule_list", {"limit": 100}),
            ("context_trigger_list", {"limit": 100}),
            ("memory_search_text", {"query": "Migrated note", "limit": 100}),
            (
                "knowledge_graph_render",
                {"record_ids": record_ids[:2], "include_orphans": True, "max_nodes": 2},
            ),
            (
                "knowledge_graph_get",
                {"record_ids": record_ids[:2], "include_orphans": True, "max_nodes": 2},
            ),
        ):
            response = await invoke(tool, {"workspace_id": a, **arguments})
            assert response["ok"], (tool, response["error"])
            shown |= _texts(response["data"])
        assert set(SEEDS) <= shown, set(SEEDS) - shown
        assert {f"Migrated note: {text}" for text in SEEDS} <= shown
        after = await _read_everything(invoke, a, seed)
        assert {t: c for t, c in after.items() if c != "ok"} == GATED_READS, after
        for tool, arguments in sorted(_sweep_arguments(seed).items()):
            if tool in after:
                # Query-like fields searching for the paths.
                seeded += await _seed_free_text(invoke, a, tool, arguments)
        for kind in RESOURCE_KINDS:
            await client.read_resource(f"memory://workspaces/{a}/{kind}")
        # Let background projection drains settle so the export pages form
        # one consistent snapshot.
        await await_projection_job_drains()
        pages = await _export_pages(invoke, a)

    # The exported workspace imports back into empty storage (R-24).
    storage = a_root / ".daem0nmcp" / "storage"
    storage.rename(a_root / ".daem0nmcp" / "retained-before-import")
    async with Client(_server((a_root, b_root))) as client:
        invoke = _invoker(client)
        assert (await invoke("session_brief", {"workspace_id": a}))["ok"]
        restored = await _import_pages(invoke, a, pages)
        assert restored["root_hash"] == pages[0]["root_hash"]
        found = await invoke(
            "memory_search_text",
            {"workspace_id": a, "query": "Migrated note", "limit": 100},
        )
        assert found["ok"], found["error"]
        assert {f"Migrated note: {text}" for text in SEEDS} <= _texts(found["data"])

    # Nested and cross-workspace inputs are reached too; the floor catches a
    # change that quietly drops fields from coverage.
    assert {
        "memory_store.context",
        "memory_store.procedure_steps",
        "memory_store_batch.records.0.content",
        "memory_preflight.target_arguments",
        "entity_evolution_trace.entity_name",
        "rule_update.patch.must_do",
        "workspace_link.label",
    } <= set(seeded), seeded
    assert len(seeded) >= 60, len(seeded)
