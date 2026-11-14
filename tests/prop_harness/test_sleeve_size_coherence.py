"""ATS-103 sleeve size + coherence knobs.

Stocks/turbo2: max_pos env-only (documented), 2× tradfi probe ($7→$14),
0.5% account equity risk (xyz: only). Crypto: fire floor coh≥7.5; never
inherits stock 0.5% account risk.
"""

from __future__ import annotations

import os

import pytest

from prop_harness.brains import eval_profile as ep
from prop_harness.config import PropConfig

EVAL = ep.PROFILE_EVALUATION


class TestTradfiAccountRisk:
    def test_stocks_half_pct_of_equity(self):
        # Live turbo2 ~$5019 → 0.5% ≈ $25.10; raise-only vs $14 floor.
        out = ep.risk_for_signal(
            EVAL, 4.0, 80.0, 200.0, 30.0, 1.0, 5.0, 100.0,
            asset="xyz:GOLD", tradfi_min_enabled=True, tradfi_min_risk=14.0,
            equity=5019.0, tradfi_account_risk_enabled=True,
            tradfi_account_risk_frac=0.005)
        assert out == pytest.approx(5019.0 * 0.005)

    def test_stocks_2x_floor_when_equity_dark(self):
        out = ep.risk_for_signal(
            EVAL, 4.0, 80.0, 200.0, 30.0, 1.0, 5.0, 100.0,
            asset="xyz:NVDA", tradfi_min_enabled=True, tradfi_min_risk=14.0,
            equity=None, tradfi_account_risk_enabled=True,
            tradfi_account_risk_frac=0.005)
        assert out == 14.0

    def test_crypto_never_takes_account_risk(self):
        # Crypto probe stays $7 even with equity + account-risk knobs on.
        out = ep.risk_for_signal(
            EVAL, 4.0, 80.0, 200.0, 30.0, 1.0, 5.0, 100.0,
            asset="BTC", crypto_min_enabled=True, crypto_min_risk=7.0,
            equity=4975.0, tradfi_account_risk_enabled=True,
            tradfi_account_risk_frac=0.005)
        assert out == 7.0

    def test_kill_switch_legacy_probe_floor(self):
        out = ep.risk_for_signal(
            EVAL, 4.0, 80.0, 200.0, 30.0, 1.0, 5.0, 100.0,
            asset="xyz:GOLD", tradfi_min_enabled=True, tradfi_min_risk=14.0,
            equity=5019.0, tradfi_account_risk_enabled=False,
            tradfi_account_risk_frac=0.005)
        assert out == 14.0

    def test_day_budget_still_clamps_account_risk(self):
        # 0.5% of $5019 ≈ $25 but remaining $10 binds.
        out = ep.risk_for_signal(
            EVAL, 4.0, 10.0, 200.0, 30.0, 1.0, 5.0, 100.0,
            asset="xyz:GOLD", tradfi_min_enabled=True, tradfi_min_risk=14.0,
            equity=5019.0, tradfi_account_risk_enabled=True,
            tradfi_account_risk_frac=0.005)
        assert out == 10.0

    def test_big_bet_still_outranks_when_larger(self):
        # coh>7 → 70% of remaining $80 = $56 > $25 account risk.
        out = ep.risk_for_signal(
            EVAL, 7.5, 80.0, 200.0, 30.0, 1.0, 5.0, 100.0,
            asset="xyz:GOLD", tradfi_min_enabled=True, tradfi_min_risk=14.0,
            equity=5019.0, tradfi_account_risk_enabled=True,
            tradfi_account_risk_frac=0.005)
        assert out == pytest.approx(56.0)


class TestCryptoMinCohFire:
    def test_refuse_below_floor(self):
        assert ep.crypto_min_coh_fire_verdict(
            4.63, "BTC", enabled=True, min_coh=7.5) == (
            ep.CRYPTO_MIN_COH_FIRE_STANDDOWN)

    def test_allow_at_or_above_floor(self):
        assert ep.crypto_min_coh_fire_verdict(
            7.5, "BTC", enabled=True, min_coh=7.5) is None
        assert ep.crypto_min_coh_fire_verdict(
            8.27, "ETH", enabled=True, min_coh=7.5) is None

    def test_stocks_never_bind(self):
        assert ep.crypto_min_coh_fire_verdict(
            2.1, "xyz:CL", enabled=True, min_coh=7.5) is None

    def test_kill_switch_legacy(self):
        assert ep.crypto_min_coh_fire_verdict(
            3.0, "SOL", enabled=False, min_coh=7.5) is None

    def test_zero_threshold_legacy(self):
        assert ep.crypto_min_coh_fire_verdict(
            3.0, "SOL", enabled=True, min_coh=0.0) is None

    def test_dark_coherence_fail_open(self):
        assert ep.crypto_min_coh_fire_verdict(
            None, "BTC", enabled=True, min_coh=7.5) is None


class TestConfigDefaults:
    def test_tradfi_defaults_2x_and_half_pct(self):
        cfg = PropConfig()
        assert cfg.eval_tradfi_min_risk_usd == 14.0
        assert cfg.eval_tradfi_account_risk_enabled is True
        assert cfg.eval_tradfi_account_risk_frac == pytest.approx(0.005)
        assert cfg.crypto_min_coh_fire == pytest.approx(7.5)
        assert cfg.crypto_min_coh_fire_enabled is True
        # Crypto probe floor unchanged — no stock 0.5% bleed.
        assert cfg.eval_crypto_min_risk_usd == 7.0

    def test_crypto_fire_kill_via_env_zero(self, monkeypatch):
        monkeypatch.setenv("PROP_STATE_ROOT", "/tmp/prop_ats103_cfg")
        monkeypatch.setenv("PROP_CRYPTO_MIN_COH_FIRE", "0")
        # Clear sibling noise
        for k in list(os.environ):
            if k.startswith("PROP_") and k not in (
                    "PROP_STATE_ROOT", "PROP_CRYPTO_MIN_COH_FIRE"):
                monkeypatch.delenv(k, raising=False)
        cfg = PropConfig.from_env()
        assert cfg.crypto_min_coh_fire_enabled is False
