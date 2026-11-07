"""Protection-resize pins — partial TP fills must re-size the resting stop.

Live driver (2026-11-07, BCH demo short): TP1+TP2 filled 1.05 of 1.3225,
venue position 0.27, but the stop_market order stayed quantity=1.32.
An oversized reduceOnly stop is untested venue behavior on trigger —
clamp vs reject vs flip. The reconciler now re-places the stop at venue
size (place-first, cancel-second) and syncs the registry, which is also
the snapshot's quantity source.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from prop_harness.brains.budget import BudgetBrain
from prop_harness.brains.drawdown import DrawdownBrain
from prop_harness.config import PropConfig
from prop_harness.executor import ProprExecutor


def _cfg(tmp_path: Path, **kw) -> PropConfig:
    return PropConfig(enabled=True, state_root=tmp_path, **kw)


class _FakeClient:
    account_id = "urn:prp-account:x"

    def __init__(self, open_orders=(), place_ok=True):
        self.open_orders = list(open_orders)
        self.place_ok = place_ok
        self.placed: list[dict] = []
        self.cancelled: list[str] = []

    def get_open_orders(self, base=None):
        return list(self.open_orders)

    def place_orders(self, body):
        self.placed.append(body)
        if not self.place_ok:
            raise RuntimeError("venue 5xx")
        return [{"orderId": "urn:prp-order:newstop", **body["orders"][0]}]

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return {}


def _stop_order(quantity="1.32", trigger="264.6608", oid="urn:prp-order:oldstop"):
    return {"orderId": oid, "type": "stop_market", "side": "buy",
            "status": "pending", "quantity": quantity,
            "triggerPrice": trigger, "reduceOnly": True}


def _bracket(quantity="1.3225", risk=5.0):
    return {"semantic_id": "prop-acct1-BCH-1", "asset": "BCH", "side": "sell",
            "quantity": quantity, "risk_usd": risk, "entry_ref": "260.88",
            "stop_price": "264.6608", "status": "open", "absence_count": 0}


def _executor(tmp_path, client):
    cfg = _cfg(tmp_path)
    (tmp_path / "journal").mkdir(exist_ok=True)
    budget = BudgetBrain(tmp_path / "budget.json")
    budget.roll(headroom=150.0)
    dd = DrawdownBrain(tmp_path / "dd.json", floor_line=4850.0,
                       floor_usd=150.0)
    return ProprExecutor(cfg, client, budget, dd), budget


class TestResizeProtection:
    def test_stop_replaced_place_before_cancel(self, tmp_path):
        client = _FakeClient(open_orders=[_stop_order()])
        ex, _ = _executor(tmp_path, client)
        ok = ex.resize_protection(_bracket(), Decimal("0.27"), "pid-1")
        assert ok
        order = client.placed[0]["orders"][0]
        assert order["type"] == "stop_market"
        assert order["quantity"] == "0.27"
        assert order["triggerPrice"] == "264.6608"
        assert order["reduceOnly"] is True
        assert order["side"] == "buy"           # short's exit side
        assert order["positionId"] == "pid-1"
        assert client.cancelled == ["urn:prp-order:oldstop"]

    def test_new_stop_not_cancelled(self, tmp_path):
        # the scan must skip the just-placed replacement
        client = _FakeClient(open_orders=[_stop_order(),
                                          _stop_order(quantity="0.27",
                                                      oid="urn:prp-order:newstop")])
        ex, _ = _executor(tmp_path, client)
        assert ex.resize_protection(_bracket(), Decimal("0.27"), "")
        assert client.cancelled == ["urn:prp-order:oldstop"]

    def test_place_failure_never_cancels(self, tmp_path):
        client = _FakeClient(open_orders=[_stop_order()], place_ok=False)
        ex, _ = _executor(tmp_path, client)
        assert ex.resize_protection(_bracket(), Decimal("0.27"), "") is False
        assert client.cancelled == []           # old stop untouched

    def test_tp_legs_untouched(self, tmp_path):
        tp = {"orderId": "urn:prp-order:tp3", "type": "take_profit_limit",
              "status": "pending", "quantity": "0.27"}
        client = _FakeClient(open_orders=[_stop_order(), tp])
        ex, _ = _executor(tmp_path, client)
        assert ex.resize_protection(_bracket(), Decimal("0.27"), "")
        assert client.cancelled == ["urn:prp-order:oldstop"]


class TestSyncBracketSize:
    def _position(self, qty="0.27", realized="11.483659"):
        return {"asset": "BCH", "quantity": qty, "positionId": "pid-1",
                "realizedPnl": realized}

    def test_shrink_syncs_registry_and_budget(self, tmp_path):
        client = _FakeClient(open_orders=[_stop_order()])
        ex, budget = _executor(tmp_path, client)
        budget.commit(5.0)
        bracket = _bracket()
        assert ex.sync_bracket_size(bracket, self._position()) is True
        assert bracket["quantity"] == "0.27"
        # released 5 * (1 - 0.27/1.3225) ~= 3.979, realized booked once
        assert budget.state.committed == pytest.approx(5.0 - 3.9792, abs=1e-3)
        assert budget.state.realized == pytest.approx(11.483659)
        assert bracket["risk_released"] == pytest.approx(3.9792, abs=1e-3)

    def test_second_tick_is_idempotent(self, tmp_path):
        client = _FakeClient(open_orders=[_stop_order()])
        ex, budget = _executor(tmp_path, client)
        budget.commit(5.0)
        bracket = _bracket()
        ex.sync_bracket_size(bracket, self._position())
        assert ex.sync_bracket_size(bracket, self._position()) is False
        assert budget.state.realized == pytest.approx(11.483659)

    def test_no_shrink_noop(self, tmp_path):
        client = _FakeClient()
        ex, budget = _executor(tmp_path, client)
        budget.commit(5.0)
        bracket = _bracket()
        assert ex.sync_bracket_size(bracket, self._position(qty="1.32")) is False
        assert bracket["quantity"] == "1.3225"
        assert budget.state.committed == 5.0
        assert client.placed == []

    def test_resize_failure_keeps_registry_stale_for_retry(self, tmp_path):
        client = _FakeClient(open_orders=[_stop_order()], place_ok=False)
        ex, _ = _executor(tmp_path, client)
        bracket = _bracket()
        assert ex.sync_bracket_size(bracket, self._position()) is False
        assert bracket["quantity"] == "1.3225"   # retry next reconcile

    def test_close_path_releases_only_remaining_risk(self, tmp_path):
        # after a partial release of 3.979, a full close frees only ~1.02
        client = _FakeClient(open_orders=[_stop_order()])
        ex, budget = _executor(tmp_path, client)
        budget.commit(5.0)
        bracket = _bracket()
        ex.sync_bracket_size(bracket, self._position())
        remaining = bracket["risk_usd"] - float(bracket.get("risk_released", 0.0))
        budget.release(max(0.0, remaining), 0.0)
        assert budget.state.committed == pytest.approx(0.0, abs=1e-6)
