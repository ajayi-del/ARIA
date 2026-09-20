"""tests/test_kline_confidence.py — pins for the epistemic gate input."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelligence.kline_confidence import kline_confidence  # noqa: E402


def test_full_confidence():
    assert kline_confidence(55, 20, False, 10.0) == pytest.approx(1.0)


def test_overfull_buffer_capped():
    assert kline_confidence(200, None, False, None) == pytest.approx(1.0)


def test_low_buffer_scales_linearly():
    assert kline_confidence(11, None, False, None) == pytest.approx(11 / 55.0)


def test_quarantine_zeroes():
    assert kline_confidence(55, None, True, None) == 0.0


def test_below_min_candles_halves():
    assert kline_confidence(30, 40, False, None) == pytest.approx(30 / 55.0 * 0.5)


def test_at_min_candles_no_penalty():
    assert kline_confidence(40, 40, False, None) == pytest.approx(40 / 55.0)


def test_stale_mark_penalty():
    assert kline_confidence(55, None, False, 120.0) == pytest.approx(0.3)


def test_fresh_mark_no_penalty():
    assert kline_confidence(55, None, False, 90.0) == pytest.approx(1.0)


def test_abstain_on_none_buffer():
    assert kline_confidence(None, 20, True, 10.0) is None


def test_combined_multipliers():
    expected = 30 / 55.0 * 0.5 * 0.3
    assert kline_confidence(30, 40, False, 120.0) == pytest.approx(expected)
