"""Pin: per-class big-bet threshold + tradfi fire floor (Governor 2026-11-15).

Stocks never print crypto-scale coherence (7,355 stock taps, one >=7.5
ever, p95 3.82); the stock edge band is 4.x (only green multi-n bucket,
both sessions). Defaults (None / 0.0) reproduce the pre-module system
bit-for-bit.
"""

from __future__ import annotations

import calendar
import time
from pathlib import Path

import pytest

from prop_harness.brains import eval_profile as ep
from prop_harness.brains.budget import BudgetBrain
from prop_harness.brains.drawdown import DrawdownBrain
from prop_harness.config import PropConfig
from prop_harness.executor import ProprExecutor


def _ts(hour_utc: int) -> float:
    return float(calendar.timegm(
        time.struct_time((2026, 11, 14, hour_utc, 0, 0, 0, 0, 0, 0))))


class TestCohBigFor:
    def test_legacy_default_base_everywhere(self):
        assert ep.coh_big_for("BTC", _ts(15), base=7.0) == 7.0
        assert ep.coh_big_for("xyz:MU", _ts(15), base=7.0) == 7.0
        assert ep.coh_big_for("xyz:GOLD", _ts(14), base=7.5,
                              tradfi=None, tradfi_usopen=None) == 7.5

    def test_tradfi_binds_xyz_only(self):
        assert ep.coh_big_for("xyz:MU", _ts(15), base=7.5,
                              tradfi=4.0) == 4.0
        assert ep.coh_big_for("xyz:GOLD", _ts(15), base=7.5,
                              tradfi=4.0) == 4.0
        # crypto untouched by the tradfi knobs
        assert ep.coh_big_for("BTC", _ts(15), base=7.5,
                              tradfi=4.0, tradfi_usopen=3.5) == 7.5
        assert ep.coh_big_for("SOL", _ts(14), base=7.0,
                              tradfi=4.0, tradfi_usopen=3.5) == 7.0

    def test_usopen_window_relaxes_in_window_only(self):
        # 14 UTC in window -> min(4.0, 3.5); 22 UTC off -> base tradfi
        assert ep.coh_big_for("xyz:MU", _ts(14), base=7.5,
                              tradfi=4.0, tradfi_usopen=3.5) == 3.5
        assert ep.coh_big_for("xyz:MU", _ts(13), base=7.5,
                              tradfi=4.0, tradfi_usopen=3.5) == 3.5
        assert ep.coh_big_for("xyz:MU", _ts(19), base=7.5,
                              tradfi=4.0, tradfi_usopen=3.5) == 3.5
        assert ep.coh_big_for("xyz:MU", _ts(20), base=7.5,
                              tradfi=4.0, tradfi_usopen=3.5) == 4.0
        assert ep.coh_big_for("xyz:MU", _ts(22), base=7.5,
                              tradfi=4.0, tradfi_usopen=3.5) == 4.0

    def test_usopen_without_tradfi_relaxes_from_base(self):
        assert ep.coh_big_for("xyz:MU", _ts(14), base=7.0,
                              tradfi_usopen=3.5) == 3.5
        assert ep.coh_big_for("xyz:MU", _ts(22), base=7.0,
                              tradfi_usopen=3.5) == 7.0

    def test_dark_ts_never_relaxes(self):
        assert ep.coh_big_for("xyz:MU", None, base=7.5,
                              tradfi=4.0, tradfi_usopen=3.5) == 4.0
        assert ep.coh_big_for("xyz:MU", 0, base=7.5,
                              tradfi=4.0, tradfi_usopen=3.5) == 4.0

    def test_bad_inputs_fall_back(self):
        assert ep.coh_big_for("xyz:MU", _ts(14), base="x") == 7.0
        assert ep.coh_big_for("xyz:MU", _ts(14), base=7.5,
                              tradfi="nan") == 7.5
        assert ep.coh_big_for("xyz:MU", _ts(14), base=7.5,
                              tradfi=-1) == 7.5


class TestTradfiMinCohFireVerdict:
    def test_off_by_default(self):
        assert ep.tradfi_min_coh_fire_verdict(3.0, "xyz:MU") is None
        assert ep.tradfi_min_coh_fire_verdict(
            3.0, "xyz:MU", min_coh=0.0) is None

    def test_crypto_never_binds(self):
        assert ep.tradfi_min_coh_fire_verdict(
            1.0, "BTC", min_coh=4.0) is None

    def test_binds_subfloor_stocks(self):
        assert ep.tradfi_min_coh_fire_verdict(
            3.9, "xyz:MU", min_coh=4.0) == ep.TRADFI_MIN_COH_FIRE_STANDDOWN
        assert ep.tradfi_min_coh_fire_verdict(
            4.0, "xyz:MU", min_coh=4.0) is None
        assert ep.tradfi_min_coh_fire_verdict(
            4.2, "xyz:GOLD", min_coh=4.0) is None

    def test_fail_open_dark_coh_and_kill(self):
        assert ep.tradfi_min_coh_fire_verdict(
            None, "xyz:MU", min_coh=4.0) is None
        assert ep.tradfi_min_coh_fire_verdict(
            3.0, "xyz:MU", enabled=False, min_coh=4.0) is None


class TestStockCohEase330:
    """ATS-199: live stock seats ease PROP_TRADFI_MIN_COH_FIRE 3.5→3.30.
    Crypto 5.7 + GOLD/XAUT symbol floor 4.0 must stay untouched."""

    LIVE_STOCK_FLOOR = 3.30

    def test_stock_boundary_at_330(self):
        assert ep.tradfi_min_coh_fire_verdict(
            3.29, "xyz:MU", min_coh=self.LIVE_STOCK_FLOOR
        ) == ep.TRADFI_MIN_COH_FIRE_STANDDOWN
        assert ep.tradfi_min_coh_fire_verdict(
            3.30, "xyz:MU", min_coh=self.LIVE_STOCK_FLOOR) is None
        assert ep.tradfi_min_coh_fire_verdict(
            3.49, "xyz:XYZ100", min_coh=self.LIVE_STOCK_FLOOR) is None

    def test_prior_35_still_stricter(self):
        # Kill/revert path: putting 3.5 back re-arms the old refuse band.
        assert ep.tradfi_min_coh_fire_verdict(
            3.40, "xyz:MU", min_coh=3.5
        ) == ep.TRADFI_MIN_COH_FIRE_STANDDOWN

    def test_crypto_floor_unaffected_by_stock_ease(self):
        assert ep.crypto_min_coh_fire_verdict(
            5.69, "BTC", min_coh=5.7
        ) == ep.CRYPTO_MIN_COH_FIRE_STANDDOWN
        assert ep.crypto_min_coh_fire_verdict(
            5.7, "ETH", min_coh=5.7) is None
        # Stock ease knob never binds crypto assets.
        assert ep.tradfi_min_coh_fire_verdict(
            1.0, "BTC", min_coh=self.LIVE_STOCK_FLOOR) is None

    def test_env_parses_330(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PROP_STATE_ROOT", str(tmp_path))
        monkeypatch.setenv("PROP_TRADFI_MIN_COH_FIRE", "3.30")
        monkeypatch.setenv("PROP_CRYPTO_MIN_COH_FIRE", "5.7")
        cfg = PropConfig.from_env()
        assert cfg.tradfi_min_coh_fire == 3.30
        assert cfg.tradfi_min_coh_fire_enabled is True
        assert cfg.crypto_min_coh_fire == 5.7


class _OkClient:
    account_id = "acct1"

    def get_open_orders(self, base=None):
        return []

    def place_orders(self, body):
        return [{"orderId": "o1"}]

    def cancel_order(self, order_id):
        return {}


def _exec(tmp_path: Path, **over) -> ProprExecutor:
    knobs = dict(
        enabled=True, state_root=tmp_path, profile="evaluation",
        asset_class="all", crypto_comove_enabled=False,
        family_cap_enabled=False, post_win_respend_enabled=False,
        cost_move_gate_enabled=False, max_positions=7,
        slot_reserve_enabled=False, eval_crypto_min_risk_enabled=False,
        crypto_min_coh_fire_enabled=False,
        eval_margin_cap_frac=0.99,
        eval_trend_budget_usd=5_000.0,
    )
    knobs.update(over)
    cfg = PropConfig(**knobs)
    (tmp_path / "journal").mkdir(exist_ok=True)
    dd = DrawdownBrain(tmp_path / "dd.json", floor_line=1000.0,
                       floor_usd=150.0)
    dd.update(5000.0)
    budget = BudgetBrain(tmp_path / "budget.json", hard_zero=30.0,
                         divisor=10.0, floor_budget=40.0)
    budget.roll(10_000.0)
    return ProprExecutor(cfg, _OkClient(), budget, dd)


class TestTradfiFireFloorExecutor:
    def test_subfloor_stock_refused(self, tmp_path):
        ex = _exec(tmp_path, tradfi_min_coh_fire=4.0)
        res = ex.place_bracket("xyz:MU", "buy", 14.0, 100.0, 92.0,
                               coherence=3.1)
        assert res.ok is False
        assert ep.TRADFI_MIN_COH_FIRE_STANDDOWN in res.reason

    def test_above_floor_stock_places(self, tmp_path):
        ex = _exec(tmp_path, tradfi_min_coh_fire=4.0)
        res = ex.place_bracket("xyz:MU", "buy", 14.0, 100.0, 92.0,
                               coherence=4.5)
        assert res.ok is True

    def test_crypto_unaffected(self, tmp_path):
        ex = _exec(tmp_path, tradfi_min_coh_fire=4.0)
        res = ex.place_bracket("BTC", "buy", 14.0, 100.0, 92.0,
                               coherence=1.0)
        assert res.ok is True

    def test_zero_floor_legacy(self, tmp_path):
        ex = _exec(tmp_path)  # tradfi_min_coh_fire defaults 0.0
        res = ex.place_bracket("xyz:MU", "buy", 14.0, 100.0, 92.0,
                               coherence=1.1)
        assert res.ok is True


class TestConfigParse:
    def test_defaults_are_legacy(self, monkeypatch, tmp_path):
        for k in ("PROP_EVAL_COH_BIG_TRADFI",
                  "PROP_EVAL_COH_BIG_TRADFI_USOPEN",
                  "PROP_TRADFI_MIN_COH_FIRE"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("PROP_STATE_ROOT", str(tmp_path))
        cfg = PropConfig.from_env()
        assert cfg.eval_coh_big_tradfi is None
        assert cfg.eval_coh_big_tradfi_usopen is None
        assert cfg.tradfi_min_coh_fire == 0.0
        assert cfg.tradfi_min_coh_fire_enabled is True

    def test_env_binds(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PROP_STATE_ROOT", str(tmp_path))
        monkeypatch.setenv("PROP_EVAL_COH_BIG_TRADFI", "4.0")
        monkeypatch.setenv("PROP_EVAL_COH_BIG_TRADFI_USOPEN", "3.5")
        monkeypatch.setenv("PROP_TRADFI_MIN_COH_FIRE", "4.0")
        cfg = PropConfig.from_env()
        assert cfg.eval_coh_big_tradfi == 4.0
        assert cfg.eval_coh_big_tradfi_usopen == 3.5
        assert cfg.tradfi_min_coh_fire == 4.0

    def test_zero_disables(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PROP_STATE_ROOT", str(tmp_path))
        monkeypatch.setenv("PROP_EVAL_COH_BIG_TRADFI", "0")
        monkeypatch.setenv("PROP_TRADFI_MIN_COH_FIRE", "0")
        cfg = PropConfig.from_env()
        assert cfg.eval_coh_big_tradfi is None
        assert cfg.tradfi_min_coh_fire_enabled is False
