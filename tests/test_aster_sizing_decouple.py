"""Aster sizing decouple pins (Governor 2026-09-27: "aster did not loose —
aster trades should not be undersized").

Two builds:
  1. WR-reason recovery exemption for aster-routed candidates (the 2026-08-27
     "WR stays global" clause superseded — journal evidence 7d Aster net
     -$0.84 vs SoDEX -$114.51; the combined WR is SoDEX-driven).
  2. dd_mult venue decouple: aster-routed candidates read the ASTER SLEEVE's
     own session-scoped drawdown through tier_multiplier_for_dd (same ladder
     as DrawdownGuard) instead of the combined-book multiplier. Dark sleeve
     data fails closed to combined.

NOTE on _aster_sleeve_dd_mult testability: the helper is a closure inside
main() (it reads the main()-scope aster_client via late binding, mirroring
every other closure in main()) and is therefore NOT importable as a module
attribute. Per spec, behavior is pinned via (a) the full ladder grid on the
importable pure function tier_multiplier_for_dd (the helper's entire
decision math is (peak-eq)/peak -> ladder) and (b) source-grep pins on the
helper + splice wiring in main.py. main() is NOT restructured to force
testability.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from risk.drawdown_guard import tier_multiplier_for_dd  # noqa: E402
from execution.venue import aster_recovery_exempt  # noqa: E402
from core.config import Settings  # noqa: E402

_MAIN_SRC = open(
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "main.py")).read()


class TestTierMultiplierForDd:
    """The real ladder (risk/drawdown_guard.py _DRAWDOWN_TIERS):
    0-5% -> 1.0, 5-10% -> 0.8, 10%+ -> 0.6 (floor _MIN_MULT 0.60)."""

    def test_no_drawdown_full_size(self):
        assert tier_multiplier_for_dd(0.0) == 1.0

    def test_under_first_tier_full_size(self):
        assert tier_multiplier_for_dd(0.04) == 1.0

    def test_first_tier_boundary(self):
        assert tier_multiplier_for_dd(0.05) == 0.8

    def test_inside_first_tier(self):
        assert tier_multiplier_for_dd(0.099) == 0.8

    def test_second_tier_boundary(self):
        assert tier_multiplier_for_dd(0.10) == 0.6

    def test_deep_drawdown_floored(self):
        assert tier_multiplier_for_dd(0.25) == 0.6

    def test_helper_dd_numbers(self):
        # The sleeve helper's worked examples from the spec: 7% below peak
        # -> 0.8; 12% below peak -> 0.6.
        assert tier_multiplier_for_dd(0.07) == 0.8
        assert tier_multiplier_for_dd(0.12) == 0.6

    def test_negative_fails_open(self):
        assert tier_multiplier_for_dd(-0.05) == 1.0

    def test_nan_fails_open(self):
        assert tier_multiplier_for_dd(float("nan")) == 1.0

    def test_inf_fails_open(self):
        assert tier_multiplier_for_dd(float("inf")) == 1.0

    def test_unreadable_type_fails_open(self):
        assert tier_multiplier_for_dd("x") == 1.0
        assert tier_multiplier_for_dd(None) == 1.0


class TestWrRecoveryExemptionGrid:
    def test_aster_win_rate_exempt_by_default(self):
        # Governor 2026-09-27: WR recovery is combined-book evidence and the
        # combined WR is SoDEX's bill — aster candidates are exempt.
        assert aster_recovery_exempt("aster", "win_rate", True) is True

    def test_aster_win_rate_kill_switch(self):
        # wr_enabled=False = 2026-08-27 doctrine bit-for-bit.
        assert aster_recovery_exempt("aster", "win_rate", True, False) is False

    def test_aster_drawdown_kill_switch(self):
        assert aster_recovery_exempt("aster", "drawdown", False, True) is False

    def test_sodex_win_rate_never_exempt(self):
        assert aster_recovery_exempt("sodex", "win_rate", True) is False

    def test_empty_reason_fails_closed(self):
        assert aster_recovery_exempt("aster", "", True, True) is False

    def test_unknown_reason_fails_closed(self):
        assert aster_recovery_exempt("aster", "mystery", True, True) is False

    def test_legacy_equivalence_grid(self):
        # wr_enabled=False collapses to the pre-2026-09-27 predicate for
        # every venue x reason combination.
        for v in ("aster", "sodex", "bybit"):
            for r in ("drawdown", "win_rate", "", "mystery"):
                for e in (True, False):
                    assert aster_recovery_exempt(v, r, e, False) == (
                        bool(e) and r == "drawdown" and v == "aster")


class TestSleeveDdHelperWiring:
    """Source-grep pins: _aster_sleeve_dd_mult is a main() closure and cannot
    be imported — the splice/helper semantics are pinned at the source."""

    def test_helper_defined_with_peak_ratchet(self):
        assert "_aster_sleeve_peak: dict = {\"eq\": 0.0}" in _MAIN_SRC
        assert "def _aster_sleeve_dd_mult():" in _MAIN_SRC
        assert ('_aster_sleeve_peak["eq"] = max(_aster_sleeve_peak["eq"], _eq)'
                in _MAIN_SRC)

    def test_helper_fail_closed_semantics(self):
        # Dark data -> None: stale cache bound, non-positive equity, and the
        # ladder read itself.
        assert "aster_dd_decouple_max_age_s" in _MAIN_SRC
        assert ("return tier_multiplier_for_dd(max(0.0, (_peak - _eq) / _peak))"
                in _MAIN_SRC)
        assert "except Exception:\n            return None" in _MAIN_SRC

    def test_splice_calls_helper_for_aster_only(self):
        assert ('bool(getattr(config, "aster_dd_decouple_enabled", True))'
                in _MAIN_SRC)
        assert '_exec_venue == "aster"' in _MAIN_SRC
        assert "_dd_mult_sleeve = _aster_sleeve_dd_mult()" in _MAIN_SRC

    def test_splice_fails_closed_to_combined(self):
        assert ("_dd_mult_effective = (_dd_mult_sleeve if _dd_mult_sleeve is not None"
                in _MAIN_SRC)
        assert "_dd_mult_combined = max(_dd_mult, _dm_mult)" in _MAIN_SRC

    def test_sizing_chain_carries_both_from_birth(self):
        assert "dd_mult_combined=round(_dd_mult_combined, 3)," in _MAIN_SRC
        assert ("dd_mult_sleeve=round(_dd_mult_sleeve, 3) if _dd_mult_sleeve is not None else None,"
                in _MAIN_SRC)

    def test_decouple_telemetry_event(self):
        assert 'logger.info("aster_dd_decoupled"' in _MAIN_SRC


class TestKnobDefaults:
    def test_wr_exempt_default_on(self):
        assert Settings().aster_wr_recovery_exempt_enabled is True

    def test_dd_decouple_default_on(self):
        assert Settings().aster_dd_decouple_enabled is True

    def test_dd_decouple_staleness_bound_default(self):
        assert Settings().aster_dd_decouple_max_age_s == 180.0
