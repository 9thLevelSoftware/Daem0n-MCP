"""Tests for IdleDreamScheduler -- idle detection, debounce, yield, lifecycle."""

import asyncio
import contextlib

import pytest

from daem0nmcp.dreaming.scheduler import IdleDreamScheduler


class TestSchedulerLifecycle:
    """Verify start/stop lifecycle management."""

    @pytest.mark.asyncio
    async def test_scheduler_start_stop(self):
        """Scheduler should report is_running correctly after start and stop."""
        scheduler = IdleDreamScheduler(idle_timeout=60.0)
        assert not scheduler.is_running

        await scheduler.start()
        assert scheduler.is_running

        await scheduler.stop()
        assert not scheduler.is_running

    @pytest.mark.asyncio
    async def test_scheduler_disabled(self):
        """Disabled scheduler should not start a monitor task."""
        scheduler = IdleDreamScheduler(idle_timeout=1.0, enabled=False)

        await scheduler.start()
        assert scheduler._monitor_task is None
        assert not scheduler.is_running

        # Stop should be safe even when not started
        await scheduler.stop()


class TestIdleDetection:
    """Verify idle timeout triggers dreaming correctly."""

    @pytest.mark.asyncio
    async def test_idle_triggers_dreaming(self):
        """After idle timeout, dream callback should be invoked."""
        dream_triggered = asyncio.Event()

        async def dream_callback(sched):
            dream_triggered.set()

        scheduler = IdleDreamScheduler(idle_timeout=0.2)
        scheduler.set_dream_callback(dream_callback)

        await scheduler.start()
        try:
            # Wait for dreaming to trigger (with generous timeout)
            await asyncio.wait_for(dream_triggered.wait(), timeout=3.0)
            assert dream_triggered.is_set(), "Dream callback was not invoked"
        finally:
            await scheduler.stop()

    @pytest.mark.asyncio
    async def test_no_dream_when_not_idle(self):
        """Should not dream when idle timeout has not passed."""
        scheduler = IdleDreamScheduler(idle_timeout=60.0)
        dream_triggered = asyncio.Event()

        async def dream_callback(sched):
            dream_triggered.set()

        scheduler.set_dream_callback(dream_callback)

        await scheduler.start()
        try:
            scheduler.notify_tool_call()
            await asyncio.sleep(0.2)
            assert not scheduler.is_dreaming
            assert not dream_triggered.is_set()
        finally:
            await scheduler.stop()


class TestDebounce:
    """Verify tool call debouncing resets the idle timer."""

    @pytest.mark.asyncio
    async def test_notify_tool_call_resets_timer(self):
        """Rapid tool calls should debounce and prevent dreaming."""
        dream_triggered = asyncio.Event()

        async def dream_callback(sched):
            dream_triggered.set()

        scheduler = IdleDreamScheduler(idle_timeout=0.5)
        scheduler.set_dream_callback(dream_callback)

        await scheduler.start()
        try:
            # Call every 0.3s -- never reaching the 0.5s timeout
            scheduler.notify_tool_call()
            await asyncio.sleep(0.3)
            scheduler.notify_tool_call()
            await asyncio.sleep(0.3)
            # Only 0.3s since last call, should NOT be dreaming
            assert not scheduler.is_dreaming
            assert not dream_triggered.is_set()
        finally:
            await scheduler.stop()


class TestCooperativeYielding:
    """Verify user_active Event signals dreaming to yield."""

    @pytest.mark.asyncio
    async def test_user_active_event_set_on_tool_call(self):
        """Tool call during dreaming should set the user_active Event."""
        dream_started = asyncio.Event()

        async def long_dream(sched):
            dream_started.set()
            # Simulate a long dream that checks for yield
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(10)

        scheduler = IdleDreamScheduler(idle_timeout=0.1)
        scheduler.set_dream_callback(long_dream)

        await scheduler.start()
        try:
            # Wait for dreaming to begin
            await asyncio.wait_for(dream_started.wait(), timeout=3.0)
            assert scheduler.is_dreaming

            # User returns -- notify tool call
            scheduler.notify_tool_call()
            assert scheduler.user_active.is_set(), (
                "user_active should be set after notify_tool_call during dreaming"
            )
        finally:
            await scheduler.stop()

    @pytest.mark.asyncio
    async def test_dreaming_yields_on_notify(self):
        """Dream callback should be able to detect user return and stop."""
        progress = []

        async def yielding_dream(sched):
            progress.append("started")
            for _ in range(50):
                if sched.user_active.is_set():
                    progress.append("yielded")
                    break
                progress.append("step")
                await asyncio.sleep(0.05)

        scheduler = IdleDreamScheduler(idle_timeout=0.1)
        scheduler.set_dream_callback(yielding_dream)

        await scheduler.start()
        try:
            # Wait for dream to start
            for _ in range(100):
                if "started" in progress:
                    break
                await asyncio.sleep(0.02)

            assert "started" in progress, "Dream should have started"

            # Let a few steps happen
            await asyncio.sleep(0.1)

            # User returns
            scheduler.notify_tool_call()

            # Wait for dream to notice and yield
            await asyncio.sleep(0.2)

            assert "yielded" in progress, "Dream should have yielded on user return"
        finally:
            await scheduler.stop()

        assert not scheduler.is_dreaming


class TestErrorResilience:
    """Verify the scheduler survives errors and edge cases."""

    @pytest.mark.asyncio
    async def test_dream_callback_exception_does_not_crash(self):
        """An exception in the dream callback should not stop the scheduler."""
        call_count = 0

        async def crashing_dream(sched):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("Dream exploded!")

        scheduler = IdleDreamScheduler(idle_timeout=0.1)
        scheduler.set_dream_callback(crashing_dream)

        await scheduler.start()
        try:
            # Wait enough time for at least one dream cycle to fail
            await asyncio.sleep(0.5)
            assert scheduler.is_running, (
                "Scheduler should still be running after callback exception"
            )
            assert call_count >= 1, "Callback should have been called at least once"
        finally:
            await scheduler.stop()

    @pytest.mark.asyncio
    async def test_stop_during_dreaming(self):
        """Stopping the scheduler while dreaming should be graceful."""
        dream_started = asyncio.Event()

        async def blocking_dream(sched):
            dream_started.set()
            # Simulate a dream that blocks for a while
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(10)

        scheduler = IdleDreamScheduler(idle_timeout=0.1)
        scheduler.set_dream_callback(blocking_dream)

        await scheduler.start()

        # Wait for dreaming to start
        await asyncio.wait_for(dream_started.wait(), timeout=3.0)
        assert scheduler.is_dreaming

        # Stop should not hang
        await asyncio.wait_for(scheduler.stop(), timeout=3.0)
        assert not scheduler.is_running
