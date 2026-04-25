"""
intelligence/regime_engine.py — Regime-First Auto-Adjustment Engine v1.0

Philosophy:
  Kent (Structure First): Regime defines what's tradeable. No valid structure = halt.
  Nietzsche (Sizing is Will to Power): Coherence × cascade × recovery × regime = conviction.

Three tightly-coupled components in one file — sharing state, avoiding circular imports:
  RegimeMultiplierEngine  — sizing multiplier per (symbol, regime) for new entries
  XAUTThermometer         — gold direction as real-time macro compass for crypto sizing
  AutoAdjustmentEngine    — conflict detection + position partial-close triggers
"""

from __future__ import annotations

import time
import structlog
from collections import deque
from dataclasses import dataclass
from typing import Dict, Optional

from intelligence.relative_strength import RegimeState, ASSET_CATEGORIES

logger = structlog.get_logger(__name__)


# ── Asset groupings used by regime rules ──────────────────────────────────────

_CRYPTO_ASSETS = frozenset({
    "BTC-USD", "ETH-USD", "XRP-USD",
    "SOL-USD", "AVAX-USD", "NEAR-USD", "ARB-USD", "OP-USD", "SUI-USD", "MNT-USD",
    "LINK-USD", "AAVE-USD",
    "BNB-USD", "HYPE-USD",
    "TRUMP-USD", "DOGE-USD", "PEPE-USD", "1000PEPE-USD", "BASED-USD",
})

def _size_mult_to_w_rec(size_mult: float) -> float:
    """Map current size_mult to W_rec tier for auto-adjustment weighting."""
    if size_mult >= 1.0:  return 1.0
    if size_mult >= 0.95: return 0.90
    return 0.75


# ── RegimeMultiplierEngine ─────────────────────────────────────────────────────

class RegimeMultiplierEngine:
    """
    Maps (symbol, RegimeState) → float sizing multiplier for new entry candidates.

    Priority order (first match wins, Kent structure-first):
      1. geopolitical_stress — energy 1.5×, XAUT 1.0× (bypass), all else 0×
      2. stagflation_fear    — XAUT 1.5×, energy 0.75×, all else 0×
      3. risk_off conf≥0.7   — XAUT 1.3×, crypto 0.5×, other 0.75×
      4. transitioning       — 0.25× (broken structure), 0.5× (uncertain), 0.75× (present)
      5. confused            — 0.5× (no clear structure)
      6. cex_flow            — cex_ecosystem 1.2×, alt_l1 1.1×, BTC/ETH 0.8×
      7. alt_season          — alt_l1 1.2×, large_cap 0.8×
      8. btc_dominance       — BTC/ETH 1.2×, alt_l1 0.7×
      9. Confidence-based    — leading_cat 1.2× (1.1× low conf), lagging_cat 0.5× (0.7× low)
      10. Default            — 1.0× (no constraint)
    """

    def get_new_entry_multiplier(self, symbol: str, rs: RegimeState) -> float:
        r    = rs.regime
        conf = rs.confidence
        cat  = ASSET_CATEGORIES.get(symbol, "unknown")
        xaut = symbol == "XAUT-USD"

        if r == "geopolitical_stress":
            if cat in ("commodity_energy", "commodity_industrial"):
                mult = 1.5
            elif xaut:
                mult = 1.0   # rotational bypass — gold has its own structure
            else:
                mult = 0.0   # all crypto + equities locked during geopolitical stress
            self._emit(r, symbol, cat, conf, mult, rs)
            return mult

        if r == "stagflation_fear":
            if xaut:
                mult = 1.5
            elif cat == "commodity_energy":
                mult = 0.75  # energy partially viable in stagflation
            else:
                mult = 0.0
            self._emit(r, symbol, cat, conf, mult, rs)
            return mult

        if r == "risk_off" and conf >= 0.7:
            if xaut:  return 1.3
            if symbol in _CRYPTO_ASSETS: return 0.5
            return 0.75

        if r == "transitioning":
            lead, lag = rs.leading_category, rs.lagging_category
            if lead in ("none", "unknown") or lag in ("none", "unknown"):
                return 0.25  # broken structure — extreme caution
            return 0.5 if conf < 0.5 else 0.75

        if r == "confused":
            return 0.5

        if r == "cex_flow":
            if cat == "cex_ecosystem":                       return 1.2
            if cat == "alt_l1":                              return 1.1
            if symbol in ("BTC-USD", "ETH-USD"):             return 0.8
            return 1.0

        if r == "alt_season":
            if cat == "alt_l1":    return 1.2
            if cat == "large_cap": return 0.8
            return 1.0

        if r == "btc_dominance":
            if symbol in ("BTC-USD", "ETH-USD"): return 1.2
            if cat == "alt_l1":                  return 0.7
            return 1.0

        # General confidence-based leading/lagging sector bias
        if conf >= 0.5:
            lead, lag = rs.leading_category, rs.lagging_category
            if cat == lead: return 1.2 if conf >= 0.7 else 1.1
            if cat == lag:  return 0.5 if conf >= 0.7 else 0.7

        return 1.0

    def _emit(self, regime: str, symbol: str, cat: str,
              conf: float, mult: float, rs: RegimeState) -> None:
        if mult == 1.0:
            return
        logger.info(
            "regime_override_active",
            regime_type=regime,
            confidence=round(conf, 3),
            symbol=symbol,
            category=cat,
            leading_sector=rs.leading_category,
            lagging_sector=rs.lagging_category,
            size_multiplier=round(mult, 2),
        )


# ── XAUTThermometer ────────────────────────────────────────────────────────────

class XAUTThermometer:
    """
    Tracks XAUT (gold) direction and coherence as a macro compass for crypto sizing.

    Mechanism:
      XAUT short + coherence ≥ 3.5 → gold falling = risk-on → longs 1.10×, shorts 0.90×
      XAUT long  + coherence ≥ 3.5 → gold rising  = risk-off → longs 0.90×, shorts 1.10×
      coherence < 1.5 or stale > 60min → thermometer off (1.0×)

    Applied only to crypto assets (_CRYPTO_ASSETS). XAUT and commodities bypass it.
    """

    _STALE_S      = 3600.0
    _MIN_COH_ON   = 1.5    # below this: thermometer off
    _MIN_COH_ACT  = 3.5    # below this: no directional amplification

    def __init__(self) -> None:
        self._direction:  str   = "none"
        self._coherence:  float = 0.0
        self._updated_at: float = 0.0

    def update(self, direction: str, coherence: float) -> None:
        """Call whenever a XAUT-USD signal fires, regardless of trade outcome."""
        prev = (self._direction, round(self._coherence, 1))
        self._direction  = direction
        self._coherence  = coherence
        self._updated_at = time.time()
        if (direction, round(coherence, 1)) != prev and coherence >= self._MIN_COH_ON:
            logger.info("xaut_thermometer_updated",
                        direction=direction, coherence=round(coherence, 2))

    def get_crypto_multiplier(self, trade_direction: str, symbol: str) -> float:
        """
        Returns multiplier for a crypto position's size based on gold direction.
        Returns 1.0 for XAUT itself, non-crypto assets, or when thermometer is inactive.
        """
        if symbol == "XAUT-USD" or symbol not in _CRYPTO_ASSETS:
            return 1.0
        if self._coherence < self._MIN_COH_ON:
            return 1.0
        if (time.time() - self._updated_at) > self._STALE_S:
            return 1.0
        if self._direction not in ("long", "short") or self._coherence < self._MIN_COH_ACT:
            return 1.0

        if self._direction == "short":
            # Gold falling = risk-on: amplify crypto longs
            if self._coherence >= 4.0:
                mult = 1.20 if trade_direction == "long" else 0.85
            else:
                mult = 1.10 if trade_direction == "long" else 0.90
        else:
            # Gold rising = risk-off: reduce crypto longs
            if self._coherence >= 4.0:
                mult = 0.80 if trade_direction == "long" else 1.15
            else:
                mult = 0.90 if trade_direction == "long" else 1.10

        logger.info(
            "xaut_thermometer_applied",
            direction=self._direction,
            coherence=round(self._coherence, 2),
            trade_direction=trade_direction,
            symbol=symbol,
            crypto_multiplier=mult,
        )
        return mult

    @property
    def is_active(self) -> bool:
        return (
            self._coherence >= self._MIN_COH_ACT
            and self._direction in ("long", "short")
            and (time.time() - self._updated_at) < self._STALE_S
        )


# ── AutoAdjustmentEngine ───────────────────────────────────────────────────────

@dataclass
class AdjustmentDecision:
    symbol:     str
    action:     str      # "close_full" | "close_pct" | "none"
    close_pct:  float    # fraction of position to close (0.0–1.0)
    reason:     str
    multiplier: float    # final W_c × W_cas × W_rec (capped 1.5)


# Coherence → W_c weight table (Nietzsche: only act on conviction)
_COHERENCE_WC = [
    (9.0, 1.00),
    (7.0, 0.75),
    (6.0, 0.50),
    (5.0, 0.25),
    (0.0, 0.00),
]


def _w_c(coherence: float) -> float:
    for threshold, weight in _COHERENCE_WC:
        if coherence >= threshold:
            return weight
    return 0.0


class AutoAdjustmentEngine:
    """
    Evaluates whether an incoming signal justifies reducing or closing a conflicting
    open position. Returns AdjustmentDecision; the caller executes the close.

    Kent gates (halt all adjustment):
      - Regime structure broken: leading/lagging == "none"/"unknown"
      - time_regime_mult == 0.0 (hard calendar block — earnings, FOMC)
      - size_mult < 0.9 (deep post-earnings recovery)

    Nietzsche sizing (W_c × W_cas × W_rec, cap 1.5):
      W_c:   coherence tier weight (0.25–1.0; 0 below 5.0)
      W_cas: cascade phase urgency (0–1.5)
      W_rec: post-earnings recovery tier (0.75–1.0)

    Safety:
      - 120s flip cooldown per symbol
      - Max 3 adjustments/hour per symbol → 30min halt
    """

    FLIP_COOLDOWN_S    = 120.0
    MAX_FLIPS_PER_HOUR = 3
    FLIP_HALT_S        = 1800.0

    def __init__(self) -> None:
        self._last_adj_ts:       Dict[str, float] = {}
        self._flip_history:      Dict[str, deque] = {}
        self._flip_halted_until: Dict[str, float] = {}

    def can_enter_after_adjustment(self, symbol: str) -> bool:
        """True once the 120s flip cooldown has elapsed since last auto-adjustment."""
        return time.time() - self._last_adj_ts.get(symbol, 0.0) >= self.FLIP_COOLDOWN_S

    def evaluate(
        self,
        symbol:             str,
        signal_direction:   str,
        coherence:          float,
        open_position_side: str,
        cascade_phase:      str,
        cascade_zscore:     float,
        regime_state:       Optional[RegimeState],
        time_regime_mult:   float,
        size_mult:          float,
    ) -> AdjustmentDecision:
        """
        Returns AdjustmentDecision describing what action (if any) to take.
        The caller is responsible for executing the close order.
        """
        _none = AdjustmentDecision(
            symbol=symbol, action="none", close_pct=0.0,
            reason="no_conflict", multiplier=0.0,
        )

        if open_position_side == signal_direction:
            return _none  # Aligned — no conflict to resolve

        # ── Kent gate 1: valid structure required ──────────────────────────────
        if regime_state is not None:
            lead = regime_state.leading_category
            lag  = regime_state.lagging_category
            if lead in ("none", "unknown") or lag in ("none", "unknown"):
                return AdjustmentDecision(symbol=symbol, action="none", close_pct=0.0,
                                          reason="kent_no_structure", multiplier=0.0)

        # ── Kent gate 2: hard calendar block ──────────────────────────────────
        if time_regime_mult == 0.0:
            return AdjustmentDecision(symbol=symbol, action="none", close_pct=0.0,
                                      reason="kent_calendar_block", multiplier=0.0)

        # ── Kent gate 3: size_mult < 0.9 (deep recovery period) ───────────────
        if size_mult < 0.9:
            return AdjustmentDecision(symbol=symbol, action="none", close_pct=0.0,
                                      reason="kent_recovery_too_deep", multiplier=0.0)

        # ── Nietzsche gate: minimum conviction required ────────────────────────
        wc = _w_c(coherence)
        if wc == 0.0:
            return _none  # coherence below 5.0 — not actionable

        # ── Safety: flip rate limits ──────────────────────────────────────────
        now = time.time()

        if now < self._flip_halted_until.get(symbol, 0.0):
            return AdjustmentDecision(symbol=symbol, action="none", close_pct=0.0,
                                      reason="flip_halt_active", multiplier=0.0)

        if not self.can_enter_after_adjustment(symbol):
            remaining = int(self.FLIP_COOLDOWN_S - (now - self._last_adj_ts.get(symbol, 0.0)))
            return AdjustmentDecision(symbol=symbol, action="none", close_pct=0.0,
                                      reason=f"flip_cooldown:{remaining}s", multiplier=0.0)

        hist = self._flip_history.setdefault(symbol, deque(maxlen=20))
        recent = [t for t in hist if now - t < 3600]
        if len(recent) >= self.MAX_FLIPS_PER_HOUR:
            self._flip_halted_until[symbol] = now + self.FLIP_HALT_S
            logger.warning("auto_adj_flip_rate_halt", symbol=symbol,
                           flips_in_hour=len(recent), halt_s=self.FLIP_HALT_S)
            return AdjustmentDecision(symbol=symbol, action="none", close_pct=0.0,
                                      reason="max_flips_halt", multiplier=0.0)

        # ── W_cas: cascade phase urgency multiplier ────────────────────────────
        _ph = cascade_phase.lower()
        if cascade_zscore > 3.0 or "expansion" in _ph:
            wcas = 1.5
        elif cascade_zscore >= 1.5 or "trigger" in _ph or "momentum" in _ph or "primed" in _ph:
            wcas = 1.0
        elif "aftermath" in _ph or "exhaustion" in _ph or "blocked" in _ph:
            wcas = 0.5
        elif "quiet" in _ph:
            wcas = 0.0
        else:
            wcas = 1.0  # idle or unrecognised — default normal urgency

        # ── W_rec: post-earnings recovery tier ────────────────────────────────
        wrec = _size_mult_to_w_rec(size_mult)

        # Final multiplier: W_c × W_cas × W_rec, cap 1.5
        final = min(1.5, wc * wcas * wrec)

        if final <= 0.0:
            return AdjustmentDecision(symbol=symbol, action="none", close_pct=0.0,
                                      reason="cascade_quiet_suppressed", multiplier=0.0)

        close_pct = min(1.0, final)
        action    = "close_full" if close_pct >= 0.95 else "close_pct"

        # Record this adjustment
        hist.append(now)
        self._last_adj_ts[symbol] = now

        logger.info(
            "auto_adjustment_decision",
            symbol=symbol,
            signal_direction=signal_direction,
            position_side=open_position_side,
            coherence=round(coherence, 2),
            cascade_phase=cascade_phase,
            cascade_zscore=round(cascade_zscore, 2),
            w_c=wc, w_cas=wcas, w_rec=wrec,
            final_mult=round(final, 3),
            action=action,
            close_pct=round(close_pct, 2),
        )

        return AdjustmentDecision(
            symbol=symbol,
            action=action,
            close_pct=close_pct,
            reason=f"conflict:coh={coherence:.1f}:mult={final:.2f}",
            multiplier=final,
        )
