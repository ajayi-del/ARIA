"""Gate realistic fill (merged-schema Phase 0, 2026-09-13): offline realistic
estimand beside the D10 mark. Pins on the pure estimand math — fee schedule
(SCHEDULE doctrine, never pnl-diff), haircut direction, exit-stack precedence
(stop outranks TP inside one bar — fail-pessimistic), conviction-decay grace,
short-side symmetry, coverage accounting, and the cross-tool D10 oracle
(my d10_arm must equal gate_economics' own rollup on the same records)."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.gate_realistic_fill as grf  # noqa: E402
from tools.gate_economics import gate_rollup  # noqa: E402


# ── Fee schedule (DIR#53 mirror) ─────────────────────────────────────────────

def test_fee_side_schedule_values():
    assert grf.fee_side("sodex") == pytest.approx(0.00038)
    assert grf.fee_side("aster") == pytest.approx(0.0004)
    assert grf.fee_side("aster_tradfi") == pytest.approx(0.00009)


def test_fee_side_unknown_defaults_sodex():
    assert grf.fee_side("binance") == grf.fee_side("sodex")


# ── Fill haircuts ─────────────────────────────────────────────────────────────

def test_entry_fill_long_pays_up():
    f = grf.entry_fill(100.0, "long", 0.001, 0.001, 0.00038)
    assert f == pytest.approx(100.0 * 1.00238)


def test_entry_fill_short_receives_less():
    f = grf.entry_fill(100.0, "short", 0.001, 0.001, 0.00038)
    assert f == pytest.approx(100.0 * (1.0 - 0.00238))


def test_exit_fill_crosses_back():
    assert grf.exit_fill(100.0, "long", 0.001, 0.00038) == \
        pytest.approx(100.0 * (1.0 - 0.00138))
    assert grf.exit_fill(100.0, "short", 0.001, 0.00038) == \
        pytest.approx(100.0 * (1.0 + 0.00138))


def test_make_tp_rr_ladder_both_sides():
    assert grf.make_tp(100.0, 99.0, "long", rr=2.0) == pytest.approx(102.0)
    assert grf.make_tp(100.0, 101.0, "short", rr=2.0) == pytest.approx(98.0)


# ── Exit-stack replay ─────────────────────────────────────────────────────────

TS = 1_000_000.0


def _bar(dt_s, o, h, l, c):
    return [TS + dt_s, o, h, l, c]


def test_stop_outranks_tp_within_one_bar():
    # long, both stop (99) and tp (102) inside the same bar's range -> stop
    path = [_bar(60, 100.0, 103.0, 98.0, 101.0)]
    sim = grf.simulate_exit(path, TS, "long", 100.0, 99.0, 102.0)
    assert sim["reason"] == "software_stop"
    assert sim["price"] == 99.0


def test_short_stop_outranks_tp_within_one_bar():
    path = [_bar(60, 100.0, 102.0, 97.0, 99.0)]
    sim = grf.simulate_exit(path, TS, "short", 100.0, 101.0, 98.0)
    assert sim["reason"] == "software_stop"
    assert sim["price"] == 101.0


def test_tp_fires_when_stop_untouched():
    path = [_bar(60, 100.0, 102.5, 99.5, 102.0)]
    sim = grf.simulate_exit(path, TS, "long", 100.0, 99.0, 102.0)
    assert sim["reason"] == "software_tp"
    assert sim["price"] == 102.0


def test_conviction_decay_only_when_bleeding_at_grace():
    # drifts -0.5% past grace without touching the stop -> decay at close
    path = [_bar(60, 100.0, 100.1, 99.6, 99.6),
            _bar(grf.GRACE_S + 60, 99.6, 99.7, 99.4, 99.5)]
    sim = grf.simulate_exit(path, TS, "long", 100.0, 99.0, 102.0)
    assert sim["reason"] == "conviction_decay"
    assert sim["price"] == 99.5


def test_no_decay_before_grace_even_when_bleeding():
    path = [_bar(60, 100.0, 100.1, 99.4, 99.5)]
    sim = grf.simulate_exit(path, TS, "long", 100.0, 99.0, 102.0)
    assert sim["reason"] == "censored"  # tape ends, decay never armed


def test_no_decay_when_unrealized_inside_band():
    # -0.2% at grace is inside the 0.4% bleed band -> hold to horizon
    path = [_bar(grf.GRACE_S + 60, 100.0, 100.1, 99.7, 99.8),
            _bar(grf.HORIZON_S + 60, 99.8, 99.9, 99.6, 99.7)]
    sim = grf.simulate_exit(path, TS, "long", 100.0, 99.0, 102.0)
    assert sim["reason"] == "horizon"


def test_horizon_exit_at_close():
    path = [_bar(60, 100.0, 100.2, 99.9, 100.1),
            _bar(grf.HORIZON_S + 120, 100.1, 100.3, 100.0, 100.2)]
    sim = grf.simulate_exit(path, TS, "long", 100.0, 99.0, 102.0)
    assert sim["reason"] == "horizon"
    assert sim["price"] == 100.2
    assert sim["hold_s"] == pytest.approx(grf.HORIZON_S + 120)


def test_empty_path_is_no_tape():
    sim = grf.simulate_exit([], TS, "long", 100.0, 99.0, 102.0)
    assert sim["reason"] == "no_tape"
    assert sim["hold_s"] == 0.0


# ── PnL + gate-net sign conventions ──────────────────────────────────────────

def test_sim_pnl_pct_sides():
    assert grf.sim_pnl_pct(100.0, 101.0, "long") == pytest.approx(1.0)
    assert grf.sim_pnl_pct(100.0, 101.0, "short") == pytest.approx(-1.0)


def test_gate_net_sign_convention():
    # refused trades that would have LOST -> gate earns (positive net)
    assert grf.gate_net([-1.0, -2.0]) == 3.0
    # refused trades that would have WON -> gate costs (negative net)
    assert grf.gate_net([6.0]) == -6.0
    assert grf.gate_net([]) == 0.0


def test_haircut_widens_a_stop_loss():
    # raw stop distance is -1.0%; realistic fills make it worse both sides
    e_in = grf.entry_fill(100.0, "long", 0.001, 0.0005, 0.00038)
    e_out = grf.exit_fill(99.0, "long", 0.001, 0.00038)
    p = grf.sim_pnl_pct(e_in, e_out, "long")
    assert p < -1.0


# ── quantile + tape_class ─────────────────────────────────────────────────────

def test_quantile_empirical():
    # nearest-rank: i = round(q * (n-1))
    assert grf.quantile([10, 20, 30, 40], 0.5) == 30
    assert grf.quantile([10, 20, 30, 40], 0.25) == 20
    assert grf.quantile([10, 20, 30, 40], 0.0) == 10
    assert grf.quantile([], 0.5) is None


def test_tape_class_abstain_paths():
    bybit_map = {"BTC-USD": "BTCUSDT"}
    tradfi = {"TSM-USD"}
    assert grf.tape_class("BTC-USD", bybit_map, tradfi) == "bybit"
    assert grf.tape_class("TSM-USD", bybit_map, tradfi) == "tradfi"
    assert grf.tape_class("DEFISSI", bybit_map, tradfi) == "skip"
    assert grf.tape_class("NOWHERE-USD", bybit_map, tradfi) == "unknown"


# ── Cross-tool D10 oracle (the join-fidelity pin) ─────────────────────────────

def _rec(gate="g", pnl=None, stopped=False, symbol="BTC-USD", direction="long",
         entry=100.0, hyp_stop=99.0, ts=TS):
    return {"gate": gate, "pnl_24h": pnl, "stopped": stopped,
            "symbol": symbol, "direction": direction, "ts": ts,
            "entry": entry, "hyp_stop": hyp_stop}


def test_d10_arm_matches_gate_economics_rollup():
    recs = [_rec(pnl=-1.0), _rec(pnl=-2.0), _rec(pnl=6.0),
            _rec(pnl=5.0, stopped=True), _rec(pnl=None)]
    mine = grf.d10_arm(recs)["g"]
    theirs = gate_rollup(recs)[0]["net_value_pct"]
    assert mine == theirs


def test_d10_arm_stopped_marked_at_stop():
    recs = [_rec(pnl=-4.0, stopped=True)]
    assert grf.d10_arm(recs)["g"] == 1.0


# ── sim_arm coverage accounting (fetch layer stubbed) ────────────────────────

def _ctx():
    return {"kyle": {}, "cs": {}, "notional": {},
            "kyle_default": 0.0, "cs_default": 0.0, "notional_default": 100.0,
            "holds": []}


def test_sim_arm_scores_bybit_and_counts_abstains(monkeypatch):
    # tape: immediate stop touch for the long (entry 100, stop 99)
    monkeypatch.setattr(grf, "_path_for",
                        lambda *a, **k: [_bar(60, 100.0, 100.2, 98.5, 99.0)])
    recs = [
        _rec(gate="dispersion"),                        # bybit -> scored
        _rec(gate="dispersion", symbol="TSM-USD"),      # tradfi -> abstain
        _rec(gate="dispersion", symbol="NOWHERE-USD"),  # unknown -> abstain
    ]
    nets, cov, details = grf.sim_arm(
        recs, _ctx(), set(), {"BTC-USD": "BTCUSDT"}, {"TSM-USD"}, 1800.0)
    assert cov["dispersion"] == [1, 2]
    assert nets["dispersion"] > 0  # a stopped long = a saved loser
    assert len(details) == 1
    assert details[0]["sim_reason"] == "software_stop"


def test_sim_arm_no_tape_abstains(monkeypatch):
    monkeypatch.setattr(grf, "_path_for", lambda *a, **k: [])
    recs = [_rec(gate="quant_filter")]
    nets, cov, details = grf.sim_arm(
        recs, _ctx(), set(), {"BTC-USD": "BTCUSDT"}, set(), 1800.0)
    assert cov["quant_filter"] == [0, 1]
    assert "quant_filter" not in nets
    assert details == []


def test_sim_arm_unparseable_record_abstains():
    recs = [_rec(gate="g", entry=0.0)]
    nets, cov, _ = grf.sim_arm(
        recs, _ctx(), set(), {"BTC-USD": "BTCUSDT"}, set(), 1800.0)
    assert cov["g"] == [0, 1]


def test_sim_arm_detail_carries_haircut_bps(monkeypatch):
    monkeypatch.setattr(grf, "_path_for",
                        lambda *a, **k: [_bar(60, 100.0, 102.5, 99.5, 102.0)])
    ctx = _ctx()
    ctx["cs_default"] = 0.0002
    ctx["kyle_default"] = 0.000001
    recs = [_rec(gate="g")]
    _, _, details = grf.sim_arm(
        recs, ctx, set(), {"BTC-USD": "BTCUSDT"}, set(), 1800.0)
    d = details[0]
    assert d["sim_reason"] == "software_tp"
    assert d["haircut_bps"] > 0
    # winner with haircut earns less than the raw +2.0% TP mark
    assert d["sim_pnl_pct"] < 2.0


# ── I/O shell ─────────────────────────────────────────────────────────────────

def test_load_jsonl_one_bad_line(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"a": 1}\nnot json\n{"b": 2}\n\n')
    out = grf._load_jsonl(str(p))
    assert out == [{"a": 1}, {"b": 2}]


def test_load_jsonl_missing_file():
    assert grf._load_jsonl("/nonexistent/nope.jsonl") == []


def test_atomic_write_roundtrip(tmp_path):
    p = str(tmp_path / "out.json")
    grf._atomic_write(p, json.dumps({"ok": True}))
    with open(p) as f:
        assert json.load(f) == {"ok": True}


def test_fidelity_skipped_when_oracle_missing(monkeypatch):
    monkeypatch.setattr(grf, "GE_ALL_PATH", "/nonexistent/ge_all.json")
    out = grf.fidelity_vs_gate_economics({"g": 1.0})
    assert out["status"] == "skipped"


def test_fidelity_pass_and_fail(monkeypatch, tmp_path):
    oracle = {"gates": [{"gate": "dispersion", "net_value_pct": 10.0},
                        {"gate": "quant_filter", "net_value_pct": -5.0}]}
    p = tmp_path / "ge_all.json"
    p.write_text(json.dumps(oracle))
    monkeypatch.setattr(grf, "GE_ALL_PATH", str(p))
    ok = grf.fidelity_vs_gate_economics(
        {"dispersion": 10.1, "quant_filter": -5.0})
    assert ok["status"] == "pass"
    assert ok["max_abs_diff"] == 0.1
    bad = grf.fidelity_vs_gate_economics(
        {"dispersion": 11.0, "quant_filter": -5.0})
    assert bad["status"] == "fail"


# ── DIR|CONVICTION-DECAY-PREFIX (A5) ─────────────────────────────────────────

def test_reason_bucket_prefix_match():
    # production stamps conviction_decay:<subreason> — prefix maps to the leg
    assert grf.reason_bucket("conviction_decay:signal_absent") == \
        "conviction_decay"
    assert grf.reason_bucket("conviction_decay:signal_abandoned") == \
        "conviction_decay"
    # every other mapping stays exact
    assert grf.reason_bucket("software_stop") == "software_stop"
    assert grf.reason_bucket("software_tp") == "software_tp"
    assert grf.reason_bucket("conviction_decay") == "conviction_decay"
    assert grf.reason_bucket("exchange_close") == "other"
    assert grf.reason_bucket("portfolio_loss_cut") == "other"
    assert grf.reason_bucket("conviction") == "other"  # near-miss, not prefix
    assert grf.reason_bucket("conviction_decayed") == "other"  # colon-bounded stem, no misroute


# ── ORACLE-2 coverage + ORACLE-3 P&L fidelity (A6/A4, stubbed tape) ──────────

def _trade_row(symbol="BTC-USD", side="long", entry=100.0, stop=99.0,
               tp=102.0, exit_px=99.0, notional=100.0, net=-1.5,
               reason="software_stop", t0_ms=2_000_000_000):
    return {"symbol": symbol, "side": side,
            "timestamp_open_ms": t0_ms, "timestamp_close_ms": t0_ms + 60_000,
            "entry_price": entry, "stop_price": stop, "tp1_price": tp,
            "exit_price": exit_px, "notional_usd": notional,
            "net_pnl": net, "exit_reason": reason}


def _stub_oracle_io(monkeypatch, rows, path):
    monkeypatch.setattr(grf, "_load_jsonl",
                        lambda p: rows if p == grf.TRADE_DB_PATH else [])
    monkeypatch.setattr(grf, "_path_for", lambda *a, **k: path)


def test_oracle2_coverage_and_match_on_covered(monkeypatch):
    # tape: immediate stop touch -> sim always says software_stop
    stop_path = [_bar(60, 100.0, 100.2, 98.5, 99.0)]
    rows = [
        _trade_row(reason="software_stop"),                    # covered, match
        _trade_row(reason="conviction_decay:signal_absent"),   # covered (A5)
        _trade_row(reason="exchange_close"),                   # uncovered
    ]
    _stub_oracle_io(monkeypatch, rows, stop_path)
    out = grf.backtest_actual_fills(_ctx(), set(), {"BTC-USD": "BTCUSDT"},
                                    set(), 0.0, 1800.0)
    assert out["n"] == 3
    assert out["n_covered"] == 2
    assert out["reason_coverage"] == pytest.approx(round(2 / 3, 3))
    # only the software_stop row matches the sim's reason on covered rows
    assert out["reason_match_rate_on_covered"] == pytest.approx(0.5)
    # the prefixed row lands in the conviction_decay leg, not "other"
    assert out["mix"]["conviction_decay"]["n"] == 1
    assert out["mix"]["other"]["n"] == 1
    # legacy field preserved
    assert "reason_match_rate" in out


def test_oracle3_pnl_fidelity_bias_and_sign(monkeypatch):
    stop_path = [_bar(60, 100.0, 100.2, 98.5, 99.0)]  # long stopped at 99
    win_path = [_bar(60, 100.0, 102.5, 99.5, 102.0)]  # long TP at 102
    paths = iter([stop_path, win_path, stop_path, stop_path])
    rows = [
        # sim stop, realized -1.5pt -> signs agree
        _trade_row(reason="software_stop", net=-1.5),
        # sim tp, realized +1.0pt -> signs agree
        _trade_row(reason="software_tp", exit_px=102.0, net=+1.0),
        # dust (notional < $10) -> stripped from the population
        _trade_row(reason="software_stop", notional=5.0, net=-0.5),
        # sim stop, realized +0.5pt -> sign disagreement
        _trade_row(reason="exchange_close", net=+0.5),
    ]
    monkeypatch.setattr(grf, "_load_jsonl",
                        lambda p: rows if p == grf.TRADE_DB_PATH else [])
    monkeypatch.setattr(grf, "_path_for", lambda *a, **k: next(paths))
    out = grf.pnl_fidelity_actual_fills(_ctx(), set(), {"BTC-USD": "BTCUSDT"},
                                        set(), 0.0, 1800.0)
    # _ctx() has zero spread/impact; only the sodex taker fee haircut applies
    fee = grf.fee_side("sodex")
    e_in = 100.0 * (1.0 + fee)
    stop_sim = (99.0 * (1.0 - fee) / e_in - 1.0) * 100.0
    tp_sim = (102.0 * (1.0 - fee) / e_in - 1.0) * 100.0
    sim_total = stop_sim + tp_sim + stop_sim
    real_total = -1.5 + 1.0 + 0.5
    assert out["n"] == 3                      # dust row stripped
    assert out["sim_pnl_total_pct"] == pytest.approx(round(sim_total, 1))
    assert out["realized_pnl_total_pct"] == pytest.approx(round(real_total, 1))
    assert out["bias_pct_points"] == pytest.approx(round(sim_total - real_total, 1))
    assert out["bias_bp_per_row"] == pytest.approx(
        round((sim_total - real_total) / 3 * 100.0, 2))
    assert out["sign_agreement"] == pytest.approx(round(2 / 3, 3))
    assert out["per_class"]["software_stop"] == {
        "n": 1, "sim": round(stop_sim, 1), "realized": -1.5,
        "bias": round(stop_sim + 1.5, 1)}
    assert out["per_class"]["exchange_close"]["n"] == 1
    assert out["caveat"] == "validated on ADMITTED, applied to REFUSED"
