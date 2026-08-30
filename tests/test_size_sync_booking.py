"""Size-sync booking pins (2026-08-30, SOL incident).

A native merged-TP fill shrank the SOL short 1.215 → 0.001 exchange-side at
01:08:02 with no ARIA close path firing. The old silent adopt in the
reconciliation loop lost the +$0.41 realized PnL to the journal, the outcome
recorder, and every belief-layer learner — then looped software-TP close
failures on the structurally unclosable $0.11 remnant for 77 minutes while a
1.215-sized native stop sat on the dust position.

classify_size_sync routes every divergence to exactly one verdict:
  none / grow (silent adopt) | shrink_silent (no mark) |
  shrink_book (partial close + stop resize) | shrink_purge (partial + dust
  purge, SoDEX sub-$10 remnant only — Aster closePosition has no floor).
"""
import pytest

from main import classify_size_sync


# ── The SOL incident itself ──────────────────────────────────────────────────

def test_sol_incident_is_shrink_purge():
    # tracked 1.215, exchange 0.001, mark 105.12 → remnant $0.105 < $10.
    assert classify_size_sync(1.215, 0.001, 105.12, "sodex") == "shrink_purge"


def test_sol_incident_on_aster_would_book_not_purge():
    # Aster closePosition has no notional floor — the remnant IS closable.
    assert classify_size_sync(1.215, 0.001, 105.12, "aster") == "shrink_book"


# ── shrink_book: actionable remnant ──────────────────────────────────────────

def test_half_fill_books_partial():
    # TP1 took half; remnant 0.6 × $105 = $63 ≥ $10 → book, keep tracking.
    assert classify_size_sync(1.215, 0.6, 105.12, "sodex") == "shrink_book"


def test_remnant_exactly_at_floor_books():
    assert classify_size_sync(2.0, 0.1, 100.0, "sodex") == "shrink_book"


def test_remnant_just_below_floor_purges():
    assert classify_size_sync(2.0, 0.099, 100.0, "sodex") == "shrink_purge"


# ── Silent paths ─────────────────────────────────────────────────────────────

def test_growth_is_silent_adopt():
    # Pyramid add / external increase — never a close.
    assert classify_size_sync(1.0, 1.4, 105.0, "sodex") == "grow"


def test_no_mark_fails_to_legacy_adopt():
    # No price to book against — fail-closed to the legacy silent path.
    assert classify_size_sync(1.215, 0.001, 0.0, "sodex") == "shrink_silent"


# ── Tolerance ────────────────────────────────────────────────────────────────

def test_rounding_drift_is_none():
    # Entry fill rounding (fill_size_adjusted_rounding): 1.215577 → 1.215
    # delta 0.000576 — must NOT book a phantom partial.
    assert classify_size_sync(1.215577, 1.215, 105.47, "sodex") == "none"


def test_tolerance_boundary():
    assert classify_size_sync(1.0, 0.9995, 100.0, "sodex") == "none"
    assert classify_size_sync(1.0, 0.998, 100.0, "sodex") == "shrink_book"


def test_equal_sizes_none():
    assert classify_size_sync(0.001, 0.001, 105.12, "sodex") == "none"
