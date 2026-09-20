"""Wiring pins for the open-book deposit arm (CEO DIR DEPOSIT-OPENBOOK-PORT #65,
deposit-openbook-port-0919).

The flat-book branch could classify deposits; the open-book wb detector had
only the withdrawal arm — a deposit landing while positions were open was
vetoed ("deposit_detection_vetoed") and left every DD anchor stale vs the new
capital. The repair adds `elif _flow == "deposit":` mirroring the withdrawal
arm, applying the NETTED external leg (the classifier's own basis) with the
gap-preserving peak doctrine of the flat-book deposit arm.
"""
import pathlib
import re
import unittest

from risk.drawdown_manager import DrawdownManager

_MAIN = pathlib.Path(__file__).resolve().parent.parent / "main.py"


class TestOpenBookDepositClassifier(unittest.TestCase):
    """The classifier already returns 'deposit' — the defect was the missing
    arm at the call site. These pins re-assert the classification basis the
    new arm consumes."""

    def test_deposit_classifies_with_close_in_window(self):
        # +53 wb with a +3 realized win in window: external leg = +50 deposit
        assert DrawdownManager.classify_external_flow_netted(53.0, 3.0) == "deposit"

    def test_deposit_classifies_behind_loss(self):
        # +47 wb with a -3 realized loss: external leg = +50 deposit
        assert DrawdownManager.classify_external_flow_netted(47.0, -3.0) == "deposit"

    def test_pure_gain_window_classifies_as_nothing(self):
        # +3 wb fully explained by a +3 close — NOT a deposit (fail-closed)
        assert DrawdownManager.classify_external_flow_netted(3.0, 3.0) is None

    def test_small_residual_ignored(self):
        assert DrawdownManager.classify_external_flow_netted(2.5, 1.0) is None


class TestOpenBookDepositWiring(unittest.TestCase):
    """Source-level wiring pins (the arm lives inside main.py's balance
    monitor loop — asserted structurally, same idiom as the etf_tide veto
    ordering pins)."""

    @classmethod
    def setUpClass(cls):
        cls.src = _MAIN.read_text()

    def test_deposit_arm_follows_withdrawal_arm(self):
        wd = self.src.index('if _flow == "withdrawal":')
        dep = self.src.index('elif _flow == "deposit":')
        assert wd < dep, "deposit arm must follow the withdrawal arm"

    def test_deposit_arm_applies_netted_leg(self):
        # raw wb delta would shift day_start/peak by trading PnL too
        assert "_dep_netted = (" in self.src
        dep_block = self.src[self.src.index('elif _flow == "deposit":'):]
        adj = dep_block.index("apply_balance_adjustment(")
        assert "_dep_netted" in dep_block[:adj + 200]

    def test_deposit_arm_reason_and_event(self):
        assert '"external_deposit_openbook"' in self.src
        assert '"deposit_anchors_adjusted"' in self.src

    def test_deposit_arm_gap_preserving_peak(self):
        # the 5s equity poll absorbs deposits into the peak before the 30s
        # loop sees them (2026-08-29 doctrine) — the arm must set
        # prev_peak + netted, not shift an already-absorbed peak again
        dep_block = self.src[self.src.index('elif _flow == "deposit":'):]
        assert re.search(
            r"_bm_prev_peak > 0.*_peak_balance = \(\s*_bm_prev_peak \+ _dep_netted",
            dep_block, re.S), "gap-preserving peak correction missing"


if __name__ == "__main__":
    unittest.main()
