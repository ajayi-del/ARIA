"""ATS-96 coherence size bump — sleeve-aware slight risk mult.

Vultr journals 2026-11-12: stocks ≥3.5 edge (n=14); crypto ≥7.5 edge (n=5).
Kill OFF / latch / dark coh → bit-for-bit no-op. Day budget clamps bind.
"""

from __future__ import annotations

import pytest

from prop_harness.brains import eval_profile as ep
from prop_harness.config import PropConfig

EVAL = ep.PROFILE_EVALUATION


class TestCohSizeBumpMult:
    def test_kill_switch_returns_one(self):
        assert ep.coherence_size_bump_mult(
            9.0, "xyz:GOLD", enabled=False) == 1.0
        assert ep.coherence_size_bump_mult(
            9.0, "BTC", enabled=False) == 1.0

    def test_day_latch_blocks_bump(self):
        assert ep.coherence_size_bump_mult(
            8.0, "BTC", enabled=True, day_latched=True) == 1.0
        assert ep.coherence_size_bump_mult(
            5.0, "xyz:NVDA", enabled=True, day_latched=True) == 1.0

    def test_dark_coherence_fail_closed(self):
        assert ep.coherence_size_bump_mult(None, "BTC") == 1.0
        assert ep.coherence_size_bump_mult("na", "xyz:GOLD") == 1.0

    def test_tradfi_threshold_and_mult(self):
        # below thr → 1.0; at/above → 1.40
        assert ep.coherence_size_bump_mult(
            3.49, "xyz:GOLD", tradfi_threshold=3.5,
            tradfi_mult=1.40) == 1.0
        assert ep.coherence_size_bump_mult(
            3.5, "xyz:GOLD", tradfi_threshold=3.5,
            tradfi_mult=1.40) == pytest.approx(1.40)
        assert ep.coherence_size_bump_mult(
            4.65, "xyz:GOLD", tradfi_threshold=3.5,
            tradfi_mult=1.40) == pytest.approx(1.40)

    def test_crypto_threshold_and_mult(self):
        assert ep.coherence_size_bump_mult(
            7.49, "BTC", crypto_threshold=7.5,
            crypto_mult=1.25) == 1.0
        assert ep.coherence_size_bump_mult(
            7.5, "BTC", crypto_threshold=7.5,
            crypto_mult=1.25) == pytest.approx(1.25)
        assert ep.coherence_size_bump_mult(
            7.8, "ETH", crypto_threshold=7.5,
            crypto_mult=1.25) == pytest.approx(1.25)

    def test_sleeve_split_ignores_other_thr(self):
        # stocks at crypto thr still use tradfi thr
        assert ep.coherence_size_bump_mult(
            7.0, "xyz:SP500", tradfi_threshold=3.5,
            tradfi_mult=1.40, crypto_threshold=7.5,
            crypto_mult=1.25) == pytest.approx(1.40)
        # crypto below 7.5 stays 1.0 even if ≥ stocks thr
        assert ep.coherence_size_bump_mult(
            4.0, "SOL", tradfi_threshold=3.5,
            tradfi_mult=1.40, crypto_threshold=7.5,
            crypto_mult=1.25) == 1.0

    def test_mult_hard_capped_at_1_5(self):
        assert ep.coherence_size_bump_mult(
            9.0, "xyz:GOLD", tradfi_mult=2.0) == pytest.approx(1.50)
        assert ep.coherence_size_bump_mult(
            9.0, "BTC", crypto_mult=3.0) == pytest.approx(1.50)


class TestApplyCohSizeBump:
    def test_stocks_probe_bumped(self):
        out, mult = ep.apply_coherence_size_bump(
            7.0, 3.5, asset="xyz:GOLD",
            tradfi_threshold=3.5, tradfi_mult=1.40,
            remaining=40.0, headroom=200.0, hard_zero=30.0, r_max=100.0)
        assert mult == pytest.approx(1.40)
        assert out == pytest.approx(9.8)

    def test_crypto_big_bet_bumped_then_day_clamp(self):
        # 28 * 1.25 = 35; remaining 40 allows it
        out, mult = ep.apply_coherence_size_bump(
            28.0, 7.5, asset="BTC",
            crypto_threshold=7.5, crypto_mult=1.25,
            remaining=40.0, headroom=200.0, hard_zero=30.0, r_max=100.0)
        assert mult == pytest.approx(1.25)
        assert out == pytest.approx(35.0)

    def test_day_budget_clamp_erases_partial_bump(self):
        # 28 * 1.25 = 35 but remaining only 30 → clamp to 30, still bumped
        out, mult = ep.apply_coherence_size_bump(
            28.0, 7.8, asset="BTC",
            crypto_threshold=7.5, crypto_mult=1.25,
            remaining=30.0, headroom=200.0, hard_zero=30.0, r_max=100.0)
        assert mult == pytest.approx(1.25)
        assert out == pytest.approx(30.0)

    def test_remaining_equal_base_is_noop(self):
        out, mult = ep.apply_coherence_size_bump(
            7.0, 8.0, asset="BTC",
            crypto_threshold=7.5, crypto_mult=1.25,
            remaining=7.0, headroom=200.0, hard_zero=30.0, r_max=100.0)
        assert mult == 1.0
        assert out == 7.0

    def test_kill_and_latch_noop(self):
        out, mult = ep.apply_coherence_size_bump(
            7.0, 8.0, asset="BTC", enabled=False,
            remaining=80.0, r_max=100.0)
        assert (out, mult) == (7.0, 1.0)
        out, mult = ep.apply_coherence_size_bump(
            7.0, 8.0, asset="BTC", day_latched=True,
            remaining=80.0, r_max=100.0)
        assert (out, mult) == (7.0, 1.0)

    def test_below_threshold_noop(self):
        out, mult = ep.apply_coherence_size_bump(
            7.0, 3.0, asset="xyz:GOLD",
            tradfi_threshold=3.5, tradfi_mult=1.40,
            remaining=40.0, r_max=100.0)
        assert (out, mult) == (7.0, 1.0)

    def test_risk_for_signal_unchanged_without_bump_wire(self):
        # Legacy pins: risk_for_signal itself does not bump
        assert ep.risk_for_signal(
            EVAL, 7.5, 40.0, 200.0, 30.0, 1.0, 5.0, 50.0) == 28.0
        assert ep.risk_for_signal(
            EVAL, 3.5, 40.0, 200.0, 30.0, 1.0, 5.0, 50.0,
            asset="xyz:GOLD", tradfi_min_enabled=True,
            tradfi_min_risk=7.0) == 7.0


class TestCohSizeBumpConfig:
    def test_defaults(self):
        c = PropConfig()
        assert c.coh_size_bump_enabled is True
        assert c.coh_size_bump_tradfi_threshold == 3.5
        assert c.coh_size_bump_tradfi_mult == 1.40
        assert c.coh_size_bump_crypto_threshold == 7.5
        assert c.coh_size_bump_crypto_mult == 1.25

    def test_env_kill(self, monkeypatch):
        monkeypatch.setenv("PROP_ENABLED", "0")
        monkeypatch.setenv("PROP_COH_SIZE_BUMP", "0")
        c = PropConfig.from_env()
        assert c.coh_size_bump_enabled is False

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("PROP_ENABLED", "0")
        monkeypatch.setenv("PROP_COH_SIZE_BUMP_TRADFI_THR", "4.0")
        monkeypatch.setenv("PROP_COH_SIZE_BUMP_TRADFI_MULT", "1.5")
        monkeypatch.setenv("PROP_COH_SIZE_BUMP_CRYPTO_THR", "8.0")
        monkeypatch.setenv("PROP_COH_SIZE_BUMP_CRYPTO_MULT", "1.3")
        c = PropConfig.from_env()
        assert c.coh_size_bump_tradfi_threshold == 4.0
        assert c.coh_size_bump_tradfi_mult == 1.5
        assert c.coh_size_bump_crypto_threshold == 8.0
        assert c.coh_size_bump_crypto_mult == 1.3
