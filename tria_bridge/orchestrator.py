"""
tria_bridge/orchestrator.py — Main entry point. Ties watcher → safety → state machine.

Run:
    python -m tria_bridge.orchestrator

Design:
  Single-threaded event loop. Signal watcher runs in a background thread,
  feeding a thread-safe Queue. Main thread pulls signals and executes serially.
  This avoids race conditions on mouse/keyboard while keeping latency low.
"""

from __future__ import annotations

import signal as sigmod
import sys
import time
from queue import Empty, Queue
from typing import Optional

from tria_bridge.config import BridgeConfig
from tria_bridge.executor import Executor
from tria_bridge.logger import BridgeLogger
from tria_bridge.safety import SafetyEngine
from tria_bridge.signal_watcher import SignalWatcher
from tria_bridge.state_machine import ExecutionResult, TradeSignal, TradeStateMachine
from tria_bridge.vision import VisionEngine


class TriaOrchestrator:
    """The General. Receives signals, applies safety, executes via state machine."""

    def __init__(self, config: Optional[BridgeConfig] = None):
        self.cfg = config or BridgeConfig.from_env()
        self.log = BridgeLogger(self.cfg.log_dir)
        self.vis = VisionEngine(self.cfg, self.log)
        self.exe = Executor(self.cfg, self.log)
        self.safety = SafetyEngine(self.cfg, self.log)
        self.queue: Queue[TradeSignal] = Queue()
        self.watcher = SignalWatcher(self.cfg, self.log, self._on_signal)
        self._shutdown = False

    def _on_signal(self, signal: TradeSignal) -> None:
        """Callback from watcher thread → enqueue for main thread."""
        self.queue.put(signal)

    def _execute_one(self, signal: TradeSignal) -> ExecutionResult:
        """Full safety + execution pipeline for a single signal."""
        t0 = time.perf_counter()

        # 1. Safety preflight
        preflight = self.safety.preflight_check(signal)
        if preflight:
            self.log.warning("trade_preflight_rejected", reason=preflight, signal=signal.__dict__)
            return ExecutionResult(success=False, state_reached=None, latency_ms=0.0, error=preflight)

        # 2. User confirmation
        if not self.safety.confirm_with_user(signal):
            self.log.info("trade_rejected_by_user", signal=signal.__dict__)
            return ExecutionResult(success=False, state_reached=None, latency_ms=0.0, error="user_rejected")

        # 3. Execute
        fsm = TradeStateMachine(self.cfg, self.log, self.vis, self.exe)
        result = fsm.execute(signal)

        # 4. Bookkeeping
        if result.success:
            self.safety.record_executed(signal)

        total_ms = (time.perf_counter() - t0) * 1000
        self.log.info(
            "trade_pipeline_complete",
            success=result.success,
            total_latency_ms=round(total_ms, 2),
            execution_latency_ms=round(result.latency_ms, 2),
            error=result.error,
            fill_confirmed=result.fill_confirmed,
        )
        return result

    def run(self) -> None:
        """Blocking main loop."""
        self.log.info("orchestrator_start", version="1.0.0", config=self.cfg.__dict__)
        self.watcher.start()

        # Graceful shutdown on SIGINT/SIGTERM
        def _signal_handler(signum, frame):
            self._shutdown = True
            self.log.info("shutdown_signal_received", signum=signum)

        sigmod.signal(sigmod.SIGINT, _signal_handler)
        sigmod.signal(sigmod.SIGTERM, _signal_handler)

        try:
            while not self._shutdown:
                try:
                    signal = self.queue.get(timeout=1.0)
                except Empty:
                    continue
                if self.safety.check_kill_switch():
                    self.log.error("orchestrator_halted_kill_switch")
                    break
                self._execute_one(signal)
        finally:
            self.watcher.stop()
            self.log.info("orchestrator_shutdown", stats=self.safety.daily_stats())


def main() -> None:
    orch = TriaOrchestrator()
    orch.run()


if __name__ == "__main__":
    main()
