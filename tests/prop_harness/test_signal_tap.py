"""Signal tap pins — cursor, dedup, TTL, gap, sleeve filter, transports."""

from __future__ import annotations

import json

from prop_harness.io.signal_tap import SYMBOL_MAP, SignalTap, fetch_local

NOW = 1_800_000_000_000  # ms


def _line(symbol="BTC-USD", side="long", ts_ms=NOW, entry=100000.0,
          stop=99500.0, sig_id=None) -> str:
    rec = {
        "id": sig_id or f"{symbol}_{side_of(stop, entry)}_{ts_ms}",
        "symbol": symbol, "side": side,
        "entry_ref": entry, "stop_price": stop, "tp1_price": entry * 1.01,
        "coherence": 5.5, "ts_ms": ts_ms, "src": "execution_decision",
    }
    return json.dumps(rec, separators=(",", ":")) + "\n"


def side_of(stop, entry):
    return "long" if stop < entry else "short"


def _tap(tmp_path, ttl_s=300.0) -> SignalTap:
    return SignalTap(tmp_path / "cursor.json", ttl_s=ttl_s)


class TestConsume:
    def test_accepts_sleeve_b_long(self, tmp_path):
        tap = _tap(tmp_path)
        chunk = _line(side="long")
        sigs, events = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        assert len(sigs) == 1
        sig = sigs[0]
        assert sig["asset"] == "BTC"
        assert sig["side"] == "buy"
        assert sig["entry_ref"] == 100000.0
        assert sig["stop_price"] == 99500.0
        assert events == []
        assert tap.offset == len(chunk.encode())

    def test_short_normalizes_to_sell(self, tmp_path):
        tap = _tap(tmp_path)
        chunk = _line(symbol="ETH-USD", side="short", entry=3000.0, stop=3015.0)
        sigs, _ = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        assert sigs[0]["side"] == "sell"
        assert sigs[0]["asset"] == "ETH"

    def test_buy_sell_passthrough(self, tmp_path):
        tap = _tap(tmp_path)
        chunk = _line(side="buy")
        sigs, _ = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        assert sigs[0]["side"] == "buy"

    def test_garbage_side_rejected(self, tmp_path):
        tap = _tap(tmp_path)
        chunk = _line(side="diagonal")
        sigs, events = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        assert sigs == []
        assert events[0]["type"] == "tap_malformed"

    def test_malformed_json_skipped_offset_advances(self, tmp_path):
        tap = _tap(tmp_path)
        good = _line()
        chunk = "{not json\n" + good
        sigs, events = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        assert len(sigs) == 1
        assert events[0]["type"] == "tap_malformed"
        assert tap.offset == len(chunk.encode())

    def test_missing_fields_rejected(self, tmp_path):
        tap = _tap(tmp_path)
        chunk = json.dumps({"id": "x", "symbol": "BTC-USD"}) + "\n"
        sigs, events = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        assert sigs == []
        assert events[0]["type"] == "tap_malformed"

    def test_non_sleeve_symbol_filtered(self, tmp_path):
        tap = _tap(tmp_path)
        # Re-encoded 2026-11-07: SPCX-USD joined SYMBOL_MAP (stocks on demo);
        # FARTCOIN-USD stays genuinely outside the 29-symbol map.
        chunk = _line(symbol="FARTCOIN-USD", entry=0.50, stop=0.49)
        sigs, events = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        assert sigs == []
        assert events[0]["type"] == "tap_filtered"

    def test_expired_signal_dropped(self, tmp_path):
        tap = _tap(tmp_path, ttl_s=300.0)
        chunk = _line(ts_ms=NOW - 301_000)
        sigs, events = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        assert sigs == []
        assert events[0]["type"] == "tap_expired"

    def test_duplicate_id_deduped(self, tmp_path):
        tap = _tap(tmp_path)
        chunk = _line(sig_id="BTC-USD_long_1")
        tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        sigs, events = tap.consume(0, "", now_ms=NOW)  # no new bytes
        assert sigs == []
        # replay the same line (e.g. after a gap re-read)
        tap.offset = 0
        sigs, events = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        assert sigs == []
        assert events[0]["type"] == "tap_duplicate"

    def test_gap_reset_on_shrink(self, tmp_path):
        tap = _tap(tmp_path)
        chunk = _line()
        tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        assert tap.offset > 0
        sigs, events = tap.consume(5, "", now_ms=NOW)  # file shrank
        assert sigs == []
        assert events[0]["type"] == "tap_gap_reset"
        assert tap.offset == 0

    def test_partial_line_held(self, tmp_path):
        tap = _tap(tmp_path)
        full = _line()
        half = full[: len(full) // 2]
        sigs, _ = tap.consume(len(half.encode()), half, now_ms=NOW)
        assert sigs == []
        assert tap.offset == 0  # nothing complete consumed
        # transport re-reads from offset 0 -> the now-complete line arrives whole
        sigs, _ = tap.consume(len(full.encode()), full, now_ms=NOW)
        assert len(sigs) == 1
        assert tap.offset == len(full.encode())

    def test_cursor_persists_across_restart(self, tmp_path):
        tap = _tap(tmp_path)
        chunk = _line(sig_id="BTC-USD_long_9")
        tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        tap2 = _tap(tmp_path)  # simulated restart
        assert tap2.offset == tap.offset
        sigs, events = tap2.consume(0, "", now_ms=NOW)
        tap2.offset = 0
        sigs, events = tap2.consume(len(chunk.encode()), chunk, now_ms=NOW)
        assert sigs == []
        assert events[0]["type"] == "tap_duplicate"

    def test_dedup_cap_trims(self, tmp_path):
        tap = SignalTap(tmp_path / "cursor.json", ttl_s=1e9, dedup_cap=10)
        chunk = "".join(_line(sig_id=f"id_{i}", ts_ms=NOW + i)
                        for i in range(14))
        tap.consume(len(chunk.encode()), chunk, now_ms=NOW + 100)
        assert len(tap.seen) <= 10
        assert "id_13" in tap.seen

    def test_sleeve_map_shape(self):
        # Re-encoded 2026-11-07 (2nd): Governor ordered stocks +
        # commodities + AAVE onto the demo account — crypto 8, commodities
        # 4, indices 2, stocks 13. xyz: assets ride XYZ_DEFAULT_SPEC.
        assert SYMBOL_MAP["BTC-USD"] == "BTC"
        assert SYMBOL_MAP["AAVE-USD"] == "AAVE"
        assert SYMBOL_MAP["XAUT-USD"] == "xyz:GOLD"
        assert SYMBOL_MAP["SPCX-USD"] == "xyz:SP500"
        assert SYMBOL_MAP["NVDA-USD"] == "xyz:NVDA"
        assert len(SYMBOL_MAP) == 29


class TestFetchLocal:
    def test_reads_from_offset(self, tmp_path):
        p = tmp_path / "outbox.jsonl"
        first = _line(sig_id="a1")
        p.write_text(first)
        size, chunk = fetch_local(p, 0)
        assert size == len(first.encode())
        assert chunk == first
        second = _line(sig_id="a2")
        with open(p, "a") as f:
            f.write(second)
        size, chunk = fetch_local(p, size)
        assert chunk == second

    def test_missing_file_returns_none(self, tmp_path):
        assert fetch_local(tmp_path / "nope.jsonl", 0) is None

    def test_shrink_reports_gap(self, tmp_path):
        p = tmp_path / "outbox.jsonl"
        p.write_text(_line(sig_id="b1"))
        p.write_text("x\n")  # truncated
        size, chunk = fetch_local(p, 500)
        assert size == 2 and chunk == ""
