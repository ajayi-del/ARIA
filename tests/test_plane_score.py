"""Pins for intelligence/plane_score.py — the distinct-plane rebuild of the
legacy 4-point pre-cascade score. Zero network, zero fixtures, deterministic."""

from __future__ import annotations

from intelligence.plane_score import (BAR_PLANES, compare, legacy_score,
                                      plane_score, plane_verdicts)


class TestFundingPlane:
    def test_pass_latest_and_one_prior_negative(self):
        out = plane_verdicts(funding_rates=[-0.01, 0.005, -0.02])
        assert out["funding"]["verdict"] == "pass"
        assert out["funding"]["legs"] == {"latest_neg": "pass",
                                          "prior2_any_neg": "pass"}

    def test_fail_when_latest_nonnegative(self):
        out = plane_verdicts(funding_rates=[-0.01, -0.02, 0.003])
        assert out["funding"]["verdict"] == "fail"
        assert out["funding"]["legs"]["latest_neg"] == "fail"

    def test_fail_when_both_priors_nonnegative(self):
        out = plane_verdicts(funding_rates=[0.001, 0.002, -0.01])
        assert out["funding"]["verdict"] == "fail"
        assert out["funding"]["legs"]["prior2_any_neg"] == "fail"

    def test_zero_is_not_negative_latest(self):
        out = plane_verdicts(funding_rates=[-0.01, -0.02, 0.0])
        assert out["funding"]["verdict"] == "fail"

    def test_zero_is_not_negative_prior(self):
        out = plane_verdicts(funding_rates=[0.0, 0.0, -0.01])
        assert out["funding"]["verdict"] == "fail"

    def test_uses_newest_three_only(self):
        out = plane_verdicts(funding_rates=[-0.5, 0.01, 0.02, -0.01])
        assert out["funding"]["verdict"] == "fail"

    def test_dark_below_three_prints(self):
        assert plane_verdicts(funding_rates=None)["funding"]["verdict"] == "dark"
        assert plane_verdicts(funding_rates=[])["funding"]["verdict"] == "dark"
        out = plane_verdicts(funding_rates=[-0.01, -0.02])
        assert out["funding"]["verdict"] == "dark"
        assert out["funding"]["legs"]["latest_neg"] == "dark"


class TestWhalePlane:
    def test_ls_strict_boundary(self):
        assert plane_verdicts(whale_ls=1.8)["whale"]["verdict"] == "fail"
        assert plane_verdicts(whale_ls=1.800001)["whale"]["verdict"] == "pass"

    def test_oi_chg_6h_zero_fails(self):
        assert plane_verdicts(oi_chg_6h=0.0)["whale"]["verdict"] == "fail"
        assert plane_verdicts(oi_chg_6h=0.001)["whale"]["verdict"] == "pass"
        assert plane_verdicts(oi_chg_6h=-0.5)["whale"]["verdict"] == "fail"

    def test_named_whale_net_positive_passes(self):
        assert plane_verdicts(named_whale_net=100.0)["whale"]["verdict"] == "pass"
        assert plane_verdicts(named_whale_net=0.0)["whale"]["verdict"] == "fail"
        assert plane_verdicts(named_whale_net=-5.0)["whale"]["verdict"] == "fail"

    def test_disagreement_recorded_no_veto(self):
        out = plane_verdicts(whale_ls=1.0, named_whale_net=250.0)
        whale = out["whale"]
        assert whale["verdict"] == "pass"
        assert whale["legs"]["whale_ls"] == "fail"
        assert whale["legs"]["named_whale_net"] == "pass"
        assert whale["legs"]["oi_chg_6h"] == "dark"

    def test_all_legs_none_dark(self):
        out = plane_verdicts(whale_ls=None, named_whale_net=None, oi_chg_6h=None)
        whale = out["whale"]
        assert whale["verdict"] == "dark"
        assert all(v == "dark" for v in whale["legs"].values())

    def test_present_failing_leg_means_fail_not_dark(self):
        out = plane_verdicts(whale_ls=1.0)
        assert out["whale"]["verdict"] == "fail"


class TestKlinePlane:
    def test_pass_both_returns_negative(self):
        out = plane_verdicts(close_now=95.0, close_6h_ago=100.0,
                             close_24h_ago=110.0)
        assert out["kline"]["verdict"] == "pass"

    def test_fail_6h_only_leg(self):
        out = plane_verdicts(close_now=105.0, close_6h_ago=100.0,
                             close_24h_ago=110.0)
        kline = out["kline"]
        assert kline["verdict"] == "fail"
        assert kline["legs"]["ret_6h_neg"] == "fail"
        assert kline["legs"]["ret_24h_neg"] == "pass"

    def test_fail_24h_only_leg(self):
        out = plane_verdicts(close_now=95.0, close_6h_ago=100.0,
                             close_24h_ago=90.0)
        kline = out["kline"]
        assert kline["verdict"] == "fail"
        assert kline["legs"]["ret_6h_neg"] == "pass"
        assert kline["legs"]["ret_24h_neg"] == "fail"

    def test_flat_return_is_not_negative(self):
        out = plane_verdicts(close_now=100.0, close_6h_ago=100.0,
                             close_24h_ago=110.0)
        assert out["kline"]["verdict"] == "fail"

    def test_dark_on_missing_reference(self):
        assert plane_verdicts(close_now=95.0, close_6h_ago=None,
                              close_24h_ago=100.0)["kline"]["verdict"] == "dark"
        assert plane_verdicts(close_now=95.0, close_6h_ago=100.0,
                              close_24h_ago=None)["kline"]["verdict"] == "dark"
        assert plane_verdicts(close_now=None, close_6h_ago=100.0,
                              close_24h_ago=110.0)["kline"]["verdict"] == "dark"

    def test_dark_on_nonpositive_reference(self):
        assert plane_verdicts(close_now=95.0, close_6h_ago=0.0,
                              close_24h_ago=100.0)["kline"]["verdict"] == "dark"
        assert plane_verdicts(close_now=95.0, close_6h_ago=100.0,
                              close_24h_ago=-1.0)["kline"]["verdict"] == "dark"


class TestCascadePlane:
    def test_liq_z_inclusive_boundary(self):
        assert plane_verdicts(liq_z=2.0)["cascade_onchain"]["verdict"] == "pass"
        assert plane_verdicts(liq_z=1.999)["cascade_onchain"]["verdict"] == "fail"

    def test_oi_flush_inclusive_boundary(self):
        assert plane_verdicts(oi_chg_1h=-0.01)["cascade_onchain"]["verdict"] == "pass"
        assert plane_verdicts(oi_chg_1h=-0.009)["cascade_onchain"]["verdict"] == "fail"

    def test_either_leg_confirms(self):
        out = plane_verdicts(liq_z=0.5, oi_chg_1h=-0.05)
        plane = out["cascade_onchain"]
        assert plane["verdict"] == "pass"
        assert plane["legs"]["liq_z"] == "fail"
        assert plane["legs"]["oi_chg_1h"] == "pass"

    def test_dark_when_both_votes_none(self):
        out = plane_verdicts(liq_z=None, oi_chg_1h=None)
        assert out["cascade_onchain"]["verdict"] == "dark"

    def test_liq_notional_alone_is_not_a_vote(self):
        out = plane_verdicts(liq_notional_1h_usd=5_000_000.0)
        plane = out["cascade_onchain"]
        assert plane["verdict"] == "dark"
        assert plane["aux_liq_notional_1h_usd"] == 5_000_000.0


class TestPlaneScore:
    def test_counts_only_pass(self):
        out = plane_score(funding_rates=[-0.01, 0.0, -0.02],   # pass
                          whale_ls=2.5,                        # pass
                          close_now=110.0, close_6h_ago=100.0,
                          close_24h_ago=105.0,                 # fail
                          liq_z=0.1)                           # fail
        assert out["plane_score"] == 2
        assert out["n_dark"] == 0
        assert out["bar_met"] is False

    def test_dark_planes_not_counted_and_reported(self):
        out = plane_score(funding_rates=[-0.01, 0.0, -0.02],   # pass
                          whale_ls=2.5,                        # pass
                          close_now=95.0, close_6h_ago=100.0,
                          close_24h_ago=110.0)                 # pass, cascade dark
        assert out["plane_score"] == 3
        assert out["n_dark"] == 1
        assert out["bar_met"] is True

    def test_bar_met_boundary_exactly_three(self):
        three = plane_score(funding_rates=[-0.01, 0.0, -0.02],
                            whale_ls=2.5,
                            close_now=95.0, close_6h_ago=100.0,
                            close_24h_ago=110.0,
                            liq_z=0.0)
        assert three["plane_score"] == BAR_PLANES
        assert three["bar_met"] is True
        two = plane_score(funding_rates=[-0.01, 0.0, -0.02],
                          whale_ls=2.5,
                          close_now=110.0, close_6h_ago=100.0,
                          close_24h_ago=105.0,
                          liq_z=0.0)
        assert two["plane_score"] == 2
        assert two["bar_met"] is False

    def test_all_dark(self):
        out = plane_score()
        assert out["plane_score"] == 0
        assert out["n_dark"] == 4
        assert out["bar_met"] is False


class TestLegacyScore:
    def test_all_none_dark(self):
        assert legacy_score(None, None, None, None) is None

    def test_partial_none_legs_count_zero(self):
        assert legacy_score(True, None, None, None) == 1
        assert legacy_score(False, None, None, None) == 0

    def test_full_count_and_thresholds(self):
        assert legacy_score(True, True, 0.5, 2.0) == 4
        assert legacy_score(True, True, 0.0, 1.8) == 2  # strict boundaries
        assert legacy_score(True, True, -0.1, 1.0) == 2


def _double_count_plane_result() -> dict:
    """Legacy-4 from funding + positioning legs only; kline failing, cascade
    dark -> plane_score 2. The double-count case this module exists to catch."""
    return plane_score(funding_rates=[-0.01, -0.02, -0.03],  # funding pass
                       whale_ls=2.0, oi_chg_6h=0.5,          # whale pass
                       close_now=110.0, close_6h_ago=100.0,
                       close_24h_ago=105.0)                  # kline fail, cascade dark


class TestCompare:
    def test_agree_armed(self):
        pr = plane_score(funding_rates=[-0.01, 0.0, -0.02],
                         whale_ls=2.0, oi_chg_6h=0.5,
                         close_now=95.0, close_6h_ago=100.0,
                         close_24h_ago=110.0)
        assert pr["bar_met"] is True
        assert compare(pr, legacy_score(True, True, 0.5, 2.0)) == "agree_armed"

    def test_legacy_only_double_count_cohort(self):
        pr = _double_count_plane_result()
        assert pr["plane_score"] == 2
        assert pr["bar_met"] is False
        legacy = legacy_score(True, True, 0.5, 2.0)
        assert legacy == 4
        assert compare(pr, legacy) == "legacy_only"

    def test_plane_only_cohort_legacy_missed(self):
        pr = plane_score(funding_rates=[-0.01, 0.0, -0.02],
                         close_now=95.0, close_6h_ago=100.0,
                         close_24h_ago=110.0,
                         liq_z=2.5)
        assert pr["bar_met"] is True
        assert compare(pr, legacy_score(False, False, -0.1, 1.0)) == "plane_only"

    def test_plane_only_when_legacy_dark(self):
        pr = plane_score(funding_rates=[-0.01, 0.0, -0.02],
                         close_now=95.0, close_6h_ago=100.0,
                         close_24h_ago=110.0,
                         liq_z=2.5)
        assert compare(pr, None) == "plane_only"

    def test_agree_calm(self):
        pr = plane_score(funding_rates=[0.01, 0.0, 0.02],
                         whale_ls=1.0,
                         close_now=110.0, close_6h_ago=100.0,
                         close_24h_ago=105.0,
                         liq_z=0.1)
        assert compare(pr, legacy_score(False, False, -0.1, 1.0)) == "agree_calm"

    def test_dark(self):
        pr = plane_score(funding_rates=[-0.01, 0.0, -0.02], whale_ls=2.0)
        assert pr["plane_score"] == 2
        assert compare(pr, None) == "dark"
