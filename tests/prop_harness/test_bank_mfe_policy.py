"""Pins: bank green-MFE policy registers (ATS-201)."""

from __future__ import annotations

import pytest

from prop_harness.brains import bank_mfe_policy as bmp


class TestParseRungsR:
    def test_default_on_empty(self):
        assert bmp.parse_rungs_r("") == bmp.DEFAULT_PEAK_RUNGS_R
        assert bmp.parse_rungs_r(None) == bmp.DEFAULT_PEAK_RUNGS_R

    def test_env_shape(self):
        assert bmp.parse_rungs_r("0.5:0.5,1.0:0.75") == (
            (0.5, 0.5), (1.0, 0.75))

    def test_junk_falls_back(self):
        assert bmp.parse_rungs_r("not-a-rung") == bmp.DEFAULT_PEAK_RUNGS_R


class TestCfPeakR:
    def test_mid_runner_recovery(self):
        # mfe 2.0R × $10 risk = $20 peak; actual −$2 → lock 75% = $15
        out = bmp.cf_peak_r_banked(
            mfe_peak_r=2.0, risk_usd=10.0, realized_pnl=-2.0)
        assert out["armed"] is True
        assert out["frac"] == 0.75
        assert out["banked"] == 15.0
        assert out["delta"] == 17.0

    def test_below_rung_no_arm(self):
        out = bmp.cf_peak_r_banked(
            mfe_peak_r=0.3, risk_usd=10.0, realized_pnl=-1.0)
        assert out["armed"] is False
        assert out["delta"] == 0.0

    def test_already_banked_above_floor(self):
        out = bmp.cf_peak_r_banked(
            mfe_peak_r=1.0, risk_usd=10.0, realized_pnl=9.0)
        assert out["armed"] is True
        assert out["banked"] == 9.0
        assert out["delta"] == 0.0


class TestCfPartial:
    def test_partial_banks_rung_marks(self):
        out = bmp.cf_partial_banked(
            mfe_peak_r=1.2, risk_usd=10.0, realized_pnl=-2.0)
        assert out["armed"] is True
        # 0.5R×25%×10 + 1.0R×25%×0.75×10 = 1.25 + 1.875 = 3.125
        assert out["partial_usd"] == pytest.approx(3.125)
        assert out["delta"] > 0
