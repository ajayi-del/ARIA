"""
tests/test_regime_engine.py — Unit tests for intelligence/regime_engine.py

Test fixtures sourced from live ARIA logs (2026-04-22):
  21:15:52  geopolitical_stress conf=0.75  — BTC long position open
  21:23:53  cex_flow conf=0.6              — ETH short coherence 8.21
  21:24:52  XAUT short coherence 3.89      — thermometer update
  21:26:33  transitioning, lagging=unknown — broken structure
  21:40:38  BTC short coherence 11.19      — vs open BTC long
  21:44:25  time_regime_mult=0.0           — earnings block
  21:45:49  ARB coherence 3.16             — below action threshold
  21:49:13  BTC size_mult=0.9              — recovery haircut
"""

import pytest
from intelligence.regime_engine import (
    RegimeMultiplierEngine,
    XAUTThermometer,
    AutoAdjustmentEngine,
)
from intelligence.relative_strength import RegimeState


# ── Fixture helpers ────────────────────────────────────────────────────────────

def _rs(
    regime: str,
    leading: str = "large_cap",
    lagging: str = "alt_l1",
    confidence: float = 0.7,
    dispersion: float = 0.003,
) -> RegimeState:
    return RegimeState(
        regime=regime,
        leading_category=leading,
        lagging_category=lagging,
        confidence=confidence,
        dispersion=dispersion,
    )


# ── RegimeMultiplierEngine ─────────────────────────────────────────────────────

class TestRegimeMultiplierEngine:
    def setup_method(self):
        self.eng = RegimeMultiplierEngine()

    # ── 21:15:52 geopolitical_stress ──────────────────────────────────────────

    def test_geo_stress_btc_locked(self):
        rs = _rs("geopolitical_stress", "commodity_energy", "large_cap", 0.75)
        assert self.eng.get_new_entry_multiplier("BTC-USD", rs) == 0.0

    def test_geo_stress_eth_locked(self):
        rs = _rs("geopolitical_stress", "commodity_energy", "large_cap", 0.75)
        assert self.eng.get_new_entry_multiplier("ETH-USD", rs) == 0.0

    def test_geo_stress_sol_locked(self):
        rs = _rs("geopolitical_stress", "commodity_energy", "large_cap", 0.75)
        assert self.eng.get_new_entry_multiplier("SOL-USD", rs) == 0.0

    def test_geo_stress_energy_boosted(self):
        rs = _rs("geopolitical_stress", "commodity_energy", "large_cap", 0.75)
        assert self.eng.get_new_entry_multiplier("CL-USD", rs) == 1.5

    def test_geo_stress_xaut_rotational_bypass(self):
        rs = _rs("geopolitical_stress", "commodity_energy", "large_cap", 0.75)
        assert self.eng.get_new_entry_multiplier("XAUT-USD", rs) == 1.0

    # ── 21:23:53 cex_flow ─────────────────────────────────────────────────────

    def test_cex_flow_bnb_leading(self):
        rs = _rs("cex_flow", "cex_ecosystem", "alt_l1", 0.6)
        assert self.eng.get_new_entry_multiplier("BNB-USD", rs) == 1.2

    def test_cex_flow_sol_alt_l1_amplified(self):
        rs = _rs("cex_flow", "cex_ecosystem", "alt_l1", 0.6)
        assert self.eng.get_new_entry_multiplier("SOL-USD", rs) == 1.1

    def test_cex_flow_eth_reduced(self):
        rs = _rs("cex_flow", "cex_ecosystem", "alt_l1", 0.6)
        assert self.eng.get_new_entry_multiplier("ETH-USD", rs) == 0.8

    def test_cex_flow_btc_reduced(self):
        rs = _rs("cex_flow", "cex_ecosystem", "alt_l1", 0.6)
        assert self.eng.get_new_entry_multiplier("BTC-USD", rs) == 0.8

    # ── 21:26:33 transitioning / unknown ─────────────────────────────────────

    def test_transitioning_unknown_lagging_extreme_caution(self):
        rs = _rs("transitioning", "meme", "unknown", 0.3)
        assert self.eng.get_new_entry_multiplier("BTC-USD", rs) == 0.25

    def test_transitioning_none_leading_extreme_caution(self):
        rs = _rs("transitioning", "none", "alt_l1", 0.3)
        assert self.eng.get_new_entry_multiplier("SOL-USD", rs) == 0.25

    def test_transitioning_low_conf(self):
        rs = _rs("transitioning", "alt_l1", "large_cap", 0.4)
        assert self.eng.get_new_entry_multiplier("BTC-USD", rs) == 0.5

    def test_transitioning_higher_conf(self):
        rs = _rs("transitioning", "alt_l1", "large_cap", 0.6)
        assert self.eng.get_new_entry_multiplier("BTC-USD", rs) == 0.75

    # ── risk_off ─────────────────────────────────────────────────────────────

    def test_risk_off_xaut_boosted(self):
        rs = _rs("risk_off", "commodity_precious", "large_cap", 0.8)
        assert self.eng.get_new_entry_multiplier("XAUT-USD", rs) == 1.3

    def test_risk_off_crypto_reduced(self):
        rs = _rs("risk_off", "commodity_precious", "large_cap", 0.8)
        assert self.eng.get_new_entry_multiplier("BTC-USD", rs) == 0.5

    def test_risk_off_low_conf_no_override(self):
        rs = _rs("risk_off", "commodity_precious", "large_cap", 0.5)
        # Below 0.7 threshold — no risk_off override; general lagging rule fires:
        # BTC (large_cap) == lagging, conf=0.5 ≥ 0.5 → 0.7× penalty
        assert self.eng.get_new_entry_multiplier("BTC-USD", rs) == pytest.approx(0.7)

    # ── alt_season ────────────────────────────────────────────────────────────

    def test_alt_season_alts_boosted(self):
        rs = _rs("alt_season", "alt_l1", "large_cap", 0.7)
        assert self.eng.get_new_entry_multiplier("SOL-USD", rs) == 1.2

    def test_alt_season_btc_reduced(self):
        rs = _rs("alt_season", "alt_l1", "large_cap", 0.7)
        assert self.eng.get_new_entry_multiplier("BTC-USD", rs) == 0.8

    # ── btc_dominance ─────────────────────────────────────────────────────────

    def test_btc_dominance_btc_boosted(self):
        rs = _rs("btc_dominance", "large_cap", "alt_l1", 0.7)
        assert self.eng.get_new_entry_multiplier("BTC-USD", rs) == 1.2

    def test_btc_dominance_alts_reduced(self):
        rs = _rs("btc_dominance", "large_cap", "alt_l1", 0.7)
        assert self.eng.get_new_entry_multiplier("SOL-USD", rs) == 0.7

    # ── confused ─────────────────────────────────────────────────────────────

    def test_confused_all_reduced(self):
        rs = _rs("confused", "none", "none", 0.1)
        assert self.eng.get_new_entry_multiplier("BTC-USD", rs) == 0.5

    # ── stagflation_fear ─────────────────────────────────────────────────────

    def test_stagflation_xaut_max(self):
        rs = _rs("stagflation_fear", "commodity_precious", "commodity_industrial", 0.7)
        assert self.eng.get_new_entry_multiplier("XAUT-USD", rs) == 1.5

    def test_stagflation_crypto_locked(self):
        rs = _rs("stagflation_fear", "commodity_precious", "commodity_industrial", 0.7)
        assert self.eng.get_new_entry_multiplier("ETH-USD", rs) == 0.0

    # ── default ──────────────────────────────────────────────────────────────

    def test_risk_on_no_override(self):
        rs = _rs("risk_on", "large_cap", "commodity_precious", 0.75)
        assert self.eng.get_new_entry_multiplier("BTC-USD", rs) == 1.2

    def test_unknown_regime_neutral(self):
        rs = _rs("some_new_regime", "large_cap", "alt_l1", 0.3)
        # Low confidence — no bias applied
        assert self.eng.get_new_entry_multiplier("BTC-USD", rs) == 1.0


# ── XAUTThermometer ───────────────────────────────────────────────────────────

class TestXAUTThermometer:
    def setup_method(self):
        self.therm = XAUTThermometer()

    # ── 21:24:52 XAUT short coherence 3.89 ───────────────────────────────────

    def test_xaut_short_amplifies_crypto_longs(self):
        self.therm.update("short", 3.89)
        assert self.therm.get_crypto_multiplier("long", "BTC-USD") == pytest.approx(1.10)

    def test_xaut_short_reduces_crypto_shorts(self):
        self.therm.update("short", 3.89)
        assert self.therm.get_crypto_multiplier("short", "ETH-USD") == pytest.approx(0.90)

    def test_xaut_long_reduces_crypto_longs(self):
        self.therm.update("long", 4.2)
        assert self.therm.get_crypto_multiplier("long", "SOL-USD") == pytest.approx(0.90)

    def test_xaut_long_amplifies_crypto_shorts(self):
        self.therm.update("long", 4.2)
        assert self.therm.get_crypto_multiplier("short", "BTC-USD") == pytest.approx(1.10)

    def test_low_coherence_thermometer_off(self):
        self.therm.update("short", 1.2)
        assert self.therm.get_crypto_multiplier("long", "SOL-USD") == pytest.approx(1.0)

    def test_coherence_between_1_5_and_3_5_neutral(self):
        self.therm.update("short", 2.5)
        assert self.therm.get_crypto_multiplier("long", "BTC-USD") == pytest.approx(1.0)

    def test_xaut_bypasses_itself(self):
        self.therm.update("short", 5.0)
        assert self.therm.get_crypto_multiplier("long", "XAUT-USD") == pytest.approx(1.0)

    def test_commodity_bypassed(self):
        self.therm.update("short", 5.0)
        assert self.therm.get_crypto_multiplier("long", "CL-USD") == pytest.approx(1.0)

    def test_is_active_high_coherence(self):
        self.therm.update("short", 4.0)
        assert self.therm.is_active is True

    def test_is_active_low_coherence(self):
        self.therm.update("short", 1.0)
        assert self.therm.is_active is False

    def test_all_crypto_assets_affected(self):
        self.therm.update("short", 4.0)
        for sym in ["BTC-USD", "ETH-USD", "SOL-USD", "BNB-USD", "LINK-USD", "AVAX-USD"]:
            mult = self.therm.get_crypto_multiplier("long", sym)
            assert mult == pytest.approx(1.10), f"{sym} should be 1.10× but got {mult}"


# ── AutoAdjustmentEngine ──────────────────────────────────────────────────────

class TestAutoAdjustmentEngine:
    def setup_method(self):
        self.eng = AutoAdjustmentEngine()
        self._rs_ok = _rs("risk_on", "large_cap", "alt_l1", 0.7)

    # ── 21:40:38 BTC short coherence 11.19 vs BTC long ───────────────────────

    def test_full_conflict_high_coh_closes_full(self):
        dec = self.eng.evaluate(
            symbol="BTC-USD",
            signal_direction="short",
            coherence=11.19,
            open_position_side="long",
            cascade_phase="idle",
            cascade_zscore=0.0,
            regime_state=self._rs_ok,
            time_regime_mult=1.0,
            size_mult=1.0,   # W_rec=1.0 → W_c(1.0) × W_cas(1.0) × W_rec(1.0) = 1.0 → close_full
        )
        assert dec.action == "close_full"
        assert dec.close_pct == pytest.approx(1.0)

    def test_medium_coh_partial_close(self):
        dec = self.eng.evaluate(
            symbol="ETH-USD",
            signal_direction="short",
            coherence=8.21,   # 21:23:53 ETH coherence 8.21
            open_position_side="long",
            cascade_phase="idle",
            cascade_zscore=0.0,
            regime_state=self._rs_ok,
            time_regime_mult=1.0,
            size_mult=0.925,
        )
        # W_c=0.75, W_cas=1.0, W_rec=0.75 → final=0.5625 → close_pct
        assert dec.action == "close_pct"
        assert 0.4 <= dec.close_pct <= 0.7

    # ── 21:44:25 time_regime_mult=0.0 → earnings block ───────────────────────

    def test_kent_calendar_block_halts(self):
        dec = self.eng.evaluate(
            symbol="BTC-USD",
            signal_direction="short",
            coherence=11.19,
            open_position_side="long",
            cascade_phase="idle",
            cascade_zscore=0.0,
            regime_state=self._rs_ok,
            time_regime_mult=0.0,
            size_mult=0.9,
        )
        assert dec.action == "none"
        assert dec.reason == "kent_calendar_block"

    # ── 21:26:33 lagging=unknown → no structure ───────────────────────────────

    def test_kent_unknown_structure_halts(self):
        rs_broken = _rs("transitioning", "meme", "unknown", 0.3)
        dec = self.eng.evaluate(
            symbol="BTC-USD",
            signal_direction="short",
            coherence=11.19,
            open_position_side="long",
            cascade_phase="idle",
            cascade_zscore=0.0,
            regime_state=rs_broken,
            time_regime_mult=1.0,
            size_mult=0.9,
        )
        assert dec.action == "none"
        assert dec.reason == "kent_no_structure"

    # ── 21:45:49 ARB coherence 3.16 → below threshold ────────────────────────

    def test_low_coherence_no_action(self):
        dec = self.eng.evaluate(
            symbol="ARB-USD",
            signal_direction="short",
            coherence=3.16,
            open_position_side="long",
            cascade_phase="idle",
            cascade_zscore=0.0,
            regime_state=self._rs_ok,
            time_regime_mult=1.0,
            size_mult=1.0,
        )
        assert dec.action == "none"

    # ── 21:29:51 cascade expansion zscore=3.09 → W_cas=1.5 ───────────────────

    def test_cascade_expansion_multiplies_urgency(self):
        dec = self.eng.evaluate(
            symbol="ETH-USD",
            signal_direction="short",
            coherence=9.75,
            open_position_side="long",
            cascade_phase="expansion",
            cascade_zscore=3.09,
            regime_state=self._rs_ok,
            time_regime_mult=1.0,
            size_mult=1.0,
        )
        # W_c=1.0, W_cas=1.5, W_rec=1.0 → capped at 1.5 → close_full
        assert dec.action == "close_full"
        assert dec.multiplier == pytest.approx(1.5)

    # ── 21:49:13 BTC size_mult=0.9 → W_rec=0.75 ─────────────────────────────

    def test_recovery_reduces_w_rec(self):
        dec = self.eng.evaluate(
            symbol="BTC-USD",
            signal_direction="short",
            coherence=9.75,
            open_position_side="long",
            cascade_phase="idle",
            cascade_zscore=0.0,
            regime_state=self._rs_ok,
            time_regime_mult=1.0,
            size_mult=0.9,
        )
        # W_c=1.0, W_cas=1.0, W_rec=0.75 → final=0.75 → close_pct
        assert dec.action == "close_pct"
        assert dec.multiplier == pytest.approx(0.75)

    def test_aligned_direction_no_conflict(self):
        dec = self.eng.evaluate(
            symbol="BTC-USD",
            signal_direction="long",
            coherence=11.0,
            open_position_side="long",
            cascade_phase="idle",
            cascade_zscore=0.0,
            regime_state=self._rs_ok,
            time_regime_mult=1.0,
            size_mult=1.0,
        )
        assert dec.action == "none"

    def test_flip_cooldown_blocks_rapid_second_flip(self):
        # First flip fires
        dec1 = self.eng.evaluate(
            symbol="BTC-USD",
            signal_direction="short",
            coherence=9.5,
            open_position_side="long",
            cascade_phase="idle",
            cascade_zscore=0.0,
            regime_state=self._rs_ok,
            time_regime_mult=1.0,
            size_mult=0.9,
        )
        assert dec1.action != "none"

        # Immediate second call on same symbol — cooldown blocks it
        dec2 = self.eng.evaluate(
            symbol="BTC-USD",
            signal_direction="short",
            coherence=9.5,
            open_position_side="long",
            cascade_phase="idle",
            cascade_zscore=0.0,
            regime_state=self._rs_ok,
            time_regime_mult=1.0,
            size_mult=0.9,
        )
        assert dec2.action == "none"
        assert "cooldown" in dec2.reason

    def test_can_enter_after_adjustment_false_immediately(self):
        self.eng._last_adj_ts["ETH-USD"] = __import__("time").time()
        assert self.eng.can_enter_after_adjustment("ETH-USD") is False

    def test_can_enter_after_adjustment_true_when_fresh(self):
        assert self.eng.can_enter_after_adjustment("LINK-USD") is True

    def test_deep_recovery_size_mult_halts(self):
        dec = self.eng.evaluate(
            symbol="BTC-USD",
            signal_direction="short",
            coherence=11.0,
            open_position_side="long",
            cascade_phase="idle",
            cascade_zscore=0.0,
            regime_state=self._rs_ok,
            time_regime_mult=1.0,
            size_mult=0.85,   # below 0.9 floor
        )
        assert dec.action == "none"
        assert dec.reason == "kent_recovery_too_deep"

