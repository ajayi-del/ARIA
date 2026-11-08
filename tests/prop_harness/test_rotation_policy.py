"""Rotation-policy gates — family cap (Rule B) + cost-vs-risk gate (§4).

The family cap keeps one representative per (theme, direction); the cost
gate kills tight-stop dust scalps whose round-trip fees would eat more
than 10% of planned risk ($0.50 on the $5 unit).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from prop_harness.brains import eval_profile as ep
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
        self.cancelled_bases: list[str] = []
        self.closed_assets: list[str] = []
        self.close_fail: bool = False

    def place_orders(self, body):
        self.placed.append(body)
        return [{"orderId": f"urn:prp-order:o{len(self.placed)}",
                 **body["orders"][0]}]

    def cancel_all_open(self, base=None):
        self.cancelled_bases.append(base)
        return 0

    def close_position(self, asset):
        if self.close_fail:
            raise RuntimeError("close refused")
        self.closed_assets.append(asset)
        return []


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


class TestFamilyHarvestReplace:
    """Graph 8 / ATS-23: replace-worse within family cap. Kill default OFF."""

    def _seed_held(self, ex, asset, side, entry, stop, coh, risk=5.0):
        res = ex.place_bracket(asset, side, risk_usd=risk,
                               entry_ref=entry, stop_price=stop,
                               coherence=coh)
        assert res.ok, res.reason
        # place_bracket stamps coherence_at_entry from the arg
        held = ex.open_brackets()[0]
        assert held.get("coherence_at_entry") == coh
        return held

    def test_knob_off_still_hard_blocks(self, tmp_path):
        # Accept: block when knob off (legacy family-cap refuse).
        client = _Client()
        ex = _executor(tmp_path, client, profile="evaluation",
                       family_harvest_enabled=False)
        self._seed_held(ex, "BCH", "sell", 300.0, 306.0, coh=5.0)
        res = ex.place_bracket("XRP", "sell", risk_usd=5.0,
                               entry_ref=2.0, stop_price=2.04,
                               coherence=8.0)
        assert not res.ok
        assert "family cap: large_alt sell held by BCH" in res.reason
        assert client.closed_assets == []

    def test_replace_when_coh_delta_and_budget_ok(self, tmp_path):
        # Accept: replace when cohΔ + budget ok.
        client = _Client()
        ex = _executor(tmp_path, client, profile="evaluation",
                       family_harvest_enabled=True,
                       family_harvest_coh_delta=0.5)
        self._seed_held(ex, "BCH", "sell", 300.0, 306.0, coh=5.0)
        res = ex.place_bracket("XRP", "sell", risk_usd=5.0,
                               entry_ref=2.0, stop_price=2.04,
                               coherence=6.0)
        assert res.ok, res.reason
        assert client.closed_assets == ["BCH"]
        assert client.cancelled_bases == ["BCH"]
        opens = ex.open_brackets()
        assert len(opens) == 1
        assert opens[0]["asset"] == "XRP"
        assert opens[0]["coherence_at_entry"] == 6.0
        # weaker seat marked closed_harvest_replace
        reg = ex._registry()
        closed = [b for b in reg["brackets"]
                  if b.get("status") == "closed_harvest_replace"]
        assert len(closed) == 1 and closed[0]["asset"] == "BCH"

    def test_insufficient_coh_delta_still_blocks(self, tmp_path):
        client = _Client()
        ex = _executor(tmp_path, client, profile="evaluation",
                       family_harvest_enabled=True,
                       family_harvest_coh_delta=0.5)
        self._seed_held(ex, "BCH", "sell", 300.0, 306.0, coh=5.0)
        res = ex.place_bracket("XRP", "sell", risk_usd=5.0,
                               entry_ref=2.0, stop_price=2.04,
                               coherence=5.4)  # < 5.0 + 0.5
        assert not res.ok
        assert "family cap:" in res.reason
        assert client.closed_assets == []

    def test_budget_short_after_free_blocks(self, tmp_path):
        # Held probe $5 committed; remaining tiny; new wants $28 → refuse.
        client = _Client()
        ex = _executor(tmp_path, client, profile="evaluation",
                       family_harvest_enabled=True)
        self._seed_held(ex, "BCH", "sell", 300.0, 306.0, coh=5.0, risk=5.0)
        # Drain remaining so even +freed $5 cannot cover $28.
        assert ex.budget.state is not None
        ex.budget.state.committed = ex.budget.state.budget - 1.0
        res = ex.place_bracket("XRP", "sell", risk_usd=28.0,
                               entry_ref=2.0, stop_price=2.04,
                               coherence=8.0)
        assert not res.ok
        assert "family cap:" in res.reason
        assert client.closed_assets == []

    def test_standard_profile_never_replaces(self, tmp_path):
        client = _Client()
        ex = _executor(tmp_path, client, profile="standard",
                       family_harvest_enabled=True)
        self._seed_held(ex, "BCH", "sell", 300.0, 306.0, coh=5.0)
        res = ex.place_bracket("XRP", "sell", risk_usd=5.0,
                               entry_ref=2.0, stop_price=2.04,
                               coherence=8.0)
        assert not res.ok
        assert "family cap:" in res.reason
        assert client.closed_assets == []

    def test_verdict_pure_no_aria_mfe_roe(self):
        # Accept: no ARIA MFE/ROE import — sleeve-local fields only.
        held = {"asset": "BCH", "semantic_id": "s1", "risk_usd": 5.0,
                "risk_released": 0.0, "coherence_at_entry": 5.0}
        plan = ep.family_harvest_verdict(
            ep.PROFILE_EVALUATION, True, 6.0, held, 5.0, 10.0, 0.5)
        assert plan is not None
        assert plan["reason"] == ep.FAMILY_HARVEST_REPLACE
        assert set(plan) <= {
            "held_asset", "held_sid", "held_coh", "new_coh",
            "freed_risk", "reason",
        }
        import prop_harness.brains.eval_profile as mod
        src = open(mod.__file__).read()
        assert "intelligence.mfe" not in src
        assert "intelligence.roe" not in src
        assert "intelligence.treasury" not in src


class TestFamilyMap:
    def test_known_assets(self):
        assert family_of("BTC") == "majors"
        assert family_of("XRP") == "large_alt"
        assert family_of("ZEC") == "privacy"
        assert family_of("xyz:GOLD") == "metal"
        assert family_of("xyz:CRCL") == "equity_fintech"

    def test_unknown_asset_is_solo_never_blocked(self):
        assert family_of("xyz:VIX") == "solo:xyz:VIX"
        assert family_of("MYSTERY") == "solo:MYSTERY"
        # solo families are unique per asset -> two unknowns never collide
        assert family_of("AAA") != family_of("BBB")
