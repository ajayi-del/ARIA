"""C2 pins (2026-09-19): dust enrichment — value-bearing exemption.

Pins main._actionable_dust_ratio with the pyramid-track exemption: a symbol
with a live pyramid track is working capital under an active plan, not dust.
Fail-open: bad exemption inputs count as dust (legacy). The venue-close-min
floor (SoDEX $10 / Aster $1) still binds.
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402


def _pos(entry, size):
    return SimpleNamespace(entry_price=entry, size=size)


def _items(**sym_positions):
    return list(sym_positions.items())


class TestDustEnrichment:
    def test_legacy_call_signature_unchanged(self):
        # One actionable dust (15 USD notional, SoDEX min close 10) of two.
        items = _items(**{"SOL-USD": [_pos(15.0, 1.0)], "BTC-USD": [_pos(100.0, 1.0)]})
        assert main._actionable_dust_ratio(items) == 0.5

    def test_pyramid_tracked_dust_exempt_when_enabled(self):
        items = _items(**{"SOL-USD": [_pos(15.0, 1.0)], "BTC-USD": [_pos(100.0, 1.0)]})
        ratio = main._actionable_dust_ratio(
            items, exempt_enabled=True, pyramid_tracks={"SOL-USD": object()})
        assert ratio == 0.0   # the tracked remnant is value-bearing, not dust

    def test_pyramid_tracked_dust_counts_when_disabled(self):
        items = _items(**{"SOL-USD": [_pos(15.0, 1.0)], "BTC-USD": [_pos(100.0, 1.0)]})
        ratio = main._actionable_dust_ratio(
            items, exempt_enabled=False, pyramid_tracks={"SOL-USD": object()})
        assert ratio == 0.5   # flag off = legacy

    def test_untracked_symbol_still_counts(self):
        items = _items(**{"SOL-USD": [_pos(15.0, 1.0)], "ETH-USD": [_pos(12.0, 1.0)]})
        ratio = main._actionable_dust_ratio(
            items, exempt_enabled=True, pyramid_tracks={"SOL-USD": object()})
        assert ratio == 0.5   # only the tracked symbol is exempt

    def test_bad_tracks_type_fails_open(self):
        items = _items(**{"SOL-USD": [_pos(15.0, 1.0)]})
        assert main._actionable_dust_ratio(
            items, exempt_enabled=True, pyramid_tracks="garbage") == 1.0
        assert main._actionable_dust_ratio(
            items, exempt_enabled=True, pyramid_tracks=None) == 1.0

    def test_unclosable_dust_never_counts(self):
        # 5 USD notional < SoDEX 10 min close — structurally unclosable,
        # exempt or not it is never actionable dust.
        items = _items(**{"SOL-USD": [_pos(5.0, 1.0)], "BTC-USD": [_pos(100.0, 1.0)]})
        assert main._actionable_dust_ratio(items) == 0.0
        assert main._actionable_dust_ratio(
            items, exempt_enabled=True, pyramid_tracks={}) == 0.0

    def test_empty_book(self):
        assert main._actionable_dust_ratio([], exempt_enabled=True,
                                           pyramid_tracks={}) == 0.0
