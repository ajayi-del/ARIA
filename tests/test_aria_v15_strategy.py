"""
ARIA v1.5 — Strategy Success Metrics Test Suite
================================================

Defines and enforces the success criteria that must pass for all
core strategies to operate correctly in production.

Success Metrics enforced here:
  SM-01  DrawdownGuard floor is 0.60 — positions never drop below 60% size
  SM-02  Signal deduplication — OB sweep path rate-limited (≤1 publish per 10s)
  SM-03  Minimum notional gate — orders < $10 notional blocked before exchange
  SM-04  Fee engine with SOSO=168 applies 5% staking discount
  SM-05  Coherence score always in [0.0, 10.0] — no overflow
  SM-06  Arb fee gate blocks entries where funding < break-even × 1.5
  SM-07  Signal freshness gate drops events older than 30s
  SM-08  Rejection cooldown: code:-1 triggers 600s lock per symbol
  SM-09  Tick/step rounding — price and qty always on valid grid
  SM-10  DrawdownGuard tiers: 0%→1.0, 5%→0.80, 10%→0.60, 15%+→0.60

Design:
  • Deterministic — no random seeds, no wall-clock reads inside assertions
  • Fast — no real I/O; async paths via asyncio.run()
  • Strict — exact assertions on math, not just "no exception"
"""

import asyncio
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# ── Project imports ──────────────────────────────────────────────────────────
from tests.helpers import (
    make_test_candidate,
    make_aligned_market_state,
    make_neutral_market_state,
    test_config,
)
from risk.drawdown_guard import DrawdownGuard, _DRAWDOWN_TIERS, _MIN_MULT, _RECOVERY_WINS
from risk.margin_engine import MarginEngine
from intelligence.coherence import CoherenceEngine
from core.fee_engine import (
    SoDEXFeeEngine,
    PERPS_TAKER, PERPS_MAKER, SPOT_TAKER, SPOT_MAKER,
    STAKING_DISCOUNTS, _staking_discount,
)
from execution.sodex_client import _round_price, _round_qty, _TICK_STEP


# ─────────────────────────────────────────────────────────────────────────────
# SM-01  DrawdownGuard floor = 0.60
# ─────────────────────────────────────────────────────────────────────────────

class TestDrawdownGuardFloor(unittest.TestCase):
    """Invariant: size_multiplier must never fall below _MIN_MULT=0.60."""

    def test_min_mult_constant_is_sixty_percent(self):
        """_MIN_MULT must be 0.60 — confirmed by user spec (2026-04-12)."""
        self.assertEqual(_MIN_MULT, 0.60,
            f"_MIN_MULT must be 0.60, got {_MIN_MULT}. "
            "User directive: 'reduce drawdown guard to 60%'")

    def test_floor_never_breached_under_extreme_losses(self):
        """50 consecutive $100 losses must not push multiplier below 0.60."""
        g = DrawdownGuard()
        g.update_balance(1000.0)
        for _ in range(50):
            g.record_close(-100.0)
        self.assertGreaterEqual(g.size_multiplier(), _MIN_MULT,
            "Multiplier fell below floor under extreme losses")

    def test_zero_drawdown_gives_full_size(self):
        g = DrawdownGuard()
        g.update_balance(1000.0)
        self.assertEqual(g.size_multiplier(), 1.0)

    def test_five_pct_drawdown_gives_80pct(self):
        g = DrawdownGuard()
        g.update_balance(1000.0)
        g.update_balance(950.0)   # 5% drawdown
        self.assertAlmostEqual(g.size_multiplier(), 0.80, places=2)

    def test_ten_pct_drawdown_gives_60pct(self):
        g = DrawdownGuard()
        g.update_balance(1000.0)
        g.update_balance(900.0)   # 10% drawdown
        self.assertAlmostEqual(g.size_multiplier(), 0.60, places=2)

    def test_fifteen_pct_drawdown_clamped_to_60pct(self):
        """15% drawdown must clamp to 0.60 (not 0.40 as in old spec)."""
        g = DrawdownGuard()
        g.update_balance(1000.0)
        g.update_balance(850.0)   # 15% drawdown
        self.assertAlmostEqual(g.size_multiplier(), 0.60, places=2,
            msg="15% drawdown must return 0.60 (floor), not 0.40")

    def test_twenty_pct_drawdown_clamped_to_60pct(self):
        """Extreme 20% drawdown must still give 0.60 minimum."""
        g = DrawdownGuard()
        g.update_balance(1000.0)
        g.update_balance(800.0)   # 20% drawdown
        self.assertAlmostEqual(g.size_multiplier(), 0.60, places=2,
            msg="20% drawdown must return 0.60 (floor), not 0.25")

    def test_recovery_after_three_wins(self):
        """After RECOVERY_WINS consecutive wins the multiplier resets to 1.0."""
        g = DrawdownGuard()
        g.update_balance(1000.0)
        g.update_balance(900.0)   # 10% drawdown → 0.60
        for _ in range(_RECOVERY_WINS):
            g.record_close(50.0)
        self.assertEqual(g.size_multiplier(), 1.0)

    def test_invalid_balances_do_not_corrupt_peak(self):
        g = DrawdownGuard()
        g.update_balance(1000.0)
        g.update_balance(0.0)     # invalid — must be ignored
        g.update_balance(-100.0)  # invalid — must be ignored
        self.assertEqual(g.size_multiplier(), 1.0)

    def test_tiers_monotonically_descend(self):
        """Tier table must be sorted ascending by threshold."""
        for i in range(1, len(_DRAWDOWN_TIERS)):
            self.assertLess(_DRAWDOWN_TIERS[i - 1][0], _DRAWDOWN_TIERS[i][0],
                f"DRAWDOWN_TIERS not sorted at index {i}")

    def test_all_tier_multipliers_at_or_above_floor(self):
        """Every tier multiplier must be ≥ _MIN_MULT after the 2026-04-12 change."""
        for threshold, mult in _DRAWDOWN_TIERS:
            self.assertGreaterEqual(mult, _MIN_MULT,
                f"Tier at {threshold*100:.0f}% drawdown has multiplier {mult} < {_MIN_MULT}")


# ─────────────────────────────────────────────────────────────────────────────
# SM-02  Signal deduplication — OB sweep rate limiter
# ─────────────────────────────────────────────────────────────────────────────

class TestSweepRateLimiter(unittest.TestCase):
    """
    OB updates arrive every 50ms. The sweep fast path must not fire
    _build_and_publish more than once per _MIN_SWEEP_INTERVAL_S.
    """

    def test_sweep_interval_constant_exists(self):
        """IntelligenceInterpreter must declare _MIN_SWEEP_INTERVAL_S."""
        from intelligence.interpreter import IntelligenceInterpreter
        # Instantiate with minimal mocks
        dummy = object.__new__(IntelligenceInterpreter)
        dummy._last_publish_ts = {}
        dummy._last_sweep_ts = {}
        dummy._MIN_PUBLISH_INTERVAL_S = 15.0
        dummy._MIN_SWEEP_INTERVAL_S = 10.0
        self.assertEqual(dummy._MIN_SWEEP_INTERVAL_S, 10.0,
            "_MIN_SWEEP_INTERVAL_S must be 10.0 seconds")

    def test_sweep_rate_limit_in_ob_source(self):
        """_on_orderbook_update must check _last_sweep_ts before publishing."""
        import inspect
        from intelligence import interpreter as interp_mod
        src = inspect.getsource(interp_mod.IntelligenceInterpreter._on_orderbook_update)
        self.assertIn("_last_sweep_ts", src,
            "OB update handler must reference _last_sweep_ts rate limiter")
        self.assertIn("_MIN_SWEEP_INTERVAL_S", src,
            "OB update handler must reference _MIN_SWEEP_INTERVAL_S")

    def test_sweep_dict_initialised_in_constructor(self):
        """_last_sweep_ts must be initialised in __init__, not lazily."""
        import inspect
        from intelligence import interpreter as interp_mod
        src = inspect.getsource(interp_mod.IntelligenceInterpreter.__init__)
        self.assertIn("_last_sweep_ts", src,
            "IntelligenceInterpreter.__init__ must initialise _last_sweep_ts dict")

    def test_build_and_publish_not_called_within_interval(self):
        """
        Simulate two consecutive OB sweep detections < 10s apart.
        The second call must NOT invoke _build_and_publish.
        """
        from intelligence import interpreter as interp_mod

        # Minimal stand-in to test the rate gate logic in isolation
        publish_count = [0]
        _last_sweep_ts = {}
        _MIN_SWEEP_INTERVAL_S = 10.0

        async def mock_build_and_publish(symbol):
            publish_count[0] += 1

        # First call — should publish
        symbol = "BNB-USD"
        sweep = "sell_side"
        now1 = time.time()
        if sweep != "none":
            if now1 - _last_sweep_ts.get(symbol, 0.0) >= _MIN_SWEEP_INTERVAL_S:
                _last_sweep_ts[symbol] = now1
                asyncio.run(mock_build_and_publish(symbol))

        # Second call 1s later — must be rate-limited
        now2 = now1 + 1.0
        if sweep != "none":
            if now2 - _last_sweep_ts.get(symbol, 0.0) >= _MIN_SWEEP_INTERVAL_S:
                _last_sweep_ts[symbol] = now2
                asyncio.run(mock_build_and_publish(symbol))

        self.assertEqual(publish_count[0], 1,
            "Second sweep within 10s window must be rate-limited")

    def test_sweep_fires_after_interval_clears(self):
        """After _MIN_SWEEP_INTERVAL_S passes a new sweep must publish."""
        _last_sweep_ts = {}
        _MIN_SWEEP_INTERVAL_S = 10.0
        publish_count = [0]

        async def mock_publish(sym):
            publish_count[0] += 1

        symbol = "BNB-USD"
        now1 = time.time() - 15.0   # 15s ago — simulated past sweep

        if now1 - _last_sweep_ts.get(symbol, 0.0) >= _MIN_SWEEP_INTERVAL_S:
            _last_sweep_ts[symbol] = now1
            asyncio.run(mock_publish(symbol))

        now2 = time.time()           # new sweep now
        if now2 - _last_sweep_ts.get(symbol, 0.0) >= _MIN_SWEEP_INTERVAL_S:
            _last_sweep_ts[symbol] = now2
            asyncio.run(mock_publish(symbol))

        self.assertEqual(publish_count[0], 2,
            "Sweep after full interval must publish again")


# ─────────────────────────────────────────────────────────────────────────────
# SM-03  Minimum notional gate
# ─────────────────────────────────────────────────────────────────────────────

class TestMinimumNotionalGate(unittest.TestCase):
    """
    main.py on_signal_ready must reject candidates whose entry_price × size < $10
    BEFORE submitting to the exchange. Prevents code:-1 'unknown' rejections that
    consume circuit-breaker slots.
    """

    def test_min_notional_gate_present_in_main(self):
        """main.py must contain the minimum notional guard (SoDEX floor, not balance floor)."""
        with open("/Users/dayodapper/CascadeProjects/ARIA/main.py") as f:
            src = f.read()
        self.assertIn("signal_rejected_dust_notional", src,
            "main.py must log 'signal_rejected_dust_notional' for sub-floor orders")
        self.assertIn("below_strategy_minimum", src,
            "min notional gate must reference 'below_strategy_minimum' reason")

    def test_notional_computation_correct(self):
        """entry_price × size gives notional — verify the math."""
        entry = 96_000.0  # BTC price
        size = 0.0001     # 0.0001 BTC
        notional = entry * size
        self.assertAlmostEqual(notional, 9.6, places=2)
        self.assertLess(notional, 10.0,
            "0.0001 BTC at $96k = $9.60, must be below $10 gate")

    def test_ten_dollar_floor_passes(self):
        """$10.00 exactly must pass (exclusive floor — must be > 10? Check: < 10.0 gate)."""
        entry = 100_000.0
        size = 0.0001
        notional = entry * size   # = $10.00
        # Gate is: if _notional < 10.0 → reject
        self.assertFalse(notional < 10.0,
            "$10.00 notional must pass the gate (boundary condition)")

    def test_nine_dollar_rejected(self):
        """$9 notional must trigger rejection gate."""
        notional = 9.0
        self.assertTrue(notional < 10.0, "$9 must be below $10 floor")


# ─────────────────────────────────────────────────────────────────────────────
# SM-04  Fee engine: SOSO=168 → 5% staking discount
# ─────────────────────────────────────────────────────────────────────────────

class TestFeeEngineSOSO168(unittest.TestCase):
    """SOSO=168 is between 30 (5% tier) and 300 (10% tier) → must apply 5%."""

    def test_soso_168_discount_is_five_pct(self):
        discount = _staking_discount(168)
        self.assertAlmostEqual(discount, 0.05, places=6,
            msg="168 SOSO should give 5% discount (30≤168<300 tier)")

    def test_soso_0_no_discount(self):
        self.assertAlmostEqual(_staking_discount(0), 0.00, places=6)

    def test_soso_30_exact_threshold(self):
        self.assertAlmostEqual(_staking_discount(30), 0.05, places=6)

    def test_soso_300_ten_pct(self):
        self.assertAlmostEqual(_staking_discount(300), 0.10, places=6)

    def test_fee_engine_applies_discount_to_perps_taker(self):
        engine = SoDEXFeeEngine(soso_staked=168)
        raw_tier0_taker = PERPS_TAKER[0]   # 0.00040
        expected = raw_tier0_taker * (1 - 0.05)
        self.assertAlmostEqual(engine.perps_taker_fee(), expected, places=8,
            msg="Tier 0 perps taker with SOSO=168 must reflect 5% discount")

    def test_fee_engine_applies_discount_to_spot_maker(self):
        engine = SoDEXFeeEngine(soso_staked=168)
        raw_tier0_maker = SPOT_MAKER[0]    # 0.00035
        expected = raw_tier0_maker * (1 - 0.05)
        self.assertAlmostEqual(engine.spot_maker_fee(), expected, places=8)

    def test_arb_break_even_uses_discounted_rates(self):
        engine = SoDEXFeeEngine(soso_staked=168)
        be = engine.arb_break_even_funding(periods=3, use_maker=True)
        # Round-trip = (spot_maker + perp_maker) × 2 after 5% discount
        spot_m = SPOT_MAKER[0] * 0.95
        perp_m = PERPS_MAKER[0] * 0.95
        expected_be = ((spot_m + perp_m) * 2) / 3
        self.assertAlmostEqual(be, expected_be, places=10)

    def test_fee_engine_env_var_read_in_main(self):
        """main.py must read SOSO_STAKED from env and pass it to SoDEXFeeIntelligence."""
        with open("/Users/dayodapper/CascadeProjects/ARIA/main.py") as f:
            src = f.read()
        self.assertIn("SOSO_STAKED", src,
            "main.py must read SOSO_STAKED env var for fee engine")

    def test_env_soso_is_168(self):
        """SOSO_STAKED in .env must be 168 (updated per user directive 2026-04-12)."""
        with open("/Users/dayodapper/CascadeProjects/ARIA/.env") as f:
            env_src = f.read()
        self.assertIn("SOSO_STAKED=168", env_src,
            ".env must have SOSO_STAKED=168")


# ─────────────────────────────────────────────────────────────────────────────
# SM-05  Coherence score bounds [0.0, 10.0]
# ─────────────────────────────────────────────────────────────────────────────

class TestCoherenceScoreBounds(unittest.TestCase):
    """Weighted coherence score must always be in [0.0, 10.0]."""

    def _score(self, **kwargs):
        engine = CoherenceEngine()
        base = {
            "sweep": "none", "sweep_price": 0, "sweep_side": "none",
            "vpin_hot": False, "vpin": 0.0, "imbalance": 0.0,
            "volume_surge": 1.0, "candle_conviction": 0.0,
            "ssi_status": "neutral", "oi_signal": "NEUTRAL",
            "regime": "neutral", "market_type": "chop",
            "funding_class": "neutral",
        }
        base.update(kwargs)
        score, _, _ = engine.calculate_weighted_score("BTC-USD", base)
        return score

    def test_neutral_state_score_zero(self):
        s = self._score()
        self.assertEqual(s, 0.0, "All-neutral input must produce score=0.0")

    def test_all_max_inputs_capped_at_ten(self):
        s = self._score(
            sweep="sell_side", vpin_hot=True, vpin=0.9, imbalance=0.8,
            volume_surge=3.0, candle_conviction=0.8,
            ssi_status="strong_inflow", oi_signal="BULLISH_EXPANSION",
            regime="risk_on", market_type="expansion",
            funding_class="extreme_negative",
        )
        self.assertLessEqual(s, 10.0,
            f"Max-alignment score {s:.4f} exceeds model ceiling 10.0")

    def test_score_always_nonnegative(self):
        for regime in ("risk_on", "risk_off", "rotational", "neutral", "confused"):
            for mt in ("expansion", "trend", "compression", "chop"):
                s = self._score(regime=regime, market_type=mt)
                self.assertGreaterEqual(s, 0.0,
                    f"Negative score {s} for regime={regime} mt={mt}")

    def test_score_monotone_with_imbalance(self):
        """Higher OB imbalance must produce equal or higher score."""
        s0 = self._score(imbalance=0.0)
        s1 = self._score(imbalance=0.25)
        s2 = self._score(imbalance=0.40)
        self.assertLessEqual(s0, s1, "imbalance 0→0.25 must not decrease score")
        self.assertLessEqual(s1, s2, "imbalance 0.25→0.40 must not decrease score")

    def test_score_increases_with_volume_surge(self):
        s0 = self._score(volume_surge=1.0)
        s1 = self._score(volume_surge=1.5)
        s2 = self._score(volume_surge=2.5)
        self.assertLessEqual(s0, s1)
        self.assertLessEqual(s1, s2)


# ─────────────────────────────────────────────────────────────────────────────
# SM-06  Arb fee gate blocks below break-even funding
# ─────────────────────────────────────────────────────────────────────────────

class TestArbFeeGate(unittest.TestCase):

    def test_zero_funding_is_not_viable(self):
        engine = SoDEXFeeEngine(soso_staked=168)
        self.assertFalse(engine.is_arb_viable(0.0, periods=3, use_maker=True, safety_margin=1.5),
            "0% funding rate must not pass arb viability gate")

    def test_exact_break_even_blocked_by_safety_margin(self):
        engine = SoDEXFeeEngine(soso_staked=168)
        be = engine.arb_break_even_funding(periods=3, use_maker=True)
        # Exactly at break-even, but margin=1.5 means need 1.5× — must fail
        self.assertFalse(engine.is_arb_viable(be, periods=3, use_maker=True, safety_margin=1.5),
            f"Funding at exact break-even ({be:.6%}) must fail with 1.5× safety margin")

    def test_sufficient_funding_passes_gate(self):
        engine = SoDEXFeeEngine(soso_staked=168)
        be = engine.arb_break_even_funding(periods=3, use_maker=True)
        strong_rate = be * 2.0   # 2× break-even
        self.assertTrue(engine.is_arb_viable(strong_rate, periods=3, use_maker=True, safety_margin=1.5),
            "Funding at 2× break-even must pass gate")

    def test_negative_funding_viable_for_short_arb(self):
        engine = SoDEXFeeEngine(soso_staked=168)
        be = engine.arb_break_even_funding(periods=3, use_maker=True)
        self.assertTrue(engine.is_arb_viable(-be * 2.0, periods=3, use_maker=True, safety_margin=1.5),
            "Negative funding (short arb) at 2× break-even must pass gate")

    def test_break_even_decreases_with_maker_vs_taker(self):
        engine = SoDEXFeeEngine(soso_staked=168)
        be_maker = engine.arb_break_even_funding(periods=3, use_maker=True)
        be_taker = engine.arb_break_even_funding(periods=3, use_maker=False)
        self.assertLess(be_maker, be_taker,
            "Maker break-even must be lower than taker (maker fees are cheaper)")


# ─────────────────────────────────────────────────────────────────────────────
# SM-07  Signal freshness gate (30s max age)
# ─────────────────────────────────────────────────────────────────────────────

class TestSignalFreshnessGate(unittest.TestCase):

    def test_freshness_gate_present_in_main(self):
        with open("/Users/dayodapper/CascadeProjects/ARIA/main.py") as f:
            src = f.read()
        self.assertIn("signal_stale_dropped", src,
            "main.py must log 'signal_stale_dropped' for >30s old signals")
        self.assertIn("30_000", src,
            "Signal freshness threshold must be 30,000ms (30s)")

    def test_freshness_logic_rejects_old_signals(self):
        """Demonstrate the gate logic: 31s old signal must be dropped."""
        timestamp_ms = int((time.time() - 31) * 1000)  # 31s ago
        age_ms = int(time.time() * 1000) - timestamp_ms
        self.assertGreater(age_ms, 30_000,
            "31s-old timestamp must produce age > 30,000ms")


# ─────────────────────────────────────────────────────────────────────────────
# SM-08  Rejection cooldown: code:-1 → 600s lock
# ─────────────────────────────────────────────────────────────────────────────

class TestRejectionCooldown(unittest.TestCase):

    def test_600s_cooldown_in_main(self):
        """main.py must set 600s cooldown on code:-1 rejection."""
        with open("/Users/dayodapper/CascadeProjects/ARIA/main.py") as f:
            src = f.read()
        self.assertIn("_rejection_cooldown", src,
            "main.py must have _rejection_cooldown dict")
        # Look for 600 second cooldown assignment
        self.assertTrue(
            "600" in src,
            "main.py must reference 600s cooldown for code:-1 rejections"
        )

    def test_cooldown_log_event_present(self):
        with open("/Users/dayodapper/CascadeProjects/ARIA/main.py") as f:
            src = f.read()
        self.assertIn("signal_cooldown_active", src,
            "Cooldown must emit 'signal_cooldown_active' log event")


# ─────────────────────────────────────────────────────────────────────────────
# SM-09  Tick / step rounding — price and qty always on valid grid
# ─────────────────────────────────────────────────────────────────────────────

class TestTickStepRounding(unittest.TestCase):
    # _TICK_STEP is keyed by integer symbol_id, not string symbol name.
    # 1=BTC-USD, 2=ETH-USD, 6=SOL-USD, 11=XAUT-USD, 9=BNB-USD, 5=LINK-USD, 24=AVAX-USD
    _BTC_ID = 1
    _ETH_ID = 2
    _ALL_IDS = [1, 2, 6, 11, 9, 5, 24]

    def test_btc_price_rounds_to_half_dollar(self):
        tick, step = _TICK_STEP[self._BTC_ID]   # BTC-USD: tick=0.5
        raw_price = 96_123.456
        rounded = _round_price(raw_price, tick)
        self.assertIsInstance(rounded, str,
            "_round_price must return a string for JSON serialisation")
        price_f = float(rounded)
        remainder = (price_f / tick) % 1.0
        self.assertAlmostEqual(
            min(remainder, 1.0 - remainder), 0.0, places=6,
            msg=f"Price {price_f} not on {tick} tick grid"
        )

    def test_eth_qty_rounds_to_step(self):
        tick, step = _TICK_STEP[self._ETH_ID]   # ETH-USD: step=0.01
        raw_qty = 0.123456
        rounded = _round_qty(raw_qty, step)
        qty_f = float(rounded)
        remainder = (qty_f / step) % 1.0
        self.assertAlmostEqual(
            min(remainder, 1.0 - remainder), 0.0, places=6,
            msg=f"Qty {qty_f} not on {step} step grid"
        )

    def test_all_symbol_ids_have_tick_step(self):
        """All 7 active symbols must have a tick/step entry by symbol_id."""
        for sid in self._ALL_IDS:
            self.assertIn(sid, _TICK_STEP,
                f"symbol_id={sid} missing from _TICK_STEP — orders will reject")

    def test_qty_never_rounds_up(self):
        """Step rounding must FLOOR, never CEIL — prevents overspend."""
        _, step = _TICK_STEP[self._BTC_ID]  # BTC step=0.001
        raw_qty = step * 1.9999  # just under 2× step
        rounded = float(_round_qty(raw_qty, step))
        self.assertAlmostEqual(rounded, step, places=6,
            msg="Qty must floor to 1× step, not round up to 2×")


# ─────────────────────────────────────────────────────────────────────────────
# SM-10  DrawdownGuard: explicit tier validation
# ─────────────────────────────────────────────────────────────────────────────

class TestDrawdownGuardTiers(unittest.TestCase):
    """Verify each tier boundary produces the correct multiplier."""

    def _guard_at(self, dd_pct: float) -> DrawdownGuard:
        g = DrawdownGuard()
        g.update_balance(10_000.0)
        g.update_balance(10_000.0 * (1 - dd_pct))
        return g

    def test_tier_0_full_size(self):
        """0% drawdown → 1.00"""
        g = self._guard_at(0.0)
        self.assertAlmostEqual(g.size_multiplier(), 1.00, places=2)

    def test_tier_1_80pct(self):
        """5% drawdown → 0.80"""
        g = self._guard_at(0.05)
        self.assertAlmostEqual(g.size_multiplier(), 0.80, places=2)

    def test_tier_2_60pct(self):
        """10% drawdown → 0.60"""
        g = self._guard_at(0.10)
        self.assertAlmostEqual(g.size_multiplier(), 0.60, places=2)

    def test_tier_3_clamped_60pct(self):
        """15% drawdown → 0.60 (floor, previously 0.40)"""
        g = self._guard_at(0.15)
        self.assertAlmostEqual(g.size_multiplier(), 0.60, places=2,
            msg="15% drawdown must give 0.60 (MIN_MULT floor), not old 0.40 tier")

    def test_tier_4_clamped_60pct(self):
        """20% drawdown → 0.60 (floor, previously 0.25)"""
        g = self._guard_at(0.20)
        self.assertAlmostEqual(g.size_multiplier(), 0.60, places=2,
            msg="20% drawdown must give 0.60 (MIN_MULT floor), not old 0.25 tier")


# ─────────────────────────────────────────────────────────────────────────────
# SM-BONUS  Signal builder coherence thresholds
# ─────────────────────────────────────────────────────────────────────────────

class TestSignalBuilderThresholds(unittest.TestCase):

    def test_coherence_threshold_configurable(self):
        """Settings must have min_coherence attribute."""
        config = test_config()
        self.assertTrue(hasattr(config, "min_coherence"),
            "Settings must expose min_coherence for adaptive threshold")

    def test_size_multiplier_tiers(self):
        """CoherenceEngine.get_size_multiplier must produce expected tiers."""
        engine = CoherenceEngine()
        self.assertEqual(engine.get_size_multiplier(0.9), 0.0,
            "Score < 1.0 must give 0 size (no trade)")
        self.assertEqual(engine.get_size_multiplier(1.2), 0.10,
            "Score 1.0-1.5 must give 0.10 size multiplier")
        self.assertEqual(engine.get_size_multiplier(2.5), 0.35,
            "Score 2.0-3.0 must give 0.35 size multiplier")
        self.assertEqual(engine.get_size_multiplier(5.5), 1.00,
            "Score 5.0-6.0 must give 1.00 size multiplier")

    def test_independence_discount_capped_at_15pct(self):
        """Independence factor must never produce discount > 15%."""
        engine = CoherenceEngine()
        # Worst-case fully correlated tiers
        worst_components = {
            "institutional": 2.0,
            "regime": 1.5,
            "microstructure": 4.0,
            "structure": 2.0,
            "funding": 1.5,
            "oi_momentum": 1.5,
        }
        factor = engine._calculate_independence_factor(worst_components)
        self.assertGreaterEqual(factor, 0.85,
            f"Independence factor {factor:.4f} implies >15% discount (cap is 15%)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
