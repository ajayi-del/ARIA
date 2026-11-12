"""HOLD-law proof pins for #39 — must all PASS before merge/claim-done.

Checklist (Dayo ULTRATHINK):
  1. 3 shorts net≈−18 reject opposite long
  2. green shorts + pump still reject (book β, not chart)
  3. individual TP credits tax once; replay no double
  4. residual keeps stop after ladder drop
  5. $5k→scale1, $25k→scale5; equity > profit_target
  6. account reset → new tax epoch
  7. missing/stale book fail-closed
  8. chart bullishness cannot bypass comove
  9. side-split: green shorts harvest while longs red
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from prop_harness.brains import basket_graph as bg
from prop_harness.brains import basket_tp as btp
from prop_harness.brains import crypto_comove as ccm
from prop_harness.brains.budget import BudgetBrain
from prop_harness.brains.drawdown import DrawdownBrain
from prop_harness.config import PropConfig
from prop_harness.executor import ProprExecutor


EVAL = "evaluation"


def _leg(asset, *, side="buy", risk=7.0, upnl=5.0, coh=5.0, peak_r=None,
         sid=None, qty="1.0", realized=0.0):
    b = {
        "semantic_id": sid or f"sid-{asset}-{side}",
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
    if realized:
        b["realized_booked"] = realized
    return b


# ── 1+2+8 comove book β ─────────────────────────────────────────────

class TestComoveBookBeta:
    def test_three_shorts_net_neg18_refuse_long(self):
        legs = [
            _leg("ENA", side="sell", risk=4.2, upnl=1.4),
            _leg("TRUMP", side="sell", risk=7.0, upnl=-0.6),
            _leg("HBAR", side="sell", risk=7.0, upnl=-0.4),
        ]
        d = ccm.crypto_comove_detail(legs)
        assert d["n_sell"] == 3 and d["n_buy"] == 0
        assert d["net_signed_risk"] == pytest.approx(-18.2)
        assert d["dominant"] == "sell"
        assert ccm.crypto_comove_verdict(
            True, "BTC", "buy", legs, asset_class="crypto"
        ) == ccm.CRYPTO_COMOVE_OPPOSITE

    def test_green_shorts_into_pump_still_refuse(self):
        # Book green on shorts — still refuse opposite long (book β).
        legs = [
            _leg("ENA", side="sell", risk=4.2, upnl=2.0),
            _leg("TRUMP", side="sell", risk=7.0, upnl=0.5),
            _leg("HBAR", side="sell", risk=7.0, upnl=0.3),
        ]
        assert sum(L["last_upnl"] for L in legs) > 0
        assert ccm.crypto_comove_verdict(
            True, "ETH", "buy", legs
        ) == ccm.CRYPTO_COMOVE_OPPOSITE

    def test_chart_cannot_bypass_comove(self):
        # Verdict signature accepts no chart/tape/SSI — only book legs.
        import inspect
        sig = inspect.signature(ccm.crypto_comove_verdict)
        names = set(sig.parameters)
        for forbidden in ("chart", "tape", "ssi", "btc_mark", "regime",
                          "mark", "klines", "pump"):
            assert forbidden not in names
        # Even with bullish-looking candidate metadata, opposite dies.
        legs = [_leg("BTC", side="sell", risk=7.0, upnl=3.0)]
        assert ccm.crypto_comove_verdict(
            True, "SOL", "buy", legs
        ) == ccm.CRYPTO_COMOVE_OPPOSITE


# ── 5 capital scale ──────────────────────────────────────────────────

class TestCapitalScaleLaw:
    def test_5k_scale1_25k_scale5(self):
        assert bg.capital_scale(5000.0) == pytest.approx(1.0)
        assert bg.capital_scale(25000.0) == pytest.approx(5.0)
        sc, src = bg.resolve_capital_scale(5000.0)
        assert sc == 1.0 and src == bg.SCALE_SOURCE_EQUITY
        sc, src = bg.resolve_capital_scale(None, 2500.0)
        assert sc == 5.0 and src == bg.SCALE_SOURCE_PROFIT_TARGET

    def test_equity_outranks_profit_target(self):
        # Conflicting: equity says 1.0, profit_target would say 5.0 → equity wins.
        sc, src = bg.resolve_capital_scale(5000.0, 2500.0)
        assert sc == pytest.approx(1.0)
        assert src == bg.SCALE_SOURCE_EQUITY
        assert bg.capital_scale(5000.0, 2500.0) == pytest.approx(1.0)


# ── 3+6 tax ledger ───────────────────────────────────────────────────

class TestTaxLedger:
    def test_individual_tp_credits_once_replay_safe(self):
        legs = [
            _leg("A", risk=7, upnl=14.0, coh=5.0, peak_r=2.0, side="buy"),
            _leg("B", risk=7, upnl=7.0, coh=5.0, side="buy"),
        ]
        legs[0]["realized_booked"] = 20.0
        d = btp.basket_tp_verdict(EVAL, legs, {}, now_ts=1.0)
        assert d.fire is False
        assert d.reason == "individual_tp_paid"
        # Replay handover — no double stamp raise.
        ho1 = btp.handover_after_individual_tp(legs[0])
        assert ho1["replay"] is False
        ho2 = btp.handover_after_individual_tp(legs[0])
        assert ho2["replay"] is True
        assert legs[0]["individual_tp_credited_usd"] == pytest.approx(20.0)
        # unpaid still uses max(epoch, banked) — no double.
        due = btp.unpaid_tax(14.0, {"taxed_usd_in_epoch": 14.0},
                             individual_banked=20.0)
        assert due == 0.0

    def test_failed_close_not_paid(self):
        state: dict = {}
        d = btp.BasketDecision(
            fire=True, reason="border", harvest_usd=12.0,
            tax_usd=12.0, peak_book_roe=0.5, fire_side="buy")
        # Only $5 actually closed → credit 5, not 12.
        btp.mark_fired(state, 1.0, d, realized_harvest_usd=5.0,
                       realized_by_side={"buy": 5.0})
        assert state["taxed_usd_in_epoch"] == pytest.approx(5.0)
        assert state["sides"]["buy"]["taxed_usd_in_epoch"] == pytest.approx(5.0)

    def test_account_reset_new_tax_epoch(self):
        state = {
            "scale_source": "equity",
            "capital_scale": 1.0,
            "scale_equity": 5000.0,
            "scale_epoch": 1.0,
            "taxed_usd_in_epoch": 40.0,
            "peak_book_roe": 0.8,
            "sides": {
                "sell": {"taxed_usd_in_epoch": 40.0, "peak_book_roe": 0.8,
                         "peak_book_usd": 20.0, "taxed_peak_roe": 0.8,
                         "last_fire_ts": 99.0},
            },
        }
        # ≥50% equity jump → new epoch.
        reset = btp.apply_scale_epoch(
            state, scale=5.0, scale_source="equity", equity=25000.0)
        assert reset is True
        assert state["scale_epoch"] == pytest.approx(2.0)
        assert state["taxed_usd_in_epoch"] == 0.0
        assert state["sides"]["sell"]["taxed_usd_in_epoch"] == 0.0

    def test_tax_uses_book_pos_and_peak_protection(self):
        # Red book → ratio tax 0; peak-profit protection still sizes tax.
        legs = [
            type("L", (), {"peak_usd": 9.0, "upnl": -2.0, "roe": -0.3,
                           "green": False})(),
        ]
        # Use real LegNode via build
        real = [
            _leg("A", side="sell", risk=7, upnl=-2.0, peak_r=1.5),  # peak$=10.5
            _leg("B", side="sell", risk=7, upnl=-1.0),
        ]
        snap = bg.build_graph(real)
        tax, max_leg = btp.tax_bank_usd(
            snap.basket.book_upnl, snap.payer, snap.legs,
            harvest_ratio=0.60, tax_bank_enabled=True)
        assert snap.basket.book_upnl < 0
        assert tax == pytest.approx(max_leg)  # peak-profit protection
        assert max_leg == pytest.approx(10.5)


# ── 4 residual stop ──────────────────────────────────────────────────

class TestResidualStop:
    def test_residual_keeps_stop_after_ladder_drop(self, tmp_path: Path):
        cfg = PropConfig(
            enabled=True, state_root=tmp_path, profile=EVAL,
            basket_tp_enabled=True,
            basket_tp_residual_pyramid_enabled=True,
            basket_tp_residual_frac=0.25,
            basket_tp_side_split_enabled=True,
            initial_balance=5000.0,
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
        client.get_open_orders = MagicMock(return_value=[
            {"orderId": "tp1", "type": "limit"},
            {"orderId": "sl1", "type": "stop_market"},
        ])
        client.cancel_order = MagicMock(return_value=None)
        client.place_orders = MagicMock(return_value=[{"orderId": "sl-new"}])
        ex = ProprExecutor(cfg, client, budget, dd)
        ex.resize_protection = MagicMock(return_value=True)
        # Two green sells → short side fires residual.
        legs = [
            _leg("ENA", side="sell", risk=7, upnl=14.0, coh=5.5, qty="10"),
            _leg("HBAR", side="sell", risk=7, upnl=10.0, coh=5.0, qty="8"),
        ]
        ok = ex.harvest_basket_tp(legs)
        assert ok is True
        assert ex.resize_protection.called
        residual = [L for L in legs if L.get("basket_residual")]
        assert residual
        for L in residual:
            assert L.get("owns_tp") is False
            assert L.get("ladder_reset") is True
            assert L.get("residual_phase") == btp.RESIDUAL_RECONCILED
            assert L.get("stop_price")  # birth stop never wiped
        # TP ladder cancelled with keep_stops — cancel_order on limit.
        assert client.cancel_order.called


# ── 7 stale book ─────────────────────────────────────────────────────

class TestStaleBookFailClosed:
    def test_missing_upnl_fail_closed(self):
        legs = [
            _leg("A", side="sell", risk=7, upnl=None, coh=5.0),
            _leg("B", side="sell", risk=7, upnl=None, coh=5.0),
        ]
        for L in legs:
            L.pop("last_upnl", None)
        d = btp.basket_tp_verdict(EVAL, legs, {}, now_ts=1.0)
        assert d.fire is False
        assert d.reason in ("no_green", "stale_book")


# ── 9 side-split ─────────────────────────────────────────────────────

class TestSideSplitTax:
    def test_green_shorts_harvest_while_longs_red(self):
        legs = [
            # Red longs — must NOT block short harvest.
            _leg("BTC", side="buy", risk=7, upnl=-3.0, coh=5.0),
            _leg("ETH", side="buy", risk=7, upnl=-2.0, coh=5.0),
            # Green shorts — trending side.
            _leg("ENA", side="sell", risk=7, upnl=14.0, coh=5.5, peak_r=2.0),
            _leg("HBAR", side="sell", risk=7, upnl=10.0, coh=5.0),
        ]
        state: dict = {}
        d = btp.basket_tp_verdict(
            EVAL, legs, state, now_ts=1.0, side_split_enabled=True)
        assert d.fire is True, d.reason
        assert d.fire_side == "sell"
        assert all(t.book_side == "sell" for t in d.targets)
        assert "buy" in d.telemetry.get("sides", {})
        assert d.telemetry["sides"]["buy"]["fire"] is False
        assert d.telemetry["sides"]["sell"]["fire"] is True

    def test_side_epochs_independent(self):
        legs = [
            _leg("ENA", side="sell", risk=7, upnl=14.0, coh=5.5),
            _leg("HBAR", side="sell", risk=7, upnl=10.0, coh=5.0),
        ]
        state: dict = {}
        d1 = btp.basket_tp_verdict(EVAL, legs, state, now_ts=1.0)
        assert d1.fire
        btp.mark_fired(state, 1.0, d1, realized_harvest_usd=d1.harvest_usd,
                       realized_by_side={"sell": d1.harvest_usd})
        # Same peak → short side peak_epoch_taxed; long still idle.
        state["sides"]["sell"]["last_fire_ts"] = 1.0
        d2 = btp.basket_tp_verdict(EVAL, legs, state, now_ts=1.0 + 200.0)
        assert d2.fire is False
        assert d2.reason in ("peak_epoch_taxed", "individual_tp_paid",
                             "below_border")
