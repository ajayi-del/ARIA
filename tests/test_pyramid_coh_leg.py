"""Pins for intelligence/pyramid_coh_leg — the pyramid coh warmup-leg repair.

Covers: real-plane passthrough, measured-state rv_rank fallback (freshness,
range, duck-typing), all-None abstention, partial-plane renormalization,
degenerate inputs, boundary values, and the pass/fail/dark verdict helper.
"""
from types import SimpleNamespace

from intelligence import breakout_coherence as bc
from intelligence import pyramid_coh_leg as pcl


# ── measured_rv_rank: the measured-state fallback ────────────────────────────

def test_measured_rv_rank_fresh_state():
    st = SimpleNamespace(volatility_percentile=0.42)
    assert pcl.measured_rv_rank(st, 60.0) == 42.0


def test_measured_rv_rank_no_state():
    assert pcl.measured_rv_rank(None, 60.0) is None


def test_measured_rv_rank_unknown_age_abstains():
    st = SimpleNamespace(volatility_percentile=0.42)
    assert pcl.measured_rv_rank(st, None) is None


def test_measured_rv_rank_stale_state_abstains():
    st = SimpleNamespace(volatility_percentile=0.42)
    assert pcl.measured_rv_rank(st, pcl.MEASURED_STATE_MAX_AGE_S + 1.0) is None


def test_measured_rv_rank_boundary_age_inclusive():
    st = SimpleNamespace(volatility_percentile=0.42)
    assert pcl.measured_rv_rank(st, pcl.MEASURED_STATE_MAX_AGE_S) == 42.0


def test_measured_rv_rank_negative_age_abstains():
    st = SimpleNamespace(volatility_percentile=0.42)
    assert pcl.measured_rv_rank(st, -0.5) is None


def test_measured_rv_rank_percentile_boundaries_valid():
    lo = SimpleNamespace(volatility_percentile=0.0)
    hi = SimpleNamespace(volatility_percentile=1.0)
    assert pcl.measured_rv_rank(lo, 10.0) == 0.0
    assert pcl.measured_rv_rank(hi, 10.0) == 100.0


def test_measured_rv_rank_out_of_range_percentile_abstains():
    over = SimpleNamespace(volatility_percentile=1.5)
    under = SimpleNamespace(volatility_percentile=-0.2)
    assert pcl.measured_rv_rank(over, 10.0) is None
    assert pcl.measured_rv_rank(under, 10.0) is None


def test_measured_rv_rank_missing_attribute_abstains():
    st = SimpleNamespace()  # no volatility_percentile
    assert pcl.measured_rv_rank(st, 10.0) is None


def test_measured_rv_rank_non_numeric_abstains():
    st = SimpleNamespace(volatility_percentile="high")
    assert pcl.measured_rv_rank(st, 10.0) is None


# ── build_inputs: assembly + fallback semantics ──────────────────────────────

def test_build_inputs_passthrough_real_planes():
    inp = pcl.build_inputs(
        "SOL-USD", funding_rate=0.0001, funding_avg=0.0002,
        oi_delta_24h_pct=12.0, parkinson_hv=0.55, rv_rank=22.0)
    assert inp.funding_rate == 0.0001
    assert inp.funding_avg == 0.0002
    assert inp.oi_delta_24h_pct == 12.0
    assert inp.parkinson_hv == 0.55
    assert inp.rv_rank == 22.0
    # never-fabricated planes stay None
    assert inp.whale_ls is None
    assert inp.narrative_score is None
    assert inp.macro_regime is None
    assert inp.sector_movers_up is None


def test_build_inputs_live_ring_beats_cache():
    st = SimpleNamespace(volatility_percentile=0.99)
    inp = pcl.build_inputs("SOL-USD", parkinson_hv=0.55, rv_rank=22.0,
                           measured_state=st, measured_state_age_s=10.0)
    assert inp.rv_rank == 22.0  # primary ring read wins, cache ignored


def test_build_inputs_fallback_fills_starved_ring():
    st = SimpleNamespace(volatility_percentile=0.30)
    inp = pcl.build_inputs("SOL-USD", parkinson_hv=0.55, rv_rank=None,
                           measured_state=st, measured_state_age_s=10.0)
    assert inp.rv_rank == 30.0


def test_build_inputs_stale_cache_no_fallback():
    st = SimpleNamespace(volatility_percentile=0.30)
    inp = pcl.build_inputs("SOL-USD", parkinson_hv=0.55, rv_rank=None,
                           measured_state=st,
                           measured_state_age_s=pcl.MEASURED_STATE_MAX_AGE_S + 1)
    assert inp.rv_rank is None


def test_build_inputs_constructs_real_fields_no_typeerror():
    # The legacy call site passed movers_3pct= (not a field) and died at the
    # constructor; this path must construct cleanly.
    inp = pcl.build_inputs("SPCX-USD")
    assert isinstance(inp, bc.CoherenceInputs)


# ── coh_leg_score: pillar lighting + renormalization ─────────────────────────

def test_score_volatility_pillar_lights_via_fallback():
    st = SimpleNamespace(volatility_percentile=0.20)  # rvr 20 < 35 coiled
    inp = pcl.build_inputs("SOL-USD", parkinson_hv=0.50,  # < 0.70 coiled
                           measured_state=st, measured_state_age_s=5.0)
    score = pcl.coh_leg_score("SOL-USD", inp)
    # coiled pillar = 2.5 of 2.5, renormalized over 1 present pillar -> 10.0
    assert score == 10.0


def test_score_all_none_stays_dark():
    inp = pcl.build_inputs("SPCX-USD")  # every plane None
    assert pcl.coh_leg_score("SPCX-USD", inp) is None


def test_score_partial_planes_renormalize():
    # positioning lit (fr/favg/oi), volatility dark (no hv) ->
    # score = 10 * positioning / 2.5; fr 0.0001 < 0.7*0.001 and oi 12 > 10
    # -> squeeze_setup 2.5 -> 10.0
    inp = pcl.build_inputs("SOL-USD", funding_rate=0.0001,
                           funding_avg=0.001, oi_delta_24h_pct=12.0)
    assert pcl.coh_leg_score("SOL-USD", inp) == 10.0


def test_score_mid_vol_band_renormalizes():
    # hv 0.80 (<0.85) + rvr 40 (<45) -> interpolated band 2.0 -> 8.0
    st = SimpleNamespace(volatility_percentile=0.40)
    inp = pcl.build_inputs("SOL-USD", parkinson_hv=0.80,
                           measured_state=st, measured_state_age_s=5.0)
    assert pcl.coh_leg_score("SOL-USD", inp) == 8.0


def test_score_hv_without_rvr_stays_dark():
    # volatility pillar needs BOTH hv and rvr; no cache -> abstain -> dark
    inp = pcl.build_inputs("SOL-USD", parkinson_hv=0.50)
    assert pcl.coh_leg_score("SOL-USD", inp) is None


def test_score_degenerate_hv_abstains():
    st = SimpleNamespace(volatility_percentile=0.20)
    inp = pcl.build_inputs("SOL-USD", parkinson_hv="not-a-float",
                           measured_state=st, measured_state_age_s=5.0)
    assert pcl.coh_leg_score("SOL-USD", inp) is None


def test_score_never_raises_on_garbage():
    inp = pcl.build_inputs("SOL-USD", funding_rate=object(),
                           funding_avg=object(), oi_delta_24h_pct=object())
    assert pcl.coh_leg_score("SOL-USD", inp) is None


# ── coh_leg_verdict: pass/fail/dark semantics ────────────────────────────────

def test_verdict_dark_on_none():
    assert pcl.coh_leg_verdict(None) == "dark"


def test_verdict_fail_below_threshold():
    assert pcl.coh_leg_verdict(4.8) == "fail"  # exhausted band, 0.5 -> 2.0... 4.8 < 5.0


def test_verdict_pass_at_threshold_inclusive():
    assert pcl.coh_leg_verdict(pcl.COH_LEG_MIN_SCORE) == "pass"


def test_verdict_pass_above_threshold():
    assert pcl.coh_leg_verdict(7.2) == "pass"


def test_verdict_custom_threshold():
    assert pcl.coh_leg_verdict(6.0, min_score=6.5) == "fail"
    assert pcl.coh_leg_verdict(6.5, min_score=6.5) == "pass"


def test_verdict_non_numeric_dark():
    assert pcl.coh_leg_verdict("high") == "dark"


# ── End-to-end: the SoDEX-symbol scenario from the live telemetry ────────────

def test_sodex_symbol_scenario_lights_honestly():
    # Typical SoDEX symbol: venue funding + 7d avg real, OI absent (no venue
    # feed), HV from 15m candles real, ring rv_rank starved, measured-state
    # cache fresh -> volatility pillar lights, positioning abstains (no OI).
    st = SimpleNamespace(volatility_percentile=0.25)
    inp = pcl.build_inputs(
        "SPCX-USD", funding_rate=0.0003, funding_avg=0.0002,
        oi_delta_24h_pct=None, parkinson_hv=0.60, rv_rank=None,
        measured_state=st, measured_state_age_s=120.0)
    score = pcl.coh_leg_score("SPCX-USD", inp)
    assert score is not None  # coh_ok=True: warmup leg present
    # hv 0.60<0.70, rvr 25<35 -> coiled 2.5/2.5 -> 10.0 single pillar
    assert score == 10.0
    assert pcl.coh_leg_verdict(score) == "pass"


def test_sodex_symbol_no_cache_stays_dark():
    # Same symbol with an empty measured-state cache: honest darkness,
    # not a fabricated pillar.
    inp = pcl.build_inputs(
        "SPCX-USD", funding_rate=0.0003, funding_avg=0.0002,
        oi_delta_24h_pct=None, parkinson_hv=0.60, rv_rank=None,
        measured_state=None, measured_state_age_s=None)
    assert pcl.coh_leg_score("SPCX-USD", inp) is None
    assert pcl.coh_leg_verdict(None) == "dark"
