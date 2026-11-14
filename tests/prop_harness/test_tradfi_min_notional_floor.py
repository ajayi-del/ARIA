"""Pin: tradfi eval min-notional floor (Governor 2026-11-14).

Sub-floor stock sizes bump up to eval_tradfi_min_notional_usd; bumped risk
is committed honestly and re-checked against the cap. 0 = legacy.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from prop_harness.brains.budget import BudgetBrain
from prop_harness.brains.drawdown import DrawdownBrain
from prop_harness.config import PropConfig
from prop_harness.executor import ProprExecutor


class _OkClient:
    account_id = "acct1"

    def get_open_orders(self, base=None):
        return []

    def place_orders(self, body):
        return [{"orderId": "o1"}]

    def cancel_order(self, order_id):
        return {}


def _exec(tmp_path: Path, min_notional: float) -> ProprExecutor:
    cfg = PropConfig(
        enabled=True, state_root=tmp_path, profile="evaluation",
        asset_class="tradfi", crypto_comove_enabled=False,
        family_cap_enabled=False, post_win_respend_enabled=False,
        cost_move_gate_enabled=False, max_positions=7,
        slot_reserve_enabled=False, eval_crypto_min_risk_enabled=False,
        crypto_min_coh_fire_enabled=False,
        eval_tradfi_min_notional_usd=min_notional,
    )
    (tmp_path / "journal").mkdir(exist_ok=True)
    dd = DrawdownBrain(tmp_path / "dd.json", floor_line=4850.0,
                       floor_usd=150.0)
    dd.update(5000.0)
    budget = BudgetBrain(tmp_path / "budget.json", hard_zero=30.0,
                         divisor=10.0, floor_budget=40.0)
    budget.roll(150.0)
    return ProprExecutor(cfg, _OkClient(), budget, dd)


class TestTradfiMinNotionalFloor:
    def test_sub_floor_bumps_and_commits_honest_risk(self, tmp_path):
        ex = _exec(tmp_path, 400.0)
        # risk $14, stop dist 8 -> natural qty 1.75 = $175 notional < floor
        res = ex.place_bracket("xyz:MU", "buy", 14.0, 100.0, 92.0)
        assert res.ok is True
        br = ex.open_brackets()[0]
        assert float(br["quantity"]) * 100.0 == pytest.approx(400.0)
        # bumped risk = 4.0 qty x $8 stop = $32, committed honestly
        assert br["risk_usd"] == pytest.approx(32.0)
        assert ex.budget.state.committed == pytest.approx(32.0)
        journal = (tmp_path / "journal" / "events.jsonl").read_text()
        assert "tradfi_min_notional_bump" in journal

    def test_above_floor_untouched(self, tmp_path):
        ex = _exec(tmp_path, 400.0)
        # risk $14, stop dist 1 -> qty 14 = $1,400 notional, no bump
        res = ex.place_bracket("xyz:MU", "buy", 14.0, 100.0, 99.0)
        assert res.ok is True
        assert ex.budget.state.committed == pytest.approx(14.0)

    def test_floor_bump_respects_cap(self, tmp_path):
        ex = _exec(tmp_path, 400.0)
        # stop dist 12 -> bumped risk $48 > $40 cap -> reject, no commit
        res = ex.place_bracket("xyz:MU", "buy", 14.0, 100.0, 88.0)
        assert res.ok is False
        assert "cap" in res.reason
        assert ex.budget.state.committed == pytest.approx(0.0)

    def test_zero_floor_is_legacy(self, tmp_path):
        ex = _exec(tmp_path, 0.0)
        res = ex.place_bracket("xyz:MU", "buy", 14.0, 100.0, 92.0)
        assert res.ok is True
        assert ex.budget.state.committed == pytest.approx(14.0)

    def test_crypto_sleeve_never_bumps(self, tmp_path):
        ex = _exec(tmp_path, 400.0)
        cfg = PropConfig(
            enabled=True, state_root=ex.config.state_root, profile="evaluation",
            asset_class="crypto", crypto_comove_enabled=False,
            family_cap_enabled=False, post_win_respend_enabled=False,
            cost_move_gate_enabled=False, max_positions=7,
            slot_reserve_enabled=False, eval_crypto_min_risk_enabled=False,
            crypto_min_coh_fire_enabled=False,
            eval_tradfi_min_notional_usd=400.0,
        )
        ex = ProprExecutor(cfg, _OkClient(), ex.budget, ex.drawdown)
        res = ex.place_bracket("xyz:MU", "buy", 14.0, 100.0, 92.0)
        assert res.ok is True
        assert ex.budget.state.committed == pytest.approx(14.0)
