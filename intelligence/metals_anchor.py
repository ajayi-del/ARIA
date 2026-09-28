"""
intelligence/metals_anchor.py — E5 metals macro-anchor brain (pure, zero-I/O).

2026-09-26 (Governor directive): metals are the barbell's left bar — slow
regime positions, not scalps. This module owns the XAUT regime anchor, the
silver/gold ratio pair, and the copper risk-on modifier. It does NOT classify
regimes: the macro regime direction arrives as an argument (relative_strength
owns classification; EXTERNAL_MACRO_SOURCES already feeds copper via Yahoo
HG=F as regime context). Department-template shape
(docs/DEPARTMENT_TEMPLATE.md): all market reads arrive as arguments; decisions
leave as frozen verdicts. main.py owns the I/O; the wiring is a later phase
(coordinator-owned).

Doctrine spine:
  1. XAUT ANCHOR — a regime position exists only when a macro regime is
     DECLARED (regime_direction "long"/"short"). No regime, no anchor.
     Total margin exposure is capped at 15% of account equity, leverage at
     4x (Governor amendment 2026-09-26 — was 2x), deployed in 3 tranches
     of 5% equity each.
  2. SILVER/GOLD PAIR — gold/silver ratio > 85 arms LONG silver / SHORT gold
     1:1 notional; exit when ratio < 75. The 75-85 band is a hysteresis
     dead zone so the pair never flaps at 85±1.
  3. COPPER MODIFIER — copper uptrend confirms risk-on (+0.15 confidence to
     pipeline-A/C crypto risk verdicts), downtrend −0.20, range 0.0. The
     output is a MODIFIER, never a trade.
  4. FREQUENCY DISCIPLINE — minimum 3 days between XAUT anchor direction
     changes (1-3 trades/week class).
  5. Kill switch — env METALS_ANCHOR_ENABLED default "true"; false ->
     metals_verdict returns all-neutral with notes ("kill_switch",).

Fail-closed law: nothing in this module raises on bad input — every
function fails toward None / abstain / 0.0.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional, Tuple


# ── Kill switch ───────────────────────────────────────────────────────────────

def metals_anchor_enabled() -> bool:
    """Module-level kill switch (env METALS_ANCHOR_ENABLED, default true).
    False = metals_verdict returns all-neutral with notes ("kill_switch",)."""
    return os.environ.get("METALS_ANCHOR_ENABLED", "true").strip().lower() in (
        "1", "true", "yes", "on")


# ── Shared helpers ────────────────────────────────────────────────────────────

def _f(x) -> Optional[float]:
    """Coerce to float; None on any garbage (fail-closed idiom)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return v


# ── Doctrine constants ────────────────────────────────────────────────────────

XAUT_CAP_FRACTION = 0.15          # max XAUT margin exposure = 15% of equity
XAUT_MAX_LEVERAGE = 4.0           # anchor leverage cap (Governor 2026-09-26: 2.0 -> 4.0)
XAUT_TRANCHES = 3                 # pyramid in 3 tranches
XAUT_TRANCHE_FRACTION = XAUT_CAP_FRACTION / XAUT_TRANCHES   # 5% each

PAIR_ENTER_RATIO = 85.0           # gold/silver ratio > 85 -> arm the pair
PAIR_EXIT_RATIO = 75.0            # ratio < 75 -> exit (75-85 = hysteresis)

COPPER_UP_MODIFIER = 0.15         # risk-on confirm
COPPER_DOWN_MODIFIER = -0.20      # risk-off
COPPER_RANGE_MODIFIER = 0.0

ANCHOR_MIN_CHANGE_INTERVAL_S = 3.0 * 86400.0   # 3 days between direction flips


# ── 1. XAUT regime anchor ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class XautSignal:
    direction: str                # "long" | "short" (the declared regime)
    margin_cap_usd: float         # remaining margin headroom under the 15% cap
    leverage_cap: float           # <= 2.0
    tranche_size_usd: float       # 5% of equity per tranche


def _norm_direction(regime_direction) -> Optional[str]:
    """Normalize the consumed regime direction; anything not long/short is
    no-regime (abstain — the brain never classifies, it consumes)."""
    try:
        d = str(regime_direction).strip().lower()
    except Exception:
        return None
    if d in ("long", "up", "bull", "bullish"):
        return "long"
    if d in ("short", "down", "bear", "bearish"):
        return "short"
    return None


def xaut_anchor(regime_direction, account_equity,
                current_xaut_exposure=0.0) -> Optional[XautSignal]:
    """Regime anchor verdict. A position exists only when a macro regime is
    declared; remaining capacity = 15% equity cap minus current exposure.
    None on: no declared regime, bad equity, or cap already fully deployed."""
    direction = _norm_direction(regime_direction)
    if direction is None:
        return None
    equity = _f(account_equity)
    if equity is None or equity <= 0:
        return None
    exposure = _f(current_xaut_exposure) or 0.0
    remaining = equity * XAUT_CAP_FRACTION - max(0.0, exposure)
    if remaining <= 0:
        return None                     # cap fully deployed — stand down
    return XautSignal(
        direction=direction,
        margin_cap_usd=remaining,
        leverage_cap=XAUT_MAX_LEVERAGE,
        tranche_size_usd=equity * XAUT_TRANCHE_FRACTION,
    )


def tranche_verdict(tranches_deployed) -> Optional[int]:
    """Which tranche to deploy next (1-3), or None when fully deployed.
    Invalid / negative / >= 3 -> None (fail-closed)."""
    try:
        n = int(tranches_deployed)
    except (TypeError, ValueError):
        return None
    if n < 0 or n >= XAUT_TRANCHES:
        return None
    return n + 1


# ── 2. Silver/gold ratio pair ─────────────────────────────────────────────────

@dataclass(frozen=True)
class PairSignal:
    action: str                   # "enter" | "exit"
    ratio: float
    silver_side: str = "long"
    gold_side: str = "short"
    silver_notional_usd: float = 0.0
    gold_notional_usd: float = 0.0


def silver_gold_pair(silver_price, gold_price, currently_open=False,
                     notional_usd=0.0) -> Optional[PairSignal]:
    """Ratio pair with hysteresis. Not open + ratio > 85 -> ENTER long silver /
    short gold 1:1 notional. Open + ratio < 75 -> EXIT. The 75-85 band is a
    dead zone (no signal either way) so the pair never flaps at the threshold.
    Bad prices -> None (fail-closed)."""
    s, g = _f(silver_price), _f(gold_price)
    if s is None or g is None or s <= 0 or g <= 0:
        return None
    ratio = g / s
    notional = max(0.0, _f(notional_usd) or 0.0)
    if currently_open:
        if ratio < PAIR_EXIT_RATIO:
            return PairSignal(action="exit", ratio=ratio,
                              silver_notional_usd=notional,
                              gold_notional_usd=notional)
        return None                     # hold — hysteresis band included
    if ratio > PAIR_ENTER_RATIO:
        return PairSignal(action="enter", ratio=ratio,
                          silver_notional_usd=notional,
                          gold_notional_usd=notional)
    return None


# ── 3. Copper regime modifier (a modifier, never a trade) ─────────────────────

def copper_regime_modifier(copper_trend) -> float:
    """Copper as the risk-on thermometer: uptrend +0.15 confidence to
    pipeline-A/C crypto risk verdicts, downtrend -0.20, range 0.0.
    Unknown trend reads as range (0.0 — abstain, never invent a state)."""
    try:
        t = str(copper_trend).strip().lower()
    except Exception:
        return COPPER_RANGE_MODIFIER
    if t in ("up", "uptrend", "bull", "bullish", "risk_on"):
        return COPPER_UP_MODIFIER
    if t in ("down", "downtrend", "bear", "bearish", "risk_off"):
        return COPPER_DOWN_MODIFIER
    return COPPER_RANGE_MODIFIER


# ── 4. Frequency discipline ───────────────────────────────────────────────────

def anchor_change_ok(last_change_ts, now) -> bool:
    """Minimum 3 days between XAUT anchor direction changes (1-3 trades/week
    class). last_change_ts None = no prior change on record -> allowed (the
    first anchor must be reachable). now missing -> False (unknown clock,
    stay defensive)."""
    n = _f(now)
    if n is None:
        return False
    last = _f(last_change_ts)
    if last is None:
        return True
    return (n - last) >= ANCHOR_MIN_CHANGE_INTERVAL_S


# ── 5. Composite verdict ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class MetalsVerdict:
    xaut_signal: Optional[XautSignal] = None
    silver_gold_pair: Optional[PairSignal] = None
    copper_confidence: float = 0.0
    notes: Tuple[str, ...] = ()


def metals_verdict(regime_direction=None, account_equity=None,
                   current_xaut_exposure=0.0,
                   silver_price=None, gold_price=None,
                   pair_open=False, pair_notional_usd=0.0,
                   copper_trend=None, now_ts=None) -> MetalsVerdict:
    """The composite E5 verdict. Each leg reads its own inputs and abstains
    independently on None; legs never contaminate each other. Kill switch
    off = all-neutral with notes ("kill_switch",). now_ts rides the
    signature for the splice plane (clock injection); the classification
    rules do not consume it."""
    if not metals_anchor_enabled():
        return MetalsVerdict(notes=("kill_switch",))

    notes = []
    xaut = xaut_anchor(regime_direction, account_equity, current_xaut_exposure)
    if xaut is not None:
        notes.append(f"xaut_anchor_{xaut.direction}")
    pair = silver_gold_pair(silver_price, gold_price, pair_open,
                            pair_notional_usd)
    if pair is not None:
        notes.append(f"silver_gold_pair_{pair.action}")
    copper_mod = copper_regime_modifier(copper_trend)
    if copper_mod > 0:
        notes.append("copper_risk_on")
    elif copper_mod < 0:
        notes.append("copper_risk_off")

    return MetalsVerdict(
        xaut_signal=xaut,
        silver_gold_pair=pair,
        copper_confidence=copper_mod,
        notes=tuple(notes),
    )
