"""IdleDreamScheduler -- monitors tool call activity and triggers dreaming.

The scheduler watches for idle periods (no MCP tool calls for a configurable
timeout), then triggers a dream callback. It uses time.monotonic() for
reliable idle detection, debounces via timestamp comparison, and yields
cooperatively via asyncio.Event when the user returns.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass  # Future type imports for dream strategies

logger = logging.getLogger(__name__)


class IdleDreamScheduler:
    """Monitors tool call activity and triggers dreaming during idle periods.

    The scheduler runs a background asyncio.Task that polls for idle timeout
    expiry. When the user has been idle for `idle_timeout` seconds, it invokes
    the registered dream callback. If the user returns (a tool call arrives),
    the `user_active` Event is set, signaling dream strategies to yield.

    Usage:
        scheduler = IdleDreamScheduler(idle_timeout=60.0)
        scheduler.set_dream_callback(my_dream_handler)
        await scheduler.start()

        # On each tool call:
        scheduler.notify_tool_call()

        # Shutdown:
        await scheduler.stop()
    """

    def __init__(self, idle_timeout: float = 60.0, enabled: bool = True):
        """Initialize the scheduler.

        Args:
            idle_timeout: Seconds of inactivity before dreaming starts.
            enabled: Master switch. If False, start() is a no-op.
        """
        self._idle_timeout = idle_timeout
        self._enabled = enabled
        self._last_tool_call: float = time.monotonic()
        self._user_active: asyncio.Event = asyncio.Event()
        self._user_active.set()  # User starts as active
        self._monitor_task: asyncio.Task | None = None
        self._running: bool = False
        self._is_dreaming: bool = False
        self._dream_callback: Callable[[IdleDreamScheduler], Awaitable[None]] | None = (
            None
        )

    def set_dream_callback(
        self,
        callback: Callable[["IdleDreamScheduler"], Awaitable[None]],
    ) -> None:
        """Set the callback invoked when idle timeout triggers dreaming.

        The callback receives the scheduler instance so strategies can
        check user_active for cooperative yielding.

        Args:
            callback: Async callable that performs dream processing.
        """
        self._dream_callback = callback

    def notify_tool_call(self) -> None:
        """Notify the scheduler that a tool call occurred.

        Resets the idle timer (debounce) and signals dreaming to yield
        if currently active. Must be synchronous -- called from middleware.

        Also auto-starts the scheduler on first tool call if enabled but
        not yet running (handles startup race condition where the event
        loop was not yet available when the scheduler was created).
        """
        self._last_tool_call = time.monotonic()
        if self._is_dreaming:
            self._user_active.set()

        # Auto-start if enabled but not yet running (startup race condition)
        if self._enabled and not self._running and self._monitor_task is None:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.start())
            except RuntimeError:
                pass  # No event loop yet

    async def start(self) -> None:
        """Start the idle monitoring loop.

        If disabled, logs an info message and returns without starting.
        """
        if not self._enabled:
            logger.info("IdleDreamScheduler disabled, not starting")
            return

        self._running = True
        self._last_tool_call = time.monotonic()
        self._monitor_task = asyncio.create_task(self._idle_monitor_loop())
        logger.info(
            "IdleDreamScheduler started (idle_timeout=%.1fs)", self._idle_timeout
        )

    async def stop(self) -> None:
        """Stop the scheduler gracefully.

        Cancels the monitor task and waits for it to finish.
        """
        self._running = False
        self._user_active.set()  # Unblock any waiting

        if self._monitor_task is not None:
            self._monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._monitor_task
            self._monitor_task = None

        logger.info("IdleDreamScheduler stopped")

    async def _idle_monitor_loop(self) -> None:
        """Main loop: wait for idle timeout, then dream, repeat."""
        try:
            while self._running:
                # Inner loop: wait until idle timeout expires
                while self._running:
                    elapsed = time.monotonic() - self._last_tool_call
                    remaining = self._idle_timeout - elapsed
                    if remaining <= 0:
                        break
                    # Poll with 1s granularity to auto-correct drift
                    await asyncio.sleep(min(remaining, 1.0))

                if not self._running:
                    break

                # Idle timeout expired -- enter dreaming
                self._user_active.clear()
                self._is_dreaming = True

                try:
                    if self._dream_callback is not None:
                        logger.debug("Idle timeout reached, starting dream session")
                        await self._dream_callback(self)
                    else:
                        logger.debug("No dream callback set, skipping dream session")
                except Exception as e:
                    logger.error("Dream session error: %s", e, exc_info=True)
                finally:
                    self._is_dreaming = False
                    self._user_active.set()

                # After dreaming ends, re-enter idle wait (outer loop continues)
                # Reset tool call time so we wait a full timeout again
                self._last_tool_call = time.monotonic()

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(
                "IdleDreamScheduler monitor loop crashed: %s", e, exc_info=True
            )

    @property
    def is_dreaming(self) -> bool:
        """True if currently executing a dream session."""
        return self._is_dreaming

    @property
    def is_running(self) -> bool:
        """True if the scheduler is running."""
        return self._running

    @property
    def user_active(self) -> asyncio.Event:
        """Event that is SET when the user is active (tool call arrived).

        Dream strategies should check this at yield points:
            if scheduler.user_active.is_set():
                break  # User returned, stop dreaming
        """
        return self._user_active
