"""
intelligence/market_context.py — Unified market state snapshot.

"The market is a device for transferring money from the impatient to the patient.
Wait for the tape to tell you when to act. The big money is made in the big swings."
— Jesse Livermore

"The most important rule of trading is to play good defense, not great offense.
Every day I assume every position I have is wrong. I know where my stop risk points
are and I'm forced to evaluate my own reasoning."
— Paul Tudor Jones

"It takes courage to be a pig. It takes courage to ride a profit with big leverage.
The trouble with me is that I have gotten out of stocks because I was bored with them,
not because I had lost confidence."
— Stanley Druckenmiller

Built once per signal tick in main.py, frozen after construction, passed to every
component downstream. All signals, regime, funding, flow, and cascade state live here.
Signal weights adapt to market mode — weights adjust signal influence, hard gates don't move.

Market modes (priority order):
  cascade_blocked    — in-cascade danger zone; hard gate on all new entries
  cascade_momentum   — liquidation cascade accelerating; ride momentum with tight stops
  cascade_primed     — cascade aftermath; recovery entry window open
  calendar_caution   — major macro event within 4h; raise institutional tier weight
  defensive          — loss streak ≥ 4; raised coherence minimum active
  normal             — baseline operation
"""

import time
from dataclasses import dataclass
from typing import Dict, Optional, Any

# ── Market Mode Constants ─────────────────────────────────────────────────────
MODE_CASCADE_BLOCKED  = "cascade_blocked"
MODE_CASCADE_MOMENTUM = "cascade_momentum"
MODE_CASCADE_PRIMED   = "cascade_primed"
MODE_CALENDAR_CAUTION = "calendar_caution"
MODE_DEFENSIVE        = "defensive"
MODE_NORMAL           = "normal"

# Calendar caution: events within this many hours trigger reduced-size mode
CALENDAR_CAUTION_HOURS = 4.0

# Defensive mode loss streak threshold (mirrors AdaptiveCalibrator.LOSS_STREAK_TRIGGER)
DEFENSIVE_STREAK_THRESHOLD = 4

# Flow bias thresholds: (buy_vol − sell_vol) / total_vol
FLOW_BIAS_BUY_THRESHOLD  =  0.20
FLOW_BIAS_SELL_THRESHOLD = -0.20


@dataclass(frozen=True)
class MarketContext:
    """
    Frozen, unified market snapshot — built once per signal tick.

    All downstream components receive the same instance.
    Immutable after construction — never mutate fields post-build().

    Key design rule (Livermore): one opinion, one frame, acted on decisively.
    A single unified context prevents different components from reading contradictory
    market state mid-decision.
    """
    # ── Cascade state ──────────────────────────────────────────────────────────
    cascade_phase:           str          # "idle" | "blocked" | "primed" | "momentum"
    cascade_type:            str          # "momentum" | "exhaustion" | "none"
    cascade_direction:       str          # "bearish" | "bullish" | ""
    cascade_notional:        float        # notional USD in last cascade batch
    cascade_aftermath_count: int          # number of confirmed aftermath signals (0-5)

    # ── Funding rates ──────────────────────────────────────────────────────────
    bybit_rates:        Dict[str, float]  # {symbol: bybit_8h_rate}
    sodex_rates:        Dict[str, float]  # {symbol: sodex_rate}
    cross_venue_spread: Dict[str, float]  # {symbol: bybit_rate - sodex_rate}

    # ── Trade flow ─────────────────────────────────────────────────────────────
    buy_deltas:  Dict[str, float]  # {symbol: buy_volume_60s}
    sell_deltas: Dict[str, float]  # {symbol: sell_volume_60s}
    flow_bias:   Dict[str, str]    # {symbol: "buy" | "sell" | "neutral"}

    # ── Regime ─────────────────────────────────────────────────────────────────
    regime:            str    # regime string from RelativeStrengthEngine
    regime_confidence: float  # 0.0–1.0 estimated from category score spread

    # ── Calendar ───────────────────────────────────────────────────────────────
    calendar_hours:  Optional[float]  # hours to next macro event (None if no upcoming event)
    calendar_regime: str              # CalendarState.regime string

    # ── Time Regime Overlay ────────────────────────────────────────────────────
    time_regime_phase: str  # e.g. "month_start_tue_wed", "pre_event_FOMC"
    time_regime_notes: str  # pipe-delimited human-readable explanation

    # ── Unified mode + weights ─────────────────────────────────────────────────
    market_mode:    str                # one of the MODE_* constants above
    signal_weights: Dict[str, float]  # {tier_name: multiplier} — empty == all 1.0

    built_at: float  # unix timestamp

    # ── Factory ────────────────────────────────────────────────────────────────

    @classmethod
    def build(
        cls,
        cascade_tracker,
        funding_history,
        trade_flow_stores: Dict[str, Any],
        relative_strength_engine,
        candle_buffers: Dict[str, Any],
        adaptive_calibrator,
        calendar_state=None,
        assets=None,
        time_regime=None,
    ) -> "MarketContext":
        """
        Build a frozen MarketContext for the current tick.

        Parameters
        ----------
        cascade_tracker          CascadeTracker instance
        funding_history          FundingHistory instance
        trade_flow_stores        {symbol: TradeFlowStore}
        relative_strength_engine RelativeStrengthEngine instance
        candle_buffers           {symbol: {"1m": CandleBuffer, "15m": ...}}
        adaptive_calibrator      AdaptiveCalibrator instance
        calendar_state           Pre-fetched CalendarState — pass cached value to avoid
                                 async call inside this sync factory.
        assets                   List of symbols to include (defaults to flow_store keys)
        """
        # ── Cascade state ─────────────────────────────────────────────────────
        phase_str   = cascade_tracker.get_phase().value
        snap        = cascade_tracker.get_snapshot()
        aftermath   = cascade_tracker.get_aftermath_signals()
        n_aftermath = sum(1 for v in aftermath.values() if v) if aftermath else 0

        if snap is not None:
            c_direction = snap.batch_direction
            c_notional  = snap.batch_notional_usd
            # Derive cascade type from velocity vs configured thresholds
            try:
                c_type = "momentum" if cascade_tracker._is_momentum_cascade(
                    snap.velocity, snap.batch_notional_usd
                ) else "exhaustion"
            except Exception:
                c_type = "exhaustion"
        else:
            c_direction = ""
            c_notional  = 0.0
            c_type      = "none"

        # ── Funding rates ─────────────────────────────────────────────────────
        syms = assets or list(trade_flow_stores.keys())
        bybit_rates: Dict[str, float] = {}
        sodex_rates: Dict[str, float] = {}
        cross_venue: Dict[str, float] = {}

        for sym in syms:
            b_rate = getattr(funding_history, "_bybit_rates", {}).get(sym, 0.0)
            bybit_rates[sym] = b_rate

            s_rates = funding_history.get_rates(sym, 1)
            s_rate  = s_rates[0] if s_rates else 0.0
            sodex_rates[sym] = s_rate

            if hasattr(funding_history, "get_cross_venue_spread"):
                spread = funding_history.get_cross_venue_spread(sym)
                cross_venue[sym] = spread if spread is not None else (b_rate - s_rate)
            else:
                cross_venue[sym] = b_rate - s_rate

        # ── Trade flow ────────────────────────────────────────────────────────
        buy_deltas:  Dict[str, float] = {}
        sell_deltas: Dict[str, float] = {}
        flow_bias:   Dict[str, str]   = {}

        for sym in syms:
            store = trade_flow_stores.get(sym)
            if store is None:
                buy_deltas[sym]  = 0.0
                sell_deltas[sym] = 0.0
                flow_bias[sym]   = "neutral"
                continue
            try:
                bv = float(store.buy_volume(window_ms=60_000))
                sv = float(store.sell_volume(window_ms=60_000))
            except Exception:
                bv, sv = 0.0, 0.0
            buy_deltas[sym]  = bv
            sell_deltas[sym] = sv
            total      = bv + sv + 1e-9
            bias_score = (bv - sv) / total
            if bias_score > FLOW_BIAS_BUY_THRESHOLD:
                flow_bias[sym] = "buy"
            elif bias_score < FLOW_BIAS_SELL_THRESHOLD:
                flow_bias[sym] = "sell"
            else:
                flow_bias[sym] = "neutral"

        # ── Regime ────────────────────────────────────────────────────────────
        try:
            matrix     = relative_strength_engine.compute_regime(candle_buffers)
            regime_str = matrix.regime
            # v2.0 RegimeState carries a confidence field directly.
            # Fall back to category-spread estimation for any legacy return type.
            if hasattr(matrix, "confidence") and matrix.confidence is not None:
                regime_conf = float(matrix.confidence)
            else:
                cat_scores = getattr(matrix, "category_scores", {})
                if cat_scores:
                    scores  = list(cat_scores.values())
                    spread  = max(scores) - min(scores) if len(scores) > 1 else 0.0
                    regime_conf = min(1.0, abs(spread) * 10.0)
                else:
                    regime_conf = 0.5
        except Exception:
            regime_str  = "confused"
            regime_conf = 0.0

        # ── Calendar state ────────────────────────────────────────────────────
        cal_hours:  Optional[float] = None
        cal_regime: str             = "normal"
        if calendar_state is not None:
            try:
                _hours = getattr(calendar_state, "hours_to_event", None)
                if _hours is not None:
                    cal_hours = float(_hours) if float(_hours) > 0 else None
                cal_regime = str(getattr(calendar_state, "regime", "normal"))
            except Exception:
                pass

        # ── Market mode (priority order) ──────────────────────────────────────
        loss_streak = getattr(adaptive_calibrator, "_loss_streak", 0)

        if cascade_tracker.is_blocked():
            mode = MODE_CASCADE_BLOCKED
        elif cascade_tracker.is_momentum():
            mode = MODE_CASCADE_MOMENTUM
        elif cascade_tracker.is_primed():
            mode = MODE_CASCADE_PRIMED
        elif (
            (cal_hours is not None and cal_hours <= CALENDAR_CAUTION_HOURS)
            or cal_regime in ("blackout", "caution")
        ):
            mode = MODE_CALENDAR_CAUTION
        elif loss_streak >= DEFENSIVE_STREAK_THRESHOLD:
            mode = MODE_DEFENSIVE
        else:
            mode = MODE_NORMAL

        weights = cls._compute_weights(mode)

        # ── Time Regime Overlay ───────────────────────────────────────────────
        _tr_phase = ""
        _tr_notes = ""
        if time_regime is not None:
            try:
                _tr_phase = str(getattr(time_regime, "phase", ""))
                _tr_notes = str(getattr(time_regime, "notes", ""))
            except Exception:
                pass

        return cls(
            cascade_phase           = phase_str,
            cascade_type            = c_type,
            cascade_direction       = c_direction,
            cascade_notional        = c_notional,
            cascade_aftermath_count = n_aftermath,
            bybit_rates             = bybit_rates,
            sodex_rates             = sodex_rates,
            cross_venue_spread      = cross_venue,
            buy_deltas              = buy_deltas,
            sell_deltas             = sell_deltas,
            flow_bias               = flow_bias,
            regime                  = regime_str,
            regime_confidence       = regime_conf,
            calendar_hours          = cal_hours,
            calendar_regime         = cal_regime,
            time_regime_phase       = _tr_phase,
            time_regime_notes       = _tr_notes,
            market_mode             = mode,
            signal_weights          = weights,
            built_at                = time.time(),
        )

    @staticmethod
    def _compute_weights(mode: str) -> Dict[str, float]:
        """
        Context-aware tier weight multipliers.

        Weights adjust signal influence — they do NOT replace hard gates.
        Unspecified tiers default to 1.0 (empty dict = all 1.0 = no adjustment).

        Druckenmiller principle: "Ride the winner." When the context is cascade
        momentum, emphasise cascade and sweep tiers; de-emphasise institutional
        signals that haven't repriced the cascade yet.
        """
        if mode == MODE_CASCADE_MOMENTUM:
            return {
                "cascade_aftermath": 2.0,   # Cascade event is the primary driver
                "microstructure":    1.5,   # Sweep / VPIN confirms momentum
                "liquidation":       1.5,   # Live forced-close flow
                "institutional":     0.5,   # Lags in a fast cascade
                "mag7_macro":        0.5,   # Macro lags cascade by design
            }
        elif mode == MODE_CASCADE_PRIMED:
            return {
                "cascade_aftermath": 2.0,   # Aftermath is the entry signal
                "microstructure":    1.5,   # Sweep confirmation on recovery
                "funding":           1.2,   # Funding normalisation confirms recovery
                "cross_venue":       1.2,   # Venue spread normalising = recovery
            }
        elif mode == MODE_CALENDAR_CAUTION:
            # Reduce all tier contributions — macro event risk is unquantifiable
            return {k: 0.7 for k in (
                "microstructure", "institutional", "regime", "structure",
                "funding", "oi_momentum", "liquidation", "mag7_macro",
                "cross_venue", "cascade_aftermath",
            )}
        elif mode == MODE_DEFENSIVE:
            return {
                "institutional": 2.0,   # Require strong institutional flow in drawdown
                "regime":        1.5,   # Macro regime must be unambiguous
                "funding":       1.2,   # Funding must support direction
                "microstructure": 0.7,  # Discount noise signals during drawdown
            }
        else:
            # Normal: empty dict → all weights 1.0 (no-op)
            return {}
