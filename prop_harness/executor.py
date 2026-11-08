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

from .bracket import DEFAULT_LADDER, build_bracket, fmt, round_qty, spec_for
from .brains.budget import BudgetBrain
from .brains.drawdown import DrawdownBrain
from .config import PropConfig
from .families import family_of
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
        if self.config.family_cap_enabled:
            fam = family_of(asset)
            for held in self.open_brackets():
                if (held["side"] == side and held["asset"] != asset
                        and family_of(held["asset"]) == fam):
                    return self._reject(
                        sid, f"family cap: {fam} {side} held by {held['asset']}")

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
        # Rotation-policy cost gate (§4): round-trip taker cost must stay
        # under cost_risk_max_frac of planned risk. notional = risk /
        # stop_dist_pct, so this binds exactly when the stop is tight
        # relative to fees — the dust-scalp class dies here.
        notional = quantity * entry
        rt_cost = notional * Decimal(str(self.config.taker_fee_rate)) * 2
        if rt_cost > Decimal(str(risk_usd)) * Decimal(
                str(self.config.cost_risk_max_frac)):
            return self._reject(
                sid, f"round-trip cost ${rt_cost:.2f} > "
                     f"{self.config.cost_risk_max_frac:.0%} of ${risk_usd:.2f} risk")

        body = build_bracket(
            account_id=self.client.account_id, asset=asset, side=side,
            quantity=str(quantity), entry_ref=str(entry), stop_price=str(stop),
            ts_ms=ts_ms, quote=self.config.quote, entry_type=entry_type, ladder=ladder,
            fee_rate=str(self.config.taker_fee_rate),
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

    def sync_bracket_size(self, bracket: dict, position: dict) -> bool:
        """Reconcile registry truth against the venue position after fills.

        TP rung fills shrink the venue position but never the registry — the
        resting stop stays sized for the ORIGINAL quantity, and venue
        behavior on an oversized reduceOnly stop is untested (clamp vs
        reject vs flip: a reject leaves the runner naked). On shrink:
        release the harvested slice of committed risk, book the realized
        delta, re-place the stop at venue size, THEN update the registry
        (retry next tick if the venue work fails). Returns True when the
        registry row changed."""
        if not isinstance(position, dict):
            return False
        sid = bracket.get("semantic_id", "")
        try:
            venue_qty = Decimal(str(position.get("quantity")))
            reg_qty = Decimal(str(bracket["quantity"]))
            venue_realized = float(position.get("realizedPnl", 0.0))
        except Exception:
            return False
        if venue_qty <= 0 or venue_qty >= reg_qty * Decimal("0.98"):
            return False
        frac_remaining = float(venue_qty / reg_qty)
        already = float(bracket.get("risk_released", 0.0))
        release = bracket["risk_usd"] * (1.0 - frac_remaining) - already
        realized_delta = venue_realized - float(bracket.get("realized_booked", 0.0))
        if release > 0 and self.budget.state is not None:
            self.budget.release(release, max(0.0, realized_delta))
            bracket["risk_released"] = already + release
            bracket["realized_booked"] = venue_realized
        ok = self.resize_protection(bracket, venue_qty,
                                    str(position.get("positionId") or ""))
        append_event(self.journal, {
            "type": "bracket_size_synced", "semantic_id": sid,
            "asset": bracket.get("asset"), "registry_qty": str(reg_qty),
            "venue_qty": str(venue_qty), "protection_resized": ok,
            "risk_released": round(release, 4),
            "realized_delta": round(realized_delta, 4),
        })
        if not ok:
            return False  # registry stays stale -> retried next reconcile
        bracket["quantity"] = str(venue_qty)
        return True

    def resize_protection(self, bracket: dict, venue_qty: Decimal,
                          position_id: str) -> bool:
        """Re-place the stop at post-fill size. Place FIRST, cancel the
        oversized stop second — the position is never without a stop."""
        asset = bracket["asset"]
        side = bracket["side"]
        sid = bracket.get("semantic_id", "")

        def _fail(stage: str, exc: Exception) -> bool:
            append_event(self.journal, {
                "type": "protection_resize_failed", "semantic_id": sid,
                "asset": asset, "stage": stage, "error": str(exc)})
            return False

        try:
            _, decimals = spec_for(asset)
            qty = round_qty(Decimal(venue_qty), decimals)
            trigger = fmt(Decimal(str(bracket["stop_price"])))
        except Exception as exc:
            return _fail("geometry", exc)
        ts_ms = int(time.time() * 1000)
        order = {
            "accountId": self.client.account_id,
            "intentId": intent_ulid(self.client.account_id,
                                    f"{asset}-sl-resize", ts_ms),
            "exchange": "hyperliquid",
            "positionSide": "long" if side == "buy" else "short",
            "productType": "perp",
            "asset": asset, "base": asset, "quote": self.config.quote,
            "type": "stop_market",
            "side": "sell" if side == "buy" else "buy",
            "timeInForce": "GTC",
            "quantity": fmt(qty),
            "triggerPrice": trigger,
            "reduceOnly": True,
            "closePosition": False,
        }
        if position_id:
            order["positionId"] = position_id
        try:
            placed = self.client.place_orders({"orders": [order]})
        except Exception as exc:
            return _fail("place", exc)
        new_ids = {o.get("orderId") for o in placed}
        cancelled: list[str] = []
        try:
            open_orders = self.client.get_open_orders(base=asset)
        except Exception as exc:
            # replacement is live; an oversized twin may linger but the
            # position is protected — report, don't unwind
            append_event(self.journal, {
                "type": "protection_resized", "semantic_id": sid,
                "asset": asset, "quantity": fmt(qty), "trigger": trigger,
                "cancelled": cancelled, "cancel_scan_error": str(exc)})
            return True
        for o in open_orders:
            if o.get("type") != "stop_market":
                continue
            oid = o.get("orderId")
            if not oid or oid in new_ids:
                continue
            try:
                self.client.cancel_order(oid)
                cancelled.append(oid)
            except Exception as exc:
                append_event(self.journal, {
                    "type": "protection_resize_failed", "semantic_id": sid,
                    "asset": asset, "stage": "cancel", "error": str(exc)})
        append_event(self.journal, {
            "type": "protection_resized", "semantic_id": sid,
            "asset": asset, "quantity": fmt(qty), "trigger": trigger,
            "cancelled": cancelled})
        return True

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
