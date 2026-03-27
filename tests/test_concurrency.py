"""Tests for concurrent access to Daem0n-MCP resources.

Tests race conditions, concurrent context access, and thread safety
of caching and memory operations.
"""

import asyncio
import shutil
import tempfile

import pytest

from daem0nmcp.cache import TTLCache, get_recall_cache, get_rules_cache
from daem0nmcp.database import DatabaseManager
from daem0nmcp.memory import MemoryManager
from daem0nmcp.rules import RulesEngine


@pytest.fixture
def temp_storage():
    """Create a temporary storage directory."""
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    shutil.rmtree(temp_dir)


@pytest.fixture
async def db_manager(temp_storage):
    """Create a database manager with temporary storage."""
    db = DatabaseManager(temp_storage)
    await db.init_db()
    yield db
    await db.close()


@pytest.fixture
async def memory_manager(db_manager):
    """Create a memory manager."""
    manager = MemoryManager(db_manager)
    yield manager
    # Close Qdrant before cleanup to release file locks on Windows
    if manager._qdrant:
        manager._qdrant.close()


@pytest.fixture
async def rules_engine(db_manager):
    """Create a rules engine."""
    return RulesEngine(db_manager)


class TestConcurrentMemoryAccess:
    """Test concurrent memory operations."""

    @pytest.mark.asyncio
    async def test_concurrent_remember(self, memory_manager):
        """Test that concurrent remember operations don't conflict."""

        async def create_memory(index: int):
            return await memory_manager.remember(
                category="decision",
                content=f"Concurrent decision number {index} for testing",
                tags=[f"test_{index}"],
            )

        # Create 10 memories concurrently
        tasks = [create_memory(i) for i in range(10)]
        results = await asyncio.gather(*tasks)

        # All should succeed
        assert len(results) == 10
        assert all("id" in r for r in results)

        # All IDs should be unique
        ids = [r["id"] for r in results]
        assert len(set(ids)) == 10

    @pytest.mark.asyncio
    async def test_concurrent_recall(self, memory_manager):
        """Test that concurrent recall operations work correctly."""
        # Create some memories first
        for i in range(5):
            await memory_manager.remember(
                category="pattern",
                content=f"Pattern for concurrent recall test {i}",
                tags=["concurrent"],
            )

        async def recall_memory():
            return await memory_manager.recall("concurrent recall test")

        # Perform 10 concurrent recalls
        tasks = [recall_memory() for _ in range(10)]
        results = await asyncio.gather(*tasks)

        # All should return valid results
        assert len(results) == 10
        assert all("found" in r for r in results)
        assert all(r["found"] > 0 for r in results)

    @pytest.mark.asyncio
    async def test_concurrent_remember_and_recall(self, memory_manager):
        """Test interleaved remember and recall operations."""
        get_recall_cache().clear()

        async def create_and_recall(index: int):
            # Create a memory
            await memory_manager.remember(
                category="learning",
                content=f"Interleaved test memory {index}",
                tags=["interleaved"],
            )

            # Immediately try to recall
            result = await memory_manager.recall("interleaved test memory")
            return result

        # Run 5 concurrent create-and-recall operations
        tasks = [create_and_recall(i) for i in range(5)]
        results = await asyncio.gather(*tasks)

        # All recalls should succeed (though may not see all memories due to timing)
        assert len(results) == 5
        assert all("found" in r for r in results)

    @pytest.mark.asyncio
    async def test_concurrent_record_outcome(self, memory_manager):
        """Test concurrent outcome recording."""
        # Create memories first
        memory_ids = []
        for i in range(5):
            result = await memory_manager.remember(
                category="decision", content=f"Decision for outcome test {i}"
            )
            memory_ids.append(result["id"])

        async def record_outcome(mem_id: int, worked: bool):
            return await memory_manager.record_outcome(
                mem_id, f"Outcome for {mem_id}", worked=worked
            )

        # Record outcomes concurrently (alternating worked/failed)
        tasks = [
            record_outcome(mem_id, i % 2 == 0) for i, mem_id in enumerate(memory_ids)
        ]
        results = await asyncio.gather(*tasks)

        # All should succeed
        assert len(results) == 5
        assert all("id" in r for r in results)


class TestConcurrentRulesAccess:
    """Test concurrent rules operations."""

    @pytest.mark.asyncio
    async def test_concurrent_add_rule(self, rules_engine):
        """Test that concurrent rule additions don't conflict."""

        async def add_rule(index: int):
            return await rules_engine.add_rule(
                trigger=f"Concurrent rule trigger {index}",
                must_do=[f"Action {index}"],
                priority=index,
            )

        # Add 10 rules concurrently
        tasks = [add_rule(i) for i in range(10)]
        results = await asyncio.gather(*tasks)

        # All should succeed
        assert len(results) == 10
        assert all("id" in r for r in results)

        # All IDs should be unique
        ids = [r["id"] for r in results]
        assert len(set(ids)) == 10

    @pytest.mark.asyncio
    async def test_concurrent_check_rules(self, rules_engine):
        """Test that concurrent check_rules operations work correctly."""
        # Create some rules first
        for i in range(5):
            await rules_engine.add_rule(
                trigger=f"Concurrent check trigger {i}", must_do=[f"Action {i}"]
            )

        get_rules_cache().clear()

        async def check_rule():
            return await rules_engine.check_rules("concurrent check trigger")

        # Perform 10 concurrent checks
        tasks = [check_rule() for _ in range(10)]
        results = await asyncio.gather(*tasks)

        # All should return valid results
        assert len(results) == 10
        assert all("action" in r for r in results)

    @pytest.mark.asyncio
    async def test_concurrent_add_and_check_rules(self, rules_engine):
        """Test interleaved add_rule and check_rules operations."""
        get_rules_cache().clear()

        async def add_and_check(index: int):
            # Add a rule
            await rules_engine.add_rule(
                trigger=f"Interleaved rule {index}", must_do=[f"Action {index}"]
            )

            # Immediately check
            result = await rules_engine.check_rules("interleaved rule")
            return result

        # Run 5 concurrent add-and-check operations
        tasks = [add_and_check(i) for i in range(5)]
        results = await asyncio.gather(*tasks)

        # All checks should succeed
        assert len(results) == 5
        assert all("action" in r for r in results)


class TestCacheConcurrency:
    """Test thread safety of the TTL cache."""

    def test_cache_concurrent_set_get(self):
        """Test concurrent set and get operations on cache."""
        import threading

        cache = TTLCache(ttl=5.0, maxsize=100)
        errors = []

        def worker(worker_id: int, iterations: int):
            try:
                for i in range(iterations):
                    key = f"worker_{worker_id}_key_{i}"
                    cache.set(key, f"value_{i}")
                    found, value = cache.get(key)
                    if found and value != f"value_{i}":
                        errors.append(f"Value mismatch for {key}")
            except Exception as e:
                errors.append(str(e))

        # Start multiple threads
        threads = [threading.Thread(target=worker, args=(i, 100)) for i in range(10)]

        for t in threads:
            t.start()

        for t in threads:
            t.join()

        # No errors should have occurred
        assert len(errors) == 0

    def test_cache_concurrent_clear(self):
        """Test concurrent clear operations."""
        import threading

        cache = TTLCache(ttl=5.0, maxsize=100)
        errors = []

        def setter(iterations: int):
            try:
                for i in range(iterations):
                    cache.set(f"key_{i}", f"value_{i}")
            except Exception as e:
                errors.append(str(e))

        def clearer(iterations: int):
            try:
                for _ in range(iterations):
                    cache.clear()
            except Exception as e:
                errors.append(str(e))

        # Start setter and clearer threads
        threads = [
            threading.Thread(target=setter, args=(100,)),
            threading.Thread(target=clearer, args=(50,)),
            threading.Thread(target=setter, args=(100,)),
            threading.Thread(target=clearer, args=(50,)),
        ]

        for t in threads:
            t.start()

        for t in threads:
            t.join()

        # No errors should have occurred
        assert len(errors) == 0

    def test_cache_concurrent_invalidate(self):
        """Test concurrent invalidate operations."""
        import threading

        cache = TTLCache(ttl=5.0, maxsize=100)
        errors = []

        # Pre-populate cache
        for i in range(100):
            cache.set(f"key_{i}", f"value_{i}")

        def invalidator(start: int, end: int):
            try:
                for i in range(start, end):
                    cache.invalidate(f"key_{i}")
            except Exception as e:
                errors.append(str(e))

        # Start multiple invalidator threads with overlapping ranges
        threads = [
            threading.Thread(target=invalidator, args=(0, 50)),
            threading.Thread(target=invalidator, args=(25, 75)),
            threading.Thread(target=invalidator, args=(50, 100)),
        ]

        for t in threads:
            t.start()

        for t in threads:
            t.join()

        # No errors should have occurred
        assert len(errors) == 0


class TestDatabaseConcurrency:
    """Test database-level concurrency."""

    @pytest.mark.asyncio
    async def test_concurrent_sessions(self, db_manager):
        """Test that concurrent database sessions work correctly."""

        async def use_session(index: int):
            async with db_manager.get_session():
                # Simulate some work
                await asyncio.sleep(0.01)
                return index

        # Run 20 concurrent session operations
        tasks = [use_session(i) for i in range(20)]
        results = await asyncio.gather(*tasks)

        # All should complete
        assert len(results) == 20
        assert set(results) == set(range(20))

    @pytest.mark.asyncio
    async def test_concurrent_transaction_isolation(self, memory_manager):
        """Test that transactions are properly isolated."""
        errors = []

        async def transactional_operation(index: int):
            try:
                # Create a memory
                result = await memory_manager.remember(
                    category="decision", content=f"Transaction test {index}"
                )

                # Verify it was created
                if "id" not in result:
                    errors.append(f"No ID returned for {index}")

                return result
            except Exception as e:
                errors.append(f"Error in transaction {index}: {e}")
                raise

        # Run 20 concurrent transactions
        tasks = [transactional_operation(i) for i in range(20)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Check for exceptions
        exceptions = [r for r in results if isinstance(r, Exception)]
        assert len(exceptions) == 0, f"Exceptions occurred: {exceptions}"

        # No errors should have occurred
        assert len(errors) == 0
