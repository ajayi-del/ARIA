"""
Module: monitoring/metrics.py
Purpose: Prometheus-compatible metrics for ARIA infrastructure resilience.
Integration: Imported by valuechain_monitor, cascade_tracker, bybit_feed,
             circuit_breaker, and health.py. All imports are guarded —
             if prometheus_client is not installed, all metrics are no-ops.

Feature Flags (config/infrastructure.yaml):
  metrics.enabled: bool  # false → all metric objects are no-ops

Failure Modes:
  - prometheus_client not installed → no-ops, warning logged once at startup
  - Any metric operation raises → silently caught (never load-bearing)

Rollback: metrics.enabled: false

Install: pip install prometheus-client
"""

import structlog
from typing import Any

log = structlog.get_logger(__name__)


# ── No-op stub — used when prometheus_client unavailable or metrics disabled ──

class _NoOpMetric:
    """Drop-in stub for any prometheus_client metric type."""

    def labels(self, **kwargs) -> "_NoOpMetric":
        return self

    def inc(self, amount: float = 1) -> None:
        pass

    def dec(self, amount: float = 1) -> None:
        pass

    def set(self, value: float) -> None:
        pass

    def observe(self, value: float) -> None:
        pass

    def time(self):
        import contextlib
        return contextlib.nullcontext()


_NOOP = _NoOpMetric()


def _noop_factory(*args, **kwargs) -> _NoOpMetric:
    return _NoOpMetric()


# ── Try to import prometheus_client ────────────────────────────────────────────

_prometheus_available = False
try:
    from prometheus_client import Counter as _Counter, Gauge as _Gauge, Histogram as _Histogram
    _prometheus_available = True
except ImportError:
    log.warning("prometheus_client_not_installed",
                note="pip install prometheus-client to enable metrics — all metrics are no-ops",
                once=True)
    _Counter = _noop_factory
    _Gauge   = _noop_factory
    _Histogram = _noop_factory


def _make(factory, *args, **kwargs) -> Any:
    """Wrap metric construction so a duplicate-registration error doesn't crash startup."""
    try:
        return factory(*args, **kwargs)
    except Exception:
        # prometheus_client raises ValueError on duplicate registration (e.g. test reruns)
        return _NoOpMetric()


# ── RPC Metrics ────────────────────────────────────────────────────────────────

rpc_requests_total = _make(
    _Counter,
    "aria_rpc_requests_total",
    "Total RPC calls made to ValueChain endpoints",
    ["endpoint", "method", "status"],   # status: success | timeout | error
)

rpc_request_duration_ms = _make(
    _Histogram,
    "aria_rpc_request_duration_ms",
    "RPC call latency in milliseconds",
    ["endpoint", "method"],
    buckets=[50, 100, 200, 500, 1000, 2000, 5000],
)

rpc_endpoint_healthy = _make(
    _Gauge,
    "aria_rpc_endpoint_healthy",
    "1 if endpoint is healthy (not in backoff), 0 otherwise",
    ["endpoint"],
)

rpc_failovers_total = _make(
    _Counter,
    "aria_rpc_failovers_total",
    "Number of times the active RPC endpoint was switched due to failure",
    ["from_endpoint", "to_endpoint"],
)


# ── WebSocket Metrics ──────────────────────────────────────────────────────────

ws_connection_state = _make(
    _Gauge,
    "aria_ws_connection_state",
    "WebSocket connection state: 0=disconnected, 1=connecting, 2=connected",
    ["feed"],
)

ws_reconnect_total = _make(
    _Counter,
    "aria_ws_reconnect_total",
    "Total WebSocket reconnection attempts",
    ["feed"],
)

ws_messages_received_total = _make(
    _Counter,
    "aria_ws_messages_received_total",
    "Total messages received per feed type",
    ["feed", "type"],
)

ws_cached_candle_served_total = _make(
    _Counter,
    "aria_ws_cached_candle_served_total",
    "Stale cached candles served during WebSocket outage",
    ["symbol"],
)


# ── Cascade Metrics ────────────────────────────────────────────────────────────

cascade_detected_total = _make(
    _Counter,
    "aria_cascade_detected_total",
    "Total cascade events detected",
    ["direction", "zscore_bucket"],  # zscore_bucket: low|medium|high|extreme
)

cascade_freeze_bypassed_total = _make(
    _Counter,
    "aria_cascade_freeze_bypassed_total",
    "Number of times a stale freeze was bypassed due to extreme zscore",
)

cascade_phase_transitions_total = _make(
    _Counter,
    "aria_cascade_phase_transitions_total",
    "Total cascade phase transitions",
    ["from_phase", "to_phase"],
)

cascade_state_restored_total = _make(
    _Counter,
    "aria_cascade_state_restored_total",
    "Times cascade state was successfully restored from disk after restart",
    ["phase"],
)


# ── Circuit Breaker Metrics ────────────────────────────────────────────────────

circuit_breaker_state = _make(
    _Gauge,
    "aria_circuit_breaker_state",
    "Circuit breaker state: 0=CLOSED, 1=OPEN, 2=HALF_OPEN",
    ["name"],
)

circuit_breaker_trips_total = _make(
    _Gauge,
    "aria_circuit_breaker_trips_total",
    "Total times a circuit breaker tripped (CLOSED→OPEN)",
    ["name"],
)

circuit_breaker_rejections_total = _make(
    _Gauge,
    "aria_circuit_breaker_rejections_total",
    "Total calls rejected because circuit was OPEN",
    ["name"],
)


# ── State Persistence Metrics ──────────────────────────────────────────────────

state_save_total = _make(
    _Counter,
    "aria_state_save_total",
    "Total state file saves",
    ["component", "status"],  # status: success | failed
)

state_load_total = _make(
    _Counter,
    "aria_state_load_total",
    "Total state file loads on startup",
    ["component", "result"],  # result: loaded | stale | missing | error
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def zscore_bucket(zscore: float) -> str:
    """Classify a zscore into a label bucket for metrics cardinality control."""
    if zscore >= 4.0:
        return "extreme"
    if zscore >= 3.0:
        return "high"
    if zscore >= 2.0:
        return "medium"
    return "low"


def is_available() -> bool:
    return _prometheus_available
