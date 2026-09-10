"""Pins for tools/day_audit.py — pure-helper behavior, no server data."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.day_audit import (  # noqa: E402
    _match_tdb, _median, _rollup, _tag_of, _wr,
)
from intelligence.plane_ledger import ledger_plane_map, plane_of  # noqa: E402


def test_wr_none_on_empty():
    assert _wr([]) is None


def test_wr_counts_net_positive_only():
    rows = [{"_net": 1.0}, {"_net": -0.5}, {"_net": 0.0}, {"_net": 2.0}]
    assert _wr(rows) == 50.0  # 0.0 is not a win


def test_median_even_and_odd():
    assert _median([1.0, 3.0, 2.0]) == 2.0
    assert _median([1.0, 3.0]) == 2.0
    assert _median([None, None]) is None


def test_plane_entry_plane_wins_over_ledger():
    t = {"entry_plane": "gated", "trade_id": "X_1"}
    assert plane_of(t, {"X_1": "fastpath"}) == "gated"


def test_plane_ledger_fallback_then_unknown():
    assert plane_of({"trade_id": "X_1"}, {"X_1": "fastpath"}) == "fastpath"
    assert plane_of({"trade_id": "X_9"}, {}) == "unknown"
    assert plane_of({}, {}) == "no_trade_db_match"


def test_match_tdb_nearest_within_tolerance():
    by_symbol = {"SOL-USD": [
        {"symbol": "SOL-USD", "timestamp_close_ms": 1000},
        {"symbol": "SOL-USD", "timestamp_close_ms": 9000},
    ]}
    hit = _match_tdb({"symbol": "SOL-USD", "closed_at_ms": 1100}, by_symbol)
    assert hit["timestamp_close_ms"] == 1000
    assert _match_tdb({"symbol": "SOL-USD", "closed_at_ms": 999999}, by_symbol) == {}
    assert _match_tdb({"symbol": "ETH-USD", "closed_at_ms": 1000}, by_symbol) == {}


def test_tag_of_symbol_and_timestamp():
    tags = [("SOL-USD", 5000, "cascade_momentum"), ("ETH-USD", 5000, "etf_tide")]
    assert _tag_of({"symbol": "ETH-USD", "closed_at_ms": 5010}, tags) == "etf_tide"
    assert _tag_of({"symbol": "SOL-USD", "closed_at_ms": 999999}, tags) == "unknown"


def test_rollup_groups_and_sorts_by_n():
    rows = [
        {"exit_reason": "a", "_net": 1.0, "_mfe": 0.2},
        {"exit_reason": "a", "_net": -1.0, "_mfe": None},
        {"exit_reason": "b", "_net": 0.5, "_mfe": 0.4},
    ]
    out = _rollup(rows, lambda r: r["exit_reason"])
    assert out[0]["cohort"] == "a" and out[0]["n"] == 2
    assert out[0]["avg_mfe_pct"] == 0.2  # None MFE excluded, not zeroed
    assert out[1]["wr_pct"] == 100.0


def test_ledger_plane_map_fallbacks():
    rows = [
        {"identity": {"attempt_id": "A_1"}, "plane": {"plane": "gated"}},
        {"identity": {"attempt_id": "A_2"}, "plane": {"entry_path_site": "post_fill"}},
        {"identity": {"attempt_id": "A_3"}, "plane": {"entry_path_site": "pre_fill"}},
        {"identity": {}, "plane": {"plane": "gated"}},
    ]
    m = ledger_plane_map(rows)
    assert m == {"A_1": "gated", "A_2": "fastpath", "A_3": "gated"}
