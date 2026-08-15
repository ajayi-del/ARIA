"""
intelligence/watcher.py — Mode 1: THE WATCHER (Lao Tzu / Wu Wei)

"The Tao does nothing, yet nothing is left undone."

The Watcher observes the market WITHOUT desire. It does not score signals,
does not recommend trades, does not know what a position is. It produces a
single scalar — market_energy (0-100) — measuring how MUCH is happening,
never WHAT. Every other mode has a bias; the Watcher is the only organ that
sees the tape as-is.

Components (all from stores that already stream — no new feeds):
  vol_energy      30% — realized-vol ratio (recent 30m vs buffer baseline)
  volume_energy   20% — volume ratio (recent 30m vs prior 90m)
  oi_energy       20% — |ΔOI%| over 30min, median across symbols
  funding_energy  15% — cross-symbol funding-rate dispersion (stdev, bps)
  liq_energy      15% — all-market liquidation events, trailing 15min
                        (reads the liq_phase_engine singleton's "" bucket)

The DayTypeClassifier connection (the existing day-reader): fraction of
classified symbols sitting in CHOP damps the composite — a market full of
locked-chop days is asleep even if one component twitches:
    energy = raw × (1 − 0.4 × chop_fraction)

Consumers: personality context cache (update_market_energy), param_store
TTL key "market_energy", shadow journal records (context_fn). Activation
schedule (<15 standby / 15-40 normal / 40-70 volatile / >70 storm) is a
Phase-B consumer decision — the Watcher only publishes the number.
"""

from __future__ import annotations

import math
import time
from collections import deque
from typing import Any, Dict, Optional

import structlog

from intelligence.liq_phase_engine import liq_phase_engine

logger = structlog.get_logger(__name__)

_OI_WINDOW_S = 1800.0       # OI velocity lookback
_OI_KEEP_S = 6 * 3600.0     # retain 6h of OI snapshots (Dreamer reads 1h/4h)
_LIQ_WINDOW_S = 900.0       # liquidation rate lookback
_WEIGHTS = {"vol": 0.30, "volume": 0.20, "oi": 0.20, "funding": 0.15, "liq": 0.15}


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _realized_vol(closes) -> float:
    if len(closes) < 3:
        return 0.0
    rets = [math.log(closes[i] / closes[i - 1])
            for i in range(1, len(closes)) if closes[i - 1] > 0 and closes[i] > 0]
    if len(rets) < 2:
        return 0.0
    mu = sum(rets) / len(rets)
    return math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1))


class Watcher:
    """Observes without desire. Publishes market_energy — nothing else."""

    def __init__(self) -> None:
        # symbol → deque[(ts, open_interest)] — own snapshots, one per compute()
        self._oi_hist: Dict[str, deque] = {}
        self._latest: Dict[str, Any] = {"energy": None, "raw_energy": None,
                                        "components": {}, "chop_fraction": 0.0,
                                        "ts": 0.0}

    def latest(self) -> Dict[str, Any]:
        return self._latest

    # ── Components ────────────────────────────────────────────────────────

    def _vol_volume_energy(self, candle_buffers) -> Dict[str, Optional[float]]:
        vol_scores, volume_scores = [], []
        for bufs in (candle_buffers or {}).values():
            buf = (bufs or {}).get("1m")
            if buf is None:
                continue
            cs = buf.latest(120)
            if len(cs) < 60:
                continue
            closes = [float(getattr(c, "close", 0) or 0) for c in cs]
            vols = [float(getattr(c, "volume", 0) or 0) for c in cs]
            rv_recent = _realized_vol(closes[-30:])
            rv_base = _realized_vol(closes)
            if rv_base > 0:
                vol_scores.append(_clamp01((rv_recent / rv_base - 0.5) / 1.5) * 100)
            recent_v = sum(vols[-30:]) / 30.0
            prior = vols[-120:-30]
            prior_v = sum(prior) / len(prior) if prior else 0.0
            if prior_v > 0:
                volume_scores.append(_clamp01((recent_v / prior_v - 0.5) / 1.5) * 100)
        med = lambda xs: sorted(xs)[len(xs) // 2] if xs else None
        return {"vol": med(vol_scores), "volume": med(volume_scores)}

    def _oi_energy(self, bybit_tickers, now: float) -> Optional[float]:
        deltas = []
        for sym, tick in (bybit_tickers or {}).items():
            oi = float((tick or {}).get("open_interest", 0.0) or 0.0)
            if oi <= 0:
                continue
            hist = self._oi_hist.setdefault(sym, deque())
            hist.append((now, oi))
            while hist and hist[0][0] < now - _OI_KEEP_S:
                hist.popleft()
            base = None
            for ts, v in hist:
                if ts >= now - _OI_WINDOW_S:
                    break
                base = v
            if base and base > 0:
                deltas.append(abs(oi - base) / base * 100.0)
        if not deltas:
            return None
        return _clamp01(sorted(deltas)[len(deltas) // 2] / 2.0) * 100

    def _funding_energy(self, bybit_tickers) -> Optional[float]:
        rates = [float((t or {}).get("funding_rate", 0.0) or 0.0)
                 for t in (bybit_tickers or {}).values()]
        rates = [r for r in rates if r != 0.0]
        if len(rates) < 5:
            return None
        mu = sum(rates) / len(rates)
        stdev_bps = math.sqrt(sum((r - mu) ** 2 for r in rates)
                              / (len(rates) - 1)) * 1e4
        return _clamp01(stdev_bps / 20.0) * 100

    def _liq_energy(self, now: float) -> Optional[float]:
        events = liq_phase_engine._events.get("")
        if events is None:
            return None
        n = sum(1 for ev in events if ev.timestamp >= now - _LIQ_WINDOW_S)
        return _clamp01(n / 12.0) * 100

    def _chop_fraction(self, day_type_classifier) -> float:
        if day_type_classifier is None:
            return 0.0
        states = getattr(day_type_classifier, "_state", {})
        classified = [s for s in states.values()
                      if s.day_type.value != "unknown"]
        if not classified:
            return 0.0
        chops = sum(1 for s in classified if s.day_type.value == "chop")
        return chops / len(classified)

    # ── Composite ─────────────────────────────────────────────────────────

    def compute(self, candle_buffers, bybit_tickers,
                day_type_classifier=None, now: Optional[float] = None
                ) -> Dict[str, Any]:
        now = now if now is not None else time.time()
        components: Dict[str, Optional[float]] = {}
        components.update(self._vol_volume_energy(candle_buffers))
        components["oi"] = self._oi_energy(bybit_tickers, now)
        components["funding"] = self._funding_energy(bybit_tickers)
        components["liq"] = self._liq_energy(now)

        present = {k: v for k, v in components.items() if v is not None}
        if not present:
            self._latest = {"energy": None, "raw_energy": None,
                            "components": components, "chop_fraction": 0.0,
                            "ts": now}
            return self._latest

        wsum = sum(_WEIGHTS[k] for k in present)
        raw = sum(present[k] * _WEIGHTS[k] for k in present) / wsum
        chop = self._chop_fraction(day_type_classifier)
        energy = raw * (1.0 - 0.4 * chop)

        self._latest = {"energy": energy, "raw_energy": raw,
                        "components": components, "chop_fraction": chop,
                        "ts": now}
        return self._latest

    # ── Dreamer interface ─────────────────────────────────────────────────

    def oi_change_pct(self, symbol: str, window_s: float,
                      now: Optional[float] = None) -> Optional[float]:
        """Signed OI change % over window for one symbol (explosive scanner)."""
        now = now if now is not None else time.time()
        hist = self._oi_hist.get(symbol)
        if not hist:
            return None
        cur = hist[-1][1]
        base = None
        for ts, v in hist:
            if ts >= now - window_s:
                break
            base = v
        if not base or base <= 0:
            return None
        return (cur - base) / base * 100.0
