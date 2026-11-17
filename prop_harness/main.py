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
from .brains import book_view as bv
from .brains import edge_boost as eb
from .brains import eval_profile as ep
from .brains import formation as fm
from .brains import kingdom_view as kv
from .brains import loss_stats as ls
from .brains import post_loss_cooloff as plc
from .brains import reentry_latch as rl
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
        # Algedonic latch path lives BEFORE the drawdown brain — the brain
        # reads it at every update() so a LOCKED latch survives restarts.
        self.algedonic_path = root / "program" / "algedonic.json"
        self.drawdown = DrawdownBrain(
            root / "accounts" / "acct1" / "dd.json",
            floor_line=config.floor_line,
            floor_usd=config.internal_floor_usd,
            latch_path=self.algedonic_path,
            latch_enabled=config.algedonic_latch_enabled,
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
                                  if config.crypto_deny_symbols else None),
                              # WS-A tap holes (a)/(b): deferred seen +
                              # TTL-aware prune; kills -> legacy inside tap.
                              seen_confirm_enabled=config.tap_seen_confirm_enabled,
                              ttl_prune_enabled=config.tap_ttl_prune_enabled)
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
        # WS-A hole (c): PROP_ARM_BOOT_REPLAY defaults 0 — no cursor rewind
        # unless explicitly armed (rewind re-fires pending-unresolved rows).
        if (self.tap is not None and config.tap_boot_replay
                and bool(getattr(config, "arm_boot_replay", False))
                and config.asset_class == "tradfi"):
            self._boot_replay_event = self.tap.arm_boot_replay()
        # M4' post-loss cooloff state (harness-owned; executor untouched —
        # WS-B boundary). Latches armed at reconcile close accounting,
        # consulted at admission.
        self._post_loss_path = (root / "accounts" / "acct1"
                                / "post_loss_cooloff.json")
        self._post_loss_latches = read_json(self._post_loss_path, {}) or {}
        # Cross-sleeve conflict telemetry throttle map (book_view).
        self._book_view_last: dict[str, float] = {}
        # Algedonic latch enforcement: one journal line per distinct
        # suppressed computed-band per boot (the latch holds every tick;
        # the journal should not).
        self._algedonic_enforce_logged: set[str] = set()
        self._m8_unknown_ts = 0.0

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
        # Algedonic latch enforcement telemetry: the latch held the sleeve at
        # a terminal band against a healthier fresh recompute — one line per
        # distinct computed band per boot.
        enf = self.drawdown.last_latch_enforcement
        if enf and enf["computed_band"] not in self._algedonic_enforce_logged:
            self._algedonic_enforce_logged.add(enf["computed_band"])
            append_event(self.journal, {
                "type": "algedonic_latch_enforced",
                "computed_band": enf["computed_band"],
                "latched_band": enf["latched_band"],
                "equity": equity,
            })
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
                    # Stop arbiter (Build 1): the six stop writers below
                    # VOTE their floors inside this window; the flush after
                    # the last stage fires at most ONE venue resize per
                    # position per tick. Kill OFF → writers resize inline,
                    # legacy bit-for-bit.
                    self.executor.begin_stop_tick(bracket)
                    # Bank protect first when partials OR residual/MFE
                    # criteria met (Dayo 75% @ ~$20 — bank never returns;
                    # GOLD 4.62R latch-only when already past entry).
                    if self.executor.protect_realized_bank(bracket, pos):
                        changed = True
                    if self.executor.arm_breakeven(bracket, pos):
                        changed = True
                    if self.hl_marks is not None and isinstance(pos, dict):
                        mark = self.hl_marks.mark(bracket["asset"])
                        # Excursion trackers first: peak + trough accrue on
                        # every mark so the close journal is never blind.
                        if self.executor.track_excursions(bracket, mark):
                            changed = True
                        # Rebound cycles accrue beside the excursions so
                        # the close journal sees defended-level evidence.
                        if self.executor.track_rebound(bracket, mark):
                            changed = True
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
                        # Live-USD peak lock AFTER trail/BE/sharpen —
                        # birth-R peak is quantity-blind; the venue uPnL
                        # plane catches reload-grown runners (LTC class).
                        if self.executor.lock_live_peak(
                                bracket, pos, mark):
                            changed = True
                        # Pyramid/scalper harvest on banked runner (retouch).
                        if self.executor.harvest_runner_bank_floor(
                                bracket, pos, mark):
                            changed = True
                    # Arbiter flush: tightest voted floor wins; dust epsilon
                    # vs the last issued floor gates the single venue call.
                    if self.executor.flush_stop_arbiter(bracket, pos):
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
                # demo→turbo veto can bind once PnL lands. C4 (2026-11-17):
                # the stamp is NET of fees/funding when the venue rows
                # carry them (fees_seen=False → gross == legacy bit-for-bit
                # and the source stamp says so).
                final_pnl = None
                fees_seen = False
                try:
                    final_pnl, fees_seen = ls.close_pnl_net_from_trades(
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
                    restamp_window_s=(float(getattr(
                        self.config, "close_restamp_window_s", 0.0) or 0.0)
                        or None),
                    realized_pnl_source=(
                        ("trades_net" if fees_seen else "trades_gross")
                        if final_pnl is not None else None),
                    account_id=str(getattr(
                        self.client, "account_id", "") or "") or None,
                )
                remaining_risk = bracket["risk_usd"] - float(
                    bracket.get("risk_released", 0.0))
                # Signed realized leg (Build 3): pass the real close pnl,
                # net of positive partials the sync-shrink path already
                # released today. OFF → legacy constant 0.0.
                realized_leg = 0.0
                if self.config.budget_realized_enabled:
                    _fp = bracket.get("realized_pnl_final")
                    try:
                        realized_leg = (float(_fp) if _fp is not None
                                        else float(bracket.get(
                                            "realized_booked", 0.0) or 0.0))
                    except (TypeError, ValueError):
                        realized_leg = 0.0
                    realized_leg -= float(bracket.get(
                        "budget_realized_released", 0.0) or 0.0)
                self.budget.release(max(0.0, remaining_risk), realized_leg)
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
                    coh_big=ep.coh_big_for(
                        bracket.get("asset"), bracket.get("opened_ts"),
                        base=float(getattr(
                            self.config, "eval_coh_big", 7.0) or 7.0),
                        tradfi=getattr(
                            self.config, "eval_coh_big_tradfi", None),
                        tradfi_usopen=getattr(
                            self.config, "eval_coh_big_tradfi_usopen",
                            None)),
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
                # Post-win same-direction cooloff (2026-11-15 salvo class).
                self.executor.arm_post_win_cooloff(bracket)
                # M4' post-loss cooloff (additive): any losing close arms
                # the (asset, side) latch consulted at admission.
                self._arm_post_loss_cooloff(bracket)
                append_event(self.journal, {
                    "type": "reconcile_closed_external",
                    "semantic_id": bracket["semantic_id"],
                    "family": bracket.get("family"),
                    "side": bracket.get("side"),
                    "realized_pnl_final": bracket.get("realized_pnl_final"),
                    "realized_pnl_source": bracket.get(
                        "realized_pnl_source"),
                    "risk_usd": bracket.get("risk_usd"),
                    "coherence_at_entry": bracket.get("coherence_at_entry"),
                    # WHY knob-gated spread: OFF keeps the legacy journal
                    # row bit-for-bit (no new keys materialize). band_label
                    # stamps the drawdown band the close landed in (Build 4).
                    **(({
                        "mfe_peak_r": bracket.get("mfe_peak_r"),
                        "mae_trough_r": bracket.get("mae_trough_r"),
                        "band_label": (self.drawdown.state.band
                                       if self.drawdown.state else None),
                    }) if self.config.mae_tracker_enabled else {}),
                    **(({"rebound_count": bracket.get("rebound_count")}
                        ) if self.config.rebound_tracker_enabled else {}),
                    **({
                        "formation_unit": bracket.get("formation_unit"),
                        "edge_point": bracket.get("edge_point"),
                    } if bracket.get("formation_unit") else {}),
                })
        if changed:
            atomic_write_json(reg_path, reg)
        # H4 spent latch while big-bets are open (SOL stack prevention):
        # commit fraction can cross the threshold without a new signal.
        self._maybe_arm_spent_latch()
        self.publish_loss_veto()
        self.publish_positions()
        self.publish_book_view()

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
        closed brackets -> this seat's OWN loss_veto.json (C9, 2026-11-17:
        the write path is always the own root — a consumer seat with
        PROP_LOSS_VETO_PATH set still reads the shared artifact but never
        writes it, so its closes can never pollute the probe's evidence).

        Heals legacy closed_external rows missing family / final PnL so a
        cold registry lights the artifact on the next publish cycle
        without waiting for a fresh close. Rows inside the
        close_restamp_window_s get a FRESH trades read each publish so a
        venue-lag first stamp (trailing fills still landing) upgrades to
        the net venue number (C4 venue-lag re-stamp).

        vetoed_ts (C12 quarantine): the payload carries the latest veto ts
        per cohort key, carried forward across publishes; edge_boost reads
        it so a cohort vetoed within the quarantine window cannot be
        boosted on the same evidence that just punished it.
        """
        if not self.config.loss_veto_enabled:
            return
        reg = self.executor._registry()
        closed: list[dict] = []
        healed = False
        now = time.time()
        restamp_s = (float(getattr(
            self.config, "close_restamp_window_s", 0.0) or 0.0) or None)
        client = getattr(self, "client", None)
        account_id = (str(getattr(client, "account_id", "") or "")
                      or None)
        for b in reg.get("brackets") or []:
            if b.get("status") != "closed_external":
                continue
            fresh_pnl = None
            fees_seen = False
            if restamp_s and client is not None and b.get("asset"):
                try:
                    cts = float(b.get("closed_ts") or 0.0)
                except (TypeError, ValueError):
                    cts = 0.0
                if cts > 0 and (now - cts) <= restamp_s:
                    try:
                        fresh_pnl, fees_seen = ls.close_pnl_net_from_trades(
                            client.get_trades(base=b["asset"]),
                            float(b.get("opened_ts", 0.0)))
                    except Exception:
                        fresh_pnl = None
            if ls.stamp_close_outcome(
                    b,
                    family=(None if b.get("family")
                            else family_of(b["asset"]) if b.get("asset")
                            else None),
                    realized_pnl_final=fresh_pnl,
                    coherence_at_entry=b.get(
                        "coherence_at_entry", b.get("coherence")),
                    restamp_window_s=restamp_s,
                    realized_pnl_source=(
                        ("trades_net" if fees_seen else "trades_gross")
                        if fresh_pnl is not None else None),
                    account_id=account_id,
                    now=now,
            ):
                healed = True
            closed.append(b)
        if healed:
            self.executor._save_registry(reg)
        stats = ls.CohortStats(min_n=self.config.loss_veto_min_n).build(
            closed,
            grain=("asset" if self.config.loss_veto_asset_grain_enabled
                   else "family"),
            account_id=(account_id if bool(getattr(
                self.config, "cohort_account_filter_enabled", True))
                else None),
            epoch_ts=float(getattr(self.config, "cohort_epoch_ts", 0.0)
                           or 0.0))
        # vetoed_ts carry-forward: the prior artifact is read BEFORE the
        # overwrite; a key currently vetoed stamps now (latest veto), a
        # key that left veto keeps its last ts so the quarantine extends
        # past the final veto observation.
        vetoed_ts: dict[str, float] = {}
        prior = read_json(self.config.loss_veto_write_artifact)
        if isinstance(prior, dict):
            pv = prior.get("vetoed_ts")
            if isinstance(pv, dict):
                for k, v in pv.items():
                    try:
                        fv = float(v)
                    except (TypeError, ValueError):
                        continue
                    if fv == fv:
                        vetoed_ts[str(k)] = fv
        for key, row in stats.items():
            if isinstance(row, dict) and row.get("veto"):
                vetoed_ts[key] = now
        # schema=2 marks asset-grain stats; consumers fail closed to EMPTY
        # on a missing/old schema so a legacy (family, side) artifact never
        # vetoes under asset keys. Kill OFF → legacy payload bit-for-bit.
        payload: dict = {"ts": now, "stats": stats,
                         "vetoed_ts": vetoed_ts}
        if self.config.loss_veto_asset_grain_enabled:
            payload["schema"] = 2
        atomic_write_json(self.config.loss_veto_write_artifact, payload)

    def publish_positions(self) -> None:
        mark_fn = None
        if self.hl_marks is not None:
            mark_fn = self.hl_marks.mark
        snap = build_positions_snapshot(
            self.executor.open_brackets(), self.client.account_id,
            self.config.asset_class, time.time(), self.config.max_positions,
            mark_fn=mark_fn)
        atomic_write_json(self.config.state_root / "prop_positions.json", snap)

    def _arm_post_loss_cooloff(self, bracket: dict) -> None:
        """M4' arm site (additive): ANY losing close latches (asset, side).

        Harness-owned state file — the executor keeps its WS-B boundary.
        Kill OFF / non-loss / missing fields → no-op (legacy bit-for-bit).
        """
        if not bool(getattr(self.config, "post_loss_cooloff_enabled", True)):
            return
        if not plc.loss_qualifies(bracket.get("realized_pnl_final")):
            return
        now = time.time()
        self._post_loss_latches = plc.arm_latch(
            self._post_loss_latches,
            asset=str(bracket.get("asset") or ""),
            side=bracket.get("side"),
            realized_pnl=float(bracket.get("realized_pnl_final")),
            now=now,
            ttl_s=float(getattr(self.config, "post_loss_cooloff_ttl_s",
                                1800.0) or 1800.0),
            semantic_id=bracket.get("semantic_id"))
        self._post_loss_latches = plc.prune_expired(
            self._post_loss_latches, now)
        atomic_write_json(self._post_loss_path, self._post_loss_latches)

    def publish_book_view(self) -> None:
        """Cross-sleeve conflict TELEMETRY (WS-A build 6) — never blocks.

        Merge own open (asset, side) exposure into the shared artifact,
        then log company_conflict_open (300s throttle per pair) for every
        opposite-side holding another live sleeve carries. Write race
        between sleeves self-heals on the next reconcile tick.
        """
        if not bool(getattr(self.config, "book_view_enabled", True)):
            return
        now = time.time()
        sleeve = str(self.client.account_id or "acct1")
        reg = self.executor._registry()
        rows = (reg or {}).get("brackets") or []
        view = read_json(self.config.book_view_artifact, {}) or {}
        view = bv.merge_sleeve(
            view, sleeve, bv.build_sleeve_entry(rows, now))
        atomic_write_json(self.config.book_view_artifact, view)
        for conflict in bv.conflicts(view, sleeve, now):
            key = "%s:%s:%s" % (conflict["other_sleeve"],
                                conflict["asset"], conflict["own_side"])
            if not bv.throttle_allows(self._book_view_last, key, now):
                continue
            bv.note_throttle(self._book_view_last, key, now)
            append_event(self.journal, {"type": bv.EVENT, **conflict})

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
        # Draft order (SLEEVE_FORMATION_20261114): when slots/budget bind,
        # the decisive soldiers arm first — cavalry-class macro strikes,
        # then giant longs, archer probes/shorts last. Ordering only.
        if (signals
                and bool(getattr(self.config, "formation_priority_enabled",
                                 True))
                and bool(getattr(self.config, "formation_tagging_enabled",
                                 True))):
            _armed = bool(getattr(self.config, "formation_cavalry_armed",
                                  False))
            signals = sorted(
                signals,
                key=lambda s: fm.draft_sort_key(fm.formation_tag_for_signal(
                    s, enabled=True, cavalry_armed=_armed)))
        for sig in signals:
            # Tap hole (a): confirm AFTER the execution attempt resolves;
            # an exception leaves the id un-burned (re-firable on rewind).
            try:
                outcome = self._execute_signal(sig)
            except Exception:
                self.tap.confirm(sig, resolved=False)
                raise
            self.tap.confirm(sig, resolved=(outcome != "unresolved"))

    def _execute_signal(self, sig: dict) -> str:
        """Returns "resolved" (terminal — id burned) or "unresolved"
        (the attempt never happened — e.g. dark drawdown plane; the id
        stays re-firable)."""
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
                return "resolved"
            append_event(self.journal, {
                "type": "tap_skip_open", "asset": sig["asset"], "id": sig["id"]})
            return "resolved"
        # M4' post-loss cooloff consult (admission path): refuse the same
        # (asset, side) that just lost while its latch is hot.
        _now = time.time()
        _pl_reason, _pl_detail = plc.cooloff_verdict(
            bool(getattr(self.config, "post_loss_cooloff_enabled", True)),
            self._post_loss_latches, sig["asset"], sig["side"], _now)
        if _pl_reason:
            self._post_loss_latches = plc.note_cooloff_block(
                self._post_loss_latches, sig["asset"], sig["side"])
            atomic_write_json(self._post_loss_path, self._post_loss_latches)
            append_event(self.journal, {
                "type": plc.COOLOFF_BLOCK, "id": sig["id"],
                "coherence": sig.get("coherence"), **_pl_detail})
            return "resolved"
        # Re-entry close-epoch latch (WS-A build 3): a fresh stop-class /
        # external close refuses the same (symbol, side) inside the TTL.
        _rl_reason, _rl_detail = rl.reentry_verdict(
            bool(getattr(self.config, "reentry_latch_enabled", True)),
            (self.executor._registry() or {}).get("brackets"),
            sig["asset"], sig["side"], _now,
            ttl_s=float(getattr(self.config, "reentry_latch_ttl_s", 1800.0)
                        or 1800.0))
        if _rl_reason:
            append_event(self.journal, {
                "type": rl.REENTRY_BLOCK, "id": sig["id"],
                "coherence": sig.get("coherence"), **_rl_detail})
            return "resolved"
        # Per-symbol fire floor (WS-A build 5): symbol-specific > class
        # floor. Consulted here (admission ownership) — the executor's
        # class-floor consult site is WS-B-owned.
        _sym_reason, _sym_floor = ep.symbol_min_coh_fire_verdict(
            sig.get("coherence"), sig.get("asset"), sig.get("symbol"),
            floor_map=ep.parse_symbol_min_coh_map(
                getattr(self.config, "symbol_min_coh_fire", "")))
        if _sym_reason:
            append_event(self.journal, {
                "type": "tap_signal", "id": sig["id"], "asset": sig["asset"],
                "side": sig["side"], "risk_usd": 0.0,
                "coherence": sig.get("coherence"),
                "symbol_floor": _sym_floor,
                "ok": False, "reason": _sym_reason})
            return "resolved"
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
                return "resolved"
        if self.drawdown.state is None:
            # Dark equity plane — the attempt never happened; the tap id
            # must NOT be burned (re-firable after the plane lights).
            return "unresolved"
        # Formation tag (draft board, telemetry): would-be unit + edge point
        # ride the tap/registry/close events so the war council can grade
        # each rank before new money moves. Never gates, never sizes.
        _fm_tag = fm.formation_tag_for_signal(
            sig,
            enabled=bool(getattr(self.config, "formation_tagging_enabled",
                                 True)),
            cavalry_armed=bool(getattr(self.config, "formation_cavalry_armed",
                                       False)))
        _fm_fields = ({
            "formation_unit": _fm_tag.get("unit"),
            "edge_point": _fm_tag.get("edge_point"),
        } if _fm_tag else {})
        # Archer slot doctrine: expendable probes/shorts never squat the
        # reserve slot(s) a Giant/Cavalry needs. Shadow-first: default
        # journals formation_slot_standby; LIVE via PROP_FORMATION_ARCHER_SLOT_LIVE=1.
        if _fm_tag:
            _slot_reason = fm.archer_slot_verdict(
                _fm_tag, len(self.executor.open_brackets()),
                self.config.max_positions,
                int(getattr(self.config, "slot_reserve_slots", 1) or 1))
            if _slot_reason:
                append_event(self.journal, {
                    "type": "formation_slot_standby", "id": sig["id"],
                    "asset": sig["asset"], "side": sig["side"],
                    "coherence": sig.get("coherence"), **_fm_fields})
                if bool(getattr(self.config, "formation_archer_slot_live",
                                False)):
                    return "resolved"
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
            _eq = None
            if self.drawdown.state is not None:
                try:
                    _eq = float(self.drawdown.state.equity)
                except (TypeError, ValueError):
                    _eq = None
            # Per-class big-bet bar (Governor 2026-11-15): stocks never
            # print crypto-scale coherence; xyz: resolves to its own bar.
            _sig_ts = None
            try:
                _sig_ts = float(sig.get("ts_ms")) / 1000.0
            except (TypeError, ValueError):
                _sig_ts = None
            _coh_big = ep.coh_big_for(
                sig.get("asset"), _sig_ts, base=self.config.eval_coh_big,
                tradfi=getattr(self.config, "eval_coh_big_tradfi", None),
                tradfi_usopen=getattr(
                    self.config, "eval_coh_big_tradfi_usopen", None))
            eval_risk = ep.risk_for_signal(
                self.config.profile, sig.get("coherence"),
                self.budget.remaining, self.drawdown.headroom,
                self.config.hard_zero_headroom_usd, self.drawdown.mult,
                self.config.r_base_usd, self.config.eval_trend_budget_usd,
                _coh_big, self.config.eval_coh_max,
                self.config.eval_risk_frac,
                asset=sig.get("asset"),
                tradfi_min_enabled=bool(getattr(
                    self.config, "eval_tradfi_min_risk_enabled", False)),
                tradfi_min_risk=float(getattr(
                    self.config, "eval_tradfi_min_risk_usd", 14.0) or 14.0),
                crypto_min_enabled=bool(getattr(
                    self.config, "eval_crypto_min_risk_enabled", False)),
                crypto_min_risk=float(getattr(
                    self.config, "eval_crypto_min_risk_usd", 7.0) or 7.0),
                day_latched=self._day_latched(),
                equity=_eq,
                tradfi_account_risk_enabled=bool(getattr(
                    self.config, "eval_tradfi_account_risk_enabled", True)),
                tradfi_account_risk_frac=float(getattr(
                    self.config, "eval_tradfi_account_risk_frac", 0.005)
                    or 0.005))
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
            # Edge boost (2026-11-17) — profit mirror of loss_veto: the
            # same cohort artifact arms a bounded size multiplier on
            # proven-positive cohorts. Spliced BEFORE the rho box so the
            # headroom clamp still binds — a boost can never exceed
            # headroom. Kill PROP_EDGE_BOOST=0 → legacy sizing bit-for-bit.
            # C12 symmetry: edge_boost_requires_veto (default on) stands
            # the boost down when the loss-veto side is disabled — the
            # artifact's negative side must be armed for its positive side
            # to be trusted. vetoed_recently quarantine: a cohort vetoed
            # within edge_boost_veto_quarantine_s cannot be boosted on the
            # evidence that just punished it.
            if (bool(getattr(self.config, "edge_boost_enabled", True))
                    and risk > 0
                    and (not bool(getattr(
                        self.config, "edge_boost_requires_veto", True))
                         or bool(getattr(
                             self.config, "loss_veto_enabled", True)))):
                _eb_art = read_json(self.config.loss_veto_artifact)
                if ls.artifact_fresh(_eb_art):
                    _eb_key = eb.key_for(
                        _eb_art, asset=sig.get("asset"),
                        side=sig.get("side"),
                        family=family_of(sig["asset"]) if sig.get("asset")
                        else None,
                        asset_grain=bool(getattr(
                            self.config, "loss_veto_asset_grain_enabled",
                            True)))
                    if _eb_key is not None and not eb.vetoed_recently(
                            _eb_art, _eb_key, now=_now,
                            quarantine_s=float(getattr(
                                self.config, "edge_boost_veto_quarantine_s",
                                0.0) or 0.0)):
                        if bool(getattr(
                                self.config, "edge_boost_r_based_enabled",
                                True)):
                            _eb_mult, _eb_row = eb.boost_for_r(
                                (_eb_art.get("stats") or {}), _eb_key,
                                min_n=int(getattr(
                                    self.config, "edge_boost_min_n", 5)
                                    or 5),
                                min_r=float(getattr(
                                    self.config, "edge_boost_min_r", 0.10)
                                    or 0.10),
                                cap=float(getattr(
                                    self.config, "edge_boost_max_mult", 1.5)
                                    or 1.5),
                                full_n=int(getattr(
                                    self.config, "edge_boost_full_n", 20)
                                    or 20))
                        else:
                            _eb_mult, _eb_row = eb.boost_for(
                                (_eb_art.get("stats") or {}), _eb_key,
                                min_n=int(getattr(
                                    self.config, "edge_boost_min_n", 5)
                                    or 5),
                                min_expectancy=float(getattr(
                                    self.config, "edge_boost_min_expectancy",
                                    0.5) or 0.5),
                                cap=float(getattr(
                                    self.config, "edge_boost_max_mult", 1.5)
                                    or 1.5))
                        if _eb_mult > 1.0:
                            _eb_pre = float(risk)
                            risk = risk * _eb_mult
                            append_event(self.journal, {
                                "type": "edge_boost_applied",
                                "id": sig["id"], "asset": sig["asset"],
                                "side": sig["side"],
                                "coherence": sig.get("coherence"),
                                "cohort": _eb_key,
                                "cohort_n": _eb_row.get("n"),
                                "cohort_n_r": _eb_row.get("n_r"),
                                "shrunk_expectancy": _eb_row.get(
                                    "shrunk_expectancy"),
                                "shrunk_expectancy_r": _eb_row.get(
                                    "shrunk_expectancy_r"),
                                "risk_pre": _eb_pre, "risk_usd": risk,
                                "boost_mult": _eb_mult})
            # M8 rho-box clamp (quant-confirmed turbo2): risk ≤ rho ×
            # remaining headroom above the kill floor; sub-min-viable →
            # DENY. Kill/rho<=0 → legacy chain bit-for-bit.
            if (bool(getattr(self.config, "rho_box_enabled", True))
                    and risk > 0):
                _hr = (self.drawdown.headroom
                       if self.drawdown.state is not None else None)
                _risk_pre = float(risk)
                risk, _rho_v = ep.rho_box_clamp(
                    risk, _hr,
                    rho=float(getattr(self.config, "rho_box", 0.12) or 0.0),
                    min_viable=float(getattr(
                        self.config, "rho_box_min_viable_usd", 7.0) or 7.0),
                    enabled=True)
                if _rho_v == "deny":
                    append_event(self.journal, {
                        "type": "m8_rho_deny", "id": sig["id"],
                        "asset": sig["asset"], "side": sig["side"],
                        "coherence": sig.get("coherence"),
                        "risk_before": _risk_pre, "headroom": _hr})
                    return "resolved"
                if _rho_v == "resize":
                    append_event(self.journal, {
                        "type": "m8_rho_resize", "id": sig["id"],
                        "asset": sig["asset"], "side": sig["side"],
                        "coherence": sig.get("coherence"),
                        "risk_before": _risk_pre, "risk_after": risk,
                        "headroom": _hr})
                elif _rho_v == "unknown":
                    # Dark drawdown plane — throttled note, legacy result.
                    if _now - self._m8_unknown_ts >= 300.0:
                        self._m8_unknown_ts = _now
                        append_event(self.journal, {
                            "type": "m8_headroom_unknown",
                            "asset": sig["asset"]})
            # Big-bet denser TP ladder (institutional scale-out); probes
            # keep EVAL_LADDER. Crypto + PROP_CRYPTO_TP_TIGHTEN → slight
            # tighter EVAL_*_CRYPTO (turbo rotation). xyz:/kill OFF keep
            # legacy EVAL_LADDER bit-for-bit (turbo2 stocks untouched).
            ladder = ep.ladder_for_signal(
                self.config.profile, sig.get("coherence"),
                big_bet_ladder_enabled=bool(getattr(
                    self.config, "big_bet_ladder_enabled", True)),
                coh_big=_coh_big,
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
                **_fm_fields,
                "ok": False, "reason": "risk cap zero"})
            return "resolved"
        res = self.executor.place_bracket(
            asset=sig["asset"], side=sig["side"], risk_usd=risk,
            entry_ref=sig["entry_ref"], stop_price=sig["stop_price"],
            ladder=ladder, coherence=sig.get("coherence"),
            formation=_fm_tag,
            # Envelope v2 ids ride the registry row (additive, executor
            # :787-813); absent fields stay off legacy rows.
            signal_meta=({k: sig[k] for k in
                          ("signal_id", "signal_ts", "epoch")
                          if sig.get(k) is not None} or None))
        _bc = sig.get("block_class") or "aria_exec"
        append_event(self.journal, {
            "type": "tap_signal", "id": sig["id"], "asset": sig["asset"],
            "side": sig["side"], "risk_usd": risk, "coherence": sig["coherence"],
            "block_class": _bc,
            "capital_reason": sig.get("capital_reason"),
            **_fm_fields,
            "ok": res.ok, "reason": res.reason})
        # §7 census — surplus route journaled before KingdomView (v1).
        if _bc == "aria_capital_block":
            append_event(self.journal, {
                "type": "capital_block_routed", "id": sig["id"],
                "asset": sig["asset"], "side": sig["side"],
                "capital_reason": sig.get("capital_reason"),
                "ok": res.ok, "reason": res.reason})
        return "resolved"

    def budget_roll(self) -> None:
        if self.drawdown.state is None:
            return
        # Build 3 telemetry: capture the outgoing day's signed realized
        # BEFORE roll() zeroes it (None state = pre-first-roll, no row).
        _prev_day = self.budget.state.day if self.budget.state else None
        _prev_realized = (self.budget.state.realized
                          if self.budget.state else None)
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
            if self.config.budget_realized_enabled and _prev_day is not None:
                append_event(self.journal, {
                    "type": "budget_realized_tracking",
                    "day": _prev_day,
                    "realized_usd": _prev_realized,
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
            "arm_boot_replay": bool(getattr(self.config, "arm_boot_replay",
                                            False)),
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
