"""tests/test_rotation_scalar.py — pins for the wave-2 Rotation Coherence Scalar.

Doctrine pinned: Z_spread/D_pct/P_dwell/S component math, the |RCS|≤1 cap,
fresh-opposing-tide force-flat, fail-closed degenerate inputs, dwell trailing
run counter, and the hysteresis arm/disarm/vote state machine (arm ≥0.5 with
2-of-3 vote, disarm <0.2 ×2 reads, band prevents churn).
"""
from types import SimpleNamespace

import pytest

from intelligence.rotation import (
    RCS_ARM, RCS_DISARM, RotationScalarHysteresis,
    dwell_bars_from_regimes, rotation_scalar,
)


def _matrix(cat=None, lead="large_cap", disp=0.01):
    return SimpleNamespace(
        category_scores=cat if cat is not None else {
            "large_cap": 0.02, "alt_l1": -0.02, "defi_infra": 0.0},
        leading_category=lead,
        lagging_category="alt_l1",
        dispersion=disp,
        confidence=0.8,
        asset_scores={"BTC-USD": 0.02},
    )


# ── component math ───────────────────────────────────────────────────────────

def test_z_spread_full_range():
    # cats {0.02, -0.02, 0.0}: mean 0, sigma = sqrt(0.0008/3)≈0.01633
    # top−bottom = 0.04 → z ≈ 2.449
    row = rotation_scalar(_matrix(), [], 8)
    assert row["z_spread"] == pytest.approx(2.449, abs=0.01)


def test_rcs_capped_at_one():
    # z/2 > 1 with d_pct 1.0 and full dwell → magnitude capped at 1
    row = rotation_scalar(_matrix(), [0.001] * 10, 100)
    assert row["rcs"] == 1.0
    assert row["p_dwell"] == 1.0
    assert row["d_pct"] == 1.0
    assert row["s_sign"] == 1


def test_rcs_sign_follows_leading_category():
    row_long = rotation_scalar(_matrix(), [0.001] * 10, 8)
    assert row_long["rcs"] > 0
    row_short = rotation_scalar(
        _matrix(cat={"large_cap": -0.02, "alt_l1": 0.02, "defi_infra": 0.0}),
        [0.001] * 10, 8)
    assert row_short["rcs"] < 0
    assert row_short["s_sign"] == -1


def test_dwell_scales_magnitude():
    full = rotation_scalar(_matrix(), [], 8)["rcs"]
    half = rotation_scalar(_matrix(), [], 4)["rcs"]
    assert full > 0
    assert half == pytest.approx(full / 2.0, abs=1e-6)


def test_d_pct_percentile_vs_history():
    # current dispersion at the 90th percentile of its own history
    row = rotation_scalar(_matrix(disp=0.019), [0.01] * 20, 8)
    assert row["d_pct"] > 0.9
    # thin history → neutral 0.5 abstain
    assert rotation_scalar(_matrix(), [0.01, 0.02], 8)["d_pct"] == 0.5


def test_zero_dispersion_history_and_dict_input():
    row = rotation_scalar(_matrix(), {"dispersion": [0.01] * 10}, 8)
    assert row["d_pct"] >= 0.0


# ── tide override ────────────────────────────────────────────────────────────

def test_fresh_opposing_tide_forces_flat():
    row = rotation_scalar(_matrix(), [0.001] * 10, 8,
                          tide_state={"sum_3d_usd": -5e8, "age_hours": 12.0})
    assert row["rcs"] == 0.0
    assert row["tide_forced_flat"] is True
    assert row["reason"] == "fresh_tide_opposed"
    assert row["tide_sign"] == -1


def test_stale_tide_does_not_force_flat():
    row = rotation_scalar(_matrix(), [0.001] * 10, 8,
                          tide_state={"sum_3d_usd": -5e8, "age_hours": 48.0})
    assert row["rcs"] != 0.0
    assert row["tide_forced_flat"] is False


def test_aligned_fresh_tide_keeps_rcs():
    row = rotation_scalar(_matrix(), [0.001] * 10, 8,
                          tide_state={"sum_3d_usd": 5e8, "age_hours": 6.0})
    assert row["rcs"] > 0
    assert row["tide_forced_flat"] is False


# ── fail-closed degenerate inputs ────────────────────────────────────────────

def test_none_matrix_is_zero():
    row = rotation_scalar(None, [], 8)
    assert row["rcs"] == 0.0
    assert row["reason"] == "no_direction"


def test_unknown_leading_category_no_direction():
    row = rotation_scalar(_matrix(lead="none"), [0.001] * 10, 8)
    assert row["rcs"] == 0.0
    assert row["s_sign"] == 0


def test_single_category_zero_z():
    row = rotation_scalar(_matrix(cat={"large_cap": 0.02}), [], 8)
    assert row["z_spread"] == 0.0
    assert row["rcs"] == 0.0


def test_equal_categories_zero_sigma_zero_z():
    row = rotation_scalar(_matrix(cat={"a": 0.01, "b": 0.01}), [], 8)
    assert row["z_spread"] == 0.0


# ── dwell counter ────────────────────────────────────────────────────────────

def test_dwell_trailing_run():
    assert dwell_bars_from_regimes(["risk_on", "risk_on", "alt_season",
                                    "alt_season", "alt_season"]) == 3
    assert dwell_bars_from_regimes(["risk_on"]) == 1
    assert dwell_bars_from_regimes([]) == 0
    assert dwell_bars_from_regimes(None) == 0
    assert dwell_bars_from_regimes(["a", "b"]) == 1


# ── hysteresis state machine ─────────────────────────────────────────────────

def _row(rcs, s=1, tide=0, z=2.0, forced=False):
    return {"rcs": rcs, "s_sign": s, "tide_sign": tide, "z_spread": z,
            "tide_forced_flat": forced}


def test_hysteresis_arms_from_flat_on_strong_read():
    h = RotationScalarHysteresis()
    v = h.update(_row(0.7))
    assert v["armed"] is True and v["direction"] == 1
    assert v["rcs_effective"] == 0.7


def test_hysteresis_band_holds_direction():
    h = RotationScalarHysteresis()
    h.update(_row(0.7))
    v = h.update(_row(0.3))  # inside [0.2, 0.5] band: holds, no churn
    assert v["direction"] == 1 and v["armed"] is True
    assert v["rcs_effective"] == 0.3


def test_hysteresis_disarms_after_two_weak_reads():
    h = RotationScalarHysteresis()
    h.update(_row(0.7))
    v1 = h.update(_row(0.1))
    assert v1["armed"] is True and v1["disarm_streak"] == 1
    v2 = h.update(_row(0.05))
    assert v2["armed"] is False and v2["direction"] == 0
    assert v2["rcs_effective"] == 0.0


def test_hysteresis_flip_needs_two_of_three_votes():
    h = RotationScalarHysteresis()
    h.update(_row(0.7, s=1, tide=1, z=2.0))
    # strong opposed read but only ONE leg flipped (s) → flip refused
    v = h.update(_row(-0.7, s=-1, tide=1, z=2.0))
    assert v["direction"] == 1
    assert v["votes"] == 1
    assert v["rcs_effective"] == 0.0
    # now a second read where TWO legs flipped (s + z) → flip arms
    v2 = h.update(_row(-0.7, s=-1, tide=1, z=-2.0))
    # note: prev read was s=-1, so s is not a "flip" vs prev; z flips 2.0→-2.0
    # and tide stays. Only 1 leg differs from prev → still refused.
    assert v2["direction"] == 1


def test_hysteresis_flip_arms_with_two_flipped_legs():
    h = RotationScalarHysteresis()
    h.update(_row(0.7, s=1, tide=1, z=2.0))
    # two legs flip vs previous read (s and z) → flip confirmed
    v = h.update(_row(-0.7, s=-1, tide=1, z=-2.0))
    assert v["votes"] == 2
    assert v["direction"] == -1 and v["armed"] is True
    assert v["rcs_effective"] == -0.7


def test_hysteresis_tide_forced_flat_counts_toward_disarm():
    h = RotationScalarHysteresis()
    h.update(_row(0.7))
    h.update(_row(0.0, forced=True))
    v = h.update(_row(0.0, forced=True))
    assert v["armed"] is False and v["direction"] == 0


def test_hysteresis_weak_read_resets_on_recovery():
    h = RotationScalarHysteresis()
    h.update(_row(0.7))
    h.update(_row(0.1))
    v = h.update(_row(0.6))  # recovers above disarm before the 2nd weak read
    assert v["armed"] is True and v["direction"] == 1
    assert v["disarm_streak"] == 0
