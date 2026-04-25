"""
execution/execution_metrics.py — Fill Quality + Slippage Tracker
ARIA Execution Alpha Patch — Component 9

Tracks per-trade entry quality: slippage, spread capture, fill latency.
Aggregated into a rolling summary for the dashboard and logs.
"""
from __future__ import annotations

from collections import deque
from typing import Dict, Optional
import time
import structlog

log = structlog.get_logger(__name__)

_WINDOW = 100   # rolling sample window


class ExecutionMetrics:
    """Lightweight fill-quality tracker. Thread-safe reads (GIL-covered appends)."""

    def __init__(self) -> None:
        self._slippage_bps: deque[float] = deque(maxlen=_WINDOW)
        self._spread_bps:   deque[float] = deque(maxlen=_WINDOW)
        self._latency_ms:   deque[float] = deque(maxlen=_WINDOW)
        self._by_symbol:    Dict[str, list] = {}   # symbol → [slippage_bps, ...]

    def on_entry_filled(
        self,
        trade_id:        str,
        symbol:          str,
        direction:       str,
        target_price:    float,
        actual_price:    float,
        spread_at_entry: float = 0.0,   # absolute spread (ask - bid)
        fill_latency_ms: float = 0.0,
    ) -> None:
        if target_price <= 0 or actual_price <= 0:
            return

        slip_signed = (actual_price - target_price) / target_price * 1e4
        # For longs: positive slip = paid more (bad). For shorts: negative slip = bad.
        if direction == "short":
            slip_signed = -slip_signed
        slippage_bps = slip_signed

        spread_bps = (spread_at_entry / actual_price * 1e4) if actual_price > 0 else 0.0

        self._slippage_bps.append(slippage_bps)
        self._spread_bps.append(spread_bps)
        if fill_latency_ms > 0:
            self._latency_ms.append(fill_latency_ms)

        self._by_symbol.setdefault(symbol, []).append(slippage_bps)

        log.debug(
            "fill_quality",
            trade_id=trade_id,
            symbol=symbol,
            direction=direction,
            target=round(target_price, 6),
            actual=round(actual_price, 6),
            slippage_bps=round(slippage_bps, 2),
            spread_bps=round(spread_bps, 2),
            latency_ms=round(fill_latency_ms, 1),
        )

    def summary(self) -> Dict:
        def _avg(d: deque) -> Optional[float]:
            return round(sum(d) / len(d), 2) if d else None

        sym_summary = {}
        for sym, slips in self._by_symbol.items():
            sym_summary[sym] = round(sum(slips) / len(slips), 2)

        return {
            "avg_slippage_bps": _avg(self._slippage_bps),
            "avg_spread_bps":   _avg(self._spread_bps),
            "avg_latency_ms":   _avg(self._latency_ms),
            "sample_count":     len(self._slippage_bps),
            "by_symbol":        sym_summary,
        }

    def worst_symbols(self, top_n: int = 3) -> list:
        ranked = sorted(
            self._by_symbol.items(),
            key=lambda kv: sum(kv[1]) / len(kv[1]) if kv[1] else 0,
            reverse=True,
        )
        return [(sym, round(sum(v) / len(v), 2)) for sym, v in ranked[:top_n] if v]
