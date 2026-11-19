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
from typing import Any, Optional

from .bracket import DEFAULT_LADDER, build_bracket, fmt, round_qty, spec_for
from .brains import act_backoff as ab
from .brains import cost_move_gate as cmg
from .brains import crypto_comove as ccm
from .brains import early_tighten_gate as etg
from .brains import eval_profile as ep
from .brains import loss_stats as ls
from .brains import offense_state as ofs
from .brains import post_win_cooloff as pwc
from .brains import post_win_respend as pwr
from .brains import rebound as rb
from .brains import stop_arbiter as sa
from .brains import stop_floor as sf
from .brains.act_backoff import ActBackoff
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
        self.cooloff_path = config.state_root / "post_win_cooloff.json"
        self.comove_flip_path = config.state_root / "comove_flip.json"
        self.act_backoff = ActBackoff(
            enabled=bool(getattr(config, "act_backoff_enabled", True)),
            initial_s=float(getattr(config, "act_backoff_initial_s", 30.0)
                            or 30.0),
            max_s=float(getattr(config, "act_backoff_max_s", 300.0) or 300.0),
            latch_after=int(getattr(config, "act_backoff_latch_after", 3)
                            or 3),
        )
        # L8/L10 latch persistence (CEO audit S4): reload the persisted
        # 429-latch state (future expiries only) and persist every mutation
        # via the injected hook. Kill PROP_ACT_BACKOFF_PERSIST=0 → legacy
        # memory-only bit-for-bit.
        self.act_backoff_path = config.state_root / "act_backoff.json"
        if bool(getattr(config, "act_backoff_persist_enabled", True)):
            _ab_boot = self.act_backoff.load_state(
                read_json(self.act_backoff_path, None))
            if _ab_boot.get("keys") or _ab_boot.get("account"):
                append_event(self.journal, _ab_boot)

            def _persist_act_backoff(state: dict) -> None:
                atomic_write_json(self.act_backoff_path, state)

            self.act_backoff.on_state_change = _persist_act_backoff
        # Stop arbiter (Build 1, 2026-11-17): per-position per-tick ballot.
        # The reconcile loop opens the window (begin_stop_tick) around the
        # six stop-writer stages and closes it with flush_stop_arbiter —
        # writers VOTE their floor inside the window, one venue resize
        # fires. Direct writer calls outside the window (tests, tools)
        # keep the legacy immediate-resize path. Kill PROP_STOP_ARBITER=0
        # → _stop_resize never holds a vote, legacy bit-for-bit.
        self._stop_ballots: dict[str, list[dict]] = {}
        self._arbiter_open = False

    def _arbiter_on(self) -> bool:
        return bool(getattr(self.config, "stop_arbiter_enabled", True))

    def _early_tighten_ok(self, bracket: dict, stage: str) -> bool:
        """Risk-decreasing age/MFE gate before management raises the stop.

        BTC 7s trail / 22s runner_harvest class. Kill OFF → always True.
        Fail-OPEN on missing opened_ts/mfe. Journal throttled per bracket.
        """
        allowed, detail = etg.early_tighten_allowed(
            bracket,
            enabled=bool(getattr(
                self.config, "early_tighten_gate_enabled", True)),
            min_age_s=float(getattr(
                self.config, "early_tighten_min_age_s", 300.0) or 300.0),
            be_min_mfe_r=float(getattr(
                self.config, "early_tighten_be_min_mfe_r", 0.5) or 0.5),
            harvest_min_mfe_r=float(getattr(
                self.config, "early_tighten_harvest_min_mfe_r", 1.0)
                or 1.0),
            stage=stage,
        )
        if allowed:
            return True
        now = time.time()
        last = float(bracket.get("_early_tighten_log_ts") or 0.0)
        if now - last >= 60.0:
            bracket["_early_tighten_log_ts"] = now
            append_event(self.journal, {
                "type": "early_tighten_blocked",
                "semantic_id": bracket.get("semantic_id"),
                "asset": bracket.get("asset"),
                "side": bracket.get("side"),
                **{k: v for k, v in detail.items() if v is not None},
            })
        return False

    def _offense_apex(self, state: dict | None, bracket: dict) -> bool:
        """Sticky-apex ownership for this seat/asset under twin_hot."""
        if not bool(getattr(self.config, "offense_sticky_apex_enabled", True)):
            return True
        v = ofs.sticky_apex_verdict(
            state, asset=bracket.get("asset"),
            seat_is_primary=bool(getattr(
                self.config, "offense_apex_primary", False)),
            own_mfe_r=bracket.get("mfe_peak_r"),
            enabled=bool(getattr(self.config, "offense_state_enabled", True)))
        return bool(v.get("is_apex", True))

    def begin_stop_tick(self, bracket: dict) -> None:
        """Open the per-tick arbiter window for one bracket (reconcile
        owns the open/flush pair). A stale ballot from a crashed tick is
        dropped here so its targets never survive into a fresh vote."""
        if not self._arbiter_on():
            return
        self._arbiter_open = True
        self._stop_ballots.pop(str(bracket.get("semantic_id") or ""), None)

    def _stop_resize(self, bracket: dict, venue_qty, position_id: str,
                     trigger: str, *, stage: str):
        """The single seam every stop-writer's venue call flows through.

        Arbiter ON + window open → the stage's floor is VOTED, no venue
        call, returns "held" (the writer returns False immediately: no
        journal, no stamps, no current_stop write — the flush owns all
        three for the winning vote). Anything else → legacy direct
        resize_protection bit-for-bit."""
        if self._arbiter_on() and self._arbiter_open:
            sid = str(bracket.get("semantic_id") or "")
            ballot = self._stop_ballots.setdefault(sid, [])
            sa.propose(ballot, stage=stage, target=trigger)
            return "held"
        return self.resize_protection(bracket, venue_qty, position_id,
                                      trigger_override=trigger)

    def flush_stop_arbiter(self, bracket: dict, position: dict) -> bool:
        """Close the window: max-floor vote wins, dust epsilon vs the
        last issued floor gates the venue call, AT MOST one resize +
        one current_stop write. Returns True when the registry row
        mutated (the reconcile loop must flush). Kill OFF → clears any
        stray ballot, never touches the venue."""
        self._arbiter_open = False
        if not self._arbiter_on():
            self._stop_ballots.clear()
            return False
        sid = str(bracket.get("semantic_id") or "")
        ballot = self._stop_ballots.pop(sid, None)
        if not ballot:
            return False
        side = bracket.get("side")
        win = sa.winner(ballot, side)
        if win is None:
            return False
        stages = [p["stage"] for p in ballot]
        coalesced = [s for s in stages if s != win["stage"]]
        prev = bracket.get("current_stop") or bracket.get("stop_price")
        base = {"semantic_id": bracket.get("semantic_id"),
                "asset": bracket.get("asset"), "side": side,
                "winner_stage": win["stage"], "target": win["target"],
                "previous_stop": prev, "stages": stages,
                "coalesced": coalesced, "n": len(ballot)}
        # Hard guard: the arbiter NEVER loosens (verdicts are already
        # tighten-only; this is the belt to their suspenders).
        if prev is not None and not sa.is_tighter(side, win["target"], prev):
            append_event(self.journal, {
                "type": sa.SUPPRESSED, "reason": "not_tighter", **base})
            return False
        # Dust epsilon: sub-epsilon floor moves are not worth a venue
        # round-trip; writers re-vote next tick. stop_arbiter_dust_r > 0
        # overrides the legacy price-relative frac with a stop-distance-
        # relative epsilon (frac on a $20k-stop asset swallowed ~0.5R).
        dust = float(getattr(self.config, "stop_arbiter_dust_frac", 0.0005)
                     or 0.0)
        eps_abs: float | None = None
        dust_r = float(getattr(self.config, "stop_arbiter_dust_r", 0.0)
                       or 0.0)
        if dust_r > 0:
            try:
                entry = float(bracket.get("entry_ref") or 0)
                stop0 = float(bracket.get("stop_price") or 0)
                stop_dist = abs(entry - stop0)
                if stop_dist > 0:
                    eps_abs = dust_r * stop_dist
            except (TypeError, ValueError):
                eps_abs = None
        if prev is not None and not sa.past_dust(side, win["target"], prev,
                                                 dust, eps_abs=eps_abs):
            append_event(self.journal, {
                "type": sa.SUPPRESSED, "reason": "dust",
                "dust_frac": dust,
                "dust_r": dust_r or None, **base})
            return False
        if not isinstance(position, dict):
            return False
        try:
            venue_qty = Decimal(str(position.get("quantity") or "0"))
        except Exception:
            return False
        if venue_qty <= 0:
            return False
        ok = self.resize_protection(
            bracket, venue_qty, str(position.get("positionId") or ""),
            trigger_override=str(win["target"]))
        if ok is None:
            return False  # act-backoff defer — writers re-vote next tick
        append_event(self.journal, {
            "type": sa.COMMIT if ok else sa.FAILED, **base})
        if not ok:
            return False
        # Single write. Stamps follow the WINNING floor only (audit
        # 2026-11-17): a LOSING bank vote never sets bank_protected, and a
        # losing floor-stage vote never sets mfe_floor_armed — the phantom
        # flags made mfe_floor_standdown_reload refuse reloads against a
        # floor the venue never received. bank_protected is also earned
        # de facto when any winner locks the entry (be proof).
        bracket["current_stop"] = str(win["target"])
        locks_entry = ep.stop_locks_entry(side, win["target"],
                                          bracket.get("entry_ref"))
        if win["stage"] == "bank" or locks_entry:
            bracket["bank_protected"] = True
        if win["stage"] in ("mfe_sharpen", "live_peak", "runner_harvest"):
            bracket["mfe_floor_armed"] = True
        if not bracket.get("be_armed") and locks_entry:
            bracket["be_armed"] = True
        self.heal_be_armed_stamp(bracket)
        return True

    def _registry(self) -> dict[str, Any]:
        return read_json(self.registry_path, {"brackets": []})

    def _save_registry(self, reg: dict[str, Any]) -> None:
        atomic_write_json(self.registry_path, reg)

    def open_brackets(self) -> list[dict]:
        return [b for b in self._registry()["brackets"] if b.get("status") == "open"]

    def _respend_latch(self) -> dict[str, Any]:
        latch = read_json(self.respend_path, {})
        # Read-path TTL prune (2026-11-17): an expired latch must never cap
        # fresh budgets via a stale respend_committed. Kill
        # PROP_STATE_TTL_PRUNE=0 → legacy raw read bit-for-bit.
        if latch and bool(getattr(self.config, "state_ttl_prune_enabled", True)):
            if not pwr.latch_active(latch, time.time()):
                return {}
        return latch

    def _save_respend_latch(self, latch: dict[str, Any]) -> None:
        atomic_write_json(self.respend_path, latch)

    def _comove_flip_state(self) -> dict[str, Any]:
        return read_json(self.comove_flip_path, {})

    def _save_comove_flip_state(self, state: dict[str, Any]) -> None:
        atomic_write_json(self.comove_flip_path, state)

    def _cooloff_latches(self) -> dict[str, Any]:
        latches = read_json(self.cooloff_path, {})
        # Read-path TTL prune (2026-11-17): expired cooloff entries are
        # dropped on load. Kill PROP_STATE_TTL_PRUNE=0 → legacy raw read.
        if latches and bool(getattr(self.config, "state_ttl_prune_enabled", True)):
            return pwc.prune_expired(latches, time.time())
        return latches

    def _save_cooloff_latches(self, latches: dict[str, Any]) -> None:
        atomic_write_json(self.cooloff_path,
                          pwc.prune_expired(latches, time.time()))

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

    def arm_post_win_cooloff(self, bracket: dict,
                             *, now: float | None = None) -> bool:
        """Arm/re-arm the (class, side) cooloff from a qualifying win close."""
        if not bool(getattr(self.config, "post_win_cooloff_enabled", False)):
            return False
        pnl = bracket.get("realized_pnl_final")
        risk = bracket.get("risk_usd")
        if not pwc.win_qualifies(
                pnl, risk,
                min_win_usd=float(getattr(
                    self.config, "post_win_cooloff_min_win_usd", 1.5) or 1.5),
                min_win_r=float(getattr(
                    self.config, "post_win_cooloff_min_win_r", 0.20) or 0.20)):
            return False
        ts = float(now if now is not None else time.time())
        cls = pwc.class_of(str(bracket.get("asset") or ""))
        latches = self._cooloff_latches()
        pwc.arm_latch(
            latches, asset_class=cls, side=bracket.get("side"),
            realized_pnl=float(pnl), risk_usd=risk, now=ts,
            ttl_s=float(getattr(
                self.config, "post_win_cooloff_ttl_s", 1800.0) or 1800.0),
            semantic_id=bracket.get("semantic_id"),
        )
        latch = latches[pwc.latch_key(cls, bracket.get("side"))]
        self._save_cooloff_latches(latches)
        append_event(self.journal, {
            "type": "post_win_cooloff_armed",
            "asset_class": cls,
            "side": latch.get("side"),
            "win_usd": latch.get("win_usd"),
            "expires_at": latch.get("expires_at"),
            "ttl_s": latch.get("ttl_s"),
            "wins": latch.get("wins"),
            "semantic_id": latch.get("semantic_id"),
        })
        return True

    def _place_maker_entry(self, body: dict, *, asset: str, side: str,
                           quantity: Decimal, entry: Decimal, stop: Decimal,
                           ts_ms: int, ladder, risk_usd: float,
                           sid: str) -> tuple[list[dict] | None, dict[str, Any]]:
        """Two-phase maker-first entry (2026-11-15 fee attack).

        Phase 1: entry-only limit GTC at entry_ref (maker 0.015% vs taker
        0.045% — the proven 3x tier cut). Poll the intent for a fill up to
        maker_entry_timeout_s. Phase 2: the protection legs ride a second
        batch against the now-live position (same intents = idempotent
        retry; the entry intent is the only one consumed by phase 1).

        Failure doctrine:
        - unfilled + fallback ON  -> full market bracket, FRESH ts_ms
          (the cancelled limit's intent is burnt venue-side).
        - unfilled + fallback OFF -> release risk, stand down.
        - phase-2 4xx after a fill -> naked position: close_position
          immediately, release risk, journal maker_entry_protection_failed.

        Returns (placed_or_None, meta). meta carries the final body
        (intents actually sent), group_id, entry_type, fill_s, reason.
        """
        timeout = float(getattr(self.config, "maker_entry_timeout_s", 8.0)
                        or 8.0)
        fallback = bool(getattr(self.config, "maker_entry_taker_fallback",
                                True))
        entry_order = body["orders"][0]
        entry_intent = entry_order["intentId"]
        group_id = body["orderGroupId"]
        self.budget.commit(risk_usd)
        t0 = time.time()

        def _find(intent: str) -> dict | None:
            try:
                return self.client.find_by_intent(intent)
            except Exception:
                return None

        try:
            p1 = self.client.place_orders(
                {"orders": [entry_order], "orderGroupId": group_id})
        except ProprAPIError as exc:
            self.budget.release(risk_usd, 0.0)
            append_event(self.journal, {
                "type": "bracket_place_rejected", "semantic_id": sid,
                "asset": asset, "side": side, "phase": "maker_entry",
                "error": str(exc), "status_code": exc.status_code,
            })
            return None, {"reason": f"place rejected: {exc}"}
        except ProprUnknownState as exc:
            found = _find(entry_intent)
            append_event(self.journal, {
                "type": "bracket_unknown", "semantic_id": sid,
                "phase": "maker_entry",
                "error": str(exc), "reconciled": bool(found),
            })
            if not found:
                self.budget.release(risk_usd, 0.0)
                return None, {"reason": f"UNKNOWN then absent: {exc}"}
            p1 = [found]

        filled = _find(entry_intent)
        if not (filled and str(filled.get("status")) == "filled"):
            filled = None
            deadline = t0 + timeout
            while time.time() < deadline:
                o = _find(entry_intent)
                if o and str(o.get("status")) == "filled":
                    filled = o
                    break
                time.sleep(1.0)
        if filled is None:
            order_id = (p1[0] or {}).get("orderId")
            if order_id:
                try:
                    self.client.cancel_order(order_id)
                except Exception:
                    pass
            o = _find(entry_intent)  # cancel/fill race re-check
            if o and str(o.get("status")) == "filled":
                filled = o
        fill_s = time.time() - t0

        if filled is None:
            if not fallback:
                self.budget.release(risk_usd, 0.0)
                append_event(self.journal, {
                    "type": "maker_entry_unfilled_standdown",
                    "semantic_id": sid, "asset": asset, "side": side,
                    "wait_s": round(fill_s, 2),
                })
                return None, {"reason": "maker_entry_unfilled_standdown"}
            fb_ts = int(time.time() * 1000)
            fb_body = build_bracket(
                account_id=self.client.account_id, asset=asset, side=side,
                quantity=str(quantity), entry_ref=str(entry),
                stop_price=str(stop), ts_ms=fb_ts, quote=self.config.quote,
                entry_type="market", ladder=ladder,
                fee_rate=str(self.config.taker_fee_rate))
            try:
                placed = self.client.place_orders(fb_body)
            except ProprAPIError as exc:
                self.budget.release(risk_usd, 0.0)
                append_event(self.journal, {
                    "type": "bracket_place_rejected", "semantic_id": sid,
                    "asset": asset, "side": side, "phase": "taker_fallback",
                    "error": str(exc), "status_code": exc.status_code,
                })
                return None, {"reason": f"place rejected: {exc}"}
            except ProprUnknownState as exc:
                fb_intent = fb_body["orders"][0]["intentId"]
                found = _find(fb_intent)
                append_event(self.journal, {
                    "type": "bracket_unknown", "semantic_id": sid,
                    "phase": "taker_fallback",
                    "error": str(exc), "reconciled": bool(found),
                })
                if not found:
                    self.budget.release(risk_usd, 0.0)
                    return None, {"reason": f"UNKNOWN then absent: {exc}"}
                placed = [found]
            append_event(self.journal, {
                "type": "maker_entry_unfilled_taker_fallback",
                "semantic_id": sid, "asset": asset, "side": side,
                "wait_s": round(fill_s, 2),
            })
            return placed, {"body": fb_body,
                            "group_id": fb_body["orderGroupId"],
                            "entry_type": "market", "fill_s": fill_s}

        # Filled (maybe partially): size protection to the ACTUAL position.
        actual_qty: Decimal | None = None
        pos_pid = None
        try:
            pos = self.client.get_positions(status="open", base=asset)
            if pos:
                actual_qty = Decimal(str(pos[0].get("quantity")))
                pos_pid = pos[0].get("positionId") or pos[0].get("id")
        except Exception:
            actual_qty = None
        legs = body["orders"][1:]
        if actual_qty is not None and str(actual_qty) != str(
                entry_order["quantity"]):
            rebuilt = build_bracket(
                account_id=self.client.account_id, asset=asset, side=side,
                quantity=str(actual_qty), entry_ref=str(entry),
                stop_price=str(stop), ts_ms=ts_ms, quote=self.config.quote,
                entry_type="limit", ladder=ladder,
                fee_rate=str(self.config.taker_fee_rate))
            legs = rebuilt["orders"][1:]
        if pos_pid:
            for leg in legs:
                leg["positionId"] = pos_pid
        try:
            p2 = self.client.place_orders(
                {"orders": legs, "orderGroupId": group_id})
        except ProprAPIError as exc:
            # Position is naked — fail CLOSED: flatten, release, journal.
            try:
                self.client.close_position(asset)
            except Exception:
                pass
            self.budget.release(risk_usd, 0.0)
            append_event(self.journal, {
                "type": "maker_entry_protection_failed", "semantic_id": sid,
                "asset": asset, "side": side,
                "error": str(exc), "status_code": exc.status_code,
            })
            return None, {
                "reason": f"maker_entry_protection_failed: {exc}"}
        except ProprUnknownState as exc:
            sl_intent = legs[0]["intentId"]
            found = _find(sl_intent)
            if not found:
                try:
                    self.client.close_position(asset)
                except Exception:
                    pass
                self.budget.release(risk_usd, 0.0)
                append_event(self.journal, {
                    "type": "maker_entry_protection_failed",
                    "semantic_id": sid, "asset": asset, "side": side,
                    "error": f"UNKNOWN then absent: {exc}",
                })
                return None, {
                    "reason": f"maker_entry_protection_failed: {exc}"}
            p2 = [found]
        append_event(self.journal, {
            "type": "maker_entry_filled", "semantic_id": sid,
            "asset": asset, "side": side, "fill_s": round(fill_s, 2),
            "quantity": str(actual_qty or entry_order["quantity"]),
        })
        sent = dict(body)
        sent["orders"] = [entry_order] + legs
        return list(p1) + list(p2), {"body": sent, "group_id": group_id,
                                     "entry_type": "limit",
                                     "fill_s": fill_s}

    def _reject(self, sid: str, reason: str, **extra) -> BracketResult:
        payload = {
            "type": "kant_reject", "semantic_id": sid, "reason": reason,
        }
        payload.update({k: v for k, v in extra.items() if v is not None})
        append_event(self.journal, payload)
        return BracketResult(ok=False, semantic_id=sid, reason=reason)

    def _loss_veto(self, asset: str, family: str, side: str) -> dict | None:
        """Consume the probe's loss_veto artifact. Fail-OPEN on stale/
        missing/corrupt — a dead probe never freezes the fleet.

        Asset grain (Build 5): schema≠2 (old/absent) → fail CLOSED to
        EMPTY (no veto) — a legacy family-keyed artifact must never veto
        under asset keys. Kill OFF → legacy (family, side) bit-for-bit.

        Grain-miss telemetry (audit 2026-11-17): a fresh artifact whose
        schema does NOT match this consumer's grain silences veto AND
        boost with zero trace — emit a throttled loss_veto_grain_miss so
        the 4-cell publisher-schema × consumer-grain matrix is audible."""
        if not self.config.loss_veto_enabled:
            return None
        artifact = read_json(self.config.loss_veto_artifact)
        if not ls.artifact_fresh(artifact):
            return None
        if bool(getattr(self.config, "loss_veto_asset_grain_enabled", True)):
            if not isinstance(artifact, dict) or artifact.get("schema") != 2:
                self._grain_miss_event(artifact, want="asset")
                return None
            return ls.veto_for(artifact.get("stats") or {}, asset, side)
        if isinstance(artifact, dict) and artifact.get("schema") == 2:
            self._grain_miss_event(artifact, want="family")
            return None
        return ls.veto_for(artifact.get("stats") or {}, family, side)

    def _grain_miss_event(self, artifact: Any, *, want: str) -> None:
        """Throttled (300s) grain-mismatch telemetry — fires at most once
        per ttl across all entry attempts (the miss is a fleet-level
        wiring defect, not a per-candidate fact)."""
        try:
            now = time.time()
            last = getattr(self, "_grain_miss_log_ts", 0.0) or 0.0
            if now - last < 300.0:
                return
            self._grain_miss_log_ts = now
            append_event(self.journal, {
                "type": "loss_veto_grain_miss",
                "want_grain": want,
                "artifact_schema": (artifact or {}).get("schema")
                if isinstance(artifact, dict) else None,
                "asset_grain_enabled": bool(getattr(
                    self.config, "loss_veto_asset_grain_enabled", True)),
            })
        except Exception:
            return

    def place_bracket(self, asset: str, side: str, risk_usd: float,
                      entry_ref: float, stop_price: float,
                      ladder=DEFAULT_LADDER, entry_type: str = "market",
                      coherence: float | None = None,
                      formation: dict | None = None,
                      signal_meta: dict | None = None) -> BracketResult:
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
        # Post-win same-direction cooloff (2026-11-15 cluster killer):
        # a fresh qualifying win locks the (class, side) lane for the TTL.
        # Outright block — pwr's $15 respend cap still ALLOWED the $7-risk
        # salvo tickets this latch exists to kill.
        cool_reason, cool_detail = pwc.cooloff_verdict(
            bool(getattr(self.config, "post_win_cooloff_enabled", False)),
            self._cooloff_latches(), asset, side, time.time())
        if cool_reason:
            latches = self._cooloff_latches()
            pwc.note_cooloff_block(latches, asset, side)
            self._save_cooloff_latches(latches)
            append_event(self.journal, {
                "type": "post_win_cooloff_blocked", "semantic_id": sid,
                **cool_detail,
            })
            return self._reject(sid, cool_reason, **{
                k: v for k, v in cool_detail.items() if k != "reason"})
        # Pyramid law: banked runner → harvest/retouch, never second birth.
        rerisk = ep.runner_rerisk_standdown(
            self.open_brackets(), asset, side,
            enabled=bool(getattr(self.config, "bank_protect_enabled", True)),
            min_bank_usd=float(getattr(
                self.config, "bank_protect_min_usd", 20.0) or 20.0))
        if rerisk:
            return self._reject(sid, rerisk)
        veto = self._loss_veto(asset, fam, side)
        if veto:
            _vkey = (f"{asset}|{side}" if bool(getattr(
                self.config, "loss_veto_asset_grain_enabled", True))
                else f"{fam}|{side}")
            return self._reject(
                sid, f"loss veto {_vkey}: n={veto['n']} "
                     f"shrunk_exp=${veto['shrunk_expectancy']:.2f}")
        # H2 coh stamp invariant — refuse dishonest / unit-stain coh.
        coh_reason = ep.coh_stamp_verdict(
            coherence, bool(getattr(
                self.config, "coh_stamp_invariant_enabled", True)))
        if coh_reason:
            return self._reject(sid, coh_reason)
        # ATS-103: crypto fire floor — mainly high-coh crypto executes
        # (and then clears coh_big → good size). xyz: stocks never bind.
        fire_reason = ep.crypto_min_coh_fire_verdict(
            coherence, asset,
            enabled=bool(getattr(
                self.config, "crypto_min_coh_fire_enabled", True)),
            min_coh=float(getattr(
                self.config, "crypto_min_coh_fire", 7.5) or 0.0))
        if fire_reason:
            return self._reject(sid, fire_reason,
                                coherence=coherence,
                                min_coh=float(getattr(
                                    self.config, "crypto_min_coh_fire", 7.5)
                                    or 7.5))
        # Governor 2026-11-15: tradfi fire floor — the sub-4 stock churn
        # loop (GOLD x24 re-entries) was the turbo2 bleed. 0.0 -> legacy.
        tfire_reason = ep.tradfi_min_coh_fire_verdict(
            coherence, asset,
            enabled=bool(getattr(
                self.config, "tradfi_min_coh_fire_enabled", True)),
            min_coh=float(getattr(
                self.config, "tradfi_min_coh_fire", 0.0) or 0.0))
        if tfire_reason:
            return self._reject(sid, tfire_reason,
                                coherence=coherence,
                                min_coh=float(getattr(
                                    self.config, "tradfi_min_coh_fire", 0.0)
                                    or 0.0))

        entry = Decimal(str(entry_ref))
        stop = Decimal(str(stop_price))
        # Widen-only BIRTH stop floor (2026-11-19): max(pct×mark,
        # atr_mult×ATR). Crypto 0.75% / tradfi 0.50%; ATR dark → pct.
        # Constant risk_usd shrinks notional. Kill PROP_STOP_FLOOR=0.
        if bool(getattr(self.config, "stop_floor_enabled", True)):
            _atr = None
            if signal_meta:
                _atr = signal_meta.get("atr") or signal_meta.get("atr_abs")
            _new_stop, _floored, _sf_detail = sf.stop_floor_verdict(
                float(stop), float(entry), side,
                mark=float(entry),
                atr=_atr,
                atr_mult=float(getattr(
                    self.config, "stop_floor_atr_mult", 1.0) or 1.0),
                asset_class=str(getattr(
                    self.config, "asset_class", "all") or "all"),
                crypto_pct=float(getattr(
                    self.config, "stop_floor_pct_crypto", 0.75) or 0.75),
                tradfi_pct=float(getattr(
                    self.config, "stop_floor_pct_tradfi", 0.50) or 0.50),
            )
            if _floored:
                stop = Decimal(str(_new_stop))
                stop_price = float(stop)
                append_event(self.journal, {
                    "type": "stop_floored", "semantic_id": sid,
                    "asset": asset, "side": side, **{
                        k: v for k, v in _sf_detail.items()
                        if k not in ("floored",)},
                })
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
        # Governor floor (2026-11-14): tradfi eval entries carry a minimum
        # notional so the challenge book trades meaningful size. Sub-floor
        # sizes bump up; risk scales with stop distance and is committed
        # honestly below. Kill: PROP_EVAL_TRADFI_MIN_NOTIONAL_USD=0.
        min_notional = float(getattr(
            self.config, "eval_tradfi_min_notional_usd", 0.0) or 0.0)
        if (min_notional > 0
                and str(getattr(self.config, "asset_class", "all")
                        or "all") == "tradfi"):
            floor_qty = round_qty(Decimal(str(min_notional)) / entry, decimals)
            if quantity < floor_qty:
                if floor_qty < min_qty:
                    return self._reject(
                        sid, f"floor qty {floor_qty} below venue min {min_qty}")
                quantity = floor_qty
                risk_usd = float(quantity * stop_dist)
                if risk_usd > cap:
                    return self._reject(
                        sid, f"floor-bumped risk ${risk_usd:.2f} > cap ${cap:.2f}")
                append_event(self.journal, {
                    "type": "tradfi_min_notional_bump", "semantic_id": sid,
                    "asset": asset, "side": side,
                    "min_notional_usd": min_notional,
                    "risk_usd": round(risk_usd, 4),
                })
        # Ghost-150k instrument (2026-11-14): ALL-asset min-notional floor —
        # the demo sleeve places real $10k+ fills at the proven edge points
        # so the journal measures avg return at large capital BEFORE the
        # account purchase. Same honest-risk contract as the tradfi floor:
        # bumped risk is recomputed, committed below, re-checked vs cap.
        # Kill: PROP_GHOST_MIN_NOTIONAL_USD=0.
        ghost_floor = float(getattr(
            self.config, "ghost_min_notional_usd", 0.0) or 0.0)
        if ghost_floor > 0 and float(quantity * entry) < ghost_floor:
            ghost_qty = round_qty(Decimal(str(ghost_floor)) / entry, decimals)
            if ghost_qty < min_qty:
                return self._reject(
                    sid, f"ghost floor qty {ghost_qty} below venue min {min_qty}")
            quantity = ghost_qty
            risk_usd = float(quantity * stop_dist)
            if risk_usd > cap:
                return self._reject(
                    sid, f"ghost-bumped risk ${risk_usd:.2f} > cap ${cap:.2f}")
            append_event(self.journal, {
                "type": "ghost_min_notional_bump", "semantic_id": sid,
                "asset": asset, "side": side,
                "ghost_min_notional_usd": ghost_floor,
                "risk_usd": round(risk_usd, 4),
            })
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

        maker_meta: dict[str, Any] = {}
        maker_used = (entry_type == "market" and bool(getattr(
            self.config, "maker_entry_enabled", False)))
        if maker_used:
            # Maker-first (fee attack): limit entry, fill-verify, then
            # protection. Single-batch limit WITHOUT fill-verify is the
            # 09-28 late-fill orphan class — resting conditionals on an
            # unfilled entry get reconciled closed, then fill naked.
            body = build_bracket(
                account_id=self.client.account_id, asset=asset, side=side,
                quantity=str(quantity), entry_ref=str(entry),
                stop_price=str(stop), ts_ms=ts_ms, quote=self.config.quote,
                entry_type="limit", ladder=ladder,
                fee_rate=str(self.config.taker_fee_rate),
            )
            placed, maker_meta = self._place_maker_entry(
                body, asset=asset, side=side, quantity=quantity,
                entry=entry, stop=stop, ts_ms=ts_ms, ladder=ladder,
                risk_usd=risk_usd, sid=sid)
            if placed is None:
                return BracketResult(ok=False, semantic_id=sid,
                                     reason=str(maker_meta.get(
                                         "reason", "maker_entry_failed")))
            body = maker_meta["body"]
        else:
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
                # 4xx reject (429/geometry): the batch does NOT exist venue-side.
                # Release the committed risk or the phantom latches the day budget.
                self.budget.release(risk_usd, 0.0)
                append_event(self.journal, {
                    "type": "bracket_place_rejected", "semantic_id": sid,
                    "asset": asset, "side": side,
                    "error": str(exc), "status_code": exc.status_code,
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
            **({"formation_unit": formation.get("unit"),
                "edge_point": formation.get("edge_point")}
               if formation else {}),
            "order_ids": [o.get("orderId") for o in placed],
            "intents": [o["intentId"] for o in body["orders"]],
            "opened_ts": time.time(),
            "absence_count": 0,
            "status": "open",
            **({"entry_type": maker_meta.get("entry_type"),
                "entry_fill_s": round(float(maker_meta["fill_s"]), 2)}
               if maker_meta else {}),
            # WS-A additive stamp (hole d): envelope v2 ids ride the row
            # so journal joins and re-entry latches see the signal epoch.
            # Epoch-less legacy rows stay valid (keys absent).
            **({k: signal_meta[k]
                for k in ("signal_id", "signal_ts", "epoch")
                if signal_meta and signal_meta.get(k) is not None}),
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
            **({"entry_type": maker_meta.get("entry_type"),
                "entry_fill_s": round(float(maker_meta["fill_s"]), 2)}
               if maker_meta else {}),
            **({"formation_unit": formation.get("unit"),
                "edge_point": formation.get("edge_point")}
               if formation else {}),
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
            if ok is None:
                return False  # act-backoff defer — no size stamp
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
            if self.protect_realized_bank(bracket, position,
                                          defer_to_arbiter=True):
                pass
            return True
        frac_remaining = float(venue_qty / reg_qty)
        already = float(bracket.get("risk_released", 0.0))
        release = bracket["risk_usd"] * (1.0 - frac_remaining) - already
        realized_delta = booked_src - prev_booked
        if release > 0 and self.budget.state is not None:
            self.budget.release(release, max(0.0, realized_delta))
            bracket["risk_released"] = already + release
            # WHY: this shrink path already releases positive partials — the
            # close site subtracts this accumulator so the signed realized
            # leg is never double-counted (2026-11-17 Build 3).
            if bool(getattr(self.config, "budget_realized_enabled", True)):
                prev_rel = float(bracket.get("budget_realized_released", 0.0)
                                 or 0.0)
                bracket["budget_realized_released"] = (
                    prev_rel + max(0.0, realized_delta))
        # Always stamp booked on shrink (budget gate must not blind
        # bank_protect when budget.state is briefly None).
        bracket["realized_booked"] = booked_src
        ok = self.resize_protection(bracket, venue_qty,
                                    str(position.get("positionId") or ""))
        if ok is None:
            return False  # act-backoff defer — no size stamp
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
        if self.protect_realized_bank(bracket, position,
                                      defer_to_arbiter=True):
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

    def protect_realized_bank(self, bracket: dict, position: dict,
                              defer_to_arbiter: bool = False) -> bool:
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
        if not self._early_tighten_ok(bracket, "bank"):
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
        # Arbiter discipline (audit 2026-11-17): sync_bracket_size calls us
        # BEFORE the reconcile loop opens the arbiter window — a direct
        # resize here would be a SECOND venue call outside the one-resize-
        # per-tick rule. Callers inside that pre-window path pass
        # defer_to_arbiter=True: we skip the resize and the in-window
        # protect_realized_bank call later in the same reconcile iteration
        # votes the same floor (criteria are state-based and re-derive).
        # Standalone callers (tests, tools) keep the legacy direct path.
        if defer_to_arbiter and self._arbiter_on():
            return False
        ok = self._stop_resize(
            bracket, venue_qty, str(position.get("positionId") or ""),
            str(target), stage="bank")
        if ok is None or ok == "held":
            return False
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
        if not self._early_tighten_ok(bracket, "breakeven"):
            return False
        entry = bracket["entry_ref"]
        venue_qty = Decimal(str(position.get("quantity") or "0"))
        if venue_qty <= 0:
            return False
        ok = self._stop_resize(
            bracket, venue_qty, str(position.get("positionId") or ""),
            str(entry), stage="breakeven")
        if ok is None or ok == "held":
            return False
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
        if not self._early_tighten_ok(bracket, "breakeven_mark"):
            return False
        sid = bracket.get("semantic_id", "")
        entry = bracket["entry_ref"]
        venue_qty = Decimal(str(position.get("quantity") or "0"))
        if venue_qty <= 0:
            return False
        ok = self._stop_resize(
            bracket, venue_qty, str(position.get("positionId") or ""),
            str(entry), stage="breakeven_mark")
        if ok is None or ok == "held":
            return False
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
                      mark: float | None,
                      state: dict | None = None) -> bool:
        """HL-mark trailing (spec §3.8 build 2): rungs breakeven+fees @1R,
        lock 1R @2R, tighten-only. Shared mfe_peak_r ≥ eval_be_arm_r floor
        before any rung (BTC #2 birth-trail class). mark=None (stale/
        absent HL) -> no-op, the uPnL breakeven arm stays the only trigger
        — fail-open bit-for-bit. The mark drives the trigger ONLY; risk
        math never sees it. Call after rebase_entry_from_venue.

        Offense compose (ATS-198): after early_tighten, optionally require
        Mid MFE proof via offense_state.trail_after_mfe_ok. Kill
        PROP_OFFENSE_TRAIL_AFTER_MFE=0 → legacy. Never edits early_tighten.
        """
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
        if not self._early_tighten_ok(bracket, "trail"):
            return False
        if (bool(getattr(self.config, "offense_state_enabled", True))
                and bool(getattr(
                    self.config, "offense_trail_after_mfe_enabled", True))):
            _ok_t, _det = ofs.trail_after_mfe_ok(
                bracket, state,
                enabled=True,
                arm_r=float(getattr(self.config, "eval_be_arm_r", 0.5)
                            or 0.5))
            if not _ok_t:
                now = time.time()
                last = float(bracket.get("_offense_trail_log_ts") or 0.0)
                if now - last >= 60.0:
                    bracket["_offense_trail_log_ts"] = now
                    append_event(self.journal, {
                        "type": "offense_trail_blocked",
                        "semantic_id": sid,
                        "asset": bracket.get("asset"),
                        **{k: v for k, v in _det.items() if v is not None},
                    })
                return False
        venue_qty = Decimal(str(position.get("quantity") or "0"))
        if venue_qty <= 0:
            return False
        ok = self._stop_resize(
            bracket, venue_qty, str(position.get("positionId") or ""),
            str(target), stage="trail")
        if ok is None or ok == "held":
            return False  # backoff defer / arbiter hold — no spam
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

    def track_excursions(self, bracket: dict, mark: float | None) -> bool:
        """Per-tick MFE peak + MAE trough ratchet (2026-11-17).

        WHY: the M3 shield-q measurement needs the close journal to carry
        mfe_peak_r AND mae_trough_r; the trough is lower-only, first mark
        seeds it. Kill OFF → False, bracket untouched (legacy bit-for-bit).
        True when the bracket mutated (registry must flush)."""
        if not bool(getattr(self.config, "mae_tracker_enabled", True)):
            return False
        if mark is None or bracket.get("status") != "open":
            return False
        prev_peak = bracket.get("mfe_peak_r")
        prev_trough = bracket.get("mae_trough_r")
        ep.ratchet_mfe_peak_r(bracket, mark)
        ep.ratchet_mae_trough_r(bracket, mark)
        return (bracket.get("mfe_peak_r") != prev_peak
                or bracket.get("mae_trough_r") != prev_trough)

    def track_rebound(self, bracket: dict, mark: float | None) -> bool:
        """Underwater-recovery cycle tracker (2026-11-17).

        WHY: coins that dip and recover repeatedly are showing defended
        levels; the cycle count rides the registry row into the close
        journal so the cohort planes (loss_veto / edge_boost) can price
        mean-reversion resilience. Kill OFF → False, bracket untouched.
        True when the bracket mutated (registry must flush)."""
        if not bool(getattr(self.config, "rebound_tracker_enabled", True)):
            return False
        if mark is None or bracket.get("status") != "open":
            return False
        mutated, recovered = rb.track_rebound(
            bracket, mark,
            arm_r=float(getattr(self.config, "rebound_arm_r", 0.5) or 0.5),
            recover_r=float(getattr(
                self.config, "rebound_recover_r", 0.0) or 0.0))
        if recovered:
            append_event(self.journal, {
                "type": "rebound_recovered",
                "semantic_id": bracket.get("semantic_id"),
                "asset": bracket.get("asset"),
                "side": bracket.get("side"),
                "rebound_count": bracket.get("rebound_count"),
                "mark": mark,
                "mfe_peak_r": bracket.get("mfe_peak_r"),
                "mae_trough_r": bracket.get("mae_trough_r"),
            })
        return mutated

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
        if not self._early_tighten_ok(bracket, "mfe_sharpen"):
            return False
        # Rebound suppression (2026-11-17): a rebound-qualified bracket
        # currently UNDERWATER may not be sharpened past entry into the
        # dip it historically recovers from. Decline-only — never loosens.
        # Kill PROP_REBOUND_SHARPEN_GUARD=0 → legacy sharpen bit-for-bit.
        if (bool(getattr(self.config, "rebound_sharpen_guard_enabled", True))
                and rb.sharpen_suppressed(
                    bracket, mark, target,
                    min_count=int(getattr(
                        self.config, "rebound_min_count", 2) or 2))):
            # Throttled (audit 2026-11-17): this fires every tick while a
            # qualified bracket sits underwater with a past-entry target —
            # ~240 events/hr/bracket unthrottled.
            now = time.time()
            ttl = float(getattr(
                self.config, "rebound_suppress_log_ttl_s", 300.0) or 300.0)
            last = bracket.get("_rebound_suppress_log_ts") or 0.0
            if now - float(last) >= ttl:
                bracket["_rebound_suppress_log_ts"] = now
                append_event(self.journal, {
                    "type": "rebound_sharpen_suppressed",
                    "semantic_id": bracket.get("semantic_id"),
                    "asset": bracket.get("asset"),
                    "side": bracket.get("side"),
                    "mark": mark, "target": str(target),
                    "previous_stop": bracket.get("current_stop"),
                    "rebound_count": bracket.get("rebound_count"),
                })
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
        ok = self._stop_resize(
            bracket, venue_qty, str(position.get("positionId") or ""),
            str(target), stage="mfe_sharpen")
        if ok is None or ok == "held":
            return False  # backoff defer / arbiter hold — no failed spam
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

    def lock_live_peak(self, bracket: dict, position: dict,
                       mark: float | None) -> bool:
        """Live-USD peak lock (PROP_LIVE_PEAK_LOCK, Governor 2026-11-15).

        The birth-R exit stack is quantity-blind: after tap_reload grows
        the position, mfe_peak_r stays frozen below the rungs while the
        REAL peak USD (venue uPnL) is multiples of birth risk. This locks
        rung-fractions of the live peak off the CURRENT venue quantity.
        Kill OFF (default) → always False, zero state writes."""
        if not bool(getattr(self.config, "live_peak_lock_enabled", False)):
            return False
        if self.config.profile != "evaluation" or mark is None:
            return False
        if not isinstance(position, dict):
            return False
        sid = bracket.get("semantic_id", "")
        venue_qty = Decimal(str(position.get("quantity") or "0"))
        if venue_qty <= 0:
            return False
        try:
            _, _dec = spec_for(bracket["asset"])
            if round_qty(venue_qty, _dec) <= 0:
                return False  # dust qty — resize no-ops; stay silent
        except Exception:
            return False
        target = ep.live_peak_lock_verdict(
            self.config.profile, bracket, mark, venue_qty,
            position.get("unrealizedPnl"),
            enabled=True,
            min_floor_usd=float(getattr(
                self.config, "live_peak_lock_min_floor_usd", 20.0) or 20.0))
        if target is None:
            return False
        if not self._early_tighten_ok(bracket, "live_peak"):
            return False
        ok = self._stop_resize(
            bracket, venue_qty, str(position.get("positionId") or ""),
            str(target), stage="live_peak")
        if ok is None or ok == "held":
            return False  # backoff defer / arbiter hold — no failed spam
        append_event(self.journal, {
            "type": "live_peak_locked" if ok else "live_peak_lock_failed",
            "semantic_id": sid, "asset": bracket.get("asset"),
            "mark": mark, "target": str(target),
            "previous_stop": bracket.get("current_stop"),
            "mfe_peak_usd_live": bracket.get("mfe_peak_usd_live"),
            "live_upnl": position.get("unrealizedPnl"),
            "venue_qty": str(venue_qty),
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

    def harvest_offense_partial(
            self, bracket: dict, position: dict,
            state: dict | None = None) -> bool:
        """State-gated R-rung qty partial (ATS-197 right-tail bank).

        Additive — does not edit Bank-MFE peak/partial stop writers.
        Kill PROP_OFFENSE_PARTIAL=0 / PROP_OFFENSE_STATE=0 → no-op.
        Sticky apex stands neighbour down under twin_hot.
        """
        if not (bool(getattr(self.config, "offense_state_enabled", True))
                and bool(getattr(self.config, "offense_partial_enabled", True))):
            return False
        if self.config.profile != "evaluation":
            return False
        if not isinstance(position, dict) or not isinstance(bracket, dict):
            return False
        is_apex = self._offense_apex(state, bracket)
        plan = ofs.r_partial_verdict(
            bracket, state,
            enabled=True,
            rungs=getattr(self.config, "offense_partial_rungs_r", ""),
            is_apex_seat=is_apex,
            sticky_apex_enabled=bool(getattr(
                self.config, "offense_sticky_apex_enabled", True)))
        if plan is None:
            return False
        reduce_qty = float(plan["reduce_qty"])
        if reduce_qty <= 0:
            return False
        sid = bracket.get("semantic_id", "")
        try:
            self.client.reduce_position(bracket["asset"], reduce_qty)
        except Exception as exc:
            append_event(self.journal, {
                "type": "offense_partial_failed",
                "semantic_id": sid, "asset": bracket.get("asset"),
                "trigger_r": plan["trigger_r"], "frac": plan["frac"],
                "reduce_qty": reduce_qty, "error": str(exc)[:160],
            })
            return False
        paid = list(bracket.get("offense_partial_rungs") or [])
        paid.append(float(plan["trigger_r"]))
        bracket["offense_partial_rungs"] = paid
        append_event(self.journal, {
            "type": "offense_partial_banked",
            "semantic_id": sid, "asset": bracket.get("asset"),
            "side": bracket.get("side"),
            "trigger_r": plan["trigger_r"], "frac": plan["frac"],
            "reduce_qty": reduce_qty,
            "mfe_peak_r": plan.get("mfe_peak_r"),
            "rung_id": plan.get("rung_id"),
            "phase": plan.get("phase"),
            "scrape_day": plan.get("scrape_day"),
            "apex": is_apex,
        })
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
        if not self._early_tighten_ok(bracket, "runner_harvest"):
            return False
        sid = bracket.get("semantic_id", "")
        venue_qty = Decimal(str(position.get("quantity") or "0"))
        if venue_qty <= 0:
            return False
        try:
            _, _dec = spec_for(bracket["asset"])
            if round_qty(venue_qty, _dec) <= 0:
                return False  # dust qty — resize no-ops; stay silent
        except Exception:
            return False
        ok = self._stop_resize(
            bracket, venue_qty, str(position.get("positionId") or ""),
            str(target), stage="runner_harvest")
        if ok is None or ok == "held":
            return False  # backoff defer / arbiter hold — no failed spam
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
                          position_id: str, trigger_override: str | None = None
                          ) -> Optional[bool]:
        """Re-place the stop at post-fill size. Place FIRST, cancel the
        oversized stop second — the position is never without a stop.
        trigger_override lets the breakeven/reload paths re-anchor the
        stop in the same atomic pass (tighten-only is the caller's job).

        Returns True/False for success/hard-fail. Returns None when
        act-backoff defers (callers must NOT journal *_failed / stamp)."""
        asset = bracket["asset"]
        side = bracket["side"]
        sid = bracket.get("semantic_id", "")
        bkey = ab.resize_key(str(sid or ""))

        def _fail(stage: str, exc: Exception) -> bool:
            if ab.is_rate_limited(exc) and bkey:
                payload = self.act_backoff.note_rate_limit(bkey)
                if payload:
                    append_event(self.journal, {
                        **payload, "semantic_id": sid, "asset": asset,
                        "stage": stage})
            append_event(self.journal, {
                "type": "protection_resize_failed", "semantic_id": sid,
                "asset": asset, "stage": stage, "error": str(exc)})
            return False

        # Flat / dust — nothing to protect (avoids qty≤0 venue rejects).
        try:
            if Decimal(str(venue_qty)) <= 0:
                return False
        except Exception:
            return False

        # 429 act-backoff: skip place while account/key cooling.
        # None = defer (Design A) so callers omit trail_failed spam.
        if not self.act_backoff.allow(bkey):
            if self.act_backoff.should_journal_deferred(bkey):
                append_event(self.journal, {
                    **self.act_backoff.deferred_payload(bkey),
                    "semantic_id": sid, "asset": asset,
                    "path": "resize_protection"})
            return None

        prev_stop = bracket.get("current_stop") or bracket.get("stop_price")
        early_detail: dict[str, Any] = {}
        try:
            _, decimals = spec_for(asset)
            qty = round_qty(Decimal(venue_qty), decimals)
            if qty <= 0:
                return False
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
        if bkey:
            self.act_backoff.note_success(bkey)
        new_ids = {o.get("orderId") for o in placed}
        cancelled: list[str] = []
        try:
            open_orders = self.client.get_open_orders(base=asset)
        except Exception as exc:
            # replacement is live; an oversized twin may linger but the
            # position is protected — report, don't unwind
            if ab.is_rate_limited(exc) and bkey:
                # place already succeeded; don't latch the resize key on
                # a cancel-scan 429 — position is protected.
                append_event(self.journal, {
                    "type": "act_backoff_scan_rate_limited",
                    "semantic_id": sid, "asset": asset,
                    "backoff_key": bkey, "error": str(exc)})
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

    def basket_close_allowed(self, semantic_id: str | None = None) -> bool:
        """FIX-6 seam: True when basket close may hit the venue.

        Call before close_position / cancel_all_open in harvest_basket_tp
        (when that module is present). Book-level key when sid is None.
        Kill OFF → always True.
        """
        key = ab.basket_key(semantic_id)
        if not self.act_backoff.allow(key):
            if self.act_backoff.should_journal_deferred(key):
                append_event(self.journal, {
                    **self.act_backoff.deferred_payload(key),
                    "semantic_id": semantic_id,
                    "path": "basket_close"})
            return False
        return True

    def note_basket_close_error(self, exc: BaseException,
                                semantic_id: str | None = None) -> None:
        """FIX-6 seam: arm backoff after a basket close 429."""
        if not ab.is_rate_limited(exc):
            return
        key = ab.basket_key(semantic_id)
        payload = self.act_backoff.note_rate_limit(key)
        # Also cool the book-level key so the next batch waits.
        if semantic_id:
            book = self.act_backoff.note_rate_limit(ab.basket_key(None))
            if book:
                append_event(self.journal, {
                    **book, "semantic_id": semantic_id, "path": "basket_close"})
        if payload:
            append_event(self.journal, {
                **payload, "semantic_id": semantic_id, "path": "basket_close"})

    def note_basket_close_success(self, semantic_id: str | None = None) -> None:
        """FIX-6 seam: clear per-leg backoff after a successful close."""
        self.act_backoff.note_success(ab.basket_key(semantic_id))

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
