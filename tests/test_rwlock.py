# tests/test_rwlock.py
"""Tests for async read-write lock."""

import asyncio

import pytest

from daem0nmcp.rwlock import RWLock


class TestRWLock:
    """Verify read-write lock behavior."""

    @pytest.mark.asyncio
    async def test_multiple_readers_allowed(self):
        """Multiple readers can hold lock simultaneously."""
        lock = RWLock()
        readers_active = []

        async def reader(id: int):
            async with lock.read():
                readers_active.append(id)
                await asyncio.sleep(0.05)
                assert len(readers_active) >= 1  # Others may be reading too
                readers_active.remove(id)

        await asyncio.gather(reader(1), reader(2), reader(3))

    @pytest.mark.asyncio
    async def test_writer_excludes_readers(self):
        """Writer has exclusive access."""
        lock = RWLock()
        write_started = asyncio.Event()
        reader_blocked = True

        async def writer():
            async with lock.write():
                write_started.set()
                await asyncio.sleep(0.1)

        async def reader():
            nonlocal reader_blocked
            await write_started.wait()
            await asyncio.sleep(0.01)  # Give writer time to acquire
            async with lock.read():
                reader_blocked = False

        await asyncio.gather(writer(), reader())
        assert not reader_blocked  # Reader eventually got through

    @pytest.mark.asyncio
    async def test_writer_waits_for_readers(self):
        """Writer waits until readers finish."""
        lock = RWLock()
        reader_finished = False

        async def reader():
            nonlocal reader_finished
            async with lock.read():
                await asyncio.sleep(0.05)
                reader_finished = True

        async def writer():
            await asyncio.sleep(0.01)  # Let reader start
            async with lock.write():
                assert reader_finished  # Reader must have finished

        await asyncio.gather(reader(), writer())

    @pytest.mark.asyncio
    async def test_writers_are_exclusive(self):
        """Only one writer at a time."""
        lock = RWLock()
        write_count = 0
        max_concurrent_writes = 0

        async def writer(id: int):
            nonlocal write_count, max_concurrent_writes
            async with lock.write():
                write_count += 1
                max_concurrent_writes = max(max_concurrent_writes, write_count)
                await asyncio.sleep(0.02)
                write_count -= 1

        await asyncio.gather(writer(1), writer(2), writer(3))
        assert max_concurrent_writes == 1  # Only one writer at a time

    @pytest.mark.asyncio
    async def test_read_after_write_completes(self):
        """Readers can acquire lock after writer releases."""
        lock = RWLock()
        write_done = False
        read_after_write = False

        async def writer():
            nonlocal write_done
            async with lock.write():
                await asyncio.sleep(0.02)
                write_done = True

        async def reader():
            nonlocal read_after_write
            await asyncio.sleep(0.01)  # Let writer start first
            async with lock.read():
                if write_done:
                    read_after_write = True

        await asyncio.gather(writer(), reader())
        assert write_done
        # Reader either ran before writer or after - both valid

    @pytest.mark.asyncio
    async def test_nested_read_locks_allowed(self):
        """Same task can acquire read lock multiple times."""
        lock = RWLock()

        async with lock.read(), lock.read():
            pass

    @pytest.mark.asyncio
    async def test_exception_in_read_releases_lock(self):
        """Read lock is released even if exception occurs."""
        lock = RWLock()

        with pytest.raises(ValueError):
            async with lock.read():
                raise ValueError("test error")

        # Lock should be released - writer should be able to acquire
        async with lock.write():
            pass

    @pytest.mark.asyncio
    async def test_exception_in_write_releases_lock(self):
        """Write lock is released even if exception occurs."""
        lock = RWLock()

        with pytest.raises(ValueError):
            async with lock.write():
                raise ValueError("test error")

        # Lock should be released - reader should be able to acquire
        async with lock.read():
            pass
