"""Signal tap pins — cursor, dedup, TTL, gap, sleeve filter, transports."""

from __future__ import annotations

import json
import time

from prop_harness.io.signal_tap import (
    DEFAULT_CRYPTO_DENY_SYMBOLS, DEFAULT_TRADFI_TTL_S, REFUSE_SYMBOLS,
    SYMBOL_MAP, SignalTap, fetch_local, parse_crypto_deny_symbols,
    refuse_reason,
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
        sigs, _ = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        # Re-encoded 2026-11-17 (hole a): seen is burned only after the
        # execution attempt resolves — the harness confirms here.
        tap.confirm(sigs[0], resolved=True, now_ms=NOW)
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
        sigs, _ = tap.consume(len(chunk.encode()), chunk, now_ms=NOW + 100)
        # Re-encoded 2026-11-17 (hole a): ids enter seen via confirm().
        for i, sig in enumerate(sigs):
            tap.confirm(sig, resolved=True, now_ms=NOW + i)
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
        assert len(SYMBOL_MAP) == 161
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
        # Forget the id (simulates empty seen after a cold turbo2 boot —
        # a cold process also drops the in-memory pending hold) and rewind.
        # Re-encoded 2026-11-17 (hole a): pending survives seen.clear().
        tap.seen.clear()
        tap._pending.clear()
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



class TestCryptoDeny:
    """ATS-102: turbo crypto toxic cohort — sleeve-scoped denylist only."""

    def test_default_cohort_pinned(self):
        assert DEFAULT_CRYPTO_DENY_SYMBOLS == frozenset({
            "WLD-USD", "APT-USD", "SOL-USD", "BCH-USD", "XMR-USD", "SUI-USD",
        })
        assert DEFAULT_CRYPTO_DENY_SYMBOLS.isdisjoint(
            {s for s, a in SYMBOL_MAP.items() if a.startswith("xyz:")})

    def test_crypto_sleeve_denies_toxic(self, tmp_path):
        tap = SignalTap(tmp_path / "cursor.json", asset_class="crypto")
        chunk = _line(symbol="WLD-USD", entry=1.0, stop=1.01)
        sigs, events = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        assert sigs == []
        assert events[0]["type"] == "tap_filtered"
        assert events[0]["reason"] == "crypto_deny"
        assert events[0]["symbol"] == "WLD-USD"

    def test_crypto_sleeve_keeps_non_deny(self, tmp_path):
        tap = SignalTap(tmp_path / "cursor.json", asset_class="crypto")
        chunk = _line()  # BTC-USD
        sigs, _ = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        assert len(sigs) == 1 and sigs[0]["asset"] == "BTC"

    def test_all_sleeve_ignores_deny(self, tmp_path):
        # demo asset_class=all must stay bit-for-bit (overflow can still fire).
        tap = SignalTap(tmp_path / "cursor.json", asset_class="all")
        chunk = _line(symbol="SOL-USD", entry=100.0, stop=99.0)
        sigs, _ = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        assert len(sigs) == 1 and sigs[0]["asset"] == "SOL"

    def test_kill_switch_passes_toxic(self, tmp_path):
        tap = SignalTap(tmp_path / "cursor.json", asset_class="crypto",
                        crypto_deny_enabled=False)
        chunk = _line(symbol="APT-USD", entry=5.0, stop=5.05)
        sigs, _ = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        assert len(sigs) == 1 and sigs[0]["asset"] == "APT"

    def test_override_list(self, tmp_path):
        tap = SignalTap(tmp_path / "cursor.json", asset_class="crypto",
                        crypto_deny_symbols=frozenset({"NEAR-USD"}))
        near = _line(symbol="NEAR-USD", entry=3.0, stop=3.03)
        wld = _line(symbol="WLD-USD", entry=1.0, stop=1.01, ts_ms=NOW + 1)
        sigs, events = tap.consume(
            len((near + wld).encode()), near + wld, now_ms=NOW + 1)
        assert [s["asset"] for s in sigs] == ["WLD"]
        assert any(e.get("reason") == "crypto_deny" for e in events)

    def test_parse_empty_uses_default(self):
        assert parse_crypto_deny_symbols(None) == DEFAULT_CRYPTO_DENY_SYMBOLS
        assert parse_crypto_deny_symbols("") == DEFAULT_CRYPTO_DENY_SYMBOLS

    def test_parse_override(self):
        assert parse_crypto_deny_symbols("WLD-USD, APT-USD") == frozenset({
            "WLD-USD", "APT-USD",
        })


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


class TestTapHoles:
    """2026-11-17 holes: (a) deferred seen-marking, (b) TTL-aware prune,
    (d) envelope v2 signal_id/signal_ts/epoch propagation + dedup priority.
    Kill switches reproduce the legacy immediate-mark / flat-cap path."""

    def test_pending_hold_not_yet_seen(self, tmp_path):
        tap = _tap(tmp_path)
        chunk = _line(sig_id="p1")
        sigs, _ = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        assert len(sigs) == 1
        assert "p1" not in tap.seen and "p1" in tap._pending
        # refire while pending is a duplicate (claim held, not burned)
        tap.offset = 0
        sigs, events = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        assert sigs == [] and events[0]["type"] == "tap_duplicate"

    def test_confirm_resolved_burns_id(self, tmp_path):
        tap = _tap(tmp_path)
        chunk = _line(sig_id="p2")
        sigs, _ = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        tap.confirm(sigs[0], resolved=True, now_ms=NOW)
        assert "p2" in tap.seen and "p2" not in tap._pending

    def test_confirm_release_allows_refire(self, tmp_path):
        # Failure-to-execute must not burn the id: release → rewind → refire.
        tap = _tap(tmp_path)
        chunk = _line(sig_id="p3")
        sigs, _ = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        tap.confirm(sigs[0], resolved=False, now_ms=NOW)
        assert "p3" not in tap.seen and "p3" not in tap._pending
        tap.offset = 0
        sigs, _ = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        assert len(sigs) == 1

    def test_seen_confirm_kill_marks_immediately(self, tmp_path):
        tap = SignalTap(tmp_path / "cursor.json", seen_confirm_enabled=False)
        chunk = _line(sig_id="imm1")
        sigs, _ = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        assert len(sigs) == 1
        assert "imm1" in tap.seen and tap._pending == {}
        tap.confirm(sigs[0], resolved=True, now_ms=NOW)  # no-op legacy
        assert "imm1" in tap.seen

    def test_ttl_prune_keeps_live_drops_stale(self, tmp_path, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: NOW / 1000.0)
        tap = SignalTap(tmp_path / "cursor.json", ttl_s=300.0,
                        tradfi_ttl_s=3600.0)
        tap.seen = {"stale": NOW - 5_000_000.0, "live": NOW - 60_000.0}
        tap._save()
        assert "live" in tap.seen and "stale" not in tap.seen

    def test_ttl_prune_kill_legacy_flat_cap(self, tmp_path):
        tap = SignalTap(tmp_path / "cursor.json", ttl_s=1e9, dedup_cap=4,
                        ttl_prune_enabled=False)
        chunk = "".join(_line(sig_id=f"k{i}", ts_ms=NOW + i)
                        for i in range(6))
        sigs, _ = tap.consume(len(chunk.encode()), chunk, now_ms=NOW + 100)
        for sig in sigs:
            tap.confirm(sig, resolved=True, now_ms=NOW)
        # legacy halve-on-overflow fired when the 5th id landed (5>4 → 2)
        assert "k0" not in tap.seen and "k5" in tap.seen
        assert len(tap.seen) == 3

    def test_signal_envelope_propagates(self, tmp_path):
        tap = _tap(tmp_path)
        rec = json.loads(_line(sig_id="leg1"))
        rec["signal_id"] = "ep7:42"
        rec["signal_ts"] = NOW
        rec["epoch"] = 7
        chunk = json.dumps(rec, separators=(",", ":")) + "\n"
        sigs, _ = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        sig = sigs[0]
        assert sig["signal_id"] == "ep7:42"
        assert sig["signal_ts"] == NOW and sig["epoch"] == 7

    def test_signal_id_outranks_legacy_id(self, tmp_path):
        tap = _tap(tmp_path)
        rec = json.loads(_line(sig_id="legA"))
        rec["signal_id"] = "ep7:42"
        chunk = json.dumps(rec, separators=(",", ":")) + "\n"
        sigs, _ = tap.consume(len(chunk.encode()), chunk, now_ms=NOW)
        tap.confirm(sigs[0], resolved=True, now_ms=NOW)
        assert "ep7:42" in tap.seen
        # same signal under a NEW legacy id still dedups
        tap.offset = 0
        rec2 = json.loads(_line(sig_id="legB"))
        rec2["signal_id"] = "ep7:42"
        chunk2 = json.dumps(rec2, separators=(",", ":")) + "\n"
        sigs, events = tap.consume(len(chunk2.encode()), chunk2, now_ms=NOW)
        assert sigs == [] and events[0]["type"] == "tap_duplicate"


class TestClockSkewGuard:
    """L7 (2026-11-17): stale only when BOTH clocks agree.

    Seed accepted at 100s skew (emit NOW, local receipt NOW+100_000) sets
    the recv anchor. Skew grows to 350s: a candidate emitting NOW+20_000
    received at NOW+370_000 is wall-stale (age 350_000 > 300_000 TTL) but
    envelope-fresh (mapped_age 270_000 - 20_000 = 250_000) → KEEP with a
    throttled tap_clock_skew event. Guard OFF / no anchor = legacy
    wall-clock-only.
    """

    def _feed(self, tap, chunk, now_ms):
        # consume(size, chunk): size is the cumulative FILE size — append
        # semantics, so each new chunk rides on top of the current offset.
        size = tap.offset + len(chunk.encode())
        return tap.consume(size, chunk, now_ms=now_ms)

    def _seed(self, tap):
        chunk = _line(sig_id="seed", ts_ms=NOW)
        sigs, events = self._feed(tap, chunk, NOW + 100_000)
        assert len(sigs) == 1 and events == []

    def test_skew_guard_keeps_just_received_old_emit(self, tmp_path):
        tap = _tap(tmp_path)
        self._seed(tap)
        chunk = _line(sig_id="skewed", ts_ms=NOW + 20_000)
        sigs, events = self._feed(tap, chunk, NOW + 370_000)
        assert len(sigs) == 1 and sigs[0]["id"] == "skewed"
        skew = [e for e in events if e["type"] == "tap_clock_skew"]
        assert len(skew) == 1
        assert skew[0]["skew_ms"] == 100_000
        assert skew[0]["age_ms"] == 350_000
        assert skew[0]["mapped_age_ms"] == 250_000
        assert not [e for e in events if e["type"] == "tap_expired"]

    def test_anchor_persists_across_restart(self, tmp_path):
        tap = _tap(tmp_path)
        self._seed(tap)
        tap2 = _tap(tmp_path)  # simulated restart, same cursor file
        tap2.offset = tap.offset  # cursor offset reloads from state file
        chunk = _line(sig_id="skewed", ts_ms=NOW + 20_000)
        sigs, events = self._feed(tap2, chunk, NOW + 370_000)
        assert len(sigs) == 1
        assert any(e["type"] == "tap_clock_skew" for e in events)

    def test_genuinely_old_row_still_expires(self, tmp_path):
        # Replay safety: an old row's emit is far behind the anchor emit,
        # so mapped_age = gap + true_age > ttl → tap_expired as legacy.
        tap = _tap(tmp_path)
        self._seed(tap)
        chunk = _line(sig_id="old", ts_ms=NOW - 400_000)
        sigs, events = self._feed(tap, chunk, NOW + 370_000)
        assert sigs == []
        assert [e["type"] for e in events] == ["tap_expired"]

    def test_cold_boot_no_anchor_is_legacy(self, tmp_path):
        tap = _tap(tmp_path)
        chunk = _line(sig_id="skewed", ts_ms=NOW + 20_000)
        sigs, events = tap.consume(len(chunk.encode()), chunk,
                                   now_ms=NOW + 370_000)
        assert sigs == []
        assert [e["type"] for e in events] == ["tap_expired"]

    def test_skew_event_throttled(self, tmp_path):
        tap = _tap(tmp_path)
        self._seed(tap)
        chunk = (_line(sig_id="s1", ts_ms=NOW + 20_000)
                 + _line(sig_id="s2", ts_ms=NOW + 30_000))
        sigs, events = self._feed(tap, chunk, NOW + 370_000)
        assert len(sigs) == 2  # both kept
        skew = [e for e in events if e["type"] == "tap_clock_skew"]
        assert len(skew) == 1  # throttled to one per window

    def test_guard_off_is_legacy_wall_clock_only(self, tmp_path):
        tap = SignalTap(tmp_path / "cursor.json", ttl_s=300.0,
                        skew_guard_enabled=False)
        self._seed(tap)
        chunk = _line(sig_id="skewed", ts_ms=NOW + 20_000)
        sigs, events = self._feed(tap, chunk, NOW + 370_000)
        assert sigs == []
        assert [e["type"] for e in events] == ["tap_expired"]

    def test_env_kill_switch_off(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PROP_TAP_SKEW_GUARD", "0")
        tap = SignalTap(tmp_path / "cursor.json", ttl_s=300.0,
                        skew_guard_enabled=None)
        assert tap.skew_guard_enabled is False
        monkeypatch.setenv("PROP_TAP_SKEW_GUARD", "1")
        tap = SignalTap(tmp_path / "cursor2.json", ttl_s=300.0,
                        skew_guard_enabled=None)
        assert tap.skew_guard_enabled is True
