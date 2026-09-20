"""B4 pins (2026-09-19): post-TP trail tightening.

Pins main.trail_tp_tighten_dist — TP2 banked trails at tp2_mult x the
category distance, TP1 at tp1_mult x; tighten-only; disabled = legacy.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402


class TestTrailTpTighten:
    def test_disabled_is_legacy(self):
        assert main.trail_tp_tighten_dist(
            1.5, tp1_hit=True, tp2_hit=True, enabled=False,
            tp1_mult=0.75, tp2_mult=0.5) == 1.5

    def test_no_tp_banked_unchanged(self):
        assert main.trail_tp_tighten_dist(
            1.5, tp1_hit=False, tp2_hit=False, enabled=True,
            tp1_mult=0.75, tp2_mult=0.5) == 1.5

    def test_tp1_tightens(self):
        assert main.trail_tp_tighten_dist(
            2.0, tp1_hit=True, tp2_hit=False, enabled=True,
            tp1_mult=0.75, tp2_mult=0.5) == 1.5

    def test_tp2_tightens_harder_and_wins_over_tp1(self):
        assert main.trail_tp_tighten_dist(
            2.0, tp1_hit=True, tp2_hit=True, enabled=True,
            tp1_mult=0.75, tp2_mult=0.5) == 1.0

    def test_unreadable_distance_passes_through(self):
        assert main.trail_tp_tighten_dist(
            "x", tp1_hit=True, tp2_hit=False, enabled=True,
            tp1_mult=0.75, tp2_mult=0.5) == "x"
