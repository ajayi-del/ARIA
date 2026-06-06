"""
tests/test_ai_fund_manager.py — Phase 3: AI Fund Manager / World Model / Will Engine

Covers:
  - ParamStore AI-writable TTL parameters
  - WorldModel environmental classification
  - WillEngine synthesis (Kant x Nietzsche x World)
  - Will veto and size modulation
  - Asset class boost mapping
"""

import sys
import os
import time

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
