"""
Production-grade Thread-Safe LRU Cache with TTL Expiration and Metric Telemetry.

Module: app1.py
Author: Principal Software Engineer & AI Systems Architect
Safety: Thread-safe, sanitizes inputs, zero external dependencies, non-blocking stats.
"""

from __future__ import annotations

import collections
import dataclasses
import logging
import sys
import threading
import time
from typing import Any, Callable, Dict, Generic, List, Optional, Tuple, TypeVar

# Configure structured enterprise logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(threadName)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("CacheEngine")

K = TypeVar("K")
V = TypeVar("V")


class CacheException(Exception):
    """Base exception for cache operations."""


class InvalidCacheParameterError(CacheException):
    """Raised when configuration parameters fail validation."""


@dataclasses.dataclass(frozen=True)
class CacheItem(Generic[V]):
    """Immutable envelope for cached values with timestamp metadata."""

    value: V
    expires_at: Optional[float]

    def is_expired(self, current_time: float) -> bool:
        """Evaluate if the cached item has surpassed its expiration deadline."""
        return self.expires_at is not None and current_time >= self.expires_at


@dataclasses.dataclass
class CacheMetrics:
    """Telemetry counters for cache performance auditing."""

    hits: int = 0
    misses: int = 0
    evictions: int = 0
    expirations: int = 0

    @property
    def hit_ratio(self) -> float:
        """Calculate hit ratio safely avoiding division by zero."""
        total = self.hits + self.misses
        return (self.hits / total) if total > 0 else 0.0


class ThreadSafeLRUCache(Generic[K, V]):
    """
    High-concurrency, memory-bounded Least Recently Used (LRU) Cache.
    
    Includes absolute Time-To-Live (TTL) eviction, thread-safe synchronization
    via re-entrant locks, and automated background maintenance routines.
    """

    def __init__(
        self,
        capacity: int = 1024,
        default_ttl: Optional[float] = None,
        cleanup_interval_seconds: float = 60.0,
    ) -> None:
        if capacity <= 0:
            raise InvalidCacheParameterError(f"Capacity must be > 0. Received: {capacity}")
        if default_ttl is not None and default_ttl <= 0:
            raise InvalidCacheParameterError(f"TTL must be positive. Received: {default_ttl}")
        if cleanup_interval_seconds <= 0:
            raise InvalidCacheParameterError("Cleanup interval must be positive.")

        self._capacity: int = capacity
        self._default_ttl: Optional[float] = default_ttl
        self._cleanup_interval: float = cleanup_interval_seconds

        self._store: collections.OrderedDict[K, CacheItem[V]] = collections.OrderedDict()
        self._lock: threading.RLock = threading.RLock()
        self._metrics: CacheMetrics = CacheMetrics()

        self._is_active: bool = True
        self._reaper_thread: threading.Thread = threading.Thread(
            target=self._eviction_reaper,
            name="CacheReaperWorker",
            daemon=True,
        )
        self._reaper_thread.start()
        logger.info("Initialized CacheEngine with capacity=%d, TTL=%s", capacity, default_ttl)

    def get(self, key: K, default: Optional[V] = None) -> Optional[V]:
        """Retrieve value by key, updating internal access recency."""
        with self._lock:
            if key not in self._store:
                self._metrics.misses += 1
                return default

            item = self._store[key]
            now = time.monotonic()

            if item.is_expired(now):
                self._metrics.expirations += 1
                self._metrics.misses += 1
                del self._store[key]
                return default

            # Move to end to denote most recently used status
            self._store.move_to_end(key, last=True)
            self._metrics.hits += 1
            return item.value

    def put(self, key: K, value: V, ttl: Optional[float] = None) -> None:
        """Store or update key-value pair with bounded eviction."""
        effective_ttl = ttl if ttl is not None else self._default_ttl
        if effective_ttl is not None and effective_ttl <= 0:
            raise InvalidCacheParameterError("Item TTL must be strictly positive.")

        now = time.monotonic()
        expires_at = (now + effective_ttl) if effective_ttl is not None else None
        new_item = CacheItem(value=value, expires_at=expires_at)

        with self._lock:
            if key in self._store:
                self._store.move_to_end(key, last=True)
            self._store[key] = new_item

            if len(self._store) > self._capacity:
                # Evict oldest entry (FIFO relative to accesses)
                evicted_key, _ = self._store.popitem(last=False)
                self._metrics.evictions += 1
                logger.debug("Evicted key '%s' due to capacity limits.", evicted_key)

    def delete(self, key: K) -> bool:
        """Explicitly remove an entry from cache."""
        with self._lock:
            if key in self._store:
                del self._store[key]
                return True
            return False

    def clear(self) -> None:
        """Purge all entries immediately."""
        with self._lock:
            self._store.clear()
            logger.info("Cache successfully flushed.")

    def sweep_expired(self) -> int:
        """Scan and clear all expired entries. Returns count of collected items."""
        now = time.monotonic()
        purged = 0
        with self._lock:
            for k in list(self._store.keys()):
                item = self._store[k]
                if item.is_expired(now):
                    del self._store[k]
                    self._metrics.expirations += 1
                    purged += 1
        return purged

    def get_telemetry(self) -> Dict[str, Any]:
        """Export snapshot of operational metrics without state mutation."""
        with self._lock:
            return {
                "size": len(self._store),
                "capacity": self._capacity,
                "hits": self._metrics.hits,
                "misses": self._metrics.misses,
                "hit_ratio": round(self._metrics.hit_ratio, 4),
                "evictions": self._metrics.evictions,
                "expirations": self._metrics.expirations,
            }

    def _eviction_reaper(self) -> None:
        """Background maintenance thread that periodically evicts expired nodes."""
        while self._is_active:
            time.sleep(self._cleanup_interval)
            try:
                purged = self.sweep_expired()
                if purged > 0:
                    logger.debug("Background sweep purged %d expired keys.", purged)
            except Exception as ex:  # Guard against thread termination
                logger.error("Error during scheduled eviction cycle: %s", ex)

    def shutdown(self) -> None:
        """Gracefully terminate background background workers."""
        self._is_active = False
        logger.info("Cache subsystem shutdown signaled.")


def _demo_worker(cache: ThreadSafeLRUCache[str, int], worker_id: int) -> None:
    """Simulate concurrent reader/writer workload for validation."""
    for i in range(10):
        key = f"key_{i % 5}"
        cache.put(key, worker_id * 100 + i, ttl=0.5)
        _ = cache.get(key)
        time.sleep(0.01)


if __name__ == "__main__":
    logger.info("Bootstrapping demonstration harness...")
    engine: ThreadSafeLRUCache[str, int] = ThreadSafeLRUCache(
        capacity=5,
        default_ttl=1.0,
        cleanup_interval_seconds=1.0,
    )

    try:
        workers: List[threading.Thread] = []
        for index in range(4):
            t = threading.Thread(target=_demo_worker, args=(engine, index), name=f"Worker-{index}")
            workers.append(t)
            t.start()

        for t in workers:
            t.join()

        logger.info("Concurrent worker simulation finished.")
        telemetry = engine.get_telemetry()
        logger.info("Current Telemetry: %s", telemetry)

        # Demonstrate deterministic TTL behavior
        logger.info("Testing TTL expiration behavior...")
        engine.put("ephemeral_key", 42, ttl=0.2)
        logger.info("Immediate fetch: %s", engine.get("ephemeral_key"))
        time.sleep(0.3)
        logger.info("Post-delay fetch (expected None): %s", engine.get("ephemeral_key"))

        final_metrics = engine.get_telemetry()
        logger.info("Final Operational Metrics: %s", final_metrics)

    finally:
        engine.shutdown()
        logger.info("Execution harness concluded safely.")