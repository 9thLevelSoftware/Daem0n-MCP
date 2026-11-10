"""Concurrent admission remains bounded and cancellation releases capacity."""

import asyncio
from types import SimpleNamespace

import pytest
from mcp import McpError
from mcp.types import INVALID_PARAMS

from daem0nmcp.api.v7.middleware import (
    RESOURCE_NOT_FOUND,
    ResourceInvocationContextError,
    V7InvocationMiddleware,
)
from daem0nmcp.covenant import InvocationScope
from daem0nmcp.workspace import WorkspaceRegistry


@pytest.mark.parametrize("limit_kind", ["principal", "workspace", "global"])
async def test_scoped_admission_rejects_and_releases_after_cancellation(
    tmp_path, limit_kind
):
    middleware = V7InvocationMiddleware(
        gate=object(),
        workspace_resolver=SimpleNamespace(resolve=lambda _: None),
        transport_mode="stdio",
        max_inflight=1 if limit_kind == "global" else 10,
        max_inflight_per_principal=1 if limit_kind == "principal" else 10,
        max_inflight_per_workspace=1 if limit_kind == "workspace" else 10,
    )
    first = InvocationScope("alice", "s1", str(tmp_path / "alpha"))
    second = InvocationScope(
        "alice" if limit_kind == "principal" else "bob",
        "s2",
        str(tmp_path / ("alpha" if limit_kind == "workspace" else "beta")),
    )
    context = SimpleNamespace(message=SimpleNamespace(name="memory_recall"))
    started = asyncio.Event()

    async def pending(_context):
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(middleware._dispatch(context, pending, first))
    await asyncio.wait_for(started.wait(), 5)
    try:
        with pytest.raises(Exception, match="ADMISSION_LIMIT_REACHED"):
            await middleware._dispatch(context, lambda _: "must not run", second)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert (
        await middleware._dispatch(context, lambda _: "available", second)
        == "available"
    )
    assert middleware._inflight == 0
    assert middleware._principal_inflight == {}
    assert middleware._workspace_inflight == {}


@pytest.mark.parametrize(
    "method", ["tasks/get", "tasks/result", "tasks/cancel", "tasks/list"]
)
@pytest.mark.parametrize("limit_kind", ["global", "principal", "workspace"])
async def test_task_lifecycle_shares_tool_capacity(tmp_path, method, limit_kind):
    if method == "tasks/list" and limit_kind == "workspace":
        # Listing spans authorized workspaces and holds global/principal slots.
        return
    middleware = V7InvocationMiddleware(
        gate=object(),
        workspace_resolver=SimpleNamespace(resolve=lambda _: None),
        transport_mode="stdio",
        max_inflight=1 if limit_kind == "global" else 10,
        max_inflight_per_principal=1 if limit_kind == "principal" else 10,
        max_inflight_per_workspace=1 if limit_kind == "workspace" else 10,
    )

    async def identity(_context):
        return "alice", "session"

    middleware.identity = identity
    scope = InvocationScope("alice", "session", str(tmp_path))
    middleware.configure_task_scope_resolver(lambda *_: scope)
    context = SimpleNamespace(method=method, message=SimpleNamespace(taskId="opaque"))
    tool_context = SimpleNamespace(message=SimpleNamespace(name="memory_recall"))
    started = asyncio.Event()

    async def pending(_context):
        started.set()
        await asyncio.Event().wait()

    active = asyncio.create_task(middleware._dispatch(tool_context, pending, scope))
    await asyncio.wait_for(started.wait(), 5)
    try:
        with pytest.raises(Exception, match="ADMISSION_LIMIT_REACHED"):
            await middleware.on_request(context, lambda _: "must not enter")
    finally:
        active.cancel()
        with pytest.raises(asyncio.CancelledError):
            await active

    started.clear()
    waiter = asyncio.create_task(middleware.on_request(context, pending))
    await asyncio.wait_for(started.wait(), 5)
    try:
        with pytest.raises(Exception, match="ADMISSION_LIMIT_REACHED"):
            await middleware._dispatch(tool_context, lambda _: "must not enter", scope)
        with pytest.raises(Exception, match="ADMISSION_LIMIT_REACHED"):
            await middleware.on_request(context, lambda _: "must not enter")
    finally:
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    assert await middleware.on_request(context, lambda _: "available") == "available"
    assert middleware._inflight == 0
    assert middleware._principal_inflight == {}
    assert middleware._workspace_inflight == {}


async def test_unknown_task_lookup_is_admitted_and_releases_capacity(tmp_path):
    middleware = V7InvocationMiddleware(
        gate=object(),
        workspace_resolver=SimpleNamespace(resolve=lambda _: None),
        transport_mode="stdio",
        max_inflight=1,
    )

    async def identity(_context):
        return "alice", "session"

    middleware.identity = identity

    def unknown(*_args):
        assert middleware._inflight == 1
        assert middleware._principal_inflight == {"alice": 1}
        raise ValueError("Task not found.")

    middleware.configure_task_scope_resolver(unknown)
    context = SimpleNamespace(
        method="tasks/get", message=SimpleNamespace(taskId="unknown")
    )
    with pytest.raises(ValueError, match="Task not found"):
        await middleware.on_request(context, lambda _: "must not enter")
    assert middleware._inflight == 0
    assert middleware._principal_inflight == {}
    assert middleware._workspace_inflight == {}


def _resource_read_middleware(tmp_path, *, granted: bool):
    """A middleware with one registered workspace and one slot per principal."""

    root = tmp_path / "registered"
    root.mkdir()
    workspace = WorkspaceRegistry([root], default_root=root).default

    def resolve(workspace_id):
        if workspace_id != workspace.workspace_id:
            raise KeyError("unknown workspace")
        return workspace

    middleware = V7InvocationMiddleware(
        gate=SimpleNamespace(workspace_authorized=lambda _scope: granted),
        workspace_resolver=SimpleNamespace(resolve=resolve),
        transport_mode="stdio",
        process_principal="alice",
        max_inflight_per_principal=1,
    )
    middleware._stdio_session_id = "session"
    return middleware, workspace


def _resource_context(workspace_id: str):
    uri = f"memory://workspaces/{workspace_id}/warnings"
    return SimpleNamespace(message=SimpleNamespace(uri=uri))


async def _refusal(middleware, workspace_id: str, read) -> tuple[int, str]:
    with pytest.raises(McpError) as refused:
        await middleware.on_read_resource(_resource_context(workspace_id), read)
    return refused.value.error.code, refused.value.error.message


async def test_resource_read_over_the_limit_is_refused_with_a_protocol_code(tmp_path):
    middleware, workspace = _resource_read_middleware(tmp_path, granted=True)
    started = asyncio.Event()

    async def pending(_context):
        started.set()
        await asyncio.Event().wait()

    context = _resource_context(workspace.workspace_id)
    active = asyncio.create_task(middleware.on_read_resource(context, pending))
    await asyncio.wait_for(started.wait(), 5)
    try:
        refusal = await _refusal(
            middleware, workspace.workspace_id, lambda _: "must not run"
        )
    finally:
        active.cancel()
        with pytest.raises(asyncio.CancelledError):
            await active
    assert refusal == (
        INVALID_PARAMS,
        "ADMISSION_LIMIT_REACHED: retry after an active request completes",
    )
    assert await middleware.on_read_resource(context, lambda _: "read") == "read"
    assert middleware._inflight == 0
    assert middleware._principal_inflight == {}
    assert middleware._workspace_inflight == {}


@pytest.mark.parametrize("saturated", ["principal", "workspace", "nothing"])
async def test_resource_refusal_does_not_reveal_a_registered_workspace(
    tmp_path, saturated
):
    """A caller without a grant gets one answer for any workspace id.

    A full limit must not change that: a caller can fill its own
    per-principal limit at will, and the per-workspace bucket can only be
    full for a workspace that exists.
    """

    middleware, workspace = _resource_read_middleware(tmp_path, granted=False)
    unknown = "ws_" + "0" * 24
    assert unknown != workspace.workspace_id
    holder = {
        "principal": InvocationScope("alice", "other", str(tmp_path / "elsewhere")),
        "workspace": InvocationScope("bob", "other", str(workspace.root)),
        "nothing": None,
    }[saturated]
    if saturated == "workspace":
        middleware._max_inflight_per_workspace = 1
    started = asyncio.Event()

    async def pending(_context):
        started.set()
        await asyncio.Event().wait()

    async def ungranted_read(_context):
        # What the resource authorizer answers without a grant.
        raise ResourceInvocationContextError()

    tool_context = SimpleNamespace(message=SimpleNamespace(name="memory_recall"))
    active = None
    if holder is not None:
        active = asyncio.create_task(
            middleware._dispatch(tool_context, pending, holder)
        )
        await asyncio.wait_for(started.wait(), 5)
    try:
        registered = await _refusal(middleware, workspace.workspace_id, ungranted_read)
        absent = await _refusal(middleware, unknown, ungranted_read)
    finally:
        if active is not None:
            active.cancel()
            with pytest.raises(asyncio.CancelledError):
                await active
    assert registered == absent
    if saturated == "principal":
        assert absent[0] == INVALID_PARAMS
    else:
        assert absent == (RESOURCE_NOT_FOUND, "Resource unavailable")
    assert middleware._inflight == 0
    assert middleware._principal_inflight == {}
    assert middleware._workspace_inflight == {}


@pytest.mark.parametrize("saturated", ["principal", "workspace", "nothing"])
async def test_tool_admission_does_not_reveal_a_registered_workspace(
    tmp_path, saturated
):
    """The same rule for tool calls: a full limit answers every id alike."""

    middleware, workspace = _resource_read_middleware(tmp_path, granted=False)
    unknown = "ws_" + "0" * 24
    holder = {
        "principal": InvocationScope("alice", "other", str(tmp_path / "elsewhere")),
        "workspace": InvocationScope("bob", "other", str(workspace.root)),
        "nothing": None,
    }[saturated]
    if saturated == "workspace":
        middleware._max_inflight_per_workspace = 1
    started = asyncio.Event()

    async def pending(_context):
        started.set()
        await asyncio.Event().wait()

    async def outcome(workspace_id: str) -> str:
        context = SimpleNamespace(
            message=SimpleNamespace(
                name="memory_recall", arguments={"workspace_id": workspace_id}
            )
        )
        try:
            # The handler turns a caller without a grant away in its envelope.
            return await middleware.on_call_tool(context, lambda _: "handler refusal")
        except Exception as refused:
            return str(refused)

    tool_context = SimpleNamespace(message=SimpleNamespace(name="memory_recall"))
    active = None
    if holder is not None:
        active = asyncio.create_task(
            middleware._dispatch(tool_context, pending, holder)
        )
        await asyncio.wait_for(started.wait(), 5)
    try:
        registered = await outcome(workspace.workspace_id)
        absent = await outcome(unknown)
    finally:
        if active is not None:
            active.cancel()
            with pytest.raises(asyncio.CancelledError):
                await active
    assert registered == absent
    if saturated == "principal":
        assert absent.startswith("ADMISSION_LIMIT_REACHED")
    else:
        assert absent == "handler refusal"
    assert middleware._inflight == 0
    assert middleware._principal_inflight == {}
    assert middleware._workspace_inflight == {}
