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
from dataclasses import asdict
from pathlib import Path

from .bracket import DEFAULT_LADDER
from .brains import eval_profile as ep
from .brains import kingdom_view as kv
from .brains import loss_stats as ls
from .brains.budget import BudgetBrain
from .brains.drawdown import DrawdownBrain
from .config import PropConfig
from .executor import ProprExecutor
from .families import family_of
from .io.journal import append_event, atomic_write_json, read_json
from .io.hl_marks import HLMarkPlane
from .io.propr_client import ProprClient
from .io.signal_tap import (DEFAULT_REMOTE_OUTBOX, SignalTap,
                            fetch_local, fetch_ssh,
                            parse_crypto_deny_symbols)


def _snapshot_r_moved(side: str, entry: float, stop: float,
                      mark: float | None) -> float | None:
    """Birth-geometry R vs an optional reference mark (HL ref plane only).

    Never enters equity/headroom/risk math — telemetry for ARIA's sniper
    signal layer. ARIA recomputes confirmation from its OWN marks.
    """
    if mark is None or entry <= 0 or stop <= 0 or entry == stop:
        return None
    risk_px = abs(entry - stop)
    if risk_px <= 0:
        return None
    if side == "sell":
        return (entry - mark) / risk_px
    if side == "buy":
        return (mark - entry) / risk_px
    return None


def build_positions_snapshot(brackets: list[dict], account_id: str,
                             sleeve: str, now: float,
                             max_positions: int = 3,
                             mark_fn=None) -> dict:
    """Geometry-only position snapshot for the ARIA prop-trend bridge.

    Contract fields per leg: side, entry, stop, be_armed, r_moved
    (optional — None when no mark). ARIA confirms ≥+1R from its OWN
    mark stores; published r_moved is reference telemetry only (HL may
    feed mark_fn — NEVER into propr equity). Empty positions list is
    meaningful: ARIA disarms when a leg vanishes. max_positions rides
    the snapshot so overflow accounts can verdict peer fullness without
    sharing config. Read-only fascia — never mutates harness policy.
    """
    positions = []
    for b in brackets:
        entry = float(b["entry_ref"])
        stop = float(b["stop_price"])
        side = b["side"]
        mark = None
        if mark_fn is not None:
            try:
                mark = mark_fn(b["asset"])
            except Exception:
                mark = None
            try:
                mark = float(mark) if mark is not None else None
            except (TypeError, ValueError):
                mark = None
            if mark is not None and (mark != mark or mark <= 0):
                mark = None
        coh = b.get("coherence_at_entry", b.get("coherence"))
        try:
            coh_f = float(coh) if coh is not None else None
            if coh_f is not None and coh_f != coh_f:
                coh_f = None
        except (TypeError, ValueError):
            coh_f = None
        positions.append({
            "asset": b["asset"],
            "side": side,
            "entry_ref": entry,
            "stop_price": stop,
            "quantity": float(b["quantity"]),
            "risk_usd": b["risk_usd"],
            "opened_ts": b["opened_ts"],
            "be_armed": bool(b.get("be_armed")),
            "r_moved": _snapshot_r_moved(side, entry, stop, mark),
            # Evidence for PropTrendBridge big-bet gate (probe squat class).
            "coherence_at_entry": coh_f,
        })
    return {
        "ts": now,
        "account_id": account_id,
        "sleeve": sleeve,
        "max_positions": max_positions,
        "positions": positions,
    }


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
        is_eval = config.profile == "evaluation"
        self.budget = BudgetBrain(
            root / "accounts" / "acct1" / "budget_day.json",
            hard_zero=config.hard_zero_headroom_usd,
            divisor=config.budget_divisor,
            floor_budget=(config.eval_daily_budget_usd if is_eval
                          else config.budget_floor_usd),
            r_base=config.r_base_usd,
            r_max=config.r_max_usd,
        )
        self.executor = ProprExecutor(config, self.client, self.budget, self.drawdown)
        self.journal = config.journal_path
        self.algedonic_path = root / "program" / "algedonic.json"
        # H4 day-budget latch (SOL stack) — persists across ticks; clears
        # on UTC budget.roll. Kill OFF → path never arms.
        self.day_latch_path = root / "accounts" / "acct1" / "day_latch.json"
        self._day_latch = read_json(self.day_latch_path, {}) or {}
        self.tap = (SignalTap(root / "cursor.json", ttl_s=config.signal_ttl_s,
                              asset_class=config.asset_class,
                              families=(frozenset(f.strip() for f in
                                                  config.families.split(",")
                                                  if f.strip())
                                        or None),
                              # §7 S3: capital_block actionable when surplus
                              # on; risk_block always dropped. Unlabeled
                              # omit = legacy aria_exec (fail-open). v1 does
                              # NOT wait on KingdomView thesis_reserve.
                              capital_surplus_enabled=config.capital_surplus_enabled,
                              # Graph 7: sleeve-aware equity TTL (Monday RTH).
                              tradfi_ttl_s=config.tradfi_signal_ttl_s,
                              # ATS-102: turbo crypto toxic cohort only.
                              crypto_deny_enabled=config.crypto_deny_enabled,
                              crypto_deny_symbols=parse_crypto_deny_symbols(
                                  config.crypto_deny_symbols
                                  if config.crypto_deny_symbols else None))
                    if config.outbox_enabled else None)
        self.hl_marks = (HLMarkPlane(stale_s=config.hl_marks_stale_s)
                         if config.hl_marks_enabled else None)
        self._hl_err_ts = 0.0
        self._hl_live_logged = False
        self._next: dict[str, float] = {}
        self._boot_replay_event: dict | None = None
        # Tradfi owner hot-cursor (Graph 7): rewind once at construct so the
        # first poll rescans in-TTL xyz: rows. Crypto/all sleeves leave the
        # cursor alone (PROP_TAP_BOOT_REPLAY=0 kills even tradfi).
        if (self.tap is not None and config.tap_boot_replay
                and config.asset_class == "tradfi"):
            self._boot_replay_event = self.tap.arm_boot_replay()

    def _due(self, name: str, every: float) -> bool:
        now = time.time()
        if now >= self._next.get(name, 0.0):
            self._next[name] = now + every
            return True
        return False

    def _day_latched(self) -> bool:
        if not bool(getattr(self.config, "day_budget_latch_enabled", True)):
            return False
        latch = self._day_latch or {}
        if not latch.get("latched"):
            return False
        day = latch.get("day")
        if self.budget.state and day and day != self.budget.state.day:
            return False  # stale pre-roll residue; clear path owns write
        return True

    def _arm_day_latch(self, reason: str, **extra) -> bool:
        """Persist H4 latch for the current UTC day. True if newly armed."""
        if not bool(getattr(self.config, "day_budget_latch_enabled", True)):
            return False
        day = (self.budget.state.day if self.budget.state
               else self.budget.utc_day())
        if self._day_latch.get("latched") and self._day_latch.get("day") == day:
            return False
        self._day_latch = {
            "latched": True, "day": day, "reason": reason,
            "armed_at": time.time(), **extra,
        }
        atomic_write_json(self.day_latch_path, self._day_latch)
        append_event(self.journal, {
            "type": "day_budget_latched", "day": day, "reason": reason,
            **{k: v for k, v in extra.items() if v is not None},
        })
        return True

    def _clear_day_latch(self, why: str = "budget_roll") -> None:
        if not self._day_latch.get("latched"):
            self._day_latch = {}
            return
        prev = dict(self._day_latch)
        self._day_latch = {}
        atomic_write_json(self.day_latch_path, self._day_latch)
        append_event(self.journal, {
            "type": "day_budget_latch_cleared", "why": why,
            "prev_reason": prev.get("reason"), "prev_day": prev.get("day"),
        })

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
        positions_list = self.client.get_positions(status="open")
        positions = {p["asset"] for p in positions_list}
        by_asset = {p["asset"]: p for p in positions_list}
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
                pos = by_asset.get(bracket["asset"])
                # Stamp uPnL for co-move underwater-flip shadow (book truth).
                if isinstance(pos, dict):
                    try:
                        upnl = float(pos.get("unrealizedPnl"))
                    except (TypeError, ValueError):
                        upnl = None
                    if upnl is not None and upnl == upnl:
                        prev = bracket.get("last_upnl")
                        bracket["last_upnl"] = upnl
                        # Raise-only peak_upnl for basket tax-bank graph.
                        try:
                            prev_peak = float(bracket.get("peak_upnl_usd") or 0.0)
                        except (TypeError, ValueError):
                            prev_peak = 0.0
                        if upnl > prev_peak:
                            bracket["peak_upnl_usd"] = upnl
                            changed = True
                        if upnl < 0:
                            if not bracket.get("upnl_negative_since"):
                                bracket["upnl_negative_since"] = time.time()
                                changed = True
                        elif bracket.get("upnl_negative_since") is not None:
                            bracket.pop("upnl_negative_since", None)
                            changed = True
                        if prev != upnl:
                            changed = True
                trades_realized = None
                if self.config.protection_resize_enabled and isinstance(pos, dict):
                    # TP partial PnL often lives on trades, not open
                    # position.realizedPnl — feed bank_protect honestly.
                    try:
                        trades_realized = ls.close_pnl_from_trades(
                            self.client.get_trades(base=bracket["asset"]),
                            float(bracket.get("opened_ts", 0.0) or 0.0))
                    except Exception:
                        trades_realized = None
                    if self.executor.sync_bracket_size(
                            bracket, pos, trades_realized=trades_realized):
                        changed = True
                if self.config.profile == "evaluation":
                    # Venue fill owns R math — rebase signal entry_ref
                    # before BE/trail/MFE (BTC #2: 74507 signal vs 75190
                    # fill → false +2.6R birth trail → −$4.21).
                    if isinstance(pos, dict) and self.executor.rebase_entry_from_venue(
                            bracket, pos):
                        changed = True
                    # Bank protect first when partials OR residual/MFE
                    # criteria met (Dayo 75% @ ~$20 — bank never returns;
                    # GOLD 4.62R latch-only when already past entry).
                    if self.executor.protect_realized_bank(bracket, pos):
                        changed = True
                    if self.executor.arm_breakeven(bracket, pos):
                        changed = True
                    if self.hl_marks is not None and isinstance(pos, dict):
                        mark = self.hl_marks.mark(bracket["asset"])
                        # H1: mark-plane BE before trail (GOLD/uPnL-dark).
                        # Shared mfe_peak_r ≥ eval_be_arm_r (~0.5) floor.
                        if self.executor.arm_breakeven_from_mark(
                                bracket, pos, mark):
                            changed = True
                        if self.executor.trail_bracket(bracket, pos, mark):
                            changed = True
                        elif self.executor.heal_be_armed_stamp(bracket):
                            changed = True
                        # Prop MFE sharpen AFTER trail/BE (peak→floor stop).
                        peak_before = bracket.get("mfe_peak_r")
                        if self.executor.sharpen_mfe_bracket(
                                bracket, pos, mark):
                            changed = True
                        elif bracket.get("mfe_peak_r") != peak_before:
                            changed = True  # peak ratchet only
                        # Pyramid/scalper harvest on banked runner (retouch).
                        if self.executor.harvest_runner_bank_floor(
                                bracket, pos, mark):
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
                bracket["closed_ts"] = time.time()
                # P0 outcome stamp: CohortStats skips rows without family
                # or realized_pnl_final (loss_stats.py). Place stamps
                # family; legacy/adopted closes backfill from asset so
                # demo→turbo veto can bind once PnL lands.
                final_pnl = None
                try:
                    final_pnl = ls.close_pnl_from_trades(
                        self.client.get_trades(base=bracket["asset"]),
                        float(bracket.get("opened_ts", 0.0)))
                except Exception:
                    pass
                ls.stamp_close_outcome(
                    bracket,
                    family=(None if bracket.get("family")
                            else family_of(bracket["asset"])),
                    realized_pnl_final=final_pnl,
                    coherence_at_entry=bracket.get(
                        "coherence_at_entry", bracket.get("coherence")),
                )
                remaining_risk = bracket["risk_usd"] - float(
                    bracket.get("risk_released", 0.0))
                self.budget.release(max(0.0, remaining_risk), 0.0)
                # H4: arm day latch on fast big-bet cold loss (SOL class).
                hold_s = None
                try:
                    hold_s = float(bracket["closed_ts"]) - float(
                        bracket.get("opened_ts", 0.0) or 0.0)
                except (TypeError, ValueError):
                    hold_s = None
                latch_reason = ep.day_budget_latch_arm_verdict(
                    self.config.profile,
                    bool(getattr(self.config, "day_budget_latch_enabled",
                                 True)),
                    coherence_at_entry=bracket.get("coherence_at_entry",
                                                   bracket.get("coherence")),
                    risk_usd=bracket.get("risk_usd"),
                    realized_pnl=bracket.get("realized_pnl_final"),
                    hold_s=hold_s,
                    coh_big=float(getattr(self.config, "eval_coh_big", 7.0)
                                  or 7.0),
                    loss_r=float(getattr(
                        self.config, "day_budget_latch_loss_r", -0.7)
                        or -0.7),
                    t_fast_s=float(getattr(
                        self.config, "day_budget_latch_t_fast_s", 300.0)
                        or 300.0),
                    spent_threshold=float(getattr(
                        self.config, "day_budget_latch_spent_threshold", 0.70)
                        or 0.70),
                )
                if latch_reason:
                    self._arm_day_latch(
                        latch_reason,
                        semantic_id=bracket.get("semantic_id"),
                        asset=bracket.get("asset"),
                        hold_s=hold_s,
                        realized_pnl_final=bracket.get("realized_pnl_final"),
                        risk_usd=bracket.get("risk_usd"),
                        coherence_at_entry=bracket.get("coherence_at_entry"),
                    )
                # Post-win re-spend latch (BTC right-tail → SOL/NEAR class).
                self.executor.arm_post_win_respend(bracket)
                append_event(self.journal, {
                    "type": "reconcile_closed_external",
                    "semantic_id": bracket["semantic_id"],
                    "family": bracket.get("family"),
                    "side": bracket.get("side"),
                    "realized_pnl_final": bracket.get("realized_pnl_final"),
                    "risk_usd": bracket.get("risk_usd"),
                    "coherence_at_entry": bracket.get("coherence_at_entry"),
                })
        if changed:
            atomic_write_json(reg_path, reg)
        # Basket TP peak-push conquest: after per-leg BE/trail/sharpen,
        # graph controller harvests at border / new basket % peak / trail
        # giveback. Uses last_upnl (+ peak_upnl ratchet) stamped above.
        if (self.config.profile == "evaluation"
                and bool(getattr(self.config, "basket_tp_enabled", True))):
            try:
                if self.executor.harvest_basket_tp(reg.get("brackets")):
                    atomic_write_json(reg_path, reg)
            except Exception as exc:
                append_event(self.journal, {
                    "type": "basket_tp_loop_error", "error": str(exc),
                })
        # H4 spent latch while big-bets are open (SOL stack prevention):
        # commit fraction can cross the threshold without a new signal.
        self._maybe_arm_spent_latch()
        self.publish_loss_veto()
        self.publish_positions()

    def _maybe_arm_spent_latch(self) -> None:
        if not bool(getattr(self.config, "day_budget_latch_enabled", True)):
            return
        if self.config.profile != "evaluation" or not self.budget.state:
            return
        if self._day_latched():
            return
        spent = ep.day_budget_spent_frac(
            self.budget.state.budget, self.budget.remaining)
        reason = ep.day_budget_latch_arm_verdict(
            self.config.profile, True, spent_frac=spent,
            spent_threshold=float(getattr(
                self.config, "day_budget_latch_spent_threshold", 0.70)
                or 0.70))
        if reason:
            self._arm_day_latch(
                reason, spent_frac=spent,
                budget=self.budget.state.budget,
                remaining=self.budget.remaining)

    def publish_loss_veto(self) -> None:
        """Publisher side of Build C: cohort stats from this account's
        closed brackets -> loss_veto.json. Consumer accounts (turbo, which
        sets PROP_LOSS_VETO_PATH at the demo's artifact) never publish.

        Heals legacy closed_external rows missing family / final PnL so a
        cold registry lights the artifact on the next publish cycle
        without waiting for a fresh close.
        """
        if not self.config.loss_veto_enabled or self.config.loss_veto_path:
            return
        reg = self.executor._registry()
        closed: list[dict] = []
        healed = False
        for b in reg.get("brackets") or []:
            if b.get("status") != "closed_external":
                continue
            if ls.stamp_close_outcome(
                    b,
                    family=(None if b.get("family")
                            else family_of(b["asset"]) if b.get("asset")
                            else None),
                    coherence_at_entry=b.get(
                        "coherence_at_entry", b.get("coherence")),
            ):
                healed = True
            closed.append(b)
        if healed:
            self.executor._save_registry(reg)
        stats = ls.CohortStats(min_n=self.config.loss_veto_min_n).build(closed)
        atomic_write_json(self.config.loss_veto_artifact,
                          {"ts": time.time(), "stats": stats})

    def publish_positions(self) -> None:
        mark_fn = None
        if self.hl_marks is not None:
            mark_fn = self.hl_marks.mark
        snap = build_positions_snapshot(
            self.executor.open_brackets(), self.client.account_id,
            self.config.asset_class, time.time(), self.config.max_positions,
            mark_fn=mark_fn)
        atomic_write_json(self.config.state_root / "prop_positions.json", snap)

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
        held = next((b for b in self.executor.open_brackets()
                     if b["asset"] == sig["asset"]), None)
        if held is not None:
            # The reload graph edge: fresh same-direction signal on a HELD
            # bracket. Live evidence 2026-11-08: skipped signals were never
            # better-priced — the money class is the matured high-coherence
            # reload into a PROVEN trade, funded from harvested risk only.
            if (held["side"] == sig["side"]
                    and self.config.profile == "evaluation"):
                res = self.executor.reload_bracket(
                    held, sig.get("coherence"), sig["entry_ref"],
                    fresh_stop=sig.get("stop_price"))
                append_event(self.journal, {
                    "type": "tap_reload", "id": sig["id"],
                    "asset": sig["asset"], "side": sig["side"],
                    "coherence": sig.get("coherence"),
                    "ok": res.ok, "reason": res.reason})
                return
            append_event(self.journal, {
                "type": "tap_skip_open", "asset": sig["asset"], "id": sig["id"]})
            return
        if self.config.overflow_only:
            # Overflow account (Governor 2026-11-08): a fresh signal is
            # ours ONLY when every peer covering its class is at max
            # positions. Reloads on brackets we already hold are exempt
            # (the held branch above returned first). KingdomView Phase 1:
            # read shared view OR legacy peer files; stale view → standby.
            now = time.time()
            legacy = [read_json(Path(p.strip()))
                      for p in self.config.overflow_peers.split(",")
                      if p.strip()]
            view = None
            if self.config.kingdom_view_enabled:
                view = kv.read_kingdom_view(
                    self.config.kingdom_view_artifact, read_json=read_json)
            snaps = kv.resolve_overflow_snaps(
                view_enabled=self.config.kingdom_view_enabled,
                view=view, legacy_snaps=legacy, now=now,
                stale_s=self.config.overflow_stale_s,
                own_account_id=str(self.client.account_id or ""))
            if not ep.overflow_verdict(
                    snaps, sig["asset"].startswith("xyz:"), now,
                    self.config.overflow_stale_s):
                append_event(self.journal, {
                    "type": "tap_overflow_standby", "id": sig["id"],
                    "asset": sig["asset"], "side": sig["side"],
                    "coherence": sig.get("coherence")})
                return
        if self.drawdown.state is None:
            return
        ladder = DEFAULT_LADDER
        risk = self.budget.risk_cap(self.drawdown.mult)
        if self.config.profile == "evaluation":
            # H4 spent-threshold arm (pre-place): concentration latch when
            # day budget already mostly consumed — probe-only thereafter.
            if (bool(getattr(self.config, "day_budget_latch_enabled", True))
                    and self.budget.state and not self._day_latched()):
                spent = ep.day_budget_spent_frac(
                    self.budget.state.budget, self.budget.remaining)
                spent_reason = ep.day_budget_latch_arm_verdict(
                    self.config.profile, True, spent_frac=spent,
                    spent_threshold=float(getattr(
                        self.config, "day_budget_latch_spent_threshold", 0.70)
                        or 0.70))
                if spent_reason:
                    self._arm_day_latch(
                        spent_reason, spent_frac=spent,
                        budget=self.budget.state.budget,
                        remaining=self.budget.remaining)
            eval_risk = ep.risk_for_signal(
                self.config.profile, sig.get("coherence"),
                self.budget.remaining, self.drawdown.headroom,
                self.config.hard_zero_headroom_usd, self.drawdown.mult,
                self.config.r_base_usd, self.config.eval_trend_budget_usd,
                self.config.eval_coh_big, self.config.eval_coh_max,
                self.config.eval_risk_frac,
                asset=sig.get("asset"),
                tradfi_min_enabled=bool(getattr(
                    self.config, "eval_tradfi_min_risk_enabled", False)),
                tradfi_min_risk=float(getattr(
                    self.config, "eval_tradfi_min_risk_usd", 7.0) or 7.0),
                crypto_min_enabled=bool(getattr(
                    self.config, "eval_crypto_min_risk_enabled", False)),
                crypto_min_risk=float(getattr(
                    self.config, "eval_crypto_min_risk_usd", 7.0) or 7.0),
                day_latched=self._day_latched())
            if eval_risk is not None:
                risk = eval_risk
            # ATS-96 sleeve coh size bump — slight 1.25–1.5× above measured
            # journal thresholds; day budget / headroom / trend ceiling clamp.
            # Kill PROP_COH_SIZE_BUMP=0 → skip (risk_for_signal bit-for-bit).
            if (bool(getattr(self.config, "coh_size_bump_enabled", True))
                    and risk > 0):
                _pre_bump = float(risk)
                risk, _bump_mult = ep.apply_coherence_size_bump(
                    risk, sig.get("coherence"),
                    enabled=True,
                    asset=sig.get("asset"),
                    tradfi_threshold=float(getattr(
                        self.config, "coh_size_bump_tradfi_threshold", 3.5)
                        or 3.5),
                    tradfi_mult=float(getattr(
                        self.config, "coh_size_bump_tradfi_mult", 1.40)
                        or 1.40),
                    crypto_threshold=float(getattr(
                        self.config, "coh_size_bump_crypto_threshold", 7.5)
                        or 7.5),
                    crypto_mult=float(getattr(
                        self.config, "coh_size_bump_crypto_mult", 1.25)
                        or 1.25),
                    day_latched=self._day_latched(),
                    remaining=self.budget.remaining,
                    headroom=self.drawdown.headroom,
                    hard_zero=self.config.hard_zero_headroom_usd,
                    r_max=self.config.eval_trend_budget_usd)
                if _bump_mult > 1.0 and risk > _pre_bump:
                    append_event(self.journal, {
                        "type": "coherence_size_bumped",
                        "id": sig["id"],
                        "asset": sig["asset"],
                        "side": sig["side"],
                        "coherence": sig.get("coherence"),
                        "risk_pre": _pre_bump,
                        "risk_usd": risk,
                        "bump_mult": _bump_mult,
                        "sleeve": ("tradfi" if str(
                            sig.get("asset") or "").startswith("xyz:")
                            else "crypto"),
                    })
            # Big-bet denser TP ladder (institutional scale-out); probes
            # keep EVAL_LADDER. Crypto + PROP_CRYPTO_TP_TIGHTEN → slight
            # tighter EVAL_*_CRYPTO (turbo rotation). xyz:/kill OFF keep
            # legacy EVAL_LADDER bit-for-bit (turbo2 stocks untouched).
            ladder = ep.ladder_for_signal(
                self.config.profile, sig.get("coherence"),
                big_bet_ladder_enabled=bool(getattr(
                    self.config, "big_bet_ladder_enabled", True)),
                coh_big=float(getattr(self.config, "eval_coh_big", 7.0)
                              or 7.0),
                risk_usd=risk,
                min_risk_usd=float(getattr(
                    self.config, "big_bet_ladder_min_risk_usd", 20.0)
                    or 20.0),
                asset=sig.get("asset"),
                crypto_tp_tighten_enabled=bool(getattr(
                    self.config, "crypto_tp_tighten_enabled", True)))
        if risk <= 0:
            append_event(self.journal, {
                "type": "tap_signal", "id": sig["id"], "asset": sig["asset"],
                "side": sig["side"], "risk_usd": 0.0,
                "coherence": sig.get("coherence"),
                "ok": False, "reason": "risk cap zero"})
            return
        res = self.executor.place_bracket(
            asset=sig["asset"], side=sig["side"], risk_usd=risk,
            entry_ref=sig["entry_ref"], stop_price=sig["stop_price"],
            ladder=ladder, coherence=sig.get("coherence"))
        _bc = sig.get("block_class") or "aria_exec"
        append_event(self.journal, {
            "type": "tap_signal", "id": sig["id"], "asset": sig["asset"],
            "side": sig["side"], "risk_usd": risk, "coherence": sig["coherence"],
            "block_class": _bc,
            "capital_reason": sig.get("capital_reason"),
            "ok": res.ok, "reason": res.reason})
        # §7 census — surplus route journaled before KingdomView (v1).
        if _bc == "aria_capital_block":
            append_event(self.journal, {
                "type": "capital_block_routed", "id": sig["id"],
                "asset": sig["asset"], "side": sig["side"],
                "capital_reason": sig.get("capital_reason"),
                "ok": res.ok, "reason": res.reason})

    def budget_roll(self) -> None:
        if self.drawdown.state is None:
            return
        rolled = self.budget.roll(self.drawdown.headroom)
        # Eval trend-day floor: the day is paying us (realized > 0) -> the
        # $50 ceiling; otherwise $40. Raise-only intraday, never shrinks.
        if self.config.profile == "evaluation" and self.budget.state:
            floor = ep.daily_floor(
                self.config.profile, self.budget.state.realized,
                self.config.eval_daily_budget_usd,
                self.config.eval_trend_budget_usd,
                self.budget.state.budget)
            if floor > self.budget.state.budget:
                self.budget.state.budget = floor
                atomic_write_json(self.budget.path, asdict(self.budget.state))
        if rolled:
            self._clear_day_latch("budget_roll")
            append_event(self.journal, {
                "type": "budget_roll", "day": self.budget.state.day,
                "budget": self.budget.state.budget,
                "headroom": self.budget.state.headroom,
            })

    def hl_poll(self) -> None:
        """Refresh the HL reference plane. Fail-OPEN: errors keep the last
        marks (staleness gate abstains consumers) and journal throttled —
        a dead HL must never become a harness incident."""
        assert self.hl_marks is not None
        try:
            n = self.hl_marks.poll()
            if not self._hl_live_logged:
                self._hl_live_logged = True
                append_event(self.journal, {"type": "hl_marks_live", "n": n})
        except Exception as exc:
            if time.time() - self._hl_err_ts > 300:
                self._hl_err_ts = time.time()
                append_event(self.journal, {
                    "type": "hl_marks_error", "error": str(exc)})

    def heartbeat(self) -> None:
        atomic_write_json(self.config.state_root / "heartbeat.json", {
            "ts": time.time(),
            "account_id": self.client.account_id,
            "enabled": self.config.enabled,
            "dd": self.drawdown.snapshot,
            "budget": self.budget.state.__dict__ if self.budget.state else None,
            "open_brackets": len(self.executor.open_brackets()),
            "tap_offset": self.tap.offset if self.tap else None,
            "hl_marks": (self.hl_marks.snapshot()
                         if self.hl_marks else None),
        })
        self.publish_kingdom_view()

    def publish_kingdom_view(self) -> None:
        """Fascia Law 2: merge ArmyStates + veto + thesis slots → shared JSON.

        Fail-open: write faults journal once and never block the loop.
        Kill: kingdom_view_enabled / kingdom_view_write False.
        """
        if not (self.config.kingdom_view_enabled
                and self.config.kingdom_view_write):
            return
        try:
            paths = kv.peer_paths_for(
                state_root=self.config.state_root,
                kingdom_view_peers=self.config.kingdom_view_peers,
                overflow_peers=self.config.overflow_peers)
            armies, expected = kv.collect_armies(paths, read_json=read_json)
            veto = read_json(self.config.loss_veto_artifact)
            if not isinstance(veto, dict):
                veto = None
            now = time.time()
            view = kv.build_kingdom_view(
                armies, now=now, loss_veto=veto, expected_ids=expected)
            ok = kv.publish_kingdom_view(
                self.config.kingdom_view_artifact, view,
                write_json=atomic_write_json)
            if not ok:
                append_event(self.journal, {
                    "type": "kingdom_view_publish_failed",
                    "path": str(self.config.kingdom_view_artifact)})
        except Exception as exc:
            append_event(self.journal, {
                "type": "kingdom_view_publish_failed", "error": str(exc)})

    def provision_leverage(self) -> None:
        """Eval-profile boot: pin venue leverage for sleeve-eligible
        family-map assets. `_LEV_OVERRIDES` (~27) are raise-above-default
        pins only — unpinned names correctly use venue defaults (crypto
        2 / xyz 4) matching limits (Governor 2026-11-08). Prefer
        /leverage-limits/effective as venue truth (`target_leverage`);
        static ladder is fail-closed fallback when limits are dark.
        Sleeve filter keeps tradfi off crypto pins and vice versa. Skip
        open brackets + `_PROVISION_REFUSED`; best-effort.
        """
        if self.config.profile != "evaluation":
            return
        from .families import family_assets
        open_assets = {b["asset"] for b in self.executor.open_brackets()}
        limits: dict | None = None
        try:
            limits = self.client.leverage_limits()
        except Exception as exc:
            append_event(self.journal, {
                "type": "leverage_limits_fetch_failed", "error": str(exc)})
        assets = ep.provision_assets_for(
            self.config.asset_class, family_assets())
        append_event(self.journal, {
            "type": "leverage_provision_plan", "sleeve": self.config.asset_class,
            "n": len(assets), "limits_known": limits is not None})
        for asset in assets:
            if asset in open_assets:
                continue
            static = ep.leverage_for(asset)
            target = ep.target_leverage(asset, limits)
            try:
                self.client.set_leverage(asset, target)
                row = {"type": "leverage_provisioned", "asset": asset,
                       "leverage": target}
                if target != static:
                    row["static"] = static
                    row["venue_truth"] = True
                append_event(self.journal, row)
            except Exception as exc:
                append_event(self.journal, {
                    "type": "leverage_provision_failed", "asset": asset,
                    "leverage": target, "static": static, "error": str(exc)})

    def run(self) -> None:
        append_event(self.journal, {
            "type": "boot", "account_id": self.client.account_id,
            "api_url": self.config.api_url, "enabled": self.config.enabled,
            "profile": self.config.profile,
            "asset_class": self.config.asset_class,
            "tradfi_signal_ttl_s": self.config.tradfi_signal_ttl_s,
            "tap_boot_replay": self.config.tap_boot_replay,
        })
        if self._boot_replay_event is not None:
            append_event(self.journal, self._boot_replay_event)
            self._boot_replay_event = None
        self.provision_leverage()
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
                if (self.hl_marks is not None
                        and self._due("hl_marks", self.config.hl_marks_poll_s)):
                    self.hl_poll()
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
