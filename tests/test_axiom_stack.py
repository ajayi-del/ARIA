"""tests/test_axiom_stack.py — pins for the Governor's axiom-stack brain.

Every axiom pinned: tier boundaries (both evidence modes + funding downgrade
+ sub-floor rejects), each Kant failure leg by name, the Nietzsche ladder,
the Governor's worked Kelly example (0.484 / 0.242), shrinkage discipline at
n=2 and n=500, the 0.30 hard ceiling, every regime state incl. priority
collisions, the cascade cooldown boundary, the propagation EIGEN trap, the
actual-stop RR, min-hold / narrative-clock boundaries, ratchet rungs,
pyramid legs, and the kill switch.
"""
from __future__ import annotations

import datetime

import pytest

from intelligence.axiom_stack import (
    CASCADE_COOLDOWN_S,
    DEPENDENCY_MAP,
    KELLY_HARD_CEIL,
    MODE_ABSOLUTE,
    MODE_PLANE,
    MODE_PROXY,
    NIETZSCHE_LADDER,
    REGIME_COIL_BREAK_UP,
    REGIME_COIL_COMPRESS,
    REGIME_DEAD_ZONE,
    REGIME_NARRATIVE_ROTATION,
    REGIME_NORMAL,
    REGIME_POST_CASCADE,
    RESTRICTED,
    axiom_stack_enabled,
    base_name,
    cascade_cooldown_active,
    classify_regime,
    classify_tier,
    full_kelly,
    kant_check,
    margin_fraction,
    min_hold_ok,
    narrative_clock_expired,
    nietzsche_multiplier,
    propagation_verdict,
    pyramid_verdict,
    ratchet_rung,
    regime_policy,
    rr_from_bracket,
    shrunk_kelly,
)

TS_NOON = 1_800_000_000.0  # arbitrary epoch well inside a non-dead-zone hour


def _dt(hour: int) -> datetime.datetime:
    return datetime.datetime(2026, 9, 26, hour, 0, 0)


# ── 1. Tier classifier — plane mode ──────────────────────────────────────────

class TestTierPlane:
    def test_tier1_at_90(self):
        assert classify_tier(MODE_PLANE, whale_pct=90) == 1

    def test_tier1_above_90(self):
        assert classify_tier(MODE_PLANE, whale_pct=97.5) == 1

    def test_tier2_band(self):
        assert classify_tier(MODE_PLANE, whale_pct=75) == 2
        assert classify_tier(MODE_PLANE, whale_pct=89.9) == 2

    def test_tier3_band(self):
        assert classify_tier(MODE_PLANE, whale_pct=60) == 3
        assert classify_tier(MODE_PLANE, whale_pct=74.9) == 3

    def test_short_skew_rejects_regardless(self):
        assert classify_tier(MODE_PLANE, whale_pct=39.9) == 0
        assert classify_tier(MODE_PLANE, whale_pct=0) == 0

    def test_no_conviction_band_40_to_60(self):
        assert classify_tier(MODE_PLANE, whale_pct=40) == 0
        assert classify_tier(MODE_PLANE, whale_pct=59.9) == 0

    def test_funding_overcrowded_downgrades_one_tier(self):
        assert classify_tier(MODE_PLANE, whale_pct=95,
                             funding_current=0.03, funding_avg=0.01) == 2
        assert classify_tier(MODE_PLANE, whale_pct=80,
                             funding_current=0.03, funding_avg=0.01) == 3
        assert classify_tier(MODE_PLANE, whale_pct=65,
                             funding_current=0.03, funding_avg=0.01) == 0

    def test_funding_not_overcrowded_keeps_tier(self):
        assert classify_tier(MODE_PLANE, whale_pct=95,
                             funding_current=0.024, funding_avg=0.01) == 1

    def test_missing_funding_evidence_abstains_no_downgrade(self):
        assert classify_tier(MODE_PLANE, whale_pct=95) == 1
        assert classify_tier(MODE_PLANE, whale_pct=95,
                             funding_current=0.5, funding_avg=0) == 1

    def test_missing_pct_rejects(self):
        assert classify_tier(MODE_PLANE, whale_pct=None) == 0
        assert classify_tier(MODE_PLANE) == 0


# ── 1b. Tier classifier — absolute mode ──────────────────────────────────────

class TestTierAbsolute:
    def test_ladder_boundaries(self):
        assert classify_tier(MODE_ABSOLUTE, whale_ls=3.5) == 1
        assert classify_tier(MODE_ABSOLUTE, whale_ls=2.0) == 2
        assert classify_tier(MODE_ABSOLUTE, whale_ls=1.5) == 3

    def test_sub_1_3_rejects(self):
        assert classify_tier(MODE_ABSOLUTE, whale_ls=1.29) == 0
        assert classify_tier(MODE_ABSOLUTE, whale_ls=0.7) == 0

    def test_no_mans_land_1_3_to_1_5(self):
        assert classify_tier(MODE_ABSOLUTE, whale_ls=1.3) == 0
        assert classify_tier(MODE_ABSOLUTE, whale_ls=1.49) == 0

    def test_top_trader_sub_1_3_forces_reject(self):
        assert classify_tier(MODE_ABSOLUTE, whale_ls=4.0, top_trader=1.2) == 0

    def test_top_trader_caps_tier_to_weaker_axis(self):
        assert classify_tier(MODE_ABSOLUTE, whale_ls=4.0, top_trader=2.5) == 2
        assert classify_tier(MODE_ABSOLUTE, whale_ls=2.2, top_trader=5.0) == 2

    def test_top_trader_absent_ignored(self):
        assert classify_tier(MODE_ABSOLUTE, whale_ls=3.8, top_trader=None) == 1

    def test_unknown_mode_fails_closed(self):
        assert classify_tier("mystery", whale_pct=99, whale_ls=9) == 0


# ── 1c. Tier classifier — proxy mode (WhaleProxy composite, 2026-09-26) ──────

class TestTierProxy:
    def test_band_boundaries(self):
        assert classify_tier(MODE_PROXY, whale_pct=0.80) == 1
        assert classify_tier(MODE_PROXY, whale_pct=0.60) == 2
        assert classify_tier(MODE_PROXY, whale_pct=0.40) == 3

    def test_watch_and_reject_bands_are_no_entry(self):
        # proxy band 4 (watch, 0.25-0.40) and band 5 (reject, <0.25)
        assert classify_tier(MODE_PROXY, whale_pct=0.39) == 0
        assert classify_tier(MODE_PROXY, whale_pct=0.25) == 0
        assert classify_tier(MODE_PROXY, whale_pct=0.20) == 0  # LINK-class

    def test_calibration_examples(self):
        assert classify_tier(MODE_PROXY, whale_pct=0.92) == 1  # FET-class
        assert classify_tier(MODE_PROXY, whale_pct=0.65) == 2  # SUI-class

    def test_no_funding_downgrade_in_proxy_mode(self):
        # funding is already inside the composite — no double penalty
        assert classify_tier(MODE_PROXY, whale_pct=0.92,
                             funding_current=100.0, funding_avg=1.0) == 1

    def test_missing_score_rejects(self):
        assert classify_tier(MODE_PROXY, whale_pct=None) == 0


# ── 2. Kant checklist ─────────────────────────────────────────────────────────

GOOD = dict(tier=1, rr_actual=2.4, narrative_day=1, coherence=6.1,
            symbol="ETH-USD", funding_current=0.01, funding_avg=0.01)


class TestKant:
    def test_all_pass_approves(self):
        v = kant_check(**GOOD)
        assert v.approved and v.failures == () and v.tier == 1

    def test_whale_floor_leg(self):
        v = kant_check(**{**GOOD, "tier": 0})
        assert not v.approved and v.failures == ("whale_floor",)

    def test_rr_leg(self):
        v = kant_check(**{**GOOD, "rr_actual": 1.79})
        assert "rr_below_min" in v.failures
        assert kant_check(**{**GOOD, "rr_actual": 1.8}).approved

    def test_rr_none_fails(self):
        assert "rr_below_min" in kant_check(**{**GOOD, "rr_actual": None}).failures

    def test_narrative_leg(self):
        assert "narrative_expired" in kant_check(**{**GOOD, "narrative_day": 5}).failures
        assert kant_check(**{**GOOD, "narrative_day": 4}).approved

    def test_coherence_leg(self):
        assert "coherence_floor" in kant_check(**{**GOOD, "coherence": 5.29}).failures
        assert kant_check(**{**GOOD, "coherence": 5.3}).approved

    @pytest.mark.parametrize("sym", ["BCH-USD", "XLM-USD", "INJ-USD",
                                     "BONK-USD", "VIRTUAL-USD"])
    def test_restricted_symbols_by_base_name(self, sym):
        v = kant_check(**{**GOOD, "symbol": sym})
        assert "restricted_symbol" in v.failures

    def test_funding_leg(self):
        v = kant_check(**{**GOOD, "funding_current": 0.026, "funding_avg": 0.01})
        assert "funding_overcrowded" in v.failures
        assert kant_check(**{**GOOD, "funding_current": 0.025,
                             "funding_avg": 0.01}).approved

    def test_multiple_failures_all_named(self):
        v = kant_check(tier=0, rr_actual=1.0, narrative_day=9, coherence=1.0,
                       symbol="BCH-USD", funding_current=0.1, funding_avg=0.01)
        assert set(v.failures) == {"whale_floor", "rr_below_min",
                                   "narrative_expired", "coherence_floor",
                                   "restricted_symbol", "funding_overcrowded"}


class TestRrFromBracket:
    def test_uses_actual_clamped_stop(self):
        # entry 100, intended stop 95 clamped to 95.5 → risk 4.5, not 5.0
        assert rr_from_bracket(100, 95.5, 109) == pytest.approx(9 / 4.5)
        assert rr_from_bracket(100, 95.0, 109) == pytest.approx(9 / 5.0)

    def test_short_side_symmetric(self):
        assert rr_from_bracket(100, 104.5, 91) == pytest.approx(9 / 4.5)

    def test_degenerate_geometry_none(self):
        assert rr_from_bracket(100, 100, 110) is None
        assert rr_from_bracket(None, 95, 110) is None


# ── 3. Nietzsche ladder ───────────────────────────────────────────────────────

class TestNietzsche:
    def test_exact_ladder(self):
        assert nietzsche_multiplier(1) == 2.5
        assert nietzsche_multiplier(2) == 1.75
        assert nietzsche_multiplier(3) == 1.25
        assert nietzsche_multiplier(0) == 0.0

    def test_unknown_tier_zero(self):
        assert nietzsche_multiplier(9) == 0.0
        assert nietzsche_multiplier(None) == 0.0


# ── 4. Kelly-by-tier ──────────────────────────────────────────────────────────

class TestKelly:
    def test_governor_worked_example(self):
        # p=0.625, b=2.66 → full ≈ 0.484, tier1 margin = 0.242 ("24%")
        f = full_kelly(0.625, 2.66)
        assert f == pytest.approx(0.484, abs=1e-3)
        assert f * 0.5 == pytest.approx(0.242, abs=1e-3)

    def test_full_kelly_degenerate(self):
        assert full_kelly(0.6, 0) is None
        assert full_kelly(0.6, None) is None
        assert full_kelly(None, 2.0) is None

    def test_shrinkage_n2_prior_dominated(self):
        # 2 wins, fantasy payoff — raw Kelly would be 0.9; shrunk must stay
        # near the prior-dominated value, far below raw.
        raw = full_kelly(1.0, 10.0)
        shrunk = shrunk_kelly(2, 0, 10.0, 1.0)
        assert shrunk is not None and shrunk < 0.5 * raw
        prior = full_kelly(0.55, 2.0)
        assert abs(shrunk - prior) < abs(raw - prior)

    def test_shrinkage_n500_converges_to_empirical(self):
        shrunk = shrunk_kelly(310, 190, 2.66, 1.0)
        raw = full_kelly(310 / 500, 2.66)
        assert shrunk == pytest.approx(raw, abs=0.01)

    def test_n_zero_is_prior_kelly(self):
        assert shrunk_kelly(0, 0, 0, 0) == pytest.approx(full_kelly(0.55, 2.0))

    def test_hard_ceil_clamps(self):
        # monster track record → tier1 raw share way above 0.30
        m = margin_fraction(1, 490, 10, 3.0, 1.0)
        assert m == KELLY_HARD_CEIL == 0.30

    def test_margin_fraction_tier_scaling(self):
        base = shrunk_kelly(100, 60, 2.66, 1.0)
        assert margin_fraction(1, 100, 60, 2.66, 1.0) == pytest.approx(
            min(base * 0.5, 0.30))
        assert margin_fraction(2, 100, 60, 2.66, 1.0) == pytest.approx(
            min(base * (1 / 3), 0.30))
        assert margin_fraction(3, 100, 60, 2.66, 1.0) == pytest.approx(
            min(base * 0.25, 0.30))

    def test_margin_fraction_tier0_and_negative_kelly(self):
        assert margin_fraction(0, 100, 60, 2.66, 1.0) == 0.0
        assert margin_fraction(1, 10, 90, 1.0, 2.0) == 0.0  # negative edge


# ── 5. Regime gate ────────────────────────────────────────────────────────────

CALM = dict(btc_day_moves_3d=[0.4, -0.8, 0.3], oi_delta_24h=0.5,
            funding_bps=1.0, btc_drop_2h_pct=0.2, btc_1h_close=100.0,
            btc_1d_bollinger_upper=110.0, oi_delta_4h=0.2,
            symbol_day_move_pct=2.0, symbol_whale_pct=70, coherence=6.0)


class TestRegime:
    def test_dead_zone_hours(self):
        for h in (23, 0, 1):
            assert classify_regime(now_utc=_dt(h), **CALM) == REGIME_DEAD_ZONE
        assert classify_regime(now_utc=_dt(2), **CALM) != REGIME_DEAD_ZONE

    def test_dead_zone_beats_post_cascade(self):
        kw = {**CALM, "btc_drop_2h_pct": -3.0, "oi_delta_24h": -3.0}
        assert classify_regime(now_utc=_dt(0), **kw) == REGIME_DEAD_ZONE

    def test_post_cascade(self):
        kw = {**CALM, "btc_drop_2h_pct": -2.0, "oi_delta_24h": -2.0}
        assert classify_regime(now_utc=_dt(12), **kw) == REGIME_POST_CASCADE
        kw2 = {**kw, "oi_delta_24h": -1.9}
        assert classify_regime(now_utc=_dt(12), **kw2) != REGIME_POST_CASCADE

    def test_coil_break_up(self):
        kw = {**CALM, "btc_1h_close": 111.0, "funding_bps": 4.0,
              "oi_delta_4h": 1.5, "btc_day_moves_3d": [2.0, -0.8, 0.3]}
        assert classify_regime(now_utc=_dt(12), **kw) == REGIME_COIL_BREAK_UP

    def test_break_up_beats_narrative(self):
        kw = {**CALM, "btc_1h_close": 111.0, "funding_bps": 4.0,
              "oi_delta_4h": 1.5, "btc_day_moves_3d": [0.2, 0.1, 0.5],
              "symbol_day_move_pct": 12.0}
        assert classify_regime(now_utc=_dt(12), **kw) == REGIME_COIL_BREAK_UP

    def test_narrative_rotation(self):
        kw = {**CALM, "btc_day_moves_3d": [0.2, 0.1, 0.5],
              "symbol_day_move_pct": 9.0, "symbol_whale_pct": 65,
              "coherence": 5.6}
        assert classify_regime(now_utc=_dt(12), **kw) == REGIME_NARRATIVE_ROTATION

    def test_narrative_beats_coil_compress(self):
        kw = {**CALM, "btc_day_moves_3d": [0.2, 0.1, 0.5],
              "symbol_day_move_pct": 9.0, "oi_delta_24h": -1.0,
              "funding_bps": 1.0, "coherence": 6.0}
        assert classify_regime(now_utc=_dt(12), **kw) == REGIME_NARRATIVE_ROTATION

    def test_coil_compress(self):
        kw = {**CALM, "btc_day_moves_3d": [0.2, -1.4, 0.5],
              "funding_bps": -2.9, "oi_delta_24h": -0.1}
        assert classify_regime(now_utc=_dt(12), **kw) == REGIME_COIL_COMPRESS

    def test_coil_compress_needs_all_three_days_small(self):
        kw = {**CALM, "btc_day_moves_3d": [0.2, -1.5, 0.5],
              "funding_bps": 1.0, "oi_delta_24h": -0.1}
        assert classify_regime(now_utc=_dt(12), **kw) == REGIME_NORMAL

    def test_normal_fallback(self):
        assert classify_regime(now_utc=_dt(12), **CALM) == REGIME_NORMAL

    def test_missing_inputs_fail_to_normal(self):
        assert classify_regime(now_utc=_dt(12)) == REGIME_NORMAL

    def test_epoch_timestamp_input(self):
        # 2026-09-26 23:30 UTC as epoch
        ts = datetime.datetime(2026, 9, 26, 23, 30,
                               tzinfo=datetime.timezone.utc).timestamp()
        assert classify_regime(now_utc=ts, **CALM) == REGIME_DEAD_ZONE


class TestCascadeCooldown:
    def test_boundary(self):
        assert cascade_cooldown_active(1000, 1000 + 1799) is True
        assert cascade_cooldown_active(1000, 1000 + 1801) is False
        assert cascade_cooldown_active(1000, 1000 + 1800) is False

    def test_unknown_clock_stays_defensive(self):
        assert cascade_cooldown_active(None, 1000) is True


class TestRegimePolicy:
    def test_coil_narratives_only_reduced_leverage(self):
        p = regime_policy(REGIME_COIL_COMPRESS)
        assert p.new_entries_allowed and not p.btc_beta_allowed
        assert p.leverage_mult == 0.75

    def test_break_up_full(self):
        p = regime_policy(REGIME_COIL_BREAK_UP)
        assert p.new_entries_allowed and p.btc_beta_allowed
        assert p.leverage_mult == 1.0 and p.reserve_deploy_max == 1.0

    def test_post_cascade_half_size(self):
        p = regime_policy(REGIME_POST_CASCADE)
        assert p.new_entries_allowed and p.leverage_mult == 0.5

    def test_dead_zone_blocked(self):
        p = regime_policy(REGIME_DEAD_ZONE)
        assert not p.new_entries_allowed and not p.btc_beta_allowed
        assert p.leverage_mult == 0.0 and p.reserve_deploy_max == 0.0

    def test_narrative_not_beta(self):
        p = regime_policy(REGIME_NARRATIVE_ROTATION)
        assert p.new_entries_allowed and not p.btc_beta_allowed

    def test_unknown_regime_blocked(self):
        assert not regime_policy("GARBAGE").new_entries_allowed


# ── 6. Propagation map ───────────────────────────────────────────────────────

class TestPropagation:
    def test_eigen_trap_plane_mode(self):
        # lead FET +21%, target EIGEN +9.46% on bearish whale evidence (pct 20)
        v = propagation_verdict("FET-USD", 21.0, "EIGEN-USD", 9.46, 80, 20,
                                MODE_PLANE)
        assert not v.approved and v.reason == "propagation_target_weak"

    def test_eigen_trap_absolute_mode(self):
        v = propagation_verdict("FET-USD", 21.0, "EIGEN-USD", 9.46, 3.0, 0.7,
                                MODE_ABSOLUTE)
        assert not v.approved and v.reason == "propagation_target_weak"

    def test_valid_ondo_pendle_lag_approves(self):
        # ONDO +10%, PENDLE +2% (< 0.4×10 = 4%), both whale-valid, edge exists
        v = propagation_verdict("ONDO-USD", 10.0, "PENDLE-USD", 2.0, 70, 65,
                                MODE_PLANE)
        assert v.approved and v.reason == "propagation_approved"

    def test_lead_weak_rejects(self):
        v = propagation_verdict("ONDO-USD", 10.0, "PENDLE-USD", 2.0, 55, 80,
                                MODE_PLANE)
        assert not v.approved and v.reason == "propagation_lead_weak"
        v2 = propagation_verdict("ONDO-USD", 10.0, "PENDLE-USD", 2.0, 1.2, 3.0,
                                 MODE_ABSOLUTE)
        assert not v2.approved and v2.reason == "propagation_lead_weak"

    def test_no_lag_rejects(self):
        v = propagation_verdict("ONDO-USD", 10.0, "PENDLE-USD", 5.0, 80, 80,
                                MODE_PLANE)
        assert not v.approved and v.reason == "propagation_no_lag"

    def test_no_family_edge_rejects(self):
        v = propagation_verdict("SOL-USD", 10.0, "LINK-USD", 1.0, 80, 80,
                                MODE_PLANE)
        assert not v.approved and v.reason == "propagation_no_edge"

    def test_dependency_map_shape(self):
        assert DEPENDENCY_MAP["ONDO"] == ["LINK", "SNX", "PENDLE", "AAVE"]
        assert DEPENDENCY_MAP["FET"] == ["RENDER", "TAO", "NVDA"]
        assert DEPENDENCY_MAP["COIN"] == ["BTC"]


# ── 7. Exit-side verdicts ────────────────────────────────────────────────────

class TestExits:
    def test_min_hold_boundary(self):
        assert min_hold_ok(599) is False
        assert min_hold_ok(600) is True

    def test_narrative_clock_boundary(self):
        assert narrative_clock_expired(4) is False
        assert narrative_clock_expired(5) is True

    def test_ratchet_rungs_exact(self):
        assert ratchet_rung(3.5) == (3, 0.18)
        assert ratchet_rung(3.0) == (3, 0.18)
        assert ratchet_rung(2.0) == (2, 0.12)
        assert ratchet_rung(1.0) == (1, 0.0764)
        assert ratchet_rung(0.99) == (0, 0.0)
        assert ratchet_rung(None) == (0, 0.0)

    def test_pyramid_approved(self):
        v = pyramid_verdict(True, 2, REGIME_NORMAL, False)
        assert v.allow and v.add_notional_frac == 0.5 and v.move_stop_to_entry

    def test_pyramid_tp1_required(self):
        assert pyramid_verdict(False, 1, REGIME_NORMAL, False).reason == "tp1_not_filled"

    def test_pyramid_once_only(self):
        assert pyramid_verdict(True, 1, REGIME_NORMAL, True).reason == "already_pyramided"

    def test_pyramid_post_cascade_blocks(self):
        assert pyramid_verdict(True, 1, REGIME_POST_CASCADE, False).reason == "regime_post_cascade"

    def test_pyramid_narrative_day_le_3(self):
        assert pyramid_verdict(True, 3, REGIME_NORMAL, False).allow
        assert pyramid_verdict(True, 4, REGIME_NORMAL, False).reason == "narrative_clock"


# ── 8. Kill switch ────────────────────────────────────────────────────────────

class TestKillSwitch:
    def test_default_true(self, monkeypatch):
        monkeypatch.delenv("AXIOM_STACK_ENABLED", raising=False)
        assert axiom_stack_enabled() is True

    def test_explicit_off(self, monkeypatch):
        monkeypatch.setenv("AXIOM_STACK_ENABLED", "false")
        assert axiom_stack_enabled() is False

    def test_base_name_helper(self):
        assert base_name("BCH-USD") == "BCH"
        assert base_name("eth-usd") == "ETH"
        assert base_name(None) == ""
