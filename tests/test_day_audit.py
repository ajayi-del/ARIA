"""Pins for tools/day_audit.py — pure-helper behavior, no server data."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.day_audit import (  # noqa: E402
    _match_tdb, _median, _rollup, _tag_of, _wr, source_split_nets,
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


def test_source_split_prefers_journal_when_present():
    # CEO directive pin — the 10-24 numbers exactly.
    split = source_split_nets(
        [{"closed_at_ms": 1, "pnl_net_usd": 6.3127}],
        [{"closed_at_ms": 1, "pnl_net_usd": -5.9409}],
    )
    assert split["net_usd_outcomes"] == 6.3127
    assert split["net_usd_journal"] == -5.9409
    assert split["pnl_source"] == "journal"
    assert split["net_usd_canonical"] == -5.9409
    assert split["source_disagreement_usd"] == round(-5.9409 - 6.3127, 4)


def test_source_split_outcomes_fallback_and_empty():
    split = source_split_nets([{"closed_at_ms": 1, "pnl_net_usd": 2.0}], [])
    assert split["pnl_source"] == "outcomes"
    assert split["net_usd_canonical"] == 2.0
    assert split["source_disagreement_usd"] is None
    empty = source_split_nets([], [])
    assert empty["net_usd_canonical"] == 0.0
    assert empty["pnl_source"] == "outcomes"


def test_source_split_skips_records_without_closed_at_ms():
    split = source_split_nets([], [{"pnl_net_usd": 9.9}])  # no closed_at_ms
    assert split["pnl_source"] == "outcomes"  # journal has no CLOSED records
    assert split["n_journal"] == 0
    assert split["net_usd_canonical"] == 0.0
