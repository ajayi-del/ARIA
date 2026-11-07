"""ProprExecutor — the Kant gate before every order.

Preflight (all must pass, rejection journaled with reason):
  config.enabled, no DISABLED file, drawdown band permits (mult > 0),
  risk-at-stop <= budget risk_cap, symbol spec known, position slot free.

Bracket shape: one batch, entry + full stop + TP ladder — the venue
structurally refuses conditionals without an entry or positionId, so a
naked position is unrepresentable (the 2026-11-03 Arcus P0 class, dead
at birth here).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from .bracket import DEFAULT_LADDER, build_bracket, spec_for
from .brains.budget import BudgetBrain
from .brains.drawdown import DrawdownBrain
from .config import PropConfig
from .ids import intent_ulid, semantic_id
from .io.journal import append_event, atomic_write_json, read_json
from .io.propr_client import ProprClient, ProprUnknownState


@dataclass
class BracketResult:
    ok: bool
    semantic_id: str
    reason: str = ""
    group_id: str = ""
    orders: list[dict] | None = None


class ProprExecutor:
    def __init__(self, config: PropConfig, client: ProprClient,
                 budget: BudgetBrain, drawdown: DrawdownBrain):
        self.config = config
        self.client = client
        self.budget = budget
        self.drawdown = drawdown
        self.registry_path = config.state_root / "accounts" / "acct1" / "registry.json"
        self.journal = config.journal_path

    def _registry(self) -> dict[str, Any]:
        return read_json(self.registry_path, {"brackets": []})

    def _save_registry(self, reg: dict[str, Any]) -> None:
        atomic_write_json(self.registry_path, reg)

    def open_brackets(self) -> list[dict]:
        return [b for b in self._registry()["brackets"] if b.get("status") == "open"]

    def _reject(self, sid: str, reason: str) -> BracketResult:
        append_event(self.journal, {
            "type": "kant_reject", "semantic_id": sid, "reason": reason,
        })
        return BracketResult(ok=False, semantic_id=sid, reason=reason)

    def place_bracket(self, asset: str, side: str, risk_usd: float,
                      entry_ref: float, stop_price: float,
                      ladder=DEFAULT_LADDER, entry_type: str = "market") -> BracketResult:
        ts_ms = int(time.time() * 1000)
        sid = semantic_id("acct1", asset, ts_ms)

        if not self.config.enabled:
            return self._reject(sid, "PROP_ENABLED=false")
        if self.config.disabled_file.exists():
            return self._reject(sid, "DISABLED touch-file present")
        if self.drawdown.state is None:
            return self._reject(sid, "no equity register — ABSTAIN")
        mult = self.drawdown.mult
        if mult <= 0:
            return self._reject(sid, f"drawdown band {self.drawdown.state.band} forbids new risk")
        cap = self.budget.risk_cap(mult)
        if risk_usd > cap:
            return self._reject(sid, f"risk ${risk_usd:.2f} > cap ${cap:.2f}")
        if len(self.open_brackets()) >= self.config.max_positions:
            return self._reject(sid, "max_positions reached")

        entry = Decimal(str(entry_ref))
        stop = Decimal(str(stop_price))
        stop_dist = abs(entry - stop)
        if stop_dist <= 0:
            return self._reject(sid, "zero stop distance")
        quantity = Decimal(str(risk_usd)) / stop_dist
        try:
            min_qty, decimals = spec_for(asset)
        except KeyError as exc:
            return self._reject(sid, str(exc))
        if quantity < min_qty:
            return self._reject(sid, f"qty {quantity} below venue min {min_qty} "
                                     f"(risk ${risk_usd:.2f} too small for stop distance)")

        body = build_bracket(
            account_id=self.client.account_id, asset=asset, side=side,
            quantity=str(quantity), entry_ref=str(entry), stop_price=str(stop),
            ts_ms=ts_ms, quote=self.config.quote, entry_type=entry_type, ladder=ladder,
        )

        self.budget.commit(risk_usd)
        try:
            placed = self.client.place_orders(body)
        except ProprUnknownState as exc:
            # 5xx/timeout: the batch may exist venue-side. Reconcile by intentId.
            entry_intent = intent_ulid(self.client.account_id, f"{asset}-entry", ts_ms)
            found = None
            try:
                found = self.client.find_by_intent(entry_intent)
            except Exception:
                pass
            append_event(self.journal, {
                "type": "bracket_unknown", "semantic_id": sid,
                "error": str(exc), "reconciled": bool(found),
            })
            if not found:
                self.budget.release(risk_usd, 0.0)
                return BracketResult(ok=False, semantic_id=sid,
                                     reason=f"UNKNOWN then absent: {exc}")
            placed = [found]

        reg = self._registry()
        reg["brackets"].append({
            "semantic_id": sid,
            "group_id": body["orderGroupId"],
            "asset": asset,
            "side": side,
            "quantity": str(quantity),
            "risk_usd": risk_usd,
            "entry_ref": str(entry),
            "stop_price": str(stop),
            "order_ids": [o.get("orderId") for o in placed],
            "intents": [o["intentId"] for o in body["orders"]],
            "opened_ts": time.time(),
            "absence_count": 0,
            "status": "open",
        })
        self._save_registry(reg)
        append_event(self.journal, {
            "type": "bracket_placed", "semantic_id": sid,
            "asset": asset, "side": side, "quantity": str(quantity),
            "risk_usd": risk_usd, "stop": str(stop),
            "orders": len(placed),
        })
        return BracketResult(ok=True, semantic_id=sid,
                             group_id=body["orderGroupId"], orders=placed)

    def flatten_all(self, reason: str) -> None:
        for bracket in self.open_brackets():
            asset = bracket["asset"]
            try:
                self.client.cancel_all_open(base=asset)
                self.client.close_position(asset)
            except Exception as exc:
                append_event(self.journal, {
                    "type": "flatten_error", "semantic_id": bracket["semantic_id"],
                    "error": str(exc),
                })
        append_event(self.journal, {"type": "flatten_all", "reason": reason})
