"""Bounded admission for dependency-free background worker pools."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from typing import Any


class BoundedWorkerBusyError(RuntimeError):
    """Raised when every worker slot is owned by unfinished work."""


class BoundedWorkerPool:
    """Run blocking operations without an unbounded executor queue."""

    def __init__(self, *, max_workers: int, thread_name_prefix: str) -> None:
        if max_workers <= 0:
            raise ValueError("max_workers must be positive")
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=thread_name_prefix,
        )
        self._slots = threading.BoundedSemaphore(max_workers)
        self._state_lock = threading.Lock()
        self._in_flight = 0

    @property
    def in_flight(self) -> int:
        """Return the number of admitted operations that have not finished."""
        with self._state_lock:
            return self._in_flight

    async def run(self, operation: Callable[[], Any]) -> Any:
        """Admit one operation, or reject immediately when the pool is full."""
        work = self._submit(operation)
        return await asyncio.wrap_future(work)

    async def run_with_cancellation_cleanup(
        self,
        operation: Callable[[], Any],
        *,
        request_cancel: Callable[[], Any],
    ) -> Any:
        """Request cancellation and drain admitted work before propagating it."""
        work = self._submit(operation)
        waiter = asyncio.wrap_future(work)
        try:
            return await asyncio.shield(waiter)
        except asyncio.CancelledError as cancellation:
            request_cancel()
            while not waiter.done():
                try:
                    await asyncio.shield(waiter)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            with suppress(BaseException):
                waiter.result()
            raise cancellation from None

    def _submit(self, operation: Callable[[], Any]) -> Future[Any]:
        """Admit and submit one operation while retaining its capacity slot."""
        if not self._slots.acquire(blocking=False):
            raise BoundedWorkerBusyError("Background worker capacity is unavailable")
        with self._state_lock:
            self._in_flight += 1

        try:
            work = self._executor.submit(operation)
        except BaseException:
            self._release_slot()
            raise

        # The concurrent future owns capacity until the underlying operation
        # truly ends. Cancelling its asyncio waiter must not over-admit work.
        work.add_done_callback(lambda _future: self._release_slot())
        return work

    def shutdown(self) -> None:
        """Shut down an explicitly owned pool after its workers have stopped."""
        self._executor.shutdown(wait=True, cancel_futures=True)

    def _release_slot(self) -> None:
        with self._state_lock:
            self._in_flight -= 1
        self._slots.release()
