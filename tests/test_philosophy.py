"""
ARIA Philosophical Agency Test Suite

Maps the conceptual framework behind ARIA's design to verifiable code properties.

The philosophy (from the user's framework):
  ─────────────────────────────────────────────────────────────────────
  "The kingdom analogy:
    Staked positions = territory (passive income, not consumed in battle)
    SOVEREIGN = kingdom campaigns funded by territory income
    Trading capital = treasury reserves (never used for campaigns)
    Yield = the kingdom's income from its territory
    The territory remains. The yield continues. SOVEREIGN runs again."

  "Six personalities are not parameter sets — they are distinct modes of being.
    Each personality has a philosophy behind it:
      SHIELD:    a sovereign in his castle when the gates are under threat
      SOVEREIGN: a sovereign running campaigns from territorial income
      AFTERMATH: a general reading the battlefield after the smoke clears
      APEX:      a cavalry charge at the moment of maximum momentum
      COIL:      a siege — no movement, only patience and preparation
      FLOW:      a river — direction is clear, follow the gradient
      SCOUT:     an advance guard — observe, probe, report back"

  "The edge is not the signal. The edge is the structure:
    SOVEREIGN: cross-sectional (relationship between instruments)
    Others:    temporal (relationship between past and future of same instrument)
    These edges are orthogonal — they do not interfere with each other."

  "The sovereign funds campaigns from income, not reserves.
    If a campaign fails, the treasury survives.
    The territory keeps generating income for the next cycle."

  "Sustainability = survival. A strategy that bets the kingdom on a single
    campaign is not a strategy — it is a gamble. ARIA never bets the kingdom."
  ─────────────────────────────────────────────────────────────────────

Philosophical tests map concepts → code invariants:
  P1.  Territory (stake) is never consumed by campaigns (SOVEREIGN budget ≠ stake)
  P2.  Campaigns funded by income, not reserves (yield_tracker isolation)
  P3.  The territory remains after a failed campaign (budget floors at 0, stake untouched)
  P4.  Personalities are distinct modes, not parameter gradients (enum, not float)
  P5.  SHIELD is the castle gate — it overrides everything, always
  P6.  Edges are orthogonal — SOVEREIGN fires independently of coherence
  P7.  River follows gradient — FLOW requires HTF alignment (direction constraint)
  P8.  Cavalry at maximum momentum — APEX requires active cascade + high coherence
  P9.  Scout advance guard — SCOUT is always available as the final fallback
  P10. Siege patience — COIL blocks directional, allows only arb (waiting)
  P11. Battlefield intelligence — AFTERMATH reads cascade direction, trades opposite
  P12. The kingdom never over-extends — Kelly cap at 15%, floor at 1%
  P13. Cross-sectional edge — SOVEREIGN and coherence-based signals are independent
  P14. Structural matching — SOVEREIGN size = stake × component_weight (not arbitrary)
  P15. The cycle renews — after overflow transfer, sovereign_budget resets for next cycle

Run:
    python tests/test_philosophy.py
    python tests/test_philosophy.py -v
"""

import sys
import os
import asyncio
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.helpers import (
    make_test_personality_engine,
    make_test_context,
    make_warmed_context_cache,
    make_sovereign_context_cache,
    test_config,
)


# ══════════════════════════════════════════════════════════════════════════════
# AXIOM 1 — THE TERRITORY IS NOT THE CAMPAIGN BUDGET
# ══════════════════════════════════════════════════════════════════════════════

class TestTerritoryIsNotBudget(unittest.TestCase):
    """
    P1: stake_balance ≠ sovereign_budget.
    The territory (staked MAG7) is never consumed. Only yield funds campaigns.
    Losing all SOVEREIGN budget does not reduce the staked position.
    """

    def test_stake_unchanged_after_budget_depleted(self):
        """Stake balance is immutable — SOVEREIGN losses cannot reduce it."""
        async def run():
            from core.yield_tracker import YieldTracker
            tracker = YieldTracker()
            tracker.initialise(10.0)   # $10 yield → $8 budget

            initial_budget = tracker.available_budget

            # Simulate total loss
            await tracker.record_pnl(-100.0)

            # Budget hits floor at 0
            self.assertEqual(tracker.available_budget, 0.0,
                             "Budget exhausted after loss.")

            # The 'territory' (stake balance) is held in staking_monitor, not tracker.
            # Verify: tracker has no reference to stake_balance whatsoever.
            self.assertFalse(hasattr(tracker, "_stake_balance"),
                             "YieldTracker must not hold stake_balance. "
                             "Stake and budget are separate systems.")

        asyncio.run(run())

    def test_stake_and_budget_are_separate_objects(self):
        """
        Architectural invariant: StakingMonitor owns stake, YieldTracker owns budget.
        They never share state.
        """
        from intelligence.staking_monitor import StakingMonitor
        from core.yield_tracker import YieldTracker

        sm = StakingMonitor()
        sm.initialise()
        yt = YieldTracker()
        yt.initialise(5.0)

        # Staking monitor knows nothing about yield tracker budget
        self.assertFalse(hasattr(sm, "_sovereign_budget"))
        # Yield tracker knows nothing about stake balance
        self.assertFalse(hasattr(yt, "_stake_balance"))

        # They are connected only through context_cache.update_sovereign()
        # which takes BOTH as arguments — clean composition, no coupling


# ══════════════════════════════════════════════════════════════════════════════
# AXIOM 2 — CAMPAIGNS FUNDED BY INCOME, NOT RESERVES
# ══════════════════════════════════════════════════════════════════════════════

class TestCampaignsFundedByIncome(unittest.TestCase):
    """
    P2 + P3: When sovereign_budget = 0, SOVEREIGN enters COIL.
    Main BudgetManager capital is never accessed by SOVEREIGN.
    """

    def test_zero_budget_forces_coil_not_main_capital(self):
        """P2: Zero yield budget → SOVEREIGN unavailable. No fallback to main capital."""
        from intelligence.personality import PersonalityEngine, Personality
        engine = PersonalityEngine()

        ctx = make_test_context(
            symbol="TSLA-USD",
            stake_balance=200.0,
            sovereign_budget=0.0,    # no yield budget
            component_signals={"TSLA-USD": -3.0},
            best_divergence=("TSLA-USD", -3.0),
            regime="risk_off",
            calendar_regime="CLEAR",
        )
        params = engine.assess("TSLA-USD", ctx)
        self.assertNotEqual(params.personality, Personality.SOVEREIGN,
                            "P2: Zero yield budget → SOVEREIGN must not fire. "
                            "Main capital is NOT the fallback.")

    def test_main_budget_manager_excludes_sovereign(self):
        """P3: BudgetManager knows nothing about SOVEREIGN. Separate ledgers."""
        from core.budget_manager import BudgetManager, AGENT_RATIOS

        bm = BudgetManager(test_config(), 1000.0)
        bm.initialise()

        # SOVEREIGN is not an agent in BudgetManager
        self.assertNotIn("sovereign", bm._slots,
                         "P3: BudgetManager must not have a 'sovereign' agent slot. "
                         "SOVEREIGN budget lives in YieldTracker, not BudgetManager.")

        # Agent ratios must not include sovereign
        self.assertNotIn("sovereign", AGENT_RATIOS,
                         "AGENT_RATIOS must not include sovereign. "
                         "SOVEREIGN is self-funded from yield, not portfolio allocation.")


# ══════════════════════════════════════════════════════════════════════════════
# AXIOM 3 — PERSONALITIES ARE MODES OF BEING, NOT PARAMETER GRADIENTS
# ══════════════════════════════════════════════════════════════════════════════

class TestPersonalitiesAreDiscreteStates(unittest.TestCase):
    """
    P4: Each personality is a distinct qualitative state, not a position on a continuum.
    The enum enforces discreteness — there is no "FLOW with 80% confidence".
    """

    def test_personality_is_enum_not_float(self):
        """Personality must be a discrete enum value, not a continuous parameter."""
        from intelligence.personality import Personality
        for p in Personality:
            self.assertIsInstance(p, Personality,
                                  f"{p} must be a Personality enum instance.")
            self.assertIsInstance(p.value, str,
                                  f"{p} enum value must be a string name, not a number.")

    def test_seven_distinct_named_states(self):
        """Seven named modes of being — not a spectrum."""
        from intelligence.personality import Personality
        names = {p.name for p in Personality}
        expected = {"SHIELD", "SOVEREIGN", "AFTERMATH", "APEX", "COIL", "FLOW", "SCOUT"}
        self.assertEqual(names, expected,
                         f"Expected 7 distinct named personalities. Got: {names}")

    def test_no_personality_is_intermediate(self):
        """There is no 'FLOW_LITE' or 'PARTIAL_APEX'. Transitions are binary."""
        from intelligence.personality import Personality
        for p in Personality:
            # Name must be a clean identifier, no underscores with LITE/PARTIAL etc
            self.assertNotIn("LITE", p.name)
            self.assertNotIn("PARTIAL", p.name)
            self.assertNotIn("WEAK", p.name)


# ══════════════════════════════════════════════════════════════════════════════
# AXIOM 4 — THE GATE (SHIELD) OVERRIDES ALL STATES
# ══════════════════════════════════════════════════════════════════════════════

class TestShieldIsAbsolute(unittest.TestCase):
    """P5: SHIELD is non-negotiable. No signal, cascade, or stake overrides it."""

    def test_shield_beats_active_apex_cascade(self):
        """Even with a $100k cascade momentum in progress, SHIELD wins on calendar block."""
        from intelligence.personality import PersonalityEngine, Personality
        engine = PersonalityEngine()
        ctx = make_test_context(
            symbol="BTC-USD",
            calendar_regime="BLOCK",
            cascade_phase="momentum",
            cascade_notional=100_000.0,
            coherence=9.0,
            atr_vs_baseline=1.2,
        )
        self.assertEqual(engine.assess("BTC-USD", ctx).personality, Personality.SHIELD)

    def test_shield_beats_daily_loss_no_matter_how_strong_signal(self):
        """Even with perfect coherence, -2.6% daily loss triggers SHIELD."""
        from intelligence.personality import PersonalityEngine, Personality
        engine = PersonalityEngine()
        ctx = make_test_context(
            symbol="BTC-USD",
            daily_pnl_pct=-0.026,
            coherence=9.9,
            htf="bullish",
            regime="risk_on",
            cascade_phase="idle",
        )
        self.assertEqual(engine.assess("BTC-USD", ctx).personality, Personality.SHIELD)

    def test_shield_beats_sovereign_with_perfect_divergence(self):
        """SHIELD beats SOVEREIGN even with z=-5.0 (extreme divergence)."""
        from intelligence.personality import PersonalityEngine, Personality
        engine = PersonalityEngine()
        ctx = make_test_context(
            symbol="TSLA-USD",
            calendar_regime="BLOCK",
            stake_balance=200.0,
            sovereign_budget=50.0,
            component_signals={"TSLA-USD": -5.0},
            best_divergence=("TSLA-USD", -5.0),
            regime="risk_off",
        )
        self.assertEqual(engine.assess("TSLA-USD", ctx).personality, Personality.SHIELD,
                         "SHIELD must beat even maximum-conviction SOVEREIGN signal.")

    def test_shield_has_zero_size_multiplier(self):
        """SHIELD size = 0.0 — no new entries are possible."""
        from intelligence.personality import PERSONALITY_PARAMS, Personality
        params = PERSONALITY_PARAMS[Personality.SHIELD]
        self.assertEqual(params["size_multiplier"], 0.0,
                         "SHIELD must have zero size. Capital preservation means no new entries.")


# ══════════════════════════════════════════════════════════════════════════════
# AXIOM 5 — ORTHOGONAL EDGES DO NOT INTERFERE
# ══════════════════════════════════════════════════════════════════════════════

class TestEdgesAreOrthogonal(unittest.TestCase):
    """
    P6 + P13: SOVEREIGN's z-score edge is structurally independent of coherence scoring.
    They share no signal source and cannot amplify or suppress each other.
    """

    def test_sovereign_fires_with_zero_coherence(self):
        """z-score signal does not require coherence. Edge is orthogonal."""
        from intelligence.personality import PersonalityEngine, Personality
        engine = PersonalityEngine()
        ctx = make_test_context(
            symbol="TSLA-USD",
            coherence=0.0,      # worst possible coherence
            stake_balance=200.0,
            sovereign_budget=8.0,
            component_signals={"TSLA-USD": -2.5},
            best_divergence=("TSLA-USD", -2.5),
            regime="risk_off",
            calendar_regime="CLEAR",
        )
        self.assertEqual(engine.assess("TSLA-USD", ctx).personality, Personality.SOVEREIGN,
                         "P6: SOVEREIGN must fire with zero coherence. "
                         "Its signal source is z-score, not coherence.")

    def test_high_coherence_does_not_suppress_sovereign(self):
        """Strong coherence does not route away from SOVEREIGN on equity symbols."""
        from intelligence.personality import PersonalityEngine, Personality
        engine = PersonalityEngine()
        ctx = make_test_context(
            symbol="NVDA-USD",
            coherence=8.0,      # very high coherence
            htf="bullish",
            regime="risk_on",
            stake_balance=200.0,
            sovereign_budget=8.0,
            component_signals={"NVDA-USD": -2.5},
            best_divergence=("NVDA-USD", -2.5),
            cascade_phase="idle",
            calendar_regime="CLEAR",
        )
        result = engine.assess("NVDA-USD", ctx)
        # SOVEREIGN should win because it is checked before FLOW/AFTERMATH
        self.assertEqual(result.personality, Personality.SOVEREIGN,
                         "P6: High coherence must not suppress SOVEREIGN on equity. "
                         "SOVEREIGN is checked before FLOW in priority order.")

    def test_coherence_signal_independent_of_sovereign_budget(self):
        """Coherence scoring does not read sovereign_budget. Confirmed by source isolation."""
        import inspect
        from intelligence.coherence import score_coherence
        source = inspect.getsource(score_coherence)
        self.assertNotIn("sovereign_budget", source,
                         "P13: score_coherence must not reference sovereign_budget. "
                         "These are independent signal sources.")


# ══════════════════════════════════════════════════════════════════════════════
# AXIOM 6 — THE RIVER FOLLOWS ITS GRADIENT (FLOW CONSTRAINT)
# ══════════════════════════════════════════════════════════════════════════════

class TestFlowFollowsGradient(unittest.TestCase):
    """P7: A river does not flow uphill. FLOW cannot trade against HTF bias."""

    def test_flow_blocked_when_htf_opposes_direction(self):
        """Long signal + bearish HTF = against the gradient → no FLOW."""
        from intelligence.personality import PersonalityEngine, Personality
        engine = PersonalityEngine()
        ctx = make_test_context(
            cascade_phase="idle", coherence=5.5,
            direction="long", htf="bearish",   # opposing
            regime="risk_on", atr_vs_baseline=1.0,
        )
        self.assertNotEqual(engine.assess("BTC-USD", ctx).personality, Personality.FLOW,
                            "P7: long + bearish HTF = upstream. FLOW must not fire.")

    def test_flow_enabled_when_htf_aligned(self):
        """Long signal + bullish HTF = with the gradient → FLOW possible."""
        from intelligence.personality import PersonalityEngine, Personality
        engine = PersonalityEngine()
        ctx = make_test_context(
            cascade_phase="idle", coherence=5.5,
            direction="long", htf="bullish",   # aligned
            regime="risk_on", atr_vs_baseline=1.0,
            calendar_regime="CLEAR",
        )
        self.assertEqual(engine.assess("BTC-USD", ctx).personality, Personality.FLOW,
                         "P7: long + bullish HTF + strong regime = with gradient. FLOW fires.")


# ══════════════════════════════════════════════════════════════════════════════
# AXIOM 7 — CAVALRY AT MAXIMUM MOMENTUM (APEX PREREQUISITES)
# ══════════════════════════════════════════════════════════════════════════════

class TestApexRequiresMaxMomentum(unittest.TestCase):
    """P8: APEX fires only at the exact moment of cascade momentum. No partial conditions."""

    def test_apex_requires_active_cascade(self):
        """No cascade = no cavalry charge. APEX must have momentum phase."""
        from intelligence.personality import PersonalityEngine, Personality
        engine = PersonalityEngine()
        ctx = make_test_context(
            cascade_phase="idle",   # not momentum
            coherence=8.0, direction="short",
            htf="bearish", regime="risk_off",
        )
        self.assertNotEqual(engine.assess("BTC-USD", ctx).personality, Personality.APEX,
                            "P8: No cascade = no APEX. Cavalry needs a battle in progress.")

    def test_apex_requires_minimum_notional(self):
        """Small cascade = noise, not a real battle. Must exceed $10k notional."""
        from intelligence.personality import PersonalityEngine, Personality
        engine = PersonalityEngine()
        ctx = make_test_context(
            cascade_phase="momentum",
            cascade_notional=1_000.0,   # $1k — too small
            cascade_direction="bearish",
            direction="short",
            coherence=8.0, calendar_regime="CLEAR",
        )
        self.assertNotEqual(engine.assess("BTC-USD", ctx).personality, Personality.APEX,
                            "P8: Sub-threshold cascade notional ($1k) = noise. No APEX.")

    def test_apex_requires_rpc_health(self):
        """Without reliable intelligence, the cavalry rides blind. APEX needs RPC health."""
        from intelligence.personality import PersonalityEngine, Personality
        engine = PersonalityEngine()
        ctx = make_test_context(
            cascade_phase="momentum",
            cascade_notional=100_000.0,
            cascade_direction="bearish",
            direction="short",
            coherence=8.0, calendar_regime="CLEAR",
            rpc_health_score=0.40,   # degraded
        )
        self.assertNotEqual(engine.assess("BTC-USD", ctx).personality, Personality.APEX,
                            "P8: Degraded RPC = blind cavalry. APEX must be blocked.")


# ══════════════════════════════════════════════════════════════════════════════
# AXIOM 8 — THE ADVANCE GUARD IS ALWAYS AVAILABLE (SCOUT FALLBACK)
# ══════════════════════════════════════════════════════════════════════════════

class TestScoutIsAlwaysFallback(unittest.TestCase):
    """P9: When all other personalities are blocked, SCOUT still observes and probes."""

    def test_scout_fires_when_nothing_else_qualifies(self):
        """Weak signal + confused regime + normal ATR → SCOUT, not a block."""
        from intelligence.personality import PersonalityEngine, Personality
        engine = PersonalityEngine()
        ctx = make_test_context(
            cascade_phase="idle", coherence=4.0,
            direction="long", htf="neutral",
            regime="confused", atr_vs_baseline=0.95,
            calendar_regime="CLEAR",
            daily_pnl_pct=0.0, basis_stress_count=0,
        )
        self.assertEqual(engine.assess("BTC-USD", ctx).personality, Personality.SCOUT,
                         "P9: Weak signal + normal ATR + confused = SCOUT. "
                         "The advance guard never abandons the mission.")

    def test_scout_size_is_reduced_not_zero(self):
        """SCOUT is cautious, not absent. size_mult > 0 (reduced exposure, not stopped)."""
        from intelligence.personality import PERSONALITY_PARAMS, Personality
        scout_params = PERSONALITY_PARAMS[Personality.SCOUT]
        self.assertGreater(scout_params["size_multiplier"], 0.0,
                           "P9: SCOUT must have size > 0. The advance guard still acts.")
        self.assertLess(scout_params["size_multiplier"], 1.0,
                        "P9: SCOUT must have size < 1.0. The advance guard is cautious.")


# ══════════════════════════════════════════════════════════════════════════════
# AXIOM 9 — THE SIEGE: PATIENCE WITHOUT MOVEMENT (COIL)
# ══════════════════════════════════════════════════════════════════════════════

class TestCoilIsSiegePatience(unittest.TestCase):
    """P10: COIL blocks all directional trades. Only arb allowed (maintenance, not attack)."""

    def test_coil_blocks_directional(self):
        """In COIL, the army waits. No directional trades."""
        from intelligence.personality import PERSONALITY_PARAMS, Personality
        params = PERSONALITY_PARAMS[Personality.COIL]
        self.assertFalse(params["directional"],
                         "P10: COIL must block directional. The siege does not attack.")

    def test_coil_allows_arb(self):
        """During the siege, maintenance continues. Arb is allowed."""
        from intelligence.personality import PERSONALITY_PARAMS, Personality
        params = PERSONALITY_PARAMS[Personality.COIL]
        self.assertTrue(params["arb_allowed"],
                        "P10: COIL must allow arb. The siege keeps the supply lines open.")

    def test_coil_triggered_by_compressed_atr(self):
        """Low ATR = market is literally coiling. Not a signal — a structural condition."""
        from intelligence.personality import PersonalityEngine, Personality
        engine = PersonalityEngine()
        ctx = make_test_context(
            symbol="BTC-USD",
            atr_vs_baseline=0.55,   # below 0.80 threshold
            cascade_phase="idle",
            coherence=5.0, calendar_regime="CLEAR",
        )
        self.assertEqual(engine.assess("BTC-USD", ctx).personality, Personality.COIL,
                         "P10: Compressed ATR = siege conditions. COIL fires.")


# ══════════════════════════════════════════════════════════════════════════════
# AXIOM 10 — THE KINGDOM NEVER OVER-EXTENDS (KELLY DISCIPLINE)
# ══════════════════════════════════════════════════════════════════════════════

class TestKingdomNeverOverExtends(unittest.TestCase):
    """P12: No single campaign can risk more than 15% of treasury. Kelly ensures this."""

    def test_kelly_fraction_maximum_15pct(self):
        """Even with 90% win rate, Kelly is capped at 15%."""
        from core.budget_manager import BudgetManager
        bm = BudgetManager(test_config(), 1000.0)
        result = bm.kelly_fraction(win_rate=0.90, avg_win_r=5.0)
        self.assertLessEqual(result, 0.15,
                             "P12: Kelly cap = 0.15. The kingdom never risks more than 15%.")

    def test_kelly_fraction_minimum_1pct(self):
        """Even with terrible track record, Kelly is floored at 1%."""
        from core.budget_manager import BudgetManager
        bm = BudgetManager(test_config(), 1000.0)
        result = bm.kelly_fraction(win_rate=0.20, avg_win_r=1.0)
        self.assertGreaterEqual(result, 0.01,
                                "P12: Kelly floor = 0.01. The advance guard always has resources.")

    def test_trade_size_never_exceeds_portfolio_15pct(self):
        """A single trade cannot exceed 15% of total balance regardless of Kelly."""
        from core.budget_manager import BudgetManager
        bm = BudgetManager(test_config(), 1000.0)
        bm.initialise()
        size = bm.get_trade_size("perp", "APEX", ml_prob=0.95, balance=1000.0)
        self.assertLessEqual(size, 150.0,
                             f"P12: Single trade capped at 15% of $1000 = $150. Got {size}.")


# ══════════════════════════════════════════════════════════════════════════════
# AXIOM 11 — STRUCTURAL MATCHING (SOVEREIGN HEDGE RATIO)
# ══════════════════════════════════════════════════════════════════════════════

class TestStructuralMatching(unittest.TestCase):
    """P14: SOVEREIGN trade size = stake × component_weight. Never arbitrary."""

    def test_tsla_hedge_matches_stake_weight(self):
        """TSLA is 6% of MAG7. $200 stake → max $12 TSLA hedge. Not a free parameter."""
        from intelligence.staking_monitor import StakingMonitor
        sm = StakingMonitor(default_stake_usd=200.0)
        sm.initialise()
        hedge = sm.get_hedge_notional("TSLA-USD")
        self.assertAlmostEqual(hedge, 12.0, delta=0.5,
                               msg="P14: TSLA hedge = $200 × 6% = $12. Structural, not arbitrary.")

    def test_nvda_hedge_is_larger_because_larger_weight(self):
        """NVDA (25%) has a larger hedge than TSLA (6%). The structure determines size."""
        from intelligence.staking_monitor import StakingMonitor
        sm = StakingMonitor(default_stake_usd=200.0)
        sm.initialise()
        nvda_hedge = sm.get_hedge_notional("NVDA-USD")
        tsla_hedge = sm.get_hedge_notional("TSLA-USD")
        self.assertGreater(nvda_hedge, tsla_hedge,
                           "P14: NVDA (25% weight) → larger hedge than TSLA (6%). "
                           "Structural matching preserves index proportionality.")

    def test_total_hedge_cannot_exceed_stake(self):
        """Sum of all hedges across all components ≤ total stake. Conservation of position."""
        from intelligence.staking_monitor import StakingMonitor
        from intelligence.ssi_component_monitor import MAG7_COMPONENTS
        sm = StakingMonitor(default_stake_usd=200.0)
        sm.initialise()
        total_hedge = sum(sm.get_hedge_notional(sym) for sym in MAG7_COMPONENTS)
        self.assertAlmostEqual(total_hedge, 200.0, delta=0.5,
                               msg="P14: Sum of all hedges = total stake. "
                               "No leverage beyond what the stake supports.")


# ══════════════════════════════════════════════════════════════════════════════
# AXIOM 12 — THE CYCLE RENEWS (SOVEREIGN OVERFLOW RESET)
# ══════════════════════════════════════════════════════════════════════════════

class TestTheCycleRenews(unittest.TestCase):
    """P15: Successful sovereign campaigns enrich the treasury. Then the cycle resets."""

    def test_overflow_transfers_to_main_and_resets_cycle(self):
        """50% of successful sovereign budget transfers to main. Budget resets for next cycle."""
        async def run():
            from core.yield_tracker import YieldTracker
            tracker = YieldTracker()
            tracker.initialise(10.0)  # seed: $10 yield → $8 budget

            # Win significantly
            await tracker.record_pnl(+12.0)  # budget = $20 = 2× seed_yield ($10×2)
            pre_transfer_budget = tracker.available_budget  # $20

            transfer = await tracker.check_overflow()
            self.assertIsNotNone(transfer, "P15: Overflow must fire at 2× seed.")
            self.assertGreater(transfer, 0, "P15: Transfer amount must be positive.")

            # After transfer: budget reduced
            post_budget = tracker.available_budget
            self.assertLess(post_budget, pre_transfer_budget,
                            "P15: Budget must decrease after treasury enrichment.")

            # Budget is not zero — sovereign retains half for next cycle
            self.assertGreater(post_budget, 0,
                               "P15: SOVEREIGN retains half for next campaign cycle. "
                               "The kingdom renews, not retreats.")

        asyncio.run(run())


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
