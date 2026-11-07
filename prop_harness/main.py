"""Prop harness main loop — single-thread scheduler.

Loops (architect spec): equity poll 30s / reconciler 15s (absence x3) /
budget roll 60s (UTC day = venue reset window) / heartbeat 60s /
signal tap 2s (M4 — reads ARIA's prop_outbox.jsonl, pull-only).

Algedonic halt: LOCKED/BREACHED latch algedonic.json (Governor-clears-only)
and stop new risk. Flatten on halt requires PROP_DISABLE_FLATTENS=true —
default is observe-and-latch, never auto-flatten.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from .brains.budget import BudgetBrain
from .brains.drawdown import DrawdownBrain
from .config import PropConfig
from .executor import ProprExecutor
from .io.journal import append_event, atomic_write_json, read_json
from .io.propr_client import ProprClient
from .io.signal_tap import (DEFAULT_REMOTE_OUTBOX, SignalTap,
                            fetch_local, fetch_ssh)


class Harness:
    def __init__(self, config: PropConfig):
        self.config = config
        root = config.state_root
        root.mkdir(parents=True, exist_ok=True)
        (root / "journal").mkdir(exist_ok=True)
        (root / "accounts" / "acct1").mkdir(parents=True, exist_ok=True)

        self.client = ProprClient(api_key=config.api_key, base_url=config.api_url)
        self.client.setup(config.account_id or None)
        self.drawdown = DrawdownBrain(
            root / "accounts" / "acct1" / "dd.json",
            floor_line=config.floor_line,
            floor_usd=config.internal_floor_usd,
        )
        self.budget = BudgetBrain(
            root / "accounts" / "acct1" / "budget_day.json",
            hard_zero=config.hard_zero_headroom_usd,
            divisor=config.budget_divisor,
            floor_budget=config.budget_floor_usd,
            r_base=config.r_base_usd,
            r_max=config.r_max_usd,
        )
        self.executor = ProprExecutor(config, self.client, self.budget, self.drawdown)
        self.journal = config.journal_path
        self.algedonic_path = root / "program" / "algedonic.json"
        self.tap = (SignalTap(root / "cursor.json", ttl_s=config.signal_ttl_s,
                              asset_class=config.asset_class)
                    if config.outbox_enabled else None)
        self._next: dict[str, float] = {}

    def _due(self, name: str, every: float) -> bool:
        now = time.time()
        if now >= self._next.get(name, 0.0):
            self._next[name] = now + every
            return True
        return False

    def equity_poll(self) -> None:
        equity = float(self.client.get_equity())
        state, transition = self.drawdown.update(equity)
        append_event(self.journal, {
            "type": "equity", "equity": equity, "headroom": state.headroom,
            "band": state.band, "transition": transition,
        })
        if state.band in ("LOCKED", "BREACHED") or state.breached:
            if not read_json(self.algedonic_path):
                atomic_write_json(self.algedonic_path, {
                    "latched_at": time.time(), "band": state.band,
                    "equity": equity, "clears": "governor-only",
                })
                append_event(self.journal, {
                    "type": "algedonic_latch", "band": state.band, "equity": equity,
                })
                if self.config.disable_flattens:
                    self.executor.flatten_all(f"algedonic {state.band}")

    def reconcile(self) -> None:
        positions = {p["asset"] for p in self.client.get_positions(status="open")}
        reg_path = self.executor.registry_path
        reg = read_json(reg_path, {"brackets": []})
        changed = False
        for bracket in reg["brackets"]:
            if bracket.get("status") != "open":
                continue
            if bracket["asset"] in positions:
                if bracket.get("absence_count"):
                    bracket["absence_count"] = 0
                    changed = True
                continue
            bracket["absence_count"] = bracket.get("absence_count", 0) + 1
            changed = True
            append_event(self.journal, {
                "type": "reconcile_absence",
                "semantic_id": bracket["semantic_id"],
                "count": bracket["absence_count"],
            })
            if bracket["absence_count"] >= 3:
                bracket["status"] = "closed_external"
                self.budget.release(bracket["risk_usd"], 0.0)
                append_event(self.journal, {
                    "type": "reconcile_closed_external",
                    "semantic_id": bracket["semantic_id"],
                })
        if changed:
            atomic_write_json(reg_path, reg)

    def signal_tap(self) -> None:
        assert self.tap is not None
        if self.config.outbox_transport == "ssh":
            size, chunk = fetch_ssh(
                self.config.outbox_host, self.config.outbox_zone,
                self.config.outbox_path or DEFAULT_REMOTE_OUTBOX,
                self.tap.offset)
        else:
            fetched = fetch_local(Path(self.config.outbox_path), self.tap.offset)
            if fetched is None:
                return
            size, chunk = fetched
        signals, events = self.tap.consume(size, chunk)
        for ev in events:
            append_event(self.journal, ev)
        for sig in signals:
            self._execute_signal(sig)

    def _execute_signal(self, sig: dict) -> None:
        if any(b["asset"] == sig["asset"] for b in self.executor.open_brackets()):
            append_event(self.journal, {
                "type": "tap_skip_open", "asset": sig["asset"], "id": sig["id"]})
            return
        if self.drawdown.state is None:
            return
        risk = self.budget.risk_cap(self.drawdown.mult)
        res = self.executor.place_bracket(
            asset=sig["asset"], side=sig["side"], risk_usd=risk,
            entry_ref=sig["entry_ref"], stop_price=sig["stop_price"])
        append_event(self.journal, {
            "type": "tap_signal", "id": sig["id"], "asset": sig["asset"],
            "side": sig["side"], "risk_usd": risk, "coherence": sig["coherence"],
            "ok": res.ok, "reason": res.reason})

    def budget_roll(self) -> None:
        if self.drawdown.state is None:
            return
        if self.budget.roll(self.drawdown.headroom):
            append_event(self.journal, {
                "type": "budget_roll", "day": self.budget.state.day,
                "budget": self.budget.state.budget,
                "headroom": self.budget.state.headroom,
            })

    def heartbeat(self) -> None:
        atomic_write_json(self.config.state_root / "heartbeat.json", {
            "ts": time.time(),
            "account_id": self.client.account_id,
            "enabled": self.config.enabled,
            "dd": self.drawdown.snapshot,
            "budget": self.budget.state.__dict__ if self.budget.state else None,
            "open_brackets": len(self.executor.open_brackets()),
            "tap_offset": self.tap.offset if self.tap else None,
        })

    def run(self) -> None:
        append_event(self.journal, {
            "type": "boot", "account_id": self.client.account_id,
            "api_url": self.config.api_url, "enabled": self.config.enabled,
        })
        while True:
            if self.config.disabled_file.exists():
                time.sleep(5)
                continue
            try:
                if self._due("equity", self.config.equity_poll_s):
                    self.equity_poll()
                if self._due("reconcile", self.config.reconcile_s):
                    self.reconcile()
                if self._due("budget", self.config.budget_roll_s):
                    self.budget_roll()
                if self.tap and self._due("signal_tap", self.config.signal_tap_s):
                    self.signal_tap()
                if self._due("heartbeat", self.config.heartbeat_s):
                    self.heartbeat()
            except Exception as exc:
                append_event(self.journal, {"type": "loop_error", "error": str(exc)})
            time.sleep(1)


def main() -> int:
    config = PropConfig.from_env()
    if not config.api_key:
        print("PROP_API_KEY missing — drop it in "
              f"{config.state_root}/prop.env", file=sys.stderr)
        return 2
    harness = Harness(config)
    print(f"prop harness up — account {harness.client.account_id} "
          f"via {config.api_url} (enabled={config.enabled})")
    harness.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
