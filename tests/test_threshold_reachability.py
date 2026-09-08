"""D25 threshold-reachability pins (CEO commission 2026-09-08).

Pure-bin math + incremental scanner behavior on a synthetic log:
offset advance, no double-count on rerun, torn tail left behind,
exact event-string matching (D24 lesson), fire/reach dates.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import daily_digest as dd  # noqa: E402


def _state(n=0):
    return {"bins": [], "n": n, "max": None, "min": None}


def test_bin_add_and_quantile():
    s = _state()
    for v in (1.0, 1.2, 3.0, 9.05, -0.33):
        dd._reach_bin_add(s, 0.5, -30.0, v)
    assert s["n"] == 5
    assert s["max"] == 9.05
    assert s["min"] == -0.33
    # median of {−0.33, 1.0, 1.2, 3.0, 9.05} = 1.2 → bin [1.0,1.5) midpoint 1.25
    assert dd._reach_quantile(s, 0.5, -30.0, 0.50) == 1.25
    # p99 → top bin [9.0,9.5) midpoint 9.25
    assert dd._reach_quantile(s, 0.5, -30.0, 0.99) == 9.25


def test_quantile_empty():
    assert dd._reach_quantile(_state(), 0.5, -30.0, 0.5) is None


def test_field_extractors():
    line = '{"coherence": 5.6341, "events_60s": 42, "timestamp": "2026-09-08T01:02:03Z"}'
    assert dd._reach_field_float(line, "coherence") == 5.6341
    assert dd._reach_field_float(line, "events_60s") == 42.0
    assert dd._reach_field_float(line, "missing") is None
    assert dd._reach_field_str(line, "timestamp") == "2026-09-08T01:02:03Z"
    assert dd._reach_field_str(line, "missing") == ""


def test_days_since():
    assert dd._reach_days_since("2026-08-21T10:00:00Z", "2026-09-08") == 18
    assert dd._reach_days_since("", "2026-09-08") is None


LINES = [
    {"event": "treasury_heartbeat", "book_roe": -0.33,
     "clusters": {"crypto_beta": {"roe": -0.33, "peak": 0.0, "tp1": 15.0,
                                  "tp2": 25.0}},
     "timestamp": "2026-09-08T05:00:35Z"},
    {"event": "treasury_order_firing", "reason": "treasury_tp1",
     "timestamp": "2026-08-21T09:00:00Z"},
    {"event": "signal_ready", "score": 6.1, "timestamp": "2026-09-08T04:00:00Z"},
    {"event": "signal_ready", "score": 3.2, "timestamp": "2026-09-08T04:05:00Z"},
    {"event": "quant_filter_blocked", "reason": "quiet_market_pause",
     "events_60s": 4, "timestamp": "2026-09-08T03:00:00Z"},
    {"event": "roe_ratchet_atr_floor_suppressed", "legacy_stop": 99.0,
     "floored_stop": 98.5, "mark": 100.0, "atr": 0.5,
     "timestamp": "2026-09-08T02:00:00Z"},
    # decoy: substring lookalike inside another event must NOT count (D24)
    {"event": "signal_ready", "coherence": 4.0,
     "note": "not a treasury_heartbeat", "timestamp": "2026-09-08T04:06:00Z"},
]


def _write_log(tmp_path, lines, torn=False):
    p = tmp_path / "aria.log"
    with open(p, "w") as f:
        for row in lines:
            f.write(json.dumps(row) + "\n")
        if torn:
            f.write('{"event": "signal_ready", "coherence": 9.9')  # no newline
    return str(p)


def test_scanner_incremental_and_exact(tmp_path):
    log = _write_log(tmp_path, LINES, torn=True)
    state = str(tmp_path / "state.json")
    st = dd.threshold_reachability_update(log, state)
    # cluster roe: 1 obs (−0.33); coherence: 2 obs (decoy's "coherence" key
    # is ignored — signal_ready carries score, not coherence)
    assert st["series"]["cluster_roe"]["n"] == 1
    assert st["series"]["coherence"]["n"] == 2
    assert st["series"]["coherence"]["max"] == 6.1  # torn 9.9 NOT consumed
    # reach counts: recovery floor 5.6 → exactly 1 (6.1); treasury tp1 → 0
    assert st["ge_counts"]["recovery_coherence_floor"] == 1
    assert "treasury_tp1" not in st["ge_counts"]
    # ratchet: |100−99|/0.5 = 2.0 ATR ≥ 1.0 → reach counted
    assert st["ge_counts"]["roe_ratchet_atr_floor"] == 1
    assert st["fires"]["treasury_tp1"] == "2026-08-21T09:00:00Z"
    assert st["fires"]["suppressed"] == "2026-09-08T02:00:00Z"
    # rerun: offset at EOF-minus-torn → nothing double-counted
    st2 = dd.threshold_reachability_update(log, state)
    assert st2["series"]["coherence"]["n"] == 2
    assert st2["ge_counts"]["recovery_coherence_floor"] == 1


def test_build_rows_verdict(tmp_path):
    log = _write_log(tmp_path, LINES)
    state = str(tmp_path / "state.json")
    rows = dd.build_threshold_reachability("2026-09-08", log, state)
    by_name = {r["name"]: r for r in rows if "name" in r}
    tp1 = by_name["treasury_tp1"]
    assert tp1["n_obs"] == 1
    assert tp1["frac_ge_threshold"] == 0.0
    assert tp1["days_since_last_fire"] == 18
    # n<1000 → no CONSTANT verdict yet (honest sample-size bar)
    assert "verdict" not in tp1
    rec = by_name["recovery_coherence_floor"]
    assert rec["frac_ge_threshold"] == 0.5
    assert rec["days_since_last_reach"] == 0
