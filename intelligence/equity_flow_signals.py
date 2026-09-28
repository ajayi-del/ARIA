"""
intelligence/equity_flow_signals.py — Axiom 7: equity-flow signal plane
(pure brain + thin publisher, 2026-09-26 Governor directive).

SoDEX equity perps as LEADING indicators for BTC/crypto decisions:
COIN (the crypto-beta equity), MSTR (244,800 BTC on balance sheet — the
institutional squeeze proxy), GOOGL/AMZN (big-tech rotation thermometer).

Doctrine (Governor's spec):
  1. COIN-as-BTC-lead — coin_change_pct > btc_change_pct + 1.0 arms the
     BTC long signal, window 3-12h. Confidence = lead_spread /
     max(|btc_change_pct|, 1.0) clamped [0,1].
     ADAPTATION (documented, intent preserved): the Governor's formula
     divided by the raw BTC change — that is a division-by-zero at flat
     BTC and a sign-inversion on negative BTC. The max(|x|, 1.0)
     denominator keeps the "confidence rises with spread, deflated when
     BTC itself is already moving" intent while staying bounded and
     sign-stable.
  2. MSTR squeeze — mstr_funding_current_bps > avg × 1.5 (avg > 0) arms
     btc_momentum_signal (institutional squeeze building). avg <= 0 or
     None = abstain (never invent a funding state).
  3. Rotation — mean(googl, amzn) < -0.5 AND coin > 0 arms
     rotation_signal (capital rotating big-tech → crypto) with a bounded
     size_tilt suggestion 1.15 for Tier-2+ crypto longs. The sizing-chain
     consumption is a later splice.
  4. Composite — equity_flow_verdict(...) -> EquityFlowVerdict (frozen).
     Any None input abstains its own leg only. Never raises, never
     invents.
  5. Publisher — the ONLY non-pure piece; all I/O injected (set_param
     callable + clock). Only armed signals are written; clearing is TTL
     expiry (param_store doctrine).
  6. Kill switch — env EQUITY_FLOW_SIGNALS_ENABLED default "true"; false
     -> publish() no-ops and the verdict is all-neutral with notes
     "kill_switch".

Department-template shape (docs/DEPARTMENT_TEMPLATE.md): all market reads
arrive as arguments; decisions leave as frozen verdicts. main.py owns the
I/O; the wiring is a later phase (coordinator-owned).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Callable, Optional, Tuple


# ── Kill switch ───────────────────────────────────────────────────────────────

def equity_flow_signals_enabled() -> bool:
    """Module-level kill switch (env EQUITY_FLOW_SIGNALS_ENABLED, default
    true). False = publish() no-ops, verdict returns all-neutral."""
    return os.environ.get("EQUITY_FLOW_SIGNALS_ENABLED", "true").strip().lower() in (
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

LEAD_SPREAD_MIN = 1.0            # COIN must lead BTC by > +1.0 pct
WINDOW_MIN_S = 10800.0           # 3h
WINDOW_MAX_S = 43200.0           # 12h
MSTR_SQUEEZE_MULT = 1.5          # current funding > avg x 1.5 = squeeze
ROTATION_TECH_DROP = -0.5        # mean(GOOGL, AMZN) < -0.5 pct
ROTATION_TILT = 1.15             # bounded size tilt for Tier-2+ crypto longs
DEFAULT_TILT = 1.0
MOMENTUM_TTL_S = 4 * 3600.0      # equity_flow:btc_momentum TTL
ROTATION_TTL_S = 4 * 3600.0      # equity_flow:rotation_tilt TTL


# ── 1. COIN-as-BTC-lead ───────────────────────────────────────────────────────

def coin_lead_signal(coin_change_pct, btc_change_pct) -> Tuple[bool, float, Optional[float]]:
    """(armed, confidence, window_s). Armed when coin leads btc by more
    than LEAD_SPREAD_MIN. Confidence = lead_spread / max(|btc|, 1.0)
    clamped [0,1] (documented flat-BTC div-guard adaptation). Window
    scales with confidence across [3h, 12h]; None when not armed.
    Any None input abstains."""
    coin, btc = _f(coin_change_pct), _f(btc_change_pct)
    if coin is None or btc is None:
        return (False, 0.0, None)
    spread = coin - btc
    if not (spread > LEAD_SPREAD_MIN):
        return (False, 0.0, None)
    conf = spread / max(abs(btc), 1.0)
    conf = max(0.0, min(1.0, conf))
    window_s = WINDOW_MIN_S + conf * (WINDOW_MAX_S - WINDOW_MIN_S)
    return (True, conf, window_s)


# ── 2. MSTR squeeze ───────────────────────────────────────────────────────────

def mstr_squeeze_signal(mstr_funding_current_bps, mstr_funding_avg_bps) -> bool:
    """Institutional squeeze building: current funding > avg x 1.5 with a
    strictly positive avg. avg <= 0 or None / current None = abstain
    (never invent a funding state)."""
    cur, avg = _f(mstr_funding_current_bps), _f(mstr_funding_avg_bps)
    if cur is None or avg is None or avg <= 0:
        return False
    return cur > avg * MSTR_SQUEEZE_MULT


# ── 3. Rotation (big-tech -> crypto) ─────────────────────────────────────────

def rotation_signal(googl_change_pct, amzn_change_pct, coin_change_pct) -> bool:
    """Both legs must hold: mean(GOOGL, AMZN) < -0.5 (big-tech bleeding)
    AND COIN > 0 (crypto catching the bid). Any None input abstains."""
    g, a, c = _f(googl_change_pct), _f(amzn_change_pct), _f(coin_change_pct)
    if g is None or a is None or c is None:
        return False
    return ((g + a) / 2.0) < ROTATION_TECH_DROP and c > 0.0


# ── 4. Composite verdict ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class EquityFlowVerdict:
    btc_long_signal: bool = False
    btc_long_confidence: float = 0.0
    btc_long_window_s: Optional[float] = None   # 10800-43200 when armed
    btc_momentum_squeeze: bool = False
    rotation_signal: bool = False
    size_tilt: float = DEFAULT_TILT             # 1.15 only when rotation armed
    notes: Tuple[str, ...] = ()


def equity_flow_verdict(coin_change_pct=None, btc_change_pct=None,
                        googl_change_pct=None, amzn_change_pct=None,
                        mstr_funding_current_bps=None, mstr_funding_avg_bps=None,
                        now_ts=None) -> EquityFlowVerdict:
    """The composite Axiom-7 verdict. Each leg reads its own inputs and
    abstains independently on None; legs never contaminate each other.
    Kill switch off = all-neutral with notes ("kill_switch",). now_ts
    rides the signature for the splice plane (clock injection); the
    classification rules do not consume it."""
    if not equity_flow_signals_enabled():
        return EquityFlowVerdict(notes=("kill_switch",))

    notes = []
    armed, conf, window_s = coin_lead_signal(coin_change_pct, btc_change_pct)
    if armed:
        notes.append("coin_leads_btc")
    squeeze = mstr_squeeze_signal(mstr_funding_current_bps, mstr_funding_avg_bps)
    if squeeze:
        notes.append("mstr_squeeze")
    rotation = rotation_signal(googl_change_pct, amzn_change_pct, coin_change_pct)
    if rotation:
        notes.append("bigtech_to_crypto_rotation")
    tilt = ROTATION_TILT if rotation else DEFAULT_TILT

    return EquityFlowVerdict(
        btc_long_signal=armed,
        btc_long_confidence=conf,
        btc_long_window_s=window_s,
        btc_momentum_squeeze=squeeze,
        rotation_signal=rotation,
        size_tilt=tilt,
        notes=tuple(notes),
    )


# ── 5. Publisher (the only non-pure piece; all I/O injected) ─────────────────

class EquityFlowPublisher:
    """Thin param_store publisher. set_param is the param_store-pattern
    callable (key, value, ttl); clock is injectable for tests. Only armed
    signals are written — clearing is handled by TTL expiry (param_store
    doctrine), so nothing is ever explicitly deleted."""

    def __init__(self, set_param: Callable[[str, object, float], object],
                 clock: Callable[[], float] = time.time):
        self._set_param = set_param
        self._clock = clock

    def publish(self, verdict: EquityFlowVerdict) -> int:
        """Write the armed keys. Returns the number of keys written.
        Kill switch off / any set_param failure = no-op (never raises)."""
        if not equity_flow_signals_enabled():
            return 0
        if verdict is None:
            return 0
        written = 0
        try:
            ts = self._clock()
        except Exception:
            ts = None
        try:
            if verdict.btc_long_signal:
                self._set_param("equity_flow:btc_long",
                                {"confidence": verdict.btc_long_confidence,
                                 "ts": ts},
                                verdict.btc_long_window_s or WINDOW_MIN_S)
                written += 1
            if verdict.btc_momentum_squeeze:
                self._set_param("equity_flow:btc_momentum",
                                {"armed": True, "ts": ts},
                                MOMENTUM_TTL_S)
                written += 1
            if verdict.rotation_signal:
                self._set_param("equity_flow:rotation_tilt",
                                {"size_tilt": verdict.size_tilt, "ts": ts},
                                ROTATION_TTL_S)
                written += 1
        except Exception:
            pass  # publisher must never raise into the caller's loop
        return written
