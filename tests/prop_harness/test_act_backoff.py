"""Giveback P0 — act backoff / latch / qty≤0 / kill-switch legacy."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from prop_harness.brains.act_backoff import (
    ActBackoff, basket_key, is_rate_limited, resize_key,
)
from prop_harness.brains.budget import BudgetBrain
from prop_harness.brains.drawdown import DrawdownBrain
from prop_harness.config import PropConfig
from prop_harness.executor import ProprExecutor
import json

from prop_harness.io.propr_client import ProprAPIError


def _events(tmp_path: Path) -> list[dict]:
    path = tmp_path / "journal" / "events.jsonl"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text().splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def _cfg(tmp_path: Path, **kw) -> PropConfig:
    return PropConfig(enabled=True, state_root=tmp_path, **kw)


class _FakeClient:
    account_id = "urn:prp-account:x"

    def __init__(self, place_exc=None):
        self.place_exc = place_exc
        self.placed: list[dict] = []
        self.cancelled: list[str] = []
        self.open_orders: list[dict] = []

    def get_open_orders(self, base=None):
        return list(self.open_orders)

    def place_orders(self, body):
        self.placed.append(body)
        if self.place_exc is not None:
            raise self.place_exc
        return [{"orderId": "urn:prp-order:newstop", **body["orders"][0]}]

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return {}


def _bracket(sid="prop-acct1-BCH-1", asset="BCH"):
    return {"semantic_id": sid, "asset": asset, "side": "sell",
            "quantity": "0.27", "risk_usd": 5.0, "entry_ref": "260.88",
            "stop_price": "264.6608", "status": "open", "absence_count": 0}


def _executor(tmp_path, client, **cfg_kw):
    cfg = _cfg(tmp_path, **cfg_kw)
    (tmp_path / "journal").mkdir(exist_ok=True)
    budget = BudgetBrain(tmp_path / "budget.json")
    budget.roll(headroom=150.0)
    dd = DrawdownBrain(tmp_path / "dd.json", floor_line=4850.0,
                       floor_usd=150.0)
    return ProprExecutor(cfg, client, budget, dd)


class TestActBackoffBrain:
    def test_exp_backoff_then_latch(self):
        b = ActBackoff(enabled=True, initial_s=10.0, max_s=80.0,
                       latch_after=3)
        now = 1_000.0
        p1 = b.note_rate_limit("resize:a", now=now)
        assert p1["type"] == "act_backoff_armed"
        assert p1["wait_s"] == 10.0
        assert b.blocked("resize:a", now=now + 1)
        assert not b.blocked("resize:a", now=now + 11)

        p2 = b.note_rate_limit("resize:a", now=now + 11)
        assert p2["wait_s"] == 20.0
        p3 = b.note_rate_limit("resize:a", now=now + 32)
        assert p3["type"] == "act_backoff_latched"
        assert p3["latched"] is True
        assert p3["wait_s"] == 80.0
        assert b.blocked("resize:a", now=now + 32 + 79)

    def test_success_clears(self):
        b = ActBackoff(initial_s=30.0, latch_after=2)
        b.note_rate_limit("k", now=100.0)
        b.note_rate_limit("k", now=140.0)
        assert "k" in b._latched
        b.note_success("k")
        assert not b.blocked("k", now=141.0)
        assert "k" not in b._latched

    def test_kill_off_noop(self):
        b = ActBackoff(enabled=False, initial_s=30.0)
        assert b.note_rate_limit("k", now=1.0) == {}
        assert not b.blocked("k", now=1.0)

    def test_is_rate_limited_shapes(self):
        assert is_rate_limited(ProprAPIError(429, None, "rate_limited"))
        assert is_rate_limited(RuntimeError("[429] too_many_requests"))
        assert not is_rate_limited(RuntimeError("qty invalid"))

    def test_key_helpers(self):
        assert resize_key("sid-1") == "resize:sid-1"
        assert basket_key("sid-1") == "basket:sid-1"
        assert basket_key(None) == "basket:__book__"


class TestResizeProtectionBackoff:
    def test_429_arms_backoff_skips_next(self, tmp_path):
        exc = ProprAPIError(429, None, "too_many_requests")
        client = _FakeClient(place_exc=exc)
        ex = _executor(tmp_path, client)
        assert ex.resize_protection(_bracket(), Decimal("0.27"), "pid") is False
        assert len(client.placed) == 1
        # second call while cooling — no place
        assert ex.resize_protection(_bracket(), Decimal("0.27"), "pid") is False
        assert len(client.placed) == 1
        types = [e["type"] for e in _events(tmp_path)]
        assert "act_backoff_armed" in types
        assert "protection_resize_failed" in types
        assert "act_backoff_deferred" in types

    def test_success_clears_so_next_place_runs(self, tmp_path):
        import time
        client = _FakeClient()
        client.open_orders = [{
            "orderId": "old", "type": "stop_market", "quantity": "0.27",
        }]
        ex = _executor(tmp_path, client)
        key = resize_key("prop-acct1-BCH-1")
        ex.act_backoff.note_rate_limit(key, now=time.time())
        assert ex.resize_protection(_bracket(), Decimal("0.27"), "pid") is False
        assert client.placed == []
        ex.act_backoff.note_success(key)
        assert ex.resize_protection(_bracket(), Decimal("0.27"), "pid") is True
        assert len(client.placed) == 1

    def test_kill_switch_legacy_retries_every_call(self, tmp_path):
        exc = ProprAPIError(429, None, "too_many_requests")
        client = _FakeClient(place_exc=exc)
        ex = _executor(tmp_path, client, act_backoff_enabled=False)
        assert ex.resize_protection(_bracket(), Decimal("0.27"), "") is False
        assert ex.resize_protection(_bracket(), Decimal("0.27"), "") is False
        assert len(client.placed) == 2
        assert not any(
            e["type"].startswith("act_backoff") for e in _events(tmp_path))

    def test_qty_le_zero_skips_place(self, tmp_path):
        client = _FakeClient()
        ex = _executor(tmp_path, client)
        assert ex.resize_protection(_bracket(), Decimal("0"), "pid") is False
        assert ex.resize_protection(_bracket(), Decimal("-1"), "pid") is False
        assert client.placed == []


class TestBasketCloseSeam:
    def test_basket_seams_share_backoff(self, tmp_path):
        client = _FakeClient()
        ex = _executor(tmp_path, client)
        assert ex.basket_close_allowed("leg-1") is True
        ex.note_basket_close_error(
            ProprAPIError(429, None, "rate_limited"), "leg-1")
        assert ex.basket_close_allowed("leg-1") is False
        assert ex.basket_close_allowed(None) is False  # book cooled
        ex.note_basket_close_success("leg-1")
        # book still latched from note_basket_close_error dual-arm
        assert ex.basket_close_allowed(None) is False
        ex.note_basket_close_success(None)
        assert ex.basket_close_allowed(None) is True
