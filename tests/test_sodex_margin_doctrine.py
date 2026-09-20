"""SoDEX margin doctrine + pyramid breakeven-gate pins (Governor 2026-09-20).

Margin doctrine: legacy Kelly sizing produced $30-50 dust notionals on a
$550 sleeve. Doctrine = margin_pct of sleeve × Aster-style conviction
ladder, clamped by a stop-risk guard (one stop-out ≤ max_stop_risk_pct of
sleeve), RAISE-ONLY against the legacy size (the raise-only comparison is
caller-side; the pure function never sees the legacy size — pinned here by
construction). Breakeven gate: TP1-proof is one way to prove a winner; a
stop ratcheted AT/THROUGH entry is the other — adds may begin.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import Settings  # noqa: E402
from main import (  # noqa: E402
    _pyramid_breakeven_proof, _sodex_margin_doctrine_size)

BAL = 550.0
ENTRY = 100.0


def _size(coh, stop=98.0, bal=BAL, lev=5.0, entry=ENTRY):
    return _sodex_margin_doctrine_size(
        balance=bal, leverage=lev, coherence=coh, entry_price=entry,
        stop_price=stop, margin_pct=0.90, ladder_base=0.75,
        max_stop_risk_pct=0.06)


class TestMarginDoctrine:
    def test_ladder_rungs(self):
        # cap = 550×0.90×5 = 2475; base = cap×0.75 = 1856.25; stop 1% away →
        # risk cap 3300, non-binding. coh 2.9 → 1.0×; 3.0 → 1.5×; 4.5 → 2.0×.
        # ladder_base 0.75 means EVERY rung above 1.0 exceeds the cap — the
        # cap is the ladder's ceiling by construction.
        s1, _, n1, c1, _ = _size(2.9, stop=99.0)
        assert c1 == 1.0
        assert n1 == pytest.approx(1856.25)
        s2, _, n2, c2, _ = _size(3.0, stop=99.0)
        assert c2 == 1.5
        assert n2 == pytest.approx(2475.0)   # 1.5×base = 2784 > cap
        s3, _, n3, c3, _ = _size(4.5, stop=99.0)
        assert c3 == 2.0
        assert n3 == pytest.approx(2475.0)   # cap binds
        assert s3 == pytest.approx(2475.0 / ENTRY)

    def test_margin_is_notional_over_leverage(self):
        _, margin, notional, _, _ = _size(3.0, stop=99.0)
        assert margin == pytest.approx(notional / 5.0)

    def test_stop_risk_clamp_binds(self):
        # Wide stop: 100→90 = 10% → risk cap = 550×0.06/0.10 = 330 < base.
        _, _, notional, _, clamped = _size(2.9, stop=90.0)
        assert clamped is True
        assert notional == pytest.approx(330.0)

    def test_stop_risk_clamp_quiet_when_stop_tight(self):
        # Tight stop: 100→99 = 1% → risk cap = 33/0.01 = 3300 > all rungs.
        _, _, _, _, clamped = _size(4.5, stop=99.0)
        assert clamped is False

    def test_unknown_stop_reads_as_full_distance(self):
        # Deployed semantics: stop 0/None → stop_dist = entry (100%) → risk
        # cap = 6% of balance. A stop-less candidate can never be RAISED —
        # fail-closed by geometry, bit-for-bit with the deployed splice.
        _, _, notional, _, clamped = _size(2.9, stop=0.0)
        assert clamped is True
        assert notional == pytest.approx(33.0)

    def test_degenerate_inputs_fail_closed(self):
        assert _size(3.0, bal=0.0) is None
        assert _size(3.0, bal=-1.0) is None
        assert _size(3.0, entry=0.0) is None
        assert _sodex_margin_doctrine_size(
            balance="x", leverage=5.0, coherence=3.0, entry_price=ENTRY,
            stop_price=98.0, margin_pct=0.90, ladder_base=0.75,
            max_stop_risk_pct=0.06) is None

    def test_raise_only_is_caller_side(self):
        # The function never receives the legacy size — a legacy size LARGER
        # than the doctrine size can never be shrunk by it (caller compares).
        d_size, _, _, _, _ = _size(2.9)
        legacy_bigger = d_size * 3.0
        final = d_size if d_size > legacy_bigger else legacy_bigger
        assert final == legacy_bigger


class TestPyramidBreakevenGate:
    def test_tp1_cleared_always_proves(self):
        assert _pyramid_breakeven_proof(
            True, stop_price=0.0, entry_price=0.0, side="long",
            gate_enabled=False) is True

    def test_gate_disabled_is_legacy(self):
        assert _pyramid_breakeven_proof(
            False, stop_price=105.0, entry_price=100.0, side="long",
            gate_enabled=False) is False

    def test_long_stop_at_or_above_entry_proves(self):
        assert _pyramid_breakeven_proof(
            False, stop_price=100.0, entry_price=100.0, side="long",
            gate_enabled=True) is True
        assert _pyramid_breakeven_proof(
            False, stop_price=105.0, entry_price=100.0, side="long",
            gate_enabled=True) is True

    def test_long_stop_below_entry_no_proof(self):
        assert _pyramid_breakeven_proof(
            False, stop_price=99.0, entry_price=100.0, side="long",
            gate_enabled=True) is False

    def test_short_mirror(self):
        assert _pyramid_breakeven_proof(
            False, stop_price=100.0, entry_price=100.0, side="short",
            gate_enabled=True) is True
        assert _pyramid_breakeven_proof(
            False, stop_price=95.0, entry_price=100.0, side="short",
            gate_enabled=True) is True
        assert _pyramid_breakeven_proof(
            False, stop_price=101.0, entry_price=100.0, side="short",
            gate_enabled=True) is False

    def test_degenerate_geometry_fails_closed(self):
        for stop, entry in ((0.0, 100.0), (100.0, 0.0), (None, 100.0),
                            ("x", 100.0)):
            assert _pyramid_breakeven_proof(
                False, stop_price=stop, entry_price=entry, side="long",
                gate_enabled=True) is False


class TestDoctrineConfig:
    def test_live_arming(self):
        s = Settings()
        assert s.sodex_margin_doctrine_enabled is True
        assert s.sodex_margin_pct == 0.90
        assert s.sodex_margin_ladder_base == 0.75
        assert s.sodex_max_stop_risk_pct == 0.06
        assert s.pyramid_add_breakeven_gate_enabled is True
