"""Wiring pins for the CEO exit-coherence schema §2 reachability repairs
(2026-10-16, Governor-approved "fix this").

R1: the three provisional-merge arms register the MFE track (the merge arm is
    the NORMAL fill path — its absence left 73.4% of closes untracked).
R2: _mfe_on_open reads the real field trade_type (execution/schemas.py:47),
    maps cascade_aftermath→scalp, and takes day_type from the live classifier.
R3: on_mark feeds the tracker ABOVE the roe_ratchet loop's ownership
    skip-stack (treasury/pyramid/router-exempt positions accrue MFE; the skips
    gate stop COMPOSITION only).
Fix (b): the treasury at-birth native-TP cancel is gated behind
    treasury_native_tp_cancel_enabled (default False — managed members keep
    their native ladder).
Arcus plane: enabled per Governor 2026-10-16.

These are source-order/contract pins — the live oracles (mfe_roundtrip /
position_closed ≥ 0.90, unknown < 0.20, mfe==0.0 < 0.40 at n ≥ 50 closes)
are graded from telemetry, review 2026-10-19.
"""
import pathlib
import re
import unittest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_MAIN = (_ROOT / "main.py").read_text()
_CONFIG = (_ROOT / "core" / "config.py").read_text()


class TestR1MergeArmsRegister(unittest.TestCase):
    def test_cascade_merge_arms_call_mfe_on_open(self):
        # main.py cascade momentum + aftermath merge arms (identical blocks).
        self.assertEqual(
            _MAIN.count("_mfe_on_open(symbol, _p0, candidate)"), 2)

    def test_bracket_merge_arm_calls_mfe_on_open(self):
        self.assertEqual(
            _MAIN.count("_mfe_on_open(_sym, _existing_in_pm[0], _cand)"), 1)

    def test_merge_registration_precedes_else_arm(self):
        # In each merge block the if-arm registration mirrors the else arm.
        for m in re.finditer(r"_guardian_provisional = False\n", _MAIN):
            tail = _MAIN[m.end():m.end() + 400]
            self.assertIn("_mfe_on_open(", tail)


class TestR2LabelContract(unittest.TestCase):
    def _helper_body(self):
        start = _MAIN.index("def _mfe_on_open(")
        end = _MAIN.index("def _mfe_on_close(")
        return _MAIN[start:end]

    def test_reads_trade_type_not_trade_class(self):
        body = self._helper_body()
        self.assertIn('getattr(candidate, "trade_type", None)', body)
        self.assertNotIn('getattr(candidate, "trade_class"', body)
        self.assertNotIn('getattr(pos, "trade_class"', body)

    def test_cascade_aftermath_maps_to_scalp(self):
        self.assertIn('"scalp" if _tt == "cascade_aftermath"',
                      self._helper_body())

    def test_day_type_from_live_classifier(self):
        body = self._helper_body()
        self.assertIn("day_type_classifier.get_day_type(symbol).value", body)
        self.assertIn("day_type_classifier.is_ready(symbol)", body)


class TestR3OnMarkHoisted(unittest.TestCase):
    def test_on_mark_above_ownership_skip_stack(self):
        loop_start = _MAIN.index("_roe_early_arm_seen: dict = {}")
        region = _MAIN[loop_start:loop_start + 9000]
        on_mark = region.index("_mfe_tracker.on_mark(")
        router_skip = region.index(
            'if _router_exit_exempted(_sym, "roe_ratchet"):')
        pyramid_skip = region.index("if (_pyr_owned and not _is_fc_pos")
        basket_skip = region.index(
            "if not _is_fc_pos and _sym in _basket_managed_syms:")
        hugo_skip = region.index("_hugo_sym_aligned(_sym, _pos.side):")
        self.assertLess(on_mark, router_skip)
        self.assertLess(on_mark, pyramid_skip)
        self.assertLess(on_mark, basket_skip)
        self.assertLess(on_mark, hugo_skip)

    def test_mark_validation_still_gates_tracking(self):
        # Quarantined / missing marks must never reach the tracker.
        loop_start = _MAIN.index("_roe_early_arm_seen: dict = {}")
        region = _MAIN[loop_start:loop_start + 9000]
        quarantine = region.index("_mark_scale_quarantined(_sym, ps=_param_store):")
        on_mark = region.index("_mfe_tracker.on_mark(")
        self.assertLess(quarantine, on_mark)


class TestFixBTreasuryTpCancelGated(unittest.TestCase):
    def test_cancel_site_gated_by_knob(self):
        site = _MAIN.index("treasury_native_tp_cancelled")
        window = _MAIN[site - 3000:site]
        self.assertIn("treasury_native_tp_cancel_enabled", window)

    def test_knob_default_off(self):
        self.assertIn("treasury_native_tp_cancel_enabled: bool = False",
                      _CONFIG)


class TestArcusEnabled(unittest.TestCase):
    def test_arcus_enabled_default_true(self):
        self.assertIn("arcus_enabled: bool = True", _CONFIG)

    def test_arcus_feed_enabled_default_true(self):
        self.assertIn("arcus_feed_enabled: bool = True", _CONFIG)


if __name__ == "__main__":
    unittest.main()
