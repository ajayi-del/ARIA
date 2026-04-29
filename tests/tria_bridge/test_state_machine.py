"""
Tests for tria_bridge.state_machine — deterministic FSM logic.
"""

import json
import os
import tempfile
import time
from unittest.mock import MagicMock

import pytest

from tria_bridge.config import BridgeConfig
from tria_bridge.executor import Executor
from tria_bridge.logger import BridgeLogger
from tria_bridge.state_machine import ExecutionResult, State, TradeSignal, TradeStateMachine
from tria_bridge.vision import VisionEngine


@pytest.fixture
def cfg():
    return BridgeConfig()


@pytest.fixture
def logger(cfg):
    with tempfile.TemporaryDirectory() as td:
        cfg.log_dir = td
        yield BridgeLogger(td)


@pytest.fixture
def mock_vision():
    v = MagicMock(spec=VisionEngine)
    v.find_template.return_value = (100, 200, 0.95)
    v.wait_for_template.return_value = (100, 200, 0.95)
    return v


@pytest.fixture
def mock_executor():
    e = MagicMock(spec=Executor)
    e.click_template.return_value = True
    e.fill_field.return_value = True
    e.type_text.return_value = None
    e.hotkey.return_value = None
    return e


class TestTradeSignal:
    def test_valid_signal(self):
        s = TradeSignal(symbol="BTC-USD", direction="LONG", size=0.01, leverage=5.0)
        assert s.validate() is None

    def test_invalid_symbol(self):
        s = TradeSignal(symbol="BTC", direction="LONG", size=0.01)
        assert s.validate() is not None

    def test_invalid_direction(self):
        s = TradeSignal(symbol="BTC-USD", direction="FLAT", size=0.01)
        assert "invalid_direction" in s.validate()

    def test_invalid_size(self):
        s = TradeSignal(symbol="BTC-USD", direction="LONG", size=0.0)
        assert "invalid_size" in s.validate()

    def test_from_json(self):
        raw = {
            "symbol": "ETH-USD",
            "direction": "short",
            "size": 0.5,
            "leverage": 3,
            "timestamp": time.time(),
        }
        s = TradeSignal.from_json(raw)
        assert s.symbol == "ETH-USD"
        assert s.direction == "SHORT"
        assert s.size == 0.5


class TestTradeStateMachine:
    def test_execute_success(self, cfg, logger, mock_vision, mock_executor):
        fsm = TradeStateMachine(cfg, logger, mock_vision, mock_executor)
        sig = TradeSignal(symbol="BTC-USD", direction="LONG", size=0.01, leverage=5.0)
        result = fsm.execute(sig)
        assert result.success is True
        assert result.state_reached == State.DONE
        assert result.latency_ms > 0

    def test_execute_invalid_signal(self, cfg, logger, mock_vision, mock_executor):
        fsm = TradeStateMachine(cfg, logger, mock_vision, mock_executor)
        sig = TradeSignal(symbol="BAD", direction="LONG", size=0.0)
        result = fsm.execute(sig)
        assert result.success is False
        assert result.error is not None

    def test_execute_direction_button_missing(self, cfg, logger, mock_vision, mock_executor):
        mock_executor.click_template.side_effect = [True, True, False]  # search, select, direction fails
        fsm = TradeStateMachine(cfg, logger, mock_vision, mock_executor)
        sig = TradeSignal(symbol="BTC-USD", direction="LONG", size=0.01)
        result = fsm.execute(sig)
        assert result.success is False
        assert "direction_button_not_found" in result.error

    def test_state_transitions_logged(self, cfg, logger, mock_vision, mock_executor):
        fsm = TradeStateMachine(cfg, logger, mock_vision, mock_executor)
        sig = TradeSignal(symbol="BTC-USD", direction="SHORT", size=0.01)
        fsm.execute(sig)
        # Logger should have state_transition entries
        assert logger._action_counter > 0
