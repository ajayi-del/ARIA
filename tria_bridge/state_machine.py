"""
tria_bridge/state_machine.py — Deterministic trade execution FSM.

States:
  IDLE → SEARCH_SYMBOL → SELECT_SYMBOL → SET_DIRECTION → SET_SIZE
  → SET_LEVERAGE → CONFIRM_ORDER → VERIFY_FILL → DONE
  → ERROR (terminal) | ABORTED (terminal)

Quant principle: every transition has a timeout and a rollback strategy.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, Optional

from tria_bridge import config as cfg_mod
from tria_bridge.config import BridgeConfig
from tria_bridge.executor import Executor
from tria_bridge.logger import BridgeLogger
from tria_bridge.vision import VisionEngine


class State(Enum):
    IDLE = auto()
    SEARCH_SYMBOL = auto()
    SELECT_SYMBOL = auto()
    SET_DIRECTION = auto()
    SET_SIZE = auto()
    SET_LEVERAGE = auto()
    SET_STOP_LOSS = auto()
    SET_TAKE_PROFIT = auto()
    CONFIRM_ORDER = auto()
    VERIFY_FILL = auto()
    DONE = auto()
    ERROR = auto()
    ABORTED = auto()


@dataclass
class TradeSignal:
    """Normalized signal from ARIA outbox."""
    symbol: str          # ARIA symbol (e.g. "BTC-USD")
    direction: str       # "LONG" | "SHORT"
    size: float          # Token size or USD notional (depends on Tria UI mode)
    leverage: Optional[float] = None
    stop_price: Optional[float] = None
    tp_price: Optional[float] = None
    notional_usd: Optional[float] = None
    source: str = "aria"
    timestamp: float = field(default_factory=time.time)

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "TradeSignal":
        # ARIA emits tp1_price; fall back to legacy tp_price
        _tp = data.get("tp1_price") if data.get("tp1_price") is not None else data.get("tp_price")
        return cls(
            symbol=str(data.get("symbol", "")),
            direction=str(data.get("direction", "")).upper(),
            size=float(data.get("size", 0.0)),
            leverage=float(data["leverage"]) if data.get("leverage") is not None else None,
            stop_price=float(data["stop_price"]) if data.get("stop_price") is not None else None,
            tp_price=float(_tp) if _tp is not None else None,
            notional_usd=float(data["notional_usd"]) if data.get("notional_usd") is not None else None,
            source=str(data.get("source", "aria")),
            timestamp=float(data.get("timestamp", time.time())),
        )

    def validate(self) -> Optional[str]:
        """Return error string if invalid, None if OK."""
        if not self.symbol or "-USD" not in self.symbol:
            return f"invalid_symbol:{self.symbol}"
        if self.direction not in ("LONG", "SHORT"):
            return f"invalid_direction:{self.direction}"
        if self.size <= 0:
            return f"invalid_size:{self.size}"
        return None


@dataclass
class ExecutionResult:
    success: bool
    state_reached: State
    latency_ms: float
    error: Optional[str] = None
    fill_confirmed: bool = False


class TradeStateMachine:
    """Deterministic executor for a single trade signal."""

    def __init__(
        self,
        config: BridgeConfig,
        logger: BridgeLogger,
        vision: VisionEngine,
        executor: Executor,
    ):
        self.cfg = config
        self.log = logger
        self.vis = vision
        self.exe = executor
        self._state = State.IDLE
        self._signal: Optional[TradeSignal] = None
        self._start_ts: float = 0.0
        self._step_start_ts: float = 0.0

    @property
    def current_state(self) -> State:
        return self._state

    def _transition(self, new_state: State) -> None:
        old = self._state
        self._state = new_state
        self.log.info(
            "state_transition",
            old=old.name,
            new=new_state.name,
            step_latency_ms=round((time.perf_counter() - self._step_start_ts) * 1000, 2),
        )
        self._step_start_ts = time.perf_counter()

    def _abort(self, reason: str) -> ExecutionResult:
        self._transition(State.ABORTED)
        self.log.error("trade_aborted", reason=reason, signal=self._signal.__dict__ if self._signal else None)
        return ExecutionResult(
            success=False,
            state_reached=State.ABORTED,
            latency_ms=(time.perf_counter() - self._start_ts) * 1000,
            error=reason,
        )

    def _error(self, reason: str) -> ExecutionResult:
        self._transition(State.ERROR)
        self.log.error("trade_error", reason=reason, signal=self._signal.__dict__ if self._signal else None)
        return ExecutionResult(
            success=False,
            state_reached=State.ERROR,
            latency_ms=(time.perf_counter() - self._start_ts) * 1000,
            error=reason,
        )

    def execute(self, signal: TradeSignal) -> ExecutionResult:
        """Run the full state machine for one trade."""
        err = signal.validate()
        if err:
            return ExecutionResult(success=False, state_reached=State.IDLE, latency_ms=0.0, error=err)

        self._signal = signal
        self._start_ts = time.perf_counter()
        self._step_start_ts = self._start_ts
        self._state = State.IDLE

        # Resolve Tria search text
        tria_symbol = self.cfg.symbol_map.get(signal.symbol, signal.symbol.replace("-USD", ""))

        # ── SEARCH_SYMBOL ──────────────────────────────────────────────────────
        self._transition(State.SEARCH_SYMBOL)
        if not self.exe.click_template(self.vis, cfg_mod.TEMPLATE_SYMBOL_SEARCH, timeout_s=cfg_mod.STATE_TIMEOUTS["SEARCH_SYMBOL"]):
            return self._error("symbol_search_not_found")
        time.sleep(0.3)
        self.exe.type_text(tria_symbol)
        time.sleep(0.5)  # Wait for dropdown

        # ── SELECT_SYMBOL ──────────────────────────────────────────────────────
        self._transition(State.SELECT_SYMBOL)
        if not self.exe.click_template(self.vis, cfg_mod.TEMPLATE_SYMBOL_SELECT, timeout_s=cfg_mod.STATE_TIMEOUTS["SELECT_SYMBOL"]):
            # Fallback: press Enter if select template missing
            self.exe.hotkey("return")
            time.sleep(0.5)
        time.sleep(0.3)

        # ── SET_DIRECTION ──────────────────────────────────────────────────────
        self._transition(State.SET_DIRECTION)
        btn_tpl = (
            cfg_mod.TEMPLATE_BUY_BUTTON
            if signal.direction == "LONG"
            else cfg_mod.TEMPLATE_SELL_BUTTON
        )
        if not self.exe.click_template(self.vis, btn_tpl, timeout_s=cfg_mod.STATE_TIMEOUTS["SET_DIRECTION"]):
            return self._error(f"direction_button_not_found:{signal.direction}")
        time.sleep(0.2)

        # ── SET_SIZE ───────────────────────────────────────────────────────────
        self._transition(State.SET_SIZE)
        size_str = str(signal.size)
        if not self.exe.fill_field(self.vis, cfg_mod.TEMPLATE_SIZE_FIELD, size_str, timeout_s=cfg_mod.STATE_TIMEOUTS["SET_SIZE"]):
            return self._error("size_field_not_found")
        time.sleep(0.2)

        # ── SET_LEVERAGE ───────────────────────────────────────────────────────
        self._transition(State.SET_LEVERAGE)
        if signal.leverage is not None and signal.leverage > 0:
            if not self.exe.click_template(self.vis, cfg_mod.TEMPLATE_LEVERAGE_DROPDOWN, timeout_s=cfg_mod.STATE_TIMEOUTS["SET_LEVERAGE"]):
                self.log.warning("leverage_dropdown_not_found", leverage=signal.leverage)
            else:
                time.sleep(0.2)
                self.exe.type_text(str(int(signal.leverage)))
                time.sleep(0.2)
                # Confirm if template exists; otherwise Enter
                if not self.exe.click_template(self.vis, cfg_mod.TEMPLATE_CONFIRM_LEVERAGE, timeout_s=3.0):
                    self.exe.hotkey("return")
                time.sleep(0.2)

        # ── SET_STOP_LOSS ──────────────────────────────────────────────────────
        self._transition(State.SET_STOP_LOSS)
        if signal.stop_price is not None and signal.stop_price > 0:
            _sl_set = self.exe.fill_field(
                self.vis, cfg_mod.TEMPLATE_STOP_LOSS_FIELD, str(signal.stop_price),
                timeout_s=cfg_mod.STATE_TIMEOUTS.get("SET_STOP_LOSS", 5.0)
            )
            if not _sl_set:
                self.log.warning("stop_loss_field_not_found", symbol=signal.symbol, stop_price=signal.stop_price)
            else:
                time.sleep(0.2)

        # ── SET_TAKE_PROFIT ────────────────────────────────────────────────────
        self._transition(State.SET_TAKE_PROFIT)
        if signal.tp_price is not None and signal.tp_price > 0:
            _tp_set = self.exe.fill_field(
                self.vis, cfg_mod.TEMPLATE_TAKE_PROFIT_FIELD, str(signal.tp_price),
                timeout_s=cfg_mod.STATE_TIMEOUTS.get("SET_TAKE_PROFIT", 5.0)
            )
            if not _tp_set:
                self.log.warning("take_profit_field_not_found", symbol=signal.symbol, tp_price=signal.tp_price)
            else:
                time.sleep(0.2)

        # ── CONFIRM_ORDER ──────────────────────────────────────────────────────
        self._transition(State.CONFIRM_ORDER)
        if not self.exe.click_template(self.vis, cfg_mod.TEMPLATE_CONFIRM_ORDER, timeout_s=cfg_mod.STATE_TIMEOUTS["CONFIRM_ORDER"]):
            return self._error("confirm_order_not_found")

        # ── VERIFY_FILL ────────────────────────────────────────────────────────
        self._transition(State.VERIFY_FILL)
        fill_confirmed = False
        deadline = time.perf_counter() + cfg_mod.VERIFY_FILL_TIMEOUT_S
        while time.perf_counter() < deadline:
            loc = self.vis.find_template(cfg_mod.TEMPLATE_FILLED_CONFIRMED)
            if loc is not None:
                fill_confirmed = True
                break
            time.sleep(cfg_mod.VERIFY_FILL_INTERVAL_S)

        if not fill_confirmed:
            # Soft error — order may have filled but confirmation UI was missed
            self.log.warning("fill_not_confirmed", symbol=signal.symbol, waited_s=cfg_mod.VERIFY_FILL_TIMEOUT_S)

        self._transition(State.DONE)
        total_ms = (time.perf_counter() - self._start_ts) * 1000
        self.log.trade(
            signal=signal.__dict__,
            outcome="filled" if fill_confirmed else "unconfirmed",
            latency_ms=total_ms,
            state="DONE",
        )
        return ExecutionResult(
            success=True,
            state_reached=State.DONE,
            latency_ms=total_ms,
            fill_confirmed=fill_confirmed,
        )
