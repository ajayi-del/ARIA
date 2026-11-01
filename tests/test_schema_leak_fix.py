"""Schema-leak-fix pins (2026-11-01 deploy bundle).

Covers:
1. DrawdownGuard.set_peak — the gap-preserving deposit repair (watchdog P0:
   310.55 → 429.91 = 2×delta double-count → fake 13.91% DD → recovery latched
   on a 0.67% real DD). adjust_peak's raw shift stays for withdrawals.
2. main._venue_leg_netted_away — the Arcus same-subaccount netting predicate
   (2026-10-31 PUMP REDUCE_ONLY_WOULD_INCREASE exit storm).
3. main._build_trade_record snap threading — the pre-teardown registry
   snapshot lands on TradeRecord; snap=None is bit-for-bit legacy.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from execution.schemas import Position  # noqa: E402
from risk.drawdown_guard import DrawdownGuard  # noqa: E402


def _position(**kw):
    base = dict(
        symbol="SOL-USD", side="long", entry_price=100.0, size=1.0,
        stop_price=99.0, tp1_price=101.0, tp2_price=102.0, tp3_price=103.0,
        liq_price=90.0, initial_margin=20.0, leverage=5,
        opened_at_ms=1_000_000,
    )
    base.update(kw)
    return Position(**base)


class TestSetPeakGapPreserving:
    """The 2026-11-01 double-count class: the 5s equity poll's update_balance
    ratchet absorbs a deposit into the peak BEFORE the 30s balance loop's
    repair fires. A raw shift then adds the delta twice."""

    def test_absorbed_deposit_not_double_counted(self):
        g = DrawdownGuard()
        g.update_balance(310.55)          # pre-deposit state
        g.update_balance(370.13)          # 5s poll absorbs the +59.58 deposit
        assert g._peak == 370.13
        # 30s loop fires with prev-peak snapshot 310.55 + delta 59.68:
        g.set_peak(310.55 + 59.68, reason="external_deposit_detected")
        assert abs(g._peak - 370.23) < 1e-9
        # Old behavior would have landed at 429.81+ — pin the exact absence.
        assert g._peak < 400.0
        # DD reads ~0 (balance 370.13 vs peak 370.23), not 13.91%.
        assert g.get_state().drawdown_pct < 0.005

    def test_unabsorbed_deposit_still_repairs(self):
        g = DrawdownGuard()
        g.update_balance(310.55)          # poll hasn't seen the deposit yet
        g.set_peak(310.55 + 59.68, reason="external_deposit_detected")
        assert abs(g._peak - 370.23) < 1e-9

    def test_set_peak_rejects_nonpositive(self):
        g = DrawdownGuard()
        g.update_balance(310.55)
        g.set_peak(0.0, reason="bad")
        g.set_peak(-10.0, reason="bad")
        assert g._peak == 310.55

    def test_set_peak_does_not_restore_multiplier(self):
        # Repair is read-only on the size multiplier — recovery stays
        # earned-by-wins (same doctrine as adjust_peak).
        g = DrawdownGuard()
        g.update_balance(400.0)
        g.update_balance(300.0)           # 25% DD → tier clamps mult to 0.60
        assert g.size_multiplier() == 0.60
        g.set_peak(300.0, reason="repair")
        assert g.size_multiplier() == 0.60  # NOT re-raised by the repair

    def test_adjust_peak_still_raw_shift_for_withdrawals(self):
        # Withdrawals can't be ratchet-absorbed (balance drops), so the raw
        # shift stays correct on that path — legacy semantics pinned.
        g = DrawdownGuard()
        g.update_balance(500.0)
        g.adjust_peak(-50.0, reason="external_withdrawal_detected")
        assert g._peak == 450.0


class TestVenueNettedPredicate:
    def test_opposite_side_netted(self):
        assert main._venue_leg_netted_away("long", "short") is True
        assert main._venue_leg_netted_away("short", "long") is True

    def test_same_side_not_netted(self):
        assert main._venue_leg_netted_away("long", "long") is False
        assert main._venue_leg_netted_away("short", "short") is False

    def test_unknown_sides_never_net(self):
        assert main._venue_leg_netted_away("", "long") is False
        assert main._venue_leg_netted_away("long", "") is False
        assert main._venue_leg_netted_away("", "") is False

    def test_pump_incident_shape(self):
        # ARIA tracked long 473,854; venue net −232.56 short (operator's
        # −474,087 netted ours away) → predicate fires, exit loop stands down.
        assert main._venue_leg_netted_away("long", "short") is True


class TestSnapThreading:
    _SNAP = {
        "mfe_usd": 1.25, "mae_usd": -0.40, "mfe_at_stop_usd": 0.90,
        "peak_pnl_usd": 1.20, "peak_pnl_ms": 1_700_000_000_000,
        "close_path_attempts": 3, "close_path_rejected": 2,
        "close_path_first_fail_reason": "reduce_only_would_increase",
        "pyramid_legs_done": 2, "pyramid_tp1_cleared": True,
        "ratchet_rung_reached": "high", "ratchet_peak_roe": 9.4,
        "ratchet_early_arm_fired": True, "treasury_cluster_engaged": True,
        "campaign_pyramid_layers": 1,
    }

    def test_snap_fields_threaded(self):
        rec = main._build_trade_record(
            _position(), 100.5, "software_stop", 0.5, snap=dict(self._SNAP))
        assert rec.mfe_usd == 1.25
        assert rec.mae_usd == -0.40
        assert rec.mfe_at_stop_usd == 0.90
        assert rec.peak_pnl_usd == 1.20
        assert rec.peak_pnl_ms == 1_700_000_000_000
        assert rec.close_path_attempts == 3
        assert rec.close_path_rejected == 2
        assert rec.close_path_first_fail_reason == "reduce_only_would_increase"
        assert rec.pyramid_legs_done == 2
        assert rec.pyramid_tp1_cleared is True
        assert rec.ratchet_rung_reached == "high"
        assert rec.ratchet_peak_roe == 9.4
        assert rec.ratchet_early_arm_fired is True
        assert rec.treasury_cluster_engaged is True
        assert rec.campaign_pyramid_layers == 1

    def test_snap_none_is_legacy_bit_for_bit(self):
        rec = main._build_trade_record(
            _position(), 100.5, "exchange_close", 0.5)
        assert rec.mfe_usd is None
        assert rec.mfe_at_stop_usd is None
        assert rec.peak_pnl_usd is None
        assert rec.close_path_attempts is None
        assert rec.close_path_rejected is None
        assert rec.close_path_first_fail_reason is None
        assert rec.pyramid_legs_done is None
        assert rec.pyramid_tp1_cleared is None
        assert rec.ratchet_rung_reached is None
        assert rec.ratchet_early_arm_fired is None
        assert rec.treasury_cluster_engaged is None
        assert rec.campaign_pyramid_layers is None

    def test_snap_partial_dict_fills_none(self):
        rec = main._build_trade_record(
            _position(), 100.5, "software_stop", 0.5,
            snap={"mfe_usd": 2.0})
        assert rec.mfe_usd == 2.0
        assert rec.pyramid_legs_done is None
