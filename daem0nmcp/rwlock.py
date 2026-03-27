# daem0nmcp/rwlock.py
"""Async read-write lock for concurrent context access."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class RWLock:
    """
    Async read-write lock implementation.

    Multiple readers can hold the lock simultaneously.
    Writers have exclusive access.

    This implementation uses asyncio.Condition for coordination:
    - Readers wait while a writer is active
    - Writers wait while readers are active or another writer is active
    - When a writer releases, all waiting readers and one waiting writer are notified

    Example usage:
        lock = RWLock()

        # Multiple readers can access concurrently
        async with lock.read():
            data = await read_shared_resource()

        # Writer gets exclusive access
        async with lock.write():
            await modify_shared_resource()
    """

    def __init__(self):
        self._read_ready = asyncio.Condition()
        self._readers = 0
        self._writer = False

    @asynccontextmanager
    async def read(self) -> AsyncIterator[None]:
        """
        Acquire read lock.

        Multiple readers can hold the lock simultaneously.
        Blocks while a writer holds the lock.
        """
        async with self._read_ready:
            while self._writer:
                await self._read_ready.wait()
            self._readers += 1
        try:
            yield
        finally:
            async with self._read_ready:
                self._readers -= 1
                if self._readers == 0:
                    self._read_ready.notify_all()

    @asynccontextmanager
    async def write(self) -> AsyncIterator[None]:
        """
        Acquire write lock (exclusive).

        Only one writer can hold the lock.
        Blocks while any readers or another writer holds the lock.
        """
        async with self._read_ready:
            while self._writer or self._readers > 0:
                await self._read_ready.wait()
            self._writer = True
        try:
            yield
        finally:
            async with self._read_ready:
                self._writer = False
                self._read_ready.notify_all()

    @property
    def readers(self) -> int:
        """Current number of active readers."""
        return self._readers

    @property
    def writing(self) -> bool:
        """Whether a writer currently holds the lock."""
        return self._writer
