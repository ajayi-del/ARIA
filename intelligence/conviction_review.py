"""Conviction Review — the cortex for the position-abandon reflex.

Replaces the v1 binary clock (age > 1800s AND ROE < -2.0% → market-close) with
a thesis-tested, regime-conditional verdict. Pure module, zero I/O: the loop in
main.py injects every lookup. Book grounding for each rule (2026-08-22 audit of
12 conviction_decay abandons: actual -$8.10 vs hold-to-stop counterfactual
+$2.77 — the exit class had negative expectancy of ~$10.9/day):

- Raschke & Connors, *Street Smarts* ("time stops"): a time-stop tests the
  THESIS — setup invalidated? — never the calendar alone, and it exists for
  flat going-nowhere positions, not for positions bleeding toward an intact
  stop. That is what the stop is for. Wired twice: (a) a same-direction
  guardian-passed signal inside the grace window means the thesis is ALIVE —
  hold; (b) a fresh opposite-direction signal while the trend-day verdict
  locks AGAINST the position is thesis INVERSION — the strongest exit signal
  the tape offers — abandon without waiting for the full grace.
- Lo, *Adaptive Markets* (ch. on regime-conditional efficiency): signal
  re-confirmation latency stretches in trending regimes. All 6 costly
  abandons in the audit were trend-aligned longs; all 4 saves were shorts.
  Grace = base × aligned_mult when the trend-day verdict is aligned, base
  otherwise. The same verdict gates entries and exits — one source of truth.
- Carver, *Systematic Trading* (forecast/time consistency): no binary cliff
  at an arbitrary price boundary. The "bleeding" band scales with ATR15:
  adverse >= max(0.4%, atr_noise_mult × ATR) — v1's −2% ROE at 5x ≈ 0.4%
  price is preserved as the floor, high-ATR alts get the wider band their
  noise requires.
- Chan, *Quantitative Trading* (half-life, ch. 4): a position must be held at
  least its recovery half-life before judgment. No fake hardcoded half-life
  here — the aligned multiplier is the stand-in until the shadow-journal
  gate_accuracy rows for gate "conviction_decay" (n>=30) measure the real
  time-to-recovery and tune the multiplier by data.
- Van Tharp, *Trade Your Way to Financial Freedom* (exits ch.): every exit is
  measured against the do-nothing-to-stop baseline. Every abandon opens a
  "continue holding" counterfactual shadow record carrying the REAL bracket
  stop (shadow_journal.record_exit_counterfactual) — stopped/won_4h/won_24h
  then answer: did exiting beat holding to the stop?

v1 defects removed here (not patched around): the 60-min winner grace was
unreachable (the ROE gate excludes all winners — removing it is
behavior-identical); the "no supporting signal" log claim was never tested
(_last_signal_ts was read and never used); the fire condition is now
direction-aware — an opposite-direction signal never counts as support.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

BASE_GRACE_S = 1800.0          # v1 loser grace — the unchanged base
MIN_ADVERSE_PCT = 0.004        # v1: ROE -2% at 5x ≈ 0.4% adverse price (fee-band floor)
V1_ROE_GATE_PCT = -2.0         # kill-switch branch reproduces v1 exactly
INVERSION_MIN_AGE_S = 900.0    # never inversion-kill a fresh position (2026-07-25 lesson)
INVERSION_WINDOW_S = 900.0     # opposite signal must be this fresh to count as inversion
SWING_TRADE_TYPES = frozenset({"aster_swing"})   # own manager, 8h doctrine


@dataclass
class PositionSnapshot:
    symbol: str
    side: str                    # "long" | "short"
    upnl: float
    entry: float
    size: float
    age_s: float
    initial_margin: float = 0.0
    trade_type: str = ""
    atr_pct: Optional[float] = None   # ATR15 / price; None = unknown


@dataclass
class Verdict:
    abandon: bool
    reason: str
    grace_s: float


def adverse_pct(snap: PositionSnapshot) -> float:
    """Adverse price excursion as a fraction of entry — leverage-independent,
    so the fee-band argument (a price-scale quantity) stays on price scale."""
    if snap.upnl >= 0 or snap.entry <= 0 or snap.size <= 0:
        return 0.0
    return -snap.upnl / (snap.entry * snap.size)


def noise_band_pct(snap: PositionSnapshot, atr_noise_mult: float) -> float:
    band = MIN_ADVERSE_PCT
    if snap.atr_pct and snap.atr_pct > 0:
        band = max(band, atr_noise_mult * snap.atr_pct)
    return band


def atr_pct_from_candles(candles: Sequence, period: int = 14) -> Optional[float]:
    """ATR/price from a candle sequence (newest last, needs period+1). Pure —
    the loop falls back to the flat floor when this returns None."""
    try:
        cs = list(candles)[-(period + 1):]
        if len(cs) < period + 1:
            return None
        trs = []
        for i in range(1, len(cs)):
            c, p = cs[i], cs[i - 1]
            trs.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
        px = float(cs[-1].close)
        return (sum(trs) / len(trs)) / px if px > 0 else None
    except Exception:
        return None


def abandonment_verdict(
    snap: PositionSnapshot,
    *,
    now: float,
    trend_verdict: str = "unknown",       # "aligned" | "counter" | "unknown"
    last_same_dir_ts: float = 0.0,        # guardian-passed same-direction signal ts
    last_opp_dir_ts: float = 0.0,         # guardian-passed opposite-direction signal ts
    aligned_mult: float = 4.0,
    atr_noise_mult: float = 0.5,
    inversion_enabled: bool = True,
    v2_enabled: bool = True,
) -> Verdict:
    if not v2_enabled:
        # v1 bit-for-bit: age > 1800s AND ROE < -2.0% → abandon. The v1 60-min
        # winner branch was unreachable (the ROE gate excludes all winners), so
        # its removal is behavior-identical.
        roe = (snap.upnl / snap.initial_margin * 100.0) if snap.initial_margin > 0 else 0.0
        if roe > V1_ROE_GATE_PCT:
            return Verdict(False, "v1_hold_roe_band", BASE_GRACE_S)
        if snap.age_s > BASE_GRACE_S:
            return Verdict(True, "v1_abandon", BASE_GRACE_S)
        return Verdict(False, "v1_hold_young", BASE_GRACE_S)

    grace = BASE_GRACE_S * aligned_mult if trend_verdict == "aligned" else BASE_GRACE_S

    if snap.trade_type in SWING_TRADE_TYPES:
        return Verdict(False, "swing_class_exempt", grace)

    if adverse_pct(snap) < noise_band_pct(snap, atr_noise_mult):
        return Verdict(False, "hold_noise_band", grace)

    # ── bleeding beyond the noise band below this line ──────────────────────
    if last_same_dir_ts > 0 and now - last_same_dir_ts <= grace:
        return Verdict(False, "hold_signal_reconfirmed", grace)

    if (inversion_enabled
            and snap.age_s >= INVERSION_MIN_AGE_S
            and trend_verdict == "counter"
            and last_opp_dir_ts > 0
            and now - last_opp_dir_ts <= INVERSION_WINDOW_S):
        return Verdict(True, "thesis_inversion", grace)

    if snap.age_s <= BASE_GRACE_S:
        return Verdict(False, "hold_young", grace)
    if snap.age_s <= grace:
        return Verdict(False, "hold_aligned_grace", grace)
    return Verdict(True, "signal_absent", grace)
