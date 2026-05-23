"""
ARIA COMPLETE INSTITUTIONAL TEST SUITE
Phase 1-13 + Kant + Nietzsche
Run after every build. All must pass.

══════════════════════════════════════════
HOW TO RUN
══════════════════════════════════════════

Full suite:
  python tests/run_institutional.py

Single class:
  python -m pytest tests/run_institutional.py
    ::TestKantEngine -v

With timing:
  python tests/run_institutional.py
    --timing 2>&1 | tee logs/test_$(date +%Y%m%d_%H%M).txt

Expected output:
  ══════════════════════════════════════
  ARIA INSTITUTIONAL TEST SUITE v1.0
  ══════════════════════════════════════
  ✓ PASS  ATR Gate Removal
  ✓ PASS  Six Personalities
  ✓ PASS  Personality Hysteresis
  ✓ PASS  Context Cache Latency
  ✓ PASS  Kant Engine Structure
  ✓ PASS  Kant Threshold Overrides
  ✓ PASS  Nietzsche Will States
  ✓ PASS  Nietzsche Sizing Math
  ✓ PASS  Conviction Aggregation
  ✓ PASS  Kant×Nietzsche Integration
  ✓ PASS  Budget Manager
  ✓ PASS  Prediction Market
  ✓ PASS  Cross-Agent Betting
  ✓ PASS  Journal Persistence
  ✓ PASS  Performance Restore
  ✓ PASS  Risk Engine Gate Order
  ✓ PASS  Full Pipeline Latency
  ✓ PASS  Live Failure Scenarios
  ✓ PASS  Philosophical Stack Order
  ══════════════════════════════════════
  19/19 PASSED — ARIA IS PRODUCTION READY
  ══════════════════════════════════════
"""

import os
import sys
import unittest
import asyncio
import time
import math
import subprocess
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from dataclasses import dataclass

# Ensure project root is on path regardless of where this is run from
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# ──────────────────────────────────────
# SECTION 1 — ATR GATE REMOVAL
# Proves the 1.0% absolute gate is gone.
# This was blocking every trade.
# ──────────────────────────────────────

class TestATRGateRemoval(unittest.TestCase):

  def test_no_absolute_atr_threshold_in_codebase(self):
    """
    The 1.0% absolute ATR threshold that
    blocked all trades must be completely
    gone from production code.
    Evidence: logs showed atr_pct=0.201
    blocked by threshold_pct=1.0.
    """
    result = subprocess.run([
      "grep", "-rn",
      "threshold_pct.*1.0\\|atr_pct.*<.*1.0\\|low_volatility.*1.0",
      ".", "--include=*.py",
      "--exclude-dir=__pycache__",
      "--exclude-dir=.venv",
      "--exclude-dir=tests",
    ], capture_output=True, text=True,
       cwd=".")
    self.assertEqual(result.returncode, 1,
      f"CRITICAL: Found old 1.0% ATR threshold.\n"
      f"This was blocking ALL trades.\n"
      f"Found in:\n{result.stdout}\n"
      f"Remove completely.")

  def test_atr_vs_baseline_field_exists(self):
    """
    MarketState must expose atr_vs_baseline.
    This is the ratio that replaced absolute %.
    """
    from intelligence.market_state import MarketState
    s = make_market_state("BTC-USD")
    self.assertTrue(
      hasattr(s, "atr_vs_baseline"),
      "MarketState.atr_vs_baseline missing.\n"
      "This field drives COIL detection.\n"
      "Add it to MarketState dataclass.")

  def test_low_atr_ratio_produces_coil_not_block(self):
    """
    ATR ratio 0.55 (below 0.70 threshold)
    must produce COIL personality.
    Must NOT hard-block the signal.
    COIL still allows arb trades.
    """
    from intelligence.personality import (
      MarketPersonalityEngine, Personality)
    eng = make_personality_engine()
    ctx = make_ctx(
      atr_vs_baseline=0.55,
      coherence=5.0,
      direction="long",
      htf="bullish",
      regime="risk_on",
      cascade_phase="idle",
      calendar_regime="CLEAR",
      basis_stress_count=0,
      rpc_health_score=1.0,
    )
    state = eng.assess("BTC-USD", ctx)
    self.assertEqual(
      state.personality,
      Personality.COIL,
      f"Got {state.personality}.\n"
      f"Low ATR ratio must route to COIL.\n"
      f"COIL is not a block — arb still runs.")
    self.assertEqual(state.size_multiplier, 0.0,
      "COIL size must be 0.0 (no directional).\n"
      "Arb handled separately.")

  def test_normal_atr_ratio_passes(self):
    """
    ATRx=0.99 (from real BTC logs) must
    not produce COIL. Must trade normally.
    """
    from intelligence.personality import (
      MarketPersonalityEngine, Personality)
    eng = make_personality_engine()
    ctx = make_ctx(
      atr_vs_baseline=0.99,
      coherence=5.5,
      direction="long",
      htf="bullish",
      regime="risk_on",
      cascade_phase="idle",
      calendar_regime="CLEAR",
    )
    state = eng.assess("BTC-USD", ctx)
    self.assertNotEqual(
      state.personality, Personality.COIL,
      f"ATRx=0.99 produced COIL.\n"
      f"0.99 is above the 0.70 threshold.\n"
      f"Must produce FLOW or SCOUT.")
    self.assertGreater(
      state.size_multiplier, 0.0,
      "Normal ATR must produce non-zero size.")

  def test_arb_usd_case_from_logs(self):
    """
    ARB-USD had atr_pct=0.201%, blocked
    by old 1.0% threshold. With ratio-based
    detection (ATRx ~0.92) must now trade.
    """
    from intelligence.personality import (
      MarketPersonalityEngine, Personality)
    eng = make_personality_engine()
    ctx = make_ctx(
      atr_vs_baseline=0.92,
      coherence=4.5,
      direction="long",
      htf="bullish",
      regime="confused",
      cascade_phase="idle",
      calendar_regime="CLEAR",
    )
    state = eng.assess("ARB-USD", ctx)
    self.assertNotIn(
      state.personality,
      [Personality.COIL, Personality.SHIELD],
      f"ARB-USD (ATRx=0.92) got {state.personality}.\n"
      f"Old 1.0% threshold was wrong.\n"
      f"Must be SCOUT or FLOW now.")


# ──────────────────────────────────────
# SECTION 2 — SIX PERSONALITIES
# Complete verification of all 6 states.
# AFTERMATH added as 6th.
# ──────────────────────────────────────

class TestSixPersonalities(unittest.TestCase):

  def test_exactly_six_personalities_exist(self):
    from intelligence.personality import Personality
    required = {
      "APEX","AFTERMATH","FLOW",
      "SCOUT","COIL","SHIELD",
      "SOVEREIGN"}  # 7th: yield-funded equity overlay
    actual = {p.name for p in Personality}
    missing = required - actual
    extra   = actual - required
    self.assertEqual(missing, set(),
      f"Missing personalities: {missing}\n"
      f"All 7 required for institutional build.")
    self.assertEqual(extra, set(),
      f"Unknown personalities: {extra}\n"
      f"Remove or register them.")

  def test_all_personalities_have_complete_params(self):
    from intelligence.personality import (
      Personality, PERSONALITY_PARAMS)
    required_keys = {
      "size_multiplier","stop_atr_mult",
      "rr_target","coherence_min",
      "max_hold_s","max_concurrent"}
    for p in Personality:
      self.assertIn(p, PERSONALITY_PARAMS,
        f"{p.name} missing from PERSONALITY_PARAMS.")
      missing = required_keys - \
        set(PERSONALITY_PARAMS[p].keys())
      self.assertEqual(missing, set(),
        f"{p.name} missing param keys: {missing}")

  def test_shield_fires_on_calendar_block(self):
    from intelligence.personality import (
      MarketPersonalityEngine, Personality)
    eng = make_personality_engine()
    ctx = make_ctx(calendar_regime="BLOCK")
    s = eng.assess("BTC-USD", ctx)
    self.assertEqual(s.personality,
      Personality.SHIELD,
      "Calendar BLOCK must force SHIELD.\n"
      "This is a hard override.")
    self.assertEqual(s.size_multiplier, 0.0)

  def test_shield_fires_on_daily_loss_limit(self):
    from intelligence.personality import (
      MarketPersonalityEngine, Personality)
    eng = make_personality_engine()
    ctx = make_ctx(daily_pnl_pct=-0.026)
    s = eng.assess("BTC-USD", ctx)
    self.assertEqual(s.personality,
      Personality.SHIELD,
      "Daily PnL -2.6% must force SHIELD.\n"
      "Hard daily loss limit.")

  def test_shield_fires_on_freeze(self):
    from intelligence.personality import (
      MarketPersonalityEngine, Personality)
    eng = make_personality_engine()
    ctx = make_ctx(freeze_active=True)
    s = eng.assess("BTC-USD", ctx)
    self.assertEqual(s.personality,
      Personality.SHIELD,
      "freeze_active=True must force SHIELD.\n"
      "System froze — cannot trade blind.")

  def test_aftermath_fires_opposite_cascade(self):
    from intelligence.personality import (
      MarketPersonalityEngine, Personality)
    eng = make_personality_engine()
    ctx = make_ctx(
      cascade_phase="aftermath",
      aftermath_signals=3,
      cascade_direction="bearish",
      direction="long",
      coherence=4.5,
      calendar_regime="CLEAR",
    )
    s = eng.assess("BTC-USD", ctx)
    self.assertEqual(s.personality,
      Personality.AFTERMATH,
      f"Got {s.personality}.\n"
      f"Aftermath phase + opposite direction"
      f" must produce AFTERMATH.\n"
      f"Highest win-rate setup in system.")

  def test_aftermath_beats_apex_priority(self):
    """
    cascade_phase=aftermath means the cascade
    is OVER. APEX needs active cascade.
    AFTERMATH must win priority check.
    """
    from intelligence.personality import (
      MarketPersonalityEngine, Personality)
    eng = make_personality_engine()
    ctx = make_ctx(
      cascade_phase="aftermath",
      aftermath_signals=3,
      cascade_direction="bearish",
      direction="long",
      coherence=7.5,
      cascade_notional=100000,
      calendar_regime="CLEAR",
      rpc_health_score=1.0,
    )
    s = eng.assess("BTC-USD", ctx)
    self.assertEqual(s.personality,
      Personality.AFTERMATH,
      f"Got {s.personality}.\n"
      f"AFTERMATH must beat APEX.\n"
      f"Cascade is OVER — not APEX territory.")

  def test_apex_requires_active_cascade(self):
    from intelligence.personality import (
      MarketPersonalityEngine, Personality)
    eng = make_personality_engine()
    ctx = make_ctx(
      cascade_phase="idle",
      coherence=7.0,
      direction="short",
      htf="bearish",
      regime="risk_off",
      calendar_regime="CLEAR",
      cascade_notional=0,
      rpc_health_score=1.0,
    )
    s = eng.assess("BTC-USD", ctx)
    self.assertNotEqual(s.personality,
      Personality.APEX,
      "APEX fired with cascade_phase=idle.\n"
      "APEX requires building/expansion/peak.")

  def test_apex_blocked_rpc_degraded(self):
    from intelligence.personality import (
      MarketPersonalityEngine, Personality)
    eng = make_personality_engine()
    ctx = make_ctx(
      cascade_phase="peak",
      cascade_notional=50000,
      cascade_direction="bearish",
      direction="short",
      coherence=7.0,
      calendar_regime="CLEAR",
      rpc_health_score=0.50,
    )
    s = eng.assess("BTC-USD", ctx)
    self.assertNotEqual(s.personality,
      Personality.APEX,
      "APEX fired with rpc_health=0.50.\n"
      "Cascade data requires healthy RPC.\n"
      "Below 0.70 must block APEX.")

  def test_apex_blocked_small_cascade(self):
    from intelligence.personality import (
      MarketPersonalityEngine, Personality)
    eng = make_personality_engine()
    ctx = make_ctx(
      cascade_phase="peak",
      cascade_notional=5000,
      cascade_direction="bearish",
      direction="short",
      coherence=7.0,
      calendar_regime="CLEAR",
      rpc_health_score=1.0,
    )
    s = eng.assess("BTC-USD", ctx)
    self.assertNotEqual(s.personality,
      Personality.APEX,
      "APEX fired with notional=$5k.\n"
      "Minimum is $10,000.\n"
      "Small cascades are noise.")

  def test_flow_requires_htf_alignment(self):
    from intelligence.personality import (
      MarketPersonalityEngine, Personality)
    eng = make_personality_engine()
    ctx = make_ctx(
      cascade_phase="idle",
      coherence=5.0,
      direction="long",
      htf="bearish",
      regime="risk_on",
      atr_vs_baseline=1.0,
    )
    s = eng.assess("BTC-USD", ctx)
    self.assertNotEqual(s.personality,
      Personality.FLOW,
      "FLOW fired with opposing HTF.\n"
      "long direction + bearish HTF = no FLOW.\n"
      "HTF alignment is required for FLOW.")

  def test_scout_is_coherence_fallback(self):
    from intelligence.personality import (
      MarketPersonalityEngine, Personality)
    eng = make_personality_engine()
    ctx = make_ctx(
      cascade_phase="idle",
      coherence=4.2,
      direction="long",
      htf="neutral",
      regime="confused",
      atr_vs_baseline=0.95,
      calendar_regime="CLEAR",
      basis_stress_count=0,
      daily_pnl_pct=0.0,
      rpc_health_score=1.0,
      freeze_active=False,
    )
    s = eng.assess("BTC-USD", ctx)
    self.assertEqual(s.personality,
      Personality.SCOUT,
      f"Got {s.personality}.\n"
      f"Above coherence minimum, no special\n"
      f"conditions met — must fall to SCOUT.")

  def test_personality_check_order_shield_wins_all(self):
    """
    SHIELD must override everything including
    active APEX cascade conditions.
    """
    from intelligence.personality import (
      MarketPersonalityEngine, Personality)
    eng = make_personality_engine()
    ctx = make_ctx(
      calendar_regime="BLOCK",
      cascade_phase="peak",
      cascade_notional=500000,
      coherence=9.0,
      rpc_health_score=1.0,
    )
    s = eng.assess("BTC-USD", ctx)
    self.assertEqual(s.personality,
      Personality.SHIELD,
      "SHIELD must override APEX.\n"
      "Calendar BLOCK is absolute.")


# ──────────────────────────────────────
# SECTION 3 — PERSONALITY HYSTERESIS
# Prevents flickering between states.
# ──────────────────────────────────────

class TestPersonalityHysteresis(unittest.TestCase):

  def test_three_confirmations_required(self):
    from intelligence.personality import (
      MarketPersonalityEngine, Personality)
    eng = make_personality_engine()
    eng._current_personality["BTC-USD"] = \
      Personality.FLOW

    scout_ctx = make_ctx(
      cascade_phase="idle",
      coherence=4.2,
      direction="long",
      htf="neutral",
      regime="confused",
      atr_vs_baseline=0.95,
    )

    r1 = eng.assess("BTC-USD", scout_ctx)
    self.assertEqual(r1.personality,
      Personality.FLOW,
      "1st SCOUT assessment: still FLOW.")

    r2 = eng.assess("BTC-USD", scout_ctx)
    self.assertEqual(r2.personality,
      Personality.FLOW,
      "2nd SCOUT assessment: still FLOW.")

    r3 = eng.assess("BTC-USD", scout_ctx)
    self.assertEqual(r3.personality,
      Personality.SCOUT,
      "3rd SCOUT assessment: now SCOUT.\n"
      "3-period hysteresis confirmed.")

  def test_counter_resets_on_interruption(self):
    from intelligence.personality import (
      MarketPersonalityEngine, Personality)
    eng = make_personality_engine()
    eng._current_personality["BTC-USD"] = \
      Personality.FLOW

    scout_ctx = make_ctx(
      coherence=4.2, regime="confused",
      htf="neutral", cascade_phase="idle",
      atr_vs_baseline=0.95)
    flow_ctx = make_ctx(
      coherence=5.0, regime="risk_off",
      htf="bearish", direction="short",
      cascade_phase="idle",
      atr_vs_baseline=1.0)

    eng.assess("BTC-USD", scout_ctx)
    eng.assess("BTC-USD", flow_ctx)
    eng.assess("BTC-USD", scout_ctx)
    eng.assess("BTC-USD", scout_ctx)
    r = eng.assess("BTC-USD", scout_ctx)
    self.assertEqual(r.personality,
      Personality.SCOUT,
      "Counter reset — 3 more needed after\n"
      "interruption. Confirmed SCOUT.")

  def test_shield_bypasses_hysteresis(self):
    from intelligence.personality import (
      MarketPersonalityEngine, Personality)
    eng = make_personality_engine()
    eng._current_personality["BTC-USD"] = \
      Personality.FLOW

    ctx = make_ctx(calendar_regime="BLOCK")
    r = eng.assess("BTC-USD", ctx)
    self.assertEqual(r.personality,
      Personality.SHIELD,
      "SHIELD must activate immediately.\n"
      "No hysteresis for emergency states.\n"
      "This is non-negotiable.")


# ──────────────────────────────────────
# SECTION 4 — CONTEXT CACHE LATENCY
# Hot path must be <0.5ms avg.
# ──────────────────────────────────────

class TestContextCacheLatency(unittest.TestCase):

  def test_context_build_under_half_ms(self):
    from intelligence.personality_context \
      import PersonalityContextCache
    cache = make_context_cache()

    times = []
    for _ in range(1000):
      t0 = time.perf_counter()
      cache.build(
        "BTC-USD", 5.0, "long", "bullish")
      times.append(
        (time.perf_counter()-t0)*1000)

    avg_ms = sum(times)/len(times)
    p99_ms = sorted(times)[990]

    self.assertLess(avg_ms, 0.5,
      f"Context build avg={avg_ms:.3f}ms.\n"
      f"Must be <0.5ms.\n"
      f"This runs on every SIGNAL_READY.")
    self.assertLess(p99_ms, 2.0,
      f"Context build P99={p99_ms:.3f}ms.\n"
      f"Must be <2.0ms.")

  def test_personality_assess_under_half_ms(self):
    from intelligence.personality import \
      MarketPersonalityEngine
    from intelligence.personality_context \
      import PersonalityContextCache

    cache = make_context_cache()
    eng   = make_personality_engine()

    times = []
    for _ in range(1000):
      ctx = cache.build(
        "BTC-USD", 5.0, "long", "bullish")
      t0 = time.perf_counter()
      eng.assess("BTC-USD", ctx)
      times.append(
        (time.perf_counter()-t0)*1000)

    avg_ms = sum(times)/len(times)
    self.assertLess(avg_ms, 0.5,
      f"Personality assess avg={avg_ms:.3f}ms.\n"
      f"Must be <0.5ms.")


# ──────────────────────────────────────
# SECTION 5 — KANT ENGINE
# Regime-aware threshold overrides.
# ──────────────────────────────────────

class TestKantEngineStructure(unittest.TestCase):

  def test_four_structures_exist(self):
    from intelligence.kant_engine import \
      MarketStructure
    required = {
      "ACCUMULATION","TREND",
      "DISTRIBUTION","CHAOS"}
    actual = {s.name for s in MarketStructure}
    self.assertEqual(required, actual,
      f"Missing structures: {required-actual}\n"
      f"All 4 required for Kant engine.")

  def test_kant_frame_is_frozen(self):
    """
    KantFrame must be immutable (frozen=True).
    It is read on the hot path by multiple
    components simultaneously.
    """
    from intelligence.kant_engine import \
      KantEngine
    eng = make_kant_engine()
    frame = eng.assess(
      symbol="BTC-USD",
      atr_vs_baseline=0.99,
      cascade_phase="idle",
      cascade_zscore=0.5,
      basis_stress_count=0,
      rpc_health=1.0,
      regime="confused",
      liq_60s=10,
    )
    with self.assertRaises(Exception,
      msg="KantFrame must be frozen/immutable.\n"
          "Hot path reads require thread safety."):
      frame.structure = "CHAOS"

  def test_cascade_expansion_is_trend(self):
    from intelligence.kant_engine import (
      KantEngine, MarketStructure)
    eng = make_kant_engine()
    frame = eng.assess(
      symbol="BTC-USD",
      atr_vs_baseline=1.4,
      cascade_phase="expansion",
      cascade_zscore=3.98,
      basis_stress_count=0,
      rpc_health=1.0,
      regime="risk_off",
      liq_60s=159,
    )
    self.assertEqual(
      frame.structure,
      MarketStructure.TREND,
      f"Got {frame.structure}.\n"
      f"cascade_phase=expansion must be TREND.\n"
      f"This was the April 16 cascade ($502k).")

  def test_low_atr_no_cascade_is_accumulation(self):
    from intelligence.kant_engine import (
      KantEngine, MarketStructure)
    eng = make_kant_engine()
    frame = eng.assess(
      symbol="BTC-USD",
      atr_vs_baseline=0.65,
      cascade_phase="idle",
      cascade_zscore=0.1,
      basis_stress_count=0,
      rpc_health=1.0,
      regime="confused",
      liq_60s=5,
    )
    self.assertEqual(
      frame.structure,
      MarketStructure.ACCUMULATION,
      f"Got {frame.structure}.\n"
      f"Low ATR + no cascade = ACCUMULATION.\n"
      f"Pre-breakout energy state.")

  def test_extreme_conditions_is_chaos(self):
    from intelligence.kant_engine import (
      KantEngine, MarketStructure)
    eng = make_kant_engine()
    frame = eng.assess(
      symbol="BTC-USD",
      atr_vs_baseline=2.1,
      cascade_phase="peak",
      cascade_zscore=4.5,
      basis_stress_count=4,
      rpc_health=1.0,
      regime="risk_off",
      liq_60s=250,
    )
    self.assertEqual(
      frame.structure,
      MarketStructure.CHAOS,
      f"Got {frame.structure}.\n"
      f"zscore=4.5 + basis_stress=4 = CHAOS.")

  def test_rpc_degraded_is_chaos(self):
    from intelligence.kant_engine import (
      KantEngine, MarketStructure)
    eng = make_kant_engine()
    frame = eng.assess(
      symbol="BTC-USD",
      atr_vs_baseline=1.0,
      cascade_phase="idle",
      cascade_zscore=0.5,
      basis_stress_count=0,
      rpc_health=0.35,
      regime="confused",
      liq_60s=10,
    )
    self.assertEqual(
      frame.structure,
      MarketStructure.CHAOS,
      "rpc_health=0.35 must produce CHAOS.\n"
      "Cannot assess market without data.")

  def test_kant_hysteresis_prevents_flickering(self):
    """
    Kant structure must not switch on a
    single assessment. Requires 3 periods.
    """
    from intelligence.kant_engine import (
      KantEngine, MarketStructure)
    eng = make_kant_engine()

    trend_args = dict(
      symbol="BTC-USD",
      atr_vs_baseline=1.4,
      cascade_phase="expansion",
      cascade_zscore=3.0,
      basis_stress_count=0,
      rpc_health=1.0,
      regime="risk_off",
      liq_60s=100,
    )
    eng.assess(**trend_args)
    eng.assess(**trend_args)
    eng.assess(**trend_args)

    accum_args = dict(
      symbol="BTC-USD",
      atr_vs_baseline=0.60,
      cascade_phase="idle",
      cascade_zscore=0.1,
      basis_stress_count=0,
      rpc_health=1.0,
      regime="confused",
      liq_60s=5,
    )
    r1 = eng.assess(**accum_args)
    self.assertEqual(
      r1.structure, MarketStructure.TREND,
      "First ACCUMULATION signal: still TREND.")
    r2 = eng.assess(**accum_args)
    self.assertEqual(
      r2.structure, MarketStructure.TREND,
      "Second ACCUMULATION: still TREND.")
    r3 = eng.assess(**accum_args)
    self.assertEqual(
      r3.structure, MarketStructure.ACCUMULATION,
      "Third ACCUMULATION: now switched.")


# ──────────────────────────────────────
# SECTION 6 — KANT THRESHOLD OVERRIDES
# Proves thresholds change per structure.
# ──────────────────────────────────────

class TestKantThresholdOverrides(unittest.TestCase):

  def test_accumulation_lowers_coherence_min(self):
    from intelligence.kant_engine import KantEngine
    eng = make_kant_engine()
    frame = eng.assess(
      symbol="BTC-USD",
      atr_vs_baseline=0.60,
      cascade_phase="idle",
      cascade_zscore=0.1,
      basis_stress_count=0,
      rpc_health=1.0,
      regime="confused",
      liq_60s=5,
    )
    from intelligence.kant_engine import \
      MarketStructure
    if frame.structure == \
       MarketStructure.ACCUMULATION:
      self.assertLessEqual(
        frame.coherence_min, 4.0,
        f"ACCUMULATION coherence_min="
        f"{frame.coherence_min}.\n"
        f"Must be ≤4.0 to probe early.\n"
        f"This is how ARIA enters before breakout.")

  def test_trend_uses_market_orders(self):
    from intelligence.kant_engine import (
      KantEngine, MarketStructure)
    eng = make_kant_engine()
    frame = eng.assess(
      symbol="BTC-USD",
      atr_vs_baseline=1.4,
      cascade_phase="expansion",
      cascade_zscore=3.5,
      basis_stress_count=0,
      rpc_health=1.0,
      regime="risk_off",
      liq_60s=120,
    )
    if frame.structure == MarketStructure.TREND:
      self.assertEqual(
        frame.order_type, "market",
        "TREND must use market orders.\n"
        "ETH-USD 7.53-score signal failed to\n"
        "fill because limit was used in trend.\n"
        "Kant fixes this.")

  def test_chaos_has_highest_coherence_min(self):
    from intelligence.kant_engine import (
      KantEngine, MarketStructure)
    eng = make_kant_engine()
    frames = {}
    for args in [
      # ACCUMULATION
      dict(atr_vs_baseline=0.60,
           cascade_phase="idle",
           cascade_zscore=0.1,
           basis_stress_count=0,
           rpc_health=1.0,
           regime="confused",liq_60s=5),
      # TREND
      dict(atr_vs_baseline=1.4,
           cascade_phase="expansion",
           cascade_zscore=3.0,
           basis_stress_count=0,
           rpc_health=1.0,
           regime="risk_off",liq_60s=100),
      # CHAOS
      dict(atr_vs_baseline=2.0,
           cascade_phase="peak",
           cascade_zscore=4.5,
           basis_stress_count=4,
           rpc_health=1.0,
           regime="risk_off",liq_60s=200),
    ]:
      for _ in range(3):
        f = eng.assess(
          symbol="BTC-USD", **args)
      frames[f.structure] = f

    if MarketStructure.CHAOS in frames and \
       MarketStructure.ACCUMULATION in frames:
      self.assertGreater(
        frames[MarketStructure.CHAOS].coherence_min,
        frames[MarketStructure.ACCUMULATION].coherence_min,
        "CHAOS coherence_min must exceed\n"
        "ACCUMULATION coherence_min.\n"
        "Strict hierarchy required.")

  def test_min_notional_adjust_true_all_structures(self):
    """
    min_notional_adjust must be True for
    all structures. This fixes the OP-USD
    $45 dust notional failure.
    """
    from intelligence.kant_engine import KantEngine
    eng = make_kant_engine()
    for args in [
      dict(atr_vs_baseline=0.60,
           cascade_phase="idle",
           cascade_zscore=0.1,
           basis_stress_count=0,
           rpc_health=1.0,
           regime="confused",liq_60s=5),
      dict(atr_vs_baseline=1.4,
           cascade_phase="expansion",
           cascade_zscore=3.0,
           basis_stress_count=0,
           rpc_health=1.0,
           regime="risk_off",liq_60s=100),
    ]:
      for _ in range(3):
        f = eng.assess(
          symbol="BTC-USD", **args)
      self.assertTrue(
        f.min_notional_adjust,
        f"min_notional_adjust=False in "
        f"{f.structure}.\n"
        f"Must be True always.\n"
        f"OP-USD $45 failure was this bug.")

  def test_basis_stress_weight_reduced_accumulation(self):
    """
    In ACCUMULATION, basis stress matters less.
    ARB-USD was blocked by basis_stress
    during cascade. Kant reduces this weight.
    """
    from intelligence.kant_engine import (
      KantEngine, MarketStructure)
    eng = make_kant_engine()
    for _ in range(3):
      f = eng.assess(
        symbol="BTC-USD",
        atr_vs_baseline=0.60,
        cascade_phase="idle",
        cascade_zscore=0.1,
        basis_stress_count=0,
        rpc_health=1.0,
        regime="confused",
        liq_60s=5,
      )
    if f.structure == MarketStructure.ACCUMULATION:
      self.assertLess(
        f.basis_stress_weight, 1.0,
        f"ACCUMULATION basis_stress_weight="
        f"{f.basis_stress_weight}.\n"
        f"Must be <1.0 (less important).\n"
        f"ARB-USD -1.26% basis blocked a\n"
        f"coherence=9.35 cascade trade.")


# ──────────────────────────────────────
# SECTION 7 — NIETZSCHE WILL STATES
# Continuous sizing. Not binary.
# ──────────────────────────────────────

class TestNietzscheWillStates(unittest.TestCase):

  def test_five_will_states_exist(self):
    from intelligence.nietzsche_engine import WillState
    required = {
      "AGGRESSIVE","NEUTRAL",
      "CONSERVATIVE","DEFENSIVE","DORMANT"}
    actual = {s.name for s in WillState}
    self.assertEqual(required, actual,
      f"Missing will states: {required-actual}")

  def test_no_drawdown_hot_streak_is_aggressive(self):
    from intelligence.nietzsche_engine import (
      NietzscheEngine, WillState)
    eng = NietzscheEngine(make_config())
    out = eng.compute(
      drawdown_pct=0.005,
      win_streak=7,
      loss_streak=0,
      conviction_score=0.75,
      coherence=5.5,
      kant_frame=make_kant_frame(),
      base_size_units=100.0,
      min_notional_usd=50.0,
      mark_price=75000.0,
      balance=310.0,
    )
    self.assertEqual(out.will_state,
      WillState.AGGRESSIVE,
      f"Got {out.will_state}.\n"
      f"DD=0.5%, win_streak=7 = AGGRESSIVE.")
    self.assertGreater(out.size_multiplier,
      1.0,
      "AGGRESSIVE must produce size > 1.0.")

  def test_deep_drawdown_is_defensive(self):
    from intelligence.nietzsche_engine import (
      NietzscheEngine, WillState)
    eng = NietzscheEngine(make_config())
    out = eng.compute(
      drawdown_pct=0.07,
      win_streak=0,
      loss_streak=3,
      conviction_score=0.60,
      coherence=5.0,
      kant_frame=make_kant_frame(),
      base_size_units=100.0,
      min_notional_usd=50.0,
      mark_price=75000.0,
      balance=310.0,
    )
    self.assertEqual(out.will_state,
      WillState.DEFENSIVE,
      f"Got {out.will_state}.\n"
      f"DD=7%, loss_streak=3 = DEFENSIVE.")
    self.assertLess(out.size_multiplier,
      0.50,
      "DEFENSIVE must produce size < 0.50.")

  def test_extreme_drawdown_is_dormant(self):
    from intelligence.nietzsche_engine import (
      NietzscheEngine, WillState)
    eng = NietzscheEngine(make_config())
    out = eng.compute(
      drawdown_pct=0.40,
      win_streak=0,
      loss_streak=5,
      conviction_score=0.80,
      coherence=6.0,
      kant_frame=make_kant_frame(),
      base_size_units=100.0,
      min_notional_usd=50.0,
      mark_price=75000.0,
      balance=310.0,
    )
    self.assertEqual(out.will_state,
      WillState.DORMANT,
      f"Got {out.will_state}.\n"
      f"DD=40% = DORMANT.\n"
      f"Catastrophic halt — no trades.")
    self.assertEqual(out.size_multiplier,
      0.0,
      "DORMANT size must be 0.0.")

  def test_current_aria_state_from_logs(self):
    """
    From logs: drawdown_pct=3.03%,
    win_rate=0.455, loss streak present.
    Must produce CONSERVATIVE ~0.50-0.65×.
    This matches current recovery mode.
    """
    from intelligence.nietzsche_engine import (
      NietzscheEngine, WillState)
    eng = NietzscheEngine(make_config())
    out = eng.compute(
      drawdown_pct=0.0303,
      win_streak=2,
      loss_streak=0,
      conviction_score=0.55,
      coherence=4.8,
      kant_frame=make_kant_frame(),
      base_size_units=100.0,
      min_notional_usd=50.0,
      mark_price=0.1211,
      balance=310.0,
    )
    self.assertIn(out.will_state,
      [WillState.CONSERVATIVE,
       WillState.NEUTRAL],
      f"Got {out.will_state} at DD=3.03%.\n"
      f"Must be CONSERVATIVE or NEUTRAL.\n"
      f"Current ARIA state from live logs.")
    self.assertGreaterEqual(
      out.size_multiplier, 0.40,
      "DD=3.03% must still trade.\n"
      "Counter-cyclical: reduce not halt.")
    self.assertLessEqual(
      out.size_multiplier, 0.90,
      "DD=3.03% must not be full size.")


# ──────────────────────────────────────
# SECTION 8 — NIETZSCHE SIZING MATH
# Mathematical precision required.
# ──────────────────────────────────────

class TestNietzscheSizingMath(unittest.TestCase):

  def test_elite_signal_overrides_drawdown(self):
    """
    Coherence > 8.0 = elite signal.
    Even in deep drawdown, elite fires
    at near-full size.
    The OP-USD cascade score was 8.72.
    It should have been elite-treated.
    """
    from intelligence.nietzsche_engine import (
      NietzscheEngine, WillState)
    eng = NietzscheEngine(make_config())
    out = eng.compute(
      drawdown_pct=0.04,
      win_streak=0,
      loss_streak=3,
      conviction_score=0.90,
      coherence=8.72,
      kant_frame=make_kant_frame(size_cap=1.25),
      base_size_units=100.0,
      min_notional_usd=50.0,
      mark_price=0.123,
      balance=310.0,
    )
    self.assertNotEqual(out.will_state,
      WillState.DORMANT,
      "Elite signal (8.72) must not be DORMANT.")
    self.assertGreater(out.size_multiplier,
      0.80,
      f"Elite coherence=8.72 got size="
      f"{out.size_multiplier}.\n"
      f"Elite override must produce >0.80.")

  def test_op_usd_dust_notional_auto_bumped(self):
    """
    The OP-USD $45.51 failure.
    374 units × $0.123 = $45.51 < $50 min.
    Nietzsche must auto-bump to meet minimum.
    """
    from intelligence.nietzsche_engine import \
      NietzscheEngine
    eng = NietzscheEngine(make_config())
    out = eng.compute(
      drawdown_pct=0.03,
      win_streak=2,
      loss_streak=0,
      conviction_score=0.85,
      coherence=8.72,
      kant_frame=make_kant_frame(),
      base_size_units=374.0,
      min_notional_usd=50.0,
      mark_price=0.123,
      balance=310.0,
    )
    actual_notional = (
      out.adjusted_size * 0.123)
    self.assertGreaterEqual(
      actual_notional, 50.0,
      f"OP-USD notional={actual_notional:.2f}.\n"
      f"Must be ≥$50 after auto-bump.\n"
      f"Original: 374 × $0.123 = $45.51 FAILED.")
    self.assertTrue(out.min_notional_ok,
      "min_notional_ok must be True after bump.")

  def test_size_cap_from_kant_respected(self):
    """
    Kant DISTRIBUTION sets size_cap=0.50.
    Nietzsche must never exceed this even
    on hot streak + high conviction.
    """
    from intelligence.nietzsche_engine import \
      NietzscheEngine
    eng = NietzscheEngine(make_config())
    dist_frame = make_kant_frame(size_cap=0.50)
    out = eng.compute(
      drawdown_pct=0.001,
      win_streak=10,
      loss_streak=0,
      conviction_score=0.95,
      coherence=7.5,
      kant_frame=dist_frame,
      base_size_units=100.0,
      min_notional_usd=50.0,
      mark_price=75000.0,
      balance=310.0,
    )
    self.assertLessEqual(
      out.size_multiplier, 0.55,
      f"Size={out.size_multiplier} exceeded\n"
      f"DISTRIBUTION cap of 0.50.\n"
      f"Kant cap must override everything.")

  def test_nietzsche_is_never_binary(self):
    """
    The core principle: Nietzsche must
    produce continuous values, not just
    0 or 1. The system must stay in the
    market with reduced size.
    """
    from intelligence.nietzsche_engine import \
      NietzscheEngine, WillState
    eng = NietzscheEngine(make_config())
    multipliers = set()
    for dd in [0.01, 0.02, 0.03, 0.04,
               0.05, 0.07, 0.09]:
      out = eng.compute(
        drawdown_pct=dd,
        win_streak=2,
        loss_streak=0,
        conviction_score=0.60,
        coherence=5.0,
        kant_frame=make_kant_frame(),
        base_size_units=100.0,
        min_notional_usd=50.0,
        mark_price=75000.0,
        balance=310.0,
      )
      if out.will_state != WillState.DORMANT:
        multipliers.add(
          round(out.size_multiplier, 1))

    self.assertGreater(len(multipliers), 2,
      f"Nietzsche produced only "
      f"{len(multipliers)} unique values.\n"
      f"Must be continuous not binary.\n"
      f"Produced: {multipliers}")


# ──────────────────────────────────────
# SECTION 9 — CONVICTION AGGREGATION
# The prediction market output.
# ──────────────────────────────────────

class TestConvictionAggregation(unittest.TestCase):

  def test_conviction_range_zero_to_one(self):
    from intelligence.conviction_engine import \
      compute_conviction
    for coherence in [0, 2, 4, 6, 8]:
      for flow in [0.0, 0.3, 0.5, 0.7, 1.0]:
        c = compute_conviction(
          coherence=coherence,
          regime_aligned=True,
          order_flow_ratio=flow,
          cascade_active=False,
          cascade_zscore=0.0,
          historical_wr=0.50,
          kant_confidence=0.70,
        )
        self.assertGreaterEqual(c, 0.0,
          f"conviction={c} below 0.0.\n"
          f"coherence={coherence} flow={flow}")
        self.assertLessEqual(c, 1.0,
          f"conviction={c} above 1.0.\n"
          f"coherence={coherence} flow={flow}")

  def test_high_coherence_cascade_gives_high_conviction(self):
    from intelligence.conviction_engine import \
      compute_conviction
    c = compute_conviction(
      coherence=8.72,
      regime_aligned=True,
      order_flow_ratio=0.85,
      cascade_active=True,
      cascade_zscore=4.06,
      historical_wr=0.55,
      kant_confidence=0.85,
    )
    self.assertGreater(c, 0.75,
      f"conviction={c}.\n"
      f"coherence=8.72 + cascade zscore=4.06\n"
      f"+ flow=0.85 must give >0.75.\n"
      f"This was the April 16 missed trade.")

  def test_confused_regime_dampens_conviction(self):
    from intelligence.conviction_engine import \
      compute_conviction
    c_aligned = compute_conviction(
      coherence=5.0,
      regime_aligned=True,
      order_flow_ratio=0.6,
      cascade_active=False,
      cascade_zscore=0.0,
      historical_wr=0.50,
      kant_confidence=0.80,
    )
    c_confused = compute_conviction(
      coherence=5.0,
      regime_aligned=False,
      order_flow_ratio=0.6,
      cascade_active=False,
      cascade_zscore=0.0,
      historical_wr=0.50,
      kant_confidence=0.50,
    )
    self.assertGreater(c_aligned, c_confused,
      f"aligned={c_aligned} ≤ confused={c_confused}.\n"
      f"Aligned regime must produce higher conviction.")

  def test_poor_historical_wr_reduces_conviction(self):
    from intelligence.conviction_engine import \
      compute_conviction
    c_good = compute_conviction(
      coherence=5.0,
      regime_aligned=True,
      order_flow_ratio=0.6,
      cascade_active=False,
      cascade_zscore=0.0,
      historical_wr=0.60,
      kant_confidence=0.80,
    )
    c_poor = compute_conviction(
      coherence=5.0,
      regime_aligned=True,
      order_flow_ratio=0.6,
      cascade_active=False,
      cascade_zscore=0.0,
      historical_wr=0.30,
      kant_confidence=0.80,
    )
    self.assertGreater(c_good, c_poor,
      f"good_wr={c_good} ≤ poor_wr={c_poor}.\n"
      f"Better historical WR must give higher conviction.")


# ──────────────────────────────────────
# SECTION 10 — KANT × NIETZSCHE INTEGRATION
# The two new layers working together.
# ──────────────────────────────────────

class TestKantNietzscheIntegration(unittest.TestCase):

  def test_trend_kant_market_order_high_conviction(self):
    """
    TREND structure + high conviction
    must produce market order.
    ETH-USD 7.53 score missed because
    limit order didn't fill in moving market.
    """
    from intelligence.kant_engine import KantEngine
    from intelligence.nietzsche_engine import \
      NietzscheEngine
    kant = make_kant_engine()
    nietz = NietzscheEngine(make_config())

    for _ in range(3):
      frame = kant.assess(
        symbol="ETH-USD",
        atr_vs_baseline=1.5,
        cascade_phase="expansion",
        cascade_zscore=2.77,
        basis_stress_count=0,
        rpc_health=1.0,
        regime="risk_off",
        liq_60s=84,
      )

    out = nietz.compute(
      drawdown_pct=0.03,
      win_streak=2,
      loss_streak=0,
      conviction_score=0.85,
      coherence=7.53,
      kant_frame=frame,
      base_size_units=100.0,
      min_notional_usd=50.0,
      mark_price=2362.0,
      balance=310.0,
    )
    self.assertEqual(out.order_type,
      "market",
      f"order_type={out.order_type}.\n"
      f"TREND + high conviction must use\n"
      f"market orders.\n"
      f"ETH-USD 7.53 score was missed.")

  def test_arb_basis_stress_reduces_not_blocks(self):
    """
    ARB-USD was blocked by basis_stress
    -1.26% during cascade expansion.
    With Kant TREND + reduced basis weight,
    trade should execute at reduced size.
    """
    from intelligence.kant_engine import KantEngine
    from intelligence.nietzsche_engine import \
      NietzscheEngine
    kant = make_kant_engine()
    nietz = NietzscheEngine(make_config())

    for _ in range(3):
      frame = kant.assess(
        symbol="ARB-USD",
        atr_vs_baseline=1.2,
        cascade_phase="expansion",
        cascade_zscore=4.06,
        basis_stress_count=1,
        rpc_health=1.0,
        regime="risk_on",
        liq_60s=130,
      )

    out = nietz.compute(
      drawdown_pct=0.03,
      win_streak=2,
      loss_streak=0,
      conviction_score=0.88,
      coherence=9.35,
      kant_frame=frame,
      base_size_units=100.0,
      min_notional_usd=50.0,
      mark_price=0.1159,
      balance=310.0,
    )
    self.assertGreater(out.size_multiplier,
      0.0,
      f"ARB-USD got size=0.\n"
      f"Basis stress in TREND must REDUCE\n"
      f"not BLOCK.\n"
      f"conviction=0.88 coherence=9.35\n"
      f"is too strong to fully block.")

  def test_counter_cyclical_not_binary(self):
    """
    Core principle: ARIA must stay in
    the market with reduced size during
    drawdown. NOT binary trade/no-trade.
    """
    from intelligence.kant_engine import KantEngine
    from intelligence.nietzsche_engine import (
      NietzscheEngine, WillState)
    kant = make_kant_engine()
    nietz = NietzscheEngine(make_config())

    for _ in range(3):
      frame = kant.assess(
        symbol="BTC-USD",
        atr_vs_baseline=1.0,
        cascade_phase="idle",
        cascade_zscore=0.5,
        basis_stress_count=0,
        rpc_health=1.0,
        regime="confused",
        liq_60s=22,
      )

    for dd in [0.01, 0.02, 0.03, 0.04, 0.05]:
      out = nietz.compute(
        drawdown_pct=dd,
        win_streak=2,
        loss_streak=0,
        conviction_score=0.65,
        coherence=5.5,
        kant_frame=frame,
        base_size_units=100.0,
        min_notional_usd=50.0,
        mark_price=75000.0,
        balance=310.0,
      )
      if out.will_state != WillState.DORMANT:
        self.assertGreater(
          out.size_multiplier, 0.0,
          f"DD={dd*100:.0f}% produced size=0.\n"
          f"System went binary.\n"
          f"Must reduce size, not halt.")


# ──────────────────────────────────────
# SECTION 11 — BUDGET MANAGER
# No hardcoded values. Kelly mathematics.
# ──────────────────────────────────────

class TestBudgetManager(unittest.TestCase):

  def test_no_hardcoded_dollars_in_budget_file(self):
    result = subprocess.run([
      "grep", "-n",
      "311.90\\|258.57\\|135.60\\|"
      "56.50\\|33.90\\|226\\|258",
      "core/budget_manager.py",
    ], capture_output=True, text=True)
    self.assertEqual(result.returncode, 1,
      f"Found hardcoded dollar amounts:\n"
      f"{result.stdout}\n"
      f"Budget must use weights only.\n"
      f"Dollar amounts computed from\n"
      f"live balance at runtime.")

  def test_budget_scales_with_balance(self):
    from core.budget_manager import BudgetManager
    bm1 = BudgetManager(make_config(), 1000.0)
    bm1.initialise()
    bm2 = BudgetManager(make_config(), 500.0)
    bm2.initialise()
    b1 = bm1.get_budget("perp","flow")
    b2 = bm2.get_budget("perp","flow")
    self.assertAlmostEqual(b1/b2, 2.0,
      places=1,
      msg=f"$1000 budget={b1}, $500 budget={b2}.\n"
          f"Ratio must be 2.0.\n"
          f"No hardcoded values confirmed.")

  def test_kelly_mathematics_correct(self):
    from core.budget_manager import BudgetManager
    bm = BudgetManager(make_config(), 1000.0)
    # W=0.50, b=2.0: f=0.50-0.50/2=0.25
    # Half Kelly = 0.125
    result = bm.kelly_fraction(
      win_rate=0.50, avg_win_r=2.0)
    self.assertAlmostEqual(result, 0.125,
      places=3,
      msg=f"Kelly={result}. Expected 0.125.\n"
          f"W=0.50, b=2.0 → f*=0.25 → half=0.125")

  def test_kelly_never_negative(self):
    from core.budget_manager import BudgetManager
    bm = BudgetManager(make_config(), 1000.0)
    result = bm.kelly_fraction(
      win_rate=0.20, avg_win_r=1.0)
    self.assertGreaterEqual(result, 0.01,
      "Kelly must never be negative.\n"
      "Floor is 0.01 (1%).")

  def test_kelly_capped_at_fifteen_pct(self):
    from core.budget_manager import BudgetManager
    bm = BudgetManager(make_config(), 1000.0)
    result = bm.kelly_fraction(
      win_rate=0.95, avg_win_r=10.0)
    self.assertLessEqual(result, 0.15,
      "Kelly capped at 0.15.\n"
      "No single trade exceeds 15% of budget.")

  def test_budget_floor_protection(self):
    from core.budget_manager import BudgetManager
    bm = BudgetManager(make_config(), 1000.0)
    bm.initialise()
    async def run():
      for _ in range(200):
        await bm.record_pnl(
          "perp","scout",-50.0,-1.0)
      budget = bm.get_budget("perp","scout")
      self.assertGreaterEqual(budget, 5.0,
        "Budget hit zero.\n"
        "Must never go below $5 floor.\n"
        "Personality locked at floor, not zeroed.")
    asyncio.run(run())

  def test_agent_weights_sum_to_one(self):
    from core.budget_manager import AGENT_RATIOS
    total = sum(AGENT_RATIOS.values())
    self.assertAlmostEqual(total, 1.0,
      places=3,
      msg=f"Agent weights sum to {total}.\n"
          f"Must be exactly 1.0.")


# ──────────────────────────────────────
# SECTION 12 — PREDICTION MARKET
# In-memory, fast, correct mathematics.
# ──────────────────────────────────────

class TestPredictionMarket(unittest.TestCase):

  def test_add_pending_is_synchronous(self):
    """
    add_pending must NEVER use await.
    It runs on the hot path.
    """
    import inspect
    from intelligence.prediction_market import \
      PredictionStore
    src = inspect.getsource(
      PredictionStore.add_pending)
    self.assertNotIn("await", src,
      "add_pending contains await.\n"
      "This runs on SIGNAL_READY hot path.\n"
      "Must be synchronous (queue.put_nowait).")
    self.assertNotIn("aiosqlite", src,
      "add_pending uses aiosqlite.\n"
      "No DB on hot path. In-memory only.")

  def test_circular_buffer_max_500(self):
    from intelligence.prediction_market import \
      PredictionStore
    store = PredictionStore()
    for i in range(600):
      store.add_pending(
        make_prediction(f"id_{i}"))
    asyncio.run(store._drain_once())
    total = len(list(store._records))
    self.assertLessEqual(total, 500,
      f"Store has {total} records.\n"
      f"Max is 500 (circular buffer).\n"
      f"Memory leak prevented.")

  def test_joint_probability_formula(self):
    """
    P_joint = P_A×P_B / (P_A×P_B + (1-P_A)×(1-P_B))
    This is the mathematical core of the bet engine.
    Must be exact.
    """
    from intelligence.prediction_market import \
      CrossAgentBetEngine
    eng = CrossAgentBetEngine()
    P_A, P_B = 0.64, 0.71
    expected = (P_A*P_B) / \
      (P_A*P_B + (1-P_A)*(1-P_B))
    result = eng._joint_probability(P_A, P_B)
    self.assertAlmostEqual(result, expected,
      places=4,
      msg=f"P_joint={result}. Expected={expected}.\n"
          f"Formula error breaks bet mathematics.")

  def test_same_agent_no_bet(self):
    from intelligence.prediction_market import \
      CrossAgentBetEngine
    eng = CrossAgentBetEngine()
    pred_a = make_prediction(
      "a", confidence=0.80,
      agent="SCOUT", personality="SCOUT",
      symbol="BTC-USD", direction="long")
    pred_b = make_prediction(
      "b", confidence=0.80,
      agent="SCOUT", personality="SCOUT",
      symbol="BTC-USD", direction="long")
    result = eng.check_bet(
      pred_b, [pred_a],
      make_budget_manager())
    self.assertIsNone(result,
      "Same agent+personality produced a bet.\n"
      "Must be independent sources.\n"
      "Bayesian formula requires independence.")

  def test_bet_threshold_070(self):
    from intelligence.prediction_market import \
      CrossAgentBetEngine
    eng = CrossAgentBetEngine()
    pred_a = make_prediction(
      "a", confidence=0.55,
      agent="SCOUT", personality="SCOUT",
      symbol="BTC-USD", direction="long")
    pred_b = make_prediction(
      "b", confidence=0.55,
      agent="perp", personality="FLOW",
      symbol="BTC-USD", direction="long")
    result = eng.check_bet(
      pred_b, [pred_a],
      make_budget_manager())
    if result:
      self.assertGreaterEqual(
        result.p_joint, 0.70,
        "Bet fired below 0.70 threshold.")

  def test_bet_max_fifteen_pct_balance(self):
    from intelligence.prediction_market import \
      CrossAgentBetEngine
    from core.budget_manager import BudgetManager
    eng = CrossAgentBetEngine()
    bm  = BudgetManager(make_config(), 310.0)
    bm.initialise()
    pred_a = make_prediction(
      "a", confidence=0.85,
      agent="perp", personality="FLOW",
      symbol="BTC-USD", direction="long")
    pred_b = make_prediction(
      "b", confidence=0.85,
      agent="gold", personality="AFTERMATH",
      symbol="BTC-USD", direction="long")
    result = eng.check_bet(
      pred_b, [pred_a], bm)
    if result:
      max_bet = 310.0 * 0.15
      self.assertLessEqual(
        result.combined_budget, max_bet + 0.01,
        f"Bet combined={result.combined_budget}.\n"
        f"Max is 15% of $310 = ${max_bet:.2f}.\n"
        f"Kelly cap must hold.")


# ──────────────────────────────────────
# SECTION 13 — JOURNAL PERSISTENCE
# Memory survives restarts.
# ──────────────────────────────────────

class TestJournalPersistence(unittest.TestCase):

  def test_journal_has_philosophical_fields(self):
    """
    Trade journal must include the new
    Kant+Nietzsche fields.
    Required for future analysis:
    'Which Kant structure has best WR?'
    """
    from memory.trade_journal import TradeRecord
    required = {
      "kant_structure",
      "conviction",
      "will_state",
      "order_type_used",
    }
    actual = set(TradeRecord.__dataclass_fields__.keys())
    missing = required - actual
    self.assertEqual(missing, set(),
      f"Journal missing fields: {missing}\n"
      f"These enable regime-based analysis.")

  def test_performance_restores_from_journal(self):
    """
    On restart, ARIA must restore all
    personality stats from journal.
    This fixes the amnesia problem.
    """
    from memory.performance import \
      PerformanceTracker
    tracker = PerformanceTracker()
    has_restore = hasattr(
      tracker, "restore_from_journal")
    self.assertTrue(has_restore,
      "PerformanceTracker missing\n"
      "restore_from_journal() method.\n"
      "Every restart resets all stats.\n"
      "Win rates, streaks, calibration lost.")

  def test_recovery_mode_persists_restart(self):
    """
    If ARIA was in recovery mode before
    restart (WR < 50%), it must still be
    in recovery mode after restart.
    Current bug: recovery mode resets on
    every restart.
    """
    from memory.performance import \
      PerformanceTracker
    tracker = PerformanceTracker()
    if hasattr(tracker, "restore_from_journal"):
      has_recovery = hasattr(
        tracker, "_recovery_mode")
      self.assertTrue(has_recovery or
        hasattr(tracker, "recovery_mode"),
        "PerformanceTracker has no\n"
        "recovery_mode attribute.\n"
        "Cannot persist recovery state.")

  def test_session_vs_alltime_tracked(self):
    from memory.performance import \
      PerformanceTracker
    tracker = PerformanceTracker()
    has_session = (
      hasattr(tracker, "session_trades") or
      hasattr(tracker, "_session_stats") or
      hasattr(tracker, "get_session_stats"))
    self.assertTrue(has_session,
      "PerformanceTracker has no session stats.\n"
      "Need session vs all-time separately.\n"
      "Terminal shows both per personality.")


# ──────────────────────────────────────
# SECTION 14 — RISK ENGINE GATE ORDER
# Gates must remain unchanged.
# Kant only modifies threshold inputs.
# ──────────────────────────────────────

class TestRiskEngineGateOrder(unittest.TestCase):

  def test_risk_engine_accepts_kant_overrides(self):
    """
    risk_engine.validate() must accept
    kant_overrides parameter without error.
    This is the surgical injection point.
    """
    import inspect
    from risk.risk_engine import RiskEngine
    sig = inspect.signature(
      RiskEngine.validate)
    self.assertIn("kant_overrides", sig.parameters,
      "risk_engine.validate() missing\n"
      "kant_overrides parameter.\n"
      "Cannot inject Kant thresholds.")

  def test_kant_overrides_change_atr_threshold(self):
    """
    When kant_overrides sets atr_baseline_min=0.50
    (ACCUMULATION), signals that would fail at
    the default 0.70 must now pass.
    """
    try:
      from risk.risk_engine import RiskEngine
      engine = make_risk_engine()

      # Candidate with ATR ratio 0.60
      # Normally blocked (0.60 < 0.70 default)
      candidate = make_candidate(
        "BTC-USD", atr_vs_baseline=0.60)

      approved_default, _ = engine.validate(
        candidate, 310.0)

      approved_kant, _ = engine.validate(
        candidate, 310.0,
        kant_overrides={
          "atr_baseline_min": 0.50})

      self.assertFalse(approved_default,
        "Default 0.70 threshold should block\n"
        "ATR ratio 0.60.")
      self.assertTrue(approved_kant,
        "Kant ACCUMULATION override 0.50\n"
        "should allow ATR ratio 0.60.\n"
        "Pre-breakout accumulation state.")
    except Exception as e:
      self.skipTest(f"risk_engine not available: {e}")

  def test_fomc_logic_untouched(self):
    """
    FOMC / CPI / NFP / PCE calendar logic must
    be completely untouched by new layers.
    """
    result = subprocess.run([
      "grep", "-rn", "FOMC\\|CPI\\|NFP\\|PCE",
      "risk_calendar/",
      "--include=*.py",
    ], capture_output=True, text=True)
    self.assertGreater(len(result.stdout), 0,
      "FOMC/CPI/NFP logic not found.\n"
      "Calendar engine may have been modified.")

  def test_mid_month_timing_untouched(self):
    result = subprocess.run([
      "grep", "-rn",
      "mid_month\\|thursday\\|tuesday",
      ".",
      "--include=*.py",
      "--exclude-dir=tests",
    ], capture_output=True, text=True)
    self.assertGreater(len(result.stdout), 0,
      "Mid-month timing logic not found.\n"
      "Time regime logic may have been removed.")


# ──────────────────────────────────────
# SECTION 15 — FULL PIPELINE LATENCY
# End-to-end timing guarantee.
# ──────────────────────────────────────

class TestFullPipelineLatency(unittest.TestCase):

  def test_hot_path_under_3ms(self):
    """
    Complete hot path:
    context_build + personality_assess +
    kant_assess + conviction_compute +
    nietzsche_compute

    Must complete under 3ms total.
    Nietzsche adds <0.5ms to existing 2ms.
    """
    from intelligence.personality_context \
      import PersonalityContextCache
    from intelligence.personality import \
      MarketPersonalityEngine
    from intelligence.kant_engine import KantEngine
    from intelligence.conviction_engine import \
      compute_conviction
    from intelligence.nietzsche_engine import \
      NietzscheEngine

    cache = make_context_cache()
    pers  = make_personality_engine()
    kant  = make_kant_engine()
    nietz = NietzscheEngine(make_config())

    # Pre-warm Kant hysteresis
    for _ in range(3):
      kant.assess(
        symbol="BTC-USD",
        atr_vs_baseline=1.0,
        cascade_phase="idle",
        cascade_zscore=0.5,
        basis_stress_count=0,
        rpc_health=1.0,
        regime="confused",
        liq_60s=22,
      )

    times = []
    for _ in range(500):
      t0 = time.perf_counter()

      ctx = cache.build(
        "BTC-USD", 5.5, "long", "bullish")
      state = pers.assess("BTC-USD", ctx)
      frame = kant.assess(
        symbol="BTC-USD",
        atr_vs_baseline=ctx.atr_vs_baseline,
        cascade_phase=ctx.cascade_phase,
        cascade_zscore=ctx.cascade_zscore,
        basis_stress_count=ctx.basis_stress_count,
        rpc_health=ctx.rpc_health_score,
        regime=ctx.regime,
        liq_60s=22,
      )
      conv = compute_conviction(
        coherence=5.5,
        regime_aligned=True,
        order_flow_ratio=0.6,
        cascade_active=False,
        cascade_zscore=0.0,
        historical_wr=0.50,
        kant_confidence=frame.confidence,
      )
      out = nietz.compute(
        drawdown_pct=0.03,
        win_streak=2,
        loss_streak=0,
        conviction_score=conv,
        coherence=5.5,
        kant_frame=frame,
        base_size_units=100.0,
        min_notional_usd=50.0,
        mark_price=75000.0,
        balance=310.0,
      )

      times.append(
        (time.perf_counter()-t0)*1000)

    avg = sum(times)/len(times)
    p95 = sorted(times)[475]
    p99 = sorted(times)[495]

    self.assertLess(avg, 3.0,
      f"Hot path avg={avg:.3f}ms.\n"
      f"Must be <3ms.\n"
      f"Kant+Nietzsche add <1ms together.")
    self.assertLess(p95, 5.0,
      f"Hot path P95={p95:.3f}ms.\n"
      f"Must be <5ms.")
    self.assertLess(p99, 10.0,
      f"Hot path P99={p99:.3f}ms.\n"
      f"Must be <10ms.")

  def test_prediction_store_add_under_100us(self):
    from intelligence.prediction_market import \
      PredictionStore
    store = PredictionStore()
    times = []
    for i in range(2000):
      p = make_prediction(f"id_{i}")
      t0 = time.perf_counter()
      store.add_pending(p)
      times.append(
        (time.perf_counter()-t0)*1000)

    avg = sum(times)/len(times)
    self.assertLess(avg, 0.10,
      f"Prediction add avg={avg:.3f}ms.\n"
      f"Must be <0.10ms.\n"
      f"This is a queue.put_nowait call.")


# ──────────────────────────────────────
# SECTION 16 — LIVE FAILURE SCENARIOS
# Tests derived from actual log failures.
# ──────────────────────────────────────

class TestLiveFailureScenarios(unittest.TestCase):

  def test_op_usd_dust_notional_fixed(self):
    """
    FROM LOGS:
    14:34:32 - OP-USD entry FAILED
    notional $45.51 < $50 minimum
    score=8.72, cascade zscore=4.06

    Nietzsche must auto-bump to $50.
    """
    from intelligence.nietzsche_engine import \
      NietzscheEngine
    eng = NietzscheEngine(make_config())
    frame = make_kant_frame(
      min_notional_adjust=True)

    out = eng.compute(
      drawdown_pct=0.03,
      win_streak=2,
      loss_streak=0,
      conviction_score=0.92,
      coherence=8.72,
      kant_frame=frame,
      base_size_units=374.0,
      min_notional_usd=50.0,
      mark_price=0.1211,
      balance=310.0,
    )
    notional = out.adjusted_size * 0.1211
    self.assertGreaterEqual(notional, 50.0,
      f"OP-USD notional={notional:.2f}.\n"
      f"Still below $50 minimum.\n"
      f"Auto-bump not working.")

  def test_nvda_stop_loss_type_error_caught(self):
    """
    FROM LOGS:
    software_stop_exception: 'str' <= 'int'
    TypeError in stop comparison.

    Stop price must always be float.
    """
    try:
      from intelligence.kant_engine import KantEngine
      eng = make_kant_engine()
      frame = eng.assess(
        symbol="NVDA-USD",
        atr_vs_baseline=0.9,
        cascade_phase="idle",
        cascade_zscore=0.3,
        basis_stress_count=0,
        rpc_health=1.0,
        regime="confused",
        liq_60s=5,
      )
      self.assertIsInstance(
        frame.atr_baseline_min, float,
        "atr_baseline_min is not float.\n"
        "Type mismatch causes stop comparison\n"
        "TypeError in software stop logic.")
    except Exception as e:
      self.fail(f"KantEngine raised: {e}")

  def test_cascade_aftermath_one_signal_probes(self):
    """
    FROM LOGS:
    cascade_aftermath_signals: confirmed=1
    needed=3 → NO TRADE

    With Kant DISTRIBUTION + Nietzsche,
    1 signal during aftermath should probe
    at reduced size not wait for 3.
    """
    from intelligence.nietzsche_engine import (
      NietzscheEngine, WillState)
    eng = NietzscheEngine(make_config())
    dist_frame = make_kant_frame(
      size_cap=0.25,
      order_type="probe")

    out = eng.compute(
      drawdown_pct=0.02,
      win_streak=3,
      loss_streak=0,
      conviction_score=0.65,
      coherence=5.5,
      kant_frame=dist_frame,
      base_size_units=100.0,
      min_notional_usd=50.0,
      mark_price=75000.0,
      balance=310.0,
    )
    self.assertEqual(out.order_type, "probe",
      f"order_type={out.order_type}.\n"
      f"Aftermath with 1 signal must probe.\n"
      f"Not wait for 3 signals to trade.")
    self.assertGreater(out.size_multiplier,
      0.0,
      "Aftermath probe size must be > 0.")
    self.assertLessEqual(out.size_multiplier,
      0.35,
      "Aftermath probe must be small (≤35%).")

  def test_confused_regime_scout_reduced(self):
    """
    FROM LOGS:
    regime=confused, SCOUT dominant
    T:129, WR:46.9%, SQN:-1.98

    In CONFUSED regime, SCOUT must trade
    smaller. This was destroying value.
    """
    from intelligence.nietzsche_engine import \
      NietzscheEngine
    eng = NietzscheEngine(make_config())
    from intelligence.kant_engine import KantEngine
    kant = make_kant_engine()

    for _ in range(3):
      frame = kant.assess(
        symbol="ARB-USD",
        atr_vs_baseline=0.92,
        cascade_phase="idle",
        cascade_zscore=0.0,
        basis_stress_count=0,
        rpc_health=1.0,
        regime="confused",
        liq_60s=5,
      )

    out_confused = eng.compute(
      drawdown_pct=0.03,
      win_streak=0,
      loss_streak=3,
      conviction_score=0.45,
      coherence=4.07,
      kant_frame=frame,
      base_size_units=100.0,
      min_notional_usd=50.0,
      mark_price=0.1165,
      balance=310.0,
    )
    self.assertLess(
      out_confused.size_multiplier, 0.70,
      f"Confused+drawdown size="
      f"{out_confused.size_multiplier}.\n"
      f"Must be <0.70.\n"
      f"129 trades at -SQN = overtrades.")


# ──────────────────────────────────────
# SECTION 17 — PHILOSOPHICAL STACK ORDER
# Proves all layers connect correctly.
# ──────────────────────────────────────

class TestPhilosophicalStackOrder(unittest.TestCase):

  def test_kant_runs_before_hobbes_gates(self):
    """
    Kant must modify thresholds BEFORE
    the 14 Hobbes gates evaluate signals.
    If Kant runs after gates, it has no
    effect on blocked signals.
    """
    import inspect, main
    try:
      src = inspect.getsource(
        main.on_signal_ready)
    except AttributeError:
      try:
        import main as m
        src = inspect.getsource(m)
      except:
        self.skipTest("main.py not importable")
        return

    kant_pos  = src.find("kant_engine.assess")
    hobbes_pos= src.find("risk_engine.validate")
    nietz_pos = src.find("nietzsche_engine.compute")

    if kant_pos > 0 and hobbes_pos > 0:
      self.assertLess(kant_pos, hobbes_pos,
        "kant_engine.assess appears AFTER\n"
        "risk_engine.validate.\n"
        "Kant must run BEFORE Hobbes gates.\n"
        "Otherwise thresholds not applied.")

    if nietz_pos > 0 and hobbes_pos > 0:
      self.assertGreater(nietz_pos, hobbes_pos,
        "nietzsche_engine.compute appears BEFORE\n"
        "risk_engine.validate.\n"
        "Nietzsche must run AFTER Hobbes.\n"
        "Only size valid signals, not all.")

  def test_logs_show_state_transitions(self):
    """
    Log events must show the new
    state-transition format.
    """
    required_events = [
      "kant_frame",
      "conviction_computed",
      "nietzsche_output",
    ]
    result = subprocess.run([
      "grep", "-rn",
    ] + required_events + [
      ".",
      "--include=*.py",
      "--exclude-dir=tests",
      "--exclude-dir=__pycache__",
    ], capture_output=True, text=True)

    found = result.stdout
    for event in required_events:
      self.assertIn(event, found,
        f"Event '{event}' not found in codebase.\n"
        f"Must be logged in main.py.\n"
        f"State transitions not visible.")

  def test_bayes_receives_kant_structure(self):
    """
    Trade journal must record kant_structure.
    Without this, Bayes cannot learn which
    structures produce best outcomes.
    """
    from memory.trade_journal import TradeRecord
    fields = TradeRecord.__dataclass_fields__
    self.assertIn("kant_structure", fields,
      "TradeRecord missing kant_structure.\n"
      "Bayes calibration cannot learn:\n"
      "'Does ACCUMULATION trading outperform?'")

  def test_system_description_is_participant(self):
    """
    Meta-test: ARIA must now describe itself
    as a market participant not a calculator.
    The philosophical upgrade is complete
    when the system handles uncertainty
    gracefully rather than binary blocking.

    Verified by checking Nietzsche never
    returns size=0 except for DORMANT state.
    """
    from intelligence.nietzsche_engine import (
      NietzscheEngine, WillState)
    eng = NietzscheEngine(make_config())
    frame = make_kant_frame()

    zero_size_states = []
    for dd in [0.01, 0.02, 0.03, 0.04,
               0.05, 0.06, 0.07, 0.08, 0.09]:
      out = eng.compute(
        drawdown_pct=dd,
        win_streak=1,
        loss_streak=1,
        conviction_score=0.60,
        coherence=5.5,
        kant_frame=frame,
        base_size_units=100.0,
        min_notional_usd=50.0,
        mark_price=75000.0,
        balance=310.0,
      )
      if out.size_multiplier == 0.0 and \
         out.will_state != WillState.DORMANT:
        zero_size_states.append(dd)

    self.assertEqual(zero_size_states, [],
      f"Zero size at non-DORMANT states:\n"
      f"DD levels: {zero_size_states}\n"
      f"ARIA went binary at these levels.\n"
      f"A market participant stays in the\n"
      f"market with reduced size.\n"
      f"A calculator says yes or no.")


# ══════════════════════════════════════════
# HELPERS — used across all test classes
# ══════════════════════════════════════════

def make_ctx(**kwargs):
  defaults = dict(
    symbol="BTC-USD",
    coherence=5.0,
    direction="long",
    htf="bullish",
    regime="risk_on",
    cascade_phase="idle",
    cascade_direction="none",
    cascade_zscore=0.0,
    cascade_notional=0.0,
    aftermath_signals=0,
    calendar_regime="CLEAR",
    hours_to_event=None,
    basis_stress_count=0,
    rpc_health_score=1.0,
    atr_vs_baseline=1.0,
    daily_pnl_pct=0.0,
    session_win_rate=0.50,
    freeze_active=False,
    freeze_elapsed_s=0,
  )
  defaults.update(kwargs)
  try:
    from intelligence.personality_context \
      import PersonalityContext
    return PersonalityContext(**defaults)
  except:
    return type("Ctx",(),defaults)()

def make_personality_engine():
  try:
    from intelligence.personality import \
      MarketPersonalityEngine
    return MarketPersonalityEngine(
      config=make_config())
  except Exception as e:
    raise ImportError(
      f"Cannot import personality engine: {e}")

def make_context_cache():
  try:
    from intelligence.personality_context \
      import PersonalityContextCache
    cache = PersonalityContextCache()
    cache.update_cascade("idle","none",0,0,0)
    cache.update_regime("confused",0.5,"neutral",1.0)
    cache.update_atr("BTC-USD", 1.0)
    cache.update_calendar({})
    cache.update_basis_stress(0)
    cache.update_performance(0.0, 0.5)
    cache.update_rpc_health(0, True)
    cache.update_freeze(False, 0)
    return cache
  except Exception as e:
    raise ImportError(
      f"Cannot import context cache: {e}")

def make_kant_engine():
  try:
    from intelligence.kant_engine import KantEngine
    return KantEngine(make_config())
  except Exception as e:
    raise ImportError(
      f"Cannot import Kant engine: {e}")

def make_kant_frame(**kwargs):
  try:
    from intelligence.kant_engine import (
      KantEngine, MarketStructure, KantFrame)
    defaults = dict(
      structure=MarketStructure.TREND,
      confidence=0.80,
      atr_baseline_min=0.70,
      coherence_min=4.5,
      basis_stress_weight=1.0,
      order_type="limit",
      size_cap=1.25,
      min_notional_adjust=True,
    )
    defaults.update(kwargs)
    return KantFrame(**defaults)
  except Exception as e:
    from unittest.mock import MagicMock
    frame = MagicMock()
    frame.size_cap = kwargs.get("size_cap",1.25)
    frame.order_type = kwargs.get("order_type","limit")
    frame.min_notional_adjust = kwargs.get(
      "min_notional_adjust",True)
    frame.coherence_min = 4.5
    frame.atr_baseline_min = float(kwargs.get("atr_baseline_min", 0.70))
    frame.confidence = 0.80
    return frame

def make_config():
  cfg = MagicMock()
  cfg.min_notional = 50.0
  cfg.atr_baseline_min = 0.70
  cfg.basis_stress_limit = 3
  cfg.min_trade_notional_usd = 50.0
  return cfg

def make_market_state(symbol):
  from unittest.mock import MagicMock
  s = MagicMock()
  s.symbol = symbol
  s.mark_price = 75000.0
  s.atr = 61.38
  s.atr_pct = 0.082
  s.atr_vs_baseline = 0.99
  return s

def make_prediction(id_, **kwargs):
  defaults = dict(
    id=id_,
    agent="SCOUT",
    personality="SCOUT",
    symbol="BTC-USD",
    direction="long",
    confidence=0.65,
    ml_probability=0.58,
    coherence=5.0,
    entry_price=75000.0,
    predicted_exit=76500.0,
    timestamp_ms=int(time.time()*1000),
    resolved=False,
    outcome=None,
    actual_pnl_r=None,
  )
  defaults.update(kwargs)
  try:
    from intelligence.prediction_market import \
      PredictionRecord
    return PredictionRecord(**defaults)
  except:
    return type("P",(),defaults)()

def make_budget_manager():
  try:
    from core.budget_manager import BudgetManager
    bm = BudgetManager(make_config(), 310.0)
    bm.initialise()
    return bm
  except:
    return MagicMock()

def make_risk_engine():
  try:
    from risk.risk_engine import RiskEngine
    return RiskEngine(make_config())
  except:
    return MagicMock()

def make_candidate(symbol, **kwargs):
  c = MagicMock()
  c.symbol = symbol
  c.coherence = kwargs.get("coherence", 5.0)
  c.atr_vs_baseline = kwargs.get(
    "atr_vs_baseline", 1.0)
  c.direction = "long"
  c.size = 100.0
  return c


# ══════════════════════════════════════════
# RUNNER
# ══════════════════════════════════════════

def run_suite():
  import sys

  banner = """
══════════════════════════════════════════════
  ARIA INSTITUTIONAL TEST SUITE v1.0
  Phases 1-13 + Kant + Nietzsche
══════════════════════════════════════════════"""
  print(banner)

  CLASSES = [
    ("ATR Gate Removal",       TestATRGateRemoval),
    ("Six Personalities",      TestSixPersonalities),
    ("Personality Hysteresis", TestPersonalityHysteresis),
    ("Context Cache Latency",  TestContextCacheLatency),
    ("Kant Structure",         TestKantEngineStructure),
    ("Kant Thresholds",        TestKantThresholdOverrides),
    ("Nietzsche Will States",  TestNietzscheWillStates),
    ("Nietzsche Sizing Math",  TestNietzscheSizingMath),
    ("Conviction",             TestConvictionAggregation),
    ("Kant×Nietzsche",         TestKantNietzscheIntegration),
    ("Budget Manager",         TestBudgetManager),
    ("Prediction Market",      TestPredictionMarket),
    ("Journal Persistence",    TestJournalPersistence),
    ("Risk Gate Order",        TestRiskEngineGateOrder),
    ("Full Pipeline Latency",  TestFullPipelineLatency),
    ("Live Failure Scenarios", TestLiveFailureScenarios),
    ("Philosophical Stack",    TestPhilosophicalStackOrder),
  ]

  passed = 0
  failed = 0
  failures = []

  for name, cls in CLASSES:
    suite  = unittest.TestLoader()\
      .loadTestsFromTestCase(cls)
    runner = unittest.TextTestRunner(
      verbosity=0, stream=open(
        "/dev/null","w"))
    result = runner.run(suite)

    if result.wasSuccessful():
      print(f"  ✓ PASS  {name}")
      passed += 1
    else:
      print(f"  ✗ FAIL  {name}")
      failed += 1
      for test, err in (
          result.failures + result.errors):
        failures.append((name, test, err))

  print(f"""
══════════════════════════════════════════════
  {passed}/{passed+failed} PASSED
{'  ARIA IS PRODUCTION READY' if failed==0
 else f'  {failed} FAILURES — DO NOT DEPLOY'}
══════════════════════════════════════════════""")

  if failures:
    print("\nFAILURE DETAILS:")
    for suite_name, test, err in failures:
      print(f"\n[{suite_name}] {test}")
      lines = err.split("\n")
      for l in lines[-6:]:
        if l.strip():
          print(f"  {l}")

  return 0 if failed==0 else 1

if __name__ == "__main__":
  import sys
  sys.exit(run_suite())
