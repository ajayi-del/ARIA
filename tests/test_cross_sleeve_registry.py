"""Cross-sleeve veto pins (Governor 2026-09-18, Phase 3).

One process, one PositionManager — an entry against our own book nets the
existing position away on the venue. The pure core `_opposing_position_reason`
is module-level (takes the positions list) so the registry read is testable;
the main()-closure helper `_opposing_position_veto` is a thin delegate.
Pins: opposing blocks, same-side passes, sub-$10 dust exempt, absent passes,
side-string normalization, fail-open on malformed rows.
"""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402


def _pos(symbol="BTC-USD", side="long", size=0.01, entry=50000.0):
    return SimpleNamespace(symbol=symbol, side=side, size=size,
                           entry_price=entry)


class TestOpposingBlocks:
    def test_long_candidate_blocked_by_short(self):
        book = [_pos(side="short", size=0.01, entry=50000.0)]   # $500 notional
        r = main._opposing_position_reason(book, "BTC-USD", "long")
        assert r is not None
        assert r.startswith("opposing_short_")
        assert r == "opposing_short_500"

    def test_short_candidate_blocked_by_long(self):
        book = [_pos(side="long", size=2.0, entry=100.0)]       # $200 notional
        r = main._opposing_position_reason(book, "BTC-USD", "short")
        assert r == "opposing_long_200"

    def test_blocks_regardless_of_other_symbols(self):
        book = [_pos(symbol="ETH-USD", side="short", size=10.0, entry=3000.0),
                _pos(symbol="BTC-USD", side="short", size=0.01, entry=50000.0)]
        r = main._opposing_position_reason(book, "BTC-USD", "long")
        assert r == "opposing_short_500"


class TestPasses:
    def test_same_side_passes(self):
        book = [_pos(side="long", size=1.0, entry=100.0)]
        assert main._opposing_position_reason(book, "BTC-USD", "long") is None

    def test_absent_symbol_passes(self):
        book = [_pos(symbol="ETH-USD", side="short", size=10.0, entry=3000.0)]
        assert main._opposing_position_reason(book, "BTC-USD", "long") is None

    def test_empty_book_passes(self):
        assert main._opposing_position_reason([], "BTC-USD", "long") is None
        assert main._opposing_position_reason(None, "BTC-USD", "long") is None

    def test_dust_exempt(self):
        # sub-$10 opposing notional: the netting-absorb path must never be fought
        book = [_pos(side="short", size=0.0001, entry=50000.0)]  # $5 notional
        assert main._opposing_position_reason(book, "BTC-USD", "long") is None

    def test_dust_boundary_blocks(self):
        book = [_pos(side="short", size=0.0002, entry=50000.0)]  # $10 exactly
        assert main._opposing_position_reason(book, "BTC-USD", "long") is not None


class TestSideNormalization:
    @pytest.mark.parametrize("side", ["long", "LONG", "Long", "buy", "BUY"])
    def test_long_aliases(self, side):
        book = [_pos(side="short", size=1.0, entry=100.0)]
        assert main._opposing_position_reason(book, "BTC-USD", side) is not None

    @pytest.mark.parametrize("side", ["short", "SHORT", "sell", "SELL", ""])
    def test_short_aliases(self, side):
        book = [_pos(side="long", size=1.0, entry=100.0)]
        assert main._opposing_position_reason(book, "BTC-USD", side) is not None


class TestFailOpen:
    def test_malformed_rows_skipped(self):
        book = [SimpleNamespace(symbol="BTC-USD"),            # no side/size
                SimpleNamespace(side="short"),                 # no symbol
                "not-a-position",
                _pos(side="short", size=1.0, entry=100.0)]
        r = main._opposing_position_reason(book, "BTC-USD", "long")
        assert r == "opposing_short_100"

    def test_zero_size_or_entry_skipped(self):
        book = [_pos(side="short", size=0.0, entry=100.0),
                _pos(side="short", size=1.0, entry=0.0)]
        assert main._opposing_position_reason(book, "BTC-USD", "long") is None

    def test_min_notional_override(self):
        book = [_pos(side="short", size=0.5, entry=100.0)]     # $50 notional
        assert main._opposing_position_reason(
            book, "BTC-USD", "long", min_notional=100.0) is None
        assert main._opposing_position_reason(
            book, "BTC-USD", "long", min_notional=1.0) == "opposing_short_50"
