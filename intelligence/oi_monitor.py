"""
OIArbMonitor — Open Interest divergence signal engine.

Reads Bybit OI from bybit_ticker_stores (already populated by bybit_feed.py).
Generates three signal types when OI and price diverge or expand together:

  DIVERGENCE: OI and price opposite → contrarian reversal expected  (strength 1.2)
  EXPANSION:  OI and price same direction → genuine trend confirm   (strength 0.56)
  SPIKE:      OI collapsed >10% → mass liquidation → recovery       (strength 1.5)

Feeds into Tier 6 of the interpreter coherence engine alongside the
ValueChain on-chain liquidation signals (takes the stronger of the two).

Why BNB specifically:
  1. Information edge: Bybit BNB OI = Binance ecosystem signal (informed flow)
  2. Thin SoDEX book: Bybit OI signals propagate to SoDEX with high S/N ratio
  3. Funding stack: OI divergence short + funding collection = two profit sources
"""

import time
import structlog
from collections import deque, defaultdict
from dataclasses import dataclass
from typing import Dict, Tuple, Any, Optional

log = structlog.get_logger(__name__)

# Minimum OI change pct to generate a signal
_MIN_OI_CHANGE_PCT  = 0.05   # 5%
_SPIKE_THRESHOLD    = 0.10   # 10% drop = liquidation spike
_SIGNAL_TTL_MS      = 300_000  # signals expire after 5 minutes
_MIN_DECAY          = 0.3    # minimum strength retained at expiry

_STRENGTH: Dict[str, float] = {
    "divergence": 1.2,
    "expansion":  0.80,   # multiplied by 0.7 at apply time → 0.56 net
    "spike":      1.5,
}


@dataclass
class OISignal:
    symbol:          str
    signal_type:     str    # "divergence" | "expansion" | "spike"
    direction:       str    # "long" | "short"
    strength:        float
    oi_change_pct:   float
    price_change_pct: float
    bybit_oi:        float
    timestamp_ms:    int
    expires_ms:      int
    note:            str


class OIArbMonitor:
    """
    Open Interest divergence signal generator.

    Reads directly from bybit_ticker_stores (populated by BybitFeed).
    No separate subscriptions required — evaluate() is called by the
    interpreter on each signal cycle.
    """

    def __init__(self, bybit_ticker_stores: Dict[str, Any]) -> None:
        self._tickers = bybit_ticker_stores
        # OI + price history per symbol for multi-sample analysis
        self._oi_history: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=20)
        )
        self._active_signals: Dict[str, OISignal] = {}
        log.info("oi_arb_monitor_init")

    # ─────────────────────────────────────────────────────────────────────────
    # Public interface
    # ─────────────────────────────────────────────────────────────────────────

    def evaluate(self, symbol: str) -> None:
        """
        Ingest latest ticker data and update OI history for this symbol.
        Call once per interpreter cycle per symbol (before get_oi_score).
        """
        ticker = self._tickers.get(symbol)
        if not ticker:
            return
        oi    = ticker.get("open_interest", 0.0)
        price = ticker.get("mark_price", 0.0)
        if oi <= 0 or price <= 0:
            return
        self._oi_history[symbol].append(
            {"oi": oi, "price": price, "time_ms": int(time.time() * 1000)}
        )
        self._evaluate_signal(symbol)

    def get_oi_score(
        self,
        symbol: str,
        candidate_direction: str,
    ) -> Tuple[float, str]:
        """
        Returns (score, signal_direction) for interpreter Tier 6 use.
        Score is adjusted for time decay and direction alignment.
        """
        now_ms = int(time.time() * 1000)
        sig    = self._active_signals.get(symbol)

        if sig is None:
            return 0.0, "none"

        if now_ms > sig.expires_ms:
            del self._active_signals[symbol]
            return 0.0, "none"

        # Time decay: linear from 1.0 → _MIN_DECAY over signal TTL
        age    = now_ms - sig.timestamp_ms
        ttl    = sig.expires_ms - sig.timestamp_ms
        decay  = max(_MIN_DECAY, 1.0 - age / ttl)
        score  = sig.strength * decay

        # Direction alignment penalty
        if sig.direction != candidate_direction:
            score *= 0.3   # signal contradicts candidate — heavy discount

        return round(score, 3), sig.direction

    def get_active_signals(self) -> list:
        """Returns all non-expired active signals for display."""
        now_ms = int(time.time() * 1000)
        expired = [k for k, s in self._active_signals.items() if now_ms > s.expires_ms]
        for k in expired:
            del self._active_signals[k]
        return [
            {
                "symbol":    s.symbol,
                "type":      s.signal_type,
                "direction": s.direction,
                "strength":  round(s.strength, 2),
                "oi_change_pct": round(s.oi_change_pct, 2),
                "age_s":     round((now_ms - s.timestamp_ms) / 1000),
                "note":      s.note,
            }
            for s in self._active_signals.values()
        ]

    # ─────────────────────────────────────────────────────────────────────────
    # Private signal computation
    # ─────────────────────────────────────────────────────────────────────────

    def _evaluate_signal(self, symbol: str) -> None:
        hist = list(self._oi_history[symbol])
        if len(hist) < 3:
            return

        recent   = hist[-1]
        previous = hist[-3]   # ~2 samples back

        if previous["oi"] <= 0 or previous["price"] <= 0:
            return

        oi_change    = (recent["oi"]    - previous["oi"])    / previous["oi"]
        price_change = (recent["price"] - previous["price"]) / previous["price"]

        if abs(oi_change) < _MIN_OI_CHANGE_PCT:
            return   # not enough OI movement

        signal_type: Optional[str] = None
        direction:   Optional[str] = None

        # ── SPIKE: OI collapsed — mass liquidation event ──────────────────────
        if oi_change < -_SPIKE_THRESHOLD:
            signal_type = "spike"
            # If price fell on OI spike: longs were liquidated → recovery long
            # If price rose on OI spike: shorts were liquidated → recovery short
            direction = "long" if price_change <= 0 else "short"

        # ── DIVERGENCE: OI and price moving opposite ───────────────────────────
        elif (oi_change > 0) != (price_change > 0):
            signal_type = "divergence"
            if price_change > 0 and oi_change < 0:
                # Price rising but OI falling → short squeeze exhausting → SHORT
                direction = "short"
            elif price_change < 0 and oi_change < 0:
                # Price falling AND OI falling → long capitulation ending → LONG
                direction = "long"

        # ── EXPANSION: OI and price same direction ─────────────────────────────
        elif (oi_change > 0) == (price_change > 0):
            signal_type = "expansion"
            direction = "long" if price_change > 0 else "short"

        if signal_type is None or direction is None:
            return

        strength = _STRENGTH[signal_type]

        # Expansion is trend-confirming not leading — reduce weight
        if signal_type == "expansion":
            strength *= 0.7

        # Whale amplifier: any single recent OI observation ≥ $5k USD in last 30s
        now_ms = int(time.time() * 1000)
        _recent_large = any(
            h["oi"] >= 5_000_000  # $5M OI in single observation
            for h in hist
            if now_ms - h["time_ms"] < 30_000
        )
        if _recent_large and signal_type in ("divergence", "spike"):
            strength = min(1.5, strength + 0.15)

        note = self._explain(signal_type, direction, oi_change, price_change)
        sig  = OISignal(
            symbol=symbol,
            signal_type=signal_type,
            direction=direction,
            strength=round(strength, 3),
            oi_change_pct=round(oi_change * 100, 3),
            price_change_pct=round(price_change * 100, 3),
            bybit_oi=recent["oi"],
            timestamp_ms=now_ms,
            expires_ms=now_ms + _SIGNAL_TTL_MS,
            note=note,
        )
        self._active_signals[symbol] = sig

        log.info("oi_arb_signal",
                 symbol=symbol, type=signal_type, direction=direction,
                 strength=round(strength, 2),
                 oi_change_pct=round(oi_change * 100, 3),
                 price_change_pct=round(price_change * 100, 3),
                 note=note)

    @staticmethod
    def _explain(
        signal_type: str,
        direction: str,
        oi_change: float,
        price_change: float,
    ) -> str:
        if signal_type == "divergence":
            if direction == "short":
                return ("price rising but OI falling — short squeeze exhausting"
                        " — reversal short expected")
            return ("price falling but OI falling — long capitulation ending"
                    " — recovery long expected")
        if signal_type == "expansion":
            move = "up" if direction == "long" else "down"
            return f"price and OI both moving {move} — genuine trend confirmation"
        if signal_type == "spike":
            liq_type = "longs" if direction == "long" else "shorts"
            return f"OI collapsed >{_SPIKE_THRESHOLD*100:.0f}% — {liq_type} liquidated — recovery incoming"
        return ""
