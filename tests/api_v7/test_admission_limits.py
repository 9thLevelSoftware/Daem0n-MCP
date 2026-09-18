"""Concurrent admission remains bounded and cancellation releases capacity."""

import asyncio
from types import SimpleNamespace

import pytest

from daem0nmcp.api.v7.middleware import V7InvocationMiddleware
from daem0nmcp.covenant import InvocationScope


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
