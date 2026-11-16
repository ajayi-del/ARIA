"""ATS-26 KingdomView crisis valve pins — joint n_eff / reduce / block / anti-lock."""

from __future__ import annotations

import pytest

from prop_harness.brains import crisis_valve as cv
from prop_harness.brains import kingdom_view as kv


NOW = 1_700_000_000.0


def _leg(asset, side="sell", be_armed=False):
    return {
        "asset": asset, "side": side, "entry_ref": 100.0, "stop_price": 101.0,
        "quantity": 1.0, "risk_usd": 5.0, "opened_ts": NOW - 10,
        "be_armed": be_armed, "r_moved": 0.0,
    }


def _view(legs_by_army, *, incomplete=False, age=5.0):
    armies = {}
    for aid, legs in legs_by_army.items():
        armies[aid] = {
            "ts": NOW - age,
            "account_id": aid,
            "sleeve": "crypto",
            "max_positions": 3,
            "positions": legs,
        }
    view = kv.build_kingdom_view(
        armies, now=NOW, expected_ids=list(armies.keys()))
    if incomplete:
        view["incomplete"] = True
    return view


class TestNEff:
    def test_same_family_side_counts_once(self):
        # BCH+XRP+LTC shorts = one large_alt|sell factor across armies
        # (the 2026-11-07 live class — correlated selloff collapse)
        view = _view({
            "demo": [_leg("BCH"), _leg("XRP")],
            "turbo1": [_leg("LTC")],
        })
        factors = cv.open_factors(view)
        assert set(factors) == {"large_alt|sell"}
        assert cv.compute_n_eff(view) == 1
        assert factors["large_alt|sell"]["n_legs"] == 3

    def test_opposite_side_is_separate_factor(self):
        view = _view({
            "turbo1": [_leg("BTC", "sell"), _leg("ETH", "buy")],
        })
        assert cv.compute_n_eff(view) == 2  # majors|sell + majors|buy

    def test_distinct_families(self):
        view = _view({
            "turbo1": [_leg("BTC"), _leg("SOL"), _leg("NEAR")],
            "turbo2": [_leg("xyz:NVDA", "buy")],
        })
        # majors|sell, large_alt|sell, l1_alt|sell, equity_semis|buy
        assert cv.compute_n_eff(view) == 4


class TestAntiLock:
    def test_disabled_always_clear(self):
        view = _view({"turbo1": [_leg("BTC"), _leg("SOL"), _leg("NEAR"),
                                 _leg("AAVE")]})
        # Force high n_eff but valve off
        v = cv.crisis_verdict(
            enabled=False, view=view, asset="DOGE", side="sell",
            coherence=5.0, planned_risk_usd=20.0, now=NOW)
        assert v["action"] == "CLEAR"
        assert v["risk_usd"] == 20.0
        assert v["reason"] == "crisis_valve_off"

    def test_stale_view_fail_open(self):
        view = _view({"turbo1": [_leg("BTC"), _leg("SOL"), _leg("NEAR")]},
                     age=500.0)
        view["ts"] = NOW - 500.0
        v = cv.crisis_verdict(
            enabled=True, view=view, asset="DOGE", side="sell",
            coherence=5.0, planned_risk_usd=20.0, now=NOW, stale_s=90.0)
        assert v["action"] == "CLEAR"
        assert v["reason"] == "view_unusable_fail_open"

    def test_incomplete_fail_open(self):
        view = _view({"turbo1": [_leg("BTC"), _leg("SOL"), _leg("NEAR")]},
                     incomplete=True)
        v = cv.crisis_verdict(
            enabled=True, view=view, asset="DOGE", side="sell",
            coherence=5.0, planned_risk_usd=20.0, now=NOW)
        assert v["action"] == "CLEAR"
        assert v["reason"] == "view_unusable_fail_open"

    def test_missing_view_fail_open(self):
        v = cv.crisis_verdict(
            enabled=True, view=None, asset="BTC", side="buy",
            coherence=6.0, planned_risk_usd=5.0, now=NOW)
        assert v["action"] == "CLEAR"
        assert v["reason"] == "view_unusable_fail_open"


class TestReduceBlock:
    def test_reduce_at_threshold(self):
        # 3 distinct factors → REDUCE new entry
        view = _view({
            "turbo1": [_leg("BTC"), _leg("SOL"), _leg("NEAR")],
        })
        assert cv.compute_n_eff(view) == 3
        v = cv.crisis_verdict(
            enabled=True, view=view, asset="AAVE", side="sell",
            coherence=5.0, planned_risk_usd=20.0, now=NOW,
            reduce_at=3, block_at=4, n_eff_budget=4, reduce_mult=0.5)
        assert v["action"] == "REDUCE"
        assert v["risk_usd"] == pytest.approx(10.0)
        assert v["would_raise"] is True

    def test_block_new_factor_at_budget(self):
        view = _view({
            "turbo1": [_leg("BTC"), _leg("SOL")],
            "turbo2": [_leg("NEAR"), _leg("AAVE")],
        })
        assert cv.compute_n_eff(view) == 4
        v = cv.crisis_verdict(
            enabled=True, view=view, asset="DOGE", side="sell",
            coherence=5.0, planned_risk_usd=20.0, now=NOW,
            reduce_at=3, block_at=4, n_eff_budget=4)
        assert v["action"] == "BLOCK"
        assert v["risk_usd"] == 0.0

    def test_same_factor_not_blocked(self):
        # Already hold majors|sell — second majors asset is same factor;
        # family_cap (per-army) owns doubles; crisis does not BLOCK.
        view = _view({
            "turbo1": [_leg("BTC"), _leg("SOL"), _leg("NEAR"), _leg("AAVE")],
        })
        assert cv.compute_n_eff(view) == 4
        v = cv.crisis_verdict(
            enabled=True, view=view, asset="ETH", side="sell",
            coherence=5.0, planned_risk_usd=20.0, now=NOW,
            reduce_at=3, block_at=4, n_eff_budget=4, reduce_mult=0.5)
        assert v["would_raise"] is False
        assert v["action"] == "REDUCE"  # still haircut at n_eff>=3
        assert v["risk_usd"] == pytest.approx(10.0)

    def test_thesis_reserve_exempts_block(self):
        view = _view({
            "turbo1": [_leg("BTC"), _leg("SOL"), _leg("NEAR"), _leg("AAVE")],
        })
        v = cv.crisis_verdict(
            enabled=True, view=view, asset="DOGE", side="sell",
            coherence=9.0, planned_risk_usd=40.0, now=NOW,
            reduce_at=3, block_at=4, n_eff_budget=4, reserve_coh=8.5,
            reduce_mult=0.5)
        assert v["reserved"] is True
        assert v["action"] == "REDUCE"  # not BLOCK; still haircuts
        assert v["risk_usd"] == pytest.approx(20.0)

    def test_be_armed_held_factor_reserved(self):
        view = _view({
            "turbo1": [_leg("BTC", be_armed=True), _leg("SOL"),
                       _leg("NEAR"), _leg("AAVE")],
        })
        v = cv.crisis_verdict(
            enabled=True, view=view, asset="ETH", side="sell",
            coherence=4.0, planned_risk_usd=20.0, now=NOW,
            reduce_at=3, block_at=4, n_eff_budget=4, reduce_mult=0.5)
        assert v["reserved"] is True
        assert v["would_raise"] is False
        assert v["action"] == "REDUCE"

    def test_stress_forces_reduce(self):
        view = _view({"turbo1": [_leg("BTC")]})  # n_eff=1 < reduce_at
        v = cv.crisis_verdict(
            enabled=True, view=view, asset="ETH", side="buy",
            coherence=5.0, planned_risk_usd=20.0, now=NOW,
            reduce_at=3, block_at=4, stress_active=True,
            stress_enabled=True, reduce_mult=0.5)
        assert v["action"] == "REDUCE"
        assert v["risk_usd"] == pytest.approx(10.0)

    def test_stress_kill_switch(self):
        view = _view({"turbo1": [_leg("BTC")]})
        v = cv.crisis_verdict(
            enabled=True, view=view, asset="ETH", side="buy",
            coherence=5.0, planned_risk_usd=20.0, now=NOW,
            stress_active=True, stress_enabled=False)
        assert v["action"] == "CLEAR"
        assert v["plane"]["stress_active"] is False

    def test_plane_never_owns_tp_or_hl_risk(self):
        view = _view({"turbo1": [_leg("BTC")]})
        plane = cv.crisis_plane(view, now=NOW)
        assert plane["owns_tp"] is False
        assert plane["hl_in_risk_math"] is False
        assert plane["schema"] == 1


class TestKingdomFold:
    def test_crisis_optional_on_view(self):
        armies = {"turbo1": {
            "ts": NOW, "account_id": "turbo1", "sleeve": "crypto",
            "max_positions": 3, "positions": [_leg("BTC")],
        }}
        provisional = kv.build_kingdom_view(
            armies, now=NOW, expected_ids=["turbo1"])
        crisis = cv.crisis_plane(provisional, now=NOW)
        view = kv.build_kingdom_view(
            armies, now=NOW, expected_ids=["turbo1"], crisis=crisis)
        assert view["crisis"]["n_eff"] == 1
        assert view["crisis"]["mode"] == "CLEAR"
