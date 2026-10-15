"""Unit tests for intelligence/arcus_scalper.py — pure brain, no I/O."""
import unittest

from intelligence.arcus_scalper import (FlowWindows, ScalpRegistry,
                                        band_edge_verdict,
                                        expansion_fade_verdict,
                                        funding_carry_usd, leverage_cap_for,
                                        manage_scalp, scalp_size,
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
        pos = dict(self._LONG, peak_fav=100.05)   # first favorable tick seen
        book = dict(self._BOOK, bid=99.99)
        v = manage_scalp(pos, book, NOW, tick=TICK)
        self.assertEqual(v, {"action": "exit_limit", "price": 100.0,
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
        r = ScalpRegistry()
        r.register_open("X", self._rec(), NOW)
        r.register_close("X", "loss", NOW + 1)
        r.register_open("X", self._rec(), NOW + 2)
        r.register_close("X", "loss", NOW + 3)
        self.assertEqual(r.cooloff_until("X"), NOW + 3 + 3600.0)
        ok, reason = r.can_open("X", "band_edge", NOW + 4)
        self.assertFalse(ok)
        self.assertEqual(reason, "cooloff")
        ok, _ = r.can_open("X", "band_edge", NOW + 3604.0)
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


if __name__ == "__main__":
    unittest.main()
