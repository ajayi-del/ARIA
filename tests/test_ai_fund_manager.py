"""
tests/test_ai_fund_manager.py — Phase 3: AI Fund Manager / World Model / Will Engine

Covers:
  - ParamStore AI-writable TTL parameters
  - WorldModel environmental classification
  - WillEngine synthesis (Kant x Nietzsche x World)
  - Will veto and size modulation
  - Asset class boost mapping
  - P1: build_candidate reads AI ParamStore overrides (blacklist, leverage, coherence, ATR)
  - P2: PortfolioAllocator target weights, current weights, concentration guard
"""

import sys
import os
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


# ══════════════════════════════════════════════════════════════════════════════
# ParamStore TTL
# ══════════════════════════════════════════════════════════════════════════════

class TestParamStoreAI:
    """AI-writable parameters with automatic expiry."""

    @pytest.fixture
    def store(self, tmp_path):
        from memory.param_store import ParamStore, STORE_PATH
        # Redirect store path to temp for isolation
        _orig = STORE_PATH
        _new = tmp_path / "param_store.json"
        # Patch module-level path via object reference
        import memory.param_store as _ps_mod
        _ps_mod.STORE_PATH = _new
        yield ParamStore(config=_MockConfig())
        _ps_mod.STORE_PATH = _orig

    def test_set_and_get_ai_param(self, store):
        store.set_ai_param("leverage_override", 7, ttl_seconds=3600)
        assert store.get_ai_param("leverage_override") == 7

    def test_ai_param_expires(self, store):
        store.set_ai_param("stop_mult_override", 3.5, ttl_seconds=1)
        assert store.get_ai_param("stop_mult_override") == 3.5
        time.sleep(1.1)
        assert store.get_ai_param("stop_mult_override") is None

    def test_ai_param_default_fallback(self, store):
        assert store.get_ai_param("nonexistent_key", default="fallback") == "fallback"

    def test_get_all_ai_params_purges_expired(self, store):
        store.set_ai_param("a", 1, ttl_seconds=3600)
        store.set_ai_param("b", 2, ttl_seconds=1)
        time.sleep(1.1)
        _all = store.get_all_ai_params()
        assert "a" in _all
        assert "b" not in _all

    def test_expire_ai_params_idempotent(self, store):
        store.expire_ai_params()  # no crash when empty
        assert store.get_all_ai_params() == {}


class _MockConfig:
    min_coherence = 2.0


# ══════════════════════════════════════════════════════════════════════════════
# WorldModel
# ══════════════════════════════════════════════════════════════════════════════

class TestWorldModel:
    """Environmental ontology: risk appetite, preferred asset class, vol regime."""

    @pytest.fixture
    def wm(self):
        from intelligence.world_model import WorldModel
        return WorldModel()

    def test_drawdown_risk_appetite_extreme(self, wm):
        state = wm.update(regime="confused", drawdown_pct=0.25)
        assert state.risk_appetite <= 0.15

    def test_drawdown_risk_appetite_mild(self, wm):
        state = wm.update(regime="risk_on", drawdown_pct=0.01)
        assert state.risk_appetite >= 0.60

    def test_regime_risk_off_reduces_appetite(self, wm):
        state = wm.update(regime="risk_off", drawdown_pct=0.01)
        assert state.risk_appetite < 0.50

    def test_preferred_asset_class_alt_season(self, wm):
        state = wm.update(regime="alt_season", drawdown_pct=0.01)
        assert state.preferred_asset_class == "crypto"

    def test_preferred_asset_class_risk_off_gold(self, wm):
        state = wm.update(regime="risk_off", drawdown_pct=0.01, xaut_confirms=True)
        assert state.preferred_asset_class == "commodity"

    def test_volatility_regime_cascade_extreme(self, wm):
        state = wm.update(regime="risk_on", drawdown_pct=0.01,
                          cascade_phase="expansion", cascade_zscore=4.5)
        assert state.volatility_regime == "extreme"

    def test_volatility_regime_normal(self, wm):
        state = wm.update(regime="risk_on", drawdown_pct=0.01,
                          atr_vs_baseline=1.0)
        assert state.volatility_regime == "normal"

    def test_time_quality_block(self, wm):
        state = wm.update(regime="risk_on", drawdown_pct=0.01,
                          calendar_regime="BLOCK")
        assert state.time_quality == 0.0

    def test_time_quality_overlap(self, wm):
        state = wm.update(regime="risk_on", drawdown_pct=0.01,
                          calendar_regime="CLEAR", time_regime="OVERLAP")
        assert state.time_quality == 1.0

    def test_correlation_convergent(self, wm):
        state = wm.update(regime="risk_on", drawdown_pct=0.01,
                          macro_confirmation=0.85)
        assert state.correlation_regime == "convergent"

    def test_correlation_divergent(self, wm):
        state = wm.update(regime="risk_on", drawdown_pct=0.01,
                          cross_market_direction="diverging", cross_market_boost=0.5)
        assert state.correlation_regime == "divergent"

    def test_narrative_non_empty(self, wm):
        state = wm.update(regime="risk_on", drawdown_pct=0.01)
        assert len(state.narrative) > 0
        assert "risk_on" in state.narrative

    def test_portfolio_concentration_guard(self, wm):
        positions = [
            {"symbol": "BTC-USD", "notional": 900, "asset_class": "crypto"},
            {"symbol": "ETH-USD", "notional": 100, "asset_class": "crypto"},
        ]
        state = wm.update(regime="normal", drawdown_pct=0.01, positions=positions)
        assert state.preferred_asset_class == "equity"  # rebalance suggestion


# ══════════════════════════════════════════════════════════════════════════════
# WillEngine
# ══════════════════════════════════════════════════════════════════════════════

class TestWillEngine:
    """Synthesis: Kant x Nietzsche x World = will probability."""

    @pytest.fixture
    def engine(self):
        from intelligence.will_engine import WillEngine
        return WillEngine(param_store=None)

    @pytest.fixture
    def kant_normal(self):
        from intelligence.kant_engine import KantFrame, MarketStructure
        return KantFrame(
            structure=MarketStructure.NORMAL,
            confidence=0.6,
            atr_baseline_min=0.7,
            coherence_min=3.5,
            basis_stress_weight=1.0,
            order_type="limit",
            size_cap=1.0,
            min_notional_adjust=True,
        )

    @pytest.fixture
    def kant_trend(self):
        from intelligence.kant_engine import KantFrame, MarketStructure
        return KantFrame(
            structure=MarketStructure.TREND,
            confidence=0.8,
            atr_baseline_min=0.7,
            coherence_min=4.5,
            basis_stress_weight=1.0,
            order_type="market",
            size_cap=1.25,
            min_notional_adjust=True,
        )

    @pytest.fixture
    def nietzsche_aggressive(self):
        from intelligence.nietzsche_engine import NietzscheOutput, WillState
        return NietzscheOutput(
            will_state=WillState.AGGRESSIVE,
            size_multiplier=1.50,
            order_type="market",
            min_notional_ok=True,
            adjusted_size=150.0,
            reason="test",
        )

    @pytest.fixture
    def nietzsche_dormant(self):
        from intelligence.nietzsche_engine import NietzscheOutput, WillState
        return NietzscheOutput(
            will_state=WillState.DORMANT,
            size_multiplier=0.0,
            order_type="none",
            min_notional_ok=False,
            adjusted_size=0.0,
            reason="test",
        )

    @pytest.fixture
    def world_normal(self):
        from intelligence.world_model import WorldState
        return WorldState(
            risk_appetite=0.5,
            preferred_asset_class="mixed",
            volatility_regime="normal",
            correlation_regime="neutral",
            liquidity_regime="normal",
            time_quality=0.8,
        )

    @pytest.fixture
    def world_defensive(self):
        from intelligence.world_model import WorldState
        return WorldState(
            risk_appetite=0.15,
            preferred_asset_class="commodity",
            volatility_regime="extreme",
            correlation_regime="divergent",
            liquidity_regime="thin",
            time_quality=0.2,
        )

    def test_will_probability_neutral(self, engine, kant_normal, nietzsche_aggressive, world_normal):
        v = engine.compute(kant_normal, nietzsche_aggressive, world_normal,
                           signal_asset_class="crypto", signal_coherence=5.0)
        assert 0.0 < v.will_probability <= 1.0
        assert v.size_scale > 0

    def test_dormant_veto(self, engine, kant_normal, nietzsche_dormant, world_normal):
        v = engine.compute(kant_normal, nietzsche_dormant, world_normal,
                           signal_asset_class="crypto", signal_coherence=5.0)
        assert v.will_probability == 0.0

    def test_defensive_reduces_size(self, engine, kant_normal, nietzsche_aggressive,
                                     world_normal, world_defensive):
        v1 = engine.compute(kant_normal, nietzsche_aggressive, world_normal,
                            signal_asset_class="crypto", signal_coherence=5.0)
        v2 = engine.compute(kant_normal, nietzsche_aggressive, world_defensive,
                            signal_asset_class="crypto", signal_coherence=5.0)
        assert v2.size_scale < v1.size_scale

    def test_preferred_asset_class_boost(self, engine, kant_normal, nietzsche_aggressive, world_normal):
        world_crypto = world_normal
        # world_normal has preferred="mixed" so both get 1.0 boost
        v = engine.compute(kant_normal, nietzsche_aggressive, world_crypto,
                           signal_asset_class="crypto", signal_coherence=5.0)
        assert v.asset_class_boost["crypto"] == 1.0

    def test_non_preferred_asset_penalty(self, engine, kant_normal, nietzsche_aggressive):
        from intelligence.world_model import WorldState
        world_equity = WorldState(
            risk_appetite=0.5,
            preferred_asset_class="equity",
            volatility_regime="normal",
            correlation_regime="neutral",
            liquidity_regime="normal",
            time_quality=0.8,
        )
        v = engine.compute(kant_normal, nietzsche_aggressive, world_equity,
                           signal_asset_class="crypto", signal_coherence=5.0)
        assert v.asset_class_boost["crypto"] == 0.90
        assert v.asset_class_boost["equity"] == 1.10

    def test_extreme_vol_order_override(self, engine, kant_normal, nietzsche_aggressive):
        from intelligence.world_model import WorldState
        world_extreme = WorldState(
            risk_appetite=0.5,
            preferred_asset_class="mixed",
            volatility_regime="extreme",
            correlation_regime="neutral",
            liquidity_regime="normal",
            time_quality=0.8,
        )
        v = engine.compute(kant_normal, nietzsche_aggressive, world_extreme,
                           signal_asset_class="crypto", signal_coherence=5.0)
        assert v.order_type_override == "market"

    def test_thin_liquidity_order_override(self, engine, kant_trend, nietzsche_aggressive):
        from intelligence.world_model import WorldState
        world_thin = WorldState(
            risk_appetite=0.5,
            preferred_asset_class="mixed",
            volatility_regime="normal",
            correlation_regime="neutral",
            liquidity_regime="thin",
            time_quality=0.8,
        )
        v = engine.compute(kant_trend, nietzsche_aggressive, world_thin,
                           signal_asset_class="crypto", signal_coherence=5.0)
        assert v.order_type_override == "limit"

    def test_confidence_override_defensive(self, engine, kant_normal, nietzsche_aggressive):
        from intelligence.world_model import WorldState
        world_low = WorldState(
            risk_appetite=0.15,
            preferred_asset_class="mixed",
            volatility_regime="normal",
            correlation_regime="neutral",
            liquidity_regime="normal",
            time_quality=0.8,
        )
        v = engine.compute(kant_normal, nietzsche_aggressive, world_low,
                           signal_asset_class="crypto", signal_coherence=5.0)
        assert v.confidence_override == 4.5

    def test_kant_cap_respected(self, engine, kant_normal, nietzsche_aggressive):
        from intelligence.world_model import WorldState
        world_aggressive = WorldState(
            risk_appetite=1.0,
            preferred_asset_class="crypto",
            volatility_regime="low",
            correlation_regime="convergent",
            liquidity_regime="deep",
            time_quality=1.0,
        )
        v = engine.compute(kant_normal, nietzsche_aggressive, world_aggressive,
                           signal_asset_class="crypto", signal_coherence=8.0)
        assert v.size_scale <= kant_normal.size_cap

    def test_reason_non_empty(self, engine, kant_normal, nietzsche_aggressive, world_normal):
        v = engine.compute(kant_normal, nietzsche_aggressive, world_normal,
                           signal_asset_class="crypto", signal_coherence=5.0)
        assert len(v.reason) > 0
        assert "will=" in v.reason


# ══════════════════════════════════════════════════════════════════════════════
# P1: build_candidate AI ParamStore overrides
# ══════════════════════════════════════════════════════════════════════════════

class _MockParamStore:
    """Minimal param_store for build_candidate tests."""
    def __init__(self, ai_params=None, stop_mult=2.5):
        self._ai = ai_params or {}
        self._stop_mult = stop_mult

    def get_ai_param(self, key, default=None):
        return self._ai.get(key, default)

    def get_stop_mult(self, symbol):
        return self._stop_mult


class _MockConfigForCandidate:
    ASSET_CONFIG = {
        "BTC-USD": {
            "category": "large_cap",
            "preferred_leverage": 5,
            "max_leverage": 25,
        },
        "AAPL-USD": {
            "category": "equity",
            "preferred_leverage": 5,
            "max_leverage": 10,
        },
    }
    max_notional_usd = 500.0
    min_trade_notional_usd = 100.0
    default_leverage = 5
    max_margin_per_trade_pct = 0.20
    stop_atr_mult = 2.5
    base_trade_usd = 200.0
    small_account_balance_threshold = 150.0
    small_account_max_margin_pct = 0.30

    def effective_base_trade(self, balance, drawdown_pct=0.0, win_streak=0, loss_streak=0):
        return self.base_trade_usd

    def effective_max_margin_pct(self, balance):
        if balance < self.small_account_balance_threshold:
            return self.small_account_max_margin_pct
        return self.max_margin_per_trade_pct


def _make_state(symbol="BTC-USD", direction="long", price=70000.0, atr=200.0, coherence=5.0):
    return types.SimpleNamespace(
        symbol=symbol,
        trade_direction=direction,
        mark_price=price,
        atr=atr,
        coherence_score=coherence,
        drawdown_pct=0.0,
        win_streak=0,
        loss_streak=0,
        session_type="US",
        atr_vs_baseline=1.0,
        timestamp_ms=0,
        signal_age_ms=0,
        macro_bias="long",
        invalidation_reason="",
        size_multiplier=1.0,
    )


class TestBuildCandidateAIParams:
    """P1: AI Fund Manager overrides are consumed by build_candidate."""

    @pytest.fixture
    def cfg(self):
        return _MockConfigForCandidate()

    def test_blacklist_reject(self, cfg):
        from main import build_candidate
        state = _make_state(symbol="BTC-USD")
        ps = _MockParamStore(ai_params={"blacklist": ["BTC-USD"]})
        cand = build_candidate(state, balance=1000, margin_engine=None, config=cfg, param_store=ps)
        assert cand is None

    def test_blacklist_pass(self, cfg):
        from main import build_candidate
        state = _make_state(symbol="ETH-USD")
        ps = _MockParamStore(ai_params={"blacklist": ["BTC-USD"]})
        cand = build_candidate(state, balance=1000, margin_engine=None, config=cfg, param_store=ps)
        assert cand is not None
        assert cand.symbol == "ETH-USD"

    def test_leverage_override_applied(self, cfg):
        from main import build_candidate
        state = _make_state(symbol="BTC-USD")
        ps = _MockParamStore(ai_params={"leverage_override": 7})
        cand = build_candidate(state, balance=1000, margin_engine=None, config=cfg, param_store=ps)
        assert cand is not None
        assert cand.leverage == 7

    def test_leverage_override_clamped_to_max(self, cfg):
        from main import build_candidate
        state = _make_state(symbol="BTC-USD")
        ps = _MockParamStore(ai_params={"leverage_override": 50})
        cand = build_candidate(state, balance=1000, margin_engine=None, config=cfg, param_store=ps)
        assert cand is not None
        assert cand.leverage == 25  # symbol max

    def test_leverage_override_floored(self, cfg):
        from main import build_candidate
        state = _make_state(symbol="BTC-USD")
        ps = _MockParamStore(ai_params={"leverage_override": 2})
        cand = build_candidate(state, balance=1000, margin_engine=None, config=cfg, param_store=ps)
        assert cand is not None
        assert cand.leverage == 5  # floor

    def test_coherence_floor_reject(self, cfg):
        from main import build_candidate
        state = _make_state(symbol="BTC-USD", coherence=3.0)
        ps = _MockParamStore(ai_params={"coherence_floor_override": 4.0})
        cand = build_candidate(state, balance=1000, margin_engine=None, config=cfg, param_store=ps)
        assert cand is None

    def test_coherence_floor_pass(self, cfg):
        from main import build_candidate
        state = _make_state(symbol="BTC-USD", coherence=4.5)
        ps = _MockParamStore(ai_params={"coherence_floor_override": 4.0})
        cand = build_candidate(state, balance=1000, margin_engine=None, config=cfg, param_store=ps)
        assert cand is not None

    def test_atr_min_pct_override_crypto(self, cfg):
        from main import build_candidate
        state = _make_state(symbol="BTC-USD", price=70000.0, atr=50.0)
        # Without override, crypto floor is 1.2% = 840. ATR 50*2.5=125, so floor dominates.
        # With override 0.02 (2%), floor = 1400.
        ps = _MockParamStore(ai_params={"atr_min_pct_override": 0.02})
        cand = build_candidate(state, balance=1000, margin_engine=None, config=cfg, param_store=ps)
        assert cand is not None
        # stop should be entry - 1400 = 68600 (long)
        assert cand.stop_price == pytest.approx(70000 - 1400, rel=1e-3)

    def test_atr_min_pct_override_equity(self, cfg):
        from main import build_candidate
        state = _make_state(symbol="AAPL-USD", price=200.0, atr=2.0)
        # Without override, equity floor is 2.5% = 5.0. ATR 2*2.5=5.0, same.
        # With override 0.05 (5%), floor = 10.0.
        ps = _MockParamStore(ai_params={"atr_min_pct_override": 0.05})
        cand = build_candidate(state, balance=1000, margin_engine=None, config=cfg, param_store=ps)
        assert cand is not None
        assert cand.stop_price == pytest.approx(200 - 10.0, rel=1e-3)


# ══════════════════════════════════════════════════════════════════════════════
# P2: PortfolioAllocator
# ══════════════════════════════════════════════════════════════════════════════

class TestPortfolioAllocator:
    """P2: Asset-class concentration guard."""

    @pytest.fixture
    def cfg(self):
        return _MockConfigForCandidate()

    @pytest.fixture
    def world_crypto(self):
        from intelligence.world_model import WorldState
        return WorldState(
            risk_appetite=0.8,
            preferred_asset_class="crypto",
            volatility_regime="normal",
            correlation_regime="neutral",
            liquidity_regime="normal",
            time_quality=0.8,
        )

    @pytest.fixture
    def world_equity(self):
        from intelligence.world_model import WorldState
        return WorldState(
            risk_appetite=0.8,
            preferred_asset_class="equity",
            volatility_regime="normal",
            correlation_regime="neutral",
            liquidity_regime="normal",
            time_quality=0.8,
        )

    def test_target_weights_crypto_preferred(self, world_crypto):
        from intelligence.portfolio_allocator import PortfolioAllocator
        tw = PortfolioAllocator.target_weights(world_crypto)
        assert tw["crypto"] > tw["equity"]
        assert tw["crypto"] > tw["commodity"]

    def test_target_weights_equity_preferred(self, world_equity):
        from intelligence.portfolio_allocator import PortfolioAllocator
        tw = PortfolioAllocator.target_weights(world_equity)
        assert tw["equity"] > tw["crypto"]
        assert tw["equity"] > tw["commodity"]

    def test_target_weights_risk_appetite_scales_down(self, world_crypto):
        from intelligence.world_model import WorldState
        from intelligence.portfolio_allocator import PortfolioAllocator
        world_low = WorldState(
            risk_appetite=0.10,
            preferred_asset_class="crypto",
            volatility_regime="normal",
            correlation_regime="neutral",
            liquidity_regime="normal",
            time_quality=0.8,
        )
        tw_high = PortfolioAllocator.target_weights(world_crypto)
        tw_low = PortfolioAllocator.target_weights(world_low)
        assert tw_low["crypto"] < tw_high["crypto"]

    def test_current_weights_empty(self, cfg):
        from intelligence.portfolio_allocator import PortfolioAllocator
        cw = PortfolioAllocator.current_weights([], balance=1000, config=cfg)
        assert cw == {"crypto": 0.0, "equity": 0.0, "commodity": 0.0}

    def test_current_weights_with_positions(self, cfg):
        from intelligence.portfolio_allocator import PortfolioAllocator
        positions = [
            types.SimpleNamespace(symbol="BTC-USD", size=0.01, entry_price=70000.0),
            types.SimpleNamespace(symbol="AAPL-USD", size=1.0, entry_price=200.0),
        ]
        cw = PortfolioAllocator.current_weights(positions, balance=1000, config=cfg)
        # BTC notional = 700, equity = 200
        assert cw["crypto"] == pytest.approx(0.70, abs=0.01)
        assert cw["equity"] == pytest.approx(0.20, abs=0.01)

    def test_check_candidate_allowed(self, cfg, world_crypto):
        from intelligence.portfolio_allocator import PortfolioAllocator
        candidate = types.SimpleNamespace(symbol="BTC-USD", size=0.001, entry_price=70000.0)
        ok, reason, _ = PortfolioAllocator.check_candidate(
            candidate, positions=[], balance=10000, world_state=world_crypto, config=cfg
        )
        assert ok is True
        assert reason == "ok"

    def test_check_candidate_rejected_concentration(self, cfg, world_crypto):
        from intelligence.portfolio_allocator import PortfolioAllocator
        # Already 75% in crypto (7500/10000). Target is 0.70 + 0.05 tolerance = 0.75.
        # Adding another 1000 notional would push to 0.85 → reject.
        positions = [
            types.SimpleNamespace(symbol="BTC-USD", size=0.1, entry_price=75000.0),
        ]
        candidate = types.SimpleNamespace(symbol="ETH-USD", size=0.01, entry_price=4000.0)
        ok, reason, _ = PortfolioAllocator.check_candidate(
            candidate, positions=positions, balance=10000, world_state=world_crypto, config=cfg
        )
        assert ok is False
        assert "exceed" in reason
