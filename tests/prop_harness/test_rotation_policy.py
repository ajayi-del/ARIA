"""Rotation-policy gates — family cap (Rule B) + cost-vs-risk gate (§4).

The family cap keeps one representative per (theme, direction); the cost
gate kills tight-stop dust scalps whose round-trip fees would eat more
than 10% of planned risk ($0.50 on the $5 unit).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from prop_harness.brains.budget import BudgetBrain
from prop_harness.brains.drawdown import DrawdownBrain
from prop_harness.config import PropConfig
from prop_harness.executor import ProprExecutor
from prop_harness.families import family_of


def _cfg(tmp_path: Path, **kw) -> PropConfig:
    return PropConfig(enabled=True, state_root=tmp_path, **kw)


class _Client:
    account_id = "urn:prp-account:x"

    def __init__(self):
        self.placed: list[dict] = []

    def place_orders(self, body):
        self.placed.append(body)
        return [{"orderId": f"urn:prp-order:o{len(self.placed)}",
                 **body["orders"][0]}]


def _executor(tmp_path, client, **cfg_kw):
    cfg = _cfg(tmp_path, **cfg_kw)
    (tmp_path / "journal").mkdir(exist_ok=True)
    (tmp_path / "accounts" / "acct1").mkdir(parents=True, exist_ok=True)
    budget = BudgetBrain(tmp_path / "budget.json")
    budget.roll(headroom=150.0)
    dd = DrawdownBrain(tmp_path / "dd.json", floor_line=4850.0,
                       floor_usd=150.0)
    dd.update(5000.0)
    return ProprExecutor(cfg, client, budget, dd)


class TestCostGate:
    def test_tight_stop_rejected(self, tmp_path):
        # stop 0.2% away -> notional = 5 / 0.002 = $2500 -> rt taker
        # cost $2.25 = 45% of the $5 risk unit -> reject
        client = _Client()
        ex = _executor(tmp_path, client)
        res = ex.place_bracket("BTC", "buy", risk_usd=5.0,
                               entry_ref=100000.0, stop_price=99800.0)
        assert not res.ok
        assert "round-trip cost" in res.reason
        assert client.placed == []

    def test_normal_stop_passes(self, tmp_path):
        # stop 2% away -> notional $250 -> rt cost $0.225 = 4.5% -> pass
        client = _Client()
        ex = _executor(tmp_path, client)
        res = ex.place_bracket("BTC", "buy", risk_usd=5.0,
                               entry_ref=100000.0, stop_price=98000.0)
        assert res.ok, res.reason
        assert client.placed


class TestFamilyCap:
    def _hold(self, ex, asset, side, entry, stop):
        res = ex.place_bracket(asset, side, risk_usd=5.0,
                               entry_ref=entry, stop_price=stop)
        assert res.ok, res.reason

    def test_same_family_same_direction_rejected(self, tmp_path):
        client = _Client()
        ex = _executor(tmp_path, client)
        self._hold(ex, "BCH", "sell", 300.0, 306.0)      # large_alt short
        res = ex.place_bracket("XRP", "sell", risk_usd=5.0,
                               entry_ref=2.0, stop_price=2.04)
        assert not res.ok
        assert "family cap: large_alt sell held by BCH" in res.reason

    def test_opposite_direction_allowed(self, tmp_path):
        # a hedge is legal: same family, opposite side
        client = _Client()
        ex = _executor(tmp_path, client)
        self._hold(ex, "BCH", "sell", 300.0, 306.0)
        res = ex.place_bracket("XRP", "buy", risk_usd=5.0,
                               entry_ref=2.0, stop_price=1.96)
        assert res.ok, res.reason

    def test_different_family_allowed(self, tmp_path):
        client = _Client()
        ex = _executor(tmp_path, client)
        self._hold(ex, "BCH", "sell", 300.0, 306.0)      # large_alt
        res = ex.place_bracket("ZEC", "sell", risk_usd=5.0,
                               entry_ref=500.0, stop_price=510.0)  # privacy
        assert res.ok, res.reason

    def test_xyz_equity_clusters(self, tmp_path):
        client = _Client()
        ex = _executor(tmp_path, client)
        self._hold(ex, "xyz:NVDA", "buy", 200.0, 196.0)  # equity_semis
        res = ex.place_bracket("xyz:SMCI", "buy", risk_usd=5.0,
                               entry_ref=40.0, stop_price=39.2)
        assert not res.ok
        assert "family cap: equity_semis buy held by xyz:NVDA" in res.reason
        # a megacap long is a different theme
        res2 = ex.place_bracket("xyz:MSFT", "buy", risk_usd=5.0,
                                entry_ref=500.0, stop_price=490.0)
        assert res2.ok, res2.reason

    def test_kill_switch_off_is_legacy(self, tmp_path):
        client = _Client()
        ex = _executor(tmp_path, client, family_cap_enabled=False)
        self._hold(ex, "BCH", "sell", 300.0, 306.0)
        res = ex.place_bracket("XRP", "sell", risk_usd=5.0,
                               entry_ref=2.0, stop_price=2.04)
        assert res.ok, res.reason


class TestFamilyMap:
    def test_known_assets(self):
        assert family_of("BTC") == "majors"
        assert family_of("XRP") == "large_alt"
        assert family_of("ZEC") == "privacy"
        assert family_of("xyz:GOLD") == "metal"
        assert family_of("xyz:CRCL") == "equity_fintech"

    def test_unknown_asset_is_solo_never_blocked(self):
        assert family_of("xyz:PLTR") == "solo:xyz:PLTR"
        assert family_of("MYSTERY") == "solo:MYSTERY"
        # solo families are unique per asset -> two unknowns never collide
        assert family_of("AAA") != family_of("BBB")
