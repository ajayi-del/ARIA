"""Gate economics tool (2026-08-29): watchdog 3d/7d gate-value spreadsheets.
Pins on the pure helpers — verdict classification, rollup math, and the
recalibration evidence bar (n≥30 both windows, net<0 both, ≥2× tail asymmetry)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.gate_economics import (  # noqa: E402
    ascii_table, gate_rollup, missed_cohorts, policy_pnl, recalibration_flags,
    verdict_of,
)


def _rec(gate="g", pnl=None, stopped=False, symbol="X-USD", direction="long",
         entry=100.0, hyp_stop=99.0):
    return {"gate": gate, "pnl_24h": pnl, "stopped": stopped,
            "symbol": symbol, "direction": direction, "ts": 1.0,
            "entry": entry, "hyp_stop": hyp_stop}


def test_verdict_stopped_is_saved():
    assert verdict_of(_rec(pnl=5.0, stopped=True)) == "saved_loser"


def test_d10_stopped_marked_at_stop_not_24h():
    # stopped, then price kept falling: credit is the stop distance, never
    # the free-running 24h mark (pre-D10 this credited +4.0%).
    r = _rec(pnl=-4.0, stopped=True)
    assert policy_pnl(r) == -1.0
    rows = gate_rollup([r])
    assert rows[0]["losses_avoided_pct"] == 1.0


def test_d10_stopped_then_recovered_not_debited():
    # stopped, then recovered by 24h: the trade was dead at the stop — the
    # recovery is not the gate's cost (pre-D10 avoided went NEGATIVE here).
    r = _rec(pnl=5.0, stopped=True)
    assert verdict_of(r) == "saved_loser"
    rows = gate_rollup([r])
    assert rows[0]["losses_avoided_pct"] == 1.0
    assert rows[0]["stopped_then_recovered"] == 1


def test_d10_short_stop_sign():
    assert policy_pnl(_rec(stopped=True, direction="short", hyp_stop=101.0)) == -1.0


def test_d10_unmarkable_stopped_is_unscored():
    assert verdict_of(_rec(pnl=-4.0, stopped=True, entry=0.0)) == "unscored"


def test_verdict_pnl_signs():
    assert verdict_of(_rec(pnl=-0.5)) == "saved_loser"
    assert verdict_of(_rec(pnl=0.5)) == "missed_winner"
    assert verdict_of(_rec(pnl=0.0)) == "scratch"
    assert verdict_of(_rec(pnl=None)) == "unscored"


def test_rollup_net_and_accuracy():
    recs = [_rec(pnl=-1.0), _rec(pnl=-2.0), _rec(pnl=6.0)]
    rows = gate_rollup(recs)
    assert len(rows) == 1
    r = rows[0]
    assert r["n_refused"] == 3
    assert r["saved_losers"] == 2 and r["missed_winners"] == 1
    assert r["accuracy_pct"] == 66.7
    assert r["losses_avoided_pct"] == 3.0
    assert r["gains_missed_pct"] == 6.0
    assert r["net_value_pct"] == -3.0


def test_rollup_sorted_by_n_desc():
    recs = [_rec(gate="a")] * 2 + [_rec(gate="b")] * 5
    rows = gate_rollup(recs)
    assert [r["gate"] for r in rows] == ["b", "a"]


def test_rollup_big_missed_detail():
    recs = [_rec(pnl=8.4, symbol="VELVET-USD", direction="long")]
    r = gate_rollup(recs)[0]
    assert r["big_missed_gt2pct"] == 1
    assert "VELVET-USD long +8.4%" in r["big_missed_detail"]


def _rows(gate, n, net, acc=85.0, avg_missed=6.0, avg_avoided=0.3):
    return [{"gate": gate, "n_refused": n, "net_value_pct": net,
             "accuracy_pct": acc, "avg_missed_per_winner": avg_missed,
             "avg_avoided_per_loser": avg_avoided}]


def test_flag_requires_both_windows_negative():
    r3 = _rows("recovery_skip", 40, -50.0)
    r7 = _rows("recovery_skip", 60, +10.0)
    assert recalibration_flags(r3, r7) == []


def test_flag_fires_on_two_negative_windows_with_asymmetry():
    r3 = _rows("recovery_skip", 40, -50.0)
    r7 = _rows("recovery_skip", 60, -200.0)
    flags = recalibration_flags(r3, r7)
    assert len(flags) == 1 and flags[0]["flag"] == "recalibrate_candidate"
    assert flags[0]["gate"] == "recovery_skip"


def test_flag_needs_min_n():
    r3 = _rows("tiny", 5, -50.0)
    r7 = _rows("tiny", 10, -200.0)
    assert recalibration_flags(r3, r7) == []


def test_flag_needs_tail_asymmetry():
    r3 = _rows("flat", 40, -50.0, avg_missed=0.5, avg_avoided=0.4)
    r7 = _rows("flat", 60, -200.0, avg_missed=0.5, avg_avoided=0.4)
    assert recalibration_flags(r3, r7) == []


def test_disable_candidate_on_accuracy_collapse():
    r3 = _rows("broken", 40, -10.0, acc=40.0)
    r7 = _rows("broken", 60, -100.0, acc=40.0)
    flags = recalibration_flags(r3, r7)
    assert flags[0]["flag"] == "disable_candidate"


def test_missed_cohorts_maps_the_tail():
    recs = [
        {**_rec(gate="recovery_skip", pnl=8.4, symbol="VELVET-USD"),
         "day_type": "trend", "session": "us"},
        {**_rec(gate="recovery_skip", pnl=4.6, symbol="TRUMP-USD"),
         "day_type": "trend", "session": "us"},
        {**_rec(gate="dispersion", pnl=2.0, symbol="X-USD"),
         "day_type": "range", "session": "asia"},
        {**_rec(gate="recovery_skip", pnl=-1.0)},  # saved — excluded
    ]
    out = missed_cohorts(recs)
    top_dt = out["by_day_type"][0]
    assert top_dt["cohort"] == "recovery_skip × trend"
    assert top_dt["n"] == 2 and top_dt["missed_pct"] == 13.0
    assert out["by_session"][0]["cohort"] == "recovery_skip × us"
    assert all("−" not in c["cohort"] for c in out["by_direction"])


def test_ascii_table_renders_stack_line():
    rows = gate_rollup([_rec(pnl=-1.0), _rec(pnl=2.0)])
    out = ascii_table(rows, "3d")
    assert "Stack:" in out and "net -1.0%" in out
