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

from daem0nmcp.api.v7.models import (
    Cursor,
    JsonObject,
    RelativePath,
    WireModel,
    _user_text_fields,
)
from daem0nmcp.api.v7.production import create_v7_server
from daem0nmcp.api.v7.resources import (
    ActiveContextResourceDocument,
    FailureResourceDocument,
    RuleResourceDocument,
    WarningResourceDocument,
)
from daem0nmcp.api.v7.tools import (
    TOOL_DATA_MODELS,
    TOOL_INPUT_MODELS,
    DiagnosticSummary,
    HealthData,
    HttpsUrl,
    MediumText,
    MemoryStoreInput,
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
from daem0nmcp.retrieval.specialized_projection import SpecializedProjectionBuilder
from daem0nmcp.storage_activation import resolve_active_database
from tests.api_v7.process_client import initialize_workspaces
from tests.api_v7.test_process_workspace_isolation_sweep import (
    CANARY,
    CROSS_WORKSPACE_TOOLS,
    GATE_CODES,
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
SEEDS = ("C:\\proj\\x.py", "/health", "\\d+")
NEUTRAL = "plain neutral words"
# A path in text is the user's business; these outcomes mean it was refused.
REFUSALS = frozenset(
    {"INVALID_ARGUMENT", "INTERNAL_ERROR", "WORKSPACE_PATH_ESCAPE", "SCHEMA_REJECTED"}
)
RESOURCE_KINDS = ("warnings", "failures", "rules", "active-context")
TOKEN = "preflight-token-placeholder-0001"


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
        "workspace_id": "ws_" + "0" * 24,
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

    Output fields are found from the models, so a new UserText field is
    covered without editing this test.  Each probe inherits the model-wide
    path check from ``WireModel``.
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
        if text != "\\\\d+" and text != "\\d+":
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
                        "trigger_type": "tag",
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
        await ok("session_brief", {})
        for kind in RESOURCE_KINDS:
            await client.read_resource(f"memory://workspaces/{ws}/{kind}")


# ---------------------------------------------------------------------------
# Mechanical coverage (R-26).


def _kind(annotation: object) -> object:
    """``"str"``, ``"json"``, a model class, ``("list", kind)`` or ``None``."""
    if annotation in (Cursor, RelativePath, HttpsUrl):
        return None
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        kinds = {_kind(item) for item in get_args(annotation) if item is not type(None)}
        return kinds.pop() if len(kinds) == 1 else None
    if origin is Annotated:
        base, *metadata = get_args(annotation)
        if any(getattr(item, "pattern", None) for item in metadata):
            return None
        return _kind(base)
    if origin in (list, set, frozenset):
        inner = _kind(get_args(annotation)[0])
        return None if inner is None else ("list", inner)
    if origin is Literal:
        return None
    if annotation is str:
        return "str"
    if annotation is JsonObject:
        return "json"
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    return None


def _free_text_paths(model: type[BaseModel], base: dict[str, Any], prefix=()):
    """Every free-text-typed field of ``model``, read from its annotations.

    Nested models are followed where ``base`` supplies them.
    """
    hints = get_type_hints(model, include_extras=True)
    for name in model.model_fields:
        kind = _kind(hints[name])
        if kind is None:
            continue
        if kind in ("str", "json") or kind == ("list", "str"):
            yield (*prefix, name), kind
        elif isinstance(kind, type) and isinstance(base.get(name), dict):
            yield from _free_text_paths(kind, base[name], (*prefix, name))
        elif (
            isinstance(kind, tuple)
            and isinstance(kind[1], type)
            and isinstance(base.get(name), list)
            and base[name]
        ):
            yield from _free_text_paths(kind[1], base[name][0], (*prefix, name, 0))


def _seeded(base: dict[str, Any], path: tuple, kind: object, text: str) -> dict:
    arguments = copy.deepcopy(base)
    target = arguments
    for step in path[:-1]:
        target = target[step]
    value: object = {"json": {"note": text, text: [text]}, "str": text}.get(
        kind,
        [text],  # type: ignore[arg-type]
    )
    target[path[-1]] = value
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

    A path must not change the outcome into a refusal: each seeded call
    either matches the same call with neutral text or is not a refusal.
    """
    base = _own_read_arguments(arguments)
    labels = []
    for path, kind in _free_text_paths(TOOL_INPUT_MODELS[tool], base):
        label = f"{tool}.{'.'.join(map(str, path))}"
        labels.append(label)
        neutral = await _target_call(
            invoke,
            workspace_id,
            tool,
            _fresh_keys(_seeded(base, path, kind, NEUTRAL), label + NEUTRAL),
        )
        for text in SEEDS:
            response = await _target_call(
                invoke,
                workspace_id,
                tool,
                _fresh_keys(_seeded(base, path, kind, text), label + text),
            )
            code = _code(response)
            assert code == _code(neutral) or code not in REFUSALS, (
                label,
                text,
                response["error"],
                _code(neutral),
            )
    return labels


async def test_every_free_text_field_accepts_paths_and_every_read_survives(tmp_path):
    """Seed every free-text input field and DB row with paths; read everything.

    An input field typed as free text but not ``UserText`` refuses the seed
    (``INVALID_ARGUMENT``), and an output field carrying it fails the read
    (``INTERNAL_ERROR``); either fails this test.
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
        writes = {
            **_sweep_arguments(seed),
            "workspace_link": {"linked_workspace_id": b},
        }
        for tool, arguments in sorted(writes.items()):
            seeded += await _seed_free_text(invoke, a, tool, arguments)

    # Rows as a migrated v6 database or a pre-fix write left them.
    _insert_rows(a_root, a, SEEDS)

    async with Client(_server((a_root, b_root))) as client:
        invoke = _invoker(client)
        assert (await invoke("session_brief", {"workspace_id": a}))["ok"]
        await _mint_tokens(invoke, a, b, seed)
        entities = await invoke("entity_list", {"workspace_id": a, "limit": 100})
        assert set(SEEDS) <= {item["name"] for item in entities["data"]["items"]}
        seed.ids["entity"] = entities["data"]["items"][0]["entity_id"]
        communities = await invoke("community_list", {"workspace_id": a})
        if communities["ok"] and communities["data"]["items"]:
            seed.ids["community"] = communities["data"]["items"][0]["community_id"]
        failures: dict[str, str] = {}
        for tool, arguments in sorted(_sweep_arguments(seed).items()):
            if _is_protected(tool) or tool in CROSS_WORKSPACE_TOOLS:
                continue
            plain = _fresh_keys(_own_read_arguments(arguments), tool)
            response = await invoke(tool, {"workspace_id": a, **plain})
            if _code(response) != "ok" and _code(response) not in GATE_CODES:
                failures[tool] = _code(response)
            # Query-like fields searching for the paths.
            await _seed_free_text(invoke, a, tool, arguments)
        for kind in RESOURCE_KINDS:
            await client.read_resource(f"memory://workspaces/{a}/{kind}")
    # edit_preflight needs a native edit request, which only the edit hook makes.
    assert failures == {"edit_preflight": "NOT_FOUND"}, failures
    # Nested and cross-workspace inputs are reached too.
    assert {
        "memory_store.context",
        "memory_store_batch.records.0.content",
        "rule_update.patch.must_do",
        "workspace_link.label",
    } <= set(seeded), seeded
