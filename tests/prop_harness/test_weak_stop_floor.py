"""Pins: widen-only birth stop floor + early-tighten management gate.

Doctrine 2026-11-19 (soft box $150, BTC 7s/22s proof):
  Birth ≠ management. Floor widens only; management waits age+R★.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from prop_harness.brains import early_tighten_gate as etg
from prop_harness.brains import stop_floor as sf
from prop_harness.brains.budget import BudgetBrain
from prop_harness.brains.drawdown import DrawdownBrain
from prop_harness.config import PropConfig
from prop_harness.executor import ProprExecutor


def _journal_rows(tmp_path: Path) -> list[dict]:
    import json
    path = tmp_path / "journal" / "events.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


class _FakeClient:
    def __init__(self):
        self.account_id = "acct1"
        self.placed: list = []

    def get_open_orders(self, base=None):
        return []

    def place_orders(self, body):
        self.placed.append(body)
        return [{"orderId": f"urn:prp-order:{len(self.placed)}",
                 **body["orders"][0]}]

    def cancel_order(self, order_id):
        return {}


def _exec(tmp_path: Path, **kw) -> ProprExecutor:
    kw.setdefault("profile", "evaluation")
    kw.setdefault("asset_class", "crypto")
    kw.setdefault("crypto_comove_enabled", False)
    kw.setdefault("family_cap_enabled", False)
    kw.setdefault("post_win_respend_enabled", False)
    kw.setdefault("cost_move_gate_live", False)
    kw.setdefault("crypto_min_coh_fire_enabled", False)
    kw.setdefault("slot_reserve_enabled", False)
    kw.setdefault("max_positions", 7)
    kw.setdefault("stop_floor_enabled", True)
    kw.setdefault("stop_floor_pct_crypto", 0.75)
    kw.setdefault("stop_floor_pct_tradfi", 0.50)
    cfg = PropConfig(enabled=True, state_root=tmp_path, **kw)
    (tmp_path / "journal").mkdir(exist_ok=True)
    dd = DrawdownBrain(tmp_path / "dd.json", floor_line=4850.0,
                       floor_usd=150.0)
    dd.update(5000.0)
    budget = BudgetBrain(tmp_path / "budget.json", hard_zero=30.0,
                         divisor=10.0, floor_budget=40.0)
    budget.roll(150.0)
    return ProprExecutor(cfg, _FakeClient(), budget, dd)


# ── Birth floor (pure) ──────────────────────────────────────────────

class TestStopFloorPure:
    def test_widens_035_crypto_to_075(self):
        # Live BTC class: 0.35% birth → floor to 0.75%.
        entry, stop = 63234.0, 63234.0 * (1 - 0.0035)
        new, floored, d = sf.stop_floor_verdict(
            stop, entry, "buy", asset_class="crypto")
        assert floored is True
        assert abs(entry - new) / entry * 100 >= 0.74
        assert d["floor_leg"] == "pct_atr_dark"
        assert d["floor_pct"] == 0.75

    def test_tradfi_class_lower_than_crypto(self):
        entry = 100.0
        stop = 100.0 * (1 - 0.0035)  # 0.35%
        c_new, c_hit, _ = sf.stop_floor_verdict(
            stop, entry, "buy", asset_class="crypto")
        t_new, t_hit, td = sf.stop_floor_verdict(
            stop, entry, "buy", asset_class="tradfi")
        assert c_hit and t_hit
        assert abs(entry - c_new) > abs(entry - t_new)
        assert td["floor_pct"] == 0.50

    def test_never_tightens_wide_birth(self):
        entry, stop = 100.0, 98.0  # 2% already wide
        new, floored, _ = sf.stop_floor_verdict(
            stop, entry, "buy", asset_class="crypto")
        assert floored is False
        assert new == stop

    def test_atr_wider_wins(self):
        entry, stop = 100.0, 99.7  # 0.3%
        # ATR 1.5 > pct_dist 0.75 → ATR leg
        new, floored, d = sf.stop_floor_verdict(
            stop, entry, "buy", asset_class="crypto",
            atr=1.5, atr_mult=1.0)
        assert floored is True
        assert d["floor_leg"] == "atr"
        assert abs(entry - new) >= 1.5 - 1e-9

    def test_atr_dark_fail_open_pct_only(self):
        entry, stop = 100.0, 99.7
        _, floored, d = sf.stop_floor_verdict(
            stop, entry, "buy", asset_class="crypto", atr=None)
        assert floored is True
        assert d["floor_leg"] == "pct_atr_dark"

    def test_kill_pct_zero_unchanged(self):
        entry, stop = 100.0, 99.7
        new, floored, _ = sf.stop_floor_verdict(
            stop, entry, "buy", floor_pct=0.0)
        assert floored is False
        assert new == stop

    def test_fee_honest_crypto_clears_3x_rt(self):
        # RT @ 0.045% = 0.09%; 0.75% = 8.3× ≥ 3× doctrine bar.
        rt = 2 * 0.00045
        assert 0.75 / 100 / rt >= 3.0
        assert 0.35 / 100 / rt < 5.0  # live death band is thin

    def test_short_side_widens_above(self):
        entry, stop = 100.0, 100.35  # 0.35% short stop above
        new, floored, _ = sf.stop_floor_verdict(
            stop, entry, "sell", asset_class="crypto")
        assert floored is True
        assert new > entry
        assert (new - entry) / entry * 100 >= 0.74


# ── Management gate (pure) ──────────────────────────────────────────

class TestEarlyTightenPure:
    def _br(self, age_s, mfe=None):
        return {"opened_ts": time.time() - age_s,
                "mfe_peak_r": mfe, "status": "open"}

    def test_blocks_btc_7s_trail(self):
        ok, d = etg.early_tighten_allowed(
            self._br(7, mfe=2.5), stage="trail")
        assert ok is False
        assert d["reason"] == "age_below_min"

    def test_blocks_btc_22s_harvest_even_with_mfe(self):
        ok, d = etg.early_tighten_allowed(
            self._br(22, mfe=2.8), stage="runner_harvest")
        assert ok is False
        assert d["reason"] == "age_below_min"

    def test_be_allows_at_300s_with_05r(self):
        ok, d = etg.early_tighten_allowed(
            self._br(300, mfe=0.5), stage="breakeven_mark")
        assert ok is True
        assert d["reason"] == "allowed"

    def test_be_blocks_at_300s_with_04r(self):
        ok, d = etg.early_tighten_allowed(
            self._br(300, mfe=0.4), stage="breakeven")
        assert ok is False
        assert d["reason"] == "mfe_below_min"
        assert d["min_mfe_r"] == 0.5

    def test_harvest_needs_1r_after_age(self):
        ok, _ = etg.early_tighten_allowed(
            self._br(400, mfe=0.9), stage="runner_harvest")
        assert ok is False
        ok2, _ = etg.early_tighten_allowed(
            self._br(400, mfe=1.0), stage="runner_harvest")
        assert ok2 is True

    def test_kill_off_allows_immediate(self):
        ok, d = etg.early_tighten_allowed(
            self._br(1, mfe=0.0), enabled=False, stage="trail")
        assert ok is True
        assert d["reason"] == "kill_off"

    def test_missing_opened_ts_fail_open(self):
        ok, d = etg.early_tighten_allowed(
            {"mfe_peak_r": 2.0}, stage="trail")
        assert ok is True
        assert d["reason"] == "missing_opened_ts_fail_open"

    def test_missing_mfe_fail_open_after_age(self):
        ok, d = etg.early_tighten_allowed(
            self._br(400, mfe=None), stage="breakeven")
        assert ok is True
        assert d["reason"] == "missing_mfe_fail_open"

    def test_stage_mfe_asymmetry(self):
        assert etg.min_mfe_for_stage("breakeven_mark") == 0.5
        assert etg.min_mfe_for_stage("trail") == 0.5
        assert etg.min_mfe_for_stage("bank") == 0.5
        assert etg.min_mfe_for_stage("runner_harvest") == 1.0
        assert etg.min_mfe_for_stage("live_peak") == 1.0
        assert etg.min_mfe_for_stage("mfe_sharpen") == 1.0


# ── Executor splice ─────────────────────────────────────────────────

class TestExecutorSplice:
    def test_place_bracket_floors_and_journals(self, tmp_path):
        ex = _exec(tmp_path)
        # BTC-like 0.35% long stop
        entry = 63000.0
        stop = entry * (1 - 0.0035)
        res = ex.place_bracket(
            "BTC", "buy", risk_usd=10.0,
            entry_ref=entry, stop_price=stop, coherence=7.5)
        assert res.ok, res.reason
        rows = _journal_rows(tmp_path)
        floored = [r for r in rows if r.get("type") == "stop_floored"]
        assert len(floored) == 1
        assert floored[0]["new_pct"] >= 0.74
        # Bracket registry carries the WIDENED stop
        br = ex.open_brackets()[0]
        assert abs(float(br["entry_ref"]) - float(br["stop_price"])) / entry \
            >= 0.0074 - 1e-6

    def test_kill_off_preserves_tight_birth(self, tmp_path):
        ex = _exec(tmp_path, stop_floor_enabled=False)
        entry = 63000.0
        stop = entry * (1 - 0.0035)
        res = ex.place_bracket(
            "BTC", "buy", risk_usd=10.0,
            entry_ref=entry, stop_price=stop, coherence=7.5)
        assert res.ok, res.reason
        rows = _journal_rows(tmp_path)
        assert not any(r.get("type") == "stop_floored" for r in rows)
        br = ex.open_brackets()[0]
        assert abs(float(br["stop_price"]) - stop) < 1e-6

    def test_early_tighten_blocks_young_trail(self, tmp_path):
        ex = _exec(tmp_path)
        bracket = {
            "semantic_id": "prop-acct1-BTC-1",
            "asset": "BTC", "side": "buy",
            "entry_ref": "63000", "stop_price": "62500",
            "current_stop": "62500",
            "opened_ts": time.time() - 22.0,
            "mfe_peak_r": 2.5,
            "status": "open",
        }
        assert ex._early_tighten_ok(bracket, "trail") is False
        assert ex._early_tighten_ok(bracket, "runner_harvest") is False
        rows = _journal_rows(tmp_path)
        assert any(r.get("type") == "early_tighten_blocked" for r in rows)

    def test_config_defaults_doctrine(self):
        cfg = PropConfig()
        assert cfg.stop_floor_enabled is True
        assert cfg.stop_floor_pct_crypto == 0.75
        assert cfg.stop_floor_pct_tradfi == 0.50
        assert cfg.early_tighten_gate_enabled is True
        assert cfg.early_tighten_min_age_s == 300.0
        assert cfg.early_tighten_be_min_mfe_r == 0.5
        assert cfg.early_tighten_harvest_min_mfe_r == 1.0
