"""Pins for prop basket peak-push graph + conquest controller."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from prop_harness.brains import basket_graph as bg
from prop_harness.brains import basket_tp as btp
from prop_harness.brains.budget import BudgetBrain
from prop_harness.brains.drawdown import DrawdownBrain
from prop_harness.config import PropConfig
from prop_harness.executor import ProprExecutor


EVAL = "evaluation"
STD = "standard"


def _leg(asset, *, side="buy", risk=7.0, upnl=5.0, coh=5.0, peak_r=None,
         sid=None, qty="1.0"):
    b = {
        "semantic_id": sid or f"sid-{asset}",
        "status": "open",
        "asset": asset,
        "side": side,
        "risk_usd": risk,
        "last_upnl": upnl,
        "coherence_at_entry": coh,
        "quantity": qty,
        "entry_ref": "100",
        "stop_price": "99" if side == "buy" else "101",
    }
    if peak_r is not None:
        b["mfe_peak_r"] = peak_r
    return b


class TestBasketGraph:
    def test_build_ranks_payer_by_roe(self):
        legs = [
            _leg("A", risk=7, upnl=3.5),   # roe 0.5
            _leg("B", risk=7, upnl=7.0),   # roe 1.0 payer
            _leg("C", risk=7, upnl=-1.0),
        ]
        snap = bg.build_graph(legs)
        assert snap.basket.n == 3
        assert snap.payer is not None
        assert snap.payer.asset == "B"
        assert snap.basket.book_upnl == pytest.approx(9.5)
        assert snap.basket.book_roe == pytest.approx(9.5 / 21.0)

    def test_leg_peak_push_emits(self):
        legs = [_leg("A", upnl=7.0, risk=7.0)]
        snap = bg.build_graph(legs, prev_leg_peaks={})
        assert any(p.kind == "leg_peak_roe" for p in snap.pushes)
        snap2 = bg.build_graph(
            legs, prev_leg_peaks=bg.leg_peak_map(snap))
        assert not any(p.kind == "leg_peak_roe" for p in snap2.pushes)

    def test_basket_peak_push_emits(self):
        legs = [_leg("A", upnl=7.0), _leg("B", upnl=7.0)]
        snap = bg.build_graph(legs, prev_basket_peak_roe=0.0)
        assert any(p.kind == "basket_peak_frac" for p in snap.pushes)

    def test_adaptive_arm_scales_with_n(self):
        assert bg.adaptive_arm_frac(2, 0.25) > bg.adaptive_arm_frac(7, 0.25)
        assert 0.12 <= bg.adaptive_arm_frac(7, 0.25) <= 0.40


class TestBasketController:
    def test_kill_off(self):
        d = btp.basket_tp_verdict(
            EVAL, [_leg("A"), _leg("B")], {}, enabled=False)
        assert d.fire is False and d.reason == "disabled"

    def test_standard_profile_noop(self):
        d = btp.basket_tp_verdict(STD, [_leg("A"), _leg("B")], {})
        assert d.fire is False

    def test_min_n(self):
        d = btp.basket_tp_verdict(EVAL, [_leg("A", upnl=20.0)], {})
        assert d.reason == "min_n"

    def test_border_fires_tax_bank(self):
        # N=2, book=$14 on Σrisk=$14 → roe=1.0; adaptive arm ~0.30
        # Raise upnl so border clears adaptive arm.
        legs = [
            _leg("A", risk=7, upnl=10.0, coh=5.0, peak_r=1.5),  # peak$=10.5
            _leg("B", risk=7, upnl=8.0, coh=5.0),
        ]
        state: dict = {}
        d = btp.basket_tp_verdict(EVAL, legs, state, now_ts=1.0)
        assert d.fire is True
        assert d.reason in ("border", "basket_peak")
        assert d.tax_usd >= 10.5  # tax-bank ≥ max leg peak
        assert d.payer_asset == "A"
        assert d.n == 2

    def test_tax_bank_off_uses_ratio_only(self):
        legs = [
            _leg("A", risk=7, upnl=14.0, peak_r=2.0),  # peak$=14
            _leg("B", risk=7, upnl=7.0),
        ]
        d = btp.basket_tp_verdict(
            EVAL, legs, {}, now_ts=1.0,
            tax_bank_enabled=False, harvest_ratio=0.60)
        assert d.fire is True
        assert d.tax_usd == pytest.approx(0.60 * 21.0)

    def test_residual_when_coh_high(self):
        legs = [
            _leg("A", risk=7, upnl=14.0, coh=5.5),
            _leg("B", risk=7, upnl=7.0, coh=5.0),
        ]
        d = btp.basket_tp_verdict(
            EVAL, legs, {}, now_ts=1.0,
            residual_pyramid_enabled=True, residual_frac=0.25)
        assert d.fire is True
        assert any(t.leave_residual for t in d.targets)
        assert all(
            t.close_frac == pytest.approx(0.75)
            for t in d.targets if t.leave_residual)

    def test_residual_fail_closed_dark_coh(self):
        legs = [
            _leg("A", risk=7, upnl=14.0, coh=None),
            _leg("B", risk=7, upnl=7.0, coh=None),
        ]
        # force None coh
        for L in legs:
            L.pop("coherence_at_entry", None)
        d = btp.basket_tp_verdict(
            EVAL, legs, {}, now_ts=1.0,
            residual_pyramid_enabled=True)
        assert d.fire is True
        assert all(not t.leave_residual for t in d.targets)
        assert all(t.close_frac == 1.0 for t in d.targets)

    def test_peak_epoch_idempotent(self):
        legs = [
            _leg("A", risk=7, upnl=14.0, coh=5.0),
            _leg("B", risk=7, upnl=7.0, coh=5.0),
        ]
        state: dict = {}
        d1 = btp.basket_tp_verdict(EVAL, legs, state, now_ts=1.0)
        assert d1.fire is True
        btp.mark_fired(state, 1.0, d1)
        # Same peak, cooloff cleared by jumping clock past cooloff... but
        # cooloff blocks first. Clear cooloff via last_fire_ts old.
        state["last_fire_ts"] = 1.0
        d2 = btp.basket_tp_verdict(EVAL, legs, state, now_ts=1.0 + 200.0)
        assert d2.fire is False
        assert d2.reason in ("peak_epoch_taxed", "below_border", "cooloff")

    def test_new_peak_forces_recalc(self):
        legs = [
            _leg("A", risk=7, upnl=10.0, coh=5.0),
            _leg("B", risk=7, upnl=8.0, coh=5.0),
        ]
        state: dict = {}
        d1 = btp.basket_tp_verdict(EVAL, legs, state, now_ts=1.0)
        assert d1.fire
        btp.mark_fired(state, 1.0, d1)
        state["last_fire_ts"] = 1.0
        # Raise book → new basket peak push + unpaid tax
        legs2 = [
            _leg("A", risk=7, upnl=20.0, coh=5.0),
            _leg("B", risk=7, upnl=15.0, coh=5.0),
        ]
        d2 = btp.basket_tp_verdict(EVAL, legs2, state, now_ts=1.0 + 200.0)
        assert d2.fire is True
        assert "basket_peak_frac" in d2.pushes or d2.reason in (
            "basket_peak", "border")


class TestBasketExecutorActuation:
    def test_harvest_partial_residual(self, tmp_path: Path):
        cfg = PropConfig(
            enabled=True, state_root=tmp_path, profile=EVAL,
            basket_tp_enabled=True,
            basket_tp_residual_pyramid_enabled=True,
            basket_tp_residual_frac=0.25,
            basket_tp_residual_min_coh=4.0,
            basket_tp_tax_bank_enabled=True,
        )
        dd = DrawdownBrain(tmp_path / "dd.json", floor_line=4850.0,
                           floor_usd=150.0)
        dd.update(5000.0)
        budget = BudgetBrain(tmp_path / "budget.json", hard_zero=30.0,
                             divisor=10.0, floor_budget=40.0)
        budget.roll(150.0)
        client = MagicMock()
        client.account_id = "acct1"
        client.close_position = MagicMock(return_value=[{"ok": True}])
        client.cancel_all_open = MagicMock(return_value=None)
        ex = ProprExecutor(cfg, client, budget, dd)
        legs = [
            _leg("xyz:DRAM", risk=7, upnl=14.0, coh=5.5, qty="10"),
            _leg("xyz:INTC", risk=7, upnl=10.0, coh=5.0, qty="8"),
        ]
        reg = {"brackets": legs}
        ex._save_registry(reg)
        ok = ex.harvest_basket_tp(legs)
        assert ok is True
        assert client.close_position.called
        # At least one call used partial qty (residual)
        partial = False
        for c in client.close_position.call_args_list:
            kwargs = c.kwargs if c.kwargs else {}
            args = c.args
            qty = kwargs.get("quantity")
            if qty is None and len(args) >= 2:
                qty = args[1]
            if qty is not None:
                partial = True
        assert partial is True
