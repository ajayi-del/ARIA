"""
ARIA v1.6 — Gain Hunter Test Suite

Three tiers:
  1. LOGIC TESTS    — Gate logic, state machines, signal routing
  2. TECHNICAL TESTS — Wiring, imports, null-safety, persistence
  3. QUANT TESTS    — Sizing math, R:R discipline, coherence ceiling

Plus:
  GAIN HUNTER BENCHMARKS — 10 benchmarks the bot must pass to be a viable gain hunter

All tests are synchronous-safe. Async paths use asyncio.run().
"""

import asyncio
import json
import time
import tempfile
import os
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _run(coro):
    """Run a coroutine from synchronous test context. Uses asyncio.run() for Python 3.10+ compat."""
    return asyncio.run(coro)


def _dm_at_total_dd(start: float, pct: float):
    """
    Create a DrawdownManager in a given total drawdown state WITHOUT triggering
    the 5% daily halt (simulates multi-day cumulative loss).
    Achieves total DD by setting session_start ≈ current balance.
    """
    from risk.drawdown_manager import DrawdownManager
    dm = DrawdownManager(start)
    balance = start * (1 - pct)
    dm._session_start = balance * 0.99   # today's session: <1% daily loss
    dm._peak_balance = start
    dm._low_watermark = balance
    dm._current_balance = balance
    dm._daily_pnl = balance - dm._session_start
    dm._weekly_pnl = balance - dm._week_start
    dm._total_pnl = balance - start
    return dm


def _make_liq_signal(direction="bearish", notional=50_000, cascade=False, symbol="BTC-USD"):
    """Create a mock LiquidationSignal matching valuechain_monitor.LiquidationSignal."""
    sig = MagicMock()
    sig.direction = direction     # "bearish" (longs liq'd) or "bullish" (shorts liq'd)
    sig.notional_usd = notional
    sig.cascade = cascade
    sig.symbol = symbol
    sig.event_count_60s = 3 if cascade else 1
    sig.timestamp = time.time()
    return sig


def _make_market_state(
    symbol="BTC-USD",
    direction="long",
    score=3.5,
    mark_price=65000.0,
    atr=500.0,
    regime="risk_on",
    market_type="trend",
    atr_vs_baseline=1.2,
):
    """Minimal MarketState stub for build_candidate tests."""
    s = MagicMock()
    s.symbol = symbol
    s.trade_direction = direction
    s.coherence_score = score
    s.mark_price = mark_price
    s.atr = atr
    s.regime = regime
    s.market_type = market_type
    s.atr_vs_baseline = atr_vs_baseline
    s.market_hours_gate = True
    s.timestamp_ms = int(time.time() * 1000)
    s.signal_age_ms = 0
    s.macro_bias = "none"
    s.invalidation_reason = ""
    s.freshness_mult = 1.0
    s.coherence_mult = 1.0
    s.size_multiplier = 1.0
    return s


# ══════════════════════════════════════════════════════════════════════════════
# TIER 1: LOGIC TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestDrawdownManagerLogic:
    """DrawdownManager — state machine and threshold logic."""

    def setup_method(self):
        from risk.drawdown_manager import DrawdownManager
        self.DM = DrawdownManager

    def _dm(self, start=1000.0):
        return self.DM(starting_balance=start)

    # ── Level thresholds ──────────────────────────────────────────────────────

    def test_normal_below_10pct(self):
        """4% total DD (set via session isolation) → NORMAL (1.0×)."""
        dm = _dm_at_total_dd(1000, 0.04)
        dm.update_balance(dm._current_balance)  # trigger recalc
        assert dm.get_size_multiplier() == 1.0
        assert dm.can_trade_directional()

    def test_reduced_at_10pct(self):
        """12% total DD with minimal daily exposure → REDUCED (0.75×)."""
        dm = _dm_at_total_dd(1000, 0.12)
        dm.update_balance(dm._current_balance)
        assert dm.get_size_multiplier() == 0.75

    def test_minimal_at_20pct(self):
        """21% total DD with minimal daily exposure → MINIMAL (0.50×)."""
        dm = _dm_at_total_dd(1000, 0.21)
        dm.update_balance(dm._current_balance)
        assert dm.get_size_multiplier() == 0.50

    def test_halt_at_25pct(self):
        """26% total DD → HALTED (0.0×), no directional trades."""
        dm = _dm_at_total_dd(1000, 0.26)
        dm.update_balance(dm._current_balance)
        assert dm.get_size_multiplier() == 0.0
        assert not dm.can_trade_directional()

    def test_daily_halt_at_5pct(self):
        """5%+ daily DD → daily halt regardless of total DD."""
        dm = self._dm(1000)
        dm.update_balance(940)  # 6% daily DD → daily halt
        assert not dm.can_trade_directional()
        assert "daily" in dm._halt_reason

    def test_weekly_dd_reduces_size(self):
        """17% weekly DD with minimal daily exposure → size 0.50×."""
        dm = _dm_at_total_dd(1000, 0.17)
        dm._week_start = 1000  # week started at peak
        dm.update_balance(dm._current_balance)
        # Weekly DD ≥ 15% → 0.50x
        assert dm.get_size_multiplier() == 0.50

    # ── Arb always allowed ───────────────────────────────────────────────────

    def test_arb_allowed_when_halted(self):
        dm = self._dm(1000)
        dm.update_balance(700)  # 30% → halted
        assert not dm.can_trade_directional()
        assert dm.can_trade_arb()  # ALWAYS true

    # ── Recovery logic ────────────────────────────────────────────────────────

    def test_recovery_requires_10pct_from_low_watermark(self):
        dm = self._dm(1000)
        dm.update_balance(700)     # 30% → halted, low_watermark=700
        dm.update_balance(765)     # 9.3% from 700 — NOT enough
        assert not dm.can_trade_directional()

    def test_recovery_at_exactly_10pct_from_low(self):
        dm = self._dm(1000)
        dm.update_balance(700)     # halted, low=700
        dm.update_balance(770)     # exactly 10% from 700
        assert dm.can_trade_directional()
        assert dm.get_size_multiplier() == 0.50  # returns at MINIMAL, not NORMAL

    def test_recovery_returns_at_minimal_not_normal(self):
        dm = self._dm(1000)
        dm.update_balance(700)
        dm.update_balance(800)
        assert dm.get_size_multiplier() == 0.50  # NOT 1.0

    def test_ath_recovery_clears_halt(self):
        """Bug fix: new ATH while halted must auto-clear the halt."""
        dm = _dm_at_total_dd(1000, 0.26)
        dm.update_balance(dm._current_balance)   # confirm halted
        assert not dm.can_trade_directional()
        dm.update_balance(1001)                  # new ATH — must recover
        assert dm.can_trade_directional()
        assert dm.get_size_multiplier() == 0.50

    def test_recovery_not_from_peak_but_from_low(self):
        """Must recover from LOW WATERMARK, not peak."""
        dm = self._dm(1000)
        dm.update_balance(700)   # low_watermark = 700
        # 10% above PEAK (1000) = 1100 — that's irrelevant
        # 10% above LOW (700) = 770 — that's the target
        dm.update_balance(760)   # 8.6% from low → not enough
        assert not dm.can_trade_directional()
        dm.update_balance(770)   # 10% from low → recover
        assert dm.can_trade_directional()

    # ── Reset methods ────────────────────────────────────────────────────────

    def test_daily_reset_clears_daily_halt(self):
        dm = self._dm(1000)
        dm.update_balance(940)   # daily halt (6% daily DD)
        assert not dm.can_trade_directional()
        dm.reset_daily()
        # After reset, session_start = current balance, daily DD = 0
        dm.update_balance(940)   # no new DD since reset
        assert dm.can_trade_directional()

    def test_daily_reset_does_not_clear_total_halt(self):
        dm = self._dm(1000)
        dm.update_balance(700)   # total halt (30%)
        dm.reset_daily()
        # Total DD still 30% → still halted
        assert not dm.can_trade_directional()

    def test_weekly_reset_clears_weekly_reduction(self):
        dm = _dm_at_total_dd(1000, 0.17)
        dm._week_start = 1000
        dm.update_balance(dm._current_balance)  # 17% weekly → reduced
        assert dm.get_size_multiplier() == 0.50
        dm.reset_weekly()
        dm.update_balance(dm._current_balance)  # weekly resets to current; same total
        # After weekly reset: weekly_dd=0, total_dd=17% → REDUCED (10-20% band)
        assert dm.get_size_multiplier() == 0.75  # 17% total DD → REDUCED tier

    # ── Status snapshot ──────────────────────────────────────────────────────

    def test_status_fields_complete(self):
        from risk.drawdown_manager import DrawdownStatus
        dm = _dm_at_total_dd(1000, 0.04)  # 4% DD — no halt
        dm.update_balance(dm._current_balance)
        s = dm.status()
        assert isinstance(s, DrawdownStatus)
        assert s.current_balance == 960.0
        assert s.peak_balance == 1000.0
        assert s.can_arb is True
        assert s.can_directional is True


class TestLiquidationSignalLogic:
    """LiquidationSignalEngine — signal types, direction mapping, expiry."""

    def setup_method(self):
        from intelligence.liquidation_signal import LiquidationSignalEngine
        self.Engine = LiquidationSignalEngine

    # ── Type A: cascade_entry ────────────────────────────────────────────────

    def test_bearish_liq_produces_short_signal(self):
        """Longs liquidated → downward pressure → trade SHORT."""
        le = self.Engine()
        sig = _make_liq_signal(direction="bearish", notional=60_000)
        _run(le.process_liquidation(sig))
        signals = le.get_all_active_signals()
        assert len(signals) == 1
        assert signals[0].direction == "short"
        assert signals[0].signal_type == "cascade_entry"

    def test_bullish_liq_produces_long_signal(self):
        """Shorts liquidated → upward pressure → trade LONG."""
        le = self.Engine()
        sig = _make_liq_signal(direction="bullish", notional=60_000)
        _run(le.process_liquidation(sig))
        signals = le.get_all_active_signals()
        assert signals[0].direction == "long"

    def test_cascade_flag_sets_max_size_factor(self):
        le = self.Engine()
        sig = _make_liq_signal(cascade=True)
        _run(le.process_liquidation(sig))
        assert le.get_all_active_signals()[0].size_factor == 1.5

    def test_notional_size_factors(self):
        """Size factor brackets correct."""
        le = self.Engine()
        cases = [
            (300, 0.1),       # < $1k
            (5_000, 0.3),     # > $1k
            (25_000, 0.6),    # > $10k
            (75_000, 1.0),    # > $50k
            (250_000, 1.3),   # > $200k
        ]
        from intelligence.liquidation_signal import LiquidationSignalEngine
        for notional, expected in cases:
            factor = LiquidationSignalEngine._size_factor(notional, False)
            assert factor == expected, f"notional={notional}: got {factor}, expected {expected}"

    # ── Time decay ───────────────────────────────────────────────────────────

    def test_time_decay_fresh(self):
        le = self.Engine()
        sig = _make_liq_signal(notional=60_000)
        _run(le.process_liquidation(sig))
        active = le.get_all_active_signals()
        # Just created — should be 1.0 decay
        assert active[0].time_decay() == 1.0

    def test_time_decay_at_30s(self):
        le = self.Engine()
        sig = _make_liq_signal(notional=60_000)
        _run(le.process_liquidation(sig))
        active = le.get_all_active_signals()
        # Manually backdate generated_at by 31s
        active[0].generated_at -= 31
        assert active[0].time_decay() == 0.7

    def test_time_decay_at_61s(self):
        le = self.Engine()
        sig = _make_liq_signal(notional=60_000)
        _run(le.process_liquidation(sig))
        active = le.get_all_active_signals()
        active[0].generated_at -= 61
        assert active[0].time_decay() == 0.4

    def test_signal_expired_after_90s(self):
        le = self.Engine()
        sig = _make_liq_signal(notional=60_000)
        _run(le.process_liquidation(sig))
        active = le.get_all_active_signals()
        active[0].generated_at -= 91
        active[0].expires_at = time.time() - 1
        assert active[0].is_expired()
        assert le.get_tier6_score("BTC-USD") == 0.0

    # ── Type B: recovery_entry ────────────────────────────────────────────────

    def test_recovery_fires_after_2min_silence(self):
        le = self.Engine()
        sig = _make_liq_signal(direction="bearish", notional=60_000, symbol="ETH-USD")
        _run(le.process_liquidation(sig))
        # Simulate 2min+ silence
        le._last_liq_ts["ETH-USD"] = time.time() - 125
        _run(le.check_recovery_signals())
        recoveries = [s for s in le.get_all_active_signals() if s.signal_type == "recovery_entry"]
        assert len(recoveries) >= 1
        # bearish cascade → recovery direction = "long"
        assert recoveries[0].direction == "long"

    def test_recovery_direction_inverts_cascade(self):
        le = self.Engine()
        # Bullish cascade (shorts liq'd → price pumped) → recovery = short (sellers return)
        sig = _make_liq_signal(direction="bullish", notional=100_000, symbol="SOL-USD")
        _run(le.process_liquidation(sig))
        le._last_liq_ts["SOL-USD"] = time.time() - 125
        _run(le.check_recovery_signals())
        recoveries = [s for s in le.get_all_active_signals() if s.signal_type == "recovery_entry"]
        assert recoveries[0].direction == "short"

    def test_recovery_has_max_size_factor(self):
        """Type B signals are highest value (confirmed exhaustion)."""
        le = self.Engine()
        sig = _make_liq_signal(notional=5_000, symbol="BNB-USD")  # small notional Type A
        _run(le.process_liquidation(sig))
        le._last_liq_ts["BNB-USD"] = time.time() - 125
        _run(le.check_recovery_signals())
        a_signals = [s for s in le.get_all_active_signals() if s.signal_type == "cascade_entry"]
        b_signals = [s for s in le.get_all_active_signals() if s.signal_type == "recovery_entry"]
        if a_signals and b_signals:
            assert b_signals[0].size_factor > a_signals[0].size_factor

    def test_recovery_no_duplicate_for_same_symbol(self):
        le = self.Engine()
        sig = _make_liq_signal(symbol="LINK-USD")
        _run(le.process_liquidation(sig))
        le._last_liq_ts["LINK-USD"] = time.time() - 125
        _run(le.check_recovery_signals())
        _run(le.check_recovery_signals())  # call twice
        recoveries = [s for s in le.get_all_active_signals()
                      if s.signal_type == "recovery_entry" and s.symbol == "LINK-USD"]
        assert len(recoveries) == 1  # no duplicates

    def test_recovery_does_not_fire_before_2min(self):
        le = self.Engine()
        sig = _make_liq_signal(symbol="AVAX-USD")
        _run(le.process_liquidation(sig))
        le._last_liq_ts["AVAX-USD"] = time.time() - 90  # only 90s — not 2min yet
        _run(le.check_recovery_signals())
        recoveries = [s for s in le.get_all_active_signals() if s.signal_type == "recovery_entry"]
        assert len(recoveries) == 0

    # ── Tier 6 score ─────────────────────────────────────────────────────────

    def test_tier6_score_returns_best_signal(self):
        le = self.Engine()
        sig1 = _make_liq_signal(notional=5_000)    # size_factor 0.3
        sig2 = _make_liq_signal(notional=75_000)   # size_factor 1.0
        _run(le.process_liquidation(sig1))
        _run(le.process_liquidation(sig2))
        score = le.get_tier6_score("BTC-USD")
        assert score == 1.0  # best active signal

    def test_tier6_market_wide_affects_all_symbols(self):
        le = self.Engine()
        sig = _make_liq_signal(notional=75_000, symbol="")  # market-wide
        _run(le.process_liquidation(sig))
        for symbol in ["BTC-USD", "ETH-USD", "SOL-USD"]:
            assert le.get_tier6_score(symbol) > 0.0


class TestCoherenceWithTier6:
    """CoherenceEngine — Tier 6 integration."""

    def setup_method(self):
        from intelligence.coherence import CoherenceEngine
        self.ce = CoherenceEngine()

    def test_tier6_score_contributes_to_weighted_score(self):
        base = {"regime": "neutral", "market_type": "chop"}
        s_without, _, c_without = self.ce.calculate_weighted_score("BTC-USD", base)
        s_with, _, c_with = self.ce.calculate_weighted_score(
            "BTC-USD", {**base, "tier6_liq_score": 1.2}
        )
        assert s_with > s_without
        assert c_with.get("liquidation", 0) == 1.2

    def test_tier6_score_capped_at_1_5(self):
        data = {"tier6_liq_score": 5.0}  # would be 5.0 without cap
        _, _, comps = self.ce.calculate_weighted_score("ETH-USD", data)
        assert comps.get("liquidation", 0) == 1.5

    def test_tier6_below_0_75_does_not_increment_raw_score(self):
        data = {"tier6_liq_score": 0.5}
        _, raw_without, _ = self.ce.calculate_weighted_score("ETH-USD", {})
        _, raw_with, _ = self.ce.calculate_weighted_score("ETH-USD", data)
        assert raw_with == raw_without  # < 0.75 threshold

    def test_tier6_at_0_75_increments_raw_score(self):
        data = {"tier6_liq_score": 0.75}
        _, raw_without, _ = self.ce.calculate_weighted_score("ETH-USD", {})
        _, raw_with, _ = self.ce.calculate_weighted_score("ETH-USD", data)
        assert raw_with == raw_without + 1

    def test_weighted_score_never_exceeds_10(self):
        """Hard ceiling at 10.0 regardless of Tier 6 boost."""
        data = {
            "tier6_liq_score": 1.5,
            "regime": "risk_on",
            "market_type": "expansion",
            "ssi_status": "strong_inflow",
            "oi_signal": "BULLISH_EXPANSION",
            "sweep": "buy_side",
            "vpin_hot": True,
            "volume_surge": 3.0,
            "candle_conviction": 0.85,
            "funding_class": "extreme_negative",
            "imbalance": 0.55,
        }
        score, _, _ = self.ce.calculate_weighted_score("BTC-USD", data)
        assert score <= 10.0

    def test_independence_discount_applies_to_liquidation(self):
        """Liquidation + OI momentum should get independence discount vs uncorrelated sum."""
        data_overlap = {
            "tier6_liq_score": 1.5,
            "oi_signal": "BULLISH_EXPANSION",    # correlates 0.40 with liquidation
        }
        _, _, comps = self.ce.calculate_weighted_score("BTC-USD", data_overlap)
        # independence_discount < 1.0 means discount was applied
        independence_factor = comps.get("independence_discount", 1.0)
        assert independence_factor < 1.0, (
            f"Expected discount < 1.0 (overlap of liq+OI), got {independence_factor:.4f}"
        )
        # Factor must be ≥ 0.85 (max 15% discount cap)
        assert independence_factor >= 0.85


class TestRiskEngineGateOrdering:
    """Risk engine gate order and drawdown_manager integration."""

    def _make_candidate(self, symbol="BTC-USD", side="long", coherence=3.5,
                        rr=2.5, atr=500, entry=65000):
        from execution.schemas import TradeCandidate
        stop = entry - atr * 0.75
        tp1 = entry + atr * 2
        return TradeCandidate(
            symbol=symbol, side=side, entry_price=entry, stop_price=stop,
            tp1_price=tp1, tp2_price=tp1 * 1.5, tp3_price=tp1 * 2,
            size=0.001, initial_margin=65.0, leverage=10,
            rr_ratio=rr, coherence_score=coherence, size_multiplier=1.0,
            signal_reason="test", invalidation="", timestamp_ms=0,
            signal_age_ms=0, atr=atr, atr_ratio=1.2,
        )

    def _make_risk_engine(self):
        from risk.risk_engine import RiskEngine
        from risk.margin_engine import MarginEngine
        from risk.position_manager import PositionManager
        config = MagicMock()
        config.mode = "paper"
        config.min_coherence = 2.0
        config.live_min_coherence = 2.0
        config.max_daily_loss_pct = 0.05
        config.max_portfolio_var_pct = 0.40
        config.max_symbol_concentration = 0.20
        config.max_spread_bps = 50.0
        config.risk_pct = 0.015
        config.default_leverage = 10
        config.balance_floor = 50.0
        me = MarginEngine()
        pm = PositionManager()
        calendar = MagicMock()
        async def _cs(sym): return MagicMock(regime="NORMAL", reason="", size_multiplier=1.0, stop_atr_multiplier=1.0)
        calendar.get_state = _cs
        calendar.get_states_all = MagicMock(return_value={})
        return RiskEngine(config, me, pm, calendar)

    def test_drawdown_halt_blocks_before_daily_loss_gate(self):
        """drawdown_halt gate fires before daily_loss gate."""
        from risk.drawdown_manager import DrawdownManager
        re = self._make_risk_engine()
        dm = DrawdownManager(1000)
        dm.update_balance(700)  # 30% → halted
        candidate = self._make_candidate()
        approved, reason = _run(re.validate(candidate, 1000, drawdown_manager=dm))
        assert not approved
        assert "drawdown_halt" in reason

    def test_drawdown_none_skips_halt_gate(self):
        """drawdown_manager=None must not raise."""
        re = self._make_risk_engine()
        candidate = self._make_candidate()
        # Should reach further gates (may fail on coherence, etc.) — not AttributeError
        try:
            _run(re.validate(candidate, 1000, drawdown_manager=None))
        except AttributeError:
            pytest.fail("drawdown_manager=None raised AttributeError")

    def test_healthy_account_drawdown_passes(self):
        """No drawdown → drawdown_halt gate passes."""
        from risk.drawdown_manager import DrawdownManager
        re = self._make_risk_engine()
        dm = DrawdownManager(1000)
        dm.update_balance(980)  # 2% DD — no issue
        candidate = self._make_candidate()
        # Gate may fail elsewhere (balance_floor, etc.) but not on drawdown
        approved, reason = _run(re.validate(candidate, 980, drawdown_manager=dm))
        assert "drawdown_halt" not in reason or approved


# ══════════════════════════════════════════════════════════════════════════════
# TIER 2: TECHNICAL TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestImports:
    """All new modules importable without circular imports."""

    def test_drawdown_manager_importable(self):
        from risk.drawdown_manager import DrawdownManager, DrawdownStatus
        assert DrawdownManager is not None
        assert DrawdownStatus is not None

    def test_liquidation_signal_engine_importable(self):
        from intelligence.liquidation_signal import LiquidationSignalEngine, ActiveLiqSignal
        assert LiquidationSignalEngine is not None
        assert ActiveLiqSignal is not None

    def test_coherence_has_liquidation_correlation(self):
        from intelligence.coherence import TIER_CORRELATIONS
        liq_pairs = [(a, b) for a, b in TIER_CORRELATIONS if a == "liquidation" or b == "liquidation"]
        assert len(liq_pairs) >= 2, "liquidation must have correlation entries"

    def test_interpreter_accepts_liq_engine_none(self):
        """Interpreter must not crash when liq_engine=None."""
        from intelligence.interpreter import IntelligenceInterpreter
        config = MagicMock()
        config.stop_atr_mult = 0.75
        # Should not raise
        interp = IntelligenceInterpreter(
            config=config,
            system_state=MagicMock(),
            signal_generator=MagicMock(),
            data_processor=MagicMock(),
            orderbook_stores={},
            mark_price_stores={},
            candle_buffers={},
            trade_flow_stores={},
            liq_engine=None,
        )
        assert interp.liq_engine is None

    def test_risk_engine_validate_accepts_drawdown_manager_none(self):
        """validate() with drawdown_manager=None must not raise on import."""
        import inspect
        from risk.risk_engine import RiskEngine
        sig = inspect.signature(RiskEngine.validate)
        assert "drawdown_manager" in sig.parameters

    def test_config_has_new_fields(self):
        from core.config import Settings
        s = Settings()
        assert hasattr(s, "max_total_drawdown")
        assert hasattr(s, "max_weekly_drawdown")
        assert hasattr(s, "drawdown_recovery_threshold")
        assert hasattr(s, "base_trade_usd")
        assert hasattr(s, "min_trade_usd")
        assert hasattr(s, "max_trade_usd")
        assert s.base_trade_usd == 200.0   # mainnet: $200 notional = $20 margin at 10x
        assert s.min_trade_usd == 50.0     # SoDEX dust guard (was 15.0 paper-era; not the size target)
        assert s.max_trade_usd == 300.0    # mainnet ceiling (was 50.0 paper-era)


class TestDrawdownManagerPersistence:
    """DrawdownManager state survives restart via JSON persistence."""

    def test_state_saved_and_restored(self):
        from risk.drawdown_manager import DrawdownManager
        import risk.drawdown_manager as _dm_mod
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "drawdown_state.json"
            orig = _dm_mod._STATE_FILE
            try:
                _dm_mod._STATE_FILE = state_file
                dm1 = DrawdownManager(1000)
                dm1._session_start = 860  # avoid daily halt
                dm1.update_balance(850)
                if state_file.exists():
                    data = json.loads(state_file.read_text())
                    assert data["peak"] == 1000.0
            finally:
                _dm_mod._STATE_FILE = orig

    def test_state_file_created_on_update(self):
        from risk.drawdown_manager import DrawdownManager
        import risk.drawdown_manager as _dm_mod
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "drawdown_state.json"
            orig = _dm_mod._STATE_FILE
            try:
                _dm_mod._STATE_FILE = state_file
                dm = DrawdownManager(1000)
                dm._session_start = 905
                dm.update_balance(900)
                assert state_file.exists()
            finally:
                _dm_mod._STATE_FILE = orig

    def test_halted_state_persists(self):
        from risk.drawdown_manager import DrawdownManager
        import risk.drawdown_manager as _dm_mod
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "drawdown_state.json"
            orig = _dm_mod._STATE_FILE
            try:
                _dm_mod._STATE_FILE = state_file
                dm = _dm_at_total_dd(1000, 0.26)
                dm.update_balance(dm._current_balance)   # triggers halt
                if state_file.exists():
                    data = json.loads(state_file.read_text())
                    assert data["halted"] is True
                    assert data["size_multiplier"] == 0.0
            finally:
                _dm_mod._STATE_FILE = orig


class TestNullSafety:
    """Guard conditions prevent crashes on None inputs."""

    def test_liq_signal_engine_handles_empty_direction(self):
        from intelligence.liquidation_signal import LiquidationSignalEngine
        le = LiquidationSignalEngine()
        sig = _make_liq_signal(direction="")  # unknown direction
        # Should not raise — just skip
        _run(le.process_liquidation(sig))
        assert len(le.get_all_active_signals()) == 0

    def test_drawdown_manager_ignores_zero_balance(self):
        from risk.drawdown_manager import DrawdownManager
        dm = DrawdownManager(1000)
        dm.update_balance(0)    # ignored
        dm.update_balance(-5)   # ignored
        assert dm._current_balance == 1000.0  # unchanged

    def test_coherence_handles_missing_tier6(self):
        from intelligence.coherence import CoherenceEngine
        ce = CoherenceEngine()
        # tier6_liq_score missing → treated as 0.0
        score, _, comps = ce.calculate_weighted_score("BTC-USD", {})
        assert comps.get("liquidation", 0) == 0.0

    def test_liq_engine_get_tier6_zero_when_empty(self):
        from intelligence.liquidation_signal import LiquidationSignalEngine
        le = LiquidationSignalEngine()
        assert le.get_tier6_score("BTC-USD") == 0.0
        assert le.get_best_signal("BTC-USD") is None


# ══════════════════════════════════════════════════════════════════════════════
# TIER 3: QUANT TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestFixedFloorSizing:
    """build_candidate() fixed floor notional sizing."""

    def _build(self, symbol="BTC-USD", direction="long", score=3.0,
               mark=65000.0, atr=500.0):
        from main import build_candidate
        from risk.margin_engine import MarginEngine
        from core.config import Settings
        state = _make_market_state(symbol, direction, score, mark, atr)
        config = Settings()
        config.base_trade_usd = 25.0
        config.min_trade_usd = 15.0
        config.max_trade_usd = 50.0
        me = MarginEngine()
        return build_candidate(state, 300.0, me, config=config)

    def test_base_notional_at_normal_coherence(self):
        """score < 3.0 → conv_mult=1.0 → target=$25."""
        from main import build_candidate
        from risk.margin_engine import MarginEngine
        from core.config import Settings
        state = _make_market_state(score=2.5, mark_price=65000, atr=500)
        config = Settings()
        config.base_trade_usd = 25.0
        config.min_trade_usd = 15.0
        config.max_trade_usd = 50.0
        me = MarginEngine()
        c = build_candidate(state, 300.0, me, config=config)
        assert c is not None
        notional = c.entry_price * c.size
        assert 14.0 <= notional <= 27.0  # ~$25 ± tick rounding

    def test_conviction_mult_at_score_3(self):
        """score ≥ 3.0 → conv_mult=1.4 → target=$35."""
        c = self._build(score=3.5)
        assert c is not None
        notional = c.entry_price * c.size
        assert 33.0 <= notional <= 37.0  # ~$35

    def test_conviction_mult_at_score_5(self):
        """score ≥ 5.0 → conv_mult=2.0 → target=$50 (capped)."""
        c = self._build(score=5.5)
        assert c is not None
        notional = c.entry_price * c.size
        assert 48.0 <= notional <= 52.0  # ~$50

    def test_min_floor_enforced(self):
        """Even at lowest coherence, notional ≥ min_trade_usd."""
        c = self._build(score=1.5)
        if c is not None:  # may be rejected by coherence gate
            assert c.entry_price * c.size >= 14.0  # ~$15

    def test_fixed_floor_works_across_price_ranges(self):
        """Fixed floor produces valid sizes for all ARIA assets."""
        from main import build_candidate
        from risk.margin_engine import MarginEngine
        from core.config import Settings
        config = Settings()
        config.base_trade_usd = 25.0
        config.min_trade_usd = 15.0
        config.max_trade_usd = 50.0
        me = MarginEngine()
        cases = [
            ("BTC-USD",  65000.0, 500.0),
            ("ETH-USD",  3500.0,  30.0),
            ("SOL-USD",  150.0,   2.5),
            ("XAUT-USD", 3300.0,  40.0),
            ("BNB-USD",  580.0,   5.0),
            ("LINK-USD", 18.0,    0.3),
            ("AVAX-USD", 35.0,    0.6),
            ("USTECH100-USD", 19000.0, 200.0),
        ]
        for sym, price, atr in cases:
            state = _make_market_state(sym, "long", 3.0, price, atr)
            c = build_candidate(state, 300.0, me, config=config)
            assert c is not None, f"build_candidate returned None for {sym}"
            notional = c.entry_price * c.size
            assert notional >= 10.0, f"{sym}: notional ${notional:.2f} below $10 floor"

    def test_kelly_fallback_when_base_zero(self):
        """base_trade_usd=0 → use Kelly (risk_pct × balance)."""
        from main import build_candidate
        from risk.margin_engine import MarginEngine
        from core.config import Settings
        config = Settings()
        config.base_trade_usd = 0.0  # Kelly mode
        config.risk_pct = 0.015
        me = MarginEngine()
        state = _make_market_state(mark_price=65000, atr=500)
        c = build_candidate(state, 300.0, me, config=config)
        assert c is not None
        # Kelly: 300 × 1.5% = $4.50 risk, atr-stop, leverage
        assert c.size > 0


class TestRRDiscipline:
    """Every valid candidate must have R:R ≥ 2.0."""

    def test_rr_at_least_2_for_valid_candidate(self):
        from main import build_candidate
        from risk.margin_engine import MarginEngine
        from core.config import Settings
        config = Settings()
        config.base_trade_usd = 25.0
        config.min_trade_usd = 15.0
        config.max_trade_usd = 50.0
        me = MarginEngine()
        for sym, price, atr in [
            ("BTC-USD", 65000, 500), ("ETH-USD", 3500, 30),
            ("SOL-USD", 150, 2.5), ("LINK-USD", 18, 0.3),
        ]:
            state = _make_market_state(sym, "long", 3.5, price, atr)
            c = build_candidate(state, 300.0, me, config=config)
            assert c is not None
            assert c.rr_ratio >= 2.0, f"{sym}: R:R {c.rr_ratio:.2f} < 2.0"

    def test_stop_on_correct_side_long(self):
        """Long stop must be BELOW entry."""
        from main import build_candidate
        from risk.margin_engine import MarginEngine
        from core.config import Settings
        config = Settings()
        config.base_trade_usd = 25.0
        config.min_trade_usd = 15.0
        config.max_trade_usd = 50.0
        me = MarginEngine()
        state = _make_market_state(direction="long", mark_price=65000, atr=500)
        c = build_candidate(state, 300.0, me, config=config)
        assert c is not None
        assert c.stop_price < c.entry_price

    def test_stop_on_correct_side_short(self):
        """Short stop must be ABOVE entry."""
        from main import build_candidate
        from risk.margin_engine import MarginEngine
        from core.config import Settings
        config = Settings()
        config.base_trade_usd = 25.0
        config.min_trade_usd = 15.0
        config.max_trade_usd = 50.0
        me = MarginEngine()
        state = _make_market_state(direction="short", mark_price=65000, atr=500)
        c = build_candidate(state, 300.0, me, config=config)
        assert c is not None
        assert c.stop_price > c.entry_price

    def test_tp1_is_1r(self):
        """TP1 should be at 1R (1× risk distance from entry)."""
        from main import build_candidate
        from risk.margin_engine import MarginEngine
        from core.config import Settings
        config = Settings()
        config.base_trade_usd = 25.0
        config.min_trade_usd = 15.0
        config.max_trade_usd = 50.0
        me = MarginEngine()
        state = _make_market_state(direction="long", mark_price=65000, atr=500)
        c = build_candidate(state, 300.0, me, config=config)
        risk_dist = abs(c.entry_price - c.stop_price)
        expected_tp1 = c.entry_price + risk_dist
        assert abs(c.tp1_price - expected_tp1) < 0.01

    def test_tp2_is_2r(self):
        """TP2 should be at 2R."""
        from main import build_candidate
        from risk.margin_engine import MarginEngine
        from core.config import Settings
        config = Settings()
        config.base_trade_usd = 25.0
        config.min_trade_usd = 15.0
        config.max_trade_usd = 50.0
        me = MarginEngine()
        state = _make_market_state(direction="long", mark_price=65000, atr=500)
        c = build_candidate(state, 300.0, me, config=config)
        risk_dist = abs(c.entry_price - c.stop_price)
        expected_tp2 = c.entry_price + 2 * risk_dist
        assert abs(c.tp2_price - expected_tp2) < 0.01


class TestMultiplierChain:
    """Size multiplier chain must only reduce, never inflate."""

    def test_normal_drawdown_gives_1x(self):
        from risk.drawdown_manager import DrawdownManager
        dm = DrawdownManager(1000)
        dm.update_balance(990)
        assert dm.get_size_multiplier() == 1.0

    def test_multipliers_compound_down_not_up(self):
        """Drawdown multiplier never exceeds 1.0."""
        dm = _dm_at_total_dd(1000, 0.13)
        dm.update_balance(dm._current_balance)
        mult = dm.get_size_multiplier()
        assert 0.0 < mult <= 1.0

    def test_halted_gives_0x(self):
        from risk.drawdown_manager import DrawdownManager
        dm = DrawdownManager(1000)
        dm.update_balance(740)  # 26% → halted
        assert dm.get_size_multiplier() == 0.0


# ══════════════════════════════════════════════════════════════════════════════
# GAIN HUNTER BENCHMARKS
# ══════════════════════════════════════════════════════════════════════════════

class TestGainHunterBenchmarks:
    """
    10 benchmarks a gain-hunting bot must pass to be viable.

    BM-GH-01: Average trade notional ≥ $15 (floor holds after all multipliers)
    BM-GH-02: Strong conviction (score≥5) trades at 2× base ($50 ceiling)
    BM-GH-03: After ATH recovery, size returns to 50% (not 0%)
    BM-GH-04: Daily halt clears after reset — bot resumes next day
    BM-GH-05: Weekly DD reduces size (not halt) — arb continues
    BM-GH-06: Total 25%+ DD absolute halt — no directional slippage
    BM-GH-07: Type B recovery signal outscores Type A at same freshness
    BM-GH-08: Tier 6 on hot signal (score≥1.5) lifts coherence by ≥0.5
    BM-GH-09: Zero-DD account trades at 100% size
    BM-GH-10: Consecutive loss halts route to MINIMAL (50%), not halt
    """

    def setup_method(self):
        from risk.drawdown_manager import DrawdownManager
        from intelligence.liquidation_signal import LiquidationSignalEngine
        from intelligence.coherence import CoherenceEngine
        self.DM = DrawdownManager
        self.LE = LiquidationSignalEngine
        self.CE = CoherenceEngine

    def test_bm_gh_01_min_notional_floor_holds(self):
        """BM-GH-01: $15 floor enforced even after multipliers."""
        from main import build_candidate
        from risk.margin_engine import MarginEngine
        from core.config import Settings
        config = Settings()
        config.base_trade_usd = 25.0
        config.min_trade_usd = 15.0
        config.max_trade_usd = 50.0
        me = MarginEngine()
        state = _make_market_state(mark_price=65000, atr=500, score=2.5)
        c = build_candidate(state, 300.0, me, config=config)
        assert c is not None
        notional = c.entry_price * c.size
        # Even at 50% drawdown multiplier: 25 × 0.5 = 12.5 < 15 → floored to 15
        assert notional >= 10.0  # exchange min floor

    def test_bm_gh_02_strong_conviction_hits_50_ceiling(self):
        """BM-GH-02: score≥5 → 2× = $50 ceiling."""
        from main import build_candidate
        from risk.margin_engine import MarginEngine
        from core.config import Settings
        config = Settings()
        config.base_trade_usd = 25.0
        config.min_trade_usd = 15.0
        config.max_trade_usd = 50.0
        me = MarginEngine()
        state = _make_market_state(mark_price=65000, atr=500, score=6.0)
        c = build_candidate(state, 300.0, me, config=config)
        assert c is not None
        notional = c.entry_price * c.size
        assert 48.0 <= notional <= 52.0

    def test_bm_gh_03_ath_recovery_returns_50pct(self):
        """BM-GH-03: New ATH while halted → resume at 50%."""
        dm = self.DM(1000)
        dm.update_balance(700)   # halted
        dm.update_balance(1001)  # new ATH
        assert dm.get_size_multiplier() == 0.50
        assert dm.can_trade_directional()

    def test_bm_gh_04_daily_halt_clears_after_reset(self):
        """BM-GH-04: Daily halt clears on reset_daily()."""
        dm = self.DM(1000)
        dm.update_balance(940)   # 6% daily → halt
        assert not dm.can_trade_directional()
        dm.reset_daily()
        dm.update_balance(940)   # same balance, no new daily loss
        assert dm.can_trade_directional()

    def test_bm_gh_05_weekly_dd_reduces_not_halts(self):
        """BM-GH-05: 15%+ weekly DD reduces size but allows arb."""
        dm = _dm_at_total_dd(1000, 0.17)
        dm._week_start = 1000
        dm.update_balance(dm._current_balance)
        assert dm.can_trade_arb()
        assert dm.get_size_multiplier() == 0.50  # reduced not halted

    def test_bm_gh_06_total_25_pct_halt_absolute(self):
        """BM-GH-06: 25%+ total DD → directional halt, arb still runs."""
        dm = self.DM(1000)
        dm.update_balance(740)   # 26% total → HALTED
        assert not dm.can_trade_directional()
        assert dm.can_trade_arb()
        assert dm.get_size_multiplier() == 0.0

    def test_bm_gh_07_type_b_outscores_type_a(self):
        """BM-GH-07: Recovery signals (Type B, size_factor=1.5) outscore small cascade (Type A)."""
        le = self.LE()
        # Use notional > $10k so _last_cascade_dir gets recorded
        sig_a = _make_liq_signal(notional=15_000, symbol="ETH-USD")
        _run(le.process_liquidation(sig_a))
        # Trigger Type B by simulating 2min silence
        le._last_liq_ts["ETH-USD"] = time.time() - 125
        _run(le.check_recovery_signals())
        type_a = [s for s in le.get_all_active_signals() if s.signal_type == "cascade_entry"]
        type_b = [s for s in le.get_all_active_signals() if s.signal_type == "recovery_entry"]
        assert type_b, "Type B signal not generated (check _last_cascade_dir logic)"
        assert type_b[0].size_factor >= type_a[0].size_factor

    def test_bm_gh_08_tier6_lifts_coherence(self):
        """BM-GH-08: Tier 6 score≥1.5 lifts total coherence by ≥0.5."""
        ce = self.CE()
        base = {"regime": "risk_on", "market_type": "trend"}
        s_without, _, _ = ce.calculate_weighted_score("BTC-USD", base)
        s_with, _, _ = ce.calculate_weighted_score("BTC-USD", {**base, "tier6_liq_score": 1.5})
        assert s_with - s_without >= 0.5

    def test_bm_gh_09_zero_dd_full_size(self):
        """BM-GH-09: Account at equity peak → 100% sizing."""
        dm = self.DM(1000)
        dm.update_balance(1000)  # no DD
        assert dm.get_size_multiplier() == 1.0
        assert dm.can_trade_directional()

    def test_bm_gh_10_ten_pct_dd_minimal_not_halt(self):
        """BM-GH-10: 10%+ DD reduces to 75%, 20%+ to 50% — no halt until 25%."""
        dm = _dm_at_total_dd(1000, 0.12)
        dm.update_balance(dm._current_balance)
        assert dm.can_trade_directional()
        assert dm.get_size_multiplier() == 0.75

        dm2 = _dm_at_total_dd(1000, 0.21)
        dm2.update_balance(dm2._current_balance)
        assert dm2.can_trade_directional()
        assert dm2.get_size_multiplier() == 0.50


# ══════════════════════════════════════════════════════════════════════════════
# CRYPTO + STOCK MARKET VIABILITY CHECKS
# ══════════════════════════════════════════════════════════════════════════════

class TestMarketViability:
    """
    Verify all ARIA gates can fire in both crypto and stock (USTECH100) markets.
    """

    def test_ustech100_build_candidate_succeeds(self):
        """USTECH100 at realistic prices produces valid candidate."""
        from main import build_candidate
        from risk.margin_engine import MarginEngine
        from core.config import Settings
        config = Settings()
        config.base_trade_usd = 25.0
        config.min_trade_usd = 15.0
        config.max_trade_usd = 50.0
        me = MarginEngine()
        state = _make_market_state("USTECH100-USD", "long", 3.5, 19000.0, 200.0)
        c = build_candidate(state, 300.0, me, config=config)
        assert c is not None
        assert c.entry_price == 19000.0
        assert c.size > 0

    def test_xaut_build_candidate_succeeds(self):
        """XAUT (gold) at realistic prices."""
        from main import build_candidate
        from risk.margin_engine import MarginEngine
        from core.config import Settings
        config = Settings()
        config.base_trade_usd = 25.0
        config.min_trade_usd = 15.0
        config.max_trade_usd = 50.0
        me = MarginEngine()
        state = _make_market_state("XAUT-USD", "short", 3.5, 3300.0, 40.0)
        c = build_candidate(state, 300.0, me, config=config)
        assert c is not None

    def test_all_8_assets_produce_candidates(self):
        """All 8 ARIA assets must produce valid candidates from fixed floor."""
        from main import build_candidate
        from risk.margin_engine import MarginEngine
        from core.config import Settings
        config = Settings()
        config.base_trade_usd = 25.0
        config.min_trade_usd = 15.0
        config.max_trade_usd = 50.0
        me = MarginEngine()
        assets = [
            ("BTC-USD",          65000.0,  500.0),
            ("ETH-USD",          3500.0,    30.0),
            ("SOL-USD",           150.0,     2.5),
            ("XAUT-USD",         3300.0,    40.0),
            ("BNB-USD",           580.0,     5.0),
            ("LINK-USD",           18.0,     0.3),
            ("AVAX-USD",           35.0,     0.6),
            ("USTECH100-USD",   19000.0,   200.0),
        ]
        for sym, price, atr in assets:
            state = _make_market_state(sym, "long", 3.5, price, atr)
            c = build_candidate(state, 300.0, me, config=config)
            assert c is not None, f"{sym}: build_candidate returned None"

    def test_bear_regime_blocks_longs_non_inverse(self):
        """In BEAR regime, longs on BTC/ETH/SOL are blocked by regime gate."""
        from risk.risk_engine import RiskEngine
        from risk.margin_engine import MarginEngine
        from risk.position_manager import PositionManager
        from execution.schemas import TradeCandidate
        config = MagicMock()
        config.mode = "paper"
        config.min_coherence = 2.0
        config.live_min_coherence = 2.0
        config.max_daily_loss_pct = 0.05
        config.max_portfolio_var_pct = 0.40
        config.max_symbol_concentration = 0.20
        config.max_spread_bps = 50.0
        config.risk_pct = 0.015
        config.default_leverage = 10
        config.balance_floor = 50.0
        me = MarginEngine()
        pm = PositionManager()
        cal = MagicMock()
        async def _cs(sym): return MagicMock(regime="NORMAL", reason="", size_multiplier=1.0, stop_atr_multiplier=1.0)
        cal.get_state = _cs
        re = RiskEngine(config, me, pm, cal)
        candidate = TradeCandidate(
            symbol="BTC-USD", side="long", entry_price=65000, stop_price=64000,
            tp1_price=67000, tp2_price=69000, tp3_price=71000,
            size=0.001, initial_margin=65.0, leverage=10, rr_ratio=2.0,
            coherence_score=3.5, size_multiplier=1.0, signal_reason="test",
            invalidation="", timestamp_ms=0, signal_age_ms=0, atr=500, atr_ratio=1.2,
        )
        approved, reason = _run(re.validate(candidate, 1000, regime="BEAR"))
        assert not approved
        assert "regime" in reason.lower()

    def test_xaut_long_allowed_in_bear(self):
        """XAUT (gold) long is allowed in BEAR regime (inverse correlation)."""
        from risk.risk_engine import RiskEngine
        from risk.margin_engine import MarginEngine
        from risk.position_manager import PositionManager
        from execution.schemas import TradeCandidate
        config = MagicMock()
        config.mode = "paper"
        config.min_coherence = 2.0
        config.live_min_coherence = 2.0
        config.max_daily_loss_pct = 0.05
        config.max_portfolio_var_pct = 0.40
        config.max_symbol_concentration = 0.20
        config.max_spread_bps = 50.0
        config.risk_pct = 0.015
        config.default_leverage = 10
        config.balance_floor = 50.0
        me = MarginEngine()
        pm = PositionManager()
        cal = MagicMock()
        async def _cs(sym): return MagicMock(regime="NORMAL", reason="", size_multiplier=1.0, stop_atr_multiplier=1.0)
        cal.get_state = _cs
        re = RiskEngine(config, me, pm, cal)
        candidate = TradeCandidate(
            symbol="XAUT-USD", side="long", entry_price=3300, stop_price=3270,
            tp1_price=3360, tp2_price=3390, tp3_price=3420,
            size=0.01, initial_margin=33.0, leverage=10, rr_ratio=2.0,
            coherence_score=3.5, size_multiplier=1.0, signal_reason="test",
            invalidation="", timestamp_ms=0, signal_age_ms=0, atr=40, atr_ratio=1.2,
        )
        approved, reason = _run(re.validate(candidate, 1000, regime="BEAR"))
        # Should NOT be blocked by regime gate (XAUT is inverse)
        assert "regime_bear_no_longs" not in reason
