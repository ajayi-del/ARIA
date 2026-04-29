"""
tria_bridge/signal_watcher.py — Watchdog-based file monitor for ARIA outbox.

Debounces rapid modifications and validates JSON schema before enqueuing.
"""

from __future__ import annotations

import json
import os
import time
from queue import Queue
from typing import Callable, Optional

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
except ImportError:
    Observer = None  # type: ignore
    FileSystemEventHandler = object  # type: ignore

from tria_bridge.config import BridgeConfig
from tria_bridge.logger import BridgeLogger
from tria_bridge.state_machine import TradeSignal


class SignalEventHandler(FileSystemEventHandler):
    """Watchdog handler with debounce."""

    def __init__(
        self,
        signal_file: str,
        logger: BridgeLogger,
        callback: Callable[[TradeSignal], None],
        debounce_s: float = 0.5,
    ):
        self.signal_file = signal_file
        self.log = logger
        self.callback = callback
        self.debounce_s = debounce_s
        self._last_modified: float = 0.0
        self._last_size: int = 0

    def on_modified(self, event) -> None:
        if event.is_directory:
            return
        if not event.src_path.endswith(os.path.basename(self.signal_file)):
            return
        now = time.perf_counter()
        if now - self._last_modified < self.debounce_s:
            return
        # Extra guard: only trigger if file size changed (avoids no-op metadata updates)
        try:
            size = os.path.getsize(event.src_path)
        except OSError:
            return
        if size == self._last_size:
            return
        self._last_modified = now
        self._last_size = size
        self._process_file(event.src_path)

    def _process_file(self, path: str) -> None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            self.log.warning("signal_file_parse_error", path=path, error=str(exc))
            return

        # Support both single object and list-of-objects
        payloads = data if isinstance(data, list) else [data]
        for payload in payloads:
            signal = TradeSignal.from_json(payload)
            err = signal.validate()
            if err:
                self.log.warning("signal_validation_failed", error=err, payload=payload)
                continue
            self.log.info("signal_detected", symbol=signal.symbol, direction=signal.direction, size=signal.size)
            try:
                self.callback(signal)
            except Exception as exc:
                self.log.error("signal_callback_failed", error=str(exc), payload=payload)


class SignalWatcher:
    """Blocking watchdog observer. Call start() to run in background thread."""

    def __init__(self, config: BridgeConfig, logger: BridgeLogger, callback: Callable[[TradeSignal], None]):
        self.cfg = config
        self.log = logger
        self.callback = callback
        if Observer is None:
            raise RuntimeError("watchdog not installed: pip install watchdog")
        self._observer = Observer()
        self._handler = SignalEventHandler(
            signal_file=config.signal_file,
            logger=logger,
            callback=callback,
        )

    def start(self) -> None:
        watch_dir = os.path.dirname(self.cfg.signal_file)
        os.makedirs(watch_dir, exist_ok=True)
        self._observer.schedule(self._handler, path=watch_dir, recursive=False)
        self._observer.start()
        self.log.info("signal_watcher_started", watch_dir=watch_dir, signal_file=self.cfg.signal_file)

    def stop(self) -> None:
        self._observer.stop()
        self._observer.join()
        self.log.info("signal_watcher_stopped")

    def join(self) -> None:
        self._observer.join()
