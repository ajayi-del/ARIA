"""Contract pin: _has_actionable_position vs the REAL PositionManager.

2026-08-28 audit: the helper iterated pm.get_all().items() while get_all()
returns a flattened LIST (risk/position_manager.py:25). Every balance-monitor
cycle threw AttributeError → balance_monitor_loop_error ×1593/14h → the
drawdown manager, daily resets, and the 2026-08-27 withdrawal-netting
classifier were dead at runtime. These tests use the real PositionManager
so a contract drift fails loudly instead of silently freezing the loop.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.schemas import Position  # noqa: E402
from risk.position_manager import PositionManager  # noqa: E402


def _pos(symbol: str, entry: float, size: float) -> Position:
    return Position(
        symbol=symbol, side="long", entry_price=entry, size=size,
        stop_price=entry * 0.99, tp1_price=entry * 1.02,
        tp2_price=entry * 1.04, tp3_price=entry * 1.06,
        liq_price=entry * 0.8, initial_margin=1.0, leverage=5,
        opened_at_ms=0,
    )


def test_get_all_returns_flat_list():
    pm = PositionManager()
    pm.add(_pos("ETH-USD", 2500.0, 0.01))
    pm.add(_pos("BTC-USD", 80000.0, 0.001))
    all_pos = pm.get_all()
    assert isinstance(all_pos, list)
    assert {p.symbol for p in all_pos} == {"ETH-USD", "BTC-USD"}


def test_actionable_false_on_empty_book():
    from main import _has_actionable_position
    assert _has_actionable_position(PositionManager()) is False


def test_actionable_false_on_dust_only_book():
    from main import _has_actionable_position
    pm = PositionManager()
    # ETH dust: 0.0001 × 2500 = $0.25 < $10 SoDEX close min
    pm.add(_pos("ETH-USD", 2500.0, 0.0001))
    assert _has_actionable_position(pm) is False


def test_actionable_true_when_any_position_closable():
    from main import _has_actionable_position
    pm = PositionManager()
    pm.add(_pos("ETH-USD", 2500.0, 0.0001))   # dust
    pm.add(_pos("BTC-USD", 80000.0, 0.001))   # $80 ≥ $10
    assert _has_actionable_position(pm) is True
