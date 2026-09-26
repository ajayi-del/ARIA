"""intelligence/adl_lens.py — SoDEX ADL risk-and-signal lens (pure brain, zero I/O).

Governor doctrine 2026-09-26, confirmed SoDEX ADL mechanics. SoDEX liquidation
runs THREE stages:

  1. Order-book liquidation — market orders against the book, NO liquidation
     fee. This is the chart "wick".
  2. Backstop — engages below 2/3 of maintenance margin. This is the
     "cascade".
  3. ADL (auto-deleveraging) — fires when the underwater account's value goes
     NEGATIVE and the backstop cannot absorb it. Positions on the WINNING side
     are ranked by UNREALIZED PnL AND LEVERAGE and force-closed at the
     PREVIOUS mark price against the underwater user. Users with no open
     positions bear zero platform losses. Large positions (> $100k) are
     liquidated 20% first with a 30s cooldown between tranches.

Official-doc reconciliation (sodex.com auto-deleveraging page, 2026-09-26):
CONFIRMED — negative-equity trigger, uPnL×leverage ranking, previous-mark
close, the zero-loss flat-user invariant, and (new) that backstop-liquidated
positions receive NO special treatment in the ADL queue (a cascaded winner
queues identically to any winner). UNVERIFIED — the >$100k 20%-first/30s
staging appears nowhere on the official ADL page; it is carried here as a
knob-based advisory from the Governor's paste, not a doc-confirmed mechanic.

The previous-mark-price close detail matters: an ADL print is stamped at the
mark BEFORE the event's terminal move, so ADL prints can GAP versus the live
tape — treat the print price as stale by construction.

ADL is BOTH a RISK and a SIGNAL:

  RISK  — a winning position at high leverage CLIMBS the ADL queue: you can
          be force-closed while winning. Correct response (Governor): "take
          partial profit when ADL rank climbs; the rank tells you the crowd
          is WITH you = prepare the cross-side trade."
  SIGNAL — an observed ADL event = extreme one-sided imbalance →
          mean-reversion likely → the counter side becomes interesting. This
          dovetails with the cross-side probe doctrine: an ADL print is a
          candidate ARM for a counter-probe, never an entry by itself.

ARCHITECTURAL LAW: this module emits ADVISORY VERDICTS ONLY — never sizes,
never closes, never orders. No ADL-rank feed exists yet, so the exposure
score is an ESTIMATOR derived from own-position economics (uPnL × leverage),
the two axes SoDEX ranks by. It is NOT the venue's queue position; treat it
as a proxy with documented knobs, and let dark inputs abstain (None).

Department shape (docs/DEPARTMENT_TEMPLATE.md): zero-I/O brain — no network,
no files, no clock; everything injected per call. Master gate
`adl_lens_enabled` (default True): False → every method returns None with
ZERO state mutation (staged_liquidation_profile excepted: it is pure venue-
mechanics documentation and returns the small-position shape — no doctrine,
no advisory content).

Knobs (config getattr, defaults in code):
  adl_lens_enabled           True     — master gate; False = module inert
  adl_upnl_ref_usd           50.0     — uPnL reference for the exposure leg
  adl_leverage_ref           38.0     — leverage reference for the exposure leg
  adl_score_warn             0.70     — score rung for the partial-profit advisory
  adl_roe_warn               80.0     — ROE rung, DELIBERATELY aligned with the
                                        ratchet weak-stop rung (80% ROE): the
                                        rung that banks profit is the same rung
                                        that warns about ADL queue exposure
  adl_partial_frac           0.50     — advisory partial-bank fraction
  adl_event_big_usd          100000.0 — "high" strength event notional (the
                                        staged-liquidation size class)
  adl_staged_threshold_usd   100000.0 — staged-liquidation notional threshold
"""
from __future__ import annotations

from typing import Optional

ADL_FIRST_TRANCHE_FRAC = 0.20   # venue mechanic: >$100k liquidated 20% first
ADL_TRANCHE_COOLDOWN_S = 30     # venue mechanic: 30s between tranches


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _to_float(x) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return v


class AdlLens:
    """Stateless ADL estimator/advisor. Every method reads its knobs from the
    injected cfg via getattr with defaults; the master gate's False state
    returns None everywhere with zero state mutation (there is no state)."""

    # ── 1. exposure estimator ───────────────────────────────────────────────

    def adl_exposure_score(self, cfg, *, unrealized_pnl_usd, leverage,
                           position_notional_usd) -> Optional[float]:
        """Estimator in [0,1] of ADL queue exposure from own economics:
        score = clamp01(upnl_leg × lev_leg), upnl_leg = clamp01(uPnL /
        adl_upnl_ref_usd), lev_leg = clamp01(leverage / adl_leverage_ref).
        Negative uPnL → 0.0 (losers are the ADL'd counterparty's problem, not
        ours). Degenerate inputs (notional <= 0, leverage <= 0, non-numeric)
        → None."""
        if not getattr(cfg, "adl_lens_enabled", True):
            return None
        upnl = _to_float(unrealized_pnl_usd)
        lev = _to_float(leverage)
        notional = _to_float(position_notional_usd)
        if upnl is None or lev is None or notional is None:
            return None
        if notional <= 0 or lev <= 0:
            return None
        if upnl <= 0:
            return 0.0
        try:
            upnl_ref = float(getattr(cfg, "adl_upnl_ref_usd", 50.0))
            lev_ref = float(getattr(cfg, "adl_leverage_ref", 38.0))
        except (TypeError, ValueError):
            upnl_ref, lev_ref = 50.0, 38.0
        if upnl_ref <= 0 or lev_ref <= 0:
            return None    # fail-closed: a broken reference must not score
        upnl_leg = _clamp01(upnl / upnl_ref)
        lev_leg = _clamp01(lev / lev_ref)
        return _clamp01(upnl_leg * lev_leg)

    # ── 2. partial-profit advisory ──────────────────────────────────────────

    def adl_partial_profit_verdict(self, cfg, *, symbol, side, score,
                                   roe_pct) -> Optional[dict]:
        """Advisory fires (bank_partial_and_prepare_cross_side) when
        score >= adl_score_warn AND roe_pct >= adl_roe_warn (both >= — the
        rungs BIND at equality). Below either rung → hold. Degenerate →
        None. Advisory only — the caller owns any execution decision."""
        if not getattr(cfg, "adl_lens_enabled", True):
            return None
        s = _to_float(score)
        roe = _to_float(roe_pct)
        if s is None or roe is None:
            return None
        if not symbol or side not in ("long", "short"):
            return None
        try:
            score_warn = float(getattr(cfg, "adl_score_warn", 0.70))
            roe_warn = float(getattr(cfg, "adl_roe_warn", 80.0))
            frac = float(getattr(cfg, "adl_partial_frac", 0.50))
        except (TypeError, ValueError):
            score_warn, roe_warn, frac = 0.70, 80.0, 0.50
        base = {"symbol": symbol, "side": side, "score": s, "roe_pct": roe}
        if s >= score_warn and roe >= roe_warn:
            return {"action": "bank_partial_and_prepare_cross_side",
                    "partial_frac": frac, **base}
        return {"action": "hold", **base}

    # ── 3. observed-event signal ────────────────────────────────────────────

    def adl_event_signal(self, cfg, *, symbol, liquidated_side,
                         event_notional_usd=None) -> Optional[dict]:
        """An observed ADL event → mean_reversion_watch on the COUNTER side
        (extreme one-sided imbalance; candidate arm for a counter-probe,
        never an entry by itself). strength = "high" when event_notional_usd
        >= adl_event_big_usd (>= binds — a print exactly at the staged-
        liquidation size class IS the big class), else "normal". Missing
        notional → "normal". liquidated_side not in long/short → None."""
        if not getattr(cfg, "adl_lens_enabled", True):
            return None
        if liquidated_side == "long":
            counter = "short"
        elif liquidated_side == "short":
            counter = "long"
        else:
            return None
        if not symbol:
            return None
        try:
            big = float(getattr(cfg, "adl_event_big_usd", 100000.0))
        except (TypeError, ValueError):
            big = 100000.0
        strength = "normal"
        n = _to_float(event_notional_usd) if event_notional_usd is not None else None
        if n is not None and n >= big:
            strength = "high"
        return {"signal": "mean_reversion_watch",
                "counter_side": counter,
                "symbol": symbol,
                "strength": strength}

    # ── 4. staged-liquidation venue mechanics ───────────────────────────────

    def staged_liquidation_profile(self, cfg, *,
                                   position_notional_usd) -> dict:
        """Pure documentation/advisory of venue mechanics: positions over
        adl_staged_threshold_usd are liquidated 20% first with a 30s cooldown.
        Small/unknown positions → the small shape. This method carries NO
        doctrine and answers even when the master gate is False — but gated,
        it returns ONLY the small-position shape (pure shape, no doctrine:
        gated, the lens never asserts a position is in the staged class)."""
        if not getattr(cfg, "adl_lens_enabled", True):
            return {">100k": False}
        n = _to_float(position_notional_usd)
        try:
            threshold = float(getattr(cfg, "adl_staged_threshold_usd", 100000.0))
        except (TypeError, ValueError):
            threshold = 100000.0
        if n is None or n <= threshold:
            return {">100k": False}
        return {">100k": True,
                "first_tranche_frac": ADL_FIRST_TRANCHE_FRAC,
                "cooldown_s": ADL_TRANCHE_COOLDOWN_S}
