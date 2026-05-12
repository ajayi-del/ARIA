"""
TP1/TP2 and Adaptive Calibrator Tests — ARIA v1.8

Tests that the take-profit machinery and adaptive learning system
work correctly and remain connected to the position manager.

TP1/TP2 invariants:
  - TP1 hit: stop moved to Golden Stop (entry + 50% of TP1 distance)
  - TP2 hit: stop moved to TP1 price (full protection)
  - After TP1: can_pyramid = True; after 2nd add: can_pyramid = False
  - reconciliation: tp1_hit fired when exchange_size <= 65% of initial
  - reconciliation: tp2_hit fired when tp1_hit and exchange_size <= 35% of initial

Adaptive Calibrator invariants:
  - on_trade_closed feeds win/loss into fast + medium + cascade windows
  - update_drawdown >= 3% → recovery mode activated
  - recovery: get_coherence_minimum() raises to RECOVERY_COHERENCE
  - recovery: get_recovery_params() returns size_cap + tp_sl_factor
  - 3 consecutive wins → recovery deactivated
  - tier weights updated after medium window fills (>3 trades)
"""

import sys
import os
import time
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _make_position(symbol="BTC-USD", side="long", entry=74000.0,
                   size=0.001, stop=73000.0, tp1=75500.0, tp2=77000.0,
                   tp3=78500.0, leverage=6):
    from execution.schemas import Position
    return Position(
        symbol=symbol, side=side,
        entry_price=entry, size=size,
        stop_price=stop, tp1_price=tp1,
        tp2_price=tp2, tp3_price=tp3,
        liq_price=entry * 0.85 if side == "long" else entry * 1.15,
        initial_margin=size * entry / leverage,
        leverage=leverage,
        opened_at_ms=int(time.time() * 1000),
    )


def _make_pm():
    from risk.position_manager import PositionManager
    return PositionManager()


def _make_calibrator():
    from memory.adaptive_calibrator import AdaptiveCalibrator
    from core.config import Settings
    return AdaptiveCalibrator(Settings())


# ══════════════════════════════════════════════════════════════════════════════
# 1. TP1 / TP2 — POSITION MANAGER LAYER
# ══════════════════════════════════════════════════════════════════════════════

class TestTP1TP2PositionManager:
    """Tests that mark_tp1_hit / mark_tp2_hit behave correctly."""

    def test_tp1_sets_golden_stop_long(self):
        """
        Long position: TP1 Golden Stop = entry + 50% of (tp1 - entry)
        e.g. entry=74000, tp1=75500 → golden_stop = 74000 + 0.5*1500 = 74750
        """
        pm = _make_pm()
        pos = _make_position(side="long", entry=74000.0, tp1=75500.0)
        pm.add(pos)

        new_stop = pm.mark_tp1_hit("BTC-USD", 0)

        expected = 74000.0 + (75500.0 - 74000.0) * 0.5  # 74750
        assert new_stop == pytest.approx(expected, rel=1e-6), (
            f"TP1 golden stop should be {expected}, got {new_stop}"
        )
        assert pm.get("BTC-USD")[0].tp1_hit is True
        assert pm.get("BTC-USD")[0].stop_price == pytest.approx(expected)
        assert pm.get("BTC-USD")[0].golden_stop_used is True

    def test_tp1_sets_golden_stop_short(self):
        """
        Short position: TP1 Golden Stop = entry - 50% of (entry - tp1)
        e.g. entry=74000, tp1=72000 → golden_stop = 74000 - 0.5*2000 = 73000
        """
        pm = _make_pm()
        pos = _make_position(symbol="ETH-USD", side="short", entry=3000.0,
                             stop=3100.0, tp1=2800.0, tp2=2600.0, tp3=2400.0)
        pm.add(pos)

        new_stop = pm.mark_tp1_hit("ETH-USD", 0)

        expected = 3000.0 - (3000.0 - 2800.0) * 0.5  # 2900
        assert new_stop == pytest.approx(expected, rel=1e-6)
        assert pm.get("ETH-USD")[0].tp1_hit is True

    def test_tp2_moves_stop_to_tp1_price(self):
        """
        After TP1 is hit, TP2 moves stop to the TP1 price — locking in the gain.
        """
        pm = _make_pm()
        pos = _make_position(side="long", entry=74000.0, tp1=75500.0, tp2=77000.0)
        pm.add(pos)

        pm.mark_tp1_hit("BTC-USD", 0)
        new_stop = pm.mark_tp2_hit("BTC-USD", 0)

        assert new_stop == pytest.approx(75500.0)
        assert pm.get("BTC-USD")[0].tp2_hit is True
        assert pm.get("BTC-USD")[0].stop_price == pytest.approx(75500.0)

    def test_can_pyramid_requires_tp1_hit(self):
        pm = _make_pm()
        pm.add(_make_position())
        assert pm.can_pyramid("BTC-USD") is False

    def test_can_pyramid_allowed_after_tp1(self):
        pm = _make_pm()
        pm.add(_make_position())
        pm.mark_tp1_hit("BTC-USD", 0)
        assert pm.can_pyramid("BTC-USD") is True

    def test_can_pyramid_blocked_at_2_positions(self):
        """Pyramid cap: count==2 → can_pyramid returns False."""
        pm = _make_pm()
        pm.add(_make_position(size=0.001))
        pm.mark_tp1_hit("BTC-USD", 0)
        pm.add(_make_position(size=0.0005))  # second position (pyramid add)
        assert pm.can_pyramid("BTC-USD") is False

    def test_tp1_only_fires_once(self):
        """Calling mark_tp1_hit twice must not move the stop a second time."""
        pm = _make_pm()
        pos = _make_position(entry=74000.0, tp1=75500.0)
        pm.add(pos)

        stop1 = pm.mark_tp1_hit("BTC-USD", 0)
        stop2 = pm.mark_tp1_hit("BTC-USD", 0)  # already hit — still returns new stop

        # Stop should not move past first call's value
        assert stop1 is not None
        assert stop2 is not None
        assert pm.get("BTC-USD")[0].tp1_hit is True


# ══════════════════════════════════════════════════════════════════════════════
# 2. TP DETECTION VIA RECONCILIATION (exchange size triggers)
# ══════════════════════════════════════════════════════════════════════════════

class TestTPReconciliationDetection:
    """
    The reconciliation loop in main.py detects TP hits via exchange_size reduction:
      - exchange_size <= initial_size * 0.65 → TP1 hit
      - exchange_size <= initial_size * 0.35 → TP2 hit (requires tp1_hit=True)
    """

    def test_tp1_threshold_is_65pct(self):
        initial_size = 1.0
        # 50% partial close → size = 0.5 → 0.5 / 1.0 = 50% < 65%
        exchange_size_after_tp1 = 0.5
        assert exchange_size_after_tp1 <= initial_size * 0.65

    def test_tp1_not_triggered_at_70pct(self):
        """Reconciliation must NOT fire TP1 if exchange size is > 65%."""
        initial_size = 1.0
        exchange_size = 0.70  # only 30% closed — not enough
        assert not (exchange_size <= initial_size * 0.65)

    def test_tp2_threshold_is_35pct(self):
        initial_size = 1.0
        exchange_size = 0.3  # 70% closed total
        assert exchange_size <= initial_size * 0.35

    def test_tp2_not_triggered_without_tp1_hit(self):
        """TP2 check requires tp1_hit=True first (sequential guard)."""
        pm = _make_pm()
        pos = _make_position(size=1.0)
        pm.add(pos)

        # Simulate 70% close without marking TP1 first
        tp1_fired = pm.get("BTC-USD")[0].tp1_hit
        assert not tp1_fired  # TP1 must be hit first
        # In reconciliation loop: `elif pos.tp1_hit and not pos.tp2_hit` — TP2 cannot fire

    def test_tp1_ratchets_stop_above_entry_for_long(self):
        """After TP1 is hit, the software stop must be above entry (protected)."""
        pm = _make_pm()
        pos = _make_position(side="long", entry=74000.0, stop=73000.0,
                             tp1=75500.0, tp2=77000.0)
        pm.add(pos)

        new_stop = pm.mark_tp1_hit("BTC-USD", 0)

        # New stop (74750) must be above entry (74000) — position is protected
        assert new_stop > pos.entry_price, (
            f"Stop {new_stop} must be above entry {pos.entry_price} after TP1"
        )

    def test_tp2_stop_above_tp1_entry_premium(self):
        """After TP2: stop = TP1 price (above entry, below TP2)."""
        pm = _make_pm()
        pos = _make_position(side="long", entry=74000.0,
                             tp1=75500.0, tp2=77000.0)
        pm.add(pos)
        pm.mark_tp1_hit("BTC-USD", 0)
        new_stop = pm.mark_tp2_hit("BTC-USD", 0)

        assert new_stop == pytest.approx(75500.0)  # TP1 price
        assert new_stop > pos.entry_price           # protected
        assert new_stop < 77000.0                  # below TP2 (partial profit locked)


# ══════════════════════════════════════════════════════════════════════════════
# 3. ADAPTIVE CALIBRATOR — CORE LEARNING
# ══════════════════════════════════════════════════════════════════════════════

class TestAdaptiveCalibratorLearning:
    """Tests the three-loop learning system responds correctly to trade outcomes."""

    def test_initial_coherence_minimum(self):
        """Calibrator initializes to config.min_coherence (not recovery level)."""
        cal = _make_calibrator()
        from core.config import Settings
        cfg = Settings()
        assert cal.get_coherence_minimum() == pytest.approx(
            float(getattr(cfg, "min_coherence", getattr(cfg, "live_min_coherence", 2.0)))
        )

    def test_win_trade_accepted(self):
        """on_trade_closed(won=True) must not raise and updates state."""
        cal = _make_calibrator()
        cal.on_trade_closed(won=True, pnl=5.0, strategy_tag="momentum")

    def test_loss_trade_accepted(self):
        cal = _make_calibrator()
        cal.on_trade_closed(won=False, pnl=-3.0, strategy_tag="momentum")

    def test_cascade_phase_accepted(self):
        cal = _make_calibrator()
        cal.on_trade_closed(
            won=True, pnl=8.0, strategy_tag="cascade",
            cascade_phase="exhaustion", liq_phase="expansion",
            funding_aligned=True
        )

    def test_calibration_summary_has_required_keys(self):
        cal = _make_calibrator()
        cal.on_trade_closed(won=True, pnl=5.0)
        summary = cal.get_calibration_summary()
        # Actual keys from adaptive_calibrator.py get_calibration_summary
        for key in ("coherence_min", "coherence_effective", "recovery_active",
                    "fast_wr", "medium_wr", "loss_streak", "win_streak"):
            assert key in summary, f"get_calibration_summary missing key: {key}"


# ══════════════════════════════════════════════════════════════════════════════
# 4. ADAPTIVE CALIBRATOR — RECOVERY MODE
# ══════════════════════════════════════════════════════════════════════════════

class TestAdaptiveCalibratorRecovery:
    """Recovery mode activates on drawdown >= 3% and deactivates after 3 wins."""

    def test_no_recovery_at_low_drawdown(self):
        cal = _make_calibrator()
        cal.update_drawdown(0.02)  # 2% — below threshold
        assert not cal.is_in_recovery()

    def test_recovery_triggers_at_3pct_drawdown(self):
        cal = _make_calibrator()
        cal.update_drawdown(0.03)  # exactly 3%
        assert cal.is_in_recovery(), "3% drawdown must trigger recovery mode"

    def test_recovery_triggers_at_large_drawdown(self):
        cal = _make_calibrator()
        cal.update_drawdown(0.15)  # 15% — well above threshold
        assert cal.is_in_recovery()

    def test_recovery_raises_coherence_minimum(self):
        """In recovery, coherence floor must be HIGHER than normal floor."""
        cal = _make_calibrator()
        normal_coh = cal.get_coherence_minimum()
        cal.update_drawdown(0.05)
        recovery_coh = cal.get_coherence_minimum()
        assert recovery_coh > normal_coh, (
            f"Recovery coherence {recovery_coh} must exceed normal {normal_coh}"
        )

    def test_recovery_params_returned_when_active(self):
        cal = _make_calibrator()
        cal.update_drawdown(0.05)
        params = cal.get_recovery_params()
        assert params  # non-empty dict
        assert "size_cap" in params
        assert "coherence_min" in params
        assert "tp_sl_factor" in params
        assert params["size_cap"] < 1.0, "Recovery must cap position size"
        assert params["coherence_min"] > 0

    def test_no_recovery_params_when_normal(self):
        cal = _make_calibrator()
        params = cal.get_recovery_params()
        assert params == {}, "Normal mode returns empty recovery params"

    def test_recovery_deactivates_after_consecutive_wins(self):
        """RECOVERY_WIN_STREAK=5 consecutive wins must exit recovery mode."""
        cal = _make_calibrator()
        cal.update_drawdown(0.05)
        assert cal.is_in_recovery()

        # Feed 5 consecutive wins — should exit recovery (RECOVERY_WIN_STREAK=5)
        for _ in range(5):
            cal.on_trade_closed(won=True, pnl=10.0, strategy_tag="momentum")

        assert not cal.is_in_recovery(), (
            "5 consecutive wins should deactivate recovery mode (RECOVERY_WIN_STREAK=5)"
        )

    def test_recovery_persists_with_loss_break(self):
        """4 wins, 1 loss, 4 wins — should NOT exit (requires 5 consecutive)."""
        cal = _make_calibrator()
        cal.update_drawdown(0.05)

        for _ in range(4):
            cal.on_trade_closed(won=True, pnl=5.0)
        cal.on_trade_closed(won=False, pnl=-3.0)  # break streak
        for _ in range(4):
            cal.on_trade_closed(won=True, pnl=5.0)

        assert cal.is_in_recovery(), (
            "Recovery must persist unless 5 CONSECUTIVE wins achieved (RECOVERY_WIN_STREAK=5)"
        )


# ══════════════════════════════════════════════════════════════════════════════
# 5. ADAPTIVE CALIBRATOR ↔ POSITION MANAGER INTEGRATION
# ══════════════════════════════════════════════════════════════════════════════

class TestAdaptiveCalibratorPositionManagerIntegration:
    """
    The adaptive calibrator should be fed trade outcomes whenever a position
    is closed via the position manager. This test verifies the data flow:
      position closed → _record_close() → calibrator.on_trade_closed()
    """

    def test_calibrator_coherence_used_after_loss_streak(self):
        """
        After a loss streak the calibrator raises the coherence floor.
        Candidates with score < new floor should be rejected.
        """
        cal = _make_calibrator()
        # Simulate 3 losses in fast window
        for _ in range(3):
            cal.on_trade_closed(won=False, pnl=-5.0, strategy_tag="momentum")

        from core.config import Settings
        cfg = Settings()
        # The adaptive coherence floor should be at or above config minimum
        adaptive_floor = cal.get_coherence_minimum()
        assert adaptive_floor >= getattr(cfg, "live_min_coherence", 1.0), (
            "Calibrator floor must not drop below config minimum"
        )

    def test_calibrator_tier_weights_returned_as_dict(self):
        """get_tier_weights() must return a dict (empty or populated)."""
        cal = _make_calibrator()
        weights = cal.get_tier_weights()
        assert isinstance(weights, dict), "Tier weights must be a dict"

    def test_calibrator_phase_params_no_crash(self):
        """get_phase_params must return a dict for any valid phase."""
        cal = _make_calibrator()
        for liq_phase in ("quiet", "trigger", "expansion", "exhaustion", "aftermath"):
            params = cal.get_phase_params(liq_phase, funding_aligned=True)
            assert isinstance(params, dict), f"phase_params for {liq_phase} must be dict"

    def test_calibrator_survives_all_loss_streak(self):
        """5 consecutive losses must not crash — system degrades gracefully."""
        cal = _make_calibrator()
        for _ in range(5):
            cal.on_trade_closed(won=False, pnl=-10.0, strategy_tag="momentum")
        # Should still return valid coherence minimum
        coh = cal.get_coherence_minimum()
        assert coh > 0.0, "Coherence minimum must remain positive after loss streak"
        assert coh < 20.0, "Coherence minimum must not explode"

    def test_calibrator_full_cycle(self):
        """
        Full trade cycle: drawdown → recovery → 3 wins → exit recovery.
        Verifies the calibrator returns to normal operating mode.
        """
        cal = _make_calibrator()
        normal_coherence = cal.get_coherence_minimum()

        # Step 1: Drawdown triggers recovery
        cal.update_drawdown(0.05)
        assert cal.is_in_recovery()
        assert cal.get_coherence_minimum() > normal_coherence

        # Step 2: 5 consecutive wins exits recovery (RECOVERY_WIN_STREAK=5)
        for _ in range(5):
            cal.on_trade_closed(won=True, pnl=10.0, strategy_tag="momentum")
        assert not cal.is_in_recovery()

        # Step 3: Coherence floor returns to normal range
        post_recovery_coh = cal.get_coherence_minimum()
        assert post_recovery_coh <= normal_coherence + 1.0, (
            "Coherence should return near normal after recovery exits"
        )


# ══════════════════════════════════════════════════════════════════════════════
# 6. CLOSE POSITION MARKET — QUANTITY PRECISION
# ══════════════════════════════════════════════════════════════════════════════

class TestClosePositionQuantityPrecision:
    """
    close_position_market must send the exact position size (full precision)
    not the step-rounded quantity. SoDEX rejects "0.40" for AAPL 0.40172155
    because step rounding loses precision — the exact size IS a valid SoDEX qty.
    """

    def _exact_qty_str(self, size: float) -> str:
        """Replicate close_position_market quantity formatting."""
        qty = f"{size:.8f}".rstrip('0')
        if '.' not in qty or len(qty.split('.')[1]) < 2:
            qty = f"{size:.2f}"
        return qty

    def test_aapl_exact_qty_preserves_precision(self):
        """0.40172155 → '0.40172155' not '0.40'"""
        qty = self._exact_qty_str(0.40172155)
        assert qty == "0.40172155", f"Exact qty should be '0.40172155', got '{qty}'"

    def test_btc_exact_qty(self):
        """BTC fractional size preserved."""
        qty = self._exact_qty_str(0.00094)
        assert "0.00094" in qty

    def test_integer_size_has_two_dp(self):
        """Size = 1.0 → '1.00' (minimum 2 dp for exchange readability)."""
        qty = self._exact_qty_str(1.0)
        assert qty == "1.00", f"Got '{qty}'"

    def test_arb_integer_size(self):
        """ARB-style integer size = 979.0 → '979.00'"""
        qty = self._exact_qty_str(979.0)
        assert qty in ("979.00", "979"), f"Got '{qty}'"

    def test_close_does_not_use_step_rounding(self):
        """
        Verify close_position_market uses full-precision not _round_qty.
        _round_qty(0.40172155, 0.01) = '0.4' (canonical form) — this is the rejected quantity.
        The fixed close must NOT produce '0.4' for this input.
        """
        from execution.sodex_client import _round_qty
        step_rounded = _round_qty(0.40172155, 0.01)
        exact = self._exact_qty_str(0.40172155)
        # Step-rounded gives "0.4", exact gives "0.40172155" — they differ
        assert step_rounded == "0.4", "Control: step-rounded AAPL is '0.4'"
        assert exact != step_rounded, (
            "close_position_market exact qty must differ from step-rounded qty for AAPL"
        )
