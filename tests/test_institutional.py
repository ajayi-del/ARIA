"""
ARIA Institutional Test Suite — Phase 12-13 Validation

Covers:
  1.  ATR Gate Fix
  2.  Six Personality States
  3.  Personality Hysteresis
  4.  Personality Context Cache
  5.  Budget Manager
  6.  Prediction Market
  7.  Terrain-Aware Coherence
  8.  Agent Terrain Rules
  9.  RPC Health and Freeze
  10. ML Classifier
  11. Latency Budget
  12. Integration

Run:
    python tests/test_institutional.py
    python tests/test_institutional.py -v
"""

import sys
import os
import unittest
import asyncio
import time
import math
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.helpers import (
    make_test_personality_engine,
    make_test_context,
    make_test_context_cache,
    make_warmed_context_cache,
    make_test_personality_state,
    make_test_context_object,
    make_market_state_no_micro,
    make_neutral_market_state,
    make_test_budget_manager,
    make_test_prediction,
    make_test_candidate,
    make_journal_with_trades,
    test_config,
)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — ATR GATE FIX VALIDATION
# ══════════════════════════════════════════════════════════════════════════════

class TestATRGateRemoved(unittest.TestCase):

    def test_atr_vs_baseline_exists_in_market_state(self):
        """MarketState must have atr_vs_baseline field."""
        from intelligence.market_state import MarketState
        state = make_neutral_market_state("BTC-USD")
        self.assertTrue(
            hasattr(state, "atr_vs_baseline"),
            "MarketState missing atr_vs_baseline. Required for personality routing."
        )

    def test_low_atr_ratio_routes_to_coil(self):
        """atr_vs_baseline < threshold → COIL, not a hard block."""
        from intelligence.personality import MarketPersonalityEngine, Personality
        engine = make_test_personality_engine()
        ctx = make_test_context(
            symbol="BTC-USD",
            atr_vs_baseline=0.55,   # below 0.80 threshold
            coherence=5.0,
            direction="long",
            htf="bullish",
            regime="risk_on",
            cascade_phase="idle",
            calendar_regime="CLEAR",
        )
        state = engine.assess("BTC-USD", ctx)
        self.assertEqual(
            state.personality, Personality.COIL,
            f"Got {state.personality} — expected COIL. Low ATR routes to COIL not block."
        )
        self.assertEqual(
            state.size_multiplier, 0.0,
            "COIL size_multiplier must be 0.0 — no directional trades in COIL."
        )

    def test_normal_atr_ratio_not_coil(self):
        """atr_vs_baseline=0.99 must NOT be COIL."""
        from intelligence.personality import MarketPersonalityEngine, Personality
        engine = make_test_personality_engine()
        ctx = make_test_context(
            symbol="BTC-USD",
            atr_vs_baseline=0.99,
            coherence=5.5,
            direction="long",
            htf="bullish",
            regime="risk_on",
            cascade_phase="idle",
            calendar_regime="CLEAR",
            basis_stress_count=0,
        )
        state = engine.assess("BTC-USD", ctx)
        self.assertNotEqual(
            state.personality, Personality.COIL,
            f"BTC with ATRx=0.99 should not be COIL. Got {state.personality}."
        )
        self.assertGreater(
            state.size_multiplier, 0.0,
            "Normal ATR should produce non-zero size."
        )

    def test_arb_usd_would_now_trade(self):
        """ARB-USD with atr_vs_baseline=0.92 should be SCOUT or FLOW, not COIL."""
        from intelligence.personality import MarketPersonalityEngine, Personality
        engine = make_test_personality_engine()
        ctx = make_test_context(
            symbol="ARB-USD",
            atr_vs_baseline=0.92,
            coherence=4.5,
            direction="long",
            htf="bullish",
            regime="confused",
            cascade_phase="idle",
            calendar_regime="CLEAR",
        )
        state = engine.assess("ARB-USD", ctx)
        self.assertNotIn(
            state.personality,
            [Personality.COIL, Personality.SHIELD],
            f"ARB-USD with ATRx=0.92 blocked as {state.personality}. Should be SCOUT or FLOW."
        )

    def test_atr_vs_baseline_computation(self):
        """compute_atr_vs_baseline: current / mean(history), 1.0 for empty."""
        from intelligence.coherence import compute_atr_vs_baseline

        current = 61.38
        history = [58.0, 62.0, 65.0, 60.0, 63.0,
                   59.0, 61.0, 64.0, 57.0, 66.0,
                   60.0, 62.0, 61.0, 63.0, 58.0,
                   64.0, 60.0, 61.0, 62.0, 65.0]
        baseline = sum(history) / len(history)
        expected = current / baseline
        result = compute_atr_vs_baseline(current, history)
        self.assertAlmostEqual(result, expected, places=3)

        # Empty history → 1.0 (neutral)
        result_zero = compute_atr_vs_baseline(61.38, [])
        self.assertEqual(result_zero, 1.0, "Empty ATR history must return 1.0 not crash.")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — SIX PERSONALITY VALIDATION
# ══════════════════════════════════════════════════════════════════════════════

class TestSixPersonalities(unittest.TestCase):

    def test_all_six_exist_in_enum(self):
        from intelligence.personality import Personality
        # Core 6 directional/structural personalities — SOVEREIGN is additive
        required = {"APEX", "AFTERMATH", "FLOW", "SCOUT", "COIL", "SHIELD"}
        actual = {p.name for p in Personality}
        self.assertTrue(required.issubset(actual),
            f"Missing personalities: {required - actual}")

    def test_all_six_have_params(self):
        from intelligence.personality import Personality, PERSONALITY_PARAMS
        for p in Personality:
            self.assertIn(p, PERSONALITY_PARAMS, f"{p.name} missing from PERSONALITY_PARAMS.")
            params = PERSONALITY_PARAMS[p]
            required_keys = {"size_multiplier", "stop_atr_mult", "rr_target",
                             "coherence_min", "max_hold_s", "max_concurrent"}
            for key in required_keys:
                self.assertIn(key, params, f"{p.name} params missing {key}")

    def test_shield_triggers_calendar_block(self):
        from intelligence.personality import MarketPersonalityEngine, Personality
        engine = make_test_personality_engine()
        ctx = make_test_context(calendar_regime="BLOCK")
        state = engine.assess("BTC-USD", ctx)
        self.assertEqual(state.personality, Personality.SHIELD,
                         "Calendar BLOCK must force SHIELD.")
        self.assertEqual(state.size_multiplier, 0.0, "SHIELD size must be 0.0.")

    def test_shield_triggers_daily_loss(self):
        from intelligence.personality import MarketPersonalityEngine, Personality
        engine = make_test_personality_engine()
        ctx = make_test_context(daily_pnl_pct=-0.026)
        state = engine.assess("BTC-USD", ctx)
        self.assertEqual(state.personality, Personality.SHIELD,
                         "Daily loss beyond 2.5% must force SHIELD.")

    def test_shield_soft_triggers_combo(self):
        from intelligence.personality import MarketPersonalityEngine, Personality
        engine = make_test_personality_engine()
        ctx = make_test_context(
            calendar_regime="caution",
            hours_to_event=1.5,
            basis_stress_count=2,
        )
        state = engine.assess("BTC-USD", ctx)
        self.assertEqual(state.personality, Personality.SHIELD,
                         "CAUTION + basis_stress>=2 = SHIELD (two soft triggers).")

    def test_aftermath_triggers_correctly(self):
        from intelligence.personality import MarketPersonalityEngine, Personality
        engine = make_test_personality_engine()
        ctx = make_test_context(
            cascade_phase="aftermath",
            aftermath_signals=3,
            cascade_direction="bearish",
            direction="long",   # opposite to cascade
            coherence=4.5,
            calendar_regime="CLEAR",
            atr_vs_baseline=1.0,
        )
        state = engine.assess("BTC-USD", ctx)
        self.assertEqual(state.personality, Personality.AFTERMATH,
                         f"Got {state.personality}. aftermath+3signals+opposite dir = AFTERMATH.")

    def test_aftermath_beats_apex_priority(self):
        """cascade_phase=aftermath means cascade is OVER — AFTERMATH wins over APEX."""
        from intelligence.personality import MarketPersonalityEngine, Personality
        engine = make_test_personality_engine()
        ctx = make_test_context(
            cascade_phase="aftermath",
            aftermath_signals=3,
            cascade_direction="bearish",
            direction="long",
            coherence=7.0,
            calendar_regime="CLEAR",
            cascade_notional=50000,
            atr_vs_baseline=1.0,
        )
        state = engine.assess("BTC-USD", ctx)
        self.assertEqual(state.personality, Personality.AFTERMATH,
                         f"Got {state.personality}. AFTERMATH must beat APEX priority.")

    def test_aftermath_requires_minimum_signals(self):
        from intelligence.personality import MarketPersonalityEngine, Personality
        engine = make_test_personality_engine()
        ctx = make_test_context(
            cascade_phase="aftermath",
            aftermath_signals=1,   # below minimum 2
            cascade_direction="bearish",
            direction="long",
            coherence=5.0,
            atr_vs_baseline=1.0,
        )
        state = engine.assess("BTC-USD", ctx)
        self.assertNotEqual(state.personality, Personality.AFTERMATH,
                            "AFTERMATH requires aftermath_signals >= 2. Only 1 → not AFTERMATH.")

    def test_apex_requires_active_cascade(self):
        from intelligence.personality import MarketPersonalityEngine, Personality
        engine = make_test_personality_engine()
        ctx = make_test_context(
            cascade_phase="idle",
            coherence=7.0,
            direction="short",
            htf="bearish",
            regime="risk_off",
            calendar_regime="CLEAR",
            cascade_notional=0,
            atr_vs_baseline=1.0,
        )
        state = engine.assess("BTC-USD", ctx)
        self.assertNotEqual(state.personality, Personality.APEX,
                            "APEX requires active cascade. idle phase → no APEX.")

    def test_apex_blocked_rpc_degraded(self):
        from intelligence.personality import MarketPersonalityEngine, Personality
        engine = make_test_personality_engine()
        ctx = make_test_context(
            cascade_phase="momentum",
            cascade_notional=50000,
            cascade_direction="bearish",
            direction="short",
            coherence=7.0,
            calendar_regime="CLEAR",
            rpc_health_score=0.50,
            atr_vs_baseline=1.0,
        )
        state = engine.assess("BTC-USD", ctx)
        self.assertNotEqual(state.personality, Personality.APEX,
                            "APEX must be blocked when RPC health < 0.70.")

    def test_apex_minimum_notional(self):
        from intelligence.personality import MarketPersonalityEngine, Personality
        engine = make_test_personality_engine()
        ctx = make_test_context(
            cascade_phase="momentum",
            cascade_notional=5000,   # below $10k
            cascade_direction="bearish",
            direction="short",
            coherence=7.0,
            calendar_regime="CLEAR",
            rpc_health_score=1.0,
            atr_vs_baseline=1.0,
        )
        state = engine.assess("BTC-USD", ctx)
        self.assertNotEqual(state.personality, Personality.APEX,
                            "APEX requires cascade_notional > $10,000. 5000 is noise.")

    def test_flow_requires_htf_alignment(self):
        from intelligence.personality import MarketPersonalityEngine, Personality
        engine = make_test_personality_engine()
        ctx = make_test_context(
            cascade_phase="idle",
            coherence=5.0,
            direction="long",
            htf="bearish",   # opposing HTF
            regime="risk_on",
            atr_vs_baseline=1.0,
        )
        state = engine.assess("BTC-USD", ctx)
        self.assertNotEqual(state.personality, Personality.FLOW,
                            "FLOW requires HTF alignment. long+bearish HTF = no FLOW.")

    def test_scout_is_the_fallback(self):
        from intelligence.personality import MarketPersonalityEngine, Personality
        engine = make_test_personality_engine()
        ctx = make_test_context(
            cascade_phase="idle",
            coherence=4.2,
            direction="long",
            htf="neutral",
            regime="confused",
            atr_vs_baseline=0.95,
            calendar_regime="CLEAR",
            basis_stress_count=0,
            daily_pnl_pct=0.0,
        )
        state = engine.assess("BTC-USD", ctx)
        self.assertEqual(state.personality, Personality.SCOUT,
                         f"Got {state.personality}. Weak signal + normal ATR + confused = SCOUT.")

    def test_personality_check_order(self):
        """SHIELD must always win, even over active APEX cascade."""
        from intelligence.personality import MarketPersonalityEngine, Personality
        engine = make_test_personality_engine()
        ctx = make_test_context(
            calendar_regime="BLOCK",
            cascade_phase="momentum",
            cascade_notional=100000,
            coherence=8.0,
            atr_vs_baseline=1.0,
        )
        state = engine.assess("BTC-USD", ctx)
        self.assertEqual(state.personality, Personality.SHIELD,
                         "SHIELD must override everything including active APEX cascade.")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — HYSTERESIS VALIDATION
# ══════════════════════════════════════════════════════════════════════════════

class TestPersonalityHysteresis(unittest.TestCase):

    def test_requires_three_confirmations(self):
        from intelligence.personality import MarketPersonalityEngine, Personality
        engine = make_test_personality_engine()

        flow_ctx = make_test_context(
            cascade_phase="idle", coherence=5.0,
            direction="short", htf="bearish",
            regime="risk_off", atr_vs_baseline=1.0,
        )
        engine.assess("BTC-USD", flow_ctx)
        engine._current_personality["BTC-USD"] = Personality.FLOW

        scout_ctx = make_test_context(
            cascade_phase="idle", coherence=4.2,
            direction="long", htf="neutral",
            regime="confused", atr_vs_baseline=0.95,
        )
        r1 = engine.assess("BTC-USD", scout_ctx)
        self.assertEqual(r1.personality, Personality.FLOW,
                         "After 1st SCOUT assessment: still FLOW (hysteresis).")

        r2 = engine.assess("BTC-USD", scout_ctx)
        self.assertEqual(r2.personality, Personality.FLOW,
                         "After 2nd SCOUT assessment: still FLOW (hysteresis).")

        r3 = engine.assess("BTC-USD", scout_ctx)
        self.assertEqual(r3.personality, Personality.SCOUT,
                         "After 3rd SCOUT assessment: switched to SCOUT.")

    def test_counter_resets_on_interruption(self):
        from intelligence.personality import MarketPersonalityEngine, Personality
        engine = make_test_personality_engine()
        engine._current_personality["BTC-USD"] = Personality.FLOW

        scout_ctx = make_test_context(
            coherence=4.2, regime="confused",
            htf="neutral", cascade_phase="idle",
            atr_vs_baseline=0.95,
        )
        flow_ctx = make_test_context(
            coherence=5.0, regime="risk_off",
            htf="bearish", direction="short",
            cascade_phase="idle", atr_vs_baseline=1.0,
        )

        engine.assess("BTC-USD", scout_ctx)   # 1st SCOUT
        engine.assess("BTC-USD", flow_ctx)    # breaks streak → reset
        engine.assess("BTC-USD", scout_ctx)   # 1st SCOUT again
        engine.assess("BTC-USD", scout_ctx)   # 2nd
        r = engine.assess("BTC-USD", scout_ctx)  # 3rd → switch
        self.assertEqual(r.personality, Personality.SCOUT,
                         "After reset + 3 more SCOUT, should switch.")

    def test_shield_bypasses_hysteresis(self):
        """SHIELD is an emergency state — no hysteresis, activates immediately."""
        from intelligence.personality import MarketPersonalityEngine, Personality
        engine = make_test_personality_engine()
        engine._current_personality["BTC-USD"] = Personality.FLOW

        ctx = make_test_context(calendar_regime="BLOCK")
        r = engine.assess("BTC-USD", ctx)
        self.assertEqual(r.personality, Personality.SHIELD,
                         "SHIELD must activate immediately, no hysteresis.")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — PERSONALITY CONTEXT CACHE
# ══════════════════════════════════════════════════════════════════════════════

class TestPersonalityContextCache(unittest.TestCase):

    def test_build_under_half_ms(self):
        from intelligence.personality_context import PersonalityContextCache
        cache = make_warmed_context_cache()

        times = []
        for _ in range(1000):
            t0 = time.perf_counter()
            cache.build("BTC-USD", 5.0, "long", "bullish")
            times.append((time.perf_counter() - t0) * 1000)

        avg_ms = sum(times) / len(times)
        p99_ms = sorted(times)[990]

        self.assertLess(avg_ms, 0.5,
                        f"Context build avg {avg_ms:.3f}ms. Must be under 0.5ms.")
        self.assertLess(p99_ms, 2.0,
                        f"Context build P99 {p99_ms:.3f}ms. Must be under 2.0ms.")

    def test_cache_updates_are_isolated(self):
        """Updating cascade must not corrupt regime."""
        from intelligence.personality_context import PersonalityContextCache
        cache = make_test_context_cache()
        cache.update_cascade("idle", "none", 0.0, 0.0, 0)
        cache.update_regime("risk_on", 0.8, "long", 1.2)

        ctx1 = cache.build("BTC-USD", 5.0, "long", "bullish")

        cache.update_cascade("momentum", "bearish", 3.0, 50000, 0)

        ctx2 = cache.build("BTC-USD", 5.0, "long", "bullish")

        self.assertEqual(ctx2.regime, "risk_on",
                         "Cascade update must not corrupt regime.")
        self.assertEqual(ctx2.cascade_phase, "momentum",
                         "Cascade update must be reflected in next build.")

    def test_rpc_health_degrades_and_recovers(self):
        from intelligence.personality_context import PersonalityContextCache
        cache = make_test_context_cache()

        cache.update_rpc_health(5, False)
        ctx = cache.build("BTC-USD", 5.0, "long", "bullish")
        self.assertLess(ctx.rpc_health_score, 0.7,
                        "5 RPC failures should degrade score below 0.70.")

        cache.update_rpc_health(0, True)
        ctx2 = cache.build("BTC-USD", 5.0, "long", "bullish")
        self.assertGreater(ctx2.rpc_health_score, ctx.rpc_health_score,
                           "Recovery must increase RPC health score.")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — BUDGET MANAGER
# ══════════════════════════════════════════════════════════════════════════════

class TestBudgetManager(unittest.TestCase):

    def test_initialise_scales_with_balance(self):
        from core.budget_manager import BudgetManager
        bm_1000 = BudgetManager(test_config(), 1000.0)
        bm_1000.initialise()
        bm_500 = BudgetManager(test_config(), 500.0)
        bm_500.initialise()

        perp_1000 = bm_1000.get_budget("perp", "flow")
        perp_500  = bm_500.get_budget("perp", "flow")

        self.assertAlmostEqual(perp_1000 / perp_500, 2.0, places=1,
                               msg="Budget must scale with balance. No hardcoded dollar amounts.")

    def test_no_hardcoded_dollar_amounts(self):
        """budget_manager.py must not contain specific dollar amounts."""
        import subprocess
        result = subprocess.run(
            ["grep", "-n", "135.60\\|56.50\\|33.90\\|= 226\\|= 258",
             "core/budget_manager.py"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 1,
                         f"Found hardcoded dollar amounts:\n{result.stdout}")

    def test_kelly_fraction_mathematics(self):
        from core.budget_manager import BudgetManager
        bm = BudgetManager(test_config(), 1000.0)
        # W=0.50, b=2.0: full Kelly = 0.50 - 0.50/2.0 = 0.25; half Kelly = 0.125
        result = bm.kelly_fraction(win_rate=0.50, avg_win_r=2.0)
        self.assertAlmostEqual(result, 0.125, places=3,
                               msg=f"Kelly(W=0.50, b=2.0) should be 0.125. Got {result}.")

        # W=0.40, b=2.0: full Kelly = 0.40 - 0.60/2.0 = 0.10; half Kelly = 0.05
        result2 = bm.kelly_fraction(win_rate=0.40, avg_win_r=2.0)
        self.assertAlmostEqual(result2, 0.05, places=3,
                               msg=f"Kelly(W=0.40, b=2.0) should be 0.05. Got {result2}.")

    def test_kelly_never_negative(self):
        from core.budget_manager import BudgetManager
        bm = BudgetManager(test_config(), 1000.0)
        result = bm.kelly_fraction(win_rate=0.20, avg_win_r=1.0)
        self.assertGreaterEqual(result, 0.01,
                                "Kelly fraction must never be negative. Minimum is 0.01.")

    def test_kelly_capped_at_fifteen_pct(self):
        from core.budget_manager import BudgetManager
        bm = BudgetManager(test_config(), 1000.0)
        result = bm.kelly_fraction(win_rate=0.90, avg_win_r=5.0)
        self.assertLessEqual(result, 0.15, "Kelly fraction capped at 0.15.")

    def test_trade_size_never_exceeds_15pct(self):
        from core.budget_manager import BudgetManager
        bm = BudgetManager(test_config(), 1000.0)
        bm.initialise()
        size = bm.get_trade_size("perp", "apex", ml_prob=0.90, balance=1000.0)
        self.assertLessEqual(size, 150.0,
                             f"Trade size {size} exceeds 15% of $1000.")

    def test_budget_floor_protection(self):
        from core.budget_manager import BudgetManager
        bm = BudgetManager(test_config(), 1000.0)
        bm.initialise()

        async def run():
            for _ in range(100):
                await bm.record_pnl("perp", "scout", -50.0, -1.0)
            budget = bm.get_budget("perp", "scout")
            self.assertGreaterEqual(budget, 5.0,
                                    "Budget must never go below $5 floor.")

        asyncio.run(run())

    def test_weights_sum_to_one(self):
        from core.budget_manager import AGENT_RATIOS
        total = sum(AGENT_RATIOS.values())
        self.assertAlmostEqual(total, 1.0, places=3,
                               msg=f"Agent weights sum to {total}. Must be exactly 1.0.")

    def test_can_bet_returns_combined(self):
        from core.budget_manager import BudgetManager
        bm = BudgetManager(test_config(), 1000.0)
        bm.initialise()
        can, combined = bm.can_bet("perp", "gold", "flow", "flow")
        if can:
            self.assertLessEqual(combined, 150.0,
                                 "Combined bet cannot exceed 15% of total $1000 balance.")

    def test_rebalance_max_step(self):
        from core.budget_manager import BudgetManager
        from intelligence.prediction_market import CalibrationResult
        bm = BudgetManager(test_config(), 1000.0)
        bm.initialise()
        initial = bm.get_budget("perp", "flow")

        async def run():
            bad_calibration = {
                "flow": CalibrationResult(
                    personality="flow",
                    n_trades=60,
                    calibration_error=0.40,
                    is_overconfident=True,
                    budget_multiplier=0.60,
                )
            }
            await bm.rebalance(bad_calibration)
            after = bm.get_budget("perp", "flow")
            if initial > 0:
                reduction = (initial - after) / initial
                self.assertLessEqual(reduction, 0.10,
                                     f"Rebalance reduced by {reduction:.1%}. Max step is 10%.")

        asyncio.run(run())


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — PREDICTION MARKET
# ══════════════════════════════════════════════════════════════════════════════

class TestPredictionMarket(unittest.TestCase):

    def test_store_is_memory_only(self):
        """add_pending must be synchronous — no await, no aiosqlite."""
        import inspect
        from intelligence.prediction_market import PredictionStore
        source = inspect.getsource(PredictionStore.add_pending)
        self.assertNotIn("aiosqlite", source,
                         "add_pending must not use aiosqlite. In-memory only.")
        self.assertNotIn("await", source,
                         "add_pending must be synchronous. No awaits on hot path.")

    def test_circular_buffer_max_500(self):
        from intelligence.prediction_market import PredictionStore

        store = PredictionStore()
        for i in range(600):
            store.add_pending(make_test_prediction(f"id_{i}"))

        asyncio.run(store._drain_once())

        total = len(list(store._records))
        self.assertLessEqual(total, 500,
                             f"Store has {total} records. Max is 500 — circular buffer.")

    def test_joint_probability_formula(self):
        """P_joint = P_A*P_B / (P_A*P_B + (1-P_A)*(1-P_B))"""
        from intelligence.prediction_market import CrossAgentBetEngine
        engine = CrossAgentBetEngine()

        P_A, P_B = 0.65, 0.65
        expected = (P_A * P_B) / (P_A * P_B + (1 - P_A) * (1 - P_B))

        result = engine._joint_probability(P_A, P_B)
        self.assertAlmostEqual(result, expected, places=3,
                               msg=f"Joint probability formula wrong. Expected {expected:.3f} got {result:.3f}.")

    def test_bet_threshold_070(self):
        from intelligence.prediction_market import CrossAgentBetEngine

        engine = CrossAgentBetEngine()
        pred_a = make_test_prediction("id_a", confidence=0.62,
                                      agent="perp", symbol="BTC-USD", direction="short")
        pred_b = make_test_prediction("id_b", confidence=0.62,
                                      agent="gold", symbol="BTC-USD", direction="short")

        bm = make_test_budget_manager()
        result = engine.check_bet(pred_b, [pred_a], bm)

        if result is not None:
            self.assertGreaterEqual(result.p_joint, 0.70,
                                    "Bet fired below 0.70 threshold.")

    def test_same_source_no_bet(self):
        from intelligence.prediction_market import CrossAgentBetEngine

        engine = CrossAgentBetEngine()
        pred_a = make_test_prediction("id_a", confidence=0.80, agent="perp",
                                      personality="flow", symbol="BTC-USD", direction="short")
        pred_b = make_test_prediction("id_b", confidence=0.80, agent="perp",
                                      personality="flow", symbol="BTC-USD", direction="short")

        bm = make_test_budget_manager()
        result = engine.check_bet(pred_b, [pred_a], bm)
        self.assertIsNone(result,
                          "Same agent + same personality must not produce a bet. Not independent.")

    def test_calibration_error_formula(self):
        """Perfect 70%-confident predictions, all correct → error ≈ 0.09."""
        from intelligence.prediction_market import PredictionStore

        store = PredictionStore()

        async def run():
            for i in range(10):
                p = make_test_prediction(f"id_{i}", confidence=0.70, personality="flow")
                store.add_pending(p)
            await store._drain_once()

            for r in list(store._records):
                store.resolve(r.symbol, "correct", 2.0)

            error = store.calibration_error("flow", n=10)
            self.assertAlmostEqual(error, 0.09, places=2,
                                   msg=f"Cal error {error:.3f}. Expected ~0.09 (70% predicted, 100% actual).")

        asyncio.run(run())

    def test_accuracy_today_calculation(self):
        from intelligence.prediction_market import PredictionStore

        store = PredictionStore()

        async def run():
            for i in range(10):
                store.add_pending(make_test_prediction(f"id_{i}", confidence=0.70, personality="flow"))
            await store._drain_once()

            records = list(store._records)
            for r in records[:7]:
                store.resolve(r.symbol, "correct", 2.0)
            for r in records[7:]:
                store.resolve(r.symbol, "incorrect", -1.0)

            acc = store.accuracy_today()
            self.assertAlmostEqual(acc, 0.70, places=1,
                                   msg=f"Accuracy {acc:.2f}. Expected 0.70 (7/10 correct).")

        asyncio.run(run())


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — TERRAIN AWARE COHERENCE
# ══════════════════════════════════════════════════════════════════════════════

class TestTerrainCoherence(unittest.TestCase):

    def test_crypto_tier4_hard_gate(self):
        """Crypto with zero Tier 4 (micro) must score 0."""
        from intelligence.coherence import score_coherence
        state = make_market_state_no_micro("BTC-USD")
        score, mult, reason = score_coherence(state, "long", signal_age_ms=0, symbol="BTC-USD")
        self.assertEqual(score, 0,
                         "Crypto with zero Tier 4 must score 0. Hard gate active for crypto.")
        self.assertIn("no_micro", str(reason),
                      "Reason must mention no_micro signal.")

    def test_commodity_no_tier4_hard_gate(self):
        """CL-USD with zero micro but strong macro/structure must score > 0."""
        from intelligence.coherence import score_coherence
        # Build a no-micro state then copy with strong macro/structure overrides.
        # MarketState is frozen Pydantic — use model_copy(update=...).
        state = make_market_state_no_micro("CL-USD").model_copy(update={
            "regime":      "risk_on",
            "market_type": "expansion",
            "macro_bias":  "bullish",
        })

        score, mult, reason = score_coherence(state, "long", signal_age_ms=0, symbol="CL-USD")
        self.assertGreater(score, 0,
                           "CL-USD with strong macro/regime/structure must score above 0. No hard gate.")

    def test_equity_tier6_double_weight(self):
        """Equity tier6_lead weight must exceed crypto tier6_lead weight."""
        from intelligence.coherence import get_tier_weights

        weights_crypto = get_tier_weights("BTC-USD")
        weights_equity = get_tier_weights("TSM-USD")

        self.assertGreater(
            weights_equity["tier6_lead"],
            weights_crypto["tier6_lead"],
            "Equity Tier 6 weight must exceed crypto. Earnings lag is primary for stocks."
        )
        self.assertAlmostEqual(weights_equity["tier6_lead"], 2.0, places=1,
                               msg="Equity Tier 6 weight should be 2.0x.")

    def test_commodity_tier5_zero_weight(self):
        """CL-USD has no perpetual funding — Tier 5 weight must be 0.0."""
        from intelligence.coherence import get_tier_weights
        weights = get_tier_weights("CL-USD")
        self.assertEqual(weights["tier5_funding"], 0.0,
                         "CL-USD has no funding rate perp. Tier 5 weight must be 0.0.")

    def test_equity_tier5_zero_weight(self):
        from intelligence.coherence import get_tier_weights
        weights = get_tier_weights("TSM-USD")
        self.assertEqual(weights["tier5_funding"], 0.0,
                         "TSM has no perpetual funding. Tier 5 weight must be 0.0.")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 8 — AGENT TERRAIN RULES
# ══════════════════════════════════════════════════════════════════════════════

class TestAgentTerrainRules(unittest.TestCase):

    def test_equity_no_apex_ever(self):
        from intelligence.personality import PERSONALITY_AVAILABILITY
        self.assertNotIn("APEX", PERSONALITY_AVAILABILITY["equity"],
                         "Equity must never have APEX. Stocks have no liquidation cascades.")

    def test_equity_no_aftermath_ever(self):
        from intelligence.personality import PERSONALITY_AVAILABILITY
        self.assertNotIn("AFTERMATH", PERSONALITY_AVAILABILITY["equity"],
                         "Equity must never have AFTERMATH. Flash crash != crypto cascade.")

    def test_commodity_no_apex(self):
        from intelligence.personality import PERSONALITY_AVAILABILITY
        self.assertNotIn("APEX", PERSONALITY_AVAILABILITY["commodity"],
                         "Commodities have no liquidation cascades. APEX not available.")

    def test_commodity_has_aftermath(self):
        from intelligence.personality import PERSONALITY_AVAILABILITY
        self.assertIn("AFTERMATH", PERSONALITY_AVAILABILITY["commodity"],
                      "XAUT and CL can have post-spike mean reversion. AFTERMATH allowed.")

    def test_crypto_has_all_six(self):
        from intelligence.personality import Personality, PERSONALITY_AVAILABILITY
        crypto = set(PERSONALITY_AVAILABILITY["crypto"])
        # SOVEREIGN is equity-only (requires MAG7 stake as structural anchor).
        # Crypto has all other personalities.
        all_non_sovereign = {p.name for p in Personality if p.name != "SOVEREIGN"}
        self.assertEqual(crypto, all_non_sovereign,
                         f"Crypto must have all non-SOVEREIGN personalities. "
                         f"Missing: {all_non_sovereign - crypto}")

    def test_equity_coil_outside_hours(self):
        """TSM-USD at 22:00 UTC Tuesday (after close) → SESSION_PERSONALITY_MAX == COIL."""
        from intelligence.market_hours import get_asset_session, SESSION_PERSONALITY_MAX
        tuesday_after_close = datetime(2026, 4, 14, 22, 0, tzinfo=timezone.utc)
        session = get_asset_session("TSM-USD", tuesday_after_close)
        max_pers = SESSION_PERSONALITY_MAX.get(session)
        self.assertEqual(max_pers, "COIL",
                         f"TSM at 22:00 UTC in session '{session}' must force COIL.")

    def test_crypto_always_open(self):
        from intelligence.market_hours import get_asset_session
        saturday = datetime(2026, 4, 11, 3, 0, tzinfo=timezone.utc)
        session = get_asset_session("BTC-USD", saturday)
        self.assertIn(session, ("always_open", "weekend"),
                      "BTC must be always_open or weekend including weekend hours.")

    def test_commodity_daily_break(self):
        """CL-USD at 22:30 UTC Tuesday → daily maintenance break."""
        from intelligence.market_hours import get_asset_session
        break_time = datetime(2026, 4, 14, 22, 30, tzinfo=timezone.utc)
        session = get_asset_session("CL-USD", break_time)
        self.assertIn(session, ("closed", "break"),
                      "CL-USD at 22:30 UTC must be in daily break period.")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 9 — RPC HEALTH AND FREEZE
# ══════════════════════════════════════════════════════════════════════════════

class TestRPCAndFreeze(unittest.TestCase):

    def test_rpc_degraded_blocks_apex(self):
        from intelligence.personality import MarketPersonalityEngine, Personality
        engine = make_test_personality_engine()
        ctx = make_test_context(
            cascade_phase="momentum",
            cascade_notional=100000,
            cascade_direction="bearish",
            direction="short",
            coherence=8.0,
            calendar_regime="CLEAR",
            rpc_health_score=0.40,
            atr_vs_baseline=1.0,
        )
        state = engine.assess("BTC-USD", ctx)
        self.assertNotEqual(state.personality, Personality.APEX,
                            "RPC degraded to 0.40 must block APEX. Cascade data unreliable.")

    def test_rpc_degraded_reduces_size(self):
        from intelligence.personality import MarketPersonalityEngine
        engine = make_test_personality_engine()

        ctx_healthy = make_test_context(
            cascade_phase="idle", coherence=5.0, direction="long",
            htf="bullish", regime="risk_on", rpc_health_score=1.0,
            atr_vs_baseline=1.0,
        )
        ctx_degraded = make_test_context(
            cascade_phase="idle", coherence=5.0, direction="long",
            htf="bullish", regime="risk_on", rpc_health_score=0.50,
            atr_vs_baseline=1.0,
        )

        state_healthy  = engine.assess("BTC-USD", ctx_healthy)
        state_degraded = engine.assess("ETH-USD", ctx_degraded)

        self.assertLess(state_degraded.size_multiplier, state_healthy.size_multiplier,
                        "RPC degraded must reduce size. Unreliable data = smaller position.")

    def test_freeze_forces_shield(self):
        from intelligence.personality import MarketPersonalityEngine, Personality
        engine = make_test_personality_engine()
        ctx = make_test_context(
            freeze_active=True,
            cascade_phase="momentum",
            coherence=8.0,
            calendar_regime="CLEAR",
            rpc_health_score=1.0,
            atr_vs_baseline=1.0,
        )
        state = engine.assess("BTC-USD", ctx)
        self.assertEqual(state.personality, Personality.SHIELD,
                         "Active freeze must force SHIELD.")

    def test_extended_freeze_shield_persists(self):
        """After freeze release, SHIELD persists (freeze_elapsed_s > 0)."""
        from intelligence.personality import MarketPersonalityEngine, Personality
        engine = make_test_personality_engine()
        ctx = make_test_context(
            freeze_active=False,
            freeze_elapsed_s=450,   # freeze was active; just released
            cascade_phase="idle",
            coherence=5.0,
            direction="long",
            htf="bullish",
            atr_vs_baseline=1.0,
        )
        state = engine.assess("BTC-USD", ctx)
        self.assertEqual(state.personality, Personality.SHIELD,
                         "Post-freeze grace period — SHIELD must persist until freeze_elapsed_s cleared.")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 10 — ML CLASSIFIER
# ══════════════════════════════════════════════════════════════════════════════

class TestMLClassifier(unittest.TestCase):

    def test_default_probability_before_training(self):
        from ml.classifier import ClassifierCache
        cache = ClassifierCache()
        prob = cache.get("BTC-USD")
        self.assertEqual(prob, 0.50,
                         "Before training, default P(win) = 0.50.")

    def test_minimum_samples_before_training(self):
        from ml.classifier import TradeClassifier
        classifier = TradeClassifier(min_samples=50)
        journal = make_journal_with_trades(wins=10, losses=10)
        trained = classifier.train(journal)
        self.assertFalse(trained,
                         "Must not train with < 50 samples. Insufficient data overfits.")

    def test_feature_vector_correct_length(self):
        from ml.classifier import TradeClassifier
        classifier = TradeClassifier()
        candidate = make_test_candidate("BTC-USD")
        personality = make_test_personality_state()
        context = make_test_context_object()

        features = classifier._candidate_to_features(candidate, personality, context)
        self.assertEqual(len(features), 20,
                         f"Feature vector has {len(features)} elements. Must be exactly 20.")

    def test_time_encoding_cyclical(self):
        """Hour 0 and 23 are adjacent; cos encoding must reflect this."""
        # Cosine encodes circularity: cos(0)=1, cos(23*2π/24)≈0.966 (close),
        # cos(12*2π/24)=cos(π)=-1 (opposite). Sin alone doesn't show this
        # because sin(0)=sin(π)=0, making hrs 0 and 12 appear identical.
        hour_0_cos  = math.cos(2 * math.pi * 0  / 24)
        hour_23_cos = math.cos(2 * math.pi * 23 / 24)
        hour_12_cos = math.cos(2 * math.pi * 12 / 24)

        diff_0_23 = abs(hour_0_cos - hour_23_cos)
        diff_0_12 = abs(hour_0_cos - hour_12_cos)

        self.assertLess(diff_0_23, diff_0_12,
                        "Cos encoding: hour 0 and 23 must be closer than hour 0 and 12.")

    def test_no_nan_in_features(self):
        from ml.classifier import TradeClassifier
        classifier = TradeClassifier()
        candidate = make_test_candidate("BTC-USD")
        personality = make_test_personality_state()
        context = make_test_context_object()

        features = classifier._candidate_to_features(candidate, personality, context)

        for i, f in enumerate(features):
            self.assertFalse(math.isnan(f) or math.isinf(f),
                             f"Feature[{i}] is {f}. NaN and Inf corrupt ML training.")

    def test_ml_size_reduction_tiers(self):
        from ml.classifier import ml_size_multiplier
        self.assertEqual(ml_size_multiplier(0.60), 1.0)
        self.assertEqual(ml_size_multiplier(0.52), 0.75)
        self.assertEqual(ml_size_multiplier(0.47), 0.50)
        self.assertEqual(ml_size_multiplier(0.40), 0.0)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 11 — LATENCY BUDGET
# ══════════════════════════════════════════════════════════════════════════════

class TestLatencyBudget(unittest.TestCase):

    def test_full_personality_pipeline_under_2ms(self):
        """context.build + assess must average < 1ms, P95 < 2ms."""
        from intelligence.personality_context import PersonalityContextCache
        from intelligence.personality import MarketPersonalityEngine

        cache  = make_warmed_context_cache()
        engine = make_test_personality_engine()

        times = []
        for _ in range(500):
            t0 = time.perf_counter()
            ctx   = cache.build("BTC-USD", 5.5, "long", "bullish")
            state = engine.assess("BTC-USD", ctx)
            times.append((time.perf_counter() - t0) * 1000)

        avg = sum(times) / len(times)
        p95 = sorted(times)[474]
        p99 = sorted(times)[494]

        self.assertLess(avg, 1.0,   f"Avg latency {avg:.3f}ms. Must be <1ms.")
        self.assertLess(p95, 2.0,   f"P95 latency {p95:.3f}ms. Must be <2ms.")
        self.assertLess(p99, 5.0,   f"P99 latency {p99:.3f}ms. Must be <5ms.")

    def test_prediction_store_add_under_1ms(self):
        from intelligence.prediction_market import PredictionStore
        store = PredictionStore()

        times = []
        for i in range(1000):
            pred = make_test_prediction(f"id_{i}")
            t0 = time.perf_counter()
            store.add_pending(pred)
            times.append((time.perf_counter() - t0) * 1000)

        avg = sum(times) / len(times)
        self.assertLess(avg, 0.1,
                        f"Prediction add avg {avg:.3f}ms. Must be under 0.1ms.")

    def test_budget_get_under_half_ms(self):
        from core.budget_manager import BudgetManager
        bm = BudgetManager(test_config(), 1000.0)
        bm.initialise()

        times = []
        for _ in range(1000):
            t0 = time.perf_counter()
            bm.get_budget("perp", "flow")
            times.append((time.perf_counter() - t0) * 1000)

        avg = sum(times) / len(times)
        self.assertLess(avg, 0.1,
                        f"Budget get avg {avg:.3f}ms. Dict lookup must be under 0.1ms.")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 12 — INTEGRATION
# ══════════════════════════════════════════════════════════════════════════════

class TestIntegration(unittest.TestCase):

    def test_full_signal_to_decision_flow(self):
        """Trace a complete signal through context → personality → prediction → size."""
        async def run():
            from intelligence.personality_context import PersonalityContextCache
            from intelligence.personality import MarketPersonalityEngine, Personality
            from intelligence.prediction_market import PredictionStore, CrossAgentBetEngine, PredictionRecord
            from core.budget_manager import BudgetManager
            import uuid

            cache  = make_warmed_context_cache()
            engine = make_test_personality_engine()
            store  = PredictionStore()
            bm     = BudgetManager(test_config(), 1000.0)
            bm.initialise()

            symbol    = "BTC-USD"
            coherence = 5.5
            direction = "long"
            htf       = "bullish"

            # Build context + assess (hot path)
            t0  = time.perf_counter()
            ctx   = cache.build(symbol, coherence, direction, htf)
            state = engine.assess(symbol, ctx)
            hot_path_ms = (time.perf_counter() - t0) * 1000

            self.assertLess(hot_path_ms, 2.0,
                            f"Hot path took {hot_path_ms:.3f}ms. Must be under 2ms.")

            if state.size_multiplier == 0.0:
                return   # COIL or SHIELD — valid outcome

            ml_prob = 0.58
            size    = bm.get_trade_size("perp", state.personality.value, ml_prob, 1000.0)

            self.assertGreater(size, 0, "Trade size must be positive.")
            self.assertLessEqual(size, 150.0, "Trade size must not exceed 15% of $1000.")

            pred = PredictionRecord(
                id=str(uuid.uuid4()), agent="perp",
                personality=state.personality.value, symbol=symbol,
                direction=direction, confidence=state.confidence,
                ml_probability=ml_prob, coherence=coherence,
                entry_price=75000.0, predicted_exit=76500.0,
                timestamp_ms=int(time.time() * 1000),
            )
            store.add_pending(pred)

            self.assertIn(state.personality,
                          [Personality.APEX, Personality.AFTERMATH,
                           Personality.FLOW, Personality.SCOUT],
                          f"Unexpected personality {state.personality} for tradeable signal.")

        asyncio.run(run())

    def test_losing_streak_reduces_all_sizes(self):
        """After 10 consecutive losses, trade size must not increase."""
        async def run():
            from core.budget_manager import BudgetManager
            bm = BudgetManager(test_config(), 1000.0)
            bm.initialise()

            initial_size = bm.get_trade_size("perp", "flow", 0.55, 1000.0)
            for _ in range(10):
                await bm.record_pnl("perp", "flow", -15.0, -1.0)
            after_size = bm.get_trade_size("perp", "flow", 0.55, 1000.0)

            self.assertLessEqual(after_size, initial_size,
                                 "After 10 losses, size must not increase.")

        asyncio.run(run())

    def test_winning_streak_stays_bounded(self):
        """After 20 wins, size must still respect the 15% Kelly cap."""
        async def run():
            from core.budget_manager import BudgetManager
            bm = BudgetManager(test_config(), 1000.0)
            bm.initialise()

            for _ in range(20):
                await bm.record_pnl("perp", "apex", 30.0, 2.0)
            size = bm.get_trade_size("perp", "apex", 0.70, 1000.0)

            self.assertLessEqual(size, 150.0,
                                 f"After 20 wins size is {size}. Kelly cap must prevent overbetting.")

        asyncio.run(run())


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
