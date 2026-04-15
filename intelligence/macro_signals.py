"""
MacroSignalEngine — seven cross-asset portfolio-level signals.

Derived from 55 live trades of ARIA observation. Each signal modifies
the coherence score of individual asset signals as a portfolio multiplier
(not a per-asset score). Called by the interpreter after per-asset coherence
is computed and before the final score is emitted.

Signals:
  1. Macro Regime Confirmation  — multiple assets same direction = stronger regime
  2. Capitulation Detector      — OI long while price falls = bottom proximity
  3. Funding Regime Aggregator  — crowd positioning across all assets
  4. Post-Event Alpha           — first signal after calendar BLOCK = highest win rate
  5. Volume Quality             — SoDEX 24h volume vs 7d average
  6. Hold Time Intelligence     — which tier combinations produce real vs noise trades
  7. XAUT Macro Thermometer     — gold scoring = risk-off confirmed, amplifies crypto shorts
"""

import time
import json
import structlog
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

log = structlog.get_logger(__name__)

_TRADE_HISTORY_PATH = Path("logs/trade_history.json")
_XAUT_SYMBOL = "XAUT-USD"


@dataclass
class MacroState:
    """Snapshot of all macro signal outputs. Updated continuously."""
    # Signal 1: Macro regime confirmation
    macro_confirmation_score: float = 0.0
    macro_direction: str = "none"
    assets_confirming: int = 0
    assets_total_active: int = 0

    # Signal 2: Capitulation
    capitulation_detected: bool = False
    capitulation_assets: int = 0
    capitulation_strength: float = 0.0

    # Signal 3: Aggregate funding
    aggregate_funding_score: float = 0.0
    funding_regime: str = "neutral"  # "crowded_long", "crowded_short", "neutral"

    # Signal 4: Post-event alpha
    post_event_active: bool = False
    post_event_direction: str = "none"
    post_event_strength: float = 0.0
    post_event_expires_ms: int = 0

    # Signal 5: Volume quality
    volume_quality_mult: float = 1.0
    volume_regime: str = "normal"

    # Signal 6: Hold time intelligence (weights by tier config)
    signal_config_weights: dict = field(default_factory=dict)

    # Signal 7: XAUT thermometer
    xaut_confirms_regime: bool = False
    xaut_macro_mult: float = 1.0
    xaut_direction: str = "none"
    xaut_coherence: float = 0.0

    last_updated: float = 0.0


class MacroSignalEngine:
    """
    Portfolio-level cross-asset intelligence engine.

    Instantiated once in IntelligenceInterpreter. Updated on every
    per-asset signal computation. Applied to each signal's coherence
    score before it is emitted to the execution pipeline.
    """

    def __init__(self, config: Any) -> None:
        self.config = config
        self.state = MacroState()
        self._min_coherence = getattr(config, "min_coherence", 1.0)

        # Per-asset current data
        self._asset_directions:  Dict[str, str]   = {}
        self._asset_coherence:   Dict[str, float] = {}
        self._asset_funding:     Dict[str, float] = {}
        self._asset_oi_flow:     Dict[str, str]   = {}
        self._asset_mark_prices: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=60)  # ~30 min of 30s samples
        )

        # Volume tracking (7-day ring buffer of daily volumes)
        self._daily_volumes: deque = deque(maxlen=7)
        self._today_volume: float = 0.0

        # Calendar state
        self._last_calendar_regime: str = "CLEAR"
        self._last_block_cleared_ms: int = 0

        # Trade history for Signal 6
        self._trade_history: list = []
        self._load_trade_history()

        self.state.last_updated = time.time()
        log.info("macro_signal_engine_init", min_coherence=self._min_coherence)

    # ─────────────────────────────────────────────────────────────────────────
    # Public update methods — called from interpreter and main.py
    # ─────────────────────────────────────────────────────────────────────────

    def update_asset_signal(
        self,
        symbol: str,
        direction: str,
        coherence: float,
        funding_rate: float,
        oi_flow_direction: str = "none",
        mark_price: float = 0.0,
    ) -> None:
        """
        Called by interpreter after each per-asset coherence is computed.
        Feeds portfolio-level state so cross-asset signals have fresh data.
        """
        self._asset_directions[symbol] = direction
        self._asset_coherence[symbol]  = coherence
        self._asset_funding[symbol]    = funding_rate
        self._asset_oi_flow[symbol]    = oi_flow_direction
        if mark_price > 0:
            self._asset_mark_prices[symbol].append(
                {"price": mark_price, "time_ms": int(time.time() * 1000)}
            )
        self._recompute_all()

    def update_volume(self, volume_24h: float) -> None:
        """Called when SoDEX 24h volume refreshes (once per hour is sufficient)."""
        self._today_volume = volume_24h
        self._compute_volume_quality()

    def update_calendar(self, regime: str) -> None:
        """Called when calendar regime changes (from main.py calendar polling loop)."""
        prev = self._last_calendar_regime
        if prev == "BLOCK" and regime == "CLEAR":
            self._last_block_cleared_ms = int(time.time() * 1000)
            log.info("post_event_window_opened",
                     note="first signal after BLOCK clears gets alpha bonus")
        self._last_calendar_regime = regime
        self._compute_post_event_alpha()

    def record_trade_outcome(
        self,
        symbol: str,
        direction: str,
        entry_coherence: float,
        tiers_fired: list,
        hold_seconds: float,
        pnl: float,
    ) -> None:
        """
        Record completed trade for Signal 6 hold-time learning.
        Called from main.py after position close.
        """
        self._trade_history.append({
            "symbol":    symbol,
            "direction": direction,
            "coherence": entry_coherence,
            "tiers":     tiers_fired,
            "hold_s":    hold_seconds,
            "pnl":       pnl,
            "timestamp": time.time(),
        })
        # Keep last 200 trades
        if len(self._trade_history) > 200:
            self._trade_history = self._trade_history[-200:]
        self._compute_hold_time_intelligence()
        self._save_trade_history()

    # ─────────────────────────────────────────────────────────────────────────
    # Core: apply all signals to a candidate trade
    # ─────────────────────────────────────────────────────────────────────────

    def apply_macro_to_coherence(
        self,
        symbol: str,
        direction: str,
        base_coherence: float,
        tiers_fired: list,
    ) -> Tuple[float, Dict]:
        """
        Apply all seven macro signals to a candidate's coherence.

        Returns (adjusted_coherence, breakdown_dict).
        Called by interpreter just before final score is emitted.
        Additive adjustments first, multiplicative last.
        """
        if direction == "none" or base_coherence <= 0:
            return base_coherence, {"base": base_coherence, "final": base_coherence}

        adj = base_coherence
        bd: Dict = {"base": round(base_coherence, 3)}

        # ── Signal 1: Macro regime confirmation ──────────────────────────────
        if (
            self.state.macro_direction == direction
            and self.state.macro_confirmation_score > 0
        ):
            bonus = self.state.macro_confirmation_score
            adj  += bonus
            bd["macro_confirmation"] = round(bonus, 3)

        # ── Signal 2: Capitulation ────────────────────────────────────────────
        if self.state.capitulation_detected:
            if direction == "short":
                adj += -0.3                    # shorts risky near capitulation
                bd["capitulation"] = -0.3
            elif direction == "long":
                adj += 0.3                     # longs favoured at bottom
                bd["capitulation"] = 0.3

        # ── Signal 3: Funding regime ──────────────────────────────────────────
        fr = self.state.aggregate_funding_score
        regime = self.state.funding_regime
        if regime == "crowded_long" and direction == "short":
            adj += abs(fr)
            bd["funding_regime"] = round(abs(fr), 3)
        elif regime == "crowded_short" and direction == "long":
            adj += fr
            bd["funding_regime"] = round(fr, 3)
        elif regime == "crowded_long" and direction == "long":
            adj -= abs(fr)                     # going long into crowded longs = bad
            bd["funding_regime"] = round(-abs(fr), 3)

        # ── Signal 4: Post-event alpha ────────────────────────────────────────
        if self.state.post_event_active and self.state.post_event_strength > 0:
            bonus = self.state.post_event_strength
            adj  += bonus
            bd["post_event"] = round(bonus, 3)

        # ── Signal 5: Volume quality (multiplicative) ─────────────────────────
        vol_mult = self.state.volume_quality_mult
        if vol_mult != 1.0:
            adj *= vol_mult
            bd["volume_mult"] = round(vol_mult, 3)

        # ── Signal 6: Hold-time intelligence (multiplicative) ─────────────────
        config_key = str(tuple(sorted(tiers_fired)))
        hold_data  = self.state.signal_config_weights.get(config_key, {})
        hold_mult  = hold_data.get("weight", 1.0)
        if hold_mult != 1.0:
            adj *= hold_mult
            bd["hold_time_mult"] = round(hold_mult, 3)

        # ── Signal 7: XAUT thermometer (multiplicative, cross-asset only) ─────
        if self.state.xaut_confirms_regime and symbol != _XAUT_SYMBOL:
            xaut_mult = self.state.xaut_macro_mult
            xaut_dir  = self.state.xaut_direction
            if xaut_dir == "long" and direction == "short":
                # Gold rising + crypto short = risk-off confirmed → amplify
                adj *= xaut_mult
                bd["xaut_mult"] = round(xaut_mult, 3)
            elif xaut_dir == "long" and direction == "long":
                # Going long crypto when gold says risk-off → reduce conviction
                adj *= (2.0 - xaut_mult)
                bd["xaut_mult"] = round(2.0 - xaut_mult, 3)
            elif xaut_dir == "short" and direction == "long":
                # Gold falling = risk-on developing → boost crypto longs
                adj *= xaut_mult
                bd["xaut_mult"] = round(xaut_mult, 3)

        adj = max(0.0, adj)   # coherence cannot go negative
        bd["final"] = round(adj, 3)

        if abs(adj - base_coherence) >= 0.05:   # only log material changes
            log.info(
                "macro_applied",
                symbol=symbol,
                direction=direction,
                **{k: v for k, v in bd.items()},
            )

        return adj, bd

    # ─────────────────────────────────────────────────────────────────────────
    # Status / summary
    # ─────────────────────────────────────────────────────────────────────────

    def summary(self) -> Dict:
        return {
            "macro_direction":    self.state.macro_direction,
            "assets_confirming":  self.state.assets_confirming,
            "macro_score":        round(self.state.macro_confirmation_score, 2),
            "capitulation":       self.state.capitulation_detected,
            "funding_regime":     self.state.funding_regime,
            "post_event_active":  self.state.post_event_active,
            "volume_regime":      self.state.volume_regime,
            "volume_mult":        round(self.state.volume_quality_mult, 2),
            "xaut_confirms":      self.state.xaut_confirms_regime,
            "xaut_direction":     self.state.xaut_direction,
            "xaut_mult":          round(self.state.xaut_macro_mult, 2),
            "configs_learned":    len(self.state.signal_config_weights),
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Private: signal computations
    # ─────────────────────────────────────────────────────────────────────────

    def _recompute_all(self) -> None:
        self._compute_macro_confirmation()
        self._compute_capitulation()
        self._compute_funding_regime()
        self._compute_xaut_thermometer()
        self.state.last_updated = time.time()

    def _compute_macro_confirmation(self) -> None:
        """Signal 1: directional alignment across assets amplifies the regime."""
        active = {
            sym: d for sym, d in self._asset_directions.items()
            if d in ("long", "short")
            and self._asset_coherence.get(sym, 0) >= self._min_coherence
        }
        if len(active) < 2:
            self.state.macro_confirmation_score = 0.0
            self.state.macro_direction          = "none"
            self.state.assets_confirming        = 0
            self.state.assets_total_active      = len(active)
            return

        long_count  = sum(1 for d in active.values() if d == "long")
        short_count = sum(1 for d in active.values() if d == "short")
        dominant       = "short" if short_count >= long_count else "long"
        dominant_count = max(long_count, short_count)
        total          = len(active)
        ratio          = dominant_count / total

        if ratio < 0.60:
            score = 0.0
        elif ratio < 0.75:
            score = 0.3
        elif ratio < 0.90:
            score = 0.6
        else:
            score = 0.9   # near-unanimous

        prev_score = self.state.macro_confirmation_score
        self.state.macro_confirmation_score = score
        self.state.macro_direction          = dominant
        self.state.assets_confirming        = dominant_count
        self.state.assets_total_active      = total

        if score > 0 and abs(score - prev_score) >= 0.1:
            log.info("macro_confirmation",
                     direction=dominant, confirming=dominant_count,
                     total=total, ratio=round(ratio, 2), score=score)

    def _compute_capitulation(self) -> None:
        """Signal 2: OI accumulating long while prices fall = bottom proximity."""
        now_ms  = int(time.time() * 1000)
        cutoff  = now_ms - 3_600_000  # 1-hour lookback

        longs_accumulating = 0
        prices_falling     = 0

        for symbol in self._asset_directions:
            if self._asset_oi_flow.get(symbol) == "long":
                longs_accumulating += 1

            prices = [p for p in self._asset_mark_prices[symbol]
                      if p["time_ms"] > cutoff]
            if len(prices) >= 2:
                chg = (prices[-1]["price"] - prices[0]["price"]) / prices[0]["price"]
                if chg < -0.02:
                    prices_falling += 1

        detected = longs_accumulating >= 3 and prices_falling >= 3
        strength = min(1.0, (longs_accumulating + prices_falling) / 10)

        prev = self.state.capitulation_detected
        self.state.capitulation_detected = detected
        self.state.capitulation_assets   = longs_accumulating
        self.state.capitulation_strength = strength

        if detected and not prev:
            log.warning("capitulation_detected",
                        longs_accumulating=longs_accumulating,
                        prices_falling=prices_falling,
                        strength=round(strength, 2),
                        note="smart money accumulating — tighten shorts, prepare longs")

    def _compute_funding_regime(self) -> None:
        """Signal 3: aggregate funding reveals crowd positioning."""
        rates = [r for r in self._asset_funding.values() if r != 0]
        if len(rates) < 3:
            self.state.funding_regime           = "neutral"
            self.state.aggregate_funding_score  = 0.0
            return

        total    = len(rates)
        positive = sum(1 for r in rates if r > 0)
        negative = sum(1 for r in rates if r < 0)
        avg_rate = sum(rates) / total

        if positive / total >= 0.75:
            regime = "crowded_long"
            score  = -0.3    # shorts get this as a bonus; longs get penalty
        elif negative / total >= 0.75:
            regime = "crowded_short"
            score  = 0.3     # longs benefit from funding collection
        else:
            regime = "neutral"
            score  = 0.0

        prev = self.state.funding_regime
        self.state.funding_regime          = regime
        self.state.aggregate_funding_score = score

        if regime != prev:
            log.info("funding_regime_change",
                     regime=regime, positive=positive, negative=negative,
                     avg_rate_pct=round(avg_rate * 100, 4), score=score)

    def _compute_post_event_alpha(self) -> None:
        """Signal 4: first signal 30 min after BLOCK clears = highest win-rate window."""
        if self._last_block_cleared_ms == 0:
            self.state.post_event_active    = False
            self.state.post_event_strength  = 0.0
            return

        now_ms  = int(time.time() * 1000)
        elapsed = now_ms - self._last_block_cleared_ms
        WINDOW  = 1_800_000   # 30 minutes

        if elapsed > WINDOW:
            self.state.post_event_active   = False
            self.state.post_event_strength = 0.0
            return

        # Linear decay from 0.5 → 0.0 over 30 min
        decay    = 1.0 - (elapsed / WINDOW)
        strength = 0.5 * decay

        self.state.post_event_active       = True
        self.state.post_event_strength     = strength
        self.state.post_event_expires_ms   = self._last_block_cleared_ms + WINDOW

        log.info("post_event_alpha_active",
                 elapsed_min=round(elapsed / 60000, 1), strength=round(strength, 2))

    def _compute_volume_quality(self) -> None:
        """Signal 5: today's SoDEX volume vs 7-day average."""
        if not self._daily_volumes:
            self.state.volume_quality_mult = 1.0
            self.state.volume_regime       = "normal"
            return

        avg_7d = sum(self._daily_volumes) / len(self._daily_volumes)
        if avg_7d == 0:
            self.state.volume_quality_mult = 1.0
            return

        ratio = self._today_volume / avg_7d

        if ratio >= 1.5:
            mult, regime = 1.15, "high_volume"
        elif ratio >= 0.8:
            mult, regime = 1.00, "normal"
        elif ratio >= 0.5:
            mult, regime = 0.90, "low_volume"
        else:
            mult, regime = 0.80, "very_low_volume"

        prev_regime = self.state.volume_regime
        self.state.volume_quality_mult = mult
        self.state.volume_regime       = regime

        if regime != prev_regime:
            log.info("volume_quality_change",
                     today_m=round(self._today_volume / 1e6, 2),
                     avg_7d_m=round(avg_7d / 1e6, 2),
                     ratio=round(ratio, 2), mult=mult, regime=regime)

    def _compute_hold_time_intelligence(self) -> None:
        """Signal 6: learn which tier configurations produce real trades vs noise."""
        if len(self._trade_history) < 20:
            return

        from collections import defaultdict
        config_stats: Dict = defaultdict(lambda: {"holds": [], "pnls": []})

        for trade in self._trade_history:
            key = str(tuple(sorted(trade.get("tiers", []))))
            config_stats[key]["holds"].append(trade.get("hold_s", 0))
            config_stats[key]["pnls"].append(trade.get("pnl", 0))

        weights = {}
        for cfg_key, stats in config_stats.items():
            holds = stats["holds"]
            if not holds:
                continue
            pct_real = sum(1 for h in holds if h >= 900) / len(holds)
            if pct_real >= 0.7:
                w = 1.2
            elif pct_real >= 0.5:
                w = 1.1
            elif pct_real >= 0.3:
                w = 1.0
            elif pct_real >= 0.1:
                w = 0.9
            else:
                w = 0.8
            weights[cfg_key] = {
                "weight":     w,
                "avg_hold_s": round(sum(holds) / len(holds)),
                "pct_real":   round(pct_real, 2),
                "n":          len(holds),
            }

        self.state.signal_config_weights = weights
        log.info("hold_time_intelligence",
                 configs=len(weights), trades=len(self._trade_history))

    def _compute_xaut_thermometer(self) -> None:
        """Signal 7: XAUT direction = macro regime thermometer for all crypto signals."""
        xaut_dir = self._asset_directions.get(_XAUT_SYMBOL, "none")
        xaut_coh = self._asset_coherence.get(_XAUT_SYMBOL, 0.0)

        self.state.xaut_direction  = xaut_dir
        self.state.xaut_coherence  = xaut_coh

        if xaut_coh < 2.0 or xaut_dir == "none":
            prev = self.state.xaut_confirms_regime
            self.state.xaut_confirms_regime = False
            self.state.xaut_macro_mult      = 1.0
            if prev:
                log.info("xaut_thermometer_off", xaut_coh=round(xaut_coh, 2))
            return

        prev_dir  = self.state.xaut_direction
        prev_mult = self.state.xaut_macro_mult
        self.state.xaut_confirms_regime = True
        if xaut_dir == "long":
            # Gold rising = risk-off → crypto shorts confirmed, crypto longs penalised
            self.state.xaut_macro_mult = 1.20
            if xaut_dir != prev_dir or self.state.xaut_macro_mult != prev_mult:
                log.info("xaut_thermometer",
                         direction="long", coherence=round(xaut_coh, 2), mult=1.20,
                         note="gold rising confirms risk-off — crypto shorts amplified 1.20×")
        else:
            # Gold falling = risk-on developing → crypto longs amplified
            self.state.xaut_macro_mult = 1.10
            if xaut_dir != prev_dir or self.state.xaut_macro_mult != prev_mult:
                log.info("xaut_thermometer",
                         direction="short", coherence=round(xaut_coh, 2), mult=1.10,
                         note="gold falling — risk-on — crypto longs amplified 1.10×")

    # ─────────────────────────────────────────────────────────────────────────
    # Persistence helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _load_trade_history(self) -> None:
        if _TRADE_HISTORY_PATH.exists():
            try:
                data = json.loads(_TRADE_HISTORY_PATH.read_text())
                self._trade_history = data.get("trades", [])
                log.info("trade_history_loaded", trades=len(self._trade_history))
            except Exception as exc:
                log.warning("trade_history_load_error", error=str(exc))

    def _save_trade_history(self) -> None:
        try:
            _TRADE_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
            _TRADE_HISTORY_PATH.write_text(
                json.dumps({"trades": self._trade_history[-200:]})
            )
        except Exception as exc:
            log.warning("trade_history_save_error", error=str(exc))
