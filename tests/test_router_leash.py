"""tests/test_router_leash.py — pins for the wave-2 exempt-class loss leash.

Doctrine pinned (SOL 2026-10-10 incident — Governor order "ensure this does
not repeat at larger size"): an exempt-class position whose unrealized loss
breaches uPnL ≤ −book_pct of book OR ROE ≤ −roe_pct of margin TRIPS the
leash; the trip is latched per-symbol until THAT position closes; a
same-symbol re-entry (new opened_at_ms) earns a fresh evaluation (the
Governor's same-symbol probe ruling). Fail-open None on degenerate input —
no verdict, the exemption stands (pre-leash system bit-for-bit).
"""
from types import SimpleNamespace

import pytest

from intelligence.router import LeashLatch, leash_verdict


# ── leash_verdict trip legs ──────────────────────────────────────────────────

def test_book_pct_leg_trips():
    # long 10 @ 100 → 97: uPnL −30 on a 1000 book = −3% > 1.5% bound
    v = leash_verdict(side="long", entry_price=100, mark_price=97, size=10,
                      leverage=1, book_balance=1000, book_pct=0.015,
                      roe_pct=0.25)
    assert v["trip"] is True
    assert v["reason"] == "book_pct"
    assert v["upnl"] == pytest.approx(-30.0)
    assert v["book_frac"] == pytest.approx(-0.03)
    assert v["roe"] == pytest.approx(-3.0)


def test_book_pct_leg_holds_when_loss_small():
    # same position on a 100k book: −0.03% — within bounds
    v = leash_verdict(side="long", entry_price=100, mark_price=97, size=10,
                      leverage=1, book_balance=100000, book_pct=0.015,
                      roe_pct=0.25)
    assert v["trip"] is False
    assert v["reason"] == "within_bounds"


def test_roe_pct_leg_trips():
    # short entry 100 → 110, lev 3: move −10% × 3 = −30% ROE < −25% bound
    # (book huge so the book leg cannot fire first)
    v = leash_verdict(side="short", entry_price=100, mark_price=110, size=1,
                      leverage=3, book_balance=1e9, book_pct=0.015,
                      roe_pct=0.25)
    assert v["trip"] is True
    assert v["reason"] == "roe_pct"
    assert v["roe"] == pytest.approx(-30.0)
    assert v["upnl"] == pytest.approx(-10.0)


def test_roe_leg_holds_at_low_leverage():
    v = leash_verdict(side="short", entry_price=100, mark_price=110, size=1,
                      leverage=1, book_balance=1e9, book_pct=0.015,
                      roe_pct=0.25)
    assert v["trip"] is False


def test_green_position_never_trips():
    v = leash_verdict(side="long", entry_price=100, mark_price=105, size=10,
                      leverage=5, book_balance=100, book_pct=0.015,
                      roe_pct=0.25)
    assert v["trip"] is False
    assert v["upnl"] == pytest.approx(50.0)


def test_unknown_book_disables_only_the_book_leg():
    # balance 0 → book_frac None; the ROE leg still binds
    v = leash_verdict(side="long", entry_price=100, mark_price=90, size=1,
                      leverage=3, book_balance=0.0, book_pct=0.015,
                      roe_pct=0.25)
    assert v["book_frac"] is None
    assert v["trip"] is True
    assert v["reason"] == "roe_pct"


def test_custom_bounds_respected():
    # looser Governor bounds: −2% book / −40% ROE — the SOL incident shape
    # (−3% book at lev 1) trips the book leg but not the ROE leg
    v = leash_verdict(side="long", entry_price=100, mark_price=97, size=1,
                      leverage=1, book_balance=100, book_pct=0.02,
                      roe_pct=0.40)
    assert v["trip"] is True
    assert v["reason"] == "book_pct"


# ── fail-open degenerate inputs ──────────────────────────────────────────────

@pytest.mark.parametrize("kw", [
    dict(side="long", entry_price=0, mark_price=97, size=10, leverage=1),
    dict(side="long", entry_price=100, mark_price=0, size=10, leverage=1),
    dict(side="long", entry_price=100, mark_price=97, size=0, leverage=1),
    dict(side="flat", entry_price=100, mark_price=97, size=10, leverage=1),
    dict(side=None, entry_price=100, mark_price=97, size=10, leverage=1),
    dict(side="long", entry_price=None, mark_price=97, size=10, leverage=1),
    dict(side="long", entry_price="x", mark_price=97, size=10, leverage=1),
])
def test_degenerate_input_returns_none(kw):
    assert leash_verdict(book_balance=1000, **kw) is None


# ── LeashLatch semantics ─────────────────────────────────────────────────────

def test_latch_holds_for_same_position_identity():
    l = LeashLatch()
    l.latch("SOL-USD", 111)
    assert l.is_latched("SOL-USD", 111, True) is True


def test_latch_clears_when_position_closes():
    l = LeashLatch()
    l.latch("SOL-USD", 111)
    assert l.is_latched("SOL-USD", 111, False) is False   # closed → cleared
    # and stays clear on the next read
    assert l.is_latched("SOL-USD", 111, True) is False


def test_latch_clears_on_identity_change():
    # same-symbol re-entry at a new level = a NEW position (probe ruling):
    # fresh leash evaluation, the old latch must not bind it
    l = LeashLatch()
    l.latch("SOL-USD", 111)
    assert l.is_latched("SOL-USD", 222, True) is False
    # and the cleared latch does not resurrect for the old identity
    assert l.is_latched("SOL-USD", 111, True) is False


def test_latch_never_latched_is_false():
    l = LeashLatch()
    assert l.is_latched("BTC-USD", 111, True) is False


def test_latch_garbage_inputs_fail_open():
    l = LeashLatch()
    l.latch("", None)                      # no-op
    l.latch("SOL-USD", "garbage")          # no-op
    assert l.is_latched("SOL-USD", None, True) is False
    assert l.is_latched(None, 111, True) is False
