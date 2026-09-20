"""tests/test_exposure_snapshot.py — pins for the book-exposure math."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelligence.exposure_snapshot import compute_book_exposure  # noqa: E402


def _leg(side, size, mark, venue="sodex", symbol="BTC-USD"):
    return {"symbol": symbol, "side": side, "size": size, "mark": mark,
            "venue": venue}


def test_long_short_netting():
    out = compute_book_exposure(
        [_leg("long", 1.0, 100.0), _leg("short", 0.5, 200.0, venue="aster")],
        [])
    assert out["primary_net"] == pytest.approx(0.0)
    assert out["net_notional"] == pytest.approx(0.0)
    assert out["gross_notional"] == pytest.approx(200.0)


def test_hedge_legs_separated():
    out = compute_book_exposure(
        [_leg("long", 1.0, 100.0)],
        [_leg("short", 0.25, 200.0, venue="bybit")])
    assert out["primary_net"] == pytest.approx(100.0)
    assert out["hedge_net"] == pytest.approx(-50.0)
    assert out["net_notional"] == pytest.approx(50.0)
    assert out["gross_notional"] == pytest.approx(150.0)


def test_per_venue_breakdown():
    out = compute_book_exposure(
        [_leg("long", 1.0, 100.0, venue="sodex"),
         _leg("short", 0.5, 200.0, venue="aster")],
        [_leg("short", 0.25, 200.0, venue="bybit")])
    assert out["per_venue"] == {"sodex": pytest.approx(100.0),
                                "aster": pytest.approx(-100.0),
                                "bybit": pytest.approx(-50.0)}


def test_missing_mark_skips_leg():
    leg = {"symbol": "X", "side": "long", "size": 1.0, "venue": "sodex"}
    out = compute_book_exposure([leg, _leg("long", 1.0, 100.0)], [])
    assert out["primary_net"] == pytest.approx(100.0)
    assert out["gross_notional"] == pytest.approx(100.0)


def test_missing_size_skips_leg():
    leg = {"symbol": "X", "side": "long", "mark": 100.0, "venue": "sodex"}
    out = compute_book_exposure([leg], [])
    assert out["primary_net"] == 0.0


def test_empty_inputs_zero():
    out = compute_book_exposure([], [])
    assert out == {"net_notional": 0.0, "gross_notional": 0.0,
                   "primary_net": 0.0, "hedge_net": 0.0, "per_venue": {}}
