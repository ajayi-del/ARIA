"""Unit tests for intelligence/arcus_scalper.py — pure brain, no I/O."""
import unittest

from intelligence.arcus_scalper import (FLOOR_RUNGS, FlowWindows,
                                        ScalpRegistry,
                                        band_edge_verdict,
                                        basket_harvest_verdict,
                                        expansion_fade_verdict,
                                        funding_carry_usd, leg_upnl_usd,
                                        leverage_cap_for,
                                        manage_scalp, pyramid_add_verdict,
                                        scalp_size,
                                        spread_capture_verdict,
                                        volume_projection)

NOW = 1_000_000.0
TICK = 0.01

# INTC-USD off-hours, pinned at the UPPER edge (mirrors test_band_clock's
# _OFF geometry: anchor 150.25, rung 0.5 → upper ≈ 151.0013 at mark 151.00).
_BAND_SHORT = {
    "tracked": True, "is_outside_rth": True, "rung": 0.5,
    "dist": {"upper_pct": 0.0009, "lower_pct": 1.0026},
    "expansion_imminent": {"upper": False, "lower": False},
    "anchor_reversion": {"armed": True, "direction": "short",
                         "anchor": 150.25, "room_pct": 0.4967,
                         "rung": 0.5, "reason": "at_upper_bound"},
    "session_bridge": {"valid": True, "gap_pct": 0.4983,
                       "direction": "long"},
}
_MARK_UPPER = 151.00
# BBO confirming the touch: mid 150.995 ≈ 0.004% from the edge.
_BOOK_UPPER = {"bid": 150.99, "ask": 151.00, "bid_qty": 50.0,
               "ask_qty": 40.0, "ts": NOW - 0.5}

_BAND_LONG = {
    "tracked": True, "is_outside_rth": True, "rung": 0.5,
    "dist": {"upper_pct": 1.0, "lower_pct": 0.0009},
    "expansion_imminent": {"upper": False, "lower": False},
    "anchor_reversion": {"armed": True, "direction": "long",
                         "anchor": 150.25, "room_pct": 0.5017,
                         "rung": 0.5, "reason": "at_lower_bound"},
}
_MARK_LOWER = 149.50
_BOOK_LOWER = {"bid": 149.50, "ask": 149.51, "bid_qty": 40.0,
               "ask_qty": 50.0, "ts": NOW - 0.5}


class TestBandEdge(unittest.TestCase):
    def test_arms_grade_a_tight_rung(self):
        v = band_edge_verdict(_BAND_SHORT, _MARK_UPPER, _BOOK_UPPER,
                              TICK, NOW)
        self.assertTrue(v["armed"])
        self.assertEqual(v["direction"], "short")
        self.assertEqual(v["grade"], "A")
        self.assertAlmostEqual(v["limit_price"], 150.99)
        self.assertAlmostEqual(v["stop"], 151.15)
        self.assertEqual(v["entry"], v["limit_price"])
        self.assertEqual(v["shadow"]["gate"], "band_edge_anchor_reversion")
        self.assertTrue(v["shadow"]["armed"])

    def test_grade_b_wide_rung(self):
        band = dict(_BAND_SHORT)
        band["anchor_reversion"] = dict(_BAND_SHORT["anchor_reversion"],
                                        rung=4.0)
        v = band_edge_verdict(band, _MARK_UPPER, _BOOK_UPPER, TICK, NOW)
        self.assertTrue(v["armed"])
        self.assertEqual(v["grade"], "B")

    def test_grade_b_thin_room(self):
        band = dict(_BAND_SHORT)
        band["anchor_reversion"] = dict(_BAND_SHORT["anchor_reversion"],
                                        room_pct=0.35)
        v = band_edge_verdict(band, _MARK_UPPER, _BOOK_UPPER, TICK, NOW)
        self.assertTrue(v["armed"])
        self.assertEqual(v["grade"], "B")

    def test_refused_when_anchor_reversion_dark(self):
        band = dict(_BAND_SHORT)
        band["anchor_reversion"] = {"armed": False, "reason": "mid_band"}
        v = band_edge_verdict(band, _MARK_UPPER, _BOOK_UPPER, TICK, NOW)
        self.assertFalse(v["armed"])
        self.assertEqual(v["reason"], "anchor_reversion_not_armed")
        self.assertFalse(v["shadow"]["armed"])

    def test_refused_stale_book(self):
        stale = dict(_BOOK_UPPER, ts=NOW - 30.0)
        v = band_edge_verdict(_BAND_SHORT, _MARK_UPPER, stale, TICK, NOW)
        self.assertFalse(v["armed"])
        self.assertEqual(v["reason"], "no_book")

    def test_refused_bbo_not_at_edge(self):
        far = dict(_BOOK_UPPER, bid=150.00, ask=150.02)
        v = band_edge_verdict(_BAND_SHORT, _MARK_UPPER, far, TICK, NOW)
        self.assertFalse(v["armed"])
        self.assertEqual(v["reason"], "bbo_no_touch")

    def test_refused_would_cross(self):
        # bid lifted through our one-tick-inside limit → taker, refuse.
        crossed = dict(_BOOK_UPPER, bid=150.995, ask=151.00)
        v = band_edge_verdict(_BAND_SHORT, _MARK_UPPER, crossed, TICK, NOW)
        self.assertFalse(v["armed"])
        self.assertEqual(v["reason"], "would_cross")

    def test_arms_long_at_lower_edge(self):
        v = band_edge_verdict(_BAND_LONG, _MARK_LOWER, _BOOK_LOWER,
                              TICK, NOW)
        self.assertTrue(v["armed"])
        self.assertEqual(v["direction"], "long")
        self.assertEqual(v["grade"], "A")
        self.assertAlmostEqual(v["limit_price"], 149.51)
        self.assertAlmostEqual(v["stop"], 149.35)

    def test_refused_bad_tick(self):
        v = band_edge_verdict(_BAND_SHORT, _MARK_UPPER, _BOOK_UPPER,
                              0.0, NOW)
        self.assertFalse(v["armed"])
        self.assertEqual(v["reason"], "bad_mark_or_tick")


class TestSpreadCapture(unittest.TestCase):
    _BOOK = {"bid": 100.00, "ask": 100.02, "bid_qty": 500.0,
             "ask_qty": 100.0, "ts": NOW - 0.2}

    def test_arms_grade_a(self):
        v = spread_capture_verdict(self._BOOK, TICK, 0.6, NOW,
                                   intended_qty=100.0)
        self.assertTrue(v["armed"])
        self.assertEqual(v["direction"], "long")
        self.assertEqual(v["grade"], "A")
        self.assertAlmostEqual(v["limit_price"], 100.00)
        self.assertAlmostEqual(v["stop"], 99.90)

    def test_grade_b_depth(self):
        book = dict(self._BOOK, bid_qty=150.0)
        v = spread_capture_verdict(book, TICK, 0.6, NOW,
                                   intended_qty=100.0)
        self.assertEqual(v["grade"], "B")

    def test_grade_b_when_depth_unverifiable(self):
        v = spread_capture_verdict(self._BOOK, TICK, 0.6, NOW)
        self.assertTrue(v["armed"])
        self.assertEqual(v["grade"], "B")

    def test_grade_c_thin_book(self):
        book = dict(self._BOOK, bid_qty=50.0, ask_qty=30.0)
        v = spread_capture_verdict(book, TICK, 0.6, NOW,
                                   intended_qty=100.0)
        self.assertEqual(v["grade"], "C")

    def test_short_side_grade_a(self):
        book = dict(self._BOOK, bid_qty=100.0, ask_qty=500.0)
        v = spread_capture_verdict(book, TICK, -0.6, NOW,
                                   intended_qty=100.0)
        self.assertTrue(v["armed"])
        self.assertEqual(v["direction"], "short")
        self.assertEqual(v["grade"], "A")
        self.assertAlmostEqual(v["limit_price"], 100.02)
        self.assertAlmostEqual(v["stop"], 100.12)

    def test_refused_thin_spread(self):
        book = dict(self._BOOK, ask=100.01)
        v = spread_capture_verdict(book, TICK, 0.6, NOW,
                                   intended_qty=100.0)
        self.assertFalse(v["armed"])
        self.assertEqual(v["reason"], "spread_too_thin")

    def test_refused_flow_weak_or_dark(self):
        v = spread_capture_verdict(self._BOOK, TICK, 0.1, NOW,
                                   intended_qty=100.0)
        self.assertFalse(v["armed"])
        self.assertEqual(v["reason"], "flow_dark_or_weak")
        v2 = spread_capture_verdict(self._BOOK, TICK, None, NOW,
                                    intended_qty=100.0)
        self.assertFalse(v2["armed"])

    def test_refused_microprice_disagrees(self):
        book = dict(self._BOOK, bid_qty=100.0, ask_qty=500.0)
        v = spread_capture_verdict(book, TICK, 0.6, NOW,
                                   intended_qty=100.0)
        self.assertFalse(v["armed"])
        self.assertEqual(v["reason"], "microprice_disagrees")

    def test_refused_off_hours(self):
        v = spread_capture_verdict(self._BOOK, TICK, 0.6, NOW,
                                   intended_qty=100.0, is_outside_rth=True)
        self.assertFalse(v["armed"])
        self.assertEqual(v["reason"], "off_hours")


class TestExpansionFade(unittest.TestCase):
    _BAND_EXP = {
        "tracked": True, "is_outside_rth": True, "rung": 0.5,
        "dist": {"upper_pct": 0.2, "lower_pct": 1.5},
        "expansion_imminent": {"upper": True, "lower": False},
        "anchor_reversion": {"armed": True, "direction": "short",
                             "anchor": 150.25, "room_pct": 0.5,
                             "rung": 0.5, "reason": "at_upper_bound"},
    }
    _BOOK = {"bid": 151.28, "ask": 151.30, "bid_qty": 10.0,
             "ask_qty": 10.0, "ts": NOW - 0.2}

    def test_arms_grade_b_by_construction(self):
        v = expansion_fade_verdict(self._BAND_EXP, 151.00, self._BOOK,
                                   TICK, NOW)
        self.assertTrue(v["armed"])
        self.assertEqual(v["direction"], "short")
        self.assertEqual(v["grade"], "B")
        self.assertAlmostEqual(v["limit_price"], 151.29)
        self.assertAlmostEqual(v["stop"], 151.45)
        self.assertEqual(v["reason"], "expansion_preposition")

    def test_refused_no_clock(self):
        band = dict(self._BAND_EXP,
                    expansion_imminent={"upper": False, "lower": False})
        v = expansion_fade_verdict(band, 151.00, self._BOOK, TICK, NOW)
        self.assertFalse(v["armed"])
        self.assertEqual(v["reason"], "no_expansion_clock")

    def test_refused_not_pinned(self):
        band = dict(self._BAND_EXP,
                    dist={"upper_pct": 1.0, "lower_pct": 0.5})
        v = expansion_fade_verdict(band, 151.00, self._BOOK, TICK, NOW)
        self.assertFalse(v["armed"])
        self.assertEqual(v["reason"], "not_at_edge")

    def test_refused_no_anchor_room(self):
        band = dict(self._BAND_EXP)
        band["anchor_reversion"] = dict(self._BAND_EXP["anchor_reversion"],
                                        room_pct=0.0)
        v = expansion_fade_verdict(band, 151.00, self._BOOK, TICK, NOW)
        self.assertFalse(v["armed"])
        self.assertEqual(v["reason"], "no_anchor_room")


class TestSizing(unittest.TestCase):
    def test_leverage_caps(self):
        self.assertEqual(leverage_cap_for("maker", "isolated"), 15)
        self.assertEqual(leverage_cap_for("taker", "isolated"), 8)
        self.assertEqual(leverage_cap_for("maker", "cross"), 8)

    def test_size_15x_maker_class(self):
        s = scalp_size(equity_usd=100.0, risk_usd=1.0, entry=100.0,
                       stop=99.5, leverage=15)
        self.assertTrue(s["ok"])
        self.assertAlmostEqual(s["notional"], 200.0)
        self.assertAlmostEqual(s["margin"], 200.0 / 15, places=4)
        self.assertAlmostEqual(s["stop_frac"], 0.005)

    def test_size_margin_cap(self):
        s = scalp_size(equity_usd=100.0, risk_usd=50.0, entry=100.0,
                       stop=99.5, leverage=15)
        self.assertTrue(s["ok"])
        self.assertAlmostEqual(s["notional"], 1500.0)   # equity × 15 cap
        self.assertAlmostEqual(s["margin"], 100.0)

    def test_margin_pct_cap_binds(self):
        # Governor 2026-10-24: per-bracket margin ≤ equity × pct.
        # risk-based $400 > cap 100 × 0.10 × 15 = $150 → clamped.
        s = scalp_size(equity_usd=100.0, risk_usd=2.0, entry=100.0,
                       stop=99.5, leverage=15, max_margin_pct=0.10)
        self.assertTrue(s["ok"])
        self.assertAlmostEqual(s["notional"], 150.0)
        self.assertAlmostEqual(s["margin"], 10.0)
        self.assertEqual(s.get("capped_by"), "margin")

    def test_margin_pct_cap_idle_when_risk_smaller(self):
        # risk-based $100 < cap $150 → uncapped, no capped_by key.
        s = scalp_size(equity_usd=100.0, risk_usd=0.5, entry=100.0,
                       stop=99.5, leverage=15, max_margin_pct=0.10)
        self.assertTrue(s["ok"])
        self.assertAlmostEqual(s["notional"], 100.0)
        self.assertNotIn("capped_by", s)

    def test_margin_pct_cap_zero_is_legacy(self):
        # 0.0 disables the cap — pre-clamp bit-for-bit.
        s = scalp_size(equity_usd=100.0, risk_usd=50.0, entry=100.0,
                       stop=99.5, leverage=15, max_margin_pct=0.0)
        self.assertTrue(s["ok"])
        self.assertAlmostEqual(s["notional"], 1500.0)
        self.assertNotIn("capped_by", s)

    def test_margin_pct_cap_garbage_fail_open(self):
        s = scalp_size(equity_usd=100.0, risk_usd=50.0, entry=100.0,
                       stop=99.5, leverage=15, max_margin_pct="junk")
        self.assertTrue(s["ok"])
        self.assertAlmostEqual(s["notional"], 1500.0)

    def test_margin_pct_cap_below_min_notional(self):
        # Cap notional $1.50 < $5 venue min → standdown, capped_by visible.
        s = scalp_size(equity_usd=100.0, risk_usd=2.0, entry=100.0,
                       stop=99.5, leverage=15, max_margin_pct=0.001)
        self.assertFalse(s["ok"])
        self.assertEqual(s["reason"], "below_min_notional")
        self.assertEqual(s.get("capped_by"), "margin")

    def test_size_8x_taker_class(self):
        s = scalp_size(equity_usd=100.0, risk_usd=50.0, entry=100.0,
                       stop=99.5, leverage=8)
        self.assertTrue(s["ok"])
        self.assertAlmostEqual(s["notional"], 800.0)    # equity × 8 cap

    def test_min_notional_floor(self):
        s = scalp_size(equity_usd=100.0, risk_usd=0.01, entry=100.0,
                       stop=99.5, leverage=15)
        self.assertFalse(s["ok"])
        self.assertEqual(s["reason"], "below_min_notional")

    def test_degenerate_geometry_fail_closed(self):
        s = scalp_size(equity_usd=100.0, risk_usd=1.0, entry=100.0,
                       stop=100.0, leverage=15)
        self.assertFalse(s["ok"])
        self.assertEqual(s["reason"], "bad_geometry")

    def test_volume_projection_dynamic(self):
        p = volume_projection(20, 1500.0)
        self.assertAlmostEqual(p["daily_volume_usd"], 30000.0)
        self.assertEqual(p["trades_needed_for_target"], 67)
        self.assertAlmostEqual(p["share_of_target"], 0.3)
        p2 = volume_projection(0, 0.0)
        self.assertIsNone(p2["trades_needed_for_target"])

    def test_funding_carry(self):
        self.assertAlmostEqual(funding_carry_usd(1500.0, 0.0001), 0.15)
        self.assertIsNone(funding_carry_usd(1500.0, None))


class TestLifecycle(unittest.TestCase):
    _LONG = {"symbol": "INTC-USD", "direction": "long", "entry": 100.0,
             "stop": 99.5, "target": 100.6, "opened_ts": NOW - 100.0,
             "peak_fav": 100.0, "qty": 1.0}
    _BOOK = {"bid": 100.10, "ask": 100.12, "bid_qty": 10.0,
             "ask_qty": 10.0, "ts": NOW - 0.1}

    def test_hold(self):
        v = manage_scalp(self._LONG, self._BOOK, NOW, tick=TICK)
        self.assertEqual(v["action"], "hold")

    def test_time_stop(self):
        pos = dict(self._LONG, opened_ts=NOW - 241.0)
        v = manage_scalp(pos, self._BOOK, NOW, tick=TICK)
        self.assertEqual(v, {"action": "exit_market", "reason": "time_stop"})

    def test_stop_touch(self):
        book = dict(self._BOOK, bid=99.49)
        v = manage_scalp(self._LONG, book, NOW, tick=TICK)
        self.assertEqual(v, {"action": "exit_market", "reason": "stop"})

    def test_breakeven_floor(self):
        # 2026-10-29 re-encode: floor breach exits AT MARKET — the legacy
        # PostOnly limit AT entry was marketable by construction, every
        # floor exit rejected POST_ONLY_WOULD_CROSS and the phantom-close
        # loop stacked PUMP to 3x intended size.
        pos = dict(self._LONG, peak_fav=100.05)   # first favorable tick seen
        book = dict(self._BOOK, bid=99.99)
        v = manage_scalp(pos, book, NOW, tick=TICK)
        self.assertEqual(v, {"action": "exit_market",
                             "reason": "breakeven_floor"})

    def test_floor_rung_locks_fraction_of_peak(self):
        # Rung 1: peak 3 ticks out → lock 40% → floor = entry + 0.4×0.03
        pos = dict(self._LONG, peak_fav=100.03)
        book = dict(self._BOOK, bid=100.011)   # ≤ 100.012 floor
        v = manage_scalp(pos, book, NOW, tick=TICK,
                         floor_rungs=FLOOR_RUNGS)
        self.assertEqual(v, {"action": "exit_market",
                             "reason": "profit_floor_rung"})

    def test_floor_rung_tightens_with_peak(self):
        # Rung 2: peak 6 ticks → lock 60% → floor = 100.036; a bid that
        # survives rung 1 (100.013 > 100.012) dies at rung 2.
        pos = dict(self._LONG, peak_fav=100.06)
        book = dict(self._BOOK, bid=100.013)
        v = manage_scalp(pos, book, NOW, tick=TICK,
                         floor_rungs=FLOOR_RUNGS)
        self.assertEqual(v, {"action": "exit_market",
                             "reason": "profit_floor_rung"})
        # ...but the same peak holds while the bid is above the rung-2 floor.
        book2 = dict(self._BOOK, bid=100.037)
        v2 = manage_scalp(pos, book2, NOW, tick=TICK,
                          floor_rungs=FLOOR_RUNGS)
        self.assertEqual(v2["action"], "hold")

    def test_floor_rung_short_mirror(self):
        short = {"symbol": "INTC-USD", "direction": "short",
                 "entry": 151.00, "stop": 151.15, "target": 150.50,
                 "opened_ts": NOW - 100.0, "peak_fav": 150.97, "qty": 1.0}
        # floor = 151.00 − 0.4×0.03 = 150.988
        book = {"bid": 150.985, "ask": 150.989, "bid_qty": 5.0,
                "ask_qty": 5.0, "ts": NOW - 0.1}
        v = manage_scalp(short, book, NOW, tick=TICK,
                         floor_rungs=FLOOR_RUNGS)
        self.assertEqual(v, {"action": "exit_market",
                             "reason": "profit_floor_rung"})

    def test_floor_rungs_off_is_flat_breakeven_floor(self):
        # Knob-off (None): even a deep peak only arms rung zero — legacy
        # breakeven floor, still at market.
        pos = dict(self._LONG, peak_fav=100.20)
        book = dict(self._BOOK, bid=100.05)   # through entry but far from peak
        v = manage_scalp(pos, book, NOW, tick=TICK, floor_rungs=None)
        self.assertEqual(v["action"], "hold")   # rung floor = entry, not hit
        book2 = dict(self._BOOK, bid=99.99)
        v2 = manage_scalp(pos, book2, NOW, tick=TICK, floor_rungs=None)
        self.assertEqual(v2, {"action": "exit_market",
                              "reason": "breakeven_floor"})

    def test_floor_not_armed_before_favorable_tick(self):
        book = dict(self._BOOK, bid=99.99)   # peak_fav == entry → no floor
        v = manage_scalp(self._LONG, book, NOW, tick=TICK)
        self.assertEqual(v["action"], "hold")

    def test_target_exit_limit(self):
        book = dict(self._BOOK, bid=100.61, ask=100.63)
        v = manage_scalp(self._LONG, book, NOW, tick=TICK)
        self.assertEqual(v, {"action": "exit_limit", "price": 100.6,
                             "reason": "target"})

    def test_adverse_band_event_aborts(self):
        short = {"symbol": "INTC-USD", "direction": "short",
                 "entry": 151.00, "stop": 151.15, "target": 150.50,
                 "opened_ts": NOW - 100.0, "peak_fav": 151.00, "qty": 1.0}
        book = {"bid": 150.90, "ask": 150.92, "bid_qty": 5.0,
                "ask_qty": 5.0, "ts": NOW - 0.1}
        ev = {"event": "tradingBoundExpanded", "side": "upper",
              "ts": NOW - 50.0}
        v = manage_scalp(short, book, NOW, tick=TICK, band_event=ev)
        self.assertEqual(v, {"action": "exit_market",
                             "reason": "band_ratchet_adverse"})

    def test_band_event_other_side_holds(self):
        short = {"symbol": "INTC-USD", "direction": "short",
                 "entry": 151.00, "stop": 151.15, "target": 150.50,
                 "opened_ts": NOW - 100.0, "peak_fav": 151.00, "qty": 1.0}
        book = {"bid": 150.90, "ask": 150.92, "bid_qty": 5.0,
                "ask_qty": 5.0, "ts": NOW - 0.1}
        ev = {"event": "tradingBoundExpanded", "side": "lower",
              "ts": NOW - 50.0}
        v = manage_scalp(short, book, NOW, tick=TICK, band_event=ev)
        self.assertEqual(v["action"], "hold")

    def test_fail_closed_on_dark_state(self):
        v = manage_scalp(None, self._BOOK, NOW, tick=TICK)
        self.assertEqual(v["action"], "hold")
        self.assertEqual(v["reason"], "insufficient_state")
        v2 = manage_scalp(self._LONG, None, NOW, tick=TICK)
        self.assertEqual(v2["reason"], "no_book")


class TestFlowWindows(unittest.TestCase):
    def test_imbalance_math(self):
        fw = FlowWindows(window_n=10, min_fills=4)
        for _ in range(3):
            fw.ingest_trade("BTC-USD", 100.0, 1.0, 1, True)
        fw.ingest_trade("BTC-USD", 100.0, 1.0, 2, False)
        self.assertAlmostEqual(fw.imbalance("BTC-USD"), 0.5)

    def test_dark_below_min_fills(self):
        fw = FlowWindows(window_n=10, min_fills=4)
        fw.ingest_trade("BTC-USD", 100.0, 1.0, 1, True)
        self.assertIsNone(fw.imbalance("BTC-USD"))

    def test_stale_direction_ignored(self):
        fw = FlowWindows(window_n=10, min_fills=1)
        self.assertFalse(fw.ingest_trade("BTC-USD", 100.0, 1.0, 1, None))
        self.assertIsNone(fw.imbalance("BTC-USD"))

    def test_window_trims(self):
        fw = FlowWindows(window_n=4, min_fills=4)
        for _ in range(3):
            fw.ingest_trade("BTC-USD", 100.0, 1.0, 1, True)
        for _ in range(3):
            fw.ingest_trade("BTC-USD", 100.0, 1.0, 2, False)
        # last 4: 1 buy + 3 sells → -0.5
        self.assertAlmostEqual(fw.imbalance("BTC-USD"), -0.5)


class TestRegistry(unittest.TestCase):
    def _rec(self, cls="band_edge"):
        return {"direction": "short", "cls": cls, "entry": 151.0,
                "stop": 151.15, "target": 150.5, "qty": 1.0,
                "notional": 151.0, "grade": "A"}

    def test_dedup_and_open(self):
        r = ScalpRegistry()
        ok, _ = r.can_open("INTC-USD", "band_edge", NOW)
        self.assertTrue(ok)
        self.assertTrue(r.register_open("INTC-USD", self._rec(), NOW))
        self.assertFalse(r.register_open("INTC-USD", self._rec(), NOW))
        ok, reason = r.can_open("INTC-USD", "band_edge", NOW)
        self.assertFalse(ok)
        self.assertEqual(reason, "symbol_open")

    def test_max_concurrent(self):
        r = ScalpRegistry(max_concurrent=3)
        for sym in ("A", "B", "C"):
            r.register_open(sym, self._rec(), NOW)
        ok, reason = r.can_open("D", "band_edge", NOW)
        self.assertFalse(ok)
        self.assertEqual(reason, "max_concurrent")

    def test_loss_cooloff(self):
        # Governor 2026-10-29: streak is 5, not 2 — two losses must NOT park.
        r = ScalpRegistry()
        for i in range(2):
            r.register_open("X", self._rec(), NOW + 2 * i)
            r.register_close("X", "loss", NOW + 2 * i + 1)
        self.assertIsNone(r.cooloff_until("X"))
        for i in range(3, 6):
            r.register_open("X", self._rec(), NOW + 2 * i)
            r.register_close("X", "loss", NOW + 2 * i + 1)
        self.assertEqual(r.cooloff_until("X"), NOW + 11 + 3600.0)
        ok, reason = r.can_open("X", "band_edge", NOW + 12)
        self.assertFalse(ok)
        self.assertEqual(reason, "cooloff")
        ok, _ = r.can_open("X", "band_edge", NOW + 3612.0)
        self.assertTrue(ok)

    def test_win_breaks_streak(self):
        r = ScalpRegistry()
        for oc in ("loss", "win", "loss"):
            r.register_open("X", self._rec(), NOW)
            r.register_close("X", oc, NOW)
        self.assertIsNone(r.cooloff_until("X"))

    def test_daily_class_counters(self):
        r = ScalpRegistry(max_concurrent=10)   # isolate the class-cap leg
        r.register_open("A", self._rec("band_edge"), NOW)
        r.register_open("B", self._rec("band_edge"), NOW)
        r.register_open("C", self._rec("spread_capture"), NOW)
        self.assertEqual(r.daily_count("band_edge", NOW), 2)
        self.assertEqual(r.daily_count("spread_capture", NOW), 1)
        self.assertEqual(r.daily_count("band_edge", NOW + 86400), 0)
        ok, reason = r.can_open("D", "band_edge", NOW, class_daily_cap=2)
        self.assertFalse(ok)
        self.assertEqual(reason, "class_daily_cap")

    def test_peak_tracking_and_manage(self):
        r = ScalpRegistry()
        rec = {"direction": "long", "cls": "spread_capture", "entry": 100.0,
               "stop": 99.5, "target": 100.6, "qty": 1.0, "grade": "A"}
        r.register_open("BTC-USD", rec, NOW - 100.0)
        r.note_price("BTC-USD", 100.05)   # floor arms
        book = {"bid": 99.99, "ask": 100.01, "bid_qty": 5.0,
                "ask_qty": 5.0, "ts": NOW - 0.1}
        v = r.manage("BTC-USD", book, NOW, tick=TICK)
        self.assertEqual(v["reason"], "breakeven_floor")
        v2 = r.manage("DARK-USD", book, NOW, tick=TICK)
        self.assertEqual(v2["reason"], "not_open")

    def test_manage_with_band_event(self):
        r = ScalpRegistry()
        r.register_open("INTC-USD", self._rec(), NOW - 100.0)
        r.ingest_band_event("INTC-USD", "tradingBoundExpanded", "upper",
                            NOW - 50.0)
        book = {"bid": 150.90, "ask": 150.92, "bid_qty": 5.0,
                "ask_qty": 5.0, "ts": NOW - 0.1}
        v = r.manage("INTC-USD", book, NOW, tick=TICK)
        self.assertEqual(v["reason"], "band_ratchet_adverse")

    # ── exit bookkeeping (phantom-close repair, 2026-10-29) ─────────────

    def test_exit_pending_short_circuits_manage(self):
        r = ScalpRegistry()
        r.register_open("INTC-USD", self._rec(), NOW - 100.0)
        self.assertEqual(r.exit_pending_since("INTC-USD"), None)
        self.assertTrue(r.mark_exit_pending("INTC-USD", NOW - 5.0,
                                            outcome="win"))
        book = {"bid": 150.90, "ask": 150.92, "bid_qty": 5.0,
                "ask_qty": 5.0, "ts": NOW - 0.1}
        v = r.manage("INTC-USD", book, NOW, tick=TICK)
        self.assertEqual(v, {"action": "hold", "reason": "exit_pending"})
        self.assertEqual(r.exit_pending_since("INTC-USD"), NOW - 5.0)
        self.assertEqual(r.position_state("INTC-USD")["exit_outcome"], "win")
        self.assertTrue(r.clear_exit_pending("INTC-USD"))
        self.assertEqual(r.exit_pending_since("INTC-USD"), None)
        self.assertNotIn("exit_outcome",
                         r.position_state("INTC-USD"))
        v2 = r.manage("INTC-USD", book, NOW, tick=TICK)
        self.assertNotEqual(v2.get("reason"), "exit_pending")

    def test_exit_pending_unknown_symbol(self):
        r = ScalpRegistry()
        self.assertFalse(r.mark_exit_pending("GHOST", NOW))
        self.assertFalse(r.clear_exit_pending("GHOST"))
        self.assertIsNone(r.exit_pending_since("GHOST"))

    def test_resync_qty(self):
        r = ScalpRegistry()
        r.register_open("INTC-USD", self._rec(), NOW)
        self.assertTrue(r.resync_qty("INTC-USD", 0.6))
        self.assertEqual(r.position_state("INTC-USD")["qty"], 0.6)
        self.assertFalse(r.resync_qty("INTC-USD", 0.0))
        self.assertFalse(r.resync_qty("GHOST", 1.0))
        self.assertEqual(r.position_state("INTC-USD")["qty"], 0.6)


if __name__ == "__main__":
    unittest.main()


class TestForeignFlowFirewall(unittest.TestCase):
    """2026-11-01 PUMP/W spam class: netting heals must park the symbol —
    the scratch close otherwise breaks the loss streak and the loop
    re-enters against the operator's manual position every ~15s
    (REDUCE_ONLY_WOULD_INCREASE, ~48k rejects/8.7h). Governor order
    "fix and deploy"."""

    def test_arm_cooloff_blocks_entry(self):
        r = ScalpRegistry()
        r.register_open("PUMP-USD", {"direction": "long", "cls": "spread_capture",
                                     "entry": 1.0, "stop": 0.9, "target": None,
                                     "qty": 10.0, "notional": 10.0,
                                     "grade": "B"}, NOW)
        r.register_close("PUMP-USD", "scratch", NOW + 10)  # heal outcome
        ok, _ = r.can_open("PUMP-USD", "spread_capture", NOW + 20)
        self.assertTrue(ok)  # legacy: scratch breaks the streak, no cooloff
        r.register_open("PUMP-USD", {"direction": "long", "cls": "spread_capture",
                                     "entry": 1.0, "stop": 0.9, "target": None,
                                     "qty": 10.0, "notional": 10.0,
                                     "grade": "B"}, NOW + 20)
        r.register_close("PUMP-USD", "scratch", NOW + 30)
        r.arm_cooloff("PUMP-USD", NOW + 30)  # firewall arm at the heal site
        ok, reason = r.can_open("PUMP-USD", "spread_capture", NOW + 40)
        self.assertFalse(ok)
        self.assertEqual(reason, "cooloff")

    def test_arm_cooloff_expires(self):
        r = ScalpRegistry(cooloff_s=3600.0)
        r.arm_cooloff("W-USD", NOW)
        ok, _ = r.can_open("W-USD", "spread_capture", NOW + 3599)
        self.assertFalse(ok)
        ok, _ = r.can_open("W-USD", "spread_capture", NOW + 3601)
        self.assertTrue(ok)

    def test_arm_cooloff_clears_loss_chain(self):
        r = ScalpRegistry()
        for _ in range(4):
            r.register_open("PUMP-USD", {"direction": "long",
                                         "cls": "spread_capture", "entry": 1.0,
                                         "stop": 0.9, "target": None,
                                         "qty": 10.0, "notional": 10.0,
                                         "grade": "B"}, NOW)
            r.register_close("PUMP-USD", "loss", NOW + 5)
        r.arm_cooloff("PUMP-USD", NOW + 10)
        # Chain cleared: after the cooloff lapses, one more loss must not
        # re-trigger the streak cooloff from the pre-firewall losses.
        r.register_open("PUMP-USD", {"direction": "long", "cls": "spread_capture",
                                     "entry": 1.0, "stop": 0.9, "target": None,
                                     "qty": 10.0, "notional": 10.0,
                                     "grade": "B"}, NOW + 4000)
        r.register_close("PUMP-USD", "loss", NOW + 4005)
        # The only cooloff standing is the firewall arm (NOW+10+3600) --
        # the streak counter did not re-arm from the pre-firewall losses.
        self.assertEqual(r.cooloff_until("PUMP-USD"), NOW + 10 + 3600.0)

    def test_config_knob_default_on(self):
        from core.config import Settings
        self.assertTrue(Settings().arcus_scalp_foreign_firewall_enabled)


class TestEntryPending(unittest.TestCase):
    """Leg A (2026-11-02): a resting PostOnly is NOT a position. The track
    opens only on a venue-confirmed fill; pending records occupy capacity;
    timeout cancels + expires clean (no close, no cooloff)."""

    def _pend(self, kind="entry", order_id="abc", qty=1.0, entry=100.0):
        return {"direction": "long", "cls": "spread_capture",
                "entry": entry, "stop": 99.5, "target": None, "qty": qty,
                "notional": qty * entry, "grade": "A", "leverage": 5,
                "order_id": order_id, "kind": kind}

    def test_parks_and_blocks_can_open(self):
        r = ScalpRegistry()
        self.assertTrue(r.register_entry_pending("INTC-USD",
                                                 self._pend(), NOW))
        self.assertFalse(r.is_open("INTC-USD"))
        ok, why = r.can_open("INTC-USD", "spread_capture", NOW)
        self.assertFalse(ok)
        self.assertEqual(why, "entry_pending")
        self.assertEqual(r.pending_count(), 1)
        self.assertEqual(r.entry_pending_since("INTC-USD"), NOW)
        self.assertEqual(r.entry_pending("INTC-USD")["order_id"], "abc")

    def test_pending_occupies_capacity(self):
        r = ScalpRegistry(max_concurrent=1)
        r.register_entry_pending("A-USD", self._pend(), NOW)
        ok, why = r.can_open("B-USD", "spread_capture", NOW)
        self.assertFalse(ok)
        self.assertEqual(why, "max_concurrent")

    def test_pending_does_not_count_toward_daily_class_cap(self):
        r = ScalpRegistry()
        r.register_entry_pending("A-USD", self._pend(), NOW)
        ok, _ = r.can_open("B-USD", "spread_capture", NOW,
                           class_daily_cap=1)
        self.assertTrue(ok)   # no fill yet → the counter is untouched
        r.confirm_entry("A-USD", 1.0, 100.0, NOW + 1)
        ok, why = r.can_open("B-USD", "spread_capture", NOW + 2,
                             class_daily_cap=1)
        self.assertFalse(ok)
        self.assertEqual(why, "class_daily_cap")

    def test_refused_when_open_and_double_pending(self):
        r = ScalpRegistry()
        r.register_open("INTC-USD", {"direction": "long",
                                     "cls": "spread_capture", "entry": 100.0,
                                     "stop": 99.5, "target": None, "qty": 1.0,
                                     "notional": 100.0, "grade": "A"}, NOW)
        self.assertFalse(r.register_entry_pending("INTC-USD",
                                                  self._pend(), NOW))
        r2 = ScalpRegistry()
        r2.register_entry_pending("INTC-USD", self._pend(), NOW)
        self.assertFalse(r2.register_entry_pending("INTC-USD",
                                                   self._pend(), NOW + 1))

    def test_add_kind_requires_open_base(self):
        r = ScalpRegistry()
        self.assertFalse(r.register_entry_pending(
            "INTC-USD", self._pend(kind="add"), NOW))     # no base track
        r.register_open("INTC-USD", {"direction": "long",
                                     "cls": "spread_capture", "entry": 100.0,
                                     "stop": 99.5, "target": None, "qty": 1.0,
                                     "notional": 100.0, "grade": "A"}, NOW)
        self.assertTrue(r.register_entry_pending(
            "INTC-USD", self._pend(kind="add", qty=0.5, entry=101.0),
            NOW + 1))

    def test_confirm_opens_on_venue_truth(self):
        r = ScalpRegistry()
        r.register_entry_pending("INTC-USD", self._pend(), NOW)
        # Venue shows a SMALLER fill at a BETTER price than the intent —
        # the track opens on venue qty/entry, never on intended geometry.
        self.assertTrue(r.confirm_entry("INTC-USD", 0.8, 99.98, NOW + 2))
        self.assertTrue(r.is_open("INTC-USD"))
        st = r.position_state("INTC-USD")
        self.assertEqual(st["qty"], 0.8)
        self.assertEqual(st["entry"], 99.98)
        self.assertAlmostEqual(st["notional"], 0.8 * 99.98)
        self.assertEqual(st["opened_ts"], NOW + 2)
        self.assertIsNone(r.entry_pending("INTC-USD"))
        self.assertNotIn("order_id", st)
        self.assertNotIn("kind", st)

    def test_confirm_bad_qty_leaves_pending(self):
        r = ScalpRegistry()
        r.register_entry_pending("INTC-USD", self._pend(), NOW)
        self.assertFalse(r.confirm_entry("INTC-USD", 0.0, 100.0, NOW + 1))
        self.assertFalse(r.confirm_entry("INTC-USD", -1.0, 100.0, NOW + 1))
        self.assertFalse(r.is_open("INTC-USD"))
        self.assertEqual(r.pending_count(), 1)   # retry next wake

    def test_confirm_unknown_symbol(self):
        r = ScalpRegistry()
        self.assertFalse(r.confirm_entry("GHOST", 1.0, 100.0, NOW))

    def test_cancel_returns_record_no_close_no_cooloff(self):
        r = ScalpRegistry()
        r.register_entry_pending("INTC-USD", self._pend(), NOW)
        rec = r.cancel_entry_pending("INTC-USD")
        self.assertEqual(rec["order_id"], "abc")
        self.assertIsNone(r.cancel_entry_pending("INTC-USD"))
        self.assertFalse(r.is_open("INTC-USD"))
        self.assertEqual(r.cooloff_until("INTC-USD"), None)
        ok, _ = r.can_open("INTC-USD", "spread_capture", NOW)
        self.assertTrue(ok)   # an unfilled entry is not a loss

    def test_confirm_add_folds_vwap(self):
        r = ScalpRegistry()
        r.register_open("INTC-USD", {"direction": "long",
                                     "cls": "spread_capture", "entry": 100.0,
                                     "stop": 99.5, "target": None, "qty": 1.0,
                                     "notional": 100.0, "grade": "A"}, NOW)
        r.register_entry_pending(
            "INTC-USD", self._pend(kind="add", qty=0.5, entry=101.0),
            NOW + 1)
        self.assertTrue(r.confirm_entry("INTC-USD", 0.5, 101.0, NOW + 2))
        st = r.position_state("INTC-USD")
        self.assertEqual(st["qty"], 1.5)
        self.assertAlmostEqual(st["entry"], (100.0 + 0.5 * 101.0) / 1.5)
        self.assertEqual(st["adds_done"], 1)
        self.assertIsNone(r.entry_pending("INTC-USD"))


class TestChurnGuards(unittest.TestCase):
    """2026-11-04 PUMP census: 36 stop-losses/day, 2-8s stop→re-entry gaps,
    direction flipping — and the 5-consecutive-loss park NEVER engaged
    because scratch/win interleave resets the chain. Rolling-window budget
    + re-entry floor + flip block close the hole."""

    def _loss(self, r, sym, ts, direction="long"):
        r.register_open(sym, {"direction": direction, "cls": "spread_capture",
                              "entry": 1.0, "stop": 0.9, "target": None,
                              "qty": 10.0, "notional": 10.0,
                              "grade": "B"}, ts)
        r.register_close(sym, "loss", ts)

    def test_hourly_budget_blocks_after_n_losses(self):
        r = ScalpRegistry(hourly_loss_budget=3)
        for i in range(3):
            self._loss(r, "PUMP-USD", NOW + i * 600)
        ok, why = r.can_open("PUMP-USD", "spread_capture", NOW + 1800)
        self.assertFalse(ok)
        self.assertEqual(why, "hourly_loss_budget")

    def test_budget_ignores_scratch_and_win_interleave(self):
        """The streak park resets on scratch/win — the rolling budget does
        not. This is the exact PUMP 2026-11-04 defeat pattern."""
        r = ScalpRegistry(hourly_loss_budget=2)
        self._loss(r, "PUMP-USD", NOW)
        r.register_open("PUMP-USD", {"direction": "long",
                                     "cls": "spread_capture", "entry": 1.0,
                                     "stop": 0.9, "target": None, "qty": 10.0,
                                     "notional": 10.0, "grade": "B"},
                        NOW + 100)
        r.register_close("PUMP-USD", "scratch", NOW + 100)  # breaks streak
        r.register_open("PUMP-USD", {"direction": "long",
                                     "cls": "spread_capture", "entry": 1.0,
                                     "stop": 0.9, "target": None, "qty": 10.0,
                                     "notional": 10.0, "grade": "B"},
                        NOW + 200)
        r.register_close("PUMP-USD", "win", NOW + 200)      # breaks streak
        self._loss(r, "PUMP-USD", NOW + 300)
        # streak chain is empty, but the window still holds 2 losses
        ok, why = r.can_open("PUMP-USD", "spread_capture", NOW + 400)
        self.assertFalse(ok)
        self.assertEqual(why, "hourly_loss_budget")

    def test_budget_rolls_off(self):
        r = ScalpRegistry(hourly_loss_budget=2, hourly_loss_window_s=3600.0)
        self._loss(r, "PUMP-USD", NOW)
        self._loss(r, "PUMP-USD", NOW + 10)
        ok, _ = r.can_open("PUMP-USD", "spread_capture", NOW + 3599)
        self.assertFalse(ok)
        ok, _ = r.can_open("PUMP-USD", "spread_capture", NOW + 3601)
        self.assertTrue(ok)

    def test_post_loss_reentry_floor(self):
        r = ScalpRegistry(hourly_loss_budget=0, post_loss_reentry_s=180.0)
        self._loss(r, "PUMP-USD", NOW)
        ok, why = r.can_open("PUMP-USD", "spread_capture", NOW + 8)
        self.assertFalse(ok)
        self.assertEqual(why, "post_loss_reentry")
        ok, _ = r.can_open("PUMP-USD", "spread_capture", NOW + 181)
        self.assertTrue(ok)

    def test_reentry_floor_only_after_loss(self):
        r = ScalpRegistry(hourly_loss_budget=0, post_loss_reentry_s=180.0)
        r.register_open("PUMP-USD", {"direction": "long",
                                     "cls": "spread_capture", "entry": 1.0,
                                     "stop": 0.9, "target": None, "qty": 10.0,
                                     "notional": 10.0, "grade": "B"}, NOW)
        r.register_close("PUMP-USD", "win", NOW)
        ok, _ = r.can_open("PUMP-USD", "spread_capture", NOW + 5)
        self.assertTrue(ok)

    def test_flip_block_same_direction_free(self):
        r = ScalpRegistry(hourly_loss_budget=0, post_loss_reentry_s=0.0,
                          flip_block_s=900.0)
        self._loss(r, "PUMP-USD", NOW, direction="long")
        ok, _ = r.can_open("PUMP-USD", "spread_capture", NOW + 10,
                           direction="long")
        self.assertTrue(ok)
        ok, why = r.can_open("PUMP-USD", "spread_capture", NOW + 10,
                             direction="short")
        self.assertFalse(ok)
        self.assertEqual(why, "flip_flop")
        ok, _ = r.can_open("PUMP-USD", "spread_capture", NOW + 901,
                           direction="short")
        self.assertTrue(ok)

    def test_guards_off_is_legacy_bit_for_bit(self):
        # 4 losses < LOSS_STREAK 5 → no legacy cooloff; with churn guards
        # OFF the close is immediately re-enterable in EITHER direction
        # (the exact PUMP churn pattern the guards exist to kill).
        r = ScalpRegistry(churn_guards_enabled=False)
        for i in range(4):
            self._loss(r, "PUMP-USD", NOW + i * 60)
        ok, _ = r.can_open("PUMP-USD", "spread_capture", NOW + 241,
                           direction="short")
        self.assertTrue(ok)

    def test_zero_knobs_disable_legs(self):
        r = ScalpRegistry(hourly_loss_budget=0, post_loss_reentry_s=0.0,
                          flip_block_s=0.0)
        self._loss(r, "PUMP-USD", NOW, direction="long")
        ok, _ = r.can_open("PUMP-USD", "spread_capture", NOW + 1,
                           direction="short")
        self.assertTrue(ok)

    def test_config_knob_defaults(self):
        from core.config import Settings
        s = Settings()
        self.assertTrue(s.arcus_scalp_churn_guards_enabled)
        self.assertEqual(s.arcus_scalp_hourly_loss_budget, 3)
        self.assertEqual(s.arcus_scalp_post_loss_reentry_s, 180.0)
        self.assertEqual(s.arcus_scalp_flip_block_s, 900.0)


class TestRegisterAdd(unittest.TestCase):
    """Pyramid add bookkeeping: VWAP re-anchor, qty grows, adds_done."""

    def _open(self, r):
        r.register_open("INTC-USD", {"direction": "long",
                                     "cls": "spread_capture", "entry": 100.0,
                                     "stop": 99.5, "target": None, "qty": 2.0,
                                     "notional": 200.0, "grade": "A"}, NOW)

    def test_vwap_reanchor(self):
        r = ScalpRegistry()
        self._open(r)
        self.assertTrue(r.register_add("INTC-USD", 1.0, 104.0, NOW + 5))
        st = r.position_state("INTC-USD")
        self.assertEqual(st["qty"], 3.0)
        self.assertAlmostEqual(st["entry"], (200.0 + 104.0) / 3.0)
        self.assertAlmostEqual(st["notional"], st["qty"] * st["entry"])
        self.assertEqual(st["adds_done"], 1)

    def test_refused_when_flat_or_bad_qty(self):
        r = ScalpRegistry()
        self.assertFalse(r.register_add("GHOST", 1.0, 100.0, NOW))
        self._open(r)
        self.assertFalse(r.register_add("INTC-USD", 0.0, 100.0, NOW))
        self.assertFalse(r.register_add("INTC-USD", -0.5, 100.0, NOW))
        self.assertEqual(r.position_state("INTC-USD")["qty"], 2.0)
        self.assertNotIn("adds_done", r.position_state("INTC-USD"))

    def test_none_price_keeps_entry(self):
        r = ScalpRegistry()
        self._open(r)
        self.assertTrue(r.register_add("INTC-USD", 1.0, None, NOW + 5))
        st = r.position_state("INTC-USD")
        self.assertEqual(st["qty"], 3.0)
        self.assertEqual(st["entry"], 100.0)


class TestFeeAwareFloor(unittest.TestCase):
    """Leg D (2026-11-02): the post-promo 6bp round-trip fee shifts the arm
    threshold AND every rung measure — raw-tick rungs priced the floor
    BELOW the fee and manufactured sub-fee scratches. fee_bps=0.0 must stay
    legacy bit-for-bit."""

    def _long(self, peak):
        return {"symbol": "INTC-USD", "direction": "long", "entry": 100.0,
                "stop": 90.0, "target": None, "opened_ts": NOW - 10.0,
                "peak_fav": peak, "qty": 1.0}

    def _book(self, bid):
        return {"bid": bid, "ask": bid + 0.02, "bid_qty": 5.0,
                "ask_qty": 5.0, "ts": NOW - 0.1}

    def test_fee_shifts_arm_threshold(self):
        # entry 100, tick 0.01, 6bps → fee_ticks 6, arm at +7 ticks.
        st = self._long(peak=100.02)   # 2 ticks: armed legacy, NOT armed
        v = manage_scalp(st, self._book(99.90), NOW, tick=TICK,
                         floor_rungs=FLOOR_RUNGS, fee_bps=6.0)
        self.assertEqual(v["action"], "hold")
        v0 = manage_scalp(st, self._book(99.90), NOW, tick=TICK,
                          floor_rungs=FLOOR_RUNGS, fee_bps=0.0)
        self.assertEqual(v0["reason"], "breakeven_floor")  # legacy armed

    def test_floor_sits_above_fee_line(self):
        # peak +10 ticks: net_fav = 10 − 6 = 4 ticks → rung 3 → lock 40%.
        # floor = entry + 6 ticks + 0.4 × 4 ticks = +7.6 ticks = 100.076.
        st = self._long(peak=100.10)
        v = manage_scalp(st, self._book(100.07), NOW, tick=TICK,
                         floor_rungs=FLOOR_RUNGS, fee_bps=6.0)
        self.assertEqual(v["action"], "exit_market")
        self.assertEqual(v["reason"], "profit_floor_rung")
        v_hold = manage_scalp(st, self._book(100.08), NOW, tick=TICK,
                              floor_rungs=FLOOR_RUNGS, fee_bps=6.0)
        self.assertEqual(v_hold["action"], "hold")
        # Same state at fee 0: net 10 ticks → rung 6 → lock 60% → floor
        # 100.06 → bid 100.07 does NOT breach → the fee flips the verdict.
        v0 = manage_scalp(st, self._book(100.07), NOW, tick=TICK,
                          floor_rungs=FLOOR_RUNGS, fee_bps=0.0)
        self.assertEqual(v0["action"], "hold")

    def test_rung_escalation_with_fee(self):
        # peak +16 ticks: net 10 → rung 6 → lock 60% → floor = 6 + 6 = 100.12
        st = self._long(peak=100.16)
        v = manage_scalp(st, self._book(100.11), NOW, tick=TICK,
                         floor_rungs=FLOOR_RUNGS, fee_bps=6.0)
        self.assertEqual(v["reason"], "profit_floor_rung")
        v_hold = manage_scalp(st, self._book(100.13), NOW, tick=TICK,
                              floor_rungs=FLOOR_RUNGS, fee_bps=6.0)
        self.assertEqual(v_hold["action"], "hold")

    def test_short_mirror(self):
        st = {"symbol": "INTC-USD", "direction": "short", "entry": 100.0,
              "stop": 110.0, "target": None, "opened_ts": NOW - 10.0,
              "peak_fav": 99.90, "qty": 1.0}
        # floor = 100 − 0.076 = 99.924; ask 99.93 ≥ floor → exit.
        book = {"bid": 99.91, "ask": 99.93, "bid_qty": 5.0, "ask_qty": 5.0,
                "ts": NOW - 0.1}
        v = manage_scalp(st, book, NOW, tick=TICK,
                         floor_rungs=FLOOR_RUNGS, fee_bps=6.0)
        self.assertEqual(v["reason"], "profit_floor_rung")

    def test_legacy_bit_for_bit_at_zero_fee(self):
        # The pre-change pins: peak 100.03 → floor 100.012 (rung 3, 40%).
        st = self._long(peak=100.03)
        v = manage_scalp(st, self._book(100.01), NOW, tick=TICK,
                         floor_rungs=FLOOR_RUNGS, fee_bps=0.0)
        self.assertEqual(v["reason"], "profit_floor_rung")
        v_hold = manage_scalp(st, self._book(100.02), NOW, tick=TICK,
                              floor_rungs=FLOOR_RUNGS, fee_bps=0.0)
        self.assertEqual(v_hold["action"], "hold")

    def test_registry_manage_passes_fee_through(self):
        r = ScalpRegistry()
        r.register_open("INTC-USD", {"direction": "long",
                                     "cls": "spread_capture", "entry": 100.0,
                                     "stop": 90.0, "target": None,
                                     "qty": 1.0, "notional": 100.0,
                                     "grade": "A"}, NOW - 10.0)
        r.note_price("INTC-USD", 100.10)
        v = r.manage("INTC-USD", self._book(100.07), NOW, tick=TICK,
                     floor_rungs=FLOOR_RUNGS, fee_bps=6.0)
        self.assertEqual(v["reason"], "profit_floor_rung")
        v0 = r.manage("INTC-USD", self._book(100.07), NOW, tick=TICK,
                      floor_rungs=FLOOR_RUNGS)   # default 0.0 → legacy
        self.assertEqual(v0["action"], "hold")


class TestBasketHarvest(unittest.TestCase):
    """Leg F (Governor 2026-11-02): the aggregate scalp-book uPnL net of
    fees banks the green legs at market when the book-ROE target clears."""

    def _leg(self, sym, direction, entry, qty, lev=5.0):
        return {"symbol": sym, "direction": direction, "entry": entry,
                "qty": qty, "notional": qty * entry, "leverage": lev}

    def _books(self, **kw):
        return {s: {"bid": b[0], "ask": b[1], "bid_qty": 5.0,
                    "ask_qty": 5.0, "ts": NOW - 0.1}
                for s, b in kw.items()}

    def test_empty_book_and_cooloff(self):
        v = basket_harvest_verdict([], {}, NOW)
        self.assertEqual(v["reason"], "empty_book")
        legs = [self._leg("A-USD", "long", 100.0, 2.0)]
        books = self._books(**{"A-USD": (102.0, 102.02)})
        v = basket_harvest_verdict(legs, books, NOW, last_harvest_ts=NOW - 10,
                                   cooloff_s=300.0)
        self.assertEqual(v["reason"], "cooloff")

    def test_dark_leg_fails_closed(self):
        legs = [self._leg("A-USD", "long", 100.0, 2.0),
                self._leg("B-USD", "short", 50.0, 4.0)]
        books = self._books(**{"A-USD": (102.0, 102.02)})  # B dark
        v = basket_harvest_verdict(legs, books, NOW)
        self.assertEqual(v["action"], "hold")
        self.assertEqual(v["reason"], "dark_leg")

    def test_below_min_profit(self):
        legs = [self._leg("A-USD", "long", 100.0, 0.05)]
        books = self._books(**{"A-USD": (101.0, 101.02)})  # +$0.05 net
        v = basket_harvest_verdict(legs, books, NOW, min_profit_usd=1.0)
        self.assertEqual(v["reason"], "below_min_profit")

    def test_below_target_roe(self):
        # +$3 net on $80 margin = 3.75% < 6% target.
        legs = [self._leg("A-USD", "long", 100.0, 2.0),
                self._leg("B-USD", "short", 50.0, 4.0)]
        books = self._books(**{"A-USD": (101.0, 101.02),
                               "B-USD": (49.73, 49.75)})
        v = basket_harvest_verdict(legs, books, NOW, fee_bps=0.0)
        self.assertEqual(v["reason"], "below_target")
        self.assertAlmostEqual(v["upnl_usd"], 3.0)
        self.assertAlmostEqual(v["roe_pct"], 3.75)

    def test_harvest_green_first(self):
        # +$5.30 net on $82 margin = 6.46% ≥ 6% → harvest, green legs only,
        # biggest bank first. (The red C leg stays open but its margin
        # still counts in the book ROE.)
        legs = [self._leg("A-USD", "long", 100.0, 2.0),
                self._leg("B-USD", "short", 50.0, 4.0),
                self._leg("C-USD", "long", 10.0, 1.0)]   # red leg stays
        books = self._books(**{"A-USD": (102.2, 102.22),    # +4.40
                               "B-USD": (49.73, 49.75),     # +1.00
                               "C-USD": (9.90, 9.92)})      # −0.10
        v = basket_harvest_verdict(legs, books, NOW, fee_bps=0.0)
        self.assertEqual(v["action"], "harvest")
        self.assertEqual(v["legs"], ["A-USD", "B-USD"])
        self.assertAlmostEqual(v["upnl_usd"], 5.3)
        self.assertAlmostEqual(v["roe_pct"], round(5.3 / 82.0 * 100.0, 4))

    def test_fee_drag_can_block_harvest(self):
        # Same book as below_target but bigger: +$5 gross, $400 notional →
        # 6bps fee drag $0.24 → net $4.76 → 5.95% < 6% → hold.
        legs = [self._leg("A-USD", "long", 100.0, 2.0),
                self._leg("B-USD", "short", 50.0, 4.0)]
        books = self._books(**{"A-USD": (102.0, 102.02),
                               "B-USD": (49.73, 49.75)})
        v = basket_harvest_verdict(legs, books, NOW, fee_bps=6.0)
        self.assertEqual(v["reason"], "below_target")
        self.assertAlmostEqual(v["upnl_usd"], 5.0 - 0.24)

    def test_leg_upnl_exit_side_and_dark(self):
        rec_l = {"direction": "long", "entry": 100.0, "qty": 2.0}
        rec_s = {"direction": "short", "entry": 100.0, "qty": 2.0}
        book = {"bid": 101.0, "ask": 101.02}
        self.assertAlmostEqual(leg_upnl_usd(rec_l, book), 2.0)   # at bid
        self.assertAlmostEqual(leg_upnl_usd(rec_s, book),
                               -2.04)                            # at ask
        self.assertIsNone(leg_upnl_usd(rec_l, None))
        self.assertIsNone(leg_upnl_usd({"direction": "long"}, book))


class TestPyramidAddVerdict(unittest.TestCase):
    """Leg F (Governor 2026-11-02: "as well as the pyramid"): the add arms
    only after the fee-adjusted first rung is proven, sizes off the BASE,
    and joins at the touch as a maker."""

    def _state(self, direction="long", peak=100.05, adds=0, qty=2.0):
        st = {"symbol": "INTC-USD", "direction": direction, "entry": 100.0,
              "stop": 99.5, "qty": qty, "notional": qty * 100.0,
              "peak_fav": peak, "adds_done": adds}
        return st

    _BOOK = {"bid": 100.20, "ask": 100.22, "bid_qty": 5.0, "ask_qty": 5.0,
             "ts": NOW - 0.1}

    def test_guards(self):
        self.assertEqual(pyramid_add_verdict(None, self._BOOK, NOW,
                                             tick=TICK)["reason"],
                         "insufficient_state")
        self.assertEqual(pyramid_add_verdict(self._state(adds=1),
                                             self._BOOK, NOW,
                                             tick=TICK)["reason"],
                         "max_adds")
        self.assertEqual(pyramid_add_verdict(self._state(), self._BOOK,
                                             NOW, tick=0.0)["reason"],
                         "bad_tick")
        self.assertEqual(pyramid_add_verdict(self._state(), None, NOW,
                                             tick=TICK)["reason"],
                         "no_book")
        st = self._state()
        del st["peak_fav"]
        self.assertEqual(pyramid_add_verdict(st, self._BOOK, NOW,
                                             tick=TICK)["reason"],
                         "no_peak")

    def test_floor_not_proven_below_rung1(self):
        # 2 ticks of excursion < rung1 3.0 → no add.
        v = pyramid_add_verdict(self._state(peak=100.02), self._BOOK, NOW,
                                tick=TICK, floor_rungs=FLOOR_RUNGS,
                                fee_bps=0.0)
        self.assertFalse(v["armed"])
        self.assertEqual(v["reason"], "floor_not_proven")

    def test_armed_joins_bid_off_base_qty(self):
        v = pyramid_add_verdict(self._state(peak=100.05), self._BOOK, NOW,
                                tick=TICK, floor_rungs=FLOOR_RUNGS,
                                fee_bps=0.0)
        self.assertTrue(v["armed"])
        self.assertEqual(v["direction"], "long")
        self.assertEqual(v["qty"], 1.0)              # 2.0 base × 0.5
        self.assertEqual(v["limit_price"], 100.20)   # long joins the bid
        self.assertEqual(v["reason"], "rung1_proven")

    def test_fee_raises_the_proof_bar(self):
        # 6bps on $100 = 6 ticks; rung1 3 → need 9 gross ticks. 5 < 9.
        v = pyramid_add_verdict(self._state(peak=100.05), self._BOOK, NOW,
                                tick=TICK, floor_rungs=FLOOR_RUNGS,
                                fee_bps=6.0)
        self.assertEqual(v["reason"], "floor_not_proven")
        v2 = pyramid_add_verdict(self._state(peak=100.10), self._BOOK, NOW,
                                 tick=TICK, floor_rungs=FLOOR_RUNGS,
                                 fee_bps=6.0)
        self.assertTrue(v2["armed"])

    def test_short_joins_ask(self):
        book = {"bid": 99.78, "ask": 99.80, "bid_qty": 5.0, "ask_qty": 5.0,
                "ts": NOW - 0.1}
        v = pyramid_add_verdict(self._state(direction="short", peak=99.95),
                                book, NOW, tick=TICK,
                                floor_rungs=FLOOR_RUNGS, fee_bps=0.0)
        self.assertTrue(v["armed"])
        self.assertEqual(v["limit_price"], 99.80)

    def test_dust_add(self):
        v = pyramid_add_verdict(self._state(), self._BOOK, NOW, tick=TICK,
                                add_frac=0.0, floor_rungs=FLOOR_RUNGS)
        self.assertEqual(v["reason"], "dust_add")


class TestConfigKnobs20261102(unittest.TestCase):
    def test_governor_knobs(self):
        from core.config import Settings
        c = Settings()
        self.assertEqual(c.arcus_scalp_max_concurrent, 13)   # Governor
        self.assertEqual(c.arcus_scalp_fee_roundtrip_bps, 6.0)
        self.assertEqual(c.arcus_scalp_entry_fill_timeout_s, 20.0)
        self.assertTrue(c.arcus_scalp_basket_enabled)
        self.assertEqual(c.arcus_scalp_basket_target_roe_pct, 6.0)
        self.assertEqual(c.arcus_scalp_basket_min_profit_usd, 1.0)
        self.assertEqual(c.arcus_scalp_basket_cooloff_s, 300.0)
        self.assertTrue(c.arcus_scalp_pyramid_enabled)
        self.assertEqual(c.arcus_scalp_pyramid_add_frac, 0.5)
        self.assertEqual(c.arcus_scalp_pyramid_max_adds, 1)
