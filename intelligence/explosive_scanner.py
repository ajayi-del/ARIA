"""
intelligence/explosive_scanner.py — Mode 7: THE DREAMER (Schumpeter)

"The fundamental impulse that sets and keeps the capitalist engine in
motion comes from the new."

While modes 1-5 analyze existing patterns, the Dreamer hunts NOVELTY:
compressed springs being loaded for a break. Four precursors (Dayo's
bullet doctrine), all read from stores that already stream — no new feeds:

  1. compression     — BB(20,2) width at ≤20th percentile of its own history
  2. oi_loading      — OI +5%/h while price stays flat (<1.5%) — someone is
                       quietly building a position (Watcher's OI history)
  3. funding_extreme — |funding| ≥ 5bps/8h — one side is crowded (squeeze fuel)
  4. volume_breakout — trailing 15m volume ≥ 3× its rolling median — the break
                       has begun

Score = precursors present (0-4). ≥3 → candidate, direction from the
crowded side (negative funding = shorts crowded = long squeeze bias).

Phase A doctrine (WuWei): the Dreamer NEVER touches the execution path.
Candidates go to the Historian (shadow_journal.record_candidate), which
scores their counterfactual edge at 1h/4h/24h. Only after the journal
proves the visions profitable does the Dreamer earn a voice in the
executable pipeline (Phase B, explicit approval).
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger(__name__)

_COMPRESSION_PCTL = 0.20
_OI_LOAD_MIN_PCT = 5.0        # % per hour
_OI_LOAD_FLAT_PX = 1.5        # price must move less than this % over same window
_FUNDING_EXTREME = 0.0005     # 5bps per 8h ≈ 54% annualized
_VOLUME_BREAKOUT_MULT = 3.0
_EMIT_COOLDOWN_S = 4 * 3600.0


def _std(xs: List[float]) -> float:
    if len(xs) < 2:
        return 0.0
    mu = sum(xs) / len(xs)
    return (sum((x - mu) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5


class ExplosiveScanner:
    """Scans constantly, speaks rarely. Phase A: speaks only to the Historian.

    Phase B adds a second, quieter voice: breakout READINESS — the same
    precursor physics as a continuous 0..1 score (precursors/4), refreshed
    by the dreamer loop and read by the Interpreter's COMPRESSION branch.
    No cooldown, no emission, no execution contact — it answers "how loaded
    is this spring?" when the coherence engine asks."""

    def __init__(self, cooldown_s: float = _EMIT_COOLDOWN_S) -> None:
        self._cooldown_s = cooldown_s
        self._last_emit: Dict[str, tuple] = {}  # symbol → (ts, score)
        self._readiness: Dict[str, tuple] = {}  # symbol → (ts, 0..1)
        # symbol → {"ts","score","bb_pctl","vol_ratio","precursors"} — the
        # compression watchlist's raw material (Report 2, 2026-08-16).
        self.metrics: Dict[str, Dict[str, Any]] = {}

    def _bb_width_pctl(self, closes: List[float]) -> Optional[float]:
        """Current BB(20,2) width percentile vs the buffer's own history."""
        if len(closes) < 60:
            return None
        widths = []
        for i in range(20, len(closes) + 1):
            w = closes[i - 20:i]
            ma = sum(w) / 20.0
            if ma <= 0:
                continue
            widths.append(2.0 * _std(w) / ma)
        if len(widths) < 20:
            return None
        cur = widths[-1]
        below = sum(1 for x in widths if x <= cur)
        return below / len(widths)

    def _volume_breakout(self, vols: List[float]) -> Optional[float]:
        """Trailing 15m volume / median rolling-15m volume."""
        if len(vols) < 60:
            return None
        windows = [sum(vols[i - 15:i]) for i in range(15, len(vols) + 1)]
        cur = windows[-1]
        base = sorted(windows[:-1])[len(windows[:-1]) // 2] if len(windows) > 1 else 0.0
        if base <= 0:
            return None
        return cur / base

    def _evaluate(self, closes: List[float], vols: List[float],
                  funding: float, oi_chg: Optional[float]
                  ) -> tuple:
        """Pure precursor physics. Returns (precursors, bb_pctl, vol_ratio)."""
        precursors: List[str] = []

        pctl = self._bb_width_pctl(closes)
        if pctl is not None and pctl <= _COMPRESSION_PCTL:
            precursors.append("compression")

        if oi_chg is not None and oi_chg >= _OI_LOAD_MIN_PCT:
            px_chg = (closes[-1] / closes[-61] - 1.0) * 100.0 if closes[-61] > 0 else 0.0
            if abs(px_chg) < _OI_LOAD_FLAT_PX:
                precursors.append("oi_loading")

        if abs(funding) >= _FUNDING_EXTREME:
            precursors.append("funding_extreme")

        vratio = self._volume_breakout(vols)
        if vratio is not None and vratio >= _VOLUME_BREAKOUT_MULT:
            precursors.append("volume_breakout")

        return precursors, pctl, vratio

    def readiness(self, symbol: str, now: Optional[float] = None,
                  max_age_s: float = 300.0) -> float:
        """Breakout-readiness 0..1 for the Interpreter. Stale → 0 (silent)."""
        entry = self._readiness.get(symbol)
        if not entry:
            return 0.0
        ts, value = entry
        now = now if now is not None else time.time()
        return value if now - ts <= max_age_s else 0.0

    def update_readiness(self, symbols: List[str], candle_buffers,
                         bybit_tickers, watcher,
                         now: Optional[float] = None,
                         lppl_enabled: bool = True) -> None:
        """Refresh the readiness registry — called every dreamer pass.

        LPPL (Sornette dragon-king, 2026-08-22): when the tail closes trace a
        super-exponential log-periodic run-up with confidence ≥0.5, readiness
        gets an additive boost min(1.0, base + 0.25×conf) — a compressed
        spring with the wave building inside it breaks harder. Additive, NOT
        a fifth precursor: a 5th gate would dilute the 3/4 candidate scores.
        lppl_enabled=False reproduces the pre-module readiness bit-for-bit."""
        from intelligence.lppl import lppl_confidence
        now = now if now is not None else time.time()
        for sym in symbols or []:
            try:
                buf = (candle_buffers.get(sym) or {}).get("1m")
                if buf is None:
                    continue
                cs = buf.latest(120)
                if len(cs) < 60:
                    continue
                closes = [float(getattr(c, "close", 0) or 0) for c in cs]
                vols = [float(getattr(c, "volume", 0) or 0) for c in cs]
                tick = (bybit_tickers or {}).get(sym) or {}
                funding = float(tick.get("funding_rate", 0.0) or 0.0)
                oi_chg = watcher.oi_change_pct(sym, 3600.0, now=now) if watcher else None
                precursors, pctl, vratio = self._evaluate(closes, vols, funding, oi_chg)
                base = len(precursors) / 4.0
                conf = lppl_confidence(closes) if lppl_enabled else None
                boost = 0.25 * conf if (conf is not None and conf >= 0.5) else 0.0
                self._readiness[sym] = (now, min(1.0, base + boost))
                self.metrics[sym] = {
                    "ts": now, "score": round(min(1.0, base + boost), 3),
                    "bb_pctl": (round(pctl * 100.0, 1) if pctl is not None else None),
                    "vol_ratio": (round(vratio, 2) if vratio is not None else None),
                    "precursors": list(precursors),
                    "lppl_conf": (round(conf, 3) if conf is not None else None),
                }
            except Exception:
                continue

    def scan(self, symbols: List[str], candle_buffers, bybit_tickers,
             watcher, now: Optional[float] = None) -> List[Dict[str, Any]]:
        now = now if now is not None else time.time()
        out: List[Dict[str, Any]] = []
        for sym in symbols or []:
            try:
                buf = (candle_buffers.get(sym) or {}).get("1m")
                if buf is None:
                    continue
                cs = buf.latest(120)
                if len(cs) < 60:
                    continue
                closes = [float(getattr(c, "close", 0) or 0) for c in cs]
                vols = [float(getattr(c, "volume", 0) or 0) for c in cs]
                tick = (bybit_tickers or {}).get(sym) or {}
                funding = float(tick.get("funding_rate", 0.0) or 0.0)

                oi_chg = watcher.oi_change_pct(sym, 3600.0, now=now) if watcher else None
                precursors, pctl, vratio = self._evaluate(
                    closes, vols, funding, oi_chg)

                score = len(precursors)
                if score < 3:
                    continue

                # Direction from the crowded side: negative funding = shorts
                # paying = crowded short = long squeeze bias. Fallback: BB side.
                if funding <= -_FUNDING_EXTREME:
                    direction = "long"
                elif funding >= _FUNDING_EXTREME:
                    direction = "short"
                else:
                    ma20 = sum(closes[-20:]) / 20.0
                    direction = "long" if closes[-1] >= ma20 else "short"

                last = self._last_emit.get(sym)
                if last and now - last[0] < self._cooldown_s and last[1] >= score:
                    continue
                self._last_emit[sym] = (now, score)

                out.append({
                    "symbol": sym, "direction": direction, "score": score,
                    "precursors": precursors,
                    "details": (f"bb_pctl={pctl:.2f} oi_chg={oi_chg} "
                                f"funding={funding:.5f} vol_x={vratio:.1f}"
                                if pctl is not None else ""),
                })
            except Exception as ex:
                logger.debug("explosive_scan_symbol_error", symbol=sym,
                             error=str(ex)[:80])
        return out


# Process-wide singleton — main.py's dreamer loop refreshes it; the
# Interpreter's COMPRESSION branch reads readiness() off the hot path.
explosive_scanner = ExplosiveScanner()
