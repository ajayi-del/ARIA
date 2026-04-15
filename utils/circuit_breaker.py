"""
Module: utils/circuit_breaker.py
Purpose: Prevent cascading failures by failing fast when a downstream service is unhealthy.
Integration: Used by valuechain_monitor._rpc_call_failover() per-endpoint.
             Import: from utils.circuit_breaker import CircuitBreaker, CircuitOpenError

Feature Flags (config/infrastructure.yaml):
  valuechain_rpc.circuit_breaker.enabled: bool       # false → passthrough, no state machine
  valuechain_rpc.circuit_breaker.failure_threshold: int = 3
  valuechain_rpc.circuit_breaker.success_threshold: int = 2
  valuechain_rpc.circuit_breaker.open_timeout_s: float = 60.0

Failure Modes:
  - Circuit OPEN → raises CircuitOpenError immediately (no network call)
  - Circuit HALF_OPEN + request fails → reopens immediately
  - enabled=false → all calls pass through, state machine never runs

Metrics Exported:
  - aria_circuit_breaker_state: gauge (0=CLOSED,1=OPEN,2=HALF_OPEN) per name
  - aria_circuit_breaker_trips_total: counter per name
  - aria_circuit_breaker_rejections_total: counter per name

Rollback: valuechain_rpc.circuit_breaker.enabled: false
"""

import asyncio
import time
import structlog
from enum import Enum
from typing import Callable, Awaitable, TypeVar, Optional

log = structlog.get_logger(__name__)

T = TypeVar("T")


class CircuitState(Enum):
    CLOSED    = "closed"     # Normal: all calls pass through
    OPEN      = "open"       # Failing: all calls rejected immediately
    HALF_OPEN = "half_open"  # Probing: one call allowed to test recovery


class CircuitOpenError(Exception):
    """Raised when a call is rejected because the circuit is OPEN."""
    def __init__(self, name: str, open_since: float):
        self.name = name
        self.open_since = open_since
        super().__init__(f"Circuit '{name}' is OPEN (opened {round(time.time() - open_since, 1)}s ago)")


class CircuitBreaker:
    """
    Async-safe circuit breaker per the classic three-state model.

    States:
      CLOSED    → Normal operation. Calls counted. Too many failures → OPEN.
      OPEN      → Fast-fail. All calls raise CircuitOpenError immediately.
                  After open_timeout_s, transitions to HALF_OPEN.
      HALF_OPEN → One probe call allowed. Success → CLOSED. Failure → OPEN.

    Thread/task safety: uses asyncio.Lock, safe for concurrent coroutines.

    Usage:
        breaker = CircuitBreaker("valuechain_rpc", failure_threshold=3)

        try:
            result = await breaker.call(some_async_function, arg1, arg2)
        except CircuitOpenError:
            # Circuit is open — use fallback
        except SomeOtherError:
            # Call was made but failed
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 3,
        success_threshold: int = 2,
        open_timeout_s: float = 60.0,
        enabled: bool = True,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.success_threshold = success_threshold
        self.open_timeout_s = open_timeout_s
        self.enabled = enabled

        self._state: CircuitState = CircuitState.CLOSED
        self._failure_count: int = 0
        self._success_count: int = 0  # successes in HALF_OPEN
        self._opened_at: float = 0.0
        self._lock = asyncio.Lock()

        # Lifetime counters for metrics
        self._trips_total: int = 0
        self._rejections_total: int = 0
        self._calls_total: int = 0
        self._success_total: int = 0

    # ── Public API ─────────────────────────────────────────────────────────────

    async def call(self, fn: Callable[..., Awaitable[T]], *args, **kwargs) -> T:
        """
        Execute fn(*args, **kwargs) under circuit breaker protection.

        Raises:
            CircuitOpenError — circuit is OPEN, call was not made
            Any exception from fn — call was made and fn raised
        """
        if not self.enabled:
            return await fn(*args, **kwargs)

        async with self._lock:
            await self._maybe_transition()
            if self._state == CircuitState.OPEN:
                self._rejections_total += 1
                _emit_metrics(self)
                raise CircuitOpenError(self.name, self._opened_at)

        # Call outside lock — don't block other coroutines during the actual request
        self._calls_total += 1
        try:
            result = await fn(*args, **kwargs)
            async with self._lock:
                await self._on_success()
            return result
        except Exception as e:
            async with self._lock:
                await self._on_failure(e)
            raise

    @property
    def state(self) -> CircuitState:
        return self._state

    def get_stats(self) -> dict:
        return {
            "name":              self.name,
            "state":             self._state.value,
            "failure_count":     self._failure_count,
            "trips_total":       self._trips_total,
            "rejections_total":  self._rejections_total,
            "calls_total":       self._calls_total,
            "success_total":     self._success_total,
            "open_since":        round(time.time() - self._opened_at, 1) if self._opened_at else None,
        }

    def reset(self) -> None:
        """Manually force circuit to CLOSED (for testing / operator override)."""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._opened_at = 0.0
        log.info("circuit_breaker_manual_reset", name=self.name)

    # ── Internal state machine ──────────────────────────────────────────────────

    async def _maybe_transition(self) -> None:
        """Check timeout: OPEN → HALF_OPEN after open_timeout_s."""
        if self._state == CircuitState.OPEN:
            if time.time() - self._opened_at >= self.open_timeout_s:
                self._state = CircuitState.HALF_OPEN
                self._success_count = 0
                log.info("circuit_breaker_half_open",
                         name=self.name,
                         open_duration_s=round(time.time() - self._opened_at, 1))

    async def _on_success(self) -> None:
        if self._state == CircuitState.HALF_OPEN:
            self._success_count += 1
            self._success_total += 1
            if self._success_count >= self.success_threshold:
                self._state = CircuitState.CLOSED
                self._failure_count = 0
                self._opened_at = 0.0
                log.info("circuit_breaker_closed",
                         name=self.name,
                         success_count=self._success_count)
        elif self._state == CircuitState.CLOSED:
            self._success_total += 1
            # Decay failure count on success (partial recovery signal)
            if self._failure_count > 0:
                self._failure_count = max(0, self._failure_count - 1)

    async def _on_failure(self, exc: Exception) -> None:
        self._failure_count += 1

        if self._state == CircuitState.HALF_OPEN:
            # Any failure in HALF_OPEN → back to OPEN immediately
            self._state = CircuitState.OPEN
            self._opened_at = time.time()
            self._trips_total += 1
            log.warning("circuit_breaker_reopened",
                        name=self.name,
                        error=str(exc)[:80],
                        note="failed in HALF_OPEN — reopening")
            _emit_metrics(self)

        elif self._state == CircuitState.CLOSED:
            if self._failure_count >= self.failure_threshold:
                self._state = CircuitState.OPEN
                self._opened_at = time.time()
                self._trips_total += 1
                log.warning("circuit_breaker_opened",
                            name=self.name,
                            failure_count=self._failure_count,
                            threshold=self.failure_threshold,
                            error=str(exc)[:80])
                _emit_metrics(self)
            else:
                log.debug("circuit_breaker_failure_counted",
                          name=self.name,
                          failure_count=self._failure_count,
                          threshold=self.failure_threshold)


# ── Metrics integration (no-op if prometheus_client unavailable) ───────────────

def _emit_metrics(breaker: "CircuitBreaker") -> None:
    """Update Prometheus gauges/counters if metrics module is available."""
    try:
        from monitoring.metrics import (
            circuit_breaker_state,
            circuit_breaker_trips_total,
            circuit_breaker_rejections_total,
        )
        _state_val = {"closed": 0, "open": 1, "half_open": 2}.get(breaker.state.value, 0)
        circuit_breaker_state.labels(name=breaker.name).set(_state_val)
        # Counters are totals — we track internally and expose as gauges instead
        circuit_breaker_trips_total.labels(name=breaker.name)._value.set(breaker._trips_total)
        circuit_breaker_rejections_total.labels(name=breaker.name)._value.set(breaker._rejections_total)
    except Exception:
        pass  # Metrics are never load-bearing
