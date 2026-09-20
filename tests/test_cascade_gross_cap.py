"""A2 pins (2026-09-19): cascade gross-notional cap.

Pins the module-level pure decisions in main.py:
  _cascade_gross_usd        — gross USD of live cascade-class (APEX/AFTERMATH)
                              positions; fail-open skips
  _cascade_gross_cap_blocked — cap<=0 = legacy (never blocks); unreadable
                              inputs fail open
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402


def _pos(entry, size, personality):
    return SimpleNamespace(entry_price=entry, size=size,
                           entry_personality=personality)


class TestCascadeGrossUsd:
    def test_apex_and_aftermath_counted(self):
        positions = [_pos(100.0, 1.0, "APEX"), _pos(50.0, 2.0, "AFTERMATH")]
        assert main._cascade_gross_usd(positions) == 200.0

    def test_standard_personalities_skipped(self):
        positions = [_pos(100.0, 1.0, "SCOUT"), _pos(100.0, 1.0, "FLOW"),
                     _pos(100.0, 1.0, "")]
        assert main._cascade_gross_usd(positions) == 0.0

    def test_unreadable_legs_fail_open(self):
        positions = [_pos("x", 1.0, "APEX"), _pos(100.0, None, "APEX"),
                     SimpleNamespace(entry_personality="APEX"),
                     _pos(100.0, 1.0, "APEX")]
        assert main._cascade_gross_usd(positions) == 100.0

    def test_empty_book(self):
        assert main._cascade_gross_usd([]) == 0.0


class TestCascadeGrossCapBlocked:
    def test_cap_zero_is_legacy(self):
        assert main._cascade_gross_cap_blocked(999999.0, 900.0, 0.0) is False

    def test_cap_negative_is_legacy(self):
        assert main._cascade_gross_cap_blocked(999999.0, 900.0, -5.0) is False

    def test_over_cap_blocks(self):
        # 1200 gross + 900 candidate = 2100 > 1800 cap
        assert main._cascade_gross_cap_blocked(1200.0, 900.0, 1800.0) is True

    def test_under_cap_passes(self):
        assert main._cascade_gross_cap_blocked(500.0, 900.0, 1800.0) is False

    def test_exactly_at_cap_passes(self):
        assert main._cascade_gross_cap_blocked(900.0, 900.0, 1800.0) is False

    def test_unreadable_inputs_fail_open(self):
        assert main._cascade_gross_cap_blocked("x", 900.0, 1800.0) is False
        assert main._cascade_gross_cap_blocked(1200.0, "x", 1800.0) is False
        assert main._cascade_gross_cap_blocked(1200.0, 900.0, "x") is False
        assert main._cascade_gross_cap_blocked(None, None, None) is False
