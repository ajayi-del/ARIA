"""intelligence/spread_signal.py — Rolling spread-zscore tracker (pure brain).

2026-09-19 shadow measurement plane. Kyle / Glosten-Milgrom: the quoted
spread IS the market maker's uncertainty price — a widening spread is
informed-flow risk repricing before the move prints. This tracker keeps a
per-symbol rolling window of (ts, spread_bps, mid) and answers two questions:
how unusual is the current spread (zscore) and which side is pressuring it
(mid drift). Zero I/O, no logging — the caller owns cadence and telemetry.
Fail-open: thin windows and degenerate (zero-variance) samples abstain.
"""
from __future__ import annotations

from collections import deque
from typing import Deque, Dict, Optional, Tuple

DIRECTION_WINDOW_S = 60.0


class SpreadSignalTracker:
    def __init__(self, window_s: float = 1800.0, min_samples: int = 60):
        self.window_s = float(window_s)
        self.min_samples = int(min_samples)
        self._buf: Dict[str, Deque[Tuple[float, float, float]]] = {}

    def update(self, symbol: str, spread_bps: float, mid: float,
               ts: float) -> None:
        try:
            t, s, m = float(ts), float(spread_bps), float(mid)
        except (TypeError, ValueError):
            return
        dq = self._buf.setdefault(symbol, deque())
        dq.append((t, s, m))
        cutoff = t - self.window_s
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def _window(self, symbol: str, ts: float):
        dq = self._buf.get(symbol)
        if not dq:
            return []
        cutoff = float(ts) - self.window_s
        return [row for row in dq if row[0] >= cutoff]

    def zscore(self, symbol: str, ts: float) -> Optional[float]:
        """(latest spread − window mean) / window std; None below min_samples
        or when the window has zero variance (std undefined → no signal)."""
        rows = self._window(symbol, ts)
        if len(rows) < self.min_samples:
            return None
        spreads = [r[1] for r in rows]
        mean = sum(spreads) / len(spreads)
        var = sum((x - mean) ** 2 for x in spreads) / len(spreads)
        if var <= 0.0:
            return None
        return (spreads[-1] - mean) / (var ** 0.5)

    def widening_direction(self, symbol: str, ts: float) -> Optional[str]:
        """Mid drift over the last 60s: rising mid = ask-side pressure
        ("ask"), falling = "bid", flat/thin = None."""
        dq = self._buf.get(symbol)
        if not dq:
            return None
        cutoff = float(ts) - DIRECTION_WINDOW_S
        mids = [r[2] for r in dq if r[0] >= cutoff]
        if len(mids) < 2:
            return None
        drift = mids[-1] - mids[0]
        if drift > 0:
            return "ask"
        if drift < 0:
            return "bid"
        return None
