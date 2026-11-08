"""ProprExecutor — the Kant gate before every order.

Preflight (all must pass, rejection journaled with reason):
  config.enabled, no DISABLED file, drawdown band permits (mult > 0),
  risk-at-stop <= risk ceiling (eval: remaining/trend-max; standard:
  budget.risk_cap), symbol spec known, position slot free.

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
from .brains import eval_profile as ep
from .brains import loss_stats as ls
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

    def _loss_veto(self, family: str, side: str) -> dict | None:
        """Consume the probe's loss_veto artifact. Fail-OPEN on stale/
        missing/corrupt — a dead probe never freezes the fleet."""
        if not self.config.loss_veto_enabled:
            return None
        artifact = read_json(self.config.loss_veto_artifact)
        if not ls.artifact_fresh(artifact):
            return None
        return ls.veto_for(artifact.get("stats") or {}, family, side)

    def place_bracket(self, asset: str, side: str, risk_usd: float,
                      entry_ref: float, stop_price: float,
                      ladder=DEFAULT_LADDER, entry_type: str = "market",
                      coherence: float | None = None) -> BracketResult:
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
        # Evaluation: risk_for_signal already sized the big bet. Legacy
        # risk_cap clamps at r_max ($7.5) and would kant_reject every
        # coh>7 approval — live undershoot vs e9cc7f3 doctrine.
        cap = ep.risk_ceiling(
            self.config.profile, self.budget.remaining,
            self.drawdown.state.headroom, self.config.hard_zero_headroom_usd,
            self.config.eval_trend_budget_usd)
        if cap is None:
            cap = self.budget.risk_cap(mult)
        if risk_usd > cap:
            return self._reject(sid, f"risk ${risk_usd:.2f} > cap ${cap:.2f}")
        if len(self.open_brackets()) >= self.config.max_positions:
            return self._reject(sid, "max_positions reached")
        fam = family_of(asset)
        if self.config.family_cap_enabled:
            for held in self.open_brackets():
                if (held["side"] == side and held["asset"] != asset
                        and family_of(held["asset"]) == fam):
                    return self._reject(
                        sid, f"family cap: {fam} {side} held by {held['asset']}")
        veto = self._loss_veto(fam, side)
        if veto:
            return self._reject(
                sid, f"loss veto {fam}|{side}: n={veto['n']} "
                     f"shrunk_exp=${veto['shrunk_expectancy']:.2f}")

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
        # Eval-profile margin cap: leverage is margin-efficiency only (risk
        # is stop-bounded), so big notional is reachable exactly on the
        # 10x/8x/25x venue classes; 2x alts size down to the cap.
        equity = self.drawdown.state.equity if self.drawdown.state else 0.0
        ok_margin = ep.margin_ok(self.config.profile, float(notional),
                                 asset, equity, self.config.eval_margin_cap_frac)
        if ok_margin is False:
            return self._reject(
                sid, f"margin ${float(notional) / ep.leverage_for(asset):.2f} "
                     f"> {self.config.eval_margin_cap_frac:.0%} of equity "
                     f"${equity:.2f} (lev {ep.leverage_for(asset)}x)")

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

        coh_entry = None
        try:
            if coherence is not None:
                coh_entry = float(coherence)
                if coh_entry != coh_entry:  # NaN
                    coh_entry = None
        except (TypeError, ValueError):
            coh_entry = None
        reg = self._registry()
        reg["brackets"].append({
            "semantic_id": sid,
            "group_id": body["orderGroupId"],
            "asset": asset,
            "side": side,
            "family": fam,
            "quantity": str(quantity),
            "risk_usd": risk_usd,
            "entry_ref": str(entry),
            "stop_price": str(stop),
            "current_stop": str(stop),
            "be_armed": False,
            "reloads": 0,
            "coherence_at_entry": coh_entry,
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
        if venue_qty <= 0:
            return False
        if venue_qty > reg_qty * Decimal("1.02"):
            # Reload fills GROW the venue position: re-place the stop at the
            # grown size (current_stop — tighten-only already enforced at
            # the reload site). Registry quantity stamped up so the next
            # reconcile sees parity. No budget touch — the reload already
            # committed its own risk line.
            ok = self.resize_protection(bracket, venue_qty,
                                        str(position.get("positionId") or ""))
            append_event(self.journal, {
                "type": "bracket_size_grown", "semantic_id": sid,
                "asset": bracket.get("asset"), "registry_qty": str(reg_qty),
                "venue_qty": str(venue_qty), "protection_resized": ok,
            })
            if not ok:
                return False  # retried next reconcile
            bracket["quantity"] = str(venue_qty)
            return True
        if venue_qty >= reg_qty * Decimal("0.98"):
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

    def arm_breakeven(self, bracket: dict, position: dict) -> bool:
        """+0.5R unrealized -> stop re-anchored to entry (the XRP +$2 ->
        -$5 give-back wound repair). The resize pass re-places the stop at
        venue size AND the new trigger atomically; stamps land only after
        the venue work succeeds (retry next tick on failure)."""
        if self.config.profile != "evaluation":
            return False
        if not isinstance(position, dict):
            return False
        sid = bracket.get("semantic_id", "")
        if not ep.should_arm_breakeven(
                self.config.profile, bracket,
                position.get("unrealizedPnl"), self.config.eval_be_arm_r):
            return False
        entry = bracket["entry_ref"]
        venue_qty = Decimal(str(position.get("quantity") or "0"))
        if venue_qty <= 0:
            return False
        ok = self.resize_protection(
            bracket, venue_qty, str(position.get("positionId") or ""),
            trigger_override=entry)
        append_event(self.journal, {
            "type": "breakeven_armed" if ok else "breakeven_arm_failed",
            "semantic_id": sid, "asset": bracket.get("asset"),
            "entry": str(entry)})
        if not ok:
            return False
        bracket["be_armed"] = True
        bracket["current_stop"] = str(entry)
        return True

    def reload_bracket(self, bracket: dict, coherence,
                       fresh_entry: float, fresh_stop=None) -> BracketResult:
        """Fresh same-direction signal on a HELD, PROVEN bracket: buy more
        out of HARVESTED risk only (risk_released), stop tighten-only
        against BOTH the held stop and the fresh signal's stop.
        Total symbol commitment never exceeds the original budget line —
        averaging into unproven risk is martingale and never reaches this
        function (reload_verdict refuses)."""
        sid = semantic_id("acct1", bracket["asset"], int(time.time() * 1000))
        if self.config.profile != "evaluation":
            return BracketResult(ok=False, semantic_id=sid,
                                 reason="reload is eval-profile only")
        verdict = ep.reload_verdict(
            self.config.profile, bracket, coherence, fresh_entry,
            self.config.eval_coh_big, self.config.eval_coh_max,
            self.config.eval_max_reloads)
        if not verdict:
            return BracketResult(ok=False, semantic_id=sid,
                                 reason="reload verdict refused")
        risk = ep.reload_size(bracket, float(verdict["frac"]))
        if risk <= 0:
            return BracketResult(ok=False, semantic_id=sid,
                                 reason="no harvested risk to reload")
        asset = bracket["asset"]
        side = bracket["side"]
        entry = Decimal(str(fresh_entry))
        stop = ep.tightened_stop(side, bracket.get("current_stop"),
                                 fresh_stop if fresh_stop is not None
                                 else bracket.get("stop_price"))
        if stop is None:
            stop = float(Decimal(str(bracket.get("current_stop")
                                             or bracket["stop_price"])))
        stop_d = Decimal(str(stop))
        stop_dist = abs(entry - stop_d)
        if stop_dist <= 0:
            return BracketResult(ok=False, semantic_id=sid,
                                 reason="reload zero stop distance")
        try:
            min_qty, _ = spec_for(asset)
        except KeyError as exc:
            return BracketResult(ok=False, semantic_id=sid, reason=str(exc))
        quantity = Decimal(str(risk)) / stop_dist
        if quantity < min_qty:
            return BracketResult(ok=False, semantic_id=sid,
                                 reason="reload below venue min qty")
        ts_ms = int(time.time() * 1000)
        order = {
            "accountId": self.client.account_id,
            "intentId": intent_ulid(self.client.account_id,
                                    f"{asset}-reload", ts_ms),
            "exchange": "hyperliquid",
            "positionSide": "long" if side == "buy" else "short",
            "productType": "perp",
            "asset": asset, "base": asset, "quote": self.config.quote,
            "type": "market",
            "side": side,
            "timeInForce": "IOC",
            "quantity": fmt(quantity),
            "reduceOnly": False,
            "closePosition": False,
        }
        self.budget.commit(risk)
        try:
            placed = self.client.place_orders({"orders": [order]})
        except Exception as exc:
            self.budget.release(risk, 0.0)
            append_event(self.journal, {
                "type": "reload_failed", "semantic_id": sid,
                "asset": asset, "error": str(exc)})
            return BracketResult(ok=False, semantic_id=sid,
                                 reason=f"reload place failed: {exc}")
        bracket["reloads"] = int(bracket.get("reloads", 0)) + 1
        bracket["risk_usd"] = float(bracket["risk_usd"]) + risk
        bracket["risk_released"] = max(
            0.0, float(bracket.get("risk_released", 0.0)) - risk)
        bracket["current_stop"] = str(stop_d)
        # The next reconcile's growth branch re-places protection at the
        # grown venue size — place-after-fill keeps the runner never naked.
        append_event(self.journal, {
            "type": "reload_placed", "semantic_id": sid,
            "asset": asset, "side": side, "quantity": str(quantity),
            "risk_usd": risk, "stop": str(stop_d),
            "better_price": bool(verdict["better"]),
            "orders": len(placed)})
        return BracketResult(ok=True, semantic_id=sid, orders=placed)

    def trail_bracket(self, bracket: dict, position: dict,
                      mark: float | None) -> bool:
        """HL-mark trailing (spec §3.8 build 2): rungs breakeven+fees @1R,
        lock 1R @2R, tighten-only. mark=None (stale/absent HL) -> no-op, the
        uPnL breakeven arm stays the only trigger — fail-open bit-for-bit.
        The mark drives the trigger ONLY; risk math never sees it."""
        if self.config.profile != "evaluation" or mark is None:
            return False
        if not isinstance(position, dict):
            return False
        sid = bracket.get("semantic_id", "")
        target = ep.trail_verdict(self.config.profile, bracket, mark,
                                  self.config.taker_fee_rate)
        if target is None:
            return False
        venue_qty = Decimal(str(position.get("quantity") or "0"))
        if venue_qty <= 0:
            return False
        ok = self.resize_protection(
            bracket, venue_qty, str(position.get("positionId") or ""),
            trigger_override=str(target))
        append_event(self.journal, {
            "type": "trail_tightened" if ok else "trail_failed",
            "semantic_id": sid, "asset": bracket.get("asset"),
            "mark": mark, "target": str(target),
            "previous_stop": bracket.get("current_stop")})
        if not ok:
            return False
        bracket["current_stop"] = str(target)
        # Proven stamp only when the trail locks at/through entry. Rung-1
        # fee-concession is looser than the uPnL exact-entry arm — stamping
        # be_armed there suppresses the tighter BE pass and lets HL unlock
        # the reload proven-gate before venue uPnL confirms (authority law:
        # marks trigger stops, not capital-state).
        if (not bracket.get("be_armed")
                and ep.stop_locks_entry(bracket["side"], target,
                                        bracket.get("entry_ref"))):
            bracket["be_armed"] = True
        return True

    def resize_protection(self, bracket: dict, venue_qty: Decimal,
                          position_id: str, trigger_override: str | None = None) -> bool:
        """Re-place the stop at post-fill size. Place FIRST, cancel the
        oversized stop second — the position is never without a stop.
        trigger_override lets the breakeven/reload paths re-anchor the
        stop in the same atomic pass (tighten-only is the caller's job)."""
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
            stop_src = trigger_override or bracket.get("current_stop") or bracket["stop_price"]
            trigger = fmt(Decimal(str(stop_src)))
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
