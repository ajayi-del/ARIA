"""Pin: ProprAPIError after budget.commit must release (429 phantom latch).

2026-11-11 turbo silence: place_orders raised ProprAPIError(429) after
commit; only ProprUnknownState released → committed phantom → day latch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from prop_harness.brains.budget import BudgetBrain
from prop_harness.brains.drawdown import DrawdownBrain
from prop_harness.config import PropConfig
from prop_harness.executor import ProprExecutor
from prop_harness.io.propr_client import ProprAPIError


class _RejectClient:
    account_id = "acct1"

    def get_open_orders(self, base=None):
        return []

    def place_orders(self, body):
        raise ProprAPIError(429, 10009, "too_many_requests")

    def cancel_order(self, order_id):
        return {}


def _exec(tmp_path: Path) -> ProprExecutor:
    cfg = PropConfig(
        enabled=True, state_root=tmp_path, profile="evaluation",
        asset_class="crypto", crypto_comove_enabled=False,
        family_cap_enabled=False, post_win_respend_enabled=False,
        cost_move_gate_enabled=False, max_positions=7,
        slot_reserve_enabled=False, eval_crypto_min_risk_enabled=False,
        crypto_min_coh_fire_enabled=False,
    )
    (tmp_path / "journal").mkdir(exist_ok=True)
    dd = DrawdownBrain(tmp_path / "dd.json", floor_line=4850.0,
                       floor_usd=150.0)
    dd.update(5000.0)
    budget = BudgetBrain(tmp_path / "budget.json", hard_zero=30.0,
                         divisor=10.0, floor_budget=40.0)
    budget.roll(150.0)
    return ProprExecutor(cfg, _RejectClient(), budget, dd)


class TestBracketPlaceApiErrorRelease:
    def test_429_releases_committed(self, tmp_path):
        ex = _exec(tmp_path)
        before = ex.budget.remaining
        committed_before = ex.budget.state.committed
        res = ex.place_bracket("ETH", "sell", 7.0, 2000.0, 2020.0)
        assert res.ok is False
        assert "place rejected" in res.reason
        assert "429" in res.reason
        assert ex.budget.state.committed == pytest.approx(committed_before)
        assert ex.budget.remaining == pytest.approx(before)
        # journaled for census
        journal = (tmp_path / "journal" / "events.jsonl").read_text()
        assert "bracket_place_rejected" in journal
        assert "too_many_requests" in journal or "429" in journal
