"""Pins: cross-sleeve book view — conflict TELEMETRY (2026-11-17).

The plane writes and reads but NEVER blocks: conflicts() is pure
evidence. Stale sleeves fail open; xyz: normalization is load-bearing
(turbo2 xyz:GOLD long vs a hypothetical GOLD short on another sleeve is
ONE conflict).
"""

from __future__ import annotations

from prop_harness.brains import book_view as bv


def _row(asset, side, status="open"):
    return {"asset": asset, "side": side, "status": status}


class TestBuildEntry:
    def test_open_rows_become_exposure(self):
        entry = bv.build_sleeve_entry(
            [_row("xyz:GOLD", "buy"), _row("BTC", "sell"),
             _row("ETH", "buy", status="closed_external")], now=100.0)
        assert entry["ts"] == 100.0
        assert entry["exposure"] == [
            {"asset": "GOLD", "side": "buy"},
            {"asset": "BTC", "side": "sell"}]

    def test_garbage_rows_skipped(self):
        entry = bv.build_sleeve_entry(
            [{"status": "open"}, _row("BTC", "sideways")], now=100.0)
        assert entry["exposure"] == []


class TestMerge:
    def test_sleeve_keyed(self):
        view = bv.merge_sleeve({}, "acct1",
                               bv.build_sleeve_entry([], now=1.0))
        assert view["sleeves"]["acct1"]["ts"] == 1.0
        assert view["schema"] == bv.SCHEMA

    def test_merge_preserves_peers(self):
        view = bv.merge_sleeve({}, "acct1",
                               bv.build_sleeve_entry([], now=1.0))
        view = bv.merge_sleeve(view, "acct2",
                               bv.build_sleeve_entry([], now=2.0))
        assert set(view["sleeves"]) == {"acct1", "acct2"}


class TestConflicts:
    def _view(self, now=1000.0):
        view = bv.merge_sleeve({}, "turbo2", bv.build_sleeve_entry(
            [_row("xyz:GOLD", "buy")], now=now))
        return bv.merge_sleeve(view, "turbo", bv.build_sleeve_entry(
            [_row("GOLD", "sell"), _row("BTC", "sell")], now=now))

    def test_opposite_side_detected(self):
        out = bv.conflicts(self._view(), "turbo2", now=1000.0)
        assert len(out) == 1
        c = out[0]
        assert c["asset"] == "GOLD"
        assert c["own_sleeve"] == "turbo2" and c["own_side"] == "buy"
        assert c["other_sleeve"] == "turbo" and c["other_side"] == "sell"

    def test_same_side_not_conflict(self):
        view = bv.merge_sleeve({}, "a", bv.build_sleeve_entry(
            [_row("BTC", "sell")], now=1000.0))
        view = bv.merge_sleeve(view, "b", bv.build_sleeve_entry(
            [_row("BTC", "sell")], now=1000.0))
        assert bv.conflicts(view, "a", now=1000.0) == []

    def test_stale_peer_fails_open(self):
        view = bv.merge_sleeve({}, "turbo", bv.build_sleeve_entry(
            [_row("GOLD", "sell")], now=100.0))  # ancient
        view = bv.merge_sleeve(view, "turbo2", bv.build_sleeve_entry(
            [_row("xyz:GOLD", "buy")], now=1000.0))
        assert bv.conflicts(view, "turbo2", now=1000.0) == []

    def test_missing_planes_allow(self):
        assert bv.conflicts(None, "a") == []
        assert bv.conflicts({}, "a") == []
        assert bv.conflicts({"sleeves": {}}, "a") == []

    def test_own_sleeve_absent_allows(self):
        view = bv.merge_sleeve({}, "b", bv.build_sleeve_entry(
            [_row("BTC", "sell")], now=1000.0))
        assert bv.conflicts(view, "a", now=1000.0) == []


class TestThrottle:
    def test_one_log_per_window(self):
        last: dict[str, float] = {}
        assert bv.throttle_allows(last, "k", now=1000.0) is True
        bv.note_throttle(last, "k", now=1000.0)
        assert bv.throttle_allows(last, "k", now=1100.0) is False
        assert bv.throttle_allows(last, "k", now=1300.0) is True

    def test_none_allows(self):
        assert bv.throttle_allows(None, "k", now=1000.0) is True
