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

from main import classify_size_sync, dust_outcome_basis


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


# ── T3d (2026-09-08): dust-purge outcome follows the remnant's OWN sign ──────
# The shrink_purge path books the closed fraction via _record_partial_close,
# then _record_close folds pos.realized_pnl into the purge close's net total —
# so the win/loss flag used to follow the PARTIAL's (almost always profitable)
# pnl, not the purged remnant's. 20 fabricated wins worth +$8.93, +4.3pp win
# rate. The purge path now passes outcome_pnl=_dust_pnl; dust_outcome_basis is
# the pure sign function it routes through.

def test_dust_purge_positive_remnant_books_win():
    assert dust_outcome_basis(0.07) == "win"
    assert dust_outcome_basis(1e-9) == "win"


def test_dust_purge_negative_remnant_books_loss():
    # The fabricated-win case: partial TP leg green, remnant red → loss.
    assert dust_outcome_basis(-0.03) == "loss"
    assert dust_outcome_basis(-8.93) == "loss"


def test_dust_purge_scratch_books_loss_journal_binary_convention():
    # The journal has no "scratch" outcome and no epsilon band — _record_close's
    # convention is strictly binary (pnl > 0 → win else loss), same as the
    # phantom firewall's zeroed 0.0. Only an exactly-zero/remnant books "loss".
    assert dust_outcome_basis(0.0) == "loss"
    assert dust_outcome_basis(0.004) == "win"    # any positive remnant = win
    assert dust_outcome_basis(-0.004) == "loss"


def test_dust_outcome_basis_degenerate_fails_closed():
    assert dust_outcome_basis(None) == "loss"
    assert dust_outcome_basis("x") == "loss"


def test_record_close_signature_accepts_outcome_pnl():
    # Wiring pin: the purge call site passes outcome_pnl; default None keeps
    # every other caller on the folded-total basis bit-for-bit.
    import inspect
    import main as _m
    # _record_close is a main()-local closure; pin the contract via source.
    src = inspect.getsource(_m)
    assert "outcome_pnl: Optional[float] = None" in src
    assert 'outcome_pnl=_dust_pnl' in src
    assert "dust_pnl_sign_booked" in src
