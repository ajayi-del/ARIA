"""Final TP invariant gate pins (Governor 2026-09-26, FARTCOIN 91-second close).

_clamp_tp_to_sodex_range guards "never clamp a TP past entry" against the
CLAMP-TIME entry, but the 3 bracket sites re-anchor the final entry AFTER the
clamp (_anchor_aster_entry_price + _vol_stop_splice) — the entry can drift past
a clamped TP1 in the gap. FARTCOIN-USD 2026-09-26 15:35 UTC: TP1 clamped to
0.1989 with the pre-anchor entry below it; the final entry anchored at 0.199 →
TP1 below the fill = a marketable adverse limit sell that instantly filled half
the position at a loss (net -$0.13, hold 91s).

_final_tp_invariant_verdict is the LAST geometry word before place_bracket:
pure, never mutates, fail-OPEN on missing/degenerate geometry (unknowns are
the other gates' jurisdiction). Enforcement policy (kill switch
tp_invariant_gate_enabled, shadow would-block scoring) lives at the call sites.
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402


def _cand(side="long", entry=0.199, tp1=0.2139, **extra):
    c = SimpleNamespace(symbol="FARTCOIN-USD", side=side,
                        entry_price=entry, tp1_price=tp1)
    for k, v in extra.items():
        setattr(c, k, v)
    return c


class TestLongInvariant:
    def test_tp_above_entry_valid(self):
        assert main._final_tp_invariant_verdict(_cand(entry=0.199, tp1=0.2139)) is True

    def test_tp_below_entry_rejected(self):
        # The FARTCOIN wound exactly: clamp landed TP1 at 0.1989, final entry 0.199.
        assert main._final_tp_invariant_verdict(_cand(entry=0.199, tp1=0.1989)) is False

    def test_tp_equal_entry_rejected(self):
        # At-entry TP1 is a scratch fill race, not a profit target.
        assert main._final_tp_invariant_verdict(_cand(entry=0.199, tp1=0.199)) is False

    def test_tp_one_tick_above_valid(self):
        assert main._final_tp_invariant_verdict(_cand(entry=0.199, tp1=0.1991)) is True


class TestShortInvariant:
    def test_tp_below_entry_valid(self):
        assert main._final_tp_invariant_verdict(
            _cand(side="short", entry=100.0, tp1=97.0)) is True

    def test_tp_above_entry_rejected(self):
        # Mirror wound: clamp-to-24h-low can land TP1 above a re-anchored short entry.
        assert main._final_tp_invariant_verdict(
            _cand(side="short", entry=100.0, tp1=100.5)) is False

    def test_tp_equal_entry_rejected(self):
        assert main._final_tp_invariant_verdict(
            _cand(side="short", entry=100.0, tp1=100.0)) is False


class TestFailOpen:
    def test_missing_tp1_valid(self):
        assert main._final_tp_invariant_verdict(_cand(tp1=None)) is True

    def test_zero_tp1_valid(self):
        assert main._final_tp_invariant_verdict(_cand(tp1=0.0)) is True

    def test_zero_entry_valid(self):
        # Degenerate entry is the sizing/floor gates' jurisdiction, not this one.
        assert main._final_tp_invariant_verdict(_cand(entry=0.0, tp1=0.1989)) is True

    def test_none_entry_valid(self):
        assert main._final_tp_invariant_verdict(_cand(entry=None)) is True

    def test_string_geometry_valid(self):
        # Unreadable types fail open — never crash the entry path on a verdict.
        assert main._final_tp_invariant_verdict(_cand(entry="x", tp1="y")) is True

    def test_missing_side_defaults_long(self):
        c = SimpleNamespace(symbol="X-USD", entry_price=1.0, tp1_price=0.5)
        assert main._final_tp_invariant_verdict(c) is False


class TestOrderingPin:
    def test_clamp_then_anchor_sequence(self):
        # The defect replayed end-to-end through the two pure pieces: clamp to
        # high*0.995 passes its own guard at the clamp-time entry, then the
        # anchor moves the entry past it — the final verdict catches what the
        # clamp-time guard could not.
        high_24h = 0.1999
        tp1_cap = high_24h * 0.995  # 0.19890...
        clamp_time_entry = 0.1980
        assert tp1_cap > clamp_time_entry  # clamp guard passes at clamp time
        final_entry = 0.199  # post-anchor drift
        assert main._final_tp_invariant_verdict(
            _cand(entry=final_entry, tp1=tp1_cap)) is False
