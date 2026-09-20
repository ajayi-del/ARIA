"""B2 pins (2026-09-19): post-boot salvo retracement filter.

Pins main.salvo_retracement_verdict — a fresh entry chasing > mult x ATR
past the last-4-closed-5m-bar extreme is rejected (peak-buy / trough-short).
Missing or degenerate data abstains (None = legacy).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402

# 4 closed bars, most recent last: (low, high)
BARS = [(100.0, 102.0), (99.0, 101.5), (98.0, 101.0), (97.0, 100.5)]


class TestSalvoRetracementVerdict:
    def test_long_chasing_retrace_rejected(self):
        # min(lows) = 97.0; threshold = 97.0 + 0.5*2.0 = 98.0; mark 99 > 98
        assert main.salvo_retracement_verdict("long", 99.0, 2.0, BARS, 0.5) == "reject"

    def test_long_at_the_lows_passes(self):
        # mark 97.5 <= 98.0 — buying the trough, not the peak
        assert main.salvo_retracement_verdict("long", 97.5, 2.0, BARS, 0.5) is None

    def test_short_chasing_retrace_rejected(self):
        # max(highs) = 102.0; threshold = 102.0 - 0.5*2.0 = 101.0; mark 100 < 101
        assert main.salvo_retracement_verdict("short", 100.0, 2.0, BARS, 0.5) == "reject"

    def test_short_at_the_highs_passes(self):
        assert main.salvo_retracement_verdict("short", 101.5, 2.0, BARS, 0.5) is None

    def test_thin_history_abstains(self):
        assert main.salvo_retracement_verdict("long", 99.0, 2.0, BARS[:3], 0.5) is None
        assert main.salvo_retracement_verdict("long", 99.0, 2.0, [], 0.5) is None
        assert main.salvo_retracement_verdict("long", 99.0, 2.0, None, 0.5) is None

    def test_degenerate_inputs_abstain(self):
        assert main.salvo_retracement_verdict("long", 0.0, 2.0, BARS, 0.5) is None
        assert main.salvo_retracement_verdict("long", 99.0, 0.0, BARS, 0.5) is None
        assert main.salvo_retracement_verdict("none", 99.0, 2.0, BARS, 0.5) is None
        assert main.salvo_retracement_verdict("long", "x", 2.0, BARS, 0.5) is None

    def test_boundary_is_not_rejected(self):
        # mark exactly at threshold = NOT beyond it
        assert main.salvo_retracement_verdict("long", 98.0, 2.0, BARS, 0.5) is None
        assert main.salvo_retracement_verdict("short", 101.0, 2.0, BARS, 0.5) is None
