"""Tests for the capacity governor + mover radar + tracker extensions —
the 2026-08-23 HYPE/MUBARAK capacity bundle. Pure brains pinned without
booting main()."""
import importlib.util
import os

import pytest


def _load(name, rel):
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        *rel.split("/"))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cg = _load("capacity_governor", "intelligence/capacity_governor.py")
mr = _load("mover_radar", "intelligence/mover_radar.py")


# ── capacity_governor.evaluate_cap ───────────────────────────────────────────

def test_under_cap_passes_without_evidence():
    assert cg.evaluate_cap(count=3, cap=4, direction="long") == ("pass", "")


def test_over_cap_no_evidence_blocks():
    assert cg.evaluate_cap(count=4, cap=4, direction="long") == ("block", "cap")


def test_recovery_suppresses_every_leg():
    for kw in ({"graduated": True}, {"hugo_aligned": True},
               {"day_move_pct": 9.0}, {"mover_relief": {"direction": "long"}}):
        assert cg.evaluate_cap(count=5, cap=4, direction="long",
                               recovery_active=True, **kw) == ("block", "recovery")


def test_graduated_and_hugo_legs_exempt():
    assert cg.evaluate_cap(count=4, cap=4, direction="long",
                           graduated=True) == ("exempt", "graduated")
    assert cg.evaluate_cap(count=4, cap=4, direction="short",
                           hugo_aligned=True) == ("exempt", "hugo_aligned")


def test_day_move_leg_fires_on_aligned_move():
    # The HYPE hole: no BTC confirmation, no graduation slot — the symbol's
    # OWN day move is the evidence (Raschke/Thorp).
    assert cg.evaluate_cap(count=6, cap=4, direction="long",
                           day_move_pct=5.2) == ("exempt", "day_move_aligned")
    assert cg.evaluate_cap(count=6, cap=4, direction="short",
                           day_move_pct=-4.1) == ("exempt", "day_move_aligned")


def test_day_move_leg_rejects_counter_and_small_moves():
    assert cg.evaluate_cap(count=6, cap=4, direction="long",
                           day_move_pct=-5.0) == ("block", "cap")
    assert cg.evaluate_cap(count=6, cap=4, direction="long",
                           day_move_pct=2.9) == ("block", "cap")
    assert cg.evaluate_cap(count=6, cap=4, direction="long",
                           day_move_pct=None) == ("block", "cap")


def test_churn_signature_kills_soft_legs_not_hard_legs():
    # Steenbarger: direction alternation within the day = churn. Day-move /
    # relief / journal legs die; graduated/Hugo survive (direction-locked by
    # their own machinery).
    dirs = {"long": 3, "short": 2}
    assert cg.evaluate_cap(count=5, cap=4, direction="long",
                           day_move_pct=8.0, dirs_today=dirs) == ("block", "cap")
    assert cg.evaluate_cap(count=5, cap=4, direction="long",
                           dirs_today=dirs, graduated=True) == ("exempt", "graduated")
    assert cg.evaluate_cap(count=5, cap=4, direction="long",
                           day_move_pct=8.0,
                           dirs_today={"long": 5}) == ("exempt", "day_move_aligned")


def test_mover_relief_requires_direction_match():
    assert cg.evaluate_cap(count=4, cap=4, direction="long",
                           mover_relief={"direction": "long"}) == ("exempt", "mover_relief")
    assert cg.evaluate_cap(count=4, cap=4, direction="short",
                           mover_relief={"direction": "long"}) == ("block", "cap")
    assert cg.evaluate_cap(count=4, cap=4, direction="long",
                           mover_relief=True) == ("block", "cap")  # non-dict junk


def test_journal_evidence_leg_thresholds():
    # Aronson: no verdict on noise; relax only when the cap is mostly WRONG.
    low_acc = {"n": 12, "accuracy": 0.25}
    high_acc = {"n": 12, "accuracy": 0.60}
    thin = {"n": 4, "accuracy": 0.0}
    assert cg.evaluate_cap(count=4, cap=4, direction="long",
                           journal_verdict=low_acc) == ("exempt", "journal_evidence")
    assert cg.evaluate_cap(count=4, cap=4, direction="long",
                           journal_verdict=high_acc) == ("block", "cap")
    assert cg.evaluate_cap(count=4, cap=4, direction="long",
                           journal_verdict=thin) == ("block", "cap")
    assert cg.evaluate_cap(count=4, cap=4, direction="long",
                           journal_verdict=low_acc,
                           journal_enabled=False) == ("block", "cap")


def test_r_budget_bounds_all_legs():
    # Carver: exempted or not, a symbol cannot consume more than the day's
    # risk budget. Budget 0 = unconfigured → evidence alone decides.
    assert cg.evaluate_cap(count=4, cap=4, direction="long", graduated=True,
                           risk_consumed_usd=8.0,
                           risk_budget_usd=7.5) == ("block", "r_budget_exhausted")
    assert cg.evaluate_cap(count=4, cap=4, direction="long", graduated=True,
                           risk_consumed_usd=3.0,
                           risk_budget_usd=7.5) == ("exempt", "graduated")
    assert cg.evaluate_cap(count=4, cap=4, direction="long", graduated=True,
                           risk_consumed_usd=99.0,
                           risk_budget_usd=0.0) == ("exempt", "graduated")


def test_leg_precedence_graduated_first():
    assert cg.evaluate_cap(count=4, cap=4, direction="long",
                           graduated=True, hugo_aligned=True,
                           day_move_pct=9.0) == ("exempt", "graduated")


# ── mover_radar.evaluate ─────────────────────────────────────────────────────

def test_radar_classifies_three_pipes():
    moves = {"HYPE-USD": 41.0, "MUBARAK-USD": -22.0, "SOL-USD": 12.0,
             "BTC-USD": 2.0}
    trades = {"SOL-USD": 2}
    signals = {"HYPE-USD": 900, "SOL-USD": 40}
    out = mr.evaluate(moves, trades, signals, threshold_pct=10.0)
    bysym = {v["symbol"]: v for v in out}
    assert "BTC-USD" not in bysym                      # below threshold
    assert bysym["HYPE-USD"]["cls"] == "blocked"       # signals, no trades
    assert bysym["HYPE-USD"]["direction"] == "long"
    assert bysym["MUBARAK-USD"]["cls"] == "silent"     # nothing at all
    assert bysym["MUBARAK-USD"]["direction"] == "short"
    assert bysym["SOL-USD"]["cls"] == "participating"  # has trades
    assert out[0]["symbol"] == "HYPE-USD"              # sorted by |move| desc


def test_radar_skips_bad_rows():
    out = mr.evaluate({"A-USD": "garbage", "B-USD": None, "C-USD": 15.0},
                      {}, {"C-USD": 3}, threshold_pct=10.0)
    assert [v["symbol"] for v in out] == ["C-USD"]


# ── DailyTradeTracker extensions ─────────────────────────────────────────────

def test_tracker_symbol_dirs_and_risk(tmp_path, monkeypatch):
    from core.clock import DailyTradeTracker, ExchangeClock
    monkeypatch.setattr(DailyTradeTracker, "_PERSIST_PATH",
                        str(tmp_path / "daily_trades.json"))
    t = DailyTradeTracker(ExchangeClock())
    t.record_open("HYPE-USD", "long", risk_usd=1.25)
    t.record_open("HYPE-USD", "long", risk_usd=0.75)
    t.record_open("ETH-USD", "short")   # no risk → not booked
    assert t.symbol_trades_today("HYPE-USD") == 2
    assert t.symbol_directions_today("HYPE-USD") == {"long": 2}
    assert t.symbol_directions_today("ETH-USD") == {"short": 1}
    assert t.symbol_risk_today("HYPE-USD") == 2.0
    assert t.symbol_risk_today("ETH-USD") == 0.0


def test_tracker_migrates_legacy_persisted_day(tmp_path, monkeypatch):
    import json
    from core.clock import DailyTradeTracker, ExchangeClock
    p = tmp_path / "daily_trades.json"
    clock = ExchangeClock()
    p.write_text(json.dumps({clock.now_date_str(): {
        "count": 4, "pnl_usd": 0.5, "symbols": {"HYPE-USD": 4},
        "directions": {"long": 4}}}))   # pre-governor file: no new keys
    monkeypatch.setattr(DailyTradeTracker, "_PERSIST_PATH", str(p))
    t = DailyTradeTracker(clock)
    assert t.symbol_trades_today("HYPE-USD") == 4      # legacy data intact
    assert t.symbol_directions_today("HYPE-USD") == {}  # empty, not KeyError
    t.record_open("HYPE-USD", "long", risk_usd=1.0)     # new keys materialize
    assert t.symbol_directions_today("HYPE-USD") == {"long": 1}
    assert t.symbol_risk_today("HYPE-USD") == 1.0


# ── shadow_journal gate-symbol verdict ───────────────────────────────────────

def test_gate_symbol_verdict_accuracy_and_min_n():
    from intelligence.shadow_journal import ShadowJournal
    j = ShadowJournal()
    rec = lambda won: {"gate": "daily_cap", "symbol": "HYPE-USD",
                       "won_24h": won, "ts": 1.0}
    j._scored = [rec(True)] * 8 + [rec(False)] * 4
    v = j.gate_symbol_verdict("daily_cap", "HYPE-USD", min_n=10)
    assert v["n"] == 12 and v["would_profit"] == 8
    assert v["accuracy"] == pytest.approx(1 - 8 / 12, abs=1e-3)
    # Below min_n → no verdict (Aronson: not on noise).
    assert j.gate_symbol_verdict("daily_cap", "HYPE-USD", min_n=13) is None
    # Other gate/symbol isolation.
    assert j.gate_symbol_verdict("dispersion", "HYPE-USD", min_n=1) is None
    assert j.gate_symbol_verdict("daily_cap", "ETH-USD", min_n=1) is None


def test_daily_cap_registered_as_rejection_event():
    from intelligence.shadow_journal import REJECTION_EVENTS
    assert REJECTION_EVENTS["daily_trade_cap_reached"] == "daily_cap"
