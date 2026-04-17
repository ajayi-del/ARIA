"""
Execution Metrics — non-blocking async latency and slippage tracker.

Per-trade timestamps track every critical interval:
  t_signal → t_entry_sent → t_fill → t_stop_sent → t_stop_confirmed

Writes to logs/execution_metrics.jsonl via background queue.
Zero impact on execution path — all emits are fire-and-forget.
"""
import asyncio
import json
import time
from dataclasses import dataclass
from typing import Optional
import structlog

logger = structlog.get_logger(__name__)

# Alert thresholds (ms)
_SLOW_FILL_MS        = 300.0
_SLOW_PROTECTION_MS  = 400.0
_RISK_WINDOW_MAX_MS  = 500.0  # CRITICAL — unprotected exposure
_HIGH_SLIPPAGE_PCT   = 0.20   # 0.20%

# Per-stage latency thresholds for fill_latency_breakdown logging
PIPELINE_THRESHOLDS = {
    "risk_ms":          50,    # risk validation: should be <50ms
    "sizing_ms":        30,    # Nietzsche + candidate build: should be <30ms
    "sign_ms":          50,    # EIP-712 signing: should be <50ms
    "http_ms":          600,   # HTTP round trip: SoDEX RTT ~460ms + margin
    "pre_size_ms":      150,   # GET /positions pre-snapshot: 1 RTT
    "entry_post_ms":    500,   # Entry POST round trip
    "fill_wait_ms":     300,   # Polling until fill confirmed (0 if immediate)
    "total_ms":         700,   # Total pipeline budget
}


@dataclass
class TradeMetrics:
    """Mutable struct populated at each execution milestone."""
    trade_id:       str
    symbol:         str
    side:           str    # "long" | "short"
    expected_price: float  # signal entry price

    t_signal:          float = 0.0  # when signal fired (epoch seconds)
    t_entry_sent:      float = 0.0  # immediately before _place_entry_order
    t_fill:            float = 0.0  # when _confirm_position_open returns True
    t_stop_sent:       float = 0.0  # immediately before _place_stop_order
    t_stop_confirmed:  float = 0.0  # when stop OrderResult.success == True
    t_tp_sent:         float = 0.0  # when TP orders are sent
    t_tp1_fill:        float = 0.0  # when TP1 fill detected (optional)
    t_be_moved:        float = 0.0  # when break-even stop placed (optional)
    t_exit:            float = 0.0  # final position exit

    actual_fill_price: float = 0.0
    stop_placed:       bool  = False
    tp_placed:         bool  = False

    # ── Computed latencies ────────────────────────────────────────────────────

    @property
    def entry_latency_ms(self) -> float:
        """Signal → entry order sent. Measures system processing overhead."""
        if self.t_entry_sent > 0 and self.t_signal > 0:
            return (self.t_entry_sent - self.t_signal) * 1000
        return 0.0

    @property
    def fill_latency_ms(self) -> float:
        """Entry order sent → fill confirmed. Measures exchange + polling speed."""
        if self.t_fill > 0 and self.t_entry_sent > 0:
            return (self.t_fill - self.t_entry_sent) * 1000
        return 0.0

    @property
    def protection_latency_ms(self) -> float:
        """Fill confirmed → stop order confirmed on exchange."""
        if self.t_stop_confirmed > 0 and self.t_fill > 0:
            return (self.t_stop_confirmed - self.t_fill) * 1000
        return 0.0

    @property
    def risk_window_ms(self) -> float:
        """
        Time position was UNPROTECTED after fill.
        This is the critical metric — should be < 500ms on any healthy system.
        """
        return self.protection_latency_ms

    @property
    def tp_latency_ms(self) -> float:
        """Fill confirmed → TP orders sent."""
        if self.t_tp_sent > 0 and self.t_fill > 0:
            return (self.t_tp_sent - self.t_fill) * 1000
        return 0.0

    @property
    def slippage_pct(self) -> float:
        """
        Signed slippage as % of expected price.
        Positive = we paid more than expected (bad for both directions).
        """
        if self.expected_price > 0 and self.actual_fill_price > 0:
            if self.side == "long":
                return (self.actual_fill_price - self.expected_price) / self.expected_price * 100
            else:
                return (self.expected_price - self.actual_fill_price) / self.expected_price * 100
        return 0.0

    def to_log_dict(self) -> dict:
        return {
            "trade_id":              self.trade_id,
            "symbol":                self.symbol,
            "side":                  self.side,
            "expected_price":        self.expected_price,
            "actual_fill_price":     self.actual_fill_price,
            "entry_latency_ms":      round(self.entry_latency_ms, 1),
            "fill_latency_ms":       round(self.fill_latency_ms, 1),
            "protection_latency_ms": round(self.protection_latency_ms, 1),
            "risk_window_ms":        round(self.risk_window_ms, 1),
            "tp_latency_ms":         round(self.tp_latency_ms, 1),
            "slippage_pct":          round(self.slippage_pct, 4),
            "stop_placed":           self.stop_placed,
            "tp_placed":             self.tp_placed,
            # Alert flags
            "ALERT_slow_fill":       self.fill_latency_ms > _SLOW_FILL_MS,
            "ALERT_risk_window":     self.risk_window_ms > _RISK_WINDOW_MAX_MS,
            "ALERT_high_slippage":   abs(self.slippage_pct) > _HIGH_SLIPPAGE_PCT,
            "ALERT_no_stop":         not self.stop_placed,
        }


class MetricsLogger:
    """
    Async queue-based execution metrics logger.

    Usage:
        metrics_logger.emit(m)  # fire-and-forget, never blocks
        metrics_logger.start()  # call once at bot startup

    Writes JSON-per-line to logs/execution_metrics.jsonl.
    Rolling EMA stats available via .get_stats() for dashboard consumption.
    """

    def __init__(self, log_path: str = "logs/execution_metrics.jsonl"):
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._log_path = log_path
        self._task: Optional[asyncio.Task] = None

        # Rolling EMA stats (α=0.1, O(1) update, no memory accumulation)
        self._n: int = 0
        self.avg_entry_latency_ms:      float = 0.0
        self.avg_fill_latency_ms:       float = 0.0
        self.avg_protection_latency_ms: float = 0.0
        self.avg_slippage_pct:          float = 0.0

    def start(self):
        """Launch background writer task. Call once after event loop starts."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._writer_loop(),
                                              name="metrics_writer")

    def emit(self, m: TradeMetrics):
        """
        Non-blocking emit. Drops silently if queue is full.
        NEVER call with await — this must never block the execution path.
        """
        try:
            self._queue.put_nowait(m)
        except asyncio.QueueFull:
            pass  # metrics are best-effort, never block trades

    async def _writer_loop(self):
        _alpha = 0.1
        while True:
            try:
                m: TradeMetrics = await asyncio.wait_for(
                    self._queue.get(), timeout=10.0
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            self._n += 1

            # Update rolling EMAs
            if m.entry_latency_ms > 0:
                self.avg_entry_latency_ms = (
                    (1 - _alpha) * self.avg_entry_latency_ms
                    + _alpha * m.entry_latency_ms
                )
            if m.fill_latency_ms > 0:
                self.avg_fill_latency_ms = (
                    (1 - _alpha) * self.avg_fill_latency_ms
                    + _alpha * m.fill_latency_ms
                )
            if m.protection_latency_ms > 0:
                self.avg_protection_latency_ms = (
                    (1 - _alpha) * self.avg_protection_latency_ms
                    + _alpha * m.protection_latency_ms
                )
            self.avg_slippage_pct = (
                (1 - _alpha) * self.avg_slippage_pct
                + _alpha * abs(m.slippage_pct)
            )

            # Structured alerts before writing
            self._fire_alerts(m)

            # Write JSON line
            record = {"ts": time.time(), **m.to_log_dict()}
            try:
                with open(self._log_path, "a") as f:
                    f.write(json.dumps(record) + "\n")
            except Exception as e:
                logger.warning("metrics_write_failed", error=str(e))

    def _fire_alerts(self, m: TradeMetrics):
        if m.risk_window_ms > _RISK_WINDOW_MAX_MS and m.risk_window_ms > 0:
            logger.warning(
                "EXEC_RISK_WINDOW_HIGH",
                symbol=m.symbol,
                risk_window_ms=round(m.risk_window_ms, 1),
                threshold_ms=_RISK_WINDOW_MAX_MS,
            )
        if not m.stop_placed:
            logger.error(
                "EXEC_NO_STOP_PLACED",
                symbol=m.symbol,
                trade_id=m.trade_id,
                fill_latency_ms=round(m.fill_latency_ms, 1),
            )
        if m.fill_latency_ms > _SLOW_FILL_MS and m.fill_latency_ms > 0:
            logger.warning(
                "EXEC_SLOW_FILL",
                symbol=m.symbol,
                fill_latency_ms=round(m.fill_latency_ms, 1),
                threshold_ms=_SLOW_FILL_MS,
            )
        if abs(m.slippage_pct) > _HIGH_SLIPPAGE_PCT and m.actual_fill_price > 0:
            logger.warning(
                "EXEC_HIGH_SLIPPAGE",
                symbol=m.symbol,
                slippage_pct=round(m.slippage_pct, 4),
                expected=m.expected_price,
                actual=m.actual_fill_price,
            )

    def get_stats(self) -> dict:
        """Snapshot of rolling averages — safe to call from dashboard."""
        return {
            "trades_tracked":            self._n,
            "avg_entry_latency_ms":      round(self.avg_entry_latency_ms, 1),
            "avg_fill_latency_ms":       round(self.avg_fill_latency_ms, 1),
            "avg_protection_latency_ms": round(self.avg_protection_latency_ms, 1),
            "avg_slippage_pct":          round(self.avg_slippage_pct, 4),
        }


# Module-level singleton — import directly in sodex_client and main
metrics_logger = MetricsLogger()
