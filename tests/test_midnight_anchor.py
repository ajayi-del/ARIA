"""Midnight-anchor day-move fix (2026-08-28).

The 1m CandleBuffer is 200 bars deep; after 03:20 UTC the true 00:00 bar
falls off and every from-midnight measurement silently read a trailing
3.33h window (live: TAO -0.15% measured vs -6.71% true). These tests pin
the anchor-cache behavior of day_move_elapsed_anchored plus the sodex
zero-entry guard.
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import day_move_elapsed_anchored  # noqa: E402

MID_MS = 1_800_000_000_000_000  # arbitrary midnight boundary
TODAY = MID_MS // 86_400_000
MIN = 60_000


def _candle(open_time, open_, close, close_time=None):
    return SimpleNamespace(open_time=open_time, open=open_, close=close,
                           close_time=close_time if close_time is not None
                           else open_time + MIN)


def _window(start_offset_min, n, open0=100.0, close_last=110.0):
    """n consecutive 1m candles starting start_offset_min after midnight."""
    cds = []
    for i in range(n):
        ot = MID_MS + (start_offset_min + i) * MIN
        cds.append(_candle(ot, open0 if i == 0 else 105.0,
                           close_last if i == n - 1 else 105.0))
    return cds


def test_anchor_cached_when_midnight_bar_visible():
    cache = {}
    move, elapsed = day_move_elapsed_anchored(
        _window(0, 200, open0=100.0, close_last=110.0), MID_MS, TODAY,
        cache, "TAO-USD")
    assert abs(move - 10.0) < 1e-9
    assert elapsed == 200 * 60.0  # last candle close_time
    assert cache["TAO-USD"] == (TODAY, 100.0, MID_MS)


def test_truncated_buffer_uses_same_day_anchor():
    cache = {"TAO-USD": (TODAY, 100.0, MID_MS)}
    # buffer now starts at 20:00 UTC — true midnight bar long gone
    move, elapsed = day_move_elapsed_anchored(
        _window(20 * 60, 200, close_last=94.0), MID_MS, TODAY,
        cache, "TAO-USD")
    assert abs(move - (-6.0)) < 1e-9                       # measured vs the anchor, not 105
    assert elapsed == (20 * 60 + 200) * 60.0  # full-day elapsed (close_time)


def test_truncated_buffer_no_anchor_fails_open_to_legacy_read():
    cache = {}
    cds = _window(20 * 60, 200, open0=105.0, close_last=110.0)
    move, _ = day_move_elapsed_anchored(cds, MID_MS, TODAY, cache, "TAO-USD")
    # legacy truncated behavior: measured off the first visible bar (105)
    assert abs(move - (110.0 / 105.0 - 1.0) * 100.0) < 1e-9


def test_yesterdays_anchor_ignored():
    cache = {"TAO-USD": (TODAY - 1, 100.0, MID_MS - 86_400_000)}
    cds = _window(20 * 60, 200, open0=105.0, close_last=110.0)
    move, _ = day_move_elapsed_anchored(cds, MID_MS, TODAY, cache, "TAO-USD")
    assert abs(move - (110.0 / 105.0 - 1.0) * 100.0) < 1e-9


def test_thirty_minute_tolerance_reanchors():
    cache = {}
    # first visible bar 25 min after midnight → within tolerance → re-anchor
    move, _ = day_move_elapsed_anchored(
        _window(25, 200, open0=100.0, close_last=110.0), MID_MS, TODAY,
        cache, "TAO-USD")
    assert abs(move - 10.0) < 1e-9
    assert cache["TAO-USD"][1] == 100.0


def test_empty_buffer_returns_none():
    assert day_move_elapsed_anchored([], MID_MS, TODAY, {}, "TAO-USD") == (None, 0.0)


def test_zero_open_returns_none():
    cds = [_candle(MID_MS, 0.0, 110.0)]
    assert day_move_elapsed_anchored(cds, MID_MS, TODAY, {}, "TAO-USD") == (None, 0.0)


# ── sodex zero-entry guard ────────────────────────────────────────────────────

def test_zero_entry_guard_rejects_before_any_io():
    import asyncio
    from execution.sodex_client import SoDEXClient
    client = object.__new__(SoDEXClient)   # no __init__: guard must not touch attrs
    bracket = SimpleNamespace(
        candidate=SimpleNamespace(entry_price=0.0, symbol="SPCX-USD",
                                  size=1.0, side="long"))
    res = asyncio.run(client._place_native_stop_order(bracket))
    assert res.status == "rejected"
    assert "zero_entry_guard" in res.error
