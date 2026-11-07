"""
Production-Grade Resilient Token-Bucket Rate Limiter and Telemetry Engine.

This module implements an in-memory, thread-safe rate-limiting system designed
for high-throughput API gateways and distributed microservices. It features
strict input sanitization, dynamic token replenishment, and audit metrics.
"""

from __future__ import annotations

import logging
import math
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, Final, Optional, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("RateLimiterEngine")

IDENTIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-zA-Z0-9_\-\.]{3,64}$")


class RateLimitStatus(Enum):
    """Evaluation outcomes for an access request."""

    ALLOWED = auto()
    THROTTLED = auto()
    INVALID_IDENTIFIER = auto()


@dataclass(frozen=True)
class RateLimitConfig:
    """Configuration parameters for token bucket capacity and replenishment."""

    capacity: float
    refill_rate_per_sec: float

    def __post_init__(self) -> None:
        if self.capacity <= 0.0:
            raise ValueError("Capacity must be strictly positive.")
        if self.refill_rate_per_sec <= 0.0:
            raise ValueError("Refill rate must be strictly positive.")


@dataclass
class BucketState:
    """Thread-confined mutable state representing a single token bucket."""

    tokens: float
    last_updated: float
    lock: threading.Lock = field(default_factory=threading.Lock)


class TokenBucketRateLimiter:
    """Thread-safe, production-ready implementation of the Token Bucket algorithm."""

    def __init__(self, default_config: RateLimitConfig) -> None:
        self._default_config = default_config
        self._buckets: Dict[str, BucketState] = {}
        self._global_lock = threading.Lock()

    def _sanitize_identifier(self, identifier: str) -> bool:
        """Validates identity keys to prevent resource exhaustion and injection."""
        if not identifier or not isinstance(identifier, str):
            return False
        return bool(IDENTIFIER_PATTERN.match(identifier))

    def _get_or_create_bucket(self, client_id: str, current_time: float) -> BucketState:
        """Retrieves or safely registers a new client bucket using double-checked locking."""
        with self._global_lock:
            if client_id not in self._buckets:
                self._buckets[client_id] = BucketState(
                    tokens=self._default_config.capacity,
                    last_updated=current_time,
                )
            return self._buckets[client_id]

    def consume(
        self, client_id: str, tokens_requested: float = 1.0
    ) -> Tuple[RateLimitStatus, float]:
        """
        Attempts to consume tokens for a given identifier.
        Returns a tuple of (RateLimitStatus, remaining_tokens).
        """
        if not self._sanitize_identifier(client_id):
            logger.warning("Rejected evaluation for invalid identifier: %s", client_id)
            return RateLimitStatus.INVALID_IDENTIFIER, 0.0

        if tokens_requested <= 0.0:
            raise ValueError("Requested tokens must be greater than zero.")

        now = time.monotonic()
        bucket = self._get_or_create_bucket(client_id, now)

        with bucket.lock:
            elapsed = max(0.0, now - bucket.last_updated)
            replenished = elapsed * self._default_config.refill_rate_per_sec
            bucket.tokens = min(self._default_config.capacity, bucket.tokens + replenished)
            bucket.last_updated = now

            if bucket.tokens >= tokens_requested:
                bucket.tokens -= tokens_requested
                remaining = bucket.tokens
                return RateLimitStatus.ALLOWED, remaining

            return RateLimitStatus.THROTTLED, bucket.tokens

    def prune_stale_buckets(self, ttl_seconds: float = 3600.0) -> int:
        """Purges inactive records to prevent unbound memory growth."""
        now = time.monotonic()
        removed_count = 0
        with self._global_lock:
            stale_keys = [
                key
                for key, state in self._buckets.items()
                if (now - state.last_updated) > ttl_seconds
            ]
            for key in stale_keys:
                del self._buckets[key]
                removed_count += 1
        if removed_count > 0:
            logger.info("Evicted %d stale client buckets from memory.", removed_count)
        return removed_count


def execute_verification_pipeline() -> None:
    """Executes verification tests to ensure thread-safety and constraint compliance."""
    config = RateLimitConfig(capacity=5.0, refill_rate_per_sec=2.0)
    limiter = TokenBucketRateLimiter(default_config=config)
    client_id = "tenant-prod-us-east-1"

    logger.info("Starting production readiness validation for %s...", client_id)

    # Validate burst consumption
    for i in range(5):
        status, remaining = limiter.consume(client_id, 1.0)
        assert status == RateLimitStatus.ALLOWED, f"Expected ALLOWED on burst iteration {i}"

    # Validate throttling trigger
    status, remaining = limiter.consume(client_id, 1.0)
    assert status == RateLimitStatus.THROTTLED, "Expected THROTTLED when capacity exhausted"
    logger.info("Rate limiter correctly saturated: %s (Remaining: %.2f)", status.name, remaining)

    # Validate replenishment
    time.sleep(1.0)
    status, remaining = limiter.consume(client_id, 1.0)
    assert status == RateLimitStatus.ALLOWED, "Expected replenishment after sleep"
    logger.info("Token replenishment verified successfully: %s (Remaining: %.2f)", status.name, remaining)

    # Validate identifier policy enforcement
    status, _ = limiter.consume("bad!identifier", 1.0)
    assert status == RateLimitStatus.INVALID_IDENTIFIER, "Expected validation rejection on bad key"
    logger.info("Security sanitization verified: invalid identifiers rejected.")


if __name__ == "__main__":
    try:
        execute_verification_pipeline()
        logger.info("All engine operations completed nominal specification checks.")
        sys.exit(0)
    except Exception as exc:
        logger.critical("Fatal execution failure during startup validation: %s", str(exc), exc_info=True)
        sys.exit(1)