"""intelligence/roe_ratchet.py — Peak-ROE mechanical stop ratchet (pure brain).

2026-09-04 (operator directive): "track highest profit ROE and chase it
mechanically instead of time — several profits are given back. The moment a
trade hits a threshold ARIA resets its stop. If ARIA bumps to a 9% ROE the
stop should automatically increase, even while the trade is on. That way we
give back less and rotate capital faster."

The ATR trail (main.py _trailing_stop_loop) activates at 1.5-3× ATR — with a
~1% ATR that is +15-30% ROE at 10x before ANY protection tightens. The band
between entry and trail activation is where profits round-trip. This brain
keys on the house ROE (pnl / initial_margin × 100, the treasury formula) and
ratchets the stop up a fixed ladder of the PEAK ROE — tighten-only, the
caller enforces that against the live stop.

Ladder (rungs crossed by the peak, lock applies to the peak):
  ≥  3%  → breakeven + fees/noise buffer (the trade has been PROVED — it
           cannot go red beyond the buffer)
  ≥  6%  → stop locks 45% of peak ROE
  ≥  9%  → stop locks 60% of peak ROE (the operator's example)
  ≥ 15%  → stop locks 70% of peak ROE, trailing — as the peak grows the
           locked level grows with it (30% giveback trail; treasury-managed
           clusters use their own 40% lock and are skipped by the caller)

Freeman-Shor / LeBeau: winners must not become losers, and give-back is the
disposition effect running in reverse — bank mechanically, never by feel.
Aronson: the ladder is fixed in code; only the breakeven rung/buffer and the
master switch are operator knobs — bounded degrees of freedom.

2026-09-06 (CEO spec D11 / M9): the ladder is ROE-denominated but the market
is volatility-denominated — ROE = price_move × leverage, so every rung
silently tightens as leverage rises (measured 12.5× dispersion in ATR units
across one day's book). The ATR floor below re-denominates the stop: a
ratchet stop may never sit closer to the mark than min_stop_dist_atr × ATR.
atr=None reproduces the legacy function bit-for-bit (kill-switch path).
"""
from __future__ import annotations

from typing import Optional

BE_RUNG_PCT = 3.0
BE_BUFFER_PCT = 0.15          # price buffer beyond breakeven (fees + noise)
MID_RUNG_PCT = 6.0
MID_LOCK_FRAC = 0.45
HIGH_RUNG_PCT = 9.0
HIGH_LOCK_FRAC = 0.60
RUNNER_RUNG_PCT = 15.0
RUNNER_LOCK_FRAC = 0.70
MIN_STOP_DIST_ATR = 1.0       # INHERITED from config.trail_distance_atr (1.0):
                              # the ratchet may never be tighter than the ATR
                              # trail at its tightest. Not a fitted constant.


def roe_pct(side: str, entry_price: float, mark_price: float,
            leverage: float) -> Optional[float]:
    """House ROE% = price move % × leverage (pnl / initial_margin × 100)."""
    try:
        _e, _m, _l = float(entry_price), float(mark_price), float(leverage)
    except (TypeError, ValueError):
        return None
    if _e <= 0 or _m <= 0 or _l <= 0:
        return None
    _move = (_m - _e) / _e if side == "long" else (_e - _m) / _e
    return _move * _l * 100.0


def ratchet_target_stop(side: str, entry_price: float, mark_price: float,
                        peak_roe: float, leverage: float,
                        be_rung_pct: float = BE_RUNG_PCT,
                        be_buffer_pct: float = BE_BUFFER_PCT,
                        atr: Optional[float] = None,
                        min_stop_dist_atr: float = MIN_STOP_DIST_ATR) -> Optional[float]:
    """Target stop price from the peak-ROE ladder, or None (below the first
    rung / degenerate input). Tighten-only vs the live stop is the CALLER's
    job. The stop is always capped to the mark side (long ≤ mark, short ≥
    mark) — a computed stop already crossed by the mark returns None and the
    software-stop guardian owns the exit. When atr is provided the stop is
    additionally floored at min_stop_dist_atr × ATR from the mark (D11)."""
    try:
        _e, _m, _l, _p = (float(entry_price), float(mark_price),
                          float(leverage), float(peak_roe))
    except (TypeError, ValueError):
        return None
    if _e <= 0 or _m <= 0 or _l <= 0 or side not in ("long", "short"):
        return None
    if _p < be_rung_pct:
        return None

    if _p >= RUNNER_RUNG_PCT:
        _lock = RUNNER_LOCK_FRAC
    elif _p >= HIGH_RUNG_PCT:
        _lock = HIGH_LOCK_FRAC
    elif _p >= MID_RUNG_PCT:
        _lock = MID_LOCK_FRAC
    else:
        _lock = None   # breakeven rung

    if _lock is None:
        _buf = be_buffer_pct / 100.0
        _stop = _e * (1.0 + _buf) if side == "long" else _e * (1.0 - _buf)
    else:
        _locked_move = (_p * _lock) / (_l * 100.0)   # ROE → price fraction
        _stop = (_e * (1.0 + _locked_move) if side == "long"
                 else _e * (1.0 - _locked_move))

    # Volatility floor (D11): never closer to the mark than
    # min_stop_dist_atr × ATR. Skipped entirely when atr is None (legacy).
    if atr and atr > 0 and min_stop_dist_atr > 0:
        _floor = float(atr) * float(min_stop_dist_atr)
        if side == "long":
            _stop = min(_stop, _m - _floor)
        else:
            _stop = max(_stop, _m + _floor)

    if side == "long" and _stop >= _m:
        return None
    if side == "short" and _stop <= _m:
        return None
    return _stop
