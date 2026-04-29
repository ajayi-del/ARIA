"""
Tests for tria_bridge.safety — budget, kill switch, confirmation gate.
"""

import os
import tempfile
import time

import pytest

from tria_bridge.config import BridgeConfig
from tria_bridge.logger import BridgeLogger
from tria_bridge.safety import DailyBudget, SafetyEngine
from tria_bridge.state_machine import TradeSignal


@pytest.fixture
def cfg():
    c = BridgeConfig()
    # Isolate from any stale kill-switch file on disk
    c.kill_switch_file = os.path.join(tempfile.gettempdir(), "tria_bridge_test_ks")
    return c


@pytest.fixture
def logger(cfg):
    with tempfile.TemporaryDirectory() as td:
        cfg.log_dir = td
        yield BridgeLogger(td)


class TestDailyBudget:
    def test_trade_limit(self):
        b = DailyBudget()
        sig = TradeSignal(symbol="BTC-USD", direction="LONG", size=0.01, notional_usd=100.0)
        assert b.can_trade(sig, max_trades=2, max_notional=1000.0, cooldown_s=0.0) is None
        b.record_trade(sig)
        assert b.can_trade(sig, max_trades=2, max_notional=1000.0, cooldown_s=0.0) is None
        b.record_trade(sig)
        err = b.can_trade(sig, max_trades=2, max_notional=1000.0, cooldown_s=0.0)
        assert err is not None
        assert "daily_trade_limit_reached" in err

    def test_notional_limit(self):
        b = DailyBudget()
        sig = TradeSignal(symbol="BTC-USD", direction="LONG", size=0.01, notional_usd=600.0)
        assert b.can_trade(sig, max_trades=10, max_notional=500.0, cooldown_s=0.0) is not None

    def test_cooldown(self):
        b = DailyBudget()
        sig = TradeSignal(symbol="BTC-USD", direction="LONG", size=0.01)
        b.record_trade(sig)
        err = b.can_trade(sig, max_trades=10, max_notional=10000.0, cooldown_s=10.0)
        assert err is not None
        assert "cooldown_active" in err

    def test_day_rotation(self):
        b = DailyBudget()
        b.day = int(time.time() // 86400) - 1  # yesterday
        b.trades = 99
        b._rotate()
        assert b.trades == 0


class TestSafetyEngine:
    def test_kill_switch_file(self, cfg, logger):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".kill", delete=False) as f:
            f.write("STOP")
            ks = f.name
        cfg.kill_switch_file = ks
        safety = SafetyEngine(cfg, logger)
        assert safety.check_kill_switch() is True
        assert safety.preflight_check(TradeSignal("BTC-USD", "LONG", 0.01)) is not None
        os.unlink(ks)

    def test_preflight_budget_block(self, cfg, logger):
        with tempfile.NamedTemporaryFile(delete=True) as tmp:
            cfg.kill_switch_file = tmp.name  # ensure no stale kill switch on disk
            cfg.max_trades_day = 0
            safety = SafetyEngine(cfg, logger)
            err = safety.preflight_check(TradeSignal("BTC-USD", "LONG", 0.01))
            assert err is not None
            assert "daily_trade_limit_reached" in err

    def test_halt_persists(self, cfg, logger):
        safety = SafetyEngine(cfg, logger)
        safety.halt("test_halt")
        assert safety.preflight_check(TradeSignal("BTC-USD", "LONG", 0.01)) is not None
