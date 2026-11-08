"""Eval-profile doctrine pins (Governor 2026-11-08) + loss-veto math.

The $25 evaluation account is an OPTION: P(pass)/time is the objective,
variance is cheap, losses cut by geometry, winners run. These pins lock
the verdict math so a future "cleanup" can never quietly revert the
doctrine to the funded-phase (standard) register.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from prop_harness.brains import eval_profile as ep
from prop_harness.brains import loss_stats as ls
from prop_harness.brains.budget import BudgetBrain
from prop_harness.brains.drawdown import DrawdownBrain
from prop_harness.config import PropConfig
from prop_harness.executor import ProprExecutor

EVAL = ep.PROFILE_EVALUATION
STD = ep.PROFILE_STANDARD


# ── risk_for_signal ──────────────────────────────────────────────────

class TestRiskForSignal:
    def test_standard_abstains(self):
        assert ep.risk_for_signal(STD, 9.0, 40.0, 200.0, 30.0, 1.0,
                                  5.0, 50.0) is None

    def test_probe_below_bar(self):
        # coherence <= 7 keeps the probe class: r_base x mult
        assert ep.risk_for_signal(EVAL, 6.9, 40.0, 200.0, 30.0, 1.0,
                                  5.0, 50.0) == 5.0

    def test_boundary_seven_is_probe(self):
        # strictly greater than coh_big — 7.0 exactly stays probe
        assert ep.risk_for_signal(EVAL, 7.0, 40.0, 200.0, 30.0, 1.0,
                                  5.0, 50.0) == 5.0

    def test_big_bet_seventy_percent(self):
        # coh > 7 -> 70% of remaining budget
        assert ep.risk_for_signal(EVAL, 7.5, 40.0, 200.0, 30.0, 1.0,
                                  5.0, 50.0) == 28.0

    def test_full_budget_at_max(self):
        # coh >= 8.5 -> 100% of remaining
        assert ep.risk_for_signal(EVAL, 8.7, 40.0, 200.0, 30.0, 1.0,
                                  5.0, 50.0) == 40.0

    def test_drawdown_mult_compresses(self):
        # CAUTIOUS band mult 0.6 compresses the big bet
        assert ep.risk_for_signal(EVAL, 7.5, 40.0, 200.0, 30.0, 0.6,
                                  5.0, 50.0) == pytest.approx(40.0 * 0.7 * 0.6)

    def test_headroom_clamp_never_bankrupts(self):
        # headroom - hard_zero binds below the budget on a small account
        assert ep.risk_for_signal(EVAL, 9.0, 40.0, 45.0, 30.0, 1.0,
                                  5.0, 50.0) == 15.0

    def test_unparseable_coherence_is_probe(self):
        assert ep.risk_for_signal(EVAL, None, 40.0, 200.0, 30.0, 1.0,
                                  5.0, 50.0) == 5.0


# ── margin cap / leverage ladder ─────────────────────────────────────

class TestMarginCap:
    def test_standard_never_binds(self):
        assert ep.margin_ok(STD, 1e9, "BTC", 100.0) is None

    def test_big_notional_needs_the_10x_class(self):
        # $3000 notional on BTC (10x) = $300 margin <= 30% x $1500 equity
        assert ep.margin_ok(EVAL, 3000.0, "BTC", 1500.0) is True
        # same notional on a 2x alt = $1500 margin > 30% x $1500
        assert ep.margin_ok(EVAL, 3000.0, "UNLISTEDALT", 1500.0) is False

    def test_leverage_ladder(self):
        assert ep.leverage_for("BTC") == 10
        assert ep.leverage_for("xyz:EUR") == 25
        assert ep.leverage_for("xyz:GOLD") == 8
        assert ep.leverage_for("xyz:SP500") == 10
        assert ep.leverage_for("xyz:UNKNOWN") == 4
        assert ep.leverage_for("SOMEALT") == 2


# ── trend day / daily floor ──────────────────────────────────────────

class TestDailyFloor:
    def test_eval_flat_day(self):
        assert ep.daily_floor(EVAL, 0.0, 40.0, 50.0, 33.0) == 40.0

    def test_eval_trend_day(self):
        assert ep.daily_floor(EVAL, 3.2, 40.0, 50.0, 33.0) == 50.0

    def test_eval_negative_realized_is_not_trend(self):
        assert ep.daily_floor(EVAL, -1.0, 40.0, 50.0, 33.0) == 40.0

    def test_standard_passthrough(self):
        assert ep.daily_floor(STD, 99.0, 40.0, 50.0, 33.0) == 33.0


# ── breakeven arm ────────────────────────────────────────────────────

class TestBreakevenArm:
    def test_arms_at_half_r(self):
        b = {"risk_usd": 10.0}
        assert ep.should_arm_breakeven(EVAL, b, 5.0) is True
        assert ep.should_arm_breakeven(EVAL, b, 4.99) is False

    def test_never_rearms(self):
        assert ep.should_arm_breakeven(EVAL, {"risk_usd": 10.0,
                                              "be_armed": True}, 99.0) is False

    def test_standard_never_arms(self):
        assert ep.should_arm_breakeven(STD, {"risk_usd": 10.0}, 99.0) is False


# ── reload graph ─────────────────────────────────────────────────────

def _proven_short(entry="260.88", released=3.0):
    return {"side": "sell", "entry_ref": entry, "risk_released": released,
            "reloads": 0}


class TestReloadVerdict:
    def test_unproven_refused(self):
        # no TP banked, no breakeven -> martingale class, refused
        b = {"side": "sell", "entry_ref": "260.88"}
        assert ep.reload_verdict(EVAL, b, 9.5, 250.0) is None

    def test_better_price_full_reload(self):
        # short: higher fresh entry = better; coh > 7 -> full harvested risk
        v = ep.reload_verdict(EVAL, _proven_short(), 7.5, 265.0)
        assert v == {"frac": 1.0, "better": True}

    def test_worse_price_needs_the_85_bar_half_size(self):
        v = ep.reload_verdict(EVAL, _proven_short(), 8.6, 250.0)
        assert v == {"frac": 0.5, "better": False}
        assert ep.reload_verdict(EVAL, _proven_short(), 7.5, 250.0) is None

    def test_be_armed_counts_as_proven(self):
        b = {"side": "sell", "entry_ref": "260.88", "be_armed": True,
             "reloads": 0}
        assert ep.reload_verdict(EVAL, b, 7.5, 265.0) is not None

    def test_reload_budget_exhausted(self):
        b = _proven_short()
        b["reloads"] = 1
        assert ep.reload_verdict(EVAL, b, 9.5, 265.0, max_reloads=1) is None

    def test_standard_never_reloads(self):
        assert ep.reload_verdict(STD, _proven_short(), 9.5, 265.0) is None

    def test_reload_spends_harvested_only(self):
        assert ep.reload_size(_proven_short(released=3.0), 0.5) == 1.5
        assert ep.reload_size({"risk_released": 0.0}, 1.0) == 0.0

    def test_tighten_only(self):
        assert ep.tightened_stop("sell", 264.0, 262.0) == 262.0
        assert ep.tightened_stop("sell", 262.0, 264.0) == 262.0
        assert ep.tightened_stop("buy", 250.0, 255.0) == 255.0
        assert ep.tightened_stop("buy", 255.0, 250.0) == 255.0
        assert ep.tightened_stop("buy", None, 250.0) is None


# ── loss-veto brain ──────────────────────────────────────────────────

def _close(fam, side, pnl, ts):
    return {"family": fam, "side": side, "realized_pnl_final": pnl,
            "closed_ts": ts}


class TestLossStats:
    def test_veto_needs_n_and_negative_shrunk(self):
        now = 1_000_000.0
        closes = [_close("crypto_majors", "sell", -5.0, now - 100)
                  for _ in range(6)]
        stats = ls.CohortStats().build(closes, now=now)
        row = stats["crypto_majors|sell"]
        assert row["n"] == 6
        assert row["shrunk_expectancy"] == pytest.approx(-30.0 / 26.0)
        assert row["veto"] is True

    def test_small_n_never_vetoes(self):
        now = 1_000_000.0
        closes = [_close("crypto_majors", "sell", -50.0, now - 100)
                  for _ in range(4)]
        stats = ls.CohortStats().build(closes, now=now)
        assert stats["crypto_majors|sell"]["veto"] is False

    def test_shrinkage_protects_lucky_cohorts(self):
        now = 1_000_000.0
        closes = ([_close("f", "buy", 5.0, now - 100) for _ in range(5)]
                  + [_close("f", "buy", -5.0, now - 100) for _ in range(5)])
        stats = ls.CohortStats().build(closes, now=now)
        assert stats["f|buy"]["veto"] is False

    def test_window_drops_old_closes(self):
        now = 1_000_000.0
        closes = [_close("f", "buy", -5.0, now - 8 * 86400)
                  for _ in range(10)]
        assert ls.CohortStats().build(closes, now=now) == {}

    def test_artifact_fresh_fail_open(self):
        assert ls.artifact_fresh(None) is False
        assert ls.artifact_fresh({}) is False
        assert ls.artifact_fresh({"ts": 100.0}, now=100.0 + 7 * 3600) is False
        assert ls.artifact_fresh({"ts": 100.0}, now=100.0 + 60) is True

    def test_veto_for(self):
        stats = {"f|sell": {"veto": True}, "f|buy": {"veto": False}}
        assert ls.veto_for(stats, "f", "sell") == {"veto": True}
        assert ls.veto_for(stats, "f", "buy") is None
        assert ls.veto_for({}, "f", "sell") is None

    def test_close_pnl_from_trades(self):
        trades = [
            {"type": "open", "realizedPnl": "0",
             "executedAt": "2026-11-08T10:00:01Z"},
            {"type": "reduce", "realizedPnl": "2.5",
             "executedAt": "2026-11-08T10:30:00Z"},
            {"type": "close", "realizedPnl": "-1.0",
             "executedAt": "2026-11-08T11:00:00Z"},
            {"type": "reduce", "realizedPnl": "99.0",
             "executedAt": "2026-11-07T11:00:00Z"},  # before opened_ts
        ]
        from datetime import datetime, timezone
        opened = datetime(2026, 11, 8, 10, 0, 0,
                          tzinfo=timezone.utc).timestamp()
        assert ls.close_pnl_from_trades(trades, opened) == pytest.approx(1.5)
        assert ls.close_pnl_from_trades([], opened) is None


# ── executor: breakeven arm + reload + growth sync ───────────────────

class _FakeClient:
    account_id = "urn:prp-account:x"

    def __init__(self):
        self.placed: list[dict] = []
        self.cancelled: list[str] = []

    def get_open_orders(self, base=None):
        return []

    def place_orders(self, body):
        self.placed.append(body)
        return [{"orderId": "urn:prp-order:new", **body["orders"][0]}]

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return {}


def _exec(tmp_path: Path, **kw):
    kw.setdefault("profile", "evaluation")
    cfg = PropConfig(enabled=True, state_root=tmp_path, **kw)
    dd = DrawdownBrain(tmp_path / "dd.json", floor_line=4850.0,
                       floor_usd=150.0)
    dd.update(5000.0)
    budget = BudgetBrain(tmp_path / "budget.json", hard_zero=30.0,
                         divisor=10.0, floor_budget=40.0)
    budget.roll(150.0)
    return ProprExecutor(cfg, _FakeClient(), budget, dd)


def _open_short(risk=10.0, released=0.0, qty="1.0"):
    return {"semantic_id": "prop-acct1-BCH-1", "asset": "BCH", "side": "sell",
            "family": "large_alt", "quantity": qty, "risk_usd": risk,
            "entry_ref": "260.88", "stop_price": "264.6608",
            "current_stop": "264.6608", "be_armed": False, "reloads": 0,
            "risk_released": released, "status": "open", "absence_count": 0,
            "opened_ts": 1.0}


class TestArmBreakeven:
    def test_arms_and_restamps(self, tmp_path):
        ex = _exec(tmp_path)
        b = _open_short()
        pos = {"quantity": "1.0", "unrealizedPnl": "5.0",
               "positionId": "p1"}
        assert ex.arm_breakeven(b, pos) is True
        assert b["be_armed"] is True
        assert b["current_stop"] == "260.88"
        order = ex.client.placed[0]["orders"][0]
        assert order["triggerPrice"] == "260.88"
        assert order["reduceOnly"] is True

    def test_below_half_r_noop(self, tmp_path):
        ex = _exec(tmp_path)
        b = _open_short()
        pos = {"quantity": "1.0", "unrealizedPnl": "4.99"}
        assert ex.arm_breakeven(b, pos) is False
        assert ex.client.placed == []

    def test_standard_profile_noop(self, tmp_path):
        ex = _exec(tmp_path, profile="standard")
        b = _open_short()
        pos = {"quantity": "1.0", "unrealizedPnl": "999.0"}
        assert ex.arm_breakeven(b, pos) is False


class TestReload:
    def test_reload_spends_harvested_and_tightens(self, tmp_path):
        ex = _exec(tmp_path)
        b = _open_short(released=4.0)
        # better price (higher, short) + coh > 7 -> full harvested risk
        res = ex.reload_bracket(b, 7.5, 262.0, fresh_stop=263.0)
        assert res.ok, res.reason
        assert b["reloads"] == 1
        assert b["risk_released"] == pytest.approx(0.0)
        assert b["risk_usd"] == pytest.approx(14.0)
        # tighten-only: min(264.6608, 263.0) for a short
        assert b["current_stop"] == "263.0"
        order = ex.client.placed[0]["orders"][0]
        assert order["type"] == "market"
        assert order["side"] == "sell"
        assert order["reduceOnly"] is False

    def test_unproven_never_reloads(self, tmp_path):
        ex = _exec(tmp_path)
        b = _open_short(released=0.0)
        res = ex.reload_bracket(b, 9.5, 262.0, fresh_stop=263.0)
        assert not res.ok
        assert ex.client.placed == []

    def test_failed_place_releases_budget(self, tmp_path):
        ex = _exec(tmp_path)

        class _Boom(_FakeClient):
            def place_orders(self, body):
                raise RuntimeError("venue 5xx")

        ex.client = _Boom()
        b = _open_short(released=4.0)
        before = ex.budget.state.committed
        res = ex.reload_bracket(b, 7.5, 262.0, fresh_stop=263.0)
        assert not res.ok
        assert ex.budget.state.committed == before
        assert b["reloads"] == 0


class TestGrowthSync:
    def test_growth_resizes_protection_up(self, tmp_path):
        ex = _exec(tmp_path)
        b = _open_short(qty="1.0")
        pos = {"quantity": "1.5", "realizedPnl": "0.0", "positionId": "p1"}
        assert ex.sync_bracket_size(b, pos) is True
        assert b["quantity"] == "1.5"
        order = ex.client.placed[0]["orders"][0]
        assert order["quantity"] == "1.5"
        # stop anchored at current_stop, not the birth stop field
        assert order["triggerPrice"] == "264.6608"


class TestLossVetoConsume:
    def test_fresh_veto_rejects(self, tmp_path):
        import time as _time
        from prop_harness.io.journal import atomic_write_json
        ex = _exec(tmp_path)
        artifact = tmp_path / "program" / "loss_veto.json"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(artifact, {
            "ts": _time.time(),
            "stats": {"large_alt|sell": {"n": 6, "veto": True,
                                         "shrunk_expectancy": -1.2}},
        })
        res = ex.place_bracket("BCH", "sell", 5.0, 260.0, 264.0)
        assert not res.ok
        assert "loss veto" in res.reason

    def test_stale_artifact_fails_open(self, tmp_path):
        from prop_harness.io.journal import atomic_write_json
        ex = _exec(tmp_path)
        artifact = tmp_path / "program" / "loss_veto.json"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(artifact, {
            "ts": 1.0,
            "stats": {"large_alt|sell": {"n": 60, "veto": True,
                                         "shrunk_expectancy": -9.0}},
        })
        res = ex.place_bracket("BCH", "sell", 5.0, 260.0, 264.0)
        # veto lifted (stale) — the reject now comes from a later gate or
        # the (fake) venue accepts
        assert "loss veto" not in res.reason
