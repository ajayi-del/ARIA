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
from .brains import basket_tp as btp
from .brains import cost_move_gate as cmg
from .brains import crypto_comove as ccm
from .brains import eval_profile as ep
from .brains import loss_stats as ls
from .brains import post_win_respend as pwr
from .brains.budget import BudgetBrain
from .brains.drawdown import DrawdownBrain
from .config import PropConfig
from .families import family_of
from .ids import intent_ulid, semantic_id
from .io.journal import append_event, atomic_write_json, read_json
from .io.propr_client import ProprAPIError, ProprClient, ProprUnknownState


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
        self.respend_path = config.state_root / "post_win_respend.json"
        self.comove_flip_path = config.state_root / "comove_flip.json"
        self.basket_tp_path = (
            config.state_root / "accounts" / "acct1" / "basket_tp.json")

    def _registry(self) -> dict[str, Any]:
        return read_json(self.registry_path, {"brackets": []})

    def _save_registry(self, reg: dict[str, Any]) -> None:
        atomic_write_json(self.registry_path, reg)

    def open_brackets(self) -> list[dict]:
        return [b for b in self._registry()["brackets"] if b.get("status") == "open"]

    def _respend_latch(self) -> dict[str, Any]:
        return read_json(self.respend_path, {})

    def _save_respend_latch(self, latch: dict[str, Any]) -> None:
        atomic_write_json(self.respend_path, latch)

    def _comove_flip_state(self) -> dict[str, Any]:
        return read_json(self.comove_flip_path, {})

    def _save_comove_flip_state(self, state: dict[str, Any]) -> None:
        atomic_write_json(self.comove_flip_path, state)

    def _flip_used_today(self) -> bool:
        st = self._comove_flip_state()
        day = time.strftime("%Y-%m-%d", time.gmtime())
        return bool(st.get("used") and st.get("day") == day)

    def _mark_flip_used(self) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        self._save_comove_flip_state({
            "used": True, "day": day, "used_at": time.time(),
        })

    def arm_post_win_respend(self, bracket: dict, *, now: float | None = None) -> bool:
        """Arm re-spend latch from a qualifying crypto win close. True if armed."""
        if not bool(getattr(self.config, "post_win_respend_enabled", True)):
            return False
        asset = str(bracket.get("asset") or "")
        if not ccm.is_crypto_asset(asset):
            return False
        sleeve = str(getattr(self.config, "asset_class", "all") or "all")
        if sleeve.lower() == "tradfi":
            return False
        fam = bracket.get("family") or family_of(asset)
        pnl = bracket.get("realized_pnl_final")
        risk = bracket.get("risk_usd")
        coh = bracket.get("coherence_at_entry", bracket.get("coherence"))
        if not pwr.win_qualifies(
                pnl, risk, coh,
                min_win_usd=float(getattr(
                    self.config, "post_win_respend_min_win_usd", 25.0) or 25.0),
                min_win_r=float(getattr(
                    self.config, "post_win_respend_min_win_r", 0.70) or 0.70),
                min_risk_for_r=float(getattr(
                    self.config, "post_win_respend_min_risk_for_r", 20.0)
                    or 20.0),
                coh_big=float(getattr(self.config, "eval_coh_big", 7.0)
                              or 7.0)):
            return False
        ts = float(now if now is not None else time.time())
        latch = pwr.build_latch(
            asset=asset, family=str(fam), side=bracket.get("side"),
            realized_pnl=float(pnl), risk_usd=risk, coherence=coh,
            now=ts,
            ttl_s=float(getattr(self.config, "post_win_respend_ttl_s", 5400.0)
                        or 5400.0),
            max_respend_usd=float(getattr(
                self.config, "post_win_respend_max_usd", 15.0) or 15.0),
            semantic_id=bracket.get("semantic_id"),
        )
        self._save_respend_latch(latch)
        append_event(self.journal, {
            "type": "post_win_respend_armed",
            "winner_asset": latch.get("winner_asset"),
            "winner_family": latch.get("winner_family"),
            "win_usd": latch.get("win_usd"),
            "max_respend_usd": latch.get("max_respend_usd"),
            "expires_at": latch.get("expires_at"),
            "ttl_s": latch.get("ttl_s"),
            "semantic_id": latch.get("semantic_id"),
        })
        return True

    def _reject(self, sid: str, reason: str, **extra) -> BracketResult:
        payload = {
            "type": "kant_reject", "semantic_id": sid, "reason": reason,
        }
        payload.update({k: v for k, v in extra.items() if v is not None})
        append_event(self.journal, payload)
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
        open_n = len(self.open_brackets())
        if open_n >= self.config.max_positions:
            return self._reject(sid, "max_positions reached")
        # Eval slot reserve: probes must not squat the last owner slot(s)
        # reserved for coh>7. Kill switch off = max_positions-only legacy.
        reserve_reason = ep.slot_reserve_verdict(
            self.config.profile,
            bool(getattr(self.config, "slot_reserve_enabled", False)),
            coherence, open_n, self.config.max_positions,
            int(getattr(self.config, "slot_reserve_slots", 1) or 1),
            float(getattr(self.config, "eval_coh_big", 7.0) or 7.0))
        if reserve_reason:
            return self._reject(sid, reserve_reason)
        fam = family_of(asset)
        if self.config.family_cap_enabled:
            for held in self.open_brackets():
                if (held["side"] == side and held["asset"] != asset
                        and family_of(held["asset"]) == fam):
                    return self._reject(
                        sid, f"family cap: {fam} {side} held by {held['asset']}")
        # Crypto co-move: refuse opposite-signed adds vs dominant book β
        # (net signed risk / majority). Alts ride the same factor as
        # majors — BTC/ETH/SOL-only would miss AVAX/SOL/… hedges.
        open_now = self.open_brackets()
        comove = ccm.crypto_comove_verdict(
            bool(getattr(self.config, "crypto_comove_enabled", True)),
            asset, side, open_now,
            min_legs=int(getattr(self.config, "crypto_comove_min_legs", 1)
                         or 1),
            min_dom_risk_usd=float(getattr(
                self.config, "crypto_comove_min_dom_risk_usd", 0.0) or 0.0),
            asset_class=str(getattr(self.config, "asset_class", "all")
                            or "all"))
        if comove:
            # Underwater / MFE-decay flip shadow (or controlled live).
            # Default: journal would_flip, still refuse opposite hedges.
            flip_reason, flip_detail = ccm.flip_shadow_verdict(
                bool(getattr(self.config, "crypto_comove_flip_enabled", True)),
                comove, open_now,
                flip_live=bool(getattr(
                    self.config, "crypto_comove_flip_live", False)),
                prefer_flat=bool(getattr(
                    self.config, "crypto_comove_flip_prefer_flat", True)),
                min_dom_risk_usd=float(getattr(
                    self.config, "crypto_comove_min_dom_risk_usd", 0.0)
                    or 0.0),
                underwater_s=float(getattr(
                    self.config, "crypto_comove_flip_underwater_s", 900.0)
                    or 900.0),
                giveback_frac=float(getattr(
                    self.config, "crypto_comove_flip_giveback", 0.60)
                    or 0.60),
                flip_used_today=self._flip_used_today(),
            )
            if flip_detail:
                append_event(self.journal, {
                    "type": ccm.CRYPTO_COMOVE_WOULD_FLIP,
                    "semantic_id": sid, "asset": asset, "side": side,
                    **{k: v for k, v in flip_detail.items()
                       if k != "event"},
                })
            if flip_reason is None and flip_detail and flip_detail.get(
                    "allow_opposite"):
                self._mark_flip_used()
                # fall through — controlled live opposite allow
            else:
                detail = ccm.crypto_comove_detail(open_now)
                return self._reject(sid, flip_reason or comove, **{
                    k: v for k, v in detail.items() if k != "reason"})
        # Post-win re-spend latch: after a large crypto win, refuse
        # β-proximate tickets that would re-spend the gain (SOL/NEAR).
        respend_reason, respend_detail = pwr.respend_verdict(
            bool(getattr(self.config, "post_win_respend_enabled", True)),
            self._respend_latch(), asset, fam, risk_usd, time.time(),
            asset_class=str(getattr(self.config, "asset_class", "all")
                            or "all"))
        if respend_reason:
            latch = self._respend_latch()
            pwr.note_respend_block(latch)
            self._save_respend_latch(latch)
            append_event(self.journal, {
                "type": "post_win_respend_blocked", "semantic_id": sid,
                **respend_detail,
            })
            return self._reject(sid, respend_reason, **{
                k: v for k, v in respend_detail.items() if k != "reason"})
        # Pyramid law: banked runner → harvest/retouch, never second birth.
        rerisk = ep.runner_rerisk_standdown(
            self.open_brackets(), asset, side,
            enabled=bool(getattr(self.config, "bank_protect_enabled", True)),
            min_bank_usd=float(getattr(
                self.config, "bank_protect_min_usd", 20.0) or 20.0))
        if rerisk:
            return self._reject(sid, rerisk)
        veto = self._loss_veto(fam, side)
        if veto:
            return self._reject(
                sid, f"loss veto {fam}|{side}: n={veto['n']} "
                     f"shrunk_exp=${veto['shrunk_expectancy']:.2f}")
        # H2 coh stamp invariant — refuse dishonest / unit-stain coh.
        coh_reason = ep.coh_stamp_verdict(
            coherence, bool(getattr(
                self.config, "coh_stamp_invariant_enabled", True)))
        if coh_reason:
            return self._reject(sid, coh_reason)

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
        # ATS-35/39: eval sleeves use cost-aware max_frac (hard-capped) so
        # ATR stops that print ~$1.29 rt on a $5 probe are not auto-killed;
        # kill-switch-off stays at base frac per sleeve.
        notional = quantity * entry
        rt_cost = notional * Decimal(str(self.config.taker_fee_rate)) * 2
        cost_frac = ep.cost_aware_max_frac(
            self.config.profile, asset,
            bool(getattr(self.config, "eval_tradfi_min_risk_enabled", False)),
            float(self.config.cost_risk_max_frac),
            float(entry), float(stop),
            float(self.config.taker_fee_rate),
            float(getattr(self.config, "eval_tradfi_cost_hard_cap", 0.30)
                  or 0.30),
            crypto_enabled=bool(getattr(
                self.config, "eval_crypto_min_risk_enabled", False)),
            crypto_hard_cap=float(getattr(
                self.config, "eval_crypto_cost_hard_cap", 0.30) or 0.30))
        if rt_cost > Decimal(str(risk_usd)) * Decimal(str(cost_frac)):
            return self._reject(
                sid, f"round-trip cost ${rt_cost:.2f} > "
                     f"{cost_frac:.0%} of ${risk_usd:.2f} risk")
        # Cost-to-expected-move ceiling (Θ). Complements the hard-cap
        # allowance raise above: refuse fee-dominated geometry without
        # mutating cost_frac. Shadow journals when live=False.
        cm_reason, cm_detail = cmg.cost_move_verdict(
            bool(getattr(self.config, "cost_move_gate_enabled", True)),
            bool(getattr(self.config, "cost_move_gate_live", True)),
            notional=float(notional),
            taker_rate=float(self.config.taker_fee_rate),
            risk_usd=risk_usd,
            theta=float(getattr(self.config, "cost_move_theta", 0.25)
                        or 0.25),
            asset_class=str(getattr(self.config, "asset_class", "all")
                            or "all"),
            asset=asset,
        )
        if cm_detail.get("would_block") or cm_detail.get("shadow"):
            append_event(self.journal, {
                "type": ("cost_to_expected_move_blocked"
                         if cm_reason else "cost_to_expected_move_would_block"),
                "semantic_id": sid, "asset": asset, **cm_detail,
            })
        if cm_reason:
            return self._reject(sid, cm_reason, **{
                k: v for k, v in cm_detail.items() if k != "reason"})
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
        except ProprAPIError as exc:
            # 4xx (incl. 429) after commit: order definitely not accepted —
            # release or phantom committed latches the day budget (2026-11-11
            # turbo silence class). Reload path already releases on Exception.
            self.budget.release(risk_usd, 0.0)
            append_event(self.journal, {
                "type": "bracket_place_rejected", "semantic_id": sid,
                "asset": asset, "side": side, "risk_usd": risk_usd,
                "status": exc.status_code, "code": exc.code,
                "error": str(exc),
            })
            return BracketResult(ok=False, semantic_id=sid,
                                 reason=f"place rejected: {exc}")
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
        # Accumulate proximate re-spend when a place cleared the latch budget.
        latch = self._respend_latch()
        if pwr.latch_active(latch, time.time()) and pwr.beta_proximate(
                latch.get("winner_family"), fam):
            pwr.note_respend_commit(latch, risk_usd)
            self._save_respend_latch(latch)
        append_event(self.journal, {
            "type": "bracket_placed", "semantic_id": sid,
            "asset": asset, "side": side, "quantity": str(quantity),
            "risk_usd": risk_usd, "stop": str(stop),
            "orders": len(placed),
        })
        return BracketResult(ok=True, semantic_id=sid,
                             group_id=body["orderGroupId"], orders=placed)

    @staticmethod
    def _best_realized(venue_realized: float,
                       trades_realized: float | None) -> float:
        """Prefer closing-leg trade sum when open-position realizedPnl is
        near-zero / stale (turbo BTC bank_protect blind — venue ~−$0.04
        while TP partials already banked)."""
        booked = float(venue_realized)
        if trades_realized is None:
            return booked
        try:
            tr = float(trades_realized)
        except (TypeError, ValueError):
            return booked
        if abs(tr) >= abs(booked) or (abs(booked) < 0.01 and abs(tr) > 0.0):
            return tr
        return booked

    def sync_bracket_size(self, bracket: dict, position: dict,
                          trades_realized: float | None = None) -> bool:
        """Reconcile registry truth against the venue position after fills.

        TP rung fills shrink the venue position but never the registry — the
        resting stop stays sized for the ORIGINAL quantity, and venue
        behavior on an oversized reduceOnly stop is untested (clamp vs
        reject vs flip: a reject leaves the runner naked). On shrink:
        release the harvested slice of committed risk, book the realized
        delta, re-place the stop at venue size, THEN update the registry
        (retry next tick if the venue work fails). Returns True when the
        registry row changed.

        trades_realized (optional): sum of reduce/close fill PnL since open
        — stamps realized_booked so bank_protect (≥$20) can arm even when
        position.realizedPnl stays near zero on the open runner."""
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
        booked_src = self._best_realized(venue_realized, trades_realized)
        prev_booked = float(bracket.get("realized_booked", 0.0) or 0.0)
        booked_changed = abs(booked_src - prev_booked) > 1e-9
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
            if booked_changed:
                bracket["realized_booked"] = booked_src
            return True
        if venue_qty >= reg_qty * Decimal("0.98"):
            # Qty flat — still refresh realized_booked from trades so
            # bank_protect can arm after TP fills that left qty≈registry
            # (fee dust / rounding) or when a prior sync booked venue≈0.
            if not booked_changed:
                return False
            bracket["realized_booked"] = booked_src
            append_event(self.journal, {
                "type": "realized_booked_synced", "semantic_id": sid,
                "asset": bracket.get("asset"),
                "realized_booked": booked_src,
                "venue_realized": venue_realized,
                "trades_realized": trades_realized,
            })
            if self.protect_realized_bank(bracket, position):
                pass
            return True
        frac_remaining = float(venue_qty / reg_qty)
        already = float(bracket.get("risk_released", 0.0))
        release = bracket["risk_usd"] * (1.0 - frac_remaining) - already
        realized_delta = booked_src - prev_booked
        if release > 0 and self.budget.state is not None:
            self.budget.release(release, max(0.0, realized_delta))
            bracket["risk_released"] = already + release
        # Always stamp booked on shrink (budget gate must not blind
        # bank_protect when budget.state is briefly None).
        bracket["realized_booked"] = booked_src
        ok = self.resize_protection(bracket, venue_qty,
                                    str(position.get("positionId") or ""))
        append_event(self.journal, {
            "type": "bracket_size_synced", "semantic_id": sid,
            "asset": bracket.get("asset"), "registry_qty": str(reg_qty),
            "venue_qty": str(venue_qty), "protection_resized": ok,
            "risk_released": round(release, 4),
            "realized_delta": round(realized_delta, 4),
            "realized_booked": booked_src,
            "trades_realized": trades_realized,
        })
        if not ok:
            return False  # registry stays stale -> retried next reconcile
        bracket["quantity"] = str(venue_qty)
        # Institutional MFE: once a partial bank lands, lock remainder so
        # the bank cannot return (Dayo 75% BTC @ ~$20). Tighten-only.
        if self.protect_realized_bank(bracket, position):
            pass  # registry already dirty; stop stamped inside
        return True

    def rebase_entry_from_venue(self, bracket: dict, position: dict) -> bool:
        """Rebase signal entry_ref → venue entryPrice (BTC #2 fill gap).

        Must run before BE/trail/MFE so R math sees the fill, not the
        signal. Journaled; resets phantom mfe_peak_r inside the brain."""
        if not isinstance(position, dict):
            return False
        prev = ep.rebase_entry_from_venue(bracket, position)
        if prev is None:
            return False
        append_event(self.journal, {
            "type": "entry_rebased_from_venue",
            "semantic_id": bracket.get("semantic_id"),
            "asset": bracket.get("asset"),
            "previous_entry": prev,
            "entry_ref": bracket.get("entry_ref"),
            "venue_entry": position.get("entryPrice"),
        })
        return True

    def protect_realized_bank(self, bracket: dict, position: dict) -> bool:
        """After bank criteria met, lock remainder stop at entry.

        Criteria: realized_booked ≥ min OR residual/MFE latch (GOLD 4.62R
        class — floor armed + stop through entry + residual peak ≥ min).
        Already-tighter trail/MFE → latch-only stamp (no loosen). Kill OFF
        → False. Compose with trail/MFE (tighten-only)."""
        if not bool(getattr(self.config, "bank_protect_enabled", True)):
            return False
        if self.config.profile != "evaluation":
            return False
        if not isinstance(position, dict):
            return False
        if bracket.get("bank_protected"):
            return False
        min_bank = float(getattr(
            self.config, "bank_protect_min_usd", 20.0) or 20.0)
        if not ep.bank_protect_criteria_met(bracket, min_bank_usd=min_bank):
            return False
        sid = bracket.get("semantic_id", "")
        target = ep.bank_protect_verdict(
            self.config.profile, bracket, enabled=True,
            min_bank_usd=min_bank)
        if target is None:
            # Already tighter than entry (trail/MFE past BE) — stamp only.
            if not ep.stop_locks_entry(
                    bracket.get("side"), bracket.get("current_stop"),
                    bracket.get("entry_ref")):
                return False
            bracket["bank_protected"] = True
            bracket["be_armed"] = True
            append_event(self.journal, {
                "type": "bank_protected",
                "semantic_id": sid, "asset": bracket.get("asset"),
                "entry": bracket.get("entry_ref"),
                "target": bracket.get("entry_ref"),
                "previous_stop": bracket.get("current_stop"),
                "realized_booked": bracket.get("realized_booked"),
                "risk_released": bracket.get("risk_released"),
                "mfe_peak_r": bracket.get("mfe_peak_r"),
                "mfe_floor_armed": bool(bracket.get("mfe_floor_armed")),
                "min_bank_usd": min_bank,
                "latch_only": True,
            })
            return True
        venue_qty = Decimal(str(position.get("quantity") or "0"))
        if venue_qty <= 0:
            return False
        ok = self.resize_protection(
            bracket, venue_qty, str(position.get("positionId") or ""),
            trigger_override=str(target))
        append_event(self.journal, {
            "type": "bank_protected" if ok else "bank_protect_failed",
            "semantic_id": sid, "asset": bracket.get("asset"),
            "entry": bracket.get("entry_ref"),
            "target": str(target),
            "previous_stop": bracket.get("current_stop"),
            "realized_booked": bracket.get("realized_booked"),
            "risk_released": bracket.get("risk_released"),
            "mfe_peak_r": bracket.get("mfe_peak_r"),
            "mfe_floor_armed": bool(bracket.get("mfe_floor_armed")),
            "min_bank_usd": min_bank,
            "latch_only": False,
        })
        if not ok:
            return False
        bracket["current_stop"] = str(target)
        bracket["bank_protected"] = True
        bracket["be_armed"] = True  # entry lock = proven
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

    def arm_breakeven_from_mark(self, bracket: dict, position: dict,
                                mark: float | None) -> bool:
        """H1 mark-plane BE (GOLD class): mfe_peak_r ≥arm_r → lock at entry.

        Shared floor = eval_be_arm_r (~0.5). Kill OFF
        (be_trail_invariant_enabled=False) → always False (legacy).
        Exact-entry resize then stamps be_armed; fee-concession never proves.
        Call after rebase_entry_from_venue so fill geometry owns R."""
        if not bool(getattr(self.config, "be_trail_invariant_enabled", True)):
            return False
        if self.config.profile != "evaluation" or mark is None:
            return False
        if not isinstance(position, dict):
            return False
        if not ep.should_arm_breakeven_from_mark(
                self.config.profile, bracket, mark,
                self.config.eval_be_arm_r):
            return False
        sid = bracket.get("semantic_id", "")
        entry = bracket["entry_ref"]
        venue_qty = Decimal(str(position.get("quantity") or "0"))
        if venue_qty <= 0:
            return False
        ok = self.resize_protection(
            bracket, venue_qty, str(position.get("positionId") or ""),
            trigger_override=entry)
        append_event(self.journal, {
            "type": "breakeven_armed_mark" if ok else "breakeven_arm_mark_failed",
            "semantic_id": sid, "asset": bracket.get("asset"),
            "entry": str(entry), "mark": mark})
        if not ok:
            return False
        bracket["be_armed"] = True
        bracket["current_stop"] = str(entry)
        return True

    def heal_be_armed_stamp(self, bracket: dict) -> bool:
        """Stamp be_armed when current_stop already locks entry (H1 heal)."""
        if not bool(getattr(self.config, "be_trail_invariant_enabled", True)):
            return False
        if not ep.be_armed_needs_heal(bracket):
            return False
        bracket["be_armed"] = True
        append_event(self.journal, {
            "type": "be_armed_healed",
            "semantic_id": bracket.get("semantic_id"),
            "asset": bracket.get("asset"),
            "current_stop": bracket.get("current_stop"),
            "entry_ref": bracket.get("entry_ref")})
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
        # BTC 30→15 doctrine: once MFE floor armed, prefer bank + stand
        # down re-add while price attacks the floor (not martingale).
        if ep.mfe_floor_standdown_reload(
                bracket,
                bool(getattr(self.config, "mfe_sharpen_enabled", True)
                     or getattr(self.config, "bank_protect_enabled", True)),
                min_bank_usd=float(getattr(
                    self.config, "bank_protect_min_usd", 20.0) or 20.0)):
            append_event(self.journal, {
                "type": "mfe_standdown_reload",
                "semantic_id": sid, "asset": bracket.get("asset"),
                "mfe_peak_r": bracket.get("mfe_peak_r"),
                "risk_usd": bracket.get("risk_usd"),
                "realized_booked": bracket.get("realized_booked"),
                "bank_protected": bool(bracket.get("bank_protected")),
            })
            return BracketResult(ok=False, semantic_id=sid,
                                 reason="mfe floor standdown")
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
        lock 1R @2R, tighten-only. Shared mfe_peak_r ≥ eval_be_arm_r floor
        before any rung (BTC #2 birth-trail class). mark=None (stale/
        absent HL) -> no-op, the uPnL breakeven arm stays the only trigger
        — fail-open bit-for-bit. The mark drives the trigger ONLY; risk
        math never sees it. Call after rebase_entry_from_venue."""
        if self.config.profile != "evaluation" or mark is None:
            return False
        if not isinstance(position, dict):
            return False
        sid = bracket.get("semantic_id", "")
        target = ep.trail_verdict(
            self.config.profile, bracket, mark,
            self.config.taker_fee_rate,
            min_peak_r=float(getattr(self.config, "eval_be_arm_r", 0.5)
                             or 0.5))
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
        # H1 heal: biconditional be_armed ↔ stop_locks_entry (false-neg).
        self.heal_be_armed_stamp(bracket)
        return True

    def sharpen_mfe_bracket(self, bracket: dict, position: dict,
                            mark: float | None) -> bool:
        """BTC-class peak→floor stop tighten (PROP_MFE_SHARPEN).

        Extends the trail/BE path — stop only, never TP. peak_usd≥trigger
        → lock max(min_floor, frac×peak). Kill OFF → always False."""
        if not bool(getattr(self.config, "mfe_sharpen_enabled", True)):
            return False
        if self.config.profile != "evaluation" or mark is None:
            return False
        if not isinstance(position, dict):
            return False
        min_floor = float(getattr(
            self.config, "mfe_sharpen_min_floor_usd", 20.0) or 20.0)
        min_bank = float(getattr(
            self.config, "bank_protect_min_usd", 20.0) or 20.0)
        base_frac = float(getattr(
            self.config, "mfe_sharpen_floor_frac", 0.5) or 0.5)
        banked_frac = float(getattr(
            self.config, "mfe_sharpen_banked_floor_frac", 0.75) or 0.75)
        target = ep.mfe_sharpen_verdict(
            self.config.profile, bracket, mark,
            enabled=True,
            peak_usd_trigger=float(getattr(
                self.config, "mfe_sharpen_peak_usd", 30.0) or 30.0),
            floor_frac=base_frac,
            min_floor_usd=min_floor,
            banked_floor_frac=banked_frac,
            min_bank_usd=min_bank)
        if target is None:
            ep.ratchet_mfe_peak_r(bracket, mark)
            return False
        sid = bracket.get("semantic_id", "")
        venue_qty = Decimal(str(position.get("quantity") or "0"))
        if venue_qty <= 0:
            return False
        peak_usd = ep.mfe_peak_usd(bracket, residual=False)
        peak_usd_residual = ep.mfe_peak_usd(bracket, residual=True)
        banked = ep._banked(bracket, min_bank)
        frac = banked_frac if banked else base_frac
        floor_label, floor_residual = ep.mfe_floor_dollars(
            bracket, floor_frac=frac, min_floor_usd=min_floor)
        ok = self.resize_protection(
            bracket, venue_qty, str(position.get("positionId") or ""),
            trigger_override=str(target))
        append_event(self.journal, {
            "type": "mfe_sharpened" if ok else "mfe_sharpen_failed",
            "semantic_id": sid, "asset": bracket.get("asset"),
            "mark": mark, "target": str(target),
            "previous_stop": bracket.get("current_stop"),
            "mfe_peak_r": bracket.get("mfe_peak_r"),
            "peak_usd": peak_usd,
            "peak_usd_residual": peak_usd_residual,
            "residual_risk_usd": ep.residual_risk_usd(bracket),
            "banked": banked,
            "floor_frac": frac,
            # floor_usd = residual (honest); label kept for phantom census.
            "floor_usd": floor_residual,
            "floor_usd_label": floor_label,
            "floor_usd_residual": floor_residual,
            "residual_stop_lock_usd": ep.residual_stop_lock_usd(bracket),
        })
        if not ok:
            return False
        bracket["current_stop"] = str(target)
        bracket["mfe_floor_armed"] = True
        if (not bracket.get("be_armed")
                and ep.stop_locks_entry(bracket["side"], target,
                                        bracket.get("entry_ref"))):
            bracket["be_armed"] = True
        self.heal_be_armed_stamp(bracket)
        return True

    def harvest_runner_bank_floor(self, bracket: dict, position: dict,
                                  mark: float | None) -> bool:
        """Scalper/pyramid harvest rungs on a banked runner (re-touch)."""
        if not bool(getattr(self.config, "runner_bank_floor_enabled", True)):
            return False
        if self.config.profile != "evaluation" or mark is None:
            return False
        if not isinstance(position, dict):
            return False
        target = ep.runner_bank_floor_verdict(
            self.config.profile, bracket, mark, enabled=True,
            min_bank_usd=float(getattr(
                self.config, "bank_protect_min_usd", 20.0) or 20.0))
        if target is None:
            return False
        sid = bracket.get("semantic_id", "")
        venue_qty = Decimal(str(position.get("quantity") or "0"))
        if venue_qty <= 0:
            return False
        ok = self.resize_protection(
            bracket, venue_qty, str(position.get("positionId") or ""),
            trigger_override=str(target))
        append_event(self.journal, {
            "type": "runner_bank_harvested" if ok else "runner_bank_harvest_failed",
            "semantic_id": sid, "asset": bracket.get("asset"),
            "mark": mark, "target": str(target),
            "previous_stop": bracket.get("current_stop"),
            "mfe_peak_r": bracket.get("mfe_peak_r"),
            "realized_booked": bracket.get("realized_booked"),
        })
        if not ok:
            return False
        bracket["current_stop"] = str(target)
        bracket["mfe_floor_armed"] = True
        if ep.stop_locks_entry(bracket["side"], target,
                               bracket.get("entry_ref")):
            bracket["be_armed"] = True
        self.heal_be_armed_stamp(bracket)
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

        prev_stop = bracket.get("current_stop") or bracket.get("stop_price")
        early_detail: dict[str, Any] = {}
        try:
            _, decimals = spec_for(asset)
            qty = round_qty(Decimal(venue_qty), decimals)
            stop_src = trigger_override or prev_stop or bracket["stop_price"]
            trigger = fmt(Decimal(str(stop_src)))
        except Exception as exc:
            return _fail("geometry", exc)
        # NEAR-class early tighten telemetry (observability only).
        if bool(getattr(self.config, "stop_resize_telemetry_enabled", True)):
            try:
                opened = float(bracket.get("opened_ts") or 0.0)
            except (TypeError, ValueError):
                opened = 0.0
            age_s = (time.time() - opened) if opened > 0 else None
            early_s = float(getattr(self.config, "stop_resize_early_s", 30.0)
                            or 30.0)
            try:
                prev_f = float(prev_stop) if prev_stop is not None else None
                new_f = float(trigger)
                entry_f = float(bracket.get("entry_ref") or 0.0)
                qty_f = float(qty)
            except (TypeError, ValueError):
                prev_f = new_f = entry_f = qty_f = None
            if (prev_f is not None and new_f is not None and entry_f
                    and qty_f and abs(new_f - prev_f) > 1e-12):
                reserved_before = abs(prev_f - entry_f) * qty_f
                reserved_after = abs(new_f - entry_f) * qty_f
                risk0 = float(bracket.get("risk_usd") or 0.0) or None
                delta_r = None
                if risk0 and risk0 > 0:
                    delta_r = (reserved_after - reserved_before) / risk0
                early_detail = {
                    "previous_trigger": str(prev_stop),
                    "new_trigger": trigger,
                    "age_s": age_s,
                    "reserved_before": round(reserved_before, 6),
                    "reserved_after": round(reserved_after, 6),
                    "stop_resize_delta_r": (
                        round(delta_r, 6) if delta_r is not None else None),
                    "trigger_override": trigger_override is not None,
                    "early": bool(age_s is not None and age_s <= early_s),
                }
                if early_detail["early"]:
                    append_event(self.journal, {
                        "type": "stop_resize_provenance",
                        "semantic_id": sid, "asset": asset,
                        **early_detail,
                    })
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
                "cancelled": cancelled, "cancel_scan_error": str(exc),
                **early_detail})
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
            "cancelled": cancelled, **early_detail})
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

    def _basket_tp_state(self) -> dict[str, Any]:
        return read_json(self.basket_tp_path, {})

    def _save_basket_tp_state(self, state: dict[str, Any]) -> None:
        atomic_write_json(self.basket_tp_path, state)

    def harvest_basket_tp(self, open_brackets: list | None = None) -> bool:
        """Peak-push conquest harvest (basket_graph → basket_tp controller).

        Reads last_upnl on open brackets. On border / new basket % peak /
        trail giveback: tax-bank close of highest-ROE winners (partial when
        residual-pyramid eligible). Fail-closed on kill / dark coh /
        cooloff / close errors. Returns True when ≥1 leg close succeeds.
        """
        if self.config.profile != "evaluation":
            return False
        if not bool(getattr(self.config, "basket_tp_enabled", True)):
            return False
        brackets = (open_brackets if open_brackets is not None
                    else self.open_brackets())
        # Open-only view for the graph (registry may carry closed rows).
        open_only = [b for b in (brackets or [])
                     if b.get("status") == "open"]
        state = self._basket_tp_state()
        now = time.time()
        decision = btp.basket_tp_verdict(
            self.config.profile, open_only, state,
            enabled=True,
            now_ts=now,
            min_n=int(getattr(self.config, "basket_tp_min_n", 2) or 2),
            min_book_usd=float(getattr(
                self.config, "basket_tp_min_book_usd", 10.0) or 10.0),
            arm_frac=float(getattr(
                self.config, "basket_tp_arm_frac", 0.25) or 0.25),
            harvest_ratio=float(getattr(
                self.config, "basket_tp_harvest_ratio", 0.60) or 0.60),
            giveback_frac=float(getattr(
                self.config, "basket_tp_giveback_frac", 0.60) or 0.60),
            cooloff_s=float(getattr(
                self.config, "basket_tp_cooloff_s", 90.0) or 90.0),
            tax_bank_enabled=bool(getattr(
                self.config, "basket_tp_tax_bank_enabled", True)),
            residual_pyramid_enabled=bool(getattr(
                self.config, "basket_tp_residual_pyramid_enabled", True)),
            residual_frac=float(getattr(
                self.config, "basket_tp_residual_frac", 0.25) or 0.25),
            residual_min_coh=float(getattr(
                self.config, "basket_tp_residual_min_coh", 4.0) or 4.0),
            adaptive_thresholds=bool(getattr(
                self.config, "basket_tp_adaptive_thresholds", True)),
        )
        # Persist peak ratchets even when below border (graph memory).
        self._save_basket_tp_state(state)
        if not decision.fire:
            if decision.reason not in (
                    "disabled", "min_n", "cooloff", "no_green", "no_risk",
                    "below_border", "no_targets", "peak_epoch_taxed"):
                append_event(self.journal, {
                    "type": "basket_tp_standdown",
                    "reason": decision.reason,
                    "n": decision.n,
                    "book_upnl": decision.book_upnl,
                    "sum_risk": decision.sum_risk,
                    "book_frac": decision.book_frac,
                    "peak_book_usd": decision.peak_book_usd,
                    "peak_book_roe": decision.peak_book_roe,
                    "payer": decision.payer_asset,
                })
            return False

        closed: list[str] = []
        residuals: list[str] = []
        errors: list[dict] = []
        by_sid = {b.get("semantic_id"): b for b in open_only}
        for tgt in decision.targets:
            asset = tgt.asset
            if not asset:
                continue
            try:
                self.client.cancel_all_open(base=asset)
                qty_arg = None
                if tgt.leave_residual and 0 < float(tgt.close_frac) < 1:
                    br = by_sid.get(tgt.semantic_id) or {}
                    try:
                        full = Decimal(str(br.get("quantity") or "0"))
                    except Exception:
                        full = Decimal("0")
                    if full > 0:
                        close_qty = (full * Decimal(str(tgt.close_frac))
                                     ).normalize()
                        if close_qty > 0 and close_qty < full:
                            qty_arg = format(close_qty, "f")
                self.client.close_position(asset, quantity=qty_arg)
                closed.append(tgt.semantic_id or asset)
                if qty_arg is not None:
                    residuals.append(tgt.semantic_id or asset)
                    # Stamp residual runner so BE/trail/sharpen keep ownership.
                    br = by_sid.get(tgt.semantic_id)
                    if isinstance(br, dict):
                        try:
                            left = (Decimal(str(br.get("quantity") or "0"))
                                    - Decimal(qty_arg))
                            if left > 0:
                                br["quantity"] = format(left, "f")
                                br["basket_residual"] = True
                        except Exception:
                            pass
            except Exception as exc:
                errors.append({
                    "semantic_id": tgt.semantic_id,
                    "asset": asset,
                    "error": str(exc),
                })
                append_event(self.journal, {
                    "type": "basket_tp_close_failed",
                    "semantic_id": tgt.semantic_id,
                    "asset": asset,
                    "error": str(exc),
                    "reason": decision.reason,
                    "close_frac": tgt.close_frac,
                })
        append_event(self.journal, {
            "type": "basket_tp_fired" if closed else "basket_tp_fire_failed",
            "reason": decision.reason,
            "n": decision.n,
            "book_upnl": decision.book_upnl,
            "sum_risk": decision.sum_risk,
            "book_frac": decision.book_frac,
            "peak_book_usd": decision.peak_book_usd,
            "peak_book_roe": decision.peak_book_roe,
            "tax_usd": decision.tax_usd,
            "harvest_usd": decision.harvest_usd,
            "max_leg_peak_usd": decision.max_leg_peak_usd,
            "payer": decision.payer_asset,
            "payer_roe": decision.payer_roe,
            "adaptive_arm_frac": decision.adaptive_arm_frac,
            "adaptive_min_book": decision.adaptive_min_book,
            "targets": [
                {"semantic_id": t.semantic_id, "asset": t.asset,
                 "upnl": t.upnl, "risk_usd": t.risk_usd, "roe": t.roe,
                 "peak_usd": t.peak_usd, "close_frac": t.close_frac,
                 "leave_residual": t.leave_residual,
                 "coherence": t.coherence}
                for t in decision.targets
            ],
            "closed": closed,
            "residuals": residuals,
            "errors": errors,
            **decision.telemetry,
        })
        if closed:
            btp.mark_fired(state, now, decision)
            self._save_basket_tp_state(state)
            # Residual qty stamps mutate caller-owned bracket dicts in place
            # (main.reconcile persists registry after this returns True).
            return True
        return False
