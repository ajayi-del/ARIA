"""Unit tests for intelligence/mfe_guardian.py — pure brain, no I/O."""
import unittest

from intelligence.mfe_guardian import (DEFAULT_POLICY,
                                       ROUNDTRIP_MFE_THRESHOLD_PCT,
                                       MfeTracker, floor_verdict)

_E = 100.0  # entry


def _open(t, pid="p1", side="long", entry=_E, **kw):
    return t.on_open(pid, entry, side, stop=kw.pop("stop", entry * 0.99),
                     notional=kw.pop("notional", 500.0),
                     regime=kw.pop("regime", "range"),
                     day_type=kw.pop("day_type", "range"),
                     trade_class=kw.pop("trade_class", "swing"), **kw)


class TestLifecycle(unittest.TestCase):
    def test_open_registers(self):
        t = MfeTracker()
        self.assertTrue(_open(t))
        self.assertEqual(t.tracked(), 1)

    def test_open_rejects_degenerate(self):
        t = MfeTracker()
        self.assertFalse(t.on_open("x", 0.0, "long"))
        self.assertFalse(t.on_open("x", -5.0, "long"))
        self.assertFalse(t.on_open("x", 100.0, "sideways"))
        self.assertFalse(t.on_open("", 100.0, "long"))
        self.assertFalse(t.on_open("x", "junk", "long"))
        self.assertEqual(t.tracked(), 0)

    def test_duplicate_open_ignored(self):
        t = MfeTracker()
        _open(t)
        t.on_mark("p1", 101.0, now_s=10.0)
        self.assertFalse(_open(t, entry=200.0))   # never resets mid-flight
        self.assertAlmostEqual(t.mfe_for("p1"), 1.0)

    def test_restart_orphan_mark_ignored(self):
        t = MfeTracker()
        self.assertIsNone(t.on_mark("ghost", 101.0, now_s=1.0))
        self.assertEqual(t.tracked(), 0)

    def test_orphan_close_ignored(self):
        t = MfeTracker()
        self.assertIsNone(t.on_close("ghost", 99.0, "stop", now_s=1.0))

    def test_double_close_second_ignored(self):
        t = MfeTracker()
        _open(t)
        self.assertIsNotNone(t.on_close("p1", 99.0, "stop", now_s=5.0))
        self.assertIsNone(t.on_close("p1", 99.0, "stop", now_s=6.0))

    def test_bad_mark_ignored(self):
        t = MfeTracker()
        _open(t)
        self.assertIsNone(t.on_mark("p1", 0.0, now_s=1.0))
        self.assertIsNone(t.on_mark("p1", "junk", now_s=1.0))
        self.assertAlmostEqual(t.mfe_for("p1"), 0.0)


class TestMfeMaeTracking(unittest.TestCase):
    def test_mfe_peak_and_mae_trough_long(self):
        t = MfeTracker()
        _open(t)
        t.on_mark("p1", 101.0, now_s=1.0)
        t.on_mark("p1", 100.4, now_s=2.0)   # pullback — MFE holds the peak
        t.on_mark("p1", 99.5, now_s=3.0)
        self.assertAlmostEqual(t.mfe_for("p1"), 1.0)
        rec = t.on_close("p1", 100.1, "tp", now_s=4.0)
        self.assertAlmostEqual(rec["mfe_pct"], 1.0)
        self.assertAlmostEqual(rec["mae_pct"], -0.5)
        self.assertAlmostEqual(rec["realized_pct"], 0.1)
        self.assertAlmostEqual(rec["giveback_pct"], 0.9, places=3)

    def test_mfe_reached_at_s_updates_on_new_peak_only(self):
        t = MfeTracker()
        _open(t)
        t.on_mark("p1", 101.0, now_s=7.0)
        t.on_mark("p1", 100.5, now_s=8.0)
        t.on_mark("p1", 101.2, now_s=9.0)
        rec = t.on_close("p1", 101.0, "tp", now_s=10.0)
        self.assertEqual(rec["mfe_reached_at_s"], 9.0)

    def test_short_side_math(self):
        t = MfeTracker()
        _open(t, side="short")
        t.on_mark("p1", 99.0, now_s=1.0)    # +1.0% favorable
        t.on_mark("p1", 100.5, now_s=2.0)   # −0.5% adverse
        rec = t.on_close("p1", 99.5, "tp", now_s=3.0)
        self.assertAlmostEqual(rec["mfe_pct"], 1.0)
        self.assertAlmostEqual(rec["mae_pct"], -0.5)
        self.assertAlmostEqual(rec["realized_pct"], 0.5)


class TestRoundtripDetection(unittest.TestCase):
    def test_census_class_roundtrip(self):
        """The 91.6% class: MFE +0.8% → closed −0.1% = roundtrip, giveback 0.9."""
        t = MfeTracker()
        _open(t)
        t.on_mark("p1", _E * 1.008, now_s=1.0)
        rec = t.on_close("p1", _E * 0.999, "software_stop", now_s=2.0)
        self.assertTrue(rec["roundtrip"])
        self.assertAlmostEqual(rec["giveback_pct"], 0.9, places=3)

    def test_winner_not_roundtrip(self):
        t = MfeTracker()
        _open(t)
        t.on_mark("p1", _E * 1.008, now_s=1.0)
        rec = t.on_close("p1", _E * 1.004, "tp", now_s=2.0)
        self.assertFalse(rec["roundtrip"])
        self.assertAlmostEqual(rec["giveback_pct"], 0.4, places=3)

    def test_below_threshold_red_close_not_roundtrip(self):
        t = MfeTracker()
        _open(t)
        t.on_mark("p1", _E * 1.003, now_s=1.0)   # below the 0.5 threshold
        rec = t.on_close("p1", _E * 0.998, "stop", now_s=2.0)
        self.assertFalse(rec["roundtrip"])

    def test_short_roundtrip(self):
        t = MfeTracker()
        _open(t, side="short")
        t.on_mark("p1", _E * 0.992, now_s=1.0)   # +0.8% favorable
        rec = t.on_close("p1", _E * 1.001, "stop", now_s=2.0)
        self.assertTrue(rec["roundtrip"])
        self.assertAlmostEqual(rec["giveback_pct"], 0.9, places=3)

    def test_roundtrip_counters_keyed(self):
        t = MfeTracker()
        _open(t, pid="a", regime="range", day_type="range", trade_class="scout")
        t.on_mark("a", _E * 1.01, now_s=1.0)
        t.on_close("a", _E * 0.999, "stop", now_s=2.0)
        _open(t, pid="b", regime="trending", day_type="trend",
              trade_class="swing")
        t.on_mark("b", _E * 1.01, now_s=1.0)
        t.on_close("b", _E * 1.005, "tp", now_s=2.0)     # not a roundtrip
        c = t.counters()
        self.assertEqual(c["closes"], 2)
        self.assertEqual(c["roundtrips"], 1)
        self.assertEqual(c["roundtrips_by_key"][("range", "range", "scout")], 1)
        self.assertNotIn(("trending", "trend", "swing"), c["roundtrips_by_key"])


class TestFloorPolicy(unittest.TestCase):
    def test_scalp_boundaries(self):
        self.assertIsNone(floor_verdict(0.49, trade_class="scout"))
        self.assertAlmostEqual(floor_verdict(0.50, trade_class="scout"), 0.25)
        self.assertAlmostEqual(floor_verdict(0.99, trade_class="SCOUT"), 0.25)
        self.assertAlmostEqual(floor_verdict(1.00, trade_class="scalp"), 0.50)

    def test_trend_aligned_boundaries(self):
        self.assertIsNone(floor_verdict(0.99, day_type="trend",
                                        trade_class="swing"))
        self.assertAlmostEqual(floor_verdict(1.00, day_type="trend",
                                             trade_class="swing"), 0.25)
        self.assertAlmostEqual(floor_verdict(1.99, day_type="trend_aligned",
                                             trade_class="swing"), 0.25)
        self.assertAlmostEqual(floor_verdict(2.00, day_type="trend_day",
                                             trade_class="swing"), 0.50)

    def test_default_boundaries(self):
        self.assertIsNone(floor_verdict(0.74, day_type="range",
                                        trade_class="swing"))
        self.assertAlmostEqual(floor_verdict(0.75, day_type="range",
                                             trade_class="swing"), 0.25)
        self.assertAlmostEqual(floor_verdict(1.50, day_type="range",
                                             trade_class="swing"), 0.50)

    def test_scalp_beats_trend_bucket(self):
        # scout on a trend day still banks fast (class wins over day-type)
        self.assertAlmostEqual(floor_verdict(0.6, day_type="trend",
                                             trade_class="scout"), 0.25)

    def test_abstain_treasury_managed(self):
        self.assertIsNone(floor_verdict(5.0, trade_class="scalp",
                                        treasury_managed=True))

    def test_abstain_roe_ratchet_active(self):
        self.assertIsNone(floor_verdict(5.0, trade_class="scalp",
                                        roe_ratchet_active=True))

    def test_kill_switch_abstains_all(self):
        pol = dict(DEFAULT_POLICY, enabled=False)
        self.assertIsNone(floor_verdict(5.0, trade_class="scalp", policy=pol))

    def test_determinism(self):
        a = floor_verdict(0.8, regime="range", day_type="range",
                          trade_class="scout")
        b = floor_verdict(0.8, regime="range", day_type="range",
                          trade_class="scout")
        self.assertEqual(a, b)
        self.assertAlmostEqual(a, 0.25)


class TestFloorRegistry(unittest.TestCase):
    def test_raise_and_telemetry_record(self):
        t = MfeTracker()
        _open(t, trade_class="scout")
        rec = t.on_mark("p1", _E * 1.006, now_s=1.0)
        self.assertIsNotNone(rec)
        self.assertIsNone(rec["old_floor"])
        self.assertAlmostEqual(rec["new_floor"], _E * 1.0015)  # 25% of 0.6%
        self.assertAlmostEqual(rec["mfe_pct"], 0.6, places=3)
        self.assertEqual(rec["policy_rung"], 0.5)
        self.assertEqual(rec["policy_bucket"], "scalp")
        self.assertEqual(rec["regime"], "range")
        self.assertEqual(rec["day_type"], "range")
        self.assertAlmostEqual(t.floor_for("p1"), _E * 1.0015)

    def test_tighten_only_long_adversarial_marks(self):
        t = MfeTracker()
        _open(t, trade_class="scout")
        marks = [100.6, 100.2, 101.3, 100.1, 100.9, 101.0, 99.8, 101.1]
        prev = None
        for i, m in enumerate(marks):
            t.on_mark("p1", m, now_s=float(i))
            cur = t.floor_for("p1")
            if prev is not None and cur is not None:
                self.assertGreaterEqual(cur, prev)   # never loosens
            prev = cur if cur is not None else prev
        # peak 1.3% → rung 1.0 → floor at 50% of peak = +0.65%
        self.assertAlmostEqual(t.floor_for("p1"), _E * 1.0065)

    def test_tighten_only_short_adversarial_marks(self):
        t = MfeTracker()
        _open(t, side="short", trade_class="scout")
        marks = [99.4, 99.8, 98.7, 99.9, 99.0, 100.2, 98.9]
        prev = None
        for i, m in enumerate(marks):
            t.on_mark("p1", m, now_s=float(i))
            cur = t.floor_for("p1")
            if prev is not None and cur is not None:
                self.assertLessEqual(cur, prev)      # never loosens (short)
            prev = cur if cur is not None else prev
        # peak 1.3% → floor at entry −0.65%
        self.assertAlmostEqual(t.floor_for("p1"), _E * 0.9935)

    def test_no_raise_below_first_rung(self):
        t = MfeTracker()
        _open(t, trade_class="scout")
        self.assertIsNone(t.on_mark("p1", _E * 1.004, now_s=1.0))
        self.assertIsNone(t.floor_for("p1"))

    def test_mark_crossed_candidate_does_not_raise(self):
        t = MfeTracker()
        _open(t, trade_class="scalp")
        rec = t.on_mark("p1", _E * 1.006, now_s=1.0)   # installs +0.15% floor
        self.assertIsNotNone(rec)
        # Mark pulls THROUGH the would-be floor: candidate (from the peak)
        # is crossed → no raise emitted, and the installed floor STANDS
        # (tighten-only; the exit stack owns the position from here).
        self.assertIsNone(t.on_mark("p1", _E * 1.001, now_s=2.0))
        self.assertAlmostEqual(t.floor_for("p1"), _E * 1.0015)
        # Short mirror: floor installed at peak, mark bounces through it.
        _open(t, pid="p2", side="short", trade_class="scalp")
        self.assertIsNotNone(t.on_mark("p2", _E * 0.994, now_s=1.0))
        self.assertIsNone(t.on_mark("p2", _E * 0.999, now_s=2.0))
        self.assertAlmostEqual(t.floor_for("p2"), _E * 0.9985)

    def test_abstaining_position_never_floors(self):
        t = MfeTracker()
        _open(t, trade_class="scout", treasury_managed=True)
        self.assertIsNone(t.on_mark("p1", _E * 1.05, now_s=1.0))
        self.assertIsNone(t.floor_for("p1"))
        _open(t, pid="p2", trade_class="scout", roe_ratchet_active=True)
        self.assertIsNone(t.on_mark("p2", _E * 1.05, now_s=1.0))
        self.assertIsNone(t.floor_for("p2"))
        # MFE is still tracked for the census even while floors abstain
        self.assertAlmostEqual(t.mfe_for("p1"), 5.0)

    def test_close_record_carries_floor(self):
        t = MfeTracker()
        _open(t, trade_class="scout")
        t.on_mark("p1", _E * 1.006, now_s=1.0)
        rec = t.on_close("p1", _E * 1.002, "mfe_floor", now_s=2.0)
        self.assertAlmostEqual(rec["floor_at_close"], _E * 1.0015)
        self.assertEqual(rec["floor_rung"], 0.5)

    def test_unknown_id_floor_reads_none(self):
        t = MfeTracker()
        self.assertIsNone(t.floor_for("ghost"))
        self.assertIsNone(t.mfe_for("ghost"))
        self.assertIsNone(t.update_floor("ghost", 100.0))


if __name__ == "__main__":
    unittest.main()
