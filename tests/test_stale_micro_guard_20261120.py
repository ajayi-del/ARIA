"""2026-11-20 fix pack — stale-Bybit micro guard + adoption stop clamp.

QNT incident: a wedged Bybit WS batch left the microstructure component
pinned at 1.5 for 5.5h while price fell 4%, and an adopted position was
seeded with a stop already through the mark (3.9s stop-out).
"""
import time

from data.bybit_feed import BybitFeed
from main import _latefill_stop_geometry


def _feed_with_ts(ts_map):
    f = BybitFeed.__new__(BybitFeed)
    f._last_msg_ts = dict(ts_map)
    return f


def test_is_symbol_stale_fresh():
    f = _feed_with_ts({"QNT-USD": time.time()})
    assert f.is_symbol_stale("QNT-USD") is False


def test_is_symbol_stale_old():
    f = _feed_with_ts({"QNT-USD": time.time() - 301})
    assert f.is_symbol_stale("QNT-USD") is True


def test_is_symbol_stale_unknown_symbol():
    assert _feed_with_ts({}).is_symbol_stale("QNT-USD") is True


def test_adoption_stop_short_safe_side_of_mark():
    # The incident geometry: entry 152.92, mark already 154.18 — the seeded
    # stop must sit above the mark, never through it.
    stop = _latefill_stop_geometry("short", 152.92, 154.18, None,
                                   floor_frac=0.02, atr_mult=1.0,
                                   min_stop_pct=0.5)
    assert stop > 154.18


def test_adoption_stop_long_safe_side_of_mark():
    stop = _latefill_stop_geometry("long", 100.0, 97.0, None,
                                   floor_frac=0.02, atr_mult=1.0,
                                   min_stop_pct=0.5)
    assert 0.0 < stop < 97.0


def test_latefill_geometry_degenerate_returns_zero():
    assert _latefill_stop_geometry("short", 0, 100.0, None) == 0.0
    assert _latefill_stop_geometry("sideways", 100.0, 100.0, None) == 0.0
