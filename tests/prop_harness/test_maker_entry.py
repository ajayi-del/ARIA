"""Pins: maker-first two-phase entries (Governor 2026-11-15 fee attack).

Taker 0.045% vs resting maker 0.015% (tier proven on the Governor's
tape). Default OFF = single-batch legacy bit-for-bit. The late-fill
orphan class (09-28) is killed by construction: protection legs are
never sent until the entry is VERIFIED filled.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from prop_harness.brains.budget import BudgetBrain
from prop_harness.brains.drawdown import DrawdownBrain
from prop_harness.config import PropConfig
from prop_harness.executor import ProprExecutor
from prop_harness.io.propr_client import ProprAPIError


class _MakerClient:
    """Scripted venue: limit entries rest open until fill_after finds."""

    account_id = "acct1"

    def __init__(self, fill_after=1, phase2_error=None, pos_qty=None):
        self.calls = []
        self.cancelled = []
        self.closed = []
        self.fill_after = fill_after  # None = never fills
        self.phase2_error = phase2_error
        self.pos_qty = pos_qty        # None = full planned qty
        self._status: dict[str, str] = {}
        self._finds: dict[str, int] = {}

    def get_open_orders(self, base=None):
        return []

    def place_orders(self, body):
        self.calls.append(body)
        orders = body["orders"]
        if len(orders) == 1 and not orders[0]["reduceOnly"]:
            intent = orders[0]["intentId"]
            self._status[intent] = ("open" if orders[0]["type"] == "limit"
                                    else "filled")
            return [{"orderId": "e1", "intentId": intent}]
        if all(o["reduceOnly"] for o in orders):
            if self.phase2_error is not None:
                raise self.phase2_error
            return [{"orderId": "p%d" % i} for i, _ in enumerate(orders)]
        # taker fallback: full bracket batch
        for o in orders:
            self._status[o["intentId"]] = "filled"
        return [{"orderId": "fb%d" % i} for i, _ in enumerate(orders)]

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        for intent, st in self._status.items():
            if st == "open":
                self._status[intent] = "cancelled"
        return {}

    def find_by_intent(self, intent_id):
        st = self._status.get(intent_id)
        if st is None:
            return None
        n = self._finds[intent_id] = self._finds.get(intent_id, 0) + 1
        if st == "open" and self.fill_after is not None and n >= self.fill_after:
            st = self._status[intent_id] = "filled"
        return {"intentId": intent_id, "status": st, "orderId": "e1"}

    def get_positions(self, status="open", base=None):
        if any(st == "filled" for st in self._status.values()):
            qty = self.pos_qty
            if qty is None:
                for body in self.calls:
                    if len(body["orders"]) == 1:
                        qty = body["orders"][0]["quantity"]
            return [{"quantity": str(qty), "positionId": "pos1"}]
        return []

    def close_position(self, asset):
        self.closed.append(asset)
        return []


def _exec(tmp_path: Path, client, **over) -> ProprExecutor:
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
    return ProprExecutor(cfg, client, budget, dd)


def _events(tmp_path: Path) -> list[dict]:
    out = []
    with open(tmp_path / "journal" / "events.jsonl") as fh:
        for line in fh:
            out.append(json.loads(line))
    return out


class TestMakerEntry:
    def test_default_off_single_batch_legacy(self, tmp_path):
        client = _MakerClient()
        ex = _exec(tmp_path, client)  # maker_entry_enabled default False
        res = ex.place_bracket("BTC", "buy", 14.0, 100.0, 92.0,
                               coherence=5.0)
        assert res.ok is True
        assert len(client.calls) == 1
        assert len(client.calls[0]["orders"]) == 5
        assert client.calls[0]["orders"][0]["type"] == "market"

    def test_maker_fill_two_phase(self, tmp_path):
        client = _MakerClient(fill_after=1)
        ex = _exec(tmp_path, client, maker_entry_enabled=True)
        res = ex.place_bracket("BTC", "buy", 14.0, 100.0, 92.0,
                               coherence=5.0)
        assert res.ok is True
        assert len(client.calls) == 2
        p1, p2 = client.calls
        assert len(p1["orders"]) == 1
        assert p1["orders"][0]["type"] == "limit"
        assert p1["orders"][0]["timeInForce"] == "GTC"
        assert p1["orders"][0]["price"] == "100"
        assert len(p2["orders"]) == 4
        assert all(o["reduceOnly"] for o in p2["orders"])
        evs = _events(tmp_path)
        assert any(e["type"] == "maker_entry_filled" for e in evs)
        placed = [e for e in evs if e["type"] == "bracket_placed"][0]
        assert placed["entry_type"] == "limit"
        assert "entry_fill_s" in placed

    def test_protection_never_sent_before_fill(self, tmp_path):
        client = _MakerClient(fill_after=3)
        ex = _exec(tmp_path, client, maker_entry_enabled=True)
        res = ex.place_bracket("BTC", "buy", 14.0, 100.0, 92.0,
                               coherence=5.0)
        assert res.ok is True
        # phase-2 legs only exist in the SECOND call
        assert len(client.calls[0]["orders"]) == 1
        assert len(client.calls[1]["orders"]) == 4

    def test_unfilled_taker_fallback(self, tmp_path, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda s: None)
        client = _MakerClient(fill_after=None)
        ex = _exec(tmp_path, client, maker_entry_enabled=True,
                   maker_entry_timeout_s=0.05)
        res = ex.place_bracket("BTC", "buy", 14.0, 100.0, 92.0,
                               coherence=5.0)
        assert res.ok is True
        assert client.cancelled == ["e1"]
        fb = client.calls[-1]
        assert len(fb["orders"]) == 5
        assert fb["orders"][0]["type"] == "market"
        evs = _events(tmp_path)
        assert any(e["type"] == "maker_entry_unfilled_taker_fallback"
                   for e in evs)
        placed = [e for e in evs if e["type"] == "bracket_placed"][0]
        assert placed["entry_type"] == "market"

    def test_fallback_fresh_intents(self, tmp_path, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda s: None)
        client = _MakerClient(fill_after=None)
        ex = _exec(tmp_path, client, maker_entry_enabled=True,
                   maker_entry_timeout_s=0.05)
        ex.place_bracket("BTC", "buy", 14.0, 100.0, 92.0, coherence=5.0)
        limit_intent = client.calls[0]["orders"][0]["intentId"]
        fb_intent = client.calls[-1]["orders"][0]["intentId"]
        assert limit_intent != fb_intent  # burnt intent never reused

    def test_unfilled_standdown_releases_budget(self, tmp_path,
                                                monkeypatch):
        monkeypatch.setattr("time.sleep", lambda s: None)
        client = _MakerClient(fill_after=None)
        ex = _exec(tmp_path, client, maker_entry_enabled=True,
                   maker_entry_timeout_s=0.05,
                   maker_entry_taker_fallback=False)
        before = ex.budget.remaining
        res = ex.place_bracket("BTC", "buy", 14.0, 100.0, 92.0,
                               coherence=5.0)
        assert res.ok is False
        assert res.reason == "maker_entry_unfilled_standdown"
        assert client.closed == []
        assert ex.budget.remaining == before
        evs = _events(tmp_path)
        assert any(e["type"] == "maker_entry_unfilled_standdown"
                   for e in evs)

    def test_phase2_reject_flattens_naked_position(self, tmp_path):
        err = ProprAPIError(400, None, "conditional needs positionId")
        client = _MakerClient(fill_after=1, phase2_error=err)
        ex = _exec(tmp_path, client, maker_entry_enabled=True)
        before = ex.budget.remaining
        res = ex.place_bracket("BTC", "buy", 14.0, 100.0, 92.0,
                               coherence=5.0)
        assert res.ok is False
        assert res.reason.startswith("maker_entry_protection_failed")
        assert client.closed == ["BTC"]
        assert ex.budget.remaining == before
        evs = _events(tmp_path)
        assert any(e["type"] == "maker_entry_protection_failed"
                   for e in evs)

    def test_partial_fill_resizes_protection(self, tmp_path):
        # planned qty 0.014 BTC @100/92 -> risk 14; venue filled 0.01
        client = _MakerClient(fill_after=1, pos_qty="0.01")
        ex = _exec(tmp_path, client, maker_entry_enabled=True)
        res = ex.place_bracket("BTC", "buy", 14.0, 100.0, 92.0,
                               coherence=5.0)
        assert res.ok is True
        p2 = client.calls[1]["orders"]
        stop = [o for o in p2 if o["type"] == "stop_market"][0]
        tps = [o for o in p2 if o["type"] == "take_profit_limit"]
        assert float(stop["quantity"]) == pytest.approx(0.01)
        assert sum(float(o["quantity"])
                   for o in tps) == pytest.approx(0.01)

    def test_registry_row_carries_entry_meta(self, tmp_path):
        client = _MakerClient(fill_after=1)
        ex = _exec(tmp_path, client, maker_entry_enabled=True)
        res = ex.place_bracket("BTC", "buy", 14.0, 100.0, 92.0,
                               coherence=5.0)
        reg = json.loads((tmp_path / "accounts" / "acct1"
                          / "registry.json").read_text())
        row = [b for b in reg["brackets"]
               if b["semantic_id"] == res.semantic_id][0]
        assert row["entry_type"] == "limit"
        assert "entry_fill_s" in row

    def test_explicit_limit_entry_unaffected(self, tmp_path):
        client = _MakerClient()
        ex = _exec(tmp_path, client, maker_entry_enabled=True)
        res = ex.place_bracket("BTC", "buy", 14.0, 100.0, 92.0,
                               entry_type="limit", coherence=5.0)
        assert res.ok is True
        # explicit limit stays single-batch legacy (maker path is
        # market-entry-only by construction)
        assert len(client.calls) == 1
        assert len(client.calls[0]["orders"]) == 5


class TestConfigParse:
    def test_defaults_are_legacy(self, monkeypatch, tmp_path):
        for k in ("PROP_MAKER_ENTRY", "PROP_MAKER_ENTRY_TIMEOUT_S",
                  "PROP_MAKER_ENTRY_TAKER_FALLBACK"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("PROP_STATE_ROOT", str(tmp_path))
        cfg = PropConfig.from_env()
        assert cfg.maker_entry_enabled is False
        assert cfg.maker_entry_timeout_s == 8.0
        assert cfg.maker_entry_taker_fallback is True

    def test_env_binds(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PROP_STATE_ROOT", str(tmp_path))
        monkeypatch.setenv("PROP_MAKER_ENTRY", "1")
        monkeypatch.setenv("PROP_MAKER_ENTRY_TIMEOUT_S", "12")
        monkeypatch.setenv("PROP_MAKER_ENTRY_TAKER_FALLBACK", "0")
        cfg = PropConfig.from_env()
        assert cfg.maker_entry_enabled is True
        assert cfg.maker_entry_timeout_s == 12.0
        assert cfg.maker_entry_taker_fallback is False
