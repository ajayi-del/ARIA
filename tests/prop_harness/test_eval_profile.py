"""Eval-profile doctrine pins (Governor 2026-11-08) + loss-veto math.

The $25 evaluation account is an OPTION: P(pass)/time is the objective,
variance is cheap, losses cut by geometry, winners run. These pins lock
the verdict math so a future "cleanup" can never quietly revert the
doctrine to the funded-phase (standard) register.
"""

from __future__ import annotations

import time
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


# ── risk_ceiling (Kant belt; must not re-impose r_max=$7.5) ───────────

class TestRiskCeiling:
    def test_standard_abstains(self):
        assert ep.risk_ceiling(STD, 40.0, 200.0, 30.0, 50.0) is None

    def test_eval_allows_big_bet(self):
        # remaining $35, trend max $50 → ceiling $35 (not legacy $7.5)
        assert ep.risk_ceiling(EVAL, 35.0, 165.0, 30.0, 50.0) == 35.0

    def test_eval_clamps_to_r_max_eval(self):
        assert ep.risk_ceiling(EVAL, 40.0, 200.0, 30.0, 50.0) == 40.0
        assert ep.risk_ceiling(EVAL, 60.0, 200.0, 30.0, 50.0) == 50.0

    def test_eval_hard_zero_forbids(self):
        assert ep.risk_ceiling(EVAL, 40.0, 30.0, 30.0, 50.0) == 0.0
        assert ep.risk_ceiling(EVAL, 40.0, 25.0, 30.0, 50.0) == 0.0

    def test_eval_headroom_clamp(self):
        # headroom - hard_zero = 15 binds below remaining
        assert ep.risk_ceiling(EVAL, 40.0, 45.0, 30.0, 50.0) == 15.0


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
        # commodity default 5 — COPPER@8 was 13304 live
        assert ep.leverage_for("xyz:COPPER") == 5
        assert ep.leverage_for("xyz:NATGAS") == 5
        # country ETFs / MAGS = equity class max 4 (5 → 13304 live)
        assert ep.leverage_for("xyz:EWJ") == 4
        assert ep.leverage_for("xyz:MAGS") == 4
        assert ep.leverage_for("xyz:UNKNOWN") == 4
        assert ep.leverage_for("SOMEALT") == 2

    def test_venue_max_and_clamp_respects_limits_api(self):
        # Live turbo2 2026-11-08 shape of /leverage-limits/effective.
        limits = {
            "defaults": {
                "crypto": 2, "equity": 4, "pre_ipo": 4,
                "index": 5, "commodity": 5, "fx": 25,
            },
            "overrides": {
                "BTC": 10, "xyz:SP500": 10, "xyz:GOLD": 8, "xyz:CL": 8,
            },
        }
        assert ep.venue_class_for("xyz:COPPER") == "commodity"
        assert ep.venue_class_for("xyz:EWJ") == "equity"
        assert ep.venue_class_for("xyz:JP225") == "index"
        assert ep.venue_max_leverage("xyz:GOLD", limits) == 8
        assert ep.venue_max_leverage("xyz:COPPER", limits) == 5
        assert ep.venue_max_leverage("xyz:EWJ", limits) == 4
        assert ep.venue_max_leverage("BTC", limits) == 10
        # clamp never raises above venue; unknown limits = passthrough
        assert ep.clamp_leverage("xyz:COPPER", 8, limits) == 5
        assert ep.clamp_leverage("xyz:EWJ", 5, limits) == 4
        assert ep.clamp_leverage("xyz:SP500", 10, limits) == 10
        assert ep.clamp_leverage("xyz:COPPER", 8, None) == 8

    def test_provision_assets_sleeve_filter(self):
        # legacy (candidates=None): elevated overrides only
        crypto = ep.provision_assets_for("crypto")
        assert crypto == ("BTC", "ETH", "SOL")
        tradfi = ep.provision_assets_for("tradfi")
        assert "BTC" not in tradfi
        assert "xyz:SP500" in tradfi and "xyz:XYZ100" in tradfi
        assert "xyz:GOLD" in tradfi and "xyz:EUR" in tradfi
        assert all(a.startswith("xyz:") for a in tradfi)
        all_assets = ep.provision_assets_for("all")
        assert set(all_assets) == set(crypto) | set(tradfi)
        assert all_assets == tuple(sorted(all_assets))

    def test_provision_assets_family_cover_not_override_only(self):
        # ATS-16: pass family-map keys → wave-3 names provision, not just 27
        from prop_harness.families import family_assets
        fam = family_assets()
        assert len(fam) == 161
        crypto = ep.provision_assets_for("crypto", fam)
        tradfi = ep.provision_assets_for("tradfi", fam)
        assert "TIA" in crypto and "BCH" in crypto and "BTC" in crypto
        assert "xyz:NVDA" in tradfi and "xyz:EUR" in tradfi
        assert "BTC" not in tradfi and "xyz:NVDA" not in crypto
        # venue-refused names never enter the plan (13302 class)
        assert "xyz:TTF" not in tradfi and "xyz:IBOV" not in tradfi
        assert "xyz:TTF" not in ep.provision_assets_for("all", fam)
        # cover gap closed: family − refused ≈ provision(all)
        refused = ep._PROVISION_REFUSED
        assert set(ep.provision_assets_for("all", fam)) == set(fam) - refused

    def test_target_leverage_prefers_venue_truth(self):
        limits = {
            "defaults": {
                "crypto": 2, "equity": 4, "pre_ipo": 4,
                "index": 5, "commodity": 5, "fx": 25,
            },
            "overrides": {
                "BTC": 10, "xyz:SP500": 10, "xyz:GOLD": 8,
            },
        }
        # venue override / class default win over static when known
        assert ep.target_leverage("BTC", limits) == 10
        assert ep.target_leverage("TIA", limits) == 2          # crypto default
        assert ep.target_leverage("xyz:NVDA", limits) == 4     # equity default
        assert ep.target_leverage("xyz:GOLD", limits) == 8
        assert ep.target_leverage("xyz:EUR", limits) == 25
        # limits dark → static ladder fail-closed
        assert ep.target_leverage("TIA", None) == 2
        assert ep.target_leverage("BTC", None) == 10

    def test_provision_assets_rejects_bad_sleeve(self):
        with pytest.raises(ValueError):
            ep.provision_assets_for("commodity")


# ── eval TP ladder vs funded DEFAULT ─────────────────────────────────

class TestEvalLadderDoctrine:
    def test_eval_ladder_is_1_2_5_not_default(self):
        # evaluation places 1R/2.5R/5R (40/30/30); funded keeps 2/3.5/6
        assert ep.EVAL_LADDER == (("1.0", "0.4"), ("2.5", "0.3"), ("5.0", "0.3"))
        from prop_harness.bracket import DEFAULT_LADDER
        assert DEFAULT_LADDER == (("2.0", "0.5"), ("3.5", "0.3"), ("6.0", "0.2"))
        assert ep.EVAL_LADDER != DEFAULT_LADDER

    def test_tia_sell_uses_eval_ladder_geometry(self):
        # turbo1 TIA short (eval profile) must bank 1R/2.5R/5R, not funded
        # DEFAULT 2/3.5/6. signal_tap → EVAL_LADDER when profile=evaluation.
        from decimal import Decimal
        from prop_harness.bracket import DEFAULT_LADDER, build_bracket, tp_price
        entry, stop = Decimal("1.00"), Decimal("1.02")  # sell: stop above
        body = build_bracket(
            account_id="acct", asset="TIA", side="sell",
            quantity="10.00", entry_ref=str(entry), stop_price=str(stop),
            ts_ms=1, ladder=ep.EVAL_LADDER)
        tps = [o for o in body["orders"] if o["type"] == "take_profit_limit"]
        assert len(tps) == 3
        expect = [
            tp_price(entry, stop, Decimal("1.0"), "sell"),
            tp_price(entry, stop, Decimal("2.5"), "sell"),
            tp_price(entry, stop, Decimal("5.0"), "sell"),
        ]
        default_expect = [
            tp_price(entry, stop, Decimal(r), "sell")
            for r, _ in DEFAULT_LADDER
        ]
        prices = [Decimal(o["price"]) for o in tps]
        assert prices == expect
        assert prices != default_expect
        # main.py: evaluation → ladder = ep.EVAL_LADDER (not DEFAULT)
        assert ep.EVAL_LADDER[0][0] == "1.0"
        assert DEFAULT_LADDER[0][0] == "2.0"


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

    def test_pnl_set_but_family_missing_stays_dark(self):
        # Dayo live class: realized_pnl_final written, family=None → empty
        # loss_veto.json — CohortStats skips; turbo/sniper learning unbound.
        now = 1_000_000.0
        cold = {"asset": "BTC", "side": "sell", "family": None,
                "realized_pnl_final": -1.25, "closed_ts": now - 10,
                "risk_usd": 5.0}
        assert ls.CohortStats(min_n=1).build([cold], now=now) == {}

    def test_stamp_backfills_family_from_families_map(self):
        from prop_harness.families import family_of
        now = 1_000_000.0
        row = {"asset": "BTC", "side": "sell", "family": None,
               "realized_pnl_final": -1.25, "closed_ts": now - 10,
               "risk_usd": 5.0}
        assert ls.stamp_close_outcome(row, family=family_of("BTC"),
                                      coherence_at_entry=7.2) is True
        assert row["family"] == "majors"
        assert row["coherence_at_entry"] == pytest.approx(7.2)
        stats = ls.CohortStats().build([row], now=now)  # default min_n=5
        assert "majors|sell" in stats
        assert stats["majors|sell"]["n"] == 1
        assert stats["majors|sell"]["veto"] is False  # n < min_n; turbos fail-open

    def test_stamp_booked_pnl_fallback(self):
        row = {"family": "large_alt", "side": "buy", "closed_ts": 1.0,
               "realized_booked": "-2.5"}
        assert ls.stamp_close_outcome(row) is True
        assert row["realized_pnl_final"] == pytest.approx(-2.5)


class TestOutcomeStampPublish:
    """Demo publisher must emit non-empty loss_veto stats once family+PnL
    flow — turbo consume + future sniper learning bind on rich outcomes."""

    def test_publish_heals_legacy_into_nonempty_artifact(self, tmp_path):
        from types import SimpleNamespace
        from prop_harness.config import PropConfig
        from prop_harness.families import family_of
        from prop_harness.io.journal import atomic_write_json, read_json
        from prop_harness.main import Harness

        now = time.time()
        reg = {"brackets": [{
            "semantic_id": "btc-1", "asset": "BTC", "side": "sell",
            "status": "closed_external", "family": None,
            "realized_pnl_final": -1.25, "closed_ts": now - 30,
            "risk_usd": 5.0, "coherence_at_entry": 6.5,
        }]}
        # Precondition: raw CohortStats stays dark (family null).
        assert ls.CohortStats(min_n=1).build(reg["brackets"], now=now) == {}

        cfg = PropConfig(enabled=True, state_root=tmp_path,
                         loss_veto_enabled=True, loss_veto_path="",
                         loss_veto_min_n=1)
        (tmp_path / "program").mkdir(parents=True, exist_ok=True)
        (tmp_path / "accounts" / "acct1").mkdir(parents=True, exist_ok=True)
        reg_path = tmp_path / "accounts" / "acct1" / "registry.json"
        atomic_write_json(reg_path, reg)

        saved = {}

        def _save(r):
            saved["reg"] = r
            atomic_write_json(reg_path, r)

        h = object.__new__(Harness)
        h.config = cfg
        h.executor = SimpleNamespace(
            _registry=lambda: read_json(reg_path, {"brackets": []}),
            _save_registry=_save,
        )
        h.publish_loss_veto()

        artifact = read_json(cfg.loss_veto_artifact)
        assert artifact is not None
        assert artifact["stats"], "demo loss_veto.json must be non-empty"
        key = f"{family_of('BTC')}|sell"
        assert key in artifact["stats"]
        assert artifact["stats"][key]["n"] == 1
        assert saved["reg"]["brackets"][0]["family"] == "majors"


# ── overflow gate ────────────────────────────────────────────────────

def _snap(sleeve="crypto", n=3, maxp=3, age=5.0, now=1_000_000.0):
    return {"ts": now - age, "sleeve": sleeve, "max_positions": maxp,
            "positions": [{"asset": f"A{i}"} for i in range(n)]}


class TestOverflowVerdict:
    NOW = 1_000_000.0

    def test_owning_peer_full_allows(self):
        assert ep.overflow_verdict([_snap(n=3)], False, self.NOW) is True

    def test_owning_peer_has_room_refuses(self):
        assert ep.overflow_verdict([_snap(n=2)], False, self.NOW) is False

    def test_peer_max_positions_respected(self):
        assert ep.overflow_verdict(
            [_snap(n=1, maxp=1)], False, self.NOW) is True

    def test_other_class_peer_does_not_cover(self):
        # a tradfi peer never covers a crypto signal
        assert ep.overflow_verdict([_snap(sleeve="tradfi")],
                                   False, self.NOW) is False
        assert ep.overflow_verdict([_snap(sleeve="tradfi")],
                                   True, self.NOW) is True

    def test_sleeve_all_covers_both(self):
        assert ep.overflow_verdict([_snap(sleeve="all")],
                                   False, self.NOW) is True
        assert ep.overflow_verdict([_snap(sleeve="all")],
                                   True, self.NOW) is True

    def test_two_covering_peers_both_must_be_full(self):
        snaps = [_snap(n=3), _snap(n=1)]
        assert ep.overflow_verdict(snaps, False, self.NOW) is False

    def test_stale_peer_fails_closed(self):
        assert ep.overflow_verdict([_snap(age=3600.0)],
                                   False, self.NOW) is False

    def test_missing_peer_fails_closed(self):
        assert ep.overflow_verdict([None], False, self.NOW) is False
        assert ep.overflow_verdict([], False, self.NOW) is False


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


# ── trailing stops (HL mark plane, spec §3.8 build 2) ────────────────

def _trail_short(current=None):
    # short: entry 260.88, birth stop 264.6608 -> stop_dist 3.7808
    return {"side": "sell", "entry_ref": "260.88", "stop_price": "264.6608",
            "current_stop": current or "264.6608"}


class TestTrailVerdict:
    FEE = 0.00045

    def test_below_first_rung_noop(self):
        b = _trail_short()
        # +0.99R: mark 260.88 - 0.99*3.7808 = 257.137
        assert ep.trail_verdict(EVAL, b, 257.14, self.FEE) is None

    def test_rung1_breakeven_plus_fees(self):
        b = _trail_short()
        # +1.5R: mark 260.88 - 1.5*3.7808 = 255.2088
        t = ep.trail_verdict(EVAL, b, 255.21, self.FEE)
        assert t == pytest.approx(260.88 * (1 + 2 * self.FEE))

    def test_rung2_locks_1r(self):
        b = _trail_short()
        # +2.5R: mark 260.88 - 2.5*3.7808 = 251.428
        t = ep.trail_verdict(EVAL, b, 251.43, self.FEE)
        assert t == pytest.approx(260.88 - 3.7808)

    def test_long_mirror(self):
        b = {"side": "buy", "entry_ref": "100.0", "stop_price": "96.0",
             "current_stop": "96.0"}
        # +2R: mark 108
        t = ep.trail_verdict(EVAL, b, 108.0, self.FEE)
        assert t == pytest.approx(104.0)  # locks +1R above entry
        # +1R: mark 104 -> breakeven MINUS fees for a long
        t1 = ep.trail_verdict(EVAL, b, 104.0, self.FEE)
        assert t1 == pytest.approx(100.0 * (1 - 2 * self.FEE))

    def test_tighten_only(self):
        # current_stop already at 262 (< rung-2 target? no: rung2 target
        # 257.1 IS tighter) — but a current stop of 255 is tighter than
        # every rung target -> None
        b = _trail_short(current="255.0")
        assert ep.trail_verdict(EVAL, b, 251.43, self.FEE) is None

    def test_be_armed_stop_blocks_rung1(self):
        # be_armed restamped current_stop to entry; rung-1 target
        # (entry + fee buffer) is LOOSER for a short -> None
        b = _trail_short(current="260.88")
        assert ep.trail_verdict(EVAL, b, 255.21, self.FEE) is None

    def test_mark_none_abstains(self):
        assert ep.trail_verdict(EVAL, _trail_short(), None, self.FEE) is None

    def test_standard_profile_never_trails(self):
        assert ep.trail_verdict(STD, _trail_short(), 251.43,
                                self.FEE) is None

    def test_unparseable_geometry_abstains(self):
        b = {"side": "sell", "entry_ref": "x", "stop_price": "264.6608"}
        assert ep.trail_verdict(EVAL, b, 251.43, self.FEE) is None

    def test_adverse_mark_noop(self):
        # short with mark ABOVE entry: moved_r negative
        assert ep.trail_verdict(EVAL, _trail_short(), 265.0,
                                self.FEE) is None


class TestStopLocksEntry:
    def test_short_through_and_fee_hole(self):
        assert ep.stop_locks_entry("sell", 260.88, 260.88) is True
        assert ep.stop_locks_entry("sell", 257.1, 260.88) is True
        # rung-1 fee concession sits ABOVE entry on a short — fee hole
        assert ep.stop_locks_entry(
            "sell", 260.88 * (1 + 2 * 0.00045), 260.88) is False

    def test_long_mirror(self):
        assert ep.stop_locks_entry("buy", 100.0, 100.0) is True
        assert ep.stop_locks_entry("buy", 104.0, 100.0) is True
        assert ep.stop_locks_entry(
            "buy", 100.0 * (1 - 2 * 0.00045), 100.0) is False


class TestTrailBracket:
    def test_trail_moves_stop(self, tmp_path):
        ex = _exec(tmp_path)
        b = _open_short()
        pos = {"quantity": "1.0", "positionId": "p1"}
        # +2.5R -> lock +1R: target 257.0992
        assert ex.trail_bracket(b, pos, 251.43) is True
        assert b["current_stop"] == "257.09920000000003" or \
               float(b["current_stop"]) == pytest.approx(257.0992)
        assert b["be_armed"] is True  # +1R lock is through entry
        order = ex.client.placed[0]["orders"][0]
        assert order["type"] == "stop_market"
        assert order["reduceOnly"] is True
        assert float(order["triggerPrice"]) == pytest.approx(257.0992)

    def test_rung1_does_not_stamp_be_armed(self, tmp_path):
        """Fee-concession rung must not suppress the tighter uPnL BE arm."""
        ex = _exec(tmp_path)
        b = _open_short()
        pos = {"quantity": "1.0", "positionId": "p1",
               "unrealizedPnl": "5.0"}
        # +1.5R -> rung 1 only (entry + fees), not through entry
        assert ex.trail_bracket(b, pos, 255.21) is True
        assert float(b["current_stop"]) == pytest.approx(
            260.88 * (1 + 2 * ex.config.taker_fee_rate))
        assert b["be_armed"] is False
        # uPnL exact-entry arm still free to fire and tighten further
        assert ex.arm_breakeven(b, pos) is True
        assert b["be_armed"] is True
        assert b["current_stop"] == "260.88"

    def test_mark_none_noop(self, tmp_path):
        ex = _exec(tmp_path)
        b = _open_short()
        pos = {"quantity": "1.0", "positionId": "p1"}
        assert ex.trail_bracket(b, pos, None) is False
        assert ex.client.placed == []

    def test_failed_place_keeps_current_stop(self, tmp_path):
        ex = _exec(tmp_path)

        class _Boom(_FakeClient):
            def place_orders(self, body):
                raise RuntimeError("venue 5xx")

        ex.client = _Boom()
        b = _open_short()
        pos = {"quantity": "1.0", "positionId": "p1"}
        assert ex.trail_bracket(b, pos, 251.43) is False
        assert b["current_stop"] == "264.6608"


class TestEvalBigBetKantGate:
    """Live undershoot (2026-11-08): risk_for_signal sized coh>7 at
    70% of remaining ($24–$40) but place_bracket still ran legacy
    risk_cap (r_max=$7.5) and would have vetoed every big bet. Pin the
    gate open for evaluation; standard stays clamped."""

    def test_eval_accepts_seventy_pct_remaining(self, tmp_path):
        ex = _exec(tmp_path)
        # _exec rolls budget at headroom 150 → floor $40 remaining
        assert ex.budget.remaining >= 28.0
        # stop ~1.5% so qty clears min; margin on 2x BCH stays under 30%
        res = ex.place_bracket("BCH", "sell", 28.0, 260.0, 264.0)
        assert res.ok, res.reason
        assert ex.client.placed, "big bet must reach the venue"

    def test_eval_still_rejects_above_remaining(self, tmp_path):
        ex = _exec(tmp_path)
        res = ex.place_bracket("BCH", "sell", 99.0, 260.0, 264.0)
        assert not res.ok
        assert "risk $" in res.reason and "> cap $" in res.reason

    def test_standard_still_clamps_at_r_max(self, tmp_path):
        ex = _exec(tmp_path, profile="standard")
        # legacy risk_cap: min(r_base, remaining, headroom/div, r_max=7.5)
        res = ex.place_bracket("BCH", "sell", 28.0, 260.0, 264.0)
        assert not res.ok
        assert "risk $28.00 > cap $" in res.reason


# ── slot reserve / probe squat (ATS-21) ──────────────────────────────

class TestSlotReserveVerdict:
    def test_kill_switch_off_never_binds(self):
        assert ep.slot_reserve_verdict(
            EVAL, False, 5.0, open_count=2, max_positions=3) is None

    def test_standard_profile_never_binds(self):
        assert ep.slot_reserve_verdict(
            STD, True, 5.0, open_count=2, max_positions=3) is None

    def test_probe_stands_down_when_last_slot_reserved(self):
        # 2/3 open → free=1 ≤ reserve=1 → probe dies, big bet lives
        assert ep.slot_reserve_verdict(
            EVAL, True, 6.9, open_count=2, max_positions=3
        ) == ep.SLOT_RESERVE_STANDDOWN
        assert ep.slot_reserve_verdict(
            EVAL, True, 7.0, open_count=2, max_positions=3
        ) == ep.SLOT_RESERVE_STANDDOWN  # boundary = probe

    def test_big_bet_consumes_reserved_slot(self):
        assert ep.slot_reserve_verdict(
            EVAL, True, 7.5, open_count=2, max_positions=3) is None

    def test_probes_ok_while_headroom_above_reserve(self):
        # 1/3 open → free=2 > reserve=1 → probe may take a non-reserved slot
        assert ep.slot_reserve_verdict(
            EVAL, True, 5.0, open_count=1, max_positions=3) is None

    def test_unparseable_coherence_is_probe(self):
        assert ep.slot_reserve_verdict(
            EVAL, True, None, open_count=2, max_positions=3
        ) == ep.SLOT_RESERVE_STANDDOWN

    def test_reserve_zero_disables(self):
        assert ep.slot_reserve_verdict(
            EVAL, True, 5.0, open_count=2, max_positions=3,
            reserve=0) is None


class TestSlotReserveExecutor:
    def _seed_opens(self, ex: ProprExecutor, n: int) -> None:
        reg = ex._registry()
        for i in range(n):
            reg["brackets"].append({
                "semantic_id": f"seed-{i}", "status": "open",
                "asset": f"SEED{i}", "side": "buy", "family": "other",
                "quantity": "1", "risk_usd": 5.0,
                "entry_ref": "100", "stop_price": "99",
                "opened_ts": 1.0, "be_armed": False,
            })
        ex._save_registry(reg)

    def test_default_off_probe_fills_last_slot(self, tmp_path):
        # legacy bit-for-bit: kill switch False → 2/3 + probe still places
        ex = _exec(tmp_path, max_positions=3, slot_reserve_enabled=False)
        self._seed_opens(ex, 2)
        res = ex.place_bracket("BCH", "sell", 5.0, 260.0, 264.0,
                               coherence=5.0)
        assert res.ok, res.reason

    def test_probe_standdown_on_reserved_slot(self, tmp_path):
        ex = _exec(tmp_path, max_positions=3, slot_reserve_enabled=True,
                   slot_reserve_slots=1)
        self._seed_opens(ex, 2)
        res = ex.place_bracket("BCH", "sell", 5.0, 260.0, 264.0,
                               coherence=5.0)
        assert not res.ok
        assert res.reason == ep.SLOT_RESERVE_STANDDOWN

    def test_coh7_still_places_on_reserved_slot(self, tmp_path):
        ex = _exec(tmp_path, max_positions=3, slot_reserve_enabled=True,
                   slot_reserve_slots=1)
        self._seed_opens(ex, 2)
        # size small enough for 2x BCH margin at $5k equity; doctrine
        # accept = coh>7 can fire, not that risk_usd≥20 clears here
        res = ex.place_bracket("BCH", "sell", 5.0, 260.0, 264.0,
                               coherence=7.5)
        assert res.ok, res.reason
        assert ex.client.placed
