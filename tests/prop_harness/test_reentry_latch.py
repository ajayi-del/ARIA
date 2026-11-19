"""Pins: re-entry close-epoch latch (2026-11-17).

A fresh closed_ts row refuses same (symbol, side) re-entry inside the
TTL; rows without closed_ts are legacy-valid and never latch. Kill OFF /
expired / wrong side / wrong symbol all allow.
"""

from __future__ import annotations

from prop_harness.brains import reentry_latch as rl


def _row(asset="BTC", side="buy", closed_ts=1000.0, status="closed_external"):
    row = {"asset": asset, "side": side, "status": status}
    if closed_ts is not None:
        row["closed_ts"] = closed_ts
    return row


class TestVerdict:
    def test_fresh_close_blocks(self):
        reason, detail = rl.reentry_verdict(
            True, [_row()], "BTC", "buy", 1100.0, ttl_s=1800.0)
        assert reason == rl.REENTRY_BLOCK
        assert detail["last_close_ts"] == 1000.0
        assert detail["age_s"] == 100.0
        assert detail["ttl_remaining_s"] == 1700.0

    def test_passes_after_ttl(self):
        assert rl.reentry_verdict(
            True, [_row()], "BTC", "buy", 2800.0, ttl_s=1800.0)[0] is None

    def test_kill_off_allows(self):
        assert rl.reentry_verdict(
            False, [_row()], "BTC", "buy", 1100.0)[0] is None

    def test_zero_ttl_allows(self):
        assert rl.reentry_verdict(
            True, [_row()], "BTC", "buy", 1100.0, ttl_s=0.0)[0] is None

    def test_wrong_side_allows(self):
        assert rl.reentry_verdict(
            True, [_row()], "BTC", "sell", 1100.0)[0] is None

    def test_wrong_symbol_allows(self):
        assert rl.reentry_verdict(
            True, [_row()], "ETH", "buy", 1100.0)[0] is None

    def test_open_row_never_latches(self):
        assert rl.reentry_verdict(
            True, [_row(status="open")], "BTC", "buy", 1100.0)[0] is None

    def test_legacy_row_without_closed_ts_valid(self):
        # Epoch-less legacy close: valid row, no latch (fail-open).
        assert rl.reentry_verdict(
            True, [_row(closed_ts=None)], "BTC", "buy", 1100.0)[0] is None

    def test_xyz_normalized(self):
        assert rl.reentry_verdict(
            True, [_row(asset="xyz:GOLD")], "GOLD", "buy", 1100.0)[0] \
            == rl.REENTRY_BLOCK

    def test_latest_close_governs(self):
        rows = [_row(closed_ts=900.0), _row(closed_ts=1500.0)]
        assert rl.reentry_verdict(
            True, rows, "BTC", "buy", 1200.0, ttl_s=1800.0)[0] is None
        assert rl.reentry_verdict(
            True, rows, "BTC", "buy", 1600.0, ttl_s=1800.0)[0] \
            == rl.REENTRY_BLOCK

    def test_future_clock_skew_allows(self):
        # close_ts ahead of now (clock skew) must not latch forever.
        assert rl.reentry_verdict(
            True, [_row(closed_ts=2000.0)], "BTC", "buy", 1100.0)[0] is None

    def test_empty_rows_allow(self):
        assert rl.reentry_verdict(True, [], "BTC", "buy", 1100.0)[0] is None
        assert rl.reentry_verdict(True, None, "BTC", "buy", 1100.0)[0] is None
