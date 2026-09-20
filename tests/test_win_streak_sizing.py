"""C1 pins (2026-09-19): win-streak size decay.

Pins main.win_streak_size_mult — 0-1 wins -> 1.0; 2 -> 0.8; 3 -> 0.6;
4+ floored at 0.40. Never increases size; disabled = legacy 1.0.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402


class TestWinStreakSizeMult:
    def test_disabled_always_one(self):
        for w in (0, 1, 2, 5, 50):
            assert main.win_streak_size_mult(w, enabled=False) == 1.0

    def test_ladder(self):
        assert main.win_streak_size_mult(0, enabled=True) == 1.0
        assert main.win_streak_size_mult(1, enabled=True) == 1.0
        assert main.win_streak_size_mult(2, enabled=True) == 0.8
        assert main.win_streak_size_mult(3, enabled=True) == 0.6
        assert main.win_streak_size_mult(4, enabled=True) == 0.4

    def test_floor_at_long_streaks(self):
        assert main.win_streak_size_mult(10, enabled=True) == 0.4
        assert main.win_streak_size_mult(100, enabled=True) == 0.4

    def test_never_exceeds_one(self):
        for w in range(0, 12):
            assert main.win_streak_size_mult(w, enabled=True) <= 1.0

    def test_unreadable_counts_as_zero(self):
        assert main.win_streak_size_mult("x", enabled=True) == 1.0
        assert main.win_streak_size_mult(None, enabled=True) == 1.0
