"""Position-snapshot pins — the geometry-only bridge contract (task #8)."""

from __future__ import annotations

import pytest

from prop_harness.main import build_positions_snapshot


def _bracket(asset="BTC", side="buy", entry="100000.0", stop="99500.0",
             qty="0.01", risk=5.0, opened=1_800_000_000.0,
             be_armed=False) -> dict:
    return {
        "semantic_id": "acct1_BTC_1", "group_id": "g1", "asset": asset,
        "side": side, "quantity": qty, "risk_usd": risk,
        "entry_ref": entry, "stop_price": stop,
        "order_ids": [1, 2], "intents": ["i1"],
        "opened_ts": opened, "absence_count": 0, "status": "open",
        "be_armed": be_armed,
    }


class TestBuildPositionsSnapshot:
    def test_shape_and_identity(self):
        snap = build_positions_snapshot([_bracket()], "urn:prp-account:x",
                                        "crypto", now=1234.5)
        assert snap["ts"] == 1234.5
        assert snap["account_id"] == "urn:prp-account:x"
        assert snap["sleeve"] == "crypto"
        assert len(snap["positions"]) == 1

    def test_geometry_fields_float_coerced(self):
        # registry stores price/quantity as Decimal strings — the bridge
        # contract serves plain floats; contract adds be_armed + r_moved
        snap = build_positions_snapshot([_bracket()], "acct", "all", now=0.0)
        pos = snap["positions"][0]
        assert pos == {
            "asset": "BTC", "side": "buy",
            "entry_ref": 100000.0, "stop_price": 99500.0,
            "quantity": 0.01, "risk_usd": 5.0,
            "opened_ts": 1_800_000_000.0,
            "be_armed": False, "r_moved": None,
            "coherence_at_entry": None,
        }

    def test_be_armed_and_r_moved_from_mark(self):
        # long entry 100 stop 99 → 1R=1; mark 101.5 → +1.5R
        snap = build_positions_snapshot(
            [_bracket(side="buy", entry="100.0", stop="99.0",
                      be_armed=True)],
            "acct", "crypto", now=1.0,
            mark_fn=lambda a: 101.5)
        pos = snap["positions"][0]
        assert pos["be_armed"] is True
        assert pos["r_moved"] == pytest.approx(1.5)

    def test_coherence_at_entry_published(self):
        b = _bracket()
        b["coherence_at_entry"] = 8.71
        snap = build_positions_snapshot([b], "acct", "crypto", now=0.0)
        assert snap["positions"][0]["coherence_at_entry"] == pytest.approx(8.71)

    def test_r_moved_short(self):
        # short entry 260.88 stop 264.6608 → 1R=3.7808; mark 257 → ~+1.03R
        snap = build_positions_snapshot(
            [_bracket(side="sell", entry="260.88", stop="264.6608")],
            "acct", "crypto", now=1.0,
            mark_fn=lambda a: 257.0)
        assert snap["positions"][0]["r_moved"] == pytest.approx(
            (260.88 - 257.0) / (264.6608 - 260.88))

    def test_no_order_ids_or_intents_leak(self):
        # geometry-only: venue order internals never cross the bridge
        snap = build_positions_snapshot([_bracket()], "acct", "all", now=0.0)
        pos = snap["positions"][0]
        assert "order_ids" not in pos
        assert "intents" not in pos
        assert "group_id" not in pos

    def test_empty_book_is_meaningful(self):
        # ARIA disarms on an empty list — absence must publish, not omit
        snap = build_positions_snapshot([], "acct", "tradfi", now=99.0)
        assert snap["positions"] == []
        assert snap["sleeve"] == "tradfi"

    def test_multiple_brackets_preserved(self):
        rows = [_bracket(asset="BCH", side="sell"),
                _bracket(asset="ZEC", side="sell", qty="1.5")]
        snap = build_positions_snapshot(rows, "acct", "crypto", now=0.0)
        assert [p["asset"] for p in snap["positions"]] == ["BCH", "ZEC"]
        assert snap["positions"][1]["quantity"] == 1.5

    def test_tradfi_asset_prefix_passthrough(self):
        snap = build_positions_snapshot(
            [_bracket(asset="xyz:NVDA", entry="200.0", stop="199.0")],
            "acct", "tradfi", now=0.0)
        assert snap["positions"][0]["asset"] == "xyz:NVDA"
        assert snap["positions"][0]["entry_ref"] == 200.0
