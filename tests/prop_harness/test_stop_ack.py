"""Pins for prop stop-ack FSM (ATS-22 / Graph 6).

Armed → acked → missed; fail-closed on missing/unknown after ACKED;
fleet HALT while any open MISSED; kill switch legacy; heal then reduce.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from prop_harness.brains import stop_ack as sa
from prop_harness.brains.budget import BudgetBrain
from prop_harness.brains.drawdown import DrawdownBrain
from prop_harness.config import PropConfig
from prop_harness.executor import ProprExecutor


# ── pure brain ───────────────────────────────────────────────────────────────


class TestTransition:
    def test_armed_to_acked_on_present(self):
        st, reason = sa.transition(
            sa.ARMED, now=100.0, armed_ts=90.0, observation=sa.PRESENT)
        assert st == sa.ACKED and reason == "acked"

    def test_armed_awaits_inside_timeout(self):
        st, reason = sa.transition(
            sa.ARMED, now=100.0, armed_ts=90.0, observation=sa.ABSENT,
            timeout_s=30.0)
        assert st == sa.ARMED and reason == "awaiting"

    def test_armed_timeout_to_missed(self):
        st, reason = sa.transition(
            sa.ARMED, now=130.0, armed_ts=90.0, observation=sa.ABSENT,
            timeout_s=30.0)
        assert st == sa.MISSED and reason == "ack_timeout"

    def test_armed_timeout_unknown_to_missed(self):
        st, reason = sa.transition(
            sa.ARMED, now=130.0, armed_ts=90.0, observation=sa.UNKNOWN,
            timeout_s=30.0)
        assert st == sa.MISSED and reason == "ack_timeout_unknown"

    def test_acked_stays_on_present(self):
        st, reason = sa.transition(
            sa.ACKED, now=200.0, armed_ts=90.0, observation=sa.PRESENT)
        assert st == sa.ACKED and reason == "confirmed"

    def test_acked_absent_fail_closed(self):
        st, reason = sa.transition(
            sa.ACKED, now=200.0, armed_ts=90.0, observation=sa.ABSENT)
        assert st == sa.MISSED and reason == "ack_lost"

    def test_acked_unknown_fail_closed(self):
        st, reason = sa.transition(
            sa.ACKED, now=200.0, armed_ts=90.0, observation=sa.UNKNOWN)
        assert st == sa.MISSED and reason == "ack_unknown"

    def test_missed_latches(self):
        st, reason = sa.transition(
            sa.MISSED, now=300.0, armed_ts=90.0, observation=sa.PRESENT)
        assert st == sa.MISSED and reason == "latched"

    def test_none_adopts_armed_then_acks(self):
        st, reason = sa.transition(
            None, now=50.0, armed_ts=None, observation=sa.PRESENT)
        assert st == sa.ACKED and reason == "acked"


class TestProtectionPresent:
    def test_stop_market_reduce_only(self):
        orders = [{"type": "stop_market", "side": "sell",
                   "reduceOnly": True, "status": "pending"}]
        assert sa.protection_present(orders, "buy") == sa.PRESENT

    def test_wrong_side_absent(self):
        orders = [{"type": "stop_market", "side": "buy",
                   "reduceOnly": True, "status": "open"}]
        assert sa.protection_present(orders, "buy") == sa.ABSENT

    def test_none_is_unknown(self):
        assert sa.protection_present(None, "buy") == sa.UNKNOWN

    def test_empty_absent(self):
        assert sa.protection_present([], "sell") == sa.ABSENT


class TestFleetHalt:
    def test_any_open_missed_halts(self):
        assert sa.fleet_halt([
            {"status": "open", "stop_ack_state": sa.ACKED},
            {"status": "open", "stop_ack_state": sa.MISSED},
        ]) is True

    def test_closed_missed_ignored(self):
        assert sa.fleet_halt([
            {"status": "closed_external", "stop_ack_state": sa.MISSED},
            {"status": "open", "stop_ack_state": sa.ACKED},
        ]) is False

    def test_empty_no_halt(self):
        assert sa.fleet_halt([]) is False
        assert sa.fleet_halt(None) is False


class TestReduceGate:
    def test_reduce_only_when_missed_and_enabled(self):
        assert sa.should_reduce(reduce_enabled=True, state=sa.MISSED)
        assert not sa.should_reduce(reduce_enabled=False, state=sa.MISSED)
        assert not sa.should_reduce(reduce_enabled=True, state=sa.ARMED)


class TestStampArmed:
    def test_stamp_sets_armed(self):
        b: dict = {}
        sa.stamp_armed(b, 42.0)
        assert b["stop_ack_state"] == sa.ARMED
        assert b["stop_ack_ts"] == 42.0
        assert b["stop_ack_reason"] == "placed"


# ── executor splice ──────────────────────────────────────────────────────────


def _cfg(tmp_path: Path, **kw) -> PropConfig:
    return PropConfig(enabled=True, state_root=tmp_path, **kw)


class _FakeClient:
    account_id = "urn:prp-account:x"

    def __init__(self, open_orders=None, place_ok=True, orders_raise=False):
        self.open_orders = list(open_orders or [])
        self.place_ok = place_ok
        self.orders_raise = orders_raise
        self.placed: list[dict] = []
        self.cancelled: list[str] = []
        self.closed: list[str] = []
        self.cancel_all_n = 0

    def get_open_orders(self, base=None):
        if self.orders_raise:
            raise RuntimeError("orders dark")
        return list(self.open_orders)

    def place_orders(self, body):
        self.placed.append(body)
        if not self.place_ok:
            raise RuntimeError("venue 5xx")
        return [{"orderId": f"urn:prp-order:n{len(self.placed)}",
                 **body["orders"][0]}]

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return {}

    def cancel_all_open(self, base=None):
        self.cancel_all_n += 1
        n = len(self.open_orders)
        self.open_orders = []
        return n

    def close_position(self, asset):
        self.closed.append(asset)
        return [{"orderId": "urn:prp-order:close"}]


def _bracket(**kw):
    b = {
        "semantic_id": "prop-acct1-ETH-1", "asset": "ETH", "side": "buy",
        "quantity": "0.1", "risk_usd": 5.0, "entry_ref": "3000",
        "stop_price": "2950", "current_stop": "2950", "status": "open",
        "absence_count": 0, "stop_ack_state": sa.ARMED, "stop_ack_ts": 100.0,
    }
    b.update(kw)
    return b


def _executor(tmp_path, client, **cfg_kw):
    cfg = _cfg(tmp_path, **cfg_kw)
    (tmp_path / "journal").mkdir(exist_ok=True)
    (tmp_path / "accounts" / "acct1").mkdir(parents=True, exist_ok=True)
    budget = BudgetBrain(tmp_path / "budget.json")
    budget.roll(headroom=150.0)
    dd = DrawdownBrain(tmp_path / "dd.json", floor_line=4850.0,
                       floor_usd=150.0)
    return ProprExecutor(cfg, client, budget, dd), budget


def _stop_order(side="sell", oid="urn:prp-order:sl"):
    return {"orderId": oid, "type": "stop_market", "side": side,
            "status": "pending", "quantity": "0.1",
            "triggerPrice": "2950", "reduceOnly": True}


class TestReconcileStopAck:
    def test_armed_to_acked(self, tmp_path):
        client = _FakeClient(open_orders=[_stop_order()])
        ex, _ = _executor(tmp_path, client)
        b = _bracket()
        changed = ex.reconcile_stop_ack(
            b, {"quantity": "0.1", "positionId": "pid"})
        assert changed
        assert b["stop_ack_state"] == sa.ACKED

    def test_acked_lost_fail_closed_heals(self, tmp_path):
        client = _FakeClient(open_orders=[])  # absent
        ex, _ = _executor(tmp_path, client)
        b = _bracket(stop_ack_state=sa.ACKED, stop_ack_ts=50.0)
        changed = ex.reconcile_stop_ack(
            b, {"quantity": "0.1", "positionId": "pid"})
        assert changed
        # heal place succeeds → re-ARMED
        assert b["stop_ack_state"] == sa.ARMED
        assert client.placed  # resize_protection fired

    def test_timeout_missed_reduce(self, tmp_path):
        client = _FakeClient(open_orders=[], place_ok=False)
        ex, _ = _executor(tmp_path, client, stop_ack_timeout_s=30.0)
        b = _bracket(stop_ack_ts=0.0)  # ancient arm
        # force now via monkeypatch of time in reconcile — set ts far past
        import time as _t
        b["stop_ack_ts"] = _t.time() - 120.0
        changed = ex.reconcile_stop_ack(
            b, {"quantity": "0.1", "positionId": "pid"})
        assert changed
        assert b["status"] == "closed_stop_ack"
        assert "ETH" in client.closed
        assert client.cancel_all_n == 1

    def test_reduce_kill_halts_without_close(self, tmp_path):
        client = _FakeClient(open_orders=[], place_ok=False)
        ex, _ = _executor(tmp_path, client, stop_ack_timeout_s=30.0,
                          stop_ack_reduce_enabled=False)
        import time as _t
        b = _bracket(stop_ack_ts=_t.time() - 120.0)
        changed = ex.reconcile_stop_ack(
            b, {"quantity": "0.1", "positionId": "pid"})
        assert changed
        assert b["stop_ack_state"] == sa.MISSED
        assert b["status"] == "open"
        assert client.closed == []

    def test_fsm_kill_noop(self, tmp_path):
        client = _FakeClient(open_orders=[])
        ex, _ = _executor(tmp_path, client, stop_ack_fsm_enabled=False)
        b = _bracket(stop_ack_state=sa.ACKED)
        assert ex.reconcile_stop_ack(b, {"quantity": "0.1"}) is False
        assert b["stop_ack_state"] == sa.ACKED

    def test_acked_unknown_fail_closed(self, tmp_path):
        client = _FakeClient(orders_raise=True, place_ok=False)
        ex, _ = _executor(tmp_path, client, stop_ack_reduce_enabled=False)
        b = _bracket(stop_ack_state=sa.ACKED, stop_ack_ts=1.0)
        changed = ex.reconcile_stop_ack(
            b, {"quantity": "0.1", "positionId": "pid"})
        assert changed
        assert b["stop_ack_state"] == sa.MISSED


class TestHaltGate:
    def test_place_rejected_when_missed(self, tmp_path):
        client = _FakeClient()
        ex, _ = _executor(tmp_path, client)
        ex._save_registry({"brackets": [_bracket(stop_ack_state=sa.MISSED)]})
        # Halt binds before drawdown — no equity register required.
        res = ex.place_bracket("BTC", "buy", 5.0, 100000.0, 99000.0)
        assert res.ok is False
        assert "stop_ack_halt" in res.reason
        assert ex.stop_ack_halted() is True

    def test_halted_false_when_acked(self, tmp_path):
        client = _FakeClient()
        ex, _ = _executor(tmp_path, client)
        ex._save_registry({"brackets": [_bracket(stop_ack_state=sa.ACKED)]})
        assert ex.stop_ack_halted() is False

    def test_halt_kill_switch(self, tmp_path):
        client = _FakeClient()
        ex, _ = _executor(tmp_path, client, stop_ack_fsm_enabled=False)
        ex._save_registry({"brackets": [_bracket(stop_ack_state=sa.MISSED)]})
        assert ex.stop_ack_halted() is False


class TestConfigEnv:
    def test_from_env_knobs(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PROP_STATE_ROOT", str(tmp_path))
        monkeypatch.setenv("PROP_STOP_ACK_FSM", "0")
        monkeypatch.setenv("PROP_STOP_ACK_REDUCE", "0")
        monkeypatch.setenv("PROP_STOP_ACK_TIMEOUT_S", "12")
        cfg = PropConfig.from_env()
        assert cfg.stop_ack_fsm_enabled is False
        assert cfg.stop_ack_reduce_enabled is False
        assert cfg.stop_ack_timeout_s == 12.0
