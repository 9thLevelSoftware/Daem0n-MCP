"""
Simple TTL cache for hot path optimization.

Provides a lightweight in-memory cache with automatic expiration.
No external dependencies required.
"""

import logging
import time
from collections.abc import Hashable
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)


class TTLCache:
    """
    A simple thread-safe TTL (Time-To-Live) cache.

    Entries expire after `ttl` seconds and are lazily cleaned up.
    Uses a simple dict with timestamp tracking.

    Attributes:
        ttl: Time-to-live in seconds for cache entries
        maxsize: Maximum number of entries (LRU eviction when exceeded)
    """

    def __init__(self, ttl: float = 5.0, maxsize: int = 100):
        """
        Initialize the cache.

        Args:
            ttl: Time-to-live in seconds (default: 5 seconds)
            maxsize: Maximum cache size (default: 100 entries)
        """
        self.ttl = ttl
        self.maxsize = maxsize
        self._cache: dict[Hashable, tuple[float, Any]] = {}
        self._lock = Lock()
        self._hits = 0
        self._misses = 0

    def get(self, key: Hashable) -> tuple[bool, Any]:
        """
        Get a value from the cache.

        Args:
            key: The cache key (must be hashable)

        Returns:
            Tuple of (found, value). If not found or expired, returns (False, None).
        """
        with self._lock:
            if key not in self._cache:
                self._misses += 1
                return False, None

            timestamp, value = self._cache[key]
            if time.time() - timestamp > self.ttl:
                # Expired
                del self._cache[key]
                self._misses += 1
                return False, None

            self._hits += 1
            return True, value

    def set(self, key: Hashable, value: Any) -> None:
        """
        Set a value in the cache.

        Args:
            key: The cache key (must be hashable)
            value: The value to cache
        """
        with self._lock:
            # Evict expired entries if at capacity
            if len(self._cache) >= self.maxsize:
                self._evict_expired()

            # If still at capacity, evict oldest
            if len(self._cache) >= self.maxsize:
                self._evict_oldest()

            self._cache[key] = (time.time(), value)

    def invalidate(self, key: Hashable) -> bool:
        """
        Remove a specific key from the cache.

        Args:
            key: The cache key to remove

        Returns:
            True if key was present and removed, False otherwise
        """
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                return True
            return False

    def clear(self) -> int:
        """
        Clear all entries from the cache.

        Returns:
            Number of entries cleared
        """
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
            return count

    def _evict_expired(self) -> int:
        """Evict all expired entries. Must be called with lock held."""
        now = time.time()
        expired = [k for k, (ts, _) in self._cache.items() if now - ts > self.ttl]
        for k in expired:
            del self._cache[k]
        return len(expired)

    def _evict_oldest(self) -> None:
        """Evict the oldest entry. Must be called with lock held."""
        if not self._cache:
            return
        oldest_key = min(self._cache.keys(), key=lambda k: self._cache[k][0])
        del self._cache[oldest_key]

    @property
    def stats(self) -> dict[str, Any]:
        """Get cache statistics."""
        with self._lock:
            total = self._hits + self._misses
            hit_rate = self._hits / total if total > 0 else 0.0
            return {
                "size": len(self._cache),
                "maxsize": self.maxsize,
                "ttl": self.ttl,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": hit_rate,
            }

    def __len__(self) -> int:
        """Return current cache size."""
        with self._lock:
            return len(self._cache)


def make_cache_key(*args, **kwargs) -> tuple:
    """
    Create a hashable cache key from arguments.

    Args:
        *args: Positional arguments
        **kwargs: Keyword arguments

    Returns:
        A hashable tuple suitable for use as a cache key
    """

    # Convert lists to tuples for hashability
    def make_hashable(obj):
        if isinstance(obj, list):
            return tuple(make_hashable(x) for x in obj)
        if isinstance(obj, dict):
            return tuple(sorted((k, make_hashable(v)) for k, v in obj.items()))
        if isinstance(obj, set):
            return frozenset(make_hashable(x) for x in obj)
        return obj

    hashable_args = tuple(make_hashable(a) for a in args)
    hashable_kwargs = tuple(sorted((k, make_hashable(v)) for k, v in kwargs.items()))

    return (hashable_args, hashable_kwargs)


# Global caches for hot paths
_recall_cache = TTLCache(ttl=5.0, maxsize=50)
_rules_cache = TTLCache(ttl=5.0, maxsize=50)


def get_recall_cache() -> TTLCache:
    """Get the global recall cache."""
    return _recall_cache


def get_rules_cache() -> TTLCache:
    """Get the global rules cache."""
    return _rules_cache


def clear_all_caches() -> dict[str, int]:
    """
    Clear all global caches.

    Returns:
        Dict with cache names and number of entries cleared
    """
    return {"recall": _recall_cache.clear(), "rules": _rules_cache.clear()}
