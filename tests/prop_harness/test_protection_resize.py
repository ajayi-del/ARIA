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
    kw.setdefault("crypto_min_coh_fire_enabled", False)
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


class TestStopResizeProvenance:
    """NEAR +2s tighten class — observability only, no behavior change."""

    def test_early_tighten_journals_provenance(self, tmp_path):
        import time
        client = _FakeClient(open_orders=[_stop_order(trigger="3.717")])
        ex, _ = _executor(tmp_path, client)
        bracket = {
            "semantic_id": "prop-acct1-NEAR-1", "asset": "NEAR",
            "side": "sell", "quantity": "807.5", "risk_usd": 64.94,
            "entry_ref": "3.6367", "stop_price": "3.717",
            "current_stop": "3.717", "status": "open",
            "opened_ts": time.time() - 2.0,
        }
        ok = ex.resize_protection(
            bracket, Decimal("807.5"), "pid-near",
            trigger_override="3.6738")
        assert ok
        journal = (tmp_path / "journal" / "events.jsonl").read_text()
        assert "stop_resize_provenance" in journal
        assert "reserved_before" in journal
        assert "stop_resize_delta_r" in journal

    def test_telemetry_kill_switch(self, tmp_path):
        import time
        client = _FakeClient(open_orders=[_stop_order()])
        cfg = _cfg(tmp_path, stop_resize_telemetry_enabled=False)
        (tmp_path / "journal").mkdir(exist_ok=True)
        budget = BudgetBrain(tmp_path / "budget.json")
        budget.roll(headroom=150.0)
        dd = DrawdownBrain(tmp_path / "dd.json", floor_line=4850.0,
                           floor_usd=150.0)
        ex = ProprExecutor(cfg, client, budget, dd)
        bracket = _bracket()
        bracket["opened_ts"] = time.time() - 1.0
        assert ex.resize_protection(
            bracket, Decimal("0.27"), "", trigger_override="264.0")
        journal = (tmp_path / "journal" / "events.jsonl").read_text()
        assert "stop_resize_provenance" not in journal


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
        # Qty flat within 2% — no resize; realized_booked still stamps so
        # bank_protect can arm (legacy returned False and stayed blind).
        assert ex.sync_bracket_size(bracket, self._position(qty="1.32")) is True
        assert bracket["quantity"] == "1.3225"
        assert bracket["realized_booked"] == pytest.approx(11.483659)
        assert budget.state.committed == 5.0
        assert client.placed == []
        # Second tick with same booked is a true noop.
        assert ex.sync_bracket_size(bracket, self._position(qty="1.32")) is False

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

    def test_trades_realized_feeds_bank_protect_when_venue_zero(self, tmp_path):
        # turbo BTC wound: position.realizedPnl ≈ −0.04 while TP legs
        # already banked ≥$20 on the trades tape.
        client = _FakeClient(open_orders=[_stop_order(quantity="0.27")])
        ex, budget = _executor(tmp_path, client)
        budget.commit(5.0)
        bracket = _bracket()
        bracket["profile_hint"] = "evaluation"
        # Shrink with venue≈0 but trades=$22 → booked stamps from trades.
        pos = self._position(qty="0.27", realized="-0.036")
        assert ex.sync_bracket_size(bracket, pos, trades_realized=22.5) is True
        assert bracket["realized_booked"] == pytest.approx(22.5)
        assert bracket["risk_released"] == pytest.approx(3.9792, abs=1e-3)

    def test_flat_qty_still_syncs_booked_from_trades(self, tmp_path):
        client = _FakeClient(open_orders=[_stop_order()])
        ex, _ = _executor(tmp_path, client)
        bracket = _bracket(quantity="1.32")
        bracket["realized_booked"] = -0.04
        pos = self._position(qty="1.32", realized="-0.04")
        assert ex.sync_bracket_size(
            bracket, pos, trades_realized=21.0) is True
        assert bracket["realized_booked"] == pytest.approx(21.0)

    def test_best_realized_prefers_trades_over_near_zero_venue(self):
        assert ProprExecutor._best_realized(-0.036, 22.5) == 22.5
        assert ProprExecutor._best_realized(11.5, 11.4) == 11.5
        assert ProprExecutor._best_realized(0.0, None) == 0.0
