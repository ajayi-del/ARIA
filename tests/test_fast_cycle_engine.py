"""
tests/test_fast_cycle_engine.py — pins for the fast-cycle campaign brain.

Doctrine under test (locked 2026-09-26): $40 isolated pool, $8 margin per
trade, leverage 15-38x = cage band min symbol cap, cage ≥ 3.0, fee governor
net cost < $7.00 per $100k filled volume (strictly-greater breach), pool
debit/credit/rebuild accounting, fail-closed geometry, disabled = standdown
with zero state mutation.
"""
from types import SimpleNamespace

import pytest

from intelligence.fast_cycle_engine import (
    FastCycleEngine,
    EntryVerdict,
    LEVERAGE_CAPS,
    cage_ratio,
    leverage_for_cage,
    est_roundtrip_fee,
)


def cfg(**over):
    base = dict(fast_cycle_enabled=True)
    base.update(over)
    return SimpleNamespace(**base)


def pos(pool="fast_cycle", margin=8.0):
    return SimpleNamespace(pool=pool, initial_margin=margin)


# ── pure helpers ─────────────────────────────────────────────────────────────

class TestCageRatio:
    def test_long_3_to_1(self):
        assert cage_ratio(100.0, 98.0, 106.0) == pytest.approx(3.0)

    def test_short_3_to_1(self):
        assert cage_ratio(100.0, 102.0, 94.0) == pytest.approx(3.0)

    def test_wide_cage(self):
        assert cage_ratio(100.0, 99.0, 109.5) == pytest.approx(9.5)

    def test_stop_at_entry_zero_division(self):
        with pytest.raises(ZeroDivisionError):
            cage_ratio(100.0, 100.0, 106.0)


class TestLeverageForCage:
    def test_band_boundaries_btc(self):
        assert leverage_for_cage(3.0, "BTC") == 15
        assert leverage_for_cage(3.9, "BTC") == 15
        assert leverage_for_cage(4.0, "BTC") == 20
        assert leverage_for_cage(5.9, "BTC") == 20
        assert leverage_for_cage(6.0, "BTC") == 28
        assert leverage_for_cage(7.9, "BTC") == 28
        assert leverage_for_cage(8.0, "BTC") == 38
        assert leverage_for_cage(12.0, "BTC") == 38

    def test_symbol_cap_matrix(self):
        # cage ≥ 8.0 band is 38 everywhere; caps bind below it.
        # Re-encoded 2026-09-28: ETH cap 25→40 — Governor confirmed the new
        # SoDEX tier ("eth has 40x available on sodex... the doxs changed").
        assert leverage_for_cage(9.0, "ETH") == 38   # band 38 < cap 40
        assert leverage_for_cage(9.0, "XAUT") == 25
        assert leverage_for_cage(9.0, "USTECH100") == 25
        assert leverage_for_cage(9.0, "SOL") == 20
        assert leverage_for_cage(9.0, "SILVER") == 20
        assert leverage_for_cage(9.0, "US500") == 20
        assert leverage_for_cage(9.0, "CL") == 20
        assert leverage_for_cage(9.0, "COPPER") == 20

    def test_mid_band_with_cap(self):
        assert leverage_for_cage(5.0, "SOL") == 20   # band 20 == cap 20
        assert leverage_for_cage(7.0, "SOL") == 20   # band 28 → cap 20
        assert leverage_for_cage(7.0, "ETH") == 28   # band 28 < cap 40 (was 25)

    def test_unknown_symbol_none(self):
        # Re-encoded 2026-09-26: DOGE joined the 10x mid-cap tier
        # (cross-review P1) — FARTCOIN is the genuinely unmapped symbol.
        assert leverage_for_cage(9.0, "FARTCOIN") is None

    def test_symbol_normalization(self):
        assert leverage_for_cage(9.0, "BTC-USD") == 38
        assert leverage_for_cage(9.0, "btc") == 38
        assert leverage_for_cage(9.0, "USTECH100-USD") == 25

    def test_max_leverage_ceiling(self):
        # Governor 2026-09-27: "reduce leverage to 15x max" — flat cap over
        # the cage ladder; 0 = legacy ladder bit-for-bit.
        assert leverage_for_cage(12.0, "BTC", max_leverage=15) == 15
        assert leverage_for_cage(8.0, "BTC", max_leverage=15) == 15
        assert leverage_for_cage(3.0, "BTC", max_leverage=15) == 15
        assert leverage_for_cage(9.0, "SOL", max_leverage=15) == 15
        assert leverage_for_cage(9.0, "FARTCOIN", max_leverage=15) is None
        assert leverage_for_cage(12.0, "BTC", max_leverage=0) == 38

    def test_max_leverage_for_flat_fallback(self):
        from intelligence.fast_cycle_engine import max_leverage_for
        assert max_leverage_for(cfg(fast_cycle_max_leverage=20), "BTC-USD") == 20
        # No knob at all → getattr default 15 (legacy).
        assert max_leverage_for(cfg(), "BTC-USD") == 15

    def test_max_leverage_for_override_wins(self):
        from intelligence.fast_cycle_engine import max_leverage_for
        c = cfg(fast_cycle_max_leverage=20,
                fast_cycle_max_leverage_by_symbol="ETH-USD:40")
        assert max_leverage_for(c, "ETH-USD") == 40    # override wins upward
        assert max_leverage_for(c, "BTC-USD") == 20    # unlisted → flat
        # Normalization: key and symbol both reduce to the base asset.
        assert max_leverage_for(c, "eth") == 40

    def test_max_leverage_for_malformed_falls_back(self):
        from intelligence.fast_cycle_engine import max_leverage_for
        c = cfg(fast_cycle_max_leverage=20,
                fast_cycle_max_leverage_by_symbol="ETH-USD:notanumber,,BTC-USD:")
        assert max_leverage_for(c, "ETH-USD") == 20
        assert max_leverage_for(c, "BTC-USD") == 20

    def test_entry_verdict_eth_override_releases_ladder(self):
        # Governor 2026-09-28: BTC campaigns at 120×20 (flat cap binds),
        # ETH released up the cage ladder toward its new 40x SoDEX tier —
        # the by-symbol override beats the flat 20 cap; the band still
        # binds (cage ≥ 6 → 28 < 40).
        eng = FastCycleEngine()
        eth = eng.entry_verdict(
            cfg(fast_cycle_max_leverage=20,
                fast_cycle_max_leverage_by_symbol="ETH-USD:40",
                fast_cycle_margin_per_trade=55.0,
                fast_cycle_pool_usd=250.0),
            symbol="ETH-USD", side="long",
            entry_price=100.0, stop_price=99.0, tp_price=106.5,
            open_positions=[], now_ts=1_000_000.0)
        assert eth.action == "approve"
        assert eth.leverage == 28   # band 28 < cap 40 — flat 20 would clamp
        assert eth.notional_usd == 55.0 * 28
        btc = eng.entry_verdict(
            cfg(fast_cycle_max_leverage=20,
                fast_cycle_max_leverage_by_symbol="ETH-USD:40",
                fast_cycle_margin_per_trade=120.0,
                fast_cycle_pool_usd=250.0),
            symbol="BTC-USD", side="long",
            entry_price=100.0, stop_price=99.0, tp_price=106.5,
            open_positions=[], now_ts=1_000_001.0)
        assert btc.action == "approve"
        assert btc.leverage == 20   # flat cap still binds BTC
        assert btc.notional_usd == 120.0 * 20

    def test_entry_verdict_max_leverage_knob(self):
        eng = FastCycleEngine()
        v = eng.entry_verdict(
            cfg(fast_cycle_max_leverage=15,
                fast_cycle_margin_per_trade=55.0,
                fast_cycle_pool_usd=250.0),
            symbol="BTC-USD", side="long",
            entry_price=100.0, stop_price=99.0, tp_price=104.0,
            open_positions=[], now_ts=1_000_000.0)
        assert v.action == "approve"
        assert v.leverage == 15   # cage 4.0 → band 20, capped to 15
        assert v.notional_usd == 55.0 * 15


class TestEstRoundtripFee:
    def test_pure_maker_default(self):
        # 2 sides × $120 notional × 0.0114%
        assert est_roundtrip_fee(120.0, 0.000114, 0.00038) == pytest.approx(0.02736)

    def test_half_maker(self):
        # one maker side + one taker side
        assert est_roundtrip_fee(1000.0, 0.000114, 0.00038,
                                 maker_share=0.5) == pytest.approx(0.494)

    def test_pure_taker(self):
        assert est_roundtrip_fee(1000.0, 0.000114, 0.00038,
                                 maker_share=0.0) == pytest.approx(0.76)


# ── verdict branches ─────────────────────────────────────────────────────────

class TestVerdict:
    def verdict(self, engine, c=None, **kw):
        args = dict(symbol="BTC-USD", side="long", entry_price=100.0,
                    stop_price=98.0, tp_price=106.0, open_positions=[],
                    now_ts=1_800_000_000.0)
        args.update(kw)
        return engine.entry_verdict(c or cfg(), **args)

    def test_approve_fields(self):
        v = self.verdict(FastCycleEngine())
        assert v.action == "approve" and v.reason == "approved"
        assert v.margin_usd == 8.0
        assert v.leverage == 15                      # cage 3.0 → band 15
        assert v.notional_usd == pytest.approx(120.0)
        assert v.cage_ratio == pytest.approx(3.0)
        # Honest blend (cross-review P1): maker entry + 75% taker exit —
        # 2 × $120 × (0.25×0.000114 + 0.75×0.00038) = 0.07524. SoDEX stops
        # fire taker on trigger; the old maker-both-sides pin (0.02736)
        # understated the round trip ~2.8x.
        assert v.est_roundtrip_fee_usd == pytest.approx(0.07524)

    def test_disabled_standdown_no_mutation(self):
        e = FastCycleEngine()
        e.on_entry("BTC-USD", 8.0)
        e.on_close("BTC-USD", 4.0, 1.0, 0.05, 500.0)
        before = (e.debited, e.fee_gauge())
        v = self.verdict(e, c=cfg(fast_cycle_enabled=False))
        assert v.action == "standdown" and v.reason == "disabled"
        assert v.margin_usd == 0.0 and v.leverage == 0 and v.notional_usd == 0.0
        assert (e.debited, e.fee_gauge()) == before  # zero state mutation

    def test_unknown_symbol(self):
        # FARTCOIN is genuinely unmapped (DOGE joined the 10x tier 2026-09-26).
        v = self.verdict(FastCycleEngine(), symbol="FARTCOIN-USD")
        assert v.action == "standdown" and v.reason == "symbol_ineligible"

    def test_midcap_tier_symbols_eligible(self):
        # Cross-review P1: the campaign-intended 10x mid-caps are eligible
        # and clamp to the 10x exchange tier.
        v = self.verdict(FastCycleEngine(), symbol="DOGE-USD",
                         tp_price=110.0)  # cage 5 → band 20 → cap 10
        assert v.action == "approve" and v.leverage == 10

    def test_stop_at_entry_bad_geometry(self):
        v = self.verdict(FastCycleEngine(), stop_price=100.0)
        assert v.reason == "bad_geometry"

    def test_long_stop_above_entry_bad_geometry(self):
        v = self.verdict(FastCycleEngine(), stop_price=101.0, tp_price=106.0)
        assert v.reason == "bad_geometry"

    def test_short_stop_below_entry_bad_geometry(self):
        v = self.verdict(FastCycleEngine(), side="short",
                         stop_price=99.0, tp_price=94.0)
        assert v.reason == "bad_geometry"

    def test_short_valid_approves(self):
        v = self.verdict(FastCycleEngine(), side="short",
                         stop_price=102.0, tp_price=94.0)
        assert v.action == "approve" and v.leverage == 15

    def test_cage_below_min_bad_geometry(self):
        # cage = |104-100| / |98-100| = 2.0 < 3.0
        v = self.verdict(FastCycleEngine(), tp_price=104.0)
        assert v.reason == "bad_geometry"
        assert v.cage_ratio == pytest.approx(2.0)

    def test_cage_exactly_min_approves(self):
        v = self.verdict(FastCycleEngine())  # cage exactly 3.0
        assert v.action == "approve"

    def test_bad_side_string_fail_closed(self):
        v = self.verdict(FastCycleEngine(), side="flat")
        assert v.reason == "bad_geometry"

    def test_buy_sell_aliases(self):
        vl = self.verdict(FastCycleEngine(), side="buy")
        vs = self.verdict(FastCycleEngine(), side="sell",
                          stop_price=102.0, tp_price=94.0)
        assert vl.action == "approve" and vs.action == "approve"

    def test_fee_budget_breach(self):
        e = FastCycleEngine()
        # net = $1.00 on $10,000 volume = $10/100k > $7 budget; volume above
        # the 2026-10-02 $10k abstain floor (fast_cycle_fee_min_volume_usd).
        e.on_close("BTC-USD", 8.0, 0.0, 1.0, 5000.0)
        v = self.verdict(e)
        assert v.action == "standdown" and v.reason == "fee_budget_breach"

    def test_fee_budget_heals(self):
        e = FastCycleEngine()
        e.on_close("BTC-USD", 8.0, 0.0, 1.0, 5000.0)   # breach
        assert self.verdict(e).reason == "fee_budget_breach"
        # A $2.00 winner against the same volume pulls net cost negative.
        e.on_close("BTC-USD", 8.0, 2.0, 0.0, 5000.0)
        g = e.fee_gauge()
        assert g["budget_ok"] is True
        assert self.verdict(e).action == "approve"

    def test_fee_budget_exactly_7_is_ok(self):
        e = FastCycleEngine()
        # net $7.00 on $100,000 volume = exactly $7.00/100k — the breach is
        # strictly-greater, so the boundary is OK.
        e.on_close("BTC-USD", 8.0, 0.0, 7.0, 50000.0)
        assert e.fee_gauge()["net_cost_per_100k"] == 7.0
        assert e.fee_gauge()["budget_ok"] is True
        assert self.verdict(e).action == "approve"

    def test_fee_budget_just_over_7_breaches(self):
        e = FastCycleEngine()
        e.on_close("BTC-USD", 8.0, 0.0, 7.01, 50000.0)  # $7.01/100k
        assert e.fee_gauge()["budget_ok"] is False
        assert self.verdict(e).reason == "fee_budget_breach"

    def test_fee_gauge_zero_volume_ok(self):
        g = FastCycleEngine().fee_gauge(cfg())
        assert g["cumulative_volume_usd"] == 0.0
        assert g["net_cost_per_100k"] == 0.0
        assert g["budget_ok"] is True

    def test_concurrency_cap(self):
        e = FastCycleEngine()
        v = self.verdict(e, open_positions=[pos() for _ in range(6)])
        assert v.reason == "concurrency_cap"

    def test_concurrency_counts_only_this_pool(self):
        e = FastCycleEngine()
        others = [pos(pool="campaign") for _ in range(10)]
        assert self.verdict(e, open_positions=others).action == "approve"

    def test_pool_binds_before_concurrency(self):
        # 5 open × $8 = $40 debited → pool exhausted at 5 < 6 concurrency.
        e = FastCycleEngine()
        e.rebuild(40.0)
        v = self.verdict(e, open_positions=[pos() for _ in range(5)])
        assert v.reason == "pool_exhausted"

    def test_pool_nearly_exhausted(self):
        e = FastCycleEngine()
        e.rebuild(33.0)  # free $7 < $8
        assert self.verdict(e).reason == "pool_exhausted"

    def test_pool_free_exactly_margin_approves(self):
        e = FastCycleEngine()
        e.rebuild(32.0)  # free exactly $8
        assert self.verdict(e).action == "approve"

    def test_check_order_disabled_beats_unknown_symbol(self):
        v = self.verdict(FastCycleEngine(), c=cfg(fast_cycle_enabled=False),
                         symbol="NOPE-USD")
        assert v.reason == "disabled"

    def test_check_order_symbol_beats_geometry(self):
        v = self.verdict(FastCycleEngine(), symbol="NOPE-USD",
                         stop_price=100.0)
        assert v.reason == "symbol_ineligible"

    def test_check_order_geometry_beats_fee_breach(self):
        e = FastCycleEngine()
        e.on_close("BTC-USD", 8.0, 0.0, 1.0, 500.0)  # breached
        v = self.verdict(e, stop_price=100.0)
        assert v.reason == "bad_geometry"

    def test_check_order_fee_breach_beats_concurrency(self):
        e = FastCycleEngine()
        e.on_close("BTC-USD", 8.0, 0.0, 1.0, 5000.0)  # $10k vol: governor binds
        v = self.verdict(e, open_positions=[pos() for _ in range(6)])
        assert v.reason == "fee_budget_breach"

    def test_check_order_concurrency_beats_pool(self):
        e = FastCycleEngine()
        e.rebuild(39.0)  # pool would also bind — concurrency wins the order
        v = self.verdict(e, open_positions=[pos() for _ in range(6)])
        assert v.reason == "concurrency_cap"

    def test_custom_knobs(self):
        e = FastCycleEngine()
        c = cfg(fast_cycle_pool_usd=16.0, fast_cycle_margin_per_trade=8.0)
        e.rebuild(16.0)
        assert self.verdict(e, c=c).reason == "pool_exhausted"

    def test_custom_cage_min(self):
        v = self.verdict(FastCycleEngine(), c=cfg(fast_cycle_cage_min=4.0),
                         tp_price=107.0)  # cage 3.5
        assert v.reason == "bad_geometry"

    def test_custom_max_concurrent(self):
        v = self.verdict(FastCycleEngine(),
                         c=cfg(fast_cycle_max_concurrent=1),
                         open_positions=[pos()])
        assert v.reason == "concurrency_cap"


# ── pool accounting ──────────────────────────────────────────────────────────

class TestPoolAccounting:
    def test_debit_credit_cycle(self):
        e = FastCycleEngine()
        for _ in range(5):
            e.on_entry("BTC-USD", 8.0)
        assert e.debited == pytest.approx(40.0)
        e.on_close("BTC-USD", 8.0, 1.0, 0.03, 120.0)
        assert e.debited == pytest.approx(32.0)

    def test_credit_never_negative(self):
        e = FastCycleEngine()
        e.on_close("BTC-USD", 8.0, 0.0, 0.0, 100.0)  # nothing debited
        assert e.debited == 0.0

    def test_rebuild(self):
        e = FastCycleEngine()
        e.on_entry("BTC-USD", 8.0)
        e.rebuild(24.0)  # boot census: 3 positions still open
        assert e.debited == pytest.approx(24.0)

    def test_rebuild_clamps_negative(self):
        e = FastCycleEngine()
        e.rebuild(-5.0)
        assert e.debited == 0.0

    def test_on_close_returns_gauge(self):
        e = FastCycleEngine()
        g = e.on_close("BTC-USD", 8.0, 1.0, 0.05, 500.0)
        assert g["cumulative_volume_usd"] == pytest.approx(1000.0)
        assert g["cumulative_fees_usd"] == pytest.approx(0.05)
        assert g["cumulative_realized_pnl_usd"] == pytest.approx(1.0)
        # net = 0.05 − 1.00 = −0.95 → −$95/100k — profitable campaign.
        assert g["net_cost_per_100k"] == pytest.approx(-95.0)
        assert g["budget_ok"] is True

    def test_volume_counts_both_sides(self):
        e = FastCycleEngine()
        e.on_close("BTC-USD", 8.0, 0.0, 0.0, 250.0)
        assert e.fee_gauge()["cumulative_volume_usd"] == pytest.approx(500.0)


# ── Cross-review doctrine pins (2026-09-26) ─────────────────────────────────

class TestFeeFloorAndGovernor:
    def verdict(self, engine, c=None, **kw):
        args = dict(symbol="BTC-USD", side="long", entry_price=100.0,
                    stop_price=98.0, tp_price=106.0, open_positions=[],
                    now_ts=1_800_000_000.0)
        args.update(kw)
        return engine.entry_verdict(c or cfg(), **args)

    def test_fee_floor_standdown(self):
        # Cross-review P0: stop 0.1% < 3x round-trip rate (0.148%) — the fee
        # leg dominates EV regardless of cage. The Governor's 2026-09-23
        # winning-scalp-killed-by-fees class, gated at birth.
        v = self.verdict(FastCycleEngine(), stop_price=99.9, tp_price=100.3)
        assert v.action == "standdown" and v.reason == "fee_floor"
        assert v.cage_ratio == pytest.approx(3.0)

    def test_fee_floor_knob_respected(self):
        v = self.verdict(FastCycleEngine(),
                         c=cfg(fast_cycle_stop_fee_floor_mult=0.5),
                         stop_price=99.9, tp_price=100.3)
        assert v.action == "approve"

    def test_governor_abstains_below_min_volume(self):
        # Cross-review P0: tiny-denominator ratchet — one early stop-out on a
        # small cumulative volume prints a huge ratio; the gate must abstain
        # below fast_cycle_fee_min_volume_usd or the first loser permanently
        # freezes the campaign.
        e = FastCycleEngine()
        e.on_close("BTC-USD", 8.0, -5.0, 0.10, 120.0)  # net cost huge/100k
        v = self.verdict(e)
        assert v.action == "approve"          # volume $240 < $1k floor

    def test_governor_binds_after_restore(self):
        # Cross-review P0: restart amnesia — restore_counters feeds the
        # VolumeLedger's persisted campaign totals so the NET doctrine
        # survives restarts.
        e = FastCycleEngine()
        e.restore_counters(volume_usd=50000.0, fees_usd=30.0,
                           realized_pnl_usd=10.0)     # net $20 → $40/100k
        g = e.fee_gauge()
        assert g["budget_ok"] is False
        v = self.verdict(e)
        assert v.action == "standdown" and v.reason == "fee_budget_breach"

    def test_restore_then_heal(self):
        e = FastCycleEngine()
        e.restore_counters(volume_usd=50000.0, fees_usd=30.0,
                           realized_pnl_usd=10.0)
        e.on_close("BTC-USD", 8.0, 500.0, 0.10, 120.0)  # profit heals ratio
        assert e.fee_gauge()["budget_ok"] is True
