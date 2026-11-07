"""
app1.py - Production-Grade Asynchronous Task & Telemetry Orchestrator

Architectural Components:
    - Circuit Breaker Pattern implementation for downstream fault tolerance.
    - Sliding Window Token Bucket Rate Limiter for request throttling.
    - Concurrent worker pool leveraging asyncio.PriorityQueue.
    - Real-time telemetry, latency metrics, and performance aggregation.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Dict, List, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("Orchestrator")


class CircuitState(enum.Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class TaskPriority(enum.IntEnum):
    CRITICAL = 1
    HIGH = 2
    NORMAL = 3
    LOW = 4


class CircuitBreakerOpenException(Exception):
    """Raised when an operation is rejected by an open circuit breaker."""
    pass


@dataclass(order=True)
class PipelineTask:
    priority: int
    task_id: str = field(compare=False)
    payload: Dict[str, Any] = field(compare=False)
    handler: Callable[[Dict[str, Any]], Coroutine[Any, Any, Any]] = field(compare=False)
    retries: int = field(default=0, compare=False)
    max_retries: int = field(default=3, compare=False)


@dataclass
class TaskResult:
    task_id: str
    success: bool
    data: Optional[Any] = None
    error: Optional[str] = None
    execution_time_ms: float = 0.0


class CircuitBreaker:
    """Manages failure rates and isolates failing external subsystems."""

    def __init__(self, failure_threshold: int = 3, recovery_time_sec: float = 5.0) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_time_sec = recovery_time_sec
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_timestamp = 0.0
        self._lock = asyncio.Lock()

    async def record_success(self) -> None:
        async with self._lock:
            self.failure_count = 0
            self.state = CircuitState.CLOSED

    async def record_failure(self) -> None:
        async with self._lock:
            self.failure_count += 1
            self.last_failure_timestamp = time.monotonic()
            if self.failure_count >= self.failure_threshold:
                self.state = CircuitState.OPEN
                logger.warning("Circuit breaker tripped to OPEN state.")

    async def allow_execution(self) -> bool:
        async with self._lock:
            if self.state == CircuitState.CLOSED:
                return True
            if self.state == CircuitState.OPEN:
                if time.monotonic() - self.last_failure_timestamp >= self.recovery_time_sec:
                    self.state = CircuitState.HALF_OPEN
                    logger.info("Circuit breaker entered HALF_OPEN state; testing recovery.")
                    return True
                return False
            return True


class TokenBucketLimiter:
    """Async thread-safe token bucket algorithm for egress throttling."""

    def __init__(self, rate_per_second: float, burst_capacity: int) -> None:
        self.rate = rate_per_second
        self.capacity = burst_capacity
        self.tokens = float(burst_capacity)
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self.last_refill
                self.last_refill = now
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)

                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return

                sleep_time = (1.0 - self.tokens) / self.rate
            await asyncio.sleep(sleep_time)


class TelemetryCollector:
    """Aggregates execution latency and error metrics."""

    def __init__(self) -> None:
        self.latencies: List[float] = []
        self.successes: int = 0
        self.failures: int = 0
        self._lock = asyncio.Lock()

    async def record(self, result: TaskResult) -> None:
        async with self._lock:
            self.latencies.append(result.execution_time_ms)
            if result.success:
                self.successes += 1
            else:
                self.failures += 1

    async def report(self) -> Dict[str, Any]:
        async with self._lock:
            if not self.latencies:
                return {"status": "No metrics captured."}

            return {
                "total_tasks": len(self.latencies),
                "successful_tasks": self.successes,
                "failed_tasks": self.failures,
                "success_rate_percent": round((self.successes / len(self.latencies)) * 100, 2),
                "p50_latency_ms": round(statistics.median(self.latencies), 2),
                "p95_latency_ms": round(statistics.quantiles(self.latencies, n=20)[18], 2)
                if len(self.latencies) >= 20
                else round(max(self.latencies), 2),
                "mean_latency_ms": round(statistics.mean(self.latencies), 2),
            }


class AsyncPipelineEngine:
    """Coordinates task scheduling, execution, resilient retries, and metrics."""

    def __init__(
        self,
        worker_concurrency: int = 4,
        rate_limit_rps: float = 10.0,
    ) -> None:
        self.concurrency = worker_concurrency
        self.queue: asyncio.PriorityQueue[PipelineTask] = asyncio.PriorityQueue()
        self.circuit_breaker = CircuitBreaker(failure_threshold=3, recovery_time_sec=2.0)
        self.rate_limiter = TokenBucketLimiter(rate_per_second=rate_limit_rps, burst_capacity=5)
        self.telemetry = TelemetryCollector()
        self.workers: List[asyncio.Task] = []
        self.is_running = False

    async def submit(self, task: PipelineTask) -> None:
        await self.queue.put(task)

    async def _execute_single_task(self, task: PipelineTask) -> TaskResult:
        await self.rate_limiter.acquire()

        if not await self.circuit_breaker.allow_execution():
            return TaskResult(
                task_id=task.task_id,
                success=False,
                error="Execution rejected by CircuitBreaker (OPEN).",
            )

        start_time = time.monotonic()
        try:
            output = await task.handler(task.payload)
            elapsed_ms = (time.monotonic() - start_time) * 1000.0
            await self.circuit_breaker.record_success()
            return TaskResult(task_id=task.task_id, success=True, data=output, execution_time_ms=elapsed_ms)
        except Exception as exc:
            elapsed_ms = (time.monotonic() - start_time) * 1000.0
            await self.circuit_breaker.record_failure()
            logger.error("Task %s failed: %s", task.task_id, exc)
            return TaskResult(task_id=task.task_id, success=False, error=str(exc), execution_time_ms=elapsed_ms)

    async def _worker_loop(self, worker_id: int) -> None:
        logger.debug("Worker-%d initialized.", worker_id)
        while self.is_running or not self.queue.empty():
            try:
                task = await asyncio.wait_for(self.queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue

            result = await self._execute_single_task(task)
            await self.telemetry.record(result)

            if not result.success and task.retries < task.max_retries:
                task.retries += 1
                backoff = 0.2 * (2 ** task.retries)
                logger.info("Rescheduling %s in %.2fs (retry %d/%d)", task.task_id, backoff, task.retries, task.max_retries)
                await asyncio.sleep(backoff)
                await self.queue.put(task)

            self.queue.task_done()

    async def start(self) -> None:
        self.is_running = True
        self.workers = [
            asyncio.create_task(self._worker_loop(i), name=f"Worker-{i}")
            for i in range(self.concurrency)
        ]
        logger.info("Engine started with %d workers.", self.concurrency)

    async def stop(self) -> None:
        self.is_running = False
        await self.queue.join()
        for worker in self.workers:
            worker.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        logger.info("Engine gracefully shutdown.")


# =====================================================================
# Simulation Handlers & Main Driver
# =====================================================================

async def mock_ai_inference(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Simulates varying compute latency and transient network drops."""
    await asyncio.sleep(payload.get("delay", 0.05))
    if payload.get("induce_failure", False):
        raise ConnectionResetError("Simulated remote gateway reset")
    return {"status": "INFERRED", "embedding_dim": 768, "input_len": len(payload.get("text", ""))}


async def main() -> None:
    logger.info("Initializing Orchestration Engine...")
    engine = AsyncPipelineEngine(worker_concurrency=4, rate_limit_rps=20.0)
    await engine.start()

    logger.info("Enqueuing synthetic enterprise batch requests...")
    for idx in range(25):
        is_err = idx in (5, 6, 7)  # Induce sequential failures to stress Circuit Breaker
        priority = TaskPriority.CRITICAL if idx % 5 == 0 else TaskPriority.NORMAL
        task = PipelineTask(
            priority=priority,
            task_id=f"TX-{1000 + idx}",
            payload={"text": f"Vectorized query string {idx}", "delay": 0.02, "induce_failure": is_err},
            handler=mock_ai_inference,
            max_retries=2,
        )
        await engine.submit(task)

    await engine.stop()

    report = await engine.telemetry.report()
    logger.info("---------------- EXECUTION METRICS ----------------")
    for key, val in report.items():
        logger.info("  %s: %s", key, val)
    logger.info("---------------------------------------------------")


if __name__ == "__main__":
    asyncio.run(main())