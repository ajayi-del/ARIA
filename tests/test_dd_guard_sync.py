"""External-flow repairs must reach every DD tracker (2026-09-02 audit).

The 2026-09-01 phantom: ~$31 of operator withdrawals went unclassified, the
combined-equity trackers read a 4.2% drawdown (real trading DD ~0.5%), and
recovery mode (floor 5.6 + 0.5x cap) re-armed at all three boots. The
operator reset flag cleared the DrawdownManager — but the calibrator's
recovery trigger reads DrawdownGuard, whose sync_peak is ratchet-only BY
DESIGN (a lower manager peak from lost state must never disarm the guard),
so recovery stayed latched until restart. Bonus defects in the same class:
the reset flag and apply_balance_adjustment never touched _day_start_balance
(daily DD kept the phantom), and detected withdrawals also failed to reach
the guard. Kill switch DD_GUARD_SYNC_FIX_ENABLED=false = legacy
(manager-only repairs) bit-for-bit."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from risk.drawdown_guard import DrawdownGuard  # noqa: E402
from risk.drawdown_manager import DrawdownManager  # noqa: E402


class TestGuardAdjustPeak:
    def test_withdrawal_delta_lowers_dd_read(self):
        g = DrawdownGuard()
        g.update_balance(100.0)          # peak 100
        g.update_balance(90.0)           # DD 10%
        assert abs(g.get_state().drawdown_pct - 0.10) < 1e-9
        g.adjust_peak(-5.0, reason="external_withdrawal_detected")
        assert g._peak == 95.0
        assert g.get_state().drawdown_pct == round(5.0 / 95.0, 4)

    def test_deposit_delta_raises_peak(self):
        g = DrawdownGuard()
        g.update_balance(100.0)
        g.update_balance(95.0)
        g.adjust_peak(+10.0, reason="external_deposit_detected")
        assert g._peak == 110.0
        assert g.get_state().drawdown_pct == round(15.0 / 110.0, 4)

    def test_zero_delta_noop(self):
        g = DrawdownGuard()
        g.update_balance(100.0)
        g.adjust_peak(0.0, reason="noop")
        assert g._peak == 100.0

    def test_negative_over_shoot_floors_at_zero(self):
        g = DrawdownGuard()
        g.update_balance(10.0)
        g.adjust_peak(-50.0, reason="external_withdrawal_detected")
        assert g._peak == 0.0
        assert g.get_state().drawdown_pct == 0.0   # fail-closed: no negative DD


class TestGuardResetPeak:
    def test_reset_clears_dd_and_multiplier(self):
        g = DrawdownGuard()
        g.update_balance(100.0)
        g.update_balance(50.0)           # deep DD — tier multiplier drops
        assert g.size_multiplier() < 1.0
        g.reset_peak(50.0, reason="reset_drawdown.flag")
        assert g._peak == 50.0
        assert g.get_state().drawdown_pct == 0.0
        assert g.size_multiplier() == 1.0

    def test_reset_rejects_nonpositive(self):
        g = DrawdownGuard()
        g.update_balance(100.0)
        g.reset_peak(0.0, reason="bad")
        g.reset_peak(-5.0, reason="bad")
        assert g._peak == 100.0          # untouched

    def test_ratchet_still_ratchets(self):
        # sync_peak stays upward-only: a lower manager peak (lost state)
        # must never disarm the guard
        g = DrawdownGuard()
        g.update_balance(100.0)
        g.sync_peak(120.0)
        assert g._peak == 120.0
        g.sync_peak(80.0)
        assert g._peak == 120.0


class TestManagerDayStartAnchor:
    def test_adjustment_shifts_day_start(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        m = DrawdownManager(starting_balance=0.0)
        m._peak_balance = 862.77
        m._low_watermark = 826.75
        m._session_start = 850.0
        m._week_start = 843.43
        m._day_start_balance = 853.84
        m.apply_balance_adjustment(-28.0, reason="external_withdrawal_detected")
        assert abs(m._peak_balance - 834.77) < 1e-9
        assert abs(m._day_start_balance - 825.84) < 1e-9

    def test_adjustment_skips_unset_day_start(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        m = DrawdownManager(starting_balance=0.0)
        m._day_start_balance = 0.0       # never seeded — must stay unset
        m.apply_balance_adjustment(-28.0, reason="external_withdrawal_detected")
        assert m._day_start_balance == 0.0


class TestMainWiring:
    @staticmethod
    def _src():
        return open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "main.py")).read()

    def test_kill_switch(self):
        src = self._src()
        assert "DD_GUARD_SYNC_FIX_ENABLED" in src
        assert "_dd_guard_sync_fix" in src

    def test_reset_flag_reaches_guard_and_day_start(self):
        src = self._src()
        i_flag = src.index('Path("logs/reset_drawdown.flag")')
        i_save = src.index("drawdown_manager._save_state()", i_flag)
        window = src[i_flag:i_save]
        assert "drawdown_manager._day_start_balance = balance" in window
        assert 'drawdown_guard.reset_peak(' in window

    def test_all_three_adjustment_sites_reach_guard(self):
        src = self._src()
        assert src.count("drawdown_guard.adjust_peak(") == 3
        assert 'reason="external_withdrawal_detected")' in src
        assert 'reason="external_deposit_detected")' in src
        assert 'reason="external_withdrawal_openbook")' in src
