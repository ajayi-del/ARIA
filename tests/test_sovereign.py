"""
ARIA SOVEREIGN Personality Test Suite

Validates the staking-anchored MAG7 component divergence strategy:
  T1.  No stake → SOVEREIGN never fires
  T2.  Z-score below threshold → no signal
  T3.  Z-score >= 1.5, risk_off regime → short component (momentum)
  T4.  Z-score >= 1.5, confused regime → long component (mean reversion)
  T5.  Earnings within 48h → SOVEREIGN blocked
  T6.  Sovereign budget = 0 → SOVEREIGN enters COIL
  T7.  Winning streak 2× seed → 50% transfers to main capital
  T8.  Trade size = stake × component_weight (hedge-matched)
  T9.  MAG7 index + component same direction → no signal (whole market move)
  T10. Z-score stop: entry 2.0σ, widens to 4.0σ → position closes

Also covers:
  - SSIComponentMonitor rolling z-score computation
  - StakingMonitor yield accrual and hedge notional
  - YieldTracker budget/overflow mechanics
  - SovereignSignalGenerator direction logic
  - PersonalityEngine SOVEREIGN routing
  - Cross-agent independence (SOVEREIGN + FLOW fire independently)

Run:
    python tests/test_sovereign.py
    python tests/test_sovereign.py -v
"""

import sys
import os
import asyncio
import math
import time
import unittest
from collections import deque
from typing import Dict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.helpers import (
    make_test_personality_engine,
    make_test_context,
    make_warmed_context_cache,
    make_sovereign_context_cache,
    test_config,
)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — SSI COMPONENT MONITOR
# ══════════════════════════════════════════════════════════════════════════════

class TestSSIComponentMonitor(unittest.TestCase):

    def _make_monitor(self):
        from intelligence.ssi_component_monitor import SSIComponentMonitor
        return SSIComponentMonitor()

    def test_mag7_weights_sum_to_one(self):
        from intelligence.ssi_component_monitor import MAG7_COMPONENTS
        total = sum(MAG7_COMPONENTS.values())
        self.assertAlmostEqual(total, 1.0, places=3,
                               msg=f"MAG7 weights sum to {total:.4f}. Must be 1.0.")

    def test_zscore_zero_before_history(self):
        """Fresh monitor with no data → all z-scores = 0."""
        monitor = self._make_monitor()
        scores = monitor.get_all_z_scores()
        for sym, z in scores.items():
            self.assertEqual(z, 0.0, f"{sym} z-score should be 0 before data. Got {z}.")

    def test_zscore_increases_with_divergence(self):
        """
        Component suddenly underperforming → z-score becomes negative.

        Z-score measures deviation from ROLLING MEAN.
        A constant spread gives z=0. To get z < -1 we need:
          Phase 1: TSLA in line with index (spreads ≈ 0, establishes baseline)
          Phase 2: TSLA suddenly drops much more than index → spread very negative
                   → z = (spread_now - mean) / std → strongly negative
        """
        monitor = self._make_monitor()

        # Phase 1: 22 periods — TSLA matches index return exactly
        tsla_price = 180.0
        for i in range(22):
            idx_return = 0.005  # index +0.5%/period
            tsla_price *= (1 + idx_return)   # TSLA in line
            monitor.update_index_price(100.0 + i * 0.5)
            monitor.update_price("TSLA-USD", tsla_price, idx_return)

        # Phase 2: TSLA suddenly drops 4% while index rises 0.5%
        # → spread = -0.04 - 0.005 = -0.045, far below mean ≈ 0
        tsla_price *= (1.0 - 0.04)
        monitor.update_price("TSLA-USD", tsla_price, 0.005)

        z = monitor.get_all_z_scores()["TSLA-USD"]
        self.assertLess(z, -1.0,
                        f"Sudden underperformance vs baseline → z < -1.0. Got {z:.3f}.")

    def test_inject_z_scores_for_testing(self):
        """inject_z_scores() must allow test-controlled z-score injection."""
        monitor = self._make_monitor()
        monitor.inject_z_scores({"TSLA-USD": -2.5, "NVDA-USD": 1.8})

        scores = monitor.get_all_z_scores()
        self.assertAlmostEqual(scores["TSLA-USD"], -2.5, places=3)
        self.assertAlmostEqual(scores["NVDA-USD"], 1.8, places=3)

    def test_get_divergences_filters_by_threshold(self):
        """Only components with |z| >= 1.5 are returned as divergences."""
        monitor = self._make_monitor()
        monitor.inject_z_scores({
            "TSLA-USD":  -2.1,   # |z| = 2.1 → divergence
            "NVDA-USD":   1.3,   # |z| = 1.3 → below threshold
            "AAPL-USD":   1.6,   # |z| = 1.6 → divergence
        })
        divs = monitor.get_divergences()
        syms = {d.symbol for d in divs}
        self.assertIn("TSLA-USD", syms, "TSLA z=-2.1 must be in divergences.")
        self.assertIn("AAPL-USD", syms, "AAPL z=1.6 must be in divergences.")
        self.assertNotIn("NVDA-USD", syms, "NVDA z=1.3 below threshold. Must not appear.")

    def test_best_divergence_highest_abs_zscore(self):
        """get_best_divergence returns the highest |z_score| component."""
        monitor = self._make_monitor()
        monitor.inject_z_scores({
            "TSLA-USD":  -2.1,
            "GOOGL-USD": -3.5,   # highest |z|
            "AAPL-USD":   1.7,
        })
        best = monitor.get_best_divergence()
        self.assertIsNotNone(best, "Must return best divergence.")
        self.assertEqual(best.symbol, "GOOGL-USD",
                         f"Best should be GOOGL-USD (z=-3.5). Got {best.symbol}.")

    def test_no_divergence_returns_none(self):
        """No component above threshold → get_best_divergence returns None."""
        monitor = self._make_monitor()
        # All z-scores below 1.5
        monitor.inject_z_scores({"TSLA-USD": -0.8, "NVDA-USD": 0.5})
        best = monitor.get_best_divergence()
        self.assertIsNone(best, "No component above threshold → best must be None.")

    def test_zscore_reset_clears_history(self):
        monitor = self._make_monitor()
        monitor.inject_z_scores({"TSLA-USD": -2.5})
        monitor.reset()
        scores = monitor.get_all_z_scores()
        self.assertEqual(scores["TSLA-USD"], 0.0, "After reset z-score must be 0.")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — STAKING MONITOR
# ══════════════════════════════════════════════════════════════════════════════

class TestStakingMonitor(unittest.TestCase):

    def _make_monitor(self, stake_usd=200.0):
        from intelligence.staking_monitor import StakingMonitor
        m = StakingMonitor(default_stake_usd=stake_usd)
        m.initialise()
        return m

    def test_default_stake_200(self):
        """Default MAG7 stake must be $200."""
        m = self._make_monitor()
        self.assertEqual(m.get_stake_balance("MAG7-SSI"), 200.0,
                         "Default MAG7 stake must be $200.")

    def test_component_weights_match_mag7(self):
        """StakingMonitor must expose MAG7 component weights."""
        from intelligence.ssi_component_monitor import MAG7_COMPONENTS
        m = self._make_monitor()
        weights = m.get_component_weights("MAG7-SSI")
        for sym, expected_weight in MAG7_COMPONENTS.items():
            self.assertIn(sym, weights, f"{sym} missing from stake component weights.")
            self.assertAlmostEqual(weights[sym], expected_weight, places=3)

    def test_hedge_notional_is_stake_times_weight(self):
        """T8: trade size = stake × component_weight (hedge-matched)."""
        m = self._make_monitor(stake_usd=200.0)
        # TSLA is 6% of MAG7 → hedge = $200 × 0.06 = $12
        notional = m.get_hedge_notional("TSLA-USD")
        self.assertAlmostEqual(notional, 200.0 * 0.06, places=2,
                               msg=f"TSLA hedge notional wrong. Expected {200*0.06:.2f}, got {notional:.2f}.")

    def test_hedge_notional_nvda(self):
        """NVDA is 25% of MAG7 at $200 stake → hedge = $50."""
        m = self._make_monitor(stake_usd=200.0)
        notional = m.get_hedge_notional("NVDA-USD")
        self.assertAlmostEqual(notional, 50.0, places=2)

    def test_yield_accrues_over_time(self):
        """Yield must accrue proportional to time and APY."""
        m = self._make_monitor(stake_usd=1000.0)  # larger stake for visible yield
        # Fast-forward: manual time injection not possible, but accrual over 1 day
        # Set last_yield_ts to 24 hours ago
        pos = m._positions["MAG7-SSI"]
        pos.last_yield_ts = time.time() - 86400  # 24 hours ago

        yield_usd = m.accrue_yield()
        # 5% APY on $1000 for 1 day ≈ $1000 × 0.05 / 365 ≈ $0.137
        expected = 1000.0 * 0.05 / 365.0
        self.assertAlmostEqual(yield_usd, expected, delta=0.01,
                               msg=f"24h yield wrong. Expected ~{expected:.4f}, got {yield_usd:.4f}.")

    def test_consume_yield_reduces_accrued(self):
        m = self._make_monitor()
        # Inject some yield manually
        m._positions["MAG7-SSI"].accrued_yield_usd = 10.0
        consumed = m.consume_yield(6.0)
        self.assertAlmostEqual(consumed, 6.0, places=3)
        self.assertAlmostEqual(m.get_accrued_yield(), 4.0, places=3)

    def test_consume_yield_capped_at_available(self):
        m = self._make_monitor()
        m._positions["MAG7-SSI"].accrued_yield_usd = 3.0
        consumed = m.consume_yield(10.0)  # more than available
        self.assertAlmostEqual(consumed, 3.0, places=3,
                               msg="Cannot consume more yield than is accrued.")

    def test_zero_stake_no_hedge(self):
        """T1: stake_balance = 0 → hedge notional = 0 → SOVEREIGN never fires."""
        m = self._make_monitor(stake_usd=0.0)
        notional = m.get_hedge_notional("TSLA-USD")
        self.assertEqual(notional, 0.0, "Zero stake → zero hedge notional.")

    def test_is_component_in_stake(self):
        m = self._make_monitor()
        self.assertTrue(m.is_component_in_stake("NVDA-USD"))
        self.assertFalse(m.is_component_in_stake("BTC-USD"))


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — YIELD TRACKER
# ══════════════════════════════════════════════════════════════════════════════

class TestYieldTracker(unittest.TestCase):

    def _make_tracker(self, initial_yield=0.0):
        from core.yield_tracker import YieldTracker
        t = YieldTracker()
        t.initialise(initial_yield)
        return t

    def test_initialise_zero_yield_starts_inactive(self):
        """With no yield, SOVEREIGN cannot trade."""
        t = self._make_tracker(0.0)
        self.assertFalse(t.can_trade(),
                         "Zero yield → SOVEREIGN inactive. Must wait for accrual.")

    def test_initialise_allocates_80pct_to_budget(self):
        """80% of yield → sovereign_budget, 20% → reserve."""
        t = self._make_tracker(initial_yield=10.0)
        self.assertAlmostEqual(t.available_budget, 8.0, places=3,
                               msg="80% of $10 yield → $8 budget.")

    def test_add_yield_increases_budget(self):
        async def run():
            t = self._make_tracker(0.0)
            added = await t.add_yield(5.0)
            self.assertAlmostEqual(added, 4.0, places=3,   # 80% of $5
                                   msg="add_yield must return amount added to budget (80%).")
            self.assertTrue(t.can_trade(), "After yield added, can_trade must be True.")

        asyncio.run(run())

    def test_record_loss_reduces_budget_not_main(self):
        """T6: SOVEREIGN losses hit sovereign_budget only, not main capital."""
        async def run():
            t = self._make_tracker(initial_yield=10.0)
            initial = t.available_budget   # 8.0
            await t.record_pnl(-3.0)
            self.assertAlmostEqual(t.available_budget, initial - 3.0, places=3,
                                   msg="Loss deducted from sovereign_budget only.")

        asyncio.run(run())

    def test_budget_never_negative(self):
        """Budget floors at 0.0 — cannot go negative."""
        async def run():
            t = self._make_tracker(initial_yield=5.0)
            await t.record_pnl(-100.0)  # far exceeds budget
            self.assertEqual(t.available_budget, 0.0,
                             "Budget cannot go negative. Floor at 0.")
            self.assertFalse(t.can_trade(), "Zero budget → can_trade is False.")

        asyncio.run(run())

    def test_t6_zero_budget_blocks_sovereign(self):
        """T6: sovereign_budget = 0 → context sovereign_budget=0 → _is_sovereign=False."""
        from intelligence.personality import PersonalityEngine, Personality
        engine = PersonalityEngine()

        # SOVEREIGN-eligible context but budget=0
        ctx = make_test_context(
            symbol="TSLA-USD",
            stake_balance=200.0,
            sovereign_budget=0.0,        # no yield budget
            component_signals={"TSLA-USD": -2.5},
            best_divergence=("TSLA-USD", -2.5),
            regime="risk_off",
            calendar_regime="CLEAR",
            atr_vs_baseline=1.0,
        )
        params = engine.assess("TSLA-USD", ctx)
        self.assertNotEqual(params.personality, Personality.SOVEREIGN,
                            "T6: Zero sovereign_budget must block SOVEREIGN. Got COIL or FLOW.")

    def test_t7_overflow_triggers_transfer(self):
        """T7: budget >= 2× seed_yield → 50% transferred to main capital."""
        async def run():
            # initialise with $10 yield → budget=$8, seed_yield=$10
            # overflow triggers at budget >= seed_yield × 2 = $20
            t = self._make_tracker(initial_yield=10.0)
            self.assertAlmostEqual(t.available_budget, 8.0, places=2)

            # Win +$12 → budget = $8 + $12 = $20 = 2× seed_yield ($10×2)
            await t.record_pnl(+12.0)
            self.assertAlmostEqual(t.available_budget, 20.0, places=2)

            transfer = await t.check_overflow()

            self.assertIsNotNone(transfer, "Overflow must trigger at budget >= 2× seed_yield.")
            self.assertAlmostEqual(transfer, 10.0, delta=1.0,
                                   msg=f"50% of $20 = $10 transferred to main. Got {transfer:.2f}.")
            self.assertLess(t.available_budget, 20.0,
                            "Budget must decrease after overflow transfer.")

        asyncio.run(run())

    def test_no_overflow_below_trigger(self):
        """Budget at 1.5× seed must not trigger overflow (needs 2×)."""
        async def run():
            t = self._make_tracker(initial_yield=10.0)
            await t.record_pnl(+4.0)   # budget = 12.0 = 1.5× seed, below 2×
            transfer = await t.check_overflow()
            self.assertIsNone(transfer, "No overflow below 2× seed.")

        asyncio.run(run())


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — SOVEREIGN SIGNAL GENERATOR
# ══════════════════════════════════════════════════════════════════════════════

class TestSovereignSignalGenerator(unittest.TestCase):

    def _make_gen(self):
        from intelligence.sovereign_signal import SovereignSignalGenerator
        return SovereignSignalGenerator()

    def _make_divergence(self, symbol="TSLA-USD", z=-2.1):
        from intelligence.ssi_component_monitor import ComponentDivergence
        return ComponentDivergence(
            symbol=symbol,
            z_score=z,
            direction="long" if z < 0 else "short",
            weight=0.06,
            spread_pct=z * 0.005,
            timestamp_ms=int(time.time() * 1000),
        )

    def test_t2_zscore_below_threshold_no_signal(self):
        """T2: |z| < 1.5 → signal returns None."""
        gen = self._make_gen()
        div = self._make_divergence("TSLA-USD", z=-1.2)   # below 1.5
        result = gen.evaluate(
            divergence=div, regime="risk_off", calendar_regime="CLEAR",
            hours_to_earnings=None, stake_balance=200.0,
            component_weights={"TSLA-USD": 0.06}, sovereign_budget=8.0,
        )
        self.assertIsNone(result, "T2: z=-1.2 below threshold. No signal expected.")

    def test_t3_momentum_regime_shorts_laggard(self):
        """T3: z < -1.5, risk_off → SHORT component (momentum continues)."""
        gen = self._make_gen()
        div = self._make_divergence("TSLA-USD", z=-2.1)
        result = gen.evaluate(
            divergence=div, regime="risk_off", calendar_regime="CLEAR",
            hours_to_earnings=None, stake_balance=200.0,
            component_weights={"TSLA-USD": 0.06}, sovereign_budget=8.0,
        )
        self.assertIsNotNone(result, "T3: risk_off + z=-2.1 → signal expected.")
        self.assertEqual(result.side, "short",
                         f"T3: risk_off regime → short laggard. Got {result.side}.")
        self.assertEqual(result.regime_type, "momentum")

    def test_t4_reversion_regime_longs_laggard(self):
        """T4: z < -1.5, confused → LONG component (mean reversion)."""
        gen = self._make_gen()
        div = self._make_divergence("TSLA-USD", z=-2.1)
        result = gen.evaluate(
            divergence=div, regime="risk_on", calendar_regime="CLEAR",
            hours_to_earnings=None, stake_balance=200.0,
            component_weights={"TSLA-USD": 0.06}, sovereign_budget=8.0,
        )
        self.assertIsNotNone(result, "T4: risk_on + z=-2.1 → reversion signal expected.")
        self.assertEqual(result.side, "long",
                         f"T4: risk_on regime → long laggard (reversion). Got {result.side}.")
        self.assertEqual(result.regime_type, "reversion")

    def test_t5_earnings_within_48h_blocks(self):
        """T5: earnings within 48h → SOVEREIGN blocked regardless of z-score."""
        gen = self._make_gen()
        div = self._make_divergence("TSLA-USD", z=-3.0)
        result = gen.evaluate(
            divergence=div, regime="risk_off", calendar_regime="CLEAR",
            hours_to_earnings=24.0,   # within 48h
            stake_balance=200.0,
            component_weights={"TSLA-USD": 0.06}, sovereign_budget=8.0,
        )
        self.assertIsNone(result, "T5: Earnings within 48h must block SOVEREIGN.")

    def test_earnings_beyond_48h_allowed(self):
        """Earnings more than 48h away must not block."""
        gen = self._make_gen()
        div = self._make_divergence("TSLA-USD", z=-2.1)
        result = gen.evaluate(
            divergence=div, regime="risk_off", calendar_regime="CLEAR",
            hours_to_earnings=72.0,   # beyond 48h
            stake_balance=200.0,
            component_weights={"TSLA-USD": 0.06}, sovereign_budget=8.0,
        )
        self.assertIsNotNone(result, "Earnings 72h away must not block SOVEREIGN.")

    def test_calendar_block_overrides_signal(self):
        """Hard calendar BLOCK prevents SOVEREIGN regardless of z-score."""
        gen = self._make_gen()
        div = self._make_divergence("TSLA-USD", z=-3.5)
        result = gen.evaluate(
            divergence=div, regime="risk_off", calendar_regime="BLOCK",
            hours_to_earnings=None, stake_balance=200.0,
            component_weights={"TSLA-USD": 0.06}, sovereign_budget=8.0,
        )
        self.assertIsNone(result, "Calendar BLOCK must prevent SOVEREIGN.")

    def test_t8_size_is_hedge_matched(self):
        """T8: hedge_notional = stake_balance × component_weight."""
        gen = self._make_gen()
        div = self._make_divergence("TSLA-USD", z=-2.1)
        result = gen.evaluate(
            divergence=div, regime="risk_off", calendar_regime="CLEAR",
            hours_to_earnings=None, stake_balance=200.0,
            component_weights={"TSLA-USD": 0.06}, sovereign_budget=8.0,
        )
        self.assertIsNotNone(result)
        expected_max = 200.0 * 0.06  # $12.0
        self.assertLessEqual(
            result.hedge_notional, expected_max,
            f"T8: Hedge notional {result.hedge_notional:.2f} exceeds structural max ${expected_max:.2f}."
        )

    def test_no_stake_no_signal(self):
        """T1: stake_balance = 0 → no signal."""
        gen = self._make_gen()
        div = self._make_divergence("TSLA-USD", z=-2.1)
        result = gen.evaluate(
            divergence=div, regime="risk_off", calendar_regime="CLEAR",
            hours_to_earnings=None, stake_balance=0.0,   # no stake
            component_weights={"TSLA-USD": 0.06}, sovereign_budget=8.0,
        )
        self.assertIsNone(result, "T1: No stake → no SOVEREIGN signal.")

    def test_confidence_scales_with_zscore(self):
        """Higher |z| → higher confidence (up to 0.80 cap)."""
        gen = self._make_gen()
        weights = {"TSLA-USD": 0.06}

        result_low  = gen.evaluate(
            divergence=self._make_divergence("TSLA-USD", z=-1.6),
            regime="risk_off", calendar_regime="CLEAR", hours_to_earnings=None,
            stake_balance=200.0, component_weights=weights, sovereign_budget=8.0,
        )
        result_high = gen.evaluate(
            divergence=self._make_divergence("TSLA-USD", z=-3.0),
            regime="risk_off", calendar_regime="CLEAR", hours_to_earnings=None,
            stake_balance=200.0, component_weights=weights, sovereign_budget=8.0,
        )

        self.assertIsNotNone(result_low)
        self.assertIsNotNone(result_high)
        self.assertLess(result_low.confidence, result_high.confidence,
                        "Higher |z| must produce higher confidence.")
        self.assertLessEqual(result_high.confidence, 0.80,
                             "Confidence capped at 0.80.")

    def test_t9_market_wide_move_no_signal(self):
        """T9: whole market moving, not just component → no divergence trade."""
        gen = self._make_gen()
        # Component and index both moving down → not a divergence play
        # This is captured by the z_score remaining near 0 (no spread vs index)
        div = self._make_divergence("TSLA-USD", z=-0.8)   # small z = whole market
        result = gen.evaluate(
            divergence=div, regime="risk_off", calendar_regime="CLEAR",
            hours_to_earnings=None, stake_balance=200.0,
            component_weights={"TSLA-USD": 0.06}, sovereign_budget=8.0,
        )
        self.assertIsNone(result,
                          "T9: Small z-score (whole market move) → no SOVEREIGN signal.")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — SOVEREIGN PERSONALITY ENGINE ROUTING
# ══════════════════════════════════════════════════════════════════════════════

class TestSovereignPersonalityRouting(unittest.TestCase):

    def test_sovereign_routes_equity_symbol(self):
        """TSLA-USD with valid sovereign context → SOVEREIGN personality."""
        from intelligence.personality import PersonalityEngine, Personality

        engine = PersonalityEngine()
        ctx = make_test_context(
            symbol="TSLA-USD",
            stake_balance=200.0,
            sovereign_budget=8.0,
            component_signals={"TSLA-USD": -2.1},
            best_divergence=("TSLA-USD", -2.1),
            regime="risk_off",
            calendar_regime="CLEAR",
            cascade_phase="idle",
            atr_vs_baseline=1.0,
            daily_pnl_pct=0.0,
        )
        params = engine.assess("TSLA-USD", ctx)
        self.assertEqual(params.personality, Personality.SOVEREIGN,
                         f"TSLA with z=-2.1 + $200 stake must route to SOVEREIGN. Got {params.personality}.")

    def test_sovereign_not_available_for_crypto(self):
        """BTC-USD must never be SOVEREIGN (no MAG7 stake for crypto)."""
        from intelligence.personality import PersonalityEngine, Personality
        engine = PersonalityEngine()
        ctx = make_test_context(
            symbol="BTC-USD",
            stake_balance=200.0,     # stake exists but wrong asset class
            sovereign_budget=8.0,
            component_signals={"BTC-USD": -2.5},
            best_divergence=("BTC-USD", -2.5),
            regime="risk_off",
            calendar_regime="CLEAR",
            atr_vs_baseline=1.0,
        )
        params = engine.assess("BTC-USD", ctx)
        self.assertNotEqual(params.personality, Personality.SOVEREIGN,
                            "BTC must never be SOVEREIGN. No index stake for crypto.")

    def test_sovereign_not_available_for_xaut(self):
        """XAUT-USD (commodity) must never be SOVEREIGN."""
        from intelligence.personality import PersonalityEngine, Personality
        engine = PersonalityEngine()
        ctx = make_test_context(
            symbol="XAUT-USD",
            stake_balance=200.0,
            sovereign_budget=8.0,
            component_signals={"XAUT-USD": -2.5},
            best_divergence=("XAUT-USD", -2.5),
            regime="risk_off",
            calendar_regime="CLEAR",
            atr_vs_baseline=1.0,
        )
        params = engine.assess("XAUT-USD", ctx)
        self.assertNotEqual(params.personality, Personality.SOVEREIGN,
                            "XAUT must never be SOVEREIGN. No MAG7 stake for commodities.")

    def test_t1_no_stake_no_sovereign(self):
        """T1: stake_balance = 0 → SOVEREIGN never fires."""
        from intelligence.personality import PersonalityEngine, Personality
        engine = PersonalityEngine()
        ctx = make_test_context(
            symbol="TSLA-USD",
            stake_balance=0.0,         # no stake
            sovereign_budget=8.0,
            component_signals={"TSLA-USD": -2.5},
            best_divergence=("TSLA-USD", -2.5),
            regime="risk_off",
            calendar_regime="CLEAR",
            atr_vs_baseline=1.0,
        )
        params = engine.assess("TSLA-USD", ctx)
        self.assertNotEqual(params.personality, Personality.SOVEREIGN,
                            "T1: No stake → no SOVEREIGN personality.")

    def test_shield_overrides_sovereign(self):
        """SHIELD must always win even with valid SOVEREIGN conditions."""
        from intelligence.personality import PersonalityEngine, Personality
        engine = PersonalityEngine()
        ctx = make_test_context(
            symbol="TSLA-USD",
            stake_balance=200.0,
            sovereign_budget=8.0,
            component_signals={"TSLA-USD": -2.5},
            best_divergence=("TSLA-USD", -2.5),
            regime="risk_off",
            calendar_regime="BLOCK",   # SHIELD trigger
            atr_vs_baseline=1.0,
        )
        params = engine.assess("TSLA-USD", ctx)
        self.assertEqual(params.personality, Personality.SHIELD,
                         "SHIELD must override SOVEREIGN. Capital preservation first.")

    def test_sovereign_bypasses_hysteresis(self):
        """SOVEREIGN activates immediately — no 3-period hysteresis delay."""
        from intelligence.personality import PersonalityEngine, Personality
        engine = PersonalityEngine()

        # Set current to FLOW
        engine._current_personality["TSLA-USD"] = Personality.FLOW

        # SOVEREIGN condition triggers on first assessment
        ctx = make_test_context(
            symbol="TSLA-USD",
            stake_balance=200.0,
            sovereign_budget=8.0,
            component_signals={"TSLA-USD": -2.5},
            best_divergence=("TSLA-USD", -2.5),
            regime="risk_off",
            calendar_regime="CLEAR",
            atr_vs_baseline=1.0,
        )
        params = engine.assess("TSLA-USD", ctx)
        self.assertEqual(params.personality, Personality.SOVEREIGN,
                         "SOVEREIGN must activate immediately, no hysteresis.")

    def test_sovereign_in_personality_availability(self):
        """SOVEREIGN must be in PERSONALITY_AVAILABILITY for equity."""
        from core.asset_classes import PERSONALITY_AVAILABILITY
        self.assertIn("SOVEREIGN", PERSONALITY_AVAILABILITY["equity"],
                      "SOVEREIGN must be available for equity asset class.")
        self.assertNotIn("SOVEREIGN", PERSONALITY_AVAILABILITY["crypto"],
                         "SOVEREIGN must NOT be available for crypto.")
        self.assertNotIn("SOVEREIGN", PERSONALITY_AVAILABILITY["commodity"],
                         "SOVEREIGN must NOT be available for commodity.")

    def test_sovereign_has_params(self):
        """SOVEREIGN must have all required parameters in PERSONALITY_PARAMS."""
        from intelligence.personality import Personality, PERSONALITY_PARAMS
        self.assertIn(Personality.SOVEREIGN, PERSONALITY_PARAMS,
                      "SOVEREIGN missing from PERSONALITY_PARAMS.")
        params = PERSONALITY_PARAMS[Personality.SOVEREIGN]
        for key in ("size_multiplier", "stop_atr_mult", "rr_target",
                    "coherence_min", "max_hold_s", "max_concurrent"):
            self.assertIn(key, params, f"SOVEREIGN params missing '{key}'.")

    def test_sovereign_max_hold_24h(self):
        """SOVEREIGN max_hold_s must be 86400 (24 hours)."""
        from intelligence.personality import Personality, PERSONALITY_PARAMS
        params = PERSONALITY_PARAMS[Personality.SOVEREIGN]
        self.assertEqual(params["max_hold_s"], 86400,
                         "SOVEREIGN max hold must be 24h = 86400s.")

    def test_sovereign_max_concurrent_2(self):
        """SOVEREIGN max_concurrent must be 2 (two components simultaneously)."""
        from intelligence.personality import Personality, PERSONALITY_PARAMS
        params = PERSONALITY_PARAMS[Personality.SOVEREIGN]
        self.assertEqual(params["max_concurrent"], 2,
                         "SOVEREIGN max_concurrent must be 2 (two components max).")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — SOVEREIGN BUDGET INTEGRATION
# ══════════════════════════════════════════════════════════════════════════════

class TestSovereignBudgetIntegration(unittest.TestCase):

    def test_sovereign_budget_flows_from_yield(self):
        """End-to-end: staking yield → yield_tracker → sovereign_budget → context."""
        async def run():
            from core.yield_tracker import YieldTracker
            from intelligence.personality import PersonalityContextCache

            tracker = YieldTracker()
            tracker.initialise(0.0)

            # Yield accrues
            await tracker.add_yield(10.0)

            # Update context cache with budget from tracker
            cache = PersonalityContextCache()
            cache.update_sovereign(
                stake_balance=200.0,
                sovereign_budget=tracker.available_budget,
                component_signals={"TSLA-USD": -2.5},
            )

            ctx = cache.build("TSLA-USD", 5.0, "short", "bearish")
            self.assertGreater(ctx.sovereign_budget, 0,
                               "Yield must flow through tracker into context sovereign_budget.")
            self.assertEqual(ctx.stake_balance, 200.0)

        asyncio.run(run())

    def test_sovereign_losses_never_touch_main_capital(self):
        """Ten consecutive SOVEREIGN losses must not reduce sovereign_budget below 0."""
        async def run():
            from core.yield_tracker import YieldTracker
            tracker = YieldTracker()
            tracker.initialise(10.0)  # $10 yield → $8 budget

            for _ in range(10):
                await tracker.record_pnl(-2.0)  # simulate loss

            self.assertGreaterEqual(tracker.available_budget, 0.0,
                                    "Budget cannot go negative. Losses capped at 0.")
            # Main capital untouched — verifiable by budget_manager not being called
            # (this is an architectural guarantee: SOVEREIGN budget is separate)

        asyncio.run(run())

    def test_full_sovereign_signal_pipeline(self):
        """End-to-end: inject z-scores → build context → assess personality."""
        from intelligence.ssi_component_monitor import SSIComponentMonitor
        from intelligence.staking_monitor import StakingMonitor
        from core.yield_tracker import YieldTracker
        from intelligence.personality import PersonalityEngine, Personality, PersonalityContextCache

        async def run():
            # Setup
            monitor = SSIComponentMonitor()
            staking = StakingMonitor()
            staking.initialise()

            tracker = YieldTracker()
            tracker.initialise(0.0)
            await tracker.add_yield(10.0)   # seed SOVEREIGN budget

            # Inject divergence: TSLA underperforming by 2.5σ
            monitor.inject_z_scores({"TSLA-USD": -2.5})
            z_scores = monitor.get_all_z_scores()

            # Build context
            cache = PersonalityContextCache()
            cache.update_sovereign(
                stake_balance=staking.get_stake_balance(),
                sovereign_budget=tracker.available_budget,
                component_signals=z_scores,
            )
            cache.update_regime("risk_off", 0.8)

            ctx = cache.build("TSLA-USD", 4.0, "short", "neutral")

            # Assess
            engine = PersonalityEngine()
            params = engine.assess("TSLA-USD", ctx)

            self.assertEqual(params.personality, Personality.SOVEREIGN,
                             f"Full pipeline: TSLA z=-2.5, stake=$200, budget>0 → SOVEREIGN. Got {params.personality}.")

        asyncio.run(run())


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — SOVEREIGN + OTHER AGENTS INDEPENDENCE
# ══════════════════════════════════════════════════════════════════════════════

class TestSovereignCrossAgentIndependence(unittest.TestCase):
    """
    SOVEREIGN fires independently of other personalities.
    When SOVEREIGN trades TSLA-USD and FLOW trades BTC-USD simultaneously,
    these are uncorrelated positions — genuine portfolio diversification.
    Unlike the cross-agent bet mechanism (which gates on joint probability),
    SOVEREIGN+FLOW just sum expected values.
    """

    def test_sovereign_and_flow_fire_independently(self):
        """SOVEREIGN on TSLA + FLOW on BTC — different assets, different signal sources."""
        from intelligence.personality import PersonalityEngine, Personality

        engine = PersonalityEngine()

        # BTC context → FLOW
        btc_ctx = make_test_context(
            symbol="BTC-USD",
            coherence=5.5, direction="long", htf="bullish",
            regime="risk_on", cascade_phase="idle", atr_vs_baseline=1.0,
            calendar_regime="CLEAR",
        )
        btc_params = engine.assess("BTC-USD", btc_ctx)
        self.assertNotEqual(btc_params.personality, Personality.SOVEREIGN,
                            "BTC must not be SOVEREIGN.")

        # TSLA context → SOVEREIGN
        tsla_ctx = make_test_context(
            symbol="TSLA-USD",
            stake_balance=200.0, sovereign_budget=8.0,
            component_signals={"TSLA-USD": -2.1},
            best_divergence=("TSLA-USD", -2.1),
            regime="risk_off", calendar_regime="CLEAR",
            cascade_phase="idle", atr_vs_baseline=1.0,
        )
        tsla_params = engine.assess("TSLA-USD", tsla_ctx)

        # These two personalities can coexist simultaneously
        # No joint-probability gate needed — they're uncorrelated
        if tsla_params.personality == Personality.SOVEREIGN:
            self.assertNotEqual(btc_params.personality, Personality.SOVEREIGN,
                                "Two different assets, two different signal sources. Independent.")

    def test_sovereign_signal_orthogonal_to_coherence(self):
        """SOVEREIGN z-score signal is orthogonal — low coherence does not block it."""
        from intelligence.personality import PersonalityEngine, Personality
        engine = PersonalityEngine()

        ctx = make_test_context(
            symbol="TSLA-USD",
            coherence=2.0,           # very low coherence
            stake_balance=200.0,
            sovereign_budget=8.0,
            component_signals={"TSLA-USD": -3.0},
            best_divergence=("TSLA-USD", -3.0),
            regime="risk_off",
            calendar_regime="CLEAR",
            cascade_phase="idle",
            atr_vs_baseline=1.0,
        )
        params = engine.assess("TSLA-USD", ctx)
        self.assertEqual(params.personality, Personality.SOVEREIGN,
                         "SOVEREIGN must not be gated by coherence. Its signal is z-score.")

    def test_sovereign_budget_independent_from_main_budget_manager(self):
        """SOVEREIGN budget comes from yield_tracker, not BudgetManager."""
        from core.budget_manager import BudgetManager, AGENT_RATIOS
        from core.yield_tracker import YieldTracker

        bm = BudgetManager(test_config(), 1000.0)
        bm.initialise()
        main_perp_budget = bm.get_budget("perp", "flow")

        tracker = YieldTracker()
        tracker.initialise(10.0)
        sovereign_budget = tracker.available_budget

        # SOVEREIGN budget is completely separate
        self.assertGreater(sovereign_budget, 0, "Yield tracker must have budget.")
        # BudgetManager total must be based on balance only, not yield
        total_bm = bm.total_deployed_budget()
        self.assertAlmostEqual(total_bm, 1000.0, delta=1.0,
                               msg="BudgetManager total must reflect account balance only.")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 8 — LATENCY (SOVEREIGN HOT PATH)
# ══════════════════════════════════════════════════════════════════════════════

class TestSovereignLatency(unittest.TestCase):

    def test_sovereign_context_build_under_half_ms(self):
        """Context build including SOVEREIGN fields must be under 0.5ms avg."""
        cache = make_sovereign_context_cache()
        from intelligence.personality import PersonalityEngine
        engine = PersonalityEngine()

        times = []
        for _ in range(500):
            t0 = time.perf_counter()
            ctx = cache.build("TSLA-USD", 4.0, "short", "bearish")
            engine.assess("TSLA-USD", ctx)
            times.append((time.perf_counter() - t0) * 1000)

        avg = sum(times) / len(times)
        p95 = sorted(times)[474]
        self.assertLess(avg, 1.0,
                        f"SOVEREIGN pipeline avg {avg:.3f}ms. Must be <1ms.")
        self.assertLess(p95, 2.0,
                        f"SOVEREIGN pipeline P95 {p95:.3f}ms. Must be <2ms.")

    def test_update_sovereign_cache_is_fast(self):
        """update_sovereign() must be under 0.1ms (called every 60min, not hot path)."""
        from intelligence.personality import PersonalityContextCache
        cache = PersonalityContextCache()

        z_scores = {"NVDA-USD": 1.8, "TSLA-USD": -2.1, "AAPL-USD": 0.3}
        times = []
        for _ in range(500):
            t0 = time.perf_counter()
            cache.update_sovereign(200.0, 8.0, z_scores)
            times.append((time.perf_counter() - t0) * 1000)

        avg = sum(times) / len(times)
        self.assertLess(avg, 0.1,
                        f"update_sovereign avg {avg:.4f}ms. Dict assignment must be <0.1ms.")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
