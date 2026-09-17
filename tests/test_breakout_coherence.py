"""Tests for intelligence/breakout_coherence.py — 4-pillar coherence scorer."""
import math

import pytest

from intelligence.breakout_coherence import (
    CoherenceInputs,
    OVERRIDE_THRESHOLDS,
    classify_macro_regime,
    compute_coherence,
    override_enabled,
    parkinson_hv,
    rv_rank,
    shadow_enabled,
    veto_override,
)


# ── Pillar 1 — positioning structure ─────────────────────────────────────────

def test_positioning_squeeze_setup():
    inp = CoherenceInputs(funding_rate=-0.0005, funding_avg=0.0005, oi_delta_24h_pct=12.0)
    res = compute_coherence("UNI-USD", inp)
    assert res.pillars["positioning"] == 2.5


def test_positioning_momentum_setup():
    inp = CoherenceInputs(funding_rate=0.002, funding_avg=0.001,
                          oi_delta_24h_pct=5.0, whale_ls=1.4)
    res = compute_coherence("X-USD", inp)
    assert res.pillars["positioning"] == 2.0


def test_positioning_momentum_needs_whale_ls():
    # Same funding/OI but whale_ls abstains → momentum branch cannot fire
    inp = CoherenceInputs(funding_rate=0.002, funding_avg=0.001,
                          oi_delta_24h_pct=5.0, whale_ls=None)
    res = compute_coherence("X-USD", inp)
    assert res.pillars["positioning"] == 0.5


def test_positioning_whale_led():
    inp = CoherenceInputs(funding_rate=0.001, funding_avg=0.001,
                          oi_delta_24h_pct=1.0, whale_ls=1.7)
    res = compute_coherence("X-USD", inp)
    assert res.pillars["positioning"] == 1.5


def test_positioning_flat_default():
    inp = CoherenceInputs(funding_rate=0.001, funding_avg=0.001,
                          oi_delta_24h_pct=0.0, whale_ls=1.0)
    res = compute_coherence("X-USD", inp)
    assert res.pillars["positioning"] == 0.5


def test_positioning_abstains_on_missing_core():
    assert compute_coherence("X", CoherenceInputs(funding_avg=0.001,
                 oi_delta_24h_pct=5.0)).pillars["positioning"] is None
    assert compute_coherence("X", CoherenceInputs(funding_rate=0.001,
                 oi_delta_24h_pct=5.0)).pillars["positioning"] is None
    assert compute_coherence("X", CoherenceInputs(funding_rate=0.001,
                 funding_avg=0.001)).pillars["positioning"] is None


# ── Pillar 2 — narrative interface ───────────────────────────────────────────

def test_narrative_none_abstains():
    res = compute_coherence("X", CoherenceInputs(narrative_score=None))
    assert res.pillars["narrative"] is None


def test_narrative_passthrough_clamped():
    res = compute_coherence("X", CoherenceInputs(narrative_score=2.2))
    assert res.pillars["narrative"] == pytest.approx(2.2)
    res2 = compute_coherence("X", CoherenceInputs(narrative_score=9.9))
    assert res2.pillars["narrative"] == pytest.approx(2.5)


# ── Pillar 3 — volatility regime ─────────────────────────────────────────────

def test_vol_coiled():
    inp = CoherenceInputs(parkinson_hv=0.55, rv_rank=20.0)
    assert compute_coherence("X", inp).pillars["volatility"] == 2.5


def test_vol_mid_band():
    inp = CoherenceInputs(parkinson_hv=0.80, rv_rank=40.0)
    assert compute_coherence("X", inp).pillars["volatility"] == 2.0


def test_vol_compressed():
    inp = CoherenceInputs(parkinson_hv=0.95, rv_rank=50.0)
    assert compute_coherence("X", inp).pillars["volatility"] == 1.8


def test_vol_exhausted():
    inp = CoherenceInputs(parkinson_hv=1.40, rv_rank=85.0)
    assert compute_coherence("X", inp).pillars["volatility"] == 0.5


def test_vol_neutral_else():
    inp = CoherenceInputs(parkinson_hv=1.10, rv_rank=50.0)
    assert compute_coherence("X", inp).pillars["volatility"] == 1.2


def test_vol_abstains_on_missing():
    assert compute_coherence("X", CoherenceInputs(rv_rank=40.0)).pillars["volatility"] is None
    assert compute_coherence("X", CoherenceInputs(parkinson_hv=0.8)).pillars["volatility"] is None


# ── Pillar 4 — cross-asset ───────────────────────────────────────────────────

def test_cross_bifurcated_with_movers():
    inp = CoherenceInputs(macro_regime="BIFURCATED", sector_movers_up=3)
    assert compute_coherence("X", inp).pillars["cross_asset"] == 2.5


def test_cross_risk_on_broad():
    inp = CoherenceInputs(macro_regime="RISK_ON", sector_movers_up=5)
    assert compute_coherence("X", inp).pillars["cross_asset"] == 2.0


def test_cross_risk_off_dead():
    inp = CoherenceInputs(macro_regime="RISK_OFF", sector_movers_up=1)
    assert compute_coherence("X", inp).pillars["cross_asset"] == 0.5


def test_cross_else_neutral():
    inp = CoherenceInputs(macro_regime="RISK_ON", sector_movers_up=2)
    assert compute_coherence("X", inp).pillars["cross_asset"] == 1.2


def test_cross_abstains_on_missing():
    assert compute_coherence("X", CoherenceInputs(macro_regime="RISK_ON")).pillars["cross_asset"] is None


# ── Abstain + renormalization math ────────────────────────────────────────────

def test_renormalization_scales_to_ten():
    # All four maxed → 10.0
    inp = CoherenceInputs(
        funding_rate=-0.0005, funding_avg=0.0005, oi_delta_24h_pct=12.0,
        narrative_score=2.5, parkinson_hv=0.55, rv_rank=20.0,
        macro_regime="BIFURCATED", sector_movers_up=4)
    res = compute_coherence("X", inp)
    assert res.score == pytest.approx(10.0)
    assert res.regime == "BREAKOUT_IMMINENT"


def test_renormalization_with_abstain():
    # Two pillars present at 2.5 of 2.5 each → 10 * 5.0/5.0 = 10.0
    inp = CoherenceInputs(
        funding_rate=-0.0005, funding_avg=0.0005, oi_delta_24h_pct=12.0,
        parkinson_hv=0.55, rv_rank=20.0)
    res = compute_coherence("X", inp)
    assert res.pillars["narrative"] is None
    assert res.pillars["cross_asset"] is None
    assert res.score == pytest.approx(10.0)


def test_renormalization_partial_present():
    # positioning 0.5 alone → 10 * 0.5/2.5 = 2.0
    inp = CoherenceInputs(funding_rate=0.001, funding_avg=0.001, oi_delta_24h_pct=0.0)
    res = compute_coherence("X", inp)
    assert res.score == pytest.approx(2.0)
    assert res.regime == "NOISE"


def test_all_abstain_zero_noise():
    res = compute_coherence("X", CoherenceInputs())
    assert res.score == 0.0
    assert res.regime == "NOISE"
    assert res.dominant_driver is None


def test_dominant_driver():
    inp = CoherenceInputs(parkinson_hv=0.55, rv_rank=20.0,
                          macro_regime="RISK_OFF", sector_movers_up=1)
    res = compute_coherence("X", inp)
    assert res.dominant_driver == "volatility"


# ── Macro regime classifier quadrants ─────────────────────────────────────────

def test_macro_bifurcated_majors_weak_alts_decoupling():
    # Today's exact regime: btc/eth red, narrative alts decoupling
    assert classify_macro_regime(-1.2, -1.5, 7, None) == "BIFURCATED"


def test_macro_bifurcated_via_etf_opposed():
    assert classify_macro_regime(0.1, 0.2, 6, "opposed") == "BIFURCATED"


def test_macro_risk_on():
    assert classify_macro_regime(1.5, 2.0, 8, None) == "RISK_ON"


def test_macro_risk_off():
    assert classify_macro_regime(-1.5, -2.0, 2, None) == "RISK_OFF"


def test_macro_conservative_defaults():
    # Majors weak but no alt breadth → not bifurcated; btc down + breadth < 5 → RISK_OFF
    assert classify_macro_regime(-1.0, -1.0, 3, None) == "RISK_OFF"
    # Flat tape → conservative RISK_OFF, never a free RISK_ON bonus
    assert classify_macro_regime(0.0, 0.0, 0, None) == "RISK_OFF"
    # Missing inputs degrade safe
    assert classify_macro_regime(None, None, None, None) == "RISK_OFF"
    assert classify_macro_regime("junk", None, None, None) == "RISK_OFF"


# ── Regime bands ──────────────────────────────────────────────────────────────

def _res_with_single_pillar(value: float):
    # narrative passthrough lets us set an exact pillar score
    return compute_coherence("X", CoherenceInputs(narrative_score=value))


def test_regime_bands():
    # single pillar at v → score = 10*v/2.5 = 4v
    assert _res_with_single_pillar(2.5).regime == "BREAKOUT_IMMINENT"      # 10.0
    assert _res_with_single_pillar(2.0).regime == "BREAKOUT_IMMINENT"      # 8.0
    assert _res_with_single_pillar(1.7).regime == "HIGH_COHERENCE_ENTRY"   # 6.8
    assert _res_with_single_pillar(1.3).regime == "MODERATE_WATCH"         # 5.2
    assert _res_with_single_pillar(0.9).regime == "LOW_SIGNAL"             # 3.6
    assert _res_with_single_pillar(0.5).regime == "NOISE"                  # 2.0


# ── Veto override map ─────────────────────────────────────────────────────────

def test_override_listed_vetoes():
    assert veto_override("quiet_market_pause", 6.5) is True
    assert veto_override("quiet_market_pause", 6.4) is False
    assert veto_override("low_winrate_regime", 7.0) is True
    assert veto_override("low_winrate_regime", 6.9) is False
    assert veto_override("coherence_decay", 7.5) is True
    assert veto_override("coherence_decay", 7.4) is False


def test_override_unlisted_veto_never_overridden():
    # Hard risk limits are never listed → never overridden, even at 10.0
    for veto in ("daily_loss_limit", "max_leverage", "liquidation_proximity",
                 "htf_counter_trend", "cascade_counter_direction", "unknown"):
        assert veto_override(veto, 10.0) is False


def test_override_bad_inputs():
    assert veto_override(None, 9.0) is False
    assert veto_override("quiet_market_pause", None) is False
    assert veto_override("quiet_market_pause", "junk") is False


def test_override_thresholds_exact_keys():
    assert set(OVERRIDE_THRESHOLDS) == {
        "quiet_market_pause", "low_winrate_regime", "coherence_decay"}


# ── parkinson_hv + rv_rank math ───────────────────────────────────────────────

def test_parkinson_hv_flat_range_zero():
    # h == l on every bar → zero log-range → zero vol
    hv = parkinson_hv([100.0] * 20, [100.0] * 20, [100.0] * 20)
    assert hv == pytest.approx(0.0)


def test_parkinson_hv_known_value():
    # Constant 2% high-low range, hourly bars (default 8760/yr)
    highs = [102.0] * 48
    lows = [100.0] * 48
    closes = [101.0] * 48
    hv = parkinson_hv(highs, lows, closes)
    r = math.log(1.02)
    expected = math.sqrt((r * r) / (4.0 * math.log(2.0)) * 8760)
    assert hv == pytest.approx(expected, rel=1e-9)
    assert hv == pytest.approx(1.113, rel=1e-2)


def test_parkinson_hv_fail_silent():
    assert parkinson_hv([], [], []) is None
    assert parkinson_hv([100.0], [99.0], [99.5]) is None            # <2 bars
    assert parkinson_hv([0.0, 1.0], [0.0, 1.0], [1.0, 1.0]) is None  # non-positive
    assert parkinson_hv([100.0, None], [99.0, 98.0], [1.0, 1.0]) is None


def test_rv_rank_math():
    series = list(range(1, 21))          # 1..20
    assert rv_rank(series, 10) == pytest.approx(50.0)
    assert rv_rank(series, 20) == pytest.approx(100.0)
    assert rv_rank(series, 0) == pytest.approx(0.0)


def test_rv_rank_thin_history_abstains():
    assert rv_rank([1.0] * 5, 1.0) is None
    assert rv_rank([], 1.0) is None
    assert rv_rank([1.0] * 20, None) is None


# ── Kill-switch helpers (defaults: shadow ON, override OFF) ──────────────────

def test_kill_switch_helpers_fail_silent():
    assert isinstance(shadow_enabled(), bool)
    assert isinstance(override_enabled(), bool)


# ── Reconstructed UNI case (2026-09-17) ───────────────────────────────────────

def test_uni_reconstructed_case_breakout_imminent_overrides_quiet_pause():
    """UNI +17.49% day: squeeze positioning (2.5), no news feed (None),
    coiling vol in the mid band (2.0), bifurcated cross-asset (2.5).
    Score = 10 * (7.0 / 7.5) = 9.33 → BREAKOUT_IMMINENT, and the stale
    quiet_market_pause veto (threshold 6.5) is overridden."""
    macro = classify_macro_regime(
        btc_day_move_pct=-1.1, eth_day_move_pct=-1.4,
        alt_breadth_up=7, etf_tide=None)
    assert macro == "BIFURCATED"

    inp = CoherenceInputs(
        funding_rate=-0.0008, funding_avg=0.0006,   # funding < 0.7*avg
        oi_delta_24h_pct=14.0,                       # OI expanding > +10%
        whale_ls=None,                               # no market-wide L/S plane
        narrative_score=None,                        # news plane unbuilt
        parkinson_hv=0.80, rv_rank=40.0,             # coiling mid band → 2.0
        macro_regime=macro, sector_movers_up=4,
    )
    res = compute_coherence("UNI-USD", inp)

    assert res.pillars["positioning"] == 2.5
    assert res.pillars["narrative"] is None
    assert res.pillars["volatility"] == 2.0
    assert res.pillars["cross_asset"] == 2.5
    assert res.score == pytest.approx(10.0 * 7.0 / 7.5, abs=1e-3)   # 9.333
    assert res.regime == "BREAKOUT_IMMINENT"
    assert veto_override("quiet_market_pause", res.score) is True
