"""Signal tap pins — cursor, dedup, TTL, gap, sleeve filter, transports."""

from __future__ import annotations

import json

from prop_harness.io.signal_tap import (
    DEFAULT_TRADFI_TTL_S, REFUSE_SYMBOLS, SYMBOL_MAP, SignalTap,
    fetch_local, refuse_reason,
)

NOW = 1_800_000_000_000  # ms


def _line(symbol="BTC-USD", side="long", ts_ms=NOW, entry=100000.0,
          stop=99500.0, sig_id=None, block_class=None,
          capital_reason=None) -> str:
    rec = {
        "id": sig_id or f"{symbol}_{side_of(stop, entry)}_{ts_ms}",
        "symbol": symbol, "side": side,
        "entry_ref": entry, "stop_price": stop, "tp1_price": entry * 1.01,
        "coherence": 5.5, "ts_ms": ts_ms, "src": "execution_decision",
    }
    if block_class is not None:
        rec["block_class"] = block_class
    if capital_reason is not None:
        rec["capital_reason"] = capital_reason
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
        # Re-encoded 2026-11-07 (wave 2): FARTCOIN joined the map; CYS-USD is
        # genuinely outside — probed venue-DARK on the WS mark stream today.
        # Graph 7: refuse table stamps explicit reason (no route invented).
        chunk = _line(symbol="CYS-USD", entry=0.50, stop=0.49)
        sigs, events = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        assert sigs == []
        assert events[0]["type"] == "tap_filtered"
        assert events[0]["reason"] == "venue_unlisted"
        assert refuse_reason("CYS-USD") == "venue_unlisted"
        assert refuse_reason("SPY-USD") == "arcus_unmapped"
        assert refuse_reason("NEVERHEARD-USD") == "unmapped"
        assert "CYS-USD" in REFUSE_SYMBOLS and "CYS-USD" not in SYMBOL_MAP

    def test_expired_signal_dropped(self, tmp_path):
        tap = _tap(tmp_path, ttl_s=300.0)
        chunk = _line(ts_ms=NOW - 301_000)
        sigs, events = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        assert sigs == []
        assert events[0]["type"] == "tap_expired"
        assert events[0]["tradfi"] is False
        assert events[0]["ttl_ms"] == 300_000.0

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
        # Re-encoded 2026-11-08 (wave 3): Governor ordered equities + FX +
        # pre-IPO onto the prop legs — 68 + 92 = 160. Propr executes on
        # Hyperliquid (margin-config exchange field); the program's
        # leverage-limits endpoint confirms fx/pre_ipo/equity classes.
        # NOTE: GET margin-config AUTO-CREATES a default config for any
        # asset string, so it is NOT a listing probe — HL's xyz dex meta
        # (143 assets, probed live) is the binding listing constraint.
        # Re-encoded 2026-11-08: SMCI + CRCL mapped (ARIA-universe equities,
        # venue-probed + margin-provisioned) — 66 + 2 = 68.
        # Re-encoded 2026-11-07 (2nd): Governor ordered stocks +
        # commodities + AAVE onto the demo account — crypto 8, commodities
        # 4, indices 2, stocks 13. xyz: assets ride XYZ_DEFAULT_SPEC.
        assert SYMBOL_MAP["BTC-USD"] == "BTC"
        assert SYMBOL_MAP["AAVE-USD"] == "AAVE"
        assert SYMBOL_MAP["XAUT-USD"] == "xyz:GOLD"
        assert SYMBOL_MAP["SPCX-USD"] == "xyz:SP500"
        assert SYMBOL_MAP["NVDA-USD"] == "xyz:NVDA"
        assert SYMBOL_MAP["SMCI-USD"] == "xyz:SMCI"
        assert SYMBOL_MAP["CRCL-USD"] == "xyz:CRCL"
        assert SYMBOL_MAP["EUR-USD"] == "xyz:EUR"
        assert SYMBOL_MAP["DXY-USD"] == "xyz:DXY"
        assert SYMBOL_MAP["SHEIN-USD"] == "xyz:SHEIN"
        assert SYMBOL_MAP["MINIMAX-USD"] == "xyz:MINIMAX"
        assert SYMBOL_MAP["NATGAS-USD"] == "xyz:NATGAS"
        assert len(SYMBOL_MAP) == 160
        # Graph 7 hygiene: refuse table covers census gaps; never invents routes
        assert len(REFUSE_SYMBOLS) >= 21
        assert set(REFUSE_SYMBOLS).isdisjoint(SYMBOL_MAP)


class TestTradfiTtlAndBootReplay:
    """Graph 7: sleeve-aware TTL + tradfi hot-cursor so equity ≠ tap_expired."""

    def test_tradfi_survives_past_crypto_ttl(self, tmp_path):
        # Default tradfi TTL 3600s — a 10-min-old NVDA row is still live while
        # a same-age BTC row expires under the 300s crypto TTL.
        tap = SignalTap(tmp_path / "cursor.json", ttl_s=300.0,
                        tradfi_ttl_s=DEFAULT_TRADFI_TTL_S)
        age = 600_000  # 10 min
        chunk = (_line(symbol="BTC-USD", ts_ms=NOW - age, sig_id="btc_old")
                 + _line(symbol="NVDA-USD", entry=200.0, stop=199.0,
                         ts_ms=NOW - age, sig_id="nvda_old"))
        sigs, events = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        assert [s["asset"] for s in sigs] == ["xyz:NVDA"]
        expired = [e for e in events if e["type"] == "tap_expired"]
        assert len(expired) == 1 and expired[0]["id"] == "btc_old"
        assert expired[0]["tradfi"] is False

    def test_tradfi_still_expires_past_sleeve_ttl(self, tmp_path):
        tap = SignalTap(tmp_path / "cursor.json", ttl_s=300.0,
                        tradfi_ttl_s=3600.0)
        chunk = _line(symbol="XAUT-USD", entry=4000.0, stop=3980.0,
                      ts_ms=NOW - 3_601_000, sig_id="gold_dead")
        sigs, events = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        assert sigs == []
        assert events[0]["type"] == "tap_expired"
        assert events[0]["tradfi"] is True
        assert events[0]["ttl_ms"] == 3_600_000.0

    def test_tradfi_ttl_kill_switch_legacy(self, tmp_path):
        # tradfi_ttl_s<=0 → crypto TTL for all (pre-Graph-7 bit-for-bit)
        tap = SignalTap(tmp_path / "cursor.json", ttl_s=300.0,
                        tradfi_ttl_s=0.0)
        chunk = _line(symbol="NVDA-USD", entry=200.0, stop=199.0,
                      ts_ms=NOW - 301_000, sig_id="nvda_legacy")
        sigs, events = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        assert sigs == []
        assert events[0]["type"] == "tap_expired"
        assert events[0]["ttl_ms"] == 300_000.0

    def test_boot_replay_rewinds_and_rescues_in_ttl(self, tmp_path):
        tap = SignalTap(tmp_path / "cursor.json", ttl_s=300.0,
                        tradfi_ttl_s=3600.0, asset_class="tradfi")
        # First pass: consume a fresh equity row (cursor advances to EOF)
        live = _line(symbol="NVDA-USD", entry=200.0, stop=199.0,
                     ts_ms=NOW - 60_000, sig_id="nvda_live")
        sigs, _ = tap.consume(len(live.encode()), live, now_ms=NOW)
        assert len(sigs) == 1
        offset_after = tap.offset
        assert offset_after > 0
        # Forget the id (simulates empty seen after a cold turbo2 boot) and
        # rewind — Monday RTH path: in-TTL equity is actionable again.
        tap.seen.clear()
        ev = tap.arm_boot_replay()
        assert ev is not None
        assert ev["type"] == "tap_boot_replay"
        assert ev["prev_offset"] == offset_after
        assert tap.offset == 0
        # Second arm at head is a no-op
        assert tap.arm_boot_replay() is None
        sigs, events = tap.consume(len(live.encode()), live, now_ms=NOW)
        assert len(sigs) == 1 and sigs[0]["id"] == "nvda_live"
        assert not any(e["type"] == "tap_expired" for e in events)

    def test_boot_replay_still_expires_stale_after_rewind(self, tmp_path):
        tap = SignalTap(tmp_path / "cursor.json", ttl_s=300.0,
                        tradfi_ttl_s=3600.0, asset_class="tradfi")
        stale = _line(symbol="NVDA-USD", entry=200.0, stop=199.0,
                      ts_ms=NOW - 3_601_000, sig_id="nvda_stale")
        tap.consume(len(stale.encode()), stale, now_ms=NOW)
        tap.seen.clear()
        tap.arm_boot_replay()
        sigs, events = tap.consume(len(stale.encode()), stale, now_ms=NOW)
        assert sigs == []
        assert events[0]["type"] == "tap_expired"


class TestFamilySubset:
    """Two-turbo partition (Governor 2026-11-08): each theme family lives on
    exactly one account so the per-account family cap doubles capacity."""

    def test_no_subset_is_bit_for_bit(self, tmp_path):
        tap = _tap(tmp_path)
        assert tap.families is None
        chunk = _line(symbol="XRP-USD", entry=2.0, stop=2.04)
        sigs, _ = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        assert len(sigs) == 1

    def test_listed_family_passes(self, tmp_path):
        tap = SignalTap(tmp_path / "cursor.json", families=frozenset({"majors"}))
        chunk = _line()  # BTC
        sigs, _ = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        assert len(sigs) == 1 and sigs[0]["asset"] == "BTC"

    def test_unlisted_family_filtered(self, tmp_path):
        tap = SignalTap(tmp_path / "cursor.json", families=frozenset({"majors"}))
        chunk = _line(symbol="XRP-USD", entry=2.0, stop=2.04)  # large_alt
        sigs, events = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        assert sigs == []
        assert events[0]["reason"] == "family_subset"
        assert events[0]["family"] == "large_alt"

    def test_tradfi_family_filtered(self, tmp_path):
        tap = SignalTap(tmp_path / "cursor.json", families=frozenset({"majors"}))
        chunk = _line(symbol="XAUT-USD", entry=4000.0, stop=3980.0)  # metal
        sigs, events = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        assert sigs == []
        assert events[0]["reason"] == "family_subset"

    def test_partition_covers_crypto(self, tmp_path):
        # turbo-1 = majors/defi/exchange/privacy; turbo-2 = large_alt/l1_alt/
        # meme/ai — disjoint, union = every crypto family in the map
        t1 = SignalTap(tmp_path / "c1.json", asset_class="crypto",
                       families=frozenset({"majors", "defi", "exchange", "privacy"}))
        t2 = SignalTap(tmp_path / "c2.json", asset_class="crypto",
                       families=frozenset({"large_alt", "l1_alt", "meme", "ai"}))
        # One consume per tap — size is the whole-file length, offsets are
        # cumulative; per-line consumes would trip the gap-reset guard.
        t1_only = "".join(_line(symbol=s, entry=100.0, stop=99.0,
                                sig_id=f"{s}_1")
                          for s in ("BTC-USD", "AAVE-USD"))
        t2_only = "".join(_line(symbol=s, entry=100.0, stop=99.0,
                                sig_id=f"{s}_2")
                          for s in ("XRP-USD", "NEAR-USD", "DOGE-USD"))
        # t1 keeps majors+defi, drops large_alt/l1_alt/meme
        sigs, _ = t1.consume(len(t1_only.encode()), t1_only, now_ms=NOW)
        assert [s["asset"] for s in sigs] == ["BTC", "AAVE"]
        sigs, _ = t1.consume(len(t2_only.encode()), t2_only, now_ms=NOW)
        assert sigs == []
        # t2 keeps large_alt+l1_alt+meme, drops majors/defi
        sigs, _ = t2.consume(len(t1_only.encode()), t1_only, now_ms=NOW)
        assert sigs == []
        sigs, _ = t2.consume(len(t2_only.encode()), t2_only, now_ms=NOW)
        assert [s["asset"] for s in sigs] == ["XRP", "NEAR", "DOGE"]


class TestAssetClassFilter:
    """Sleeve split 2026-11-07: demo keeps crypto, Turbo takes xyz: tradfi."""

    def test_crypto_drops_xyz(self, tmp_path):
        tap = SignalTap(tmp_path / "cursor.json", asset_class="crypto")
        chunk = _line(symbol="NVDA-USD", entry=200.0, stop=199.0)
        sigs, events = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        assert sigs == []
        assert events[0]["type"] == "tap_filtered"
        assert events[0]["reason"] == "asset_class"

    def test_crypto_keeps_crypto(self, tmp_path):
        tap = SignalTap(tmp_path / "cursor.json", asset_class="crypto")
        chunk = _line()
        sigs, _ = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        assert len(sigs) == 1 and sigs[0]["asset"] == "BTC"

    def test_tradfi_drops_crypto(self, tmp_path):
        tap = SignalTap(tmp_path / "cursor.json", asset_class="tradfi")
        chunk = _line()
        sigs, events = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        assert sigs == []
        assert events[0]["reason"] == "asset_class"

    def test_tradfi_keeps_xyz(self, tmp_path):
        tap = SignalTap(tmp_path / "cursor.json", asset_class="tradfi")
        chunk = _line(symbol="XAUT-USD", entry=4000.0, stop=3980.0)
        sigs, _ = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        assert len(sigs) == 1 and sigs[0]["asset"] == "xyz:GOLD"

    def test_all_is_default_bit_for_bit(self, tmp_path):
        tap = SignalTap(tmp_path / "cursor.json")
        assert tap.asset_class == "all"
        chunk = _line(symbol="NVDA-USD", entry=200.0, stop=199.0)
        sigs, _ = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        assert len(sigs) == 1

    def test_bad_class_rejected(self, tmp_path):
        import pytest
        with pytest.raises(ValueError):
            SignalTap(tmp_path / "cursor.json", asset_class="everything")


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


class TestBlockClassSurplus:
    """§7 / graph-synthesis: capital_block consumable; risk_block never;
    unlabeled omit = legacy aria_exec (fail-open). No KingdomView gate in v1."""

    def test_unlabeled_is_legacy_exec(self, tmp_path):
        tap = _tap(tmp_path)
        chunk = _line(sig_id="unlabeled_1")  # no block_class
        sigs, events = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        assert len(sigs) == 1
        assert sigs[0]["block_class"] == "aria_exec"
        assert events == []

    def test_capital_block_accepted_when_surplus_on(self, tmp_path):
        tap = SignalTap(tmp_path / "cursor.json", capital_surplus_enabled=True)
        chunk = _line(sig_id="cap_1", block_class="aria_capital_block",
                      capital_reason="notional_floor")
        sigs, events = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        assert len(sigs) == 1
        assert sigs[0]["block_class"] == "aria_capital_block"
        assert sigs[0]["capital_reason"] == "notional_floor"
        assert events == []

    def test_capital_block_standby_when_surplus_off(self, tmp_path):
        tap = SignalTap(tmp_path / "cursor.json", capital_surplus_enabled=False)
        chunk = _line(sig_id="cap_off", block_class="aria_capital_block",
                      capital_reason="margin_budget")
        sigs, events = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        assert sigs == []
        assert events[0]["type"] == "capital_block_standby"

    def test_risk_block_always_dropped(self, tmp_path):
        tap = SignalTap(tmp_path / "cursor.json", capital_surplus_enabled=True)
        chunk = _line(sig_id="risk_1", block_class="aria_risk_block")
        sigs, events = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        assert sigs == []
        assert events[0]["type"] == "risk_block_dropped"

    def test_aria_exec_labeled_passes(self, tmp_path):
        tap = _tap(tmp_path)
        chunk = _line(sig_id="exec_1", block_class="aria_exec")
        sigs, _ = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        assert len(sigs) == 1
        assert sigs[0]["block_class"] == "aria_exec"
