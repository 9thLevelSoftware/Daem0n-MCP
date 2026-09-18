"""Production lifecycle must quiesce writers before closing their dependencies."""

from __future__ import annotations

import asyncio

import pytest

from daem0nmcp.api.v7.production import _runtime_lifespan


@pytest.mark.asyncio
async def test_shutdown_drains_background_work_before_closing_its_dependency():
    state = {"open": False, "written": False}
    release = asyncio.Event()

    class Database:
        def start(self):
            state["open"] = True

        def close(self):
            assert state["written"]
            state["open"] = False

    class Worker:
        async def start(self):
            async def write():
                await release.wait()
                assert state["open"]
                state["written"] = True

            self.task = asyncio.create_task(write())

        async def aclose(self):
            release.set()
            await self.task

    async with _runtime_lifespan((Database(), Worker()))(object()):
        assert state["open"]
    assert state == {"open": False, "written": True}


@pytest.mark.asyncio
async def test_failed_start_and_failed_close_still_release_other_resources():
    closed = []

    class Resource:
        def close(self):
            closed.append("resource")

    class Broken:
        async def start(self):
            raise ValueError("start failed")

        async def aclose(self):
            closed.append("broken")
            raise RuntimeError("close failed")

    with pytest.raises(RuntimeError, match="close failed") as caught:
        async with _runtime_lifespan((Resource(), Broken()))(object()):
            pytest.fail("startup failure must prevent serving")
    assert isinstance(caught.value.__context__, ValueError)
    assert closed == ["broken", "resource"]
