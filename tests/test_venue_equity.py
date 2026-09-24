"""Venue-equity clamp pins (Governor 2026-09-23).

Sizing reads the general (combined) portfolio; the final margin can never
exceed what the EXECUTING venue can post. _venue_equity_clamp resizes DOWN
to the venue ceiling (never below the venue min-notional floor), refuses
fail-closed on unknown equity, and is inert when
venue_equity_check_enabled=False (pre-2026-09-23 bit-for-bit).
"""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402


def _cfg(**over):
    base = dict(
        venue_equity_check_enabled=True,
        ASSET_CONFIG={},
        aster_margin_pct=0.95,
        aster_tradfi_margin_pct=0.40,
        sodex_margin_pct=0.90,
        default_leverage=5,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _cand(symbol="BTC-USD", entry=100.0, size=10.0, lev=8):
    return SimpleNamespace(symbol=symbol, entry_price=entry, size=size,
                           leverage=lev,
                           initial_margin=entry * size / (lev or 1))


@pytest.fixture(autouse=True)
def _min_notional(monkeypatch):
    # Deterministic venue floor; the floor's own pins live elsewhere.
    monkeypatch.setattr(main, "_venue_min_notional",
                        lambda symbol, eq, cfg: 10.0)


class TestClamp:
    def test_fits_untouched(self):
        c = _cand()
        ok, reject = main._venue_equity_clamp(c, 1000.0, _cfg(), "sodex")
        assert (ok, reject) == (False, None)
        assert c.size == 10.0

    def test_clamps_down_to_ceiling(self):
        # eq 100 × 0.90 = 90 margin ceiling; intent margin = 1000/8 = 125.
        c = _cand()
        ok, reject = main._venue_equity_clamp(c, 100.0, _cfg(), "sodex")
        assert (ok, reject) == (True, None)
        assert c.size == pytest.approx(720.0 / 100.0)   # 90 × 8 / 100
        assert c.initial_margin == pytest.approx(90.0)
        # Floor-rounding can never exceed the ceiling.
        assert c.size * c.entry_price / c.leverage <= 90.0 + 1e-9

    def test_unknown_equity_fail_closed(self):
        c = _cand()
        ok, reject = main._venue_equity_clamp(c, 0.0, _cfg(), "sodex")
        assert (ok, reject) == (False, "venue_equity_unknown")
        assert c.size == 10.0

    def test_floor_unaffordable(self):
        # eq 1 → ceiling 0.9 × 8 = 7.2 < 10.0 min notional.
        c = _cand()
        ok, reject = main._venue_equity_clamp(c, 1.0, _cfg(), "sodex")
        assert (ok, reject) == (False, "venue_equity_insufficient")

    def test_kill_switch_legacy(self):
        c = _cand()
        ok, reject = main._venue_equity_clamp(
            c, 1.0, _cfg(venue_equity_check_enabled=False), "sodex")
        assert (ok, reject) == (False, None)
        assert c.size == 10.0

    def test_aster_crypto_pct(self):
        # eq 100 × 0.95 = 95 ceiling at lev 8 → notional 760.
        c = _cand()
        ok, _ = main._venue_equity_clamp(c, 100.0, _cfg(), "aster")
        assert ok is True
        assert c.size == pytest.approx(760.0 / 100.0)

    def test_aster_tradfi_pct(self):
        cfg = _cfg(ASSET_CONFIG={"PAXG-USD": {"category": "commodity"}})
        c = _cand(symbol="PAXG-USD")
        ok, _ = main._venue_equity_clamp(c, 100.0, cfg, "aster")
        assert ok is True
        assert c.size == pytest.approx(100.0 * 0.40 * 8 / 100.0)

    def test_degenerate_candidate_passthrough(self):
        c = _cand(entry=0.0)
        assert main._venue_equity_clamp(c, 100.0, _cfg(), "sodex") == (False, None)
        c2 = _cand(size=0.0)
        assert main._venue_equity_clamp(c2, 100.0, _cfg(), "sodex") == (False, None)

    def test_leverage_floor_one(self):
        # leverage 0/None reads as 1 — never a division blowup.
        c = _cand(lev=0)
        ok, reject = main._venue_equity_clamp(c, 1000.0, _cfg(), "sodex")
        assert reject is None
