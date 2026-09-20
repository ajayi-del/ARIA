"""tests/test_spread_signal.py — pins for the spread-zscore shadow plane."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelligence.spread_signal import SpreadSignalTracker  # noqa: E402

SYM = "BTC-USD"


def _feed_flat(tr, n, spread, mid=100.0, ts0=0.0, step=1.0):
    for i in range(n):
        tr.update(SYM, spread, mid, ts0 + i * step)


def test_insufficient_samples_abstains():
    tr = SpreadSignalTracker(min_samples=60)
    _feed_flat(tr, 59, 1.0)
    assert tr.zscore(SYM, 60.0) is None


def test_unknown_symbol_abstains():
    tr = SpreadSignalTracker()
    assert tr.zscore("NOPE", 100.0) is None
    assert tr.widening_direction("NOPE", 100.0) is None


def test_spread_spike_scores_high():
    tr = SpreadSignalTracker(min_samples=60)
    _feed_flat(tr, 59, 1.0, ts0=0.0)
    tr.update(SYM, 10.0, 100.0, 59.0)
    z = tr.zscore(SYM, 59.0)
    assert z is not None and z > 2.0


def test_zero_variance_abstains():
    tr = SpreadSignalTracker(min_samples=60)
    _feed_flat(tr, 80, 2.0)
    assert tr.zscore(SYM, 100.0) is None


def test_window_pruning():
    tr = SpreadSignalTracker(window_s=100.0, min_samples=5)
    _feed_flat(tr, 201, 1.0, ts0=0.0, step=1.0)
    dq = tr._buf[SYM]
    assert len(dq) == 101            # ts 100..200 survive the cutoff
    assert dq[0][0] == 100.0


def test_pruned_history_leaves_window():
    """Old 10bps spreads fall out of the window; only the recent 1bps regime
    remains → zero variance → None (without pruning std > 0 and a z exists)."""
    tr = SpreadSignalTracker(window_s=100.0, min_samples=60)
    _feed_flat(tr, 60, 10.0, ts0=0.0)
    _feed_flat(tr, 60, 1.0, ts0=1000.0)
    assert tr.zscore(SYM, 1060.0) is None


def test_direction_rising_mid_is_ask():
    tr = SpreadSignalTracker()
    tr.update(SYM, 1.0, 100.0, 1000.0)
    tr.update(SYM, 1.0, 101.0, 1030.0)
    assert tr.widening_direction(SYM, 1030.0) == "ask"


def test_direction_falling_mid_is_bid():
    tr = SpreadSignalTracker()
    tr.update(SYM, 1.0, 100.0, 1000.0)
    tr.update(SYM, 1.0, 99.0, 1030.0)
    assert tr.widening_direction(SYM, 1030.0) == "bid"


def test_direction_needs_two_mids():
    tr = SpreadSignalTracker()
    tr.update(SYM, 1.0, 100.0, 1000.0)
    assert tr.widening_direction(SYM, 1000.0) is None


def test_direction_ignores_mids_older_than_60s():
    tr = SpreadSignalTracker()
    tr.update(SYM, 1.0, 50.0, 900.0)    # ancient — outside the 60s slice
    tr.update(SYM, 1.0, 100.0, 1000.0)
    assert tr.widening_direction(SYM, 1000.0) is None
