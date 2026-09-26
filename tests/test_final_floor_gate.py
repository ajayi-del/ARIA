"""Final-notional floor gate pins (Governor 2026-09-26).

The raise-to floor (min_trade_notional_usd, now 100) runs in the sizing
chain BEFORE the kelly_correlation/vol-stop crushers, so the FINAL notional
can land far under the floor (Aster dust fills $2.7-43 at 0.15x intent,
2026-09-25/26). _final_notional_floor_gate is the post-crush admission
predicate at the 3 venue-equity-clamp bracket sites: pure geometry returning
(would_reject, final_notional) — enforcement policy (kill switch
final_floor_enforcement_enabled, campaign exemption) lives at the call sites.
"""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402

FLOOR = 100.0


def _cand(symbol="BTC-USD", entry=100.0, size=1.0):
    return SimpleNamespace(symbol=symbol, entry_price=entry, size=size)


class TestFinalNotionalFloorGate:
    def test_above_floor_passes(self):
        ok, n = main._final_notional_floor_gate(_cand(size=1.5), FLOOR)
        assert (ok, n) == (False, 150.0)

    def test_at_floor_passes(self):
        ok, n = main._final_notional_floor_gate(_cand(size=1.0), FLOOR)
        assert (ok, n) == (False, 100.0)

    def test_below_floor_rejects(self):
        # The audited dust class: $250 intent x kelly 0.2 x vol-stop 0.75 = 37.5.
        ok, n = main._final_notional_floor_gate(_cand(size=0.375), FLOOR)
        assert ok is True
        assert n == pytest.approx(37.5)

    def test_tiny_dust_rejects(self):
        ok, n = main._final_notional_floor_gate(_cand(size=0.027), FLOOR)
        assert ok is True
        assert n == pytest.approx(2.7)

    def test_just_below_rejects(self):
        ok, n = main._final_notional_floor_gate(_cand(size=0.9999), FLOOR)
        assert ok is True
        assert n == pytest.approx(99.99)

    def test_degenerate_entry_passthrough(self):
        assert main._final_notional_floor_gate(
            _cand(entry=0.0), FLOOR) == (False, 0.0)

    def test_degenerate_size_passthrough(self):
        assert main._final_notional_floor_gate(
            _cand(size=0.0), FLOOR) == (False, 0.0)

    def test_unreadable_candidate_passthrough(self):
        assert main._final_notional_floor_gate(
            SimpleNamespace(symbol="X"), FLOOR) == (False, 0.0)

    def test_bad_floor_passthrough(self):
        assert main._final_notional_floor_gate(
            _cand(), "not-a-number") == (False, 0.0)

    def test_candidate_never_mutated(self):
        c = _cand(size=0.375)
        main._final_notional_floor_gate(c, FLOOR)
        assert c.size == 0.375
        assert c.entry_price == 100.0

    def test_zero_floor_never_rejects(self):
        ok, n = main._final_notional_floor_gate(_cand(size=0.001), 0.0)
        assert (ok, n) == (False, pytest.approx(0.1))


class TestFloorKnob:
    def test_knob_is_100(self):
        from core.config import Settings
        assert Settings().min_trade_notional_usd == 100.0

    def test_kill_switch_default_on(self):
        from core.config import Settings
        assert Settings().final_floor_enforcement_enabled is True

    def test_campaign_floor_untouched(self):
        # Campaign book carries its own venue-aware floors — W1 must not move them.
        from core.config import Settings
        cfg = Settings()
        assert cfg.campaign_min_notional_usd == 250.0
        assert cfg.campaign_venue_min_notional_aster == 3.0

    def test_sodex_gate_floor_still_100(self):
        from core.config import Settings
        assert Settings().sodex_min_notional_usd == 100.0
