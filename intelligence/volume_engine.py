"""
intelligence/volume_engine.py — Cumulative volume + fee ledger (SoPoints doctrine).

Governor doctrine (locked 2026-09-26): ARIA farms SoDEX SoPoints — cumulative
filled notional is the asset. Target: $1.0M-1.6M cumulative volume over 20
weeks (~$50k-80k/week pace; the weekly_target knob defaults to the $65k
midpoint). SoPoints snapshot: every Tuesday 12:00 UTC. The campaign engine
must keep net cost = gross fees − campaign realized PnL < $7 per $100k
volume. SoDEX fee tiers are 14-day rolling (perps + 2×spot; this module
tracks perps volume — spot legs are injected by the caller as pool="spot"
fills if the coordinator wires them): T0 ≤$5M maker 0.012%/taker 0.040%;
T1 >$5M 0.010/0.036; T2 >$25M 0.006/0.032; T3 >$100M 0.002/0.028; T4+ maker
0. SOSO staking discount 5% multiplicative (SOSO_DISCOUNT = 0.05).

This module is the measurement plane. One row per FILL, appended
synchronously to logs/volume_ledger.jsonl (append-only, journal-permanence
doctrine: rows are NEVER rewritten — a mis-booked fill is corrected by a
compensating row, never by editing history). An atomic snapshot JSON
(tmp-write + os.replace) mirrors the current gauge so external consumers
never parse the JSONL.

Department template laws: zero I/O beyond the two injected paths; one
append writer; one-bad-line read doctrine (a corrupt line is skipped,
never fatal — idiom mirrors intelligence/shadow_journal.py:557-563); kill
switch whose False state reproduces the pre-module system bit-for-bit
(the coordinator skips every record_fill call and no file is created).

Kill switch / config knobs (coordinator adds to core/config.py; the class
takes them as constructor params, defaults reproduce doctrine):
  volume_engine_enabled=True          — False: coordinator never calls us
  volume_ledger_path="logs/volume_ledger.jsonl"
  volume_snapshot_path="logs/volume_gauge.json"
  volume_weekly_target_usd=65000.0    — $50k-80k band midpoint

Telemetry events (emitted by the COORDINATOR off this module's reads —
the brain itself is structlog-quiet on the hot path):
  volume_gauge             — one emit per gauge write (the snapshot row)
  volume_tuesday_snapshot  — coordinator emits when
                             tuesday_snapshot_marker(now_ts) flips True
                             (10-minute window after Tuesday 12:00 UTC)
  volume_fee_tier_change   — coordinator emits when consume_tier_change()
                             returns a tier (14d rolling volume crossed a
                             strictly-greater threshold, either direction)

Fee-tier boundary doctrine: thresholds are STRICTLY-GREATER — exactly $5M
14d volume is T0, $5M+1 is T1. T4 threshold is not in the locked doctrine
(only "maker 0" is fixed); it is forward-declared at +inf so T3 is the top
reachable tier until the Governor supplies the T4 boundary — fail-closed,
never under-charges.
"""

from __future__ import annotations

import json
import os
import time
from typing import Dict, List, Optional, Tuple

import structlog

log = structlog.get_logger(__name__)

SCHEMA_VERSION = 1

# ── Doctrine constants ────────────────────────────────────────────────────────

SOSO_DISCOUNT = 0.05  # SOSO staking fee discount, multiplicative

# (tier_name, min_14d_volume_usd_EXCLUSIVE, maker_rate, taker_rate)
# Rates are BASE rates before the SOSO discount. Threshold semantics are
# strictly-greater: tier applies when vol_14d > threshold.
FEE_TIERS: Tuple[Tuple[str, float, float, float], ...] = (
    ("T0", 0.0,           0.00012, 0.00040),
    ("T1", 5_000_000.0,   0.00010, 0.00036),
    ("T2", 25_000_000.0,  0.00006, 0.00032),
    ("T3", 100_000_000.0, 0.00002, 0.00028),
    ("T4", float("inf"),  0.0,     0.00028),  # threshold not in doctrine; maker 0
)

CAMPAIGN_POOL = "campaign"
NET_COST_ABSTAIN_VOLUME_USD = 1_000.0  # no fake precision below $1k pool volume

WEEK_S = 7 * 86400
FORTNIGHT_S = 14 * 86400
SNAPSHOT_WINDOW_S = 600  # 10 minutes after Tuesday 12:00 UTC


# ── Pure helpers ──────────────────────────────────────────────────────────────

def fee_tier_for_14d_volume(vol: float) -> Tuple[str, float, float]:
    """(tier_name, maker_rate, taker_rate) for a 14d rolling perps volume.

    Strictly-greater thresholds: exactly $5M -> T0. Rates are base rates
    (apply (1 - SOSO_DISCOUNT) for the staked rate). Fail-closed: a
    non-numeric / negative volume reads as T0."""
    try:
        v = float(vol)
    except (TypeError, ValueError):
        v = 0.0
    if not (v == v) or v < 0.0:  # NaN or negative
        v = 0.0
    best = FEE_TIERS[0]
    for name, threshold, maker, taker in FEE_TIERS:
        if v > threshold:
            best = (name, threshold, maker, taker)
    return (best[0], best[2], best[3])


def discounted_rate(base_rate: float) -> float:
    """SOSO staking discount, multiplicative."""
    return float(base_rate) * (1.0 - SOSO_DISCOUNT)


def next_tuesday_noon_utc(now_ts: float) -> float:
    """Next Tuesday 12:00:00 UTC strictly after now_ts (epoch seconds).

    Pure calendar math on UTC; no I/O. If now IS Tuesday 12:00:00 exactly,
    returns next week's (the current snapshot instant is the window start,
    not a future countdown target)."""
    tm = time.gmtime(float(now_ts))
    midnight = float(now_ts) - (tm.tm_hour * 3600 + tm.tm_min * 60 + tm.tm_sec)
    days_ahead = (1 - tm.tm_wday) % 7  # Tuesday = weekday 1
    cand = midnight + days_ahead * 86400 + 12 * 3600
    if cand <= float(now_ts):
        cand += WEEK_S
    return cand


# ── Ledger ────────────────────────────────────────────────────────────────────

class VolumeLedger:
    """Append-only cumulative volume + fee ledger.

    Boot recovery: load the JSONL, rebuild cumulative state; a missing file
    is a fresh start, NOT an error. One-bad-line doctrine: a corrupt line
    costs that row only; later rows still load."""

    def __init__(self, ledger_path: str = "logs/volume_ledger.jsonl",
                 snapshot_path: str = "logs/volume_gauge.json",
                 weekly_target_usd: float = 65000.0):
        self._ledger_path = str(ledger_path)
        self._snapshot_path = str(snapshot_path)
        self._weekly_target = float(weekly_target_usd)
        self._rows: List[dict] = []
        self._cumulative_volume = 0.0
        self._cumulative_fees = 0.0
        self._pools: Dict[str, Dict[str, float]] = {}
        self._last_tier: Optional[str] = None
        self._pending_tier_change: Optional[str] = None
        self.skipped_lines = 0
        self._load()

    # ── persistence ───────────────────────────────────────────────────────

    def _load(self) -> None:
        """One-bad-line read: corrupt rows skipped, file never fatal."""
        try:
            with open(self._ledger_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        self.skipped_lines += 1
                        continue
                    if not isinstance(row, dict):
                        self.skipped_lines += 1
                        continue
                    try:
                        self._apply(row)
                    except Exception:
                        self.skipped_lines += 1
                        continue
                    self._rows.append(row)
        except FileNotFoundError:
            pass  # fresh start is not an error
        except Exception as _e:
            log.warning("volume_ledger_load_failed", error=str(_e)[:120])

    def _apply(self, row: dict) -> None:
        """Fold one row into cumulative state. Raises on garbage (caller
        catches and counts the skip)."""
        notional = float(row["notional_usd"])
        fee = float(row.get("fee_usd", 0.0) or 0.0)
        pool = str(row.get("pool") or "standard")
        pnl = float(row.get("realized_pnl_usd", 0.0) or 0.0)
        self._cumulative_volume += notional
        self._cumulative_fees += fee
        p = self._pools.setdefault(pool, {"volume": 0.0, "fees": 0.0,
                                          "realized_pnl": 0.0, "fills": 0})
        p["volume"] += notional
        p["fees"] += fee
        p["realized_pnl"] += pnl
        p["fills"] += 1

    def _write_snapshot(self, gauge: dict) -> bool:
        """Atomic snapshot: tmp-write + os.replace. A crash mid-write leaves
        either the OLD snapshot or a stray .tmp — never a torn file under the
        real name. Never raises."""
        tmp = self._snapshot_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(json.dumps(gauge, default=str))
            os.replace(tmp, self._snapshot_path)
            return True
        except Exception as _e:
            log.warning("volume_snapshot_write_failed", error=str(_e)[:120])
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            return False

    # ── write side ────────────────────────────────────────────────────────

    def record_fill(self, *, ts: float, symbol: str, side: str,
                    notional_usd: float, fee_usd: float, pool: str,
                    realized_pnl_usd: float = 0.0,
                    maker: bool = False) -> bool:
        """Append one fill row, update cumulative gauges, rewrite the atomic
        snapshot. Never raises, never blocks the trade path — a serialization
        or disk failure costs this row only (one-bad-line doctrine)."""
        try:
            row = {
                "schema": SCHEMA_VERSION,
                "ts": float(ts),
                "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                        time.gmtime(float(ts))),
                "symbol": str(symbol),
                "side": str(side),
                "notional_usd": round(float(notional_usd), 6),
                "fee_usd": round(float(fee_usd), 6),
                "pool": str(pool or "standard"),
                "realized_pnl_usd": round(float(realized_pnl_usd or 0.0), 6),
                "maker": bool(maker),
            }
            line = json.dumps(row, default=str)
        except Exception as _se:
            log.warning("volume_ledger_serialize_failed", error=str(_se)[:120])
            return False
        try:
            with open(self._ledger_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as _we:
            log.warning("volume_ledger_write_failed", error=str(_we)[:120])
            return False
        self._rows.append(row)
        try:
            self._apply(row)
        except Exception:
            pass
        # Fee-tier transition detector (14d rolling volume crossing a
        # strictly-greater threshold, either direction).
        try:
            tier_now, _, _ = fee_tier_for_14d_volume(
                self._window_volume(float(ts), FORTNIGHT_S))
            if self._last_tier is None:
                # First fill of the process: a fresh-boot discovery of a
                # non-T0 tier is worth one event (the coordinator emits the
                # tier state once post-boot).
                if tier_now != "T0":
                    self._pending_tier_change = tier_now
                self._last_tier = tier_now
            elif tier_now != self._last_tier:
                self._pending_tier_change = tier_now
                self._last_tier = tier_now
        except Exception:
            pass
        try:
            self._write_snapshot(self.gauge(float(ts)))
        except Exception:
            pass
        return True

    # ── read side ─────────────────────────────────────────────────────────

    def _window_volume(self, now_ts: float, span_s: float) -> float:
        cutoff = float(now_ts) - span_s
        total = 0.0
        for r in self._rows:
            try:
                if float(r.get("ts", 0.0)) > cutoff:
                    total += float(r.get("notional_usd", 0.0))
            except (TypeError, ValueError):
                continue
        return total

    def pool_window_sums(self, pool: str, now_ts: float, span_s: float,
                         epoch_ts: float = 0.0) -> dict:
        """Windowed pool totals for the fast-cycle fee governor
        (2026-10-02 Governor directive): sums over ledger rows for `pool`
        with ts > max(now_ts - span_s, epoch_ts). span_s <= 0 = lifetime
        (still epoch-floored). Row arithmetic matches the engine's on_close
        contract: every fill is a row, so a round trip contributes 2x
        notional and both legs' fees."""
        cutoff = max(float(now_ts) - span_s if span_s > 0 else 0.0,
                     float(epoch_ts))
        vol = fees = pnl = 0.0
        for r in self._rows:
            try:
                if r.get("pool") != pool:
                    continue
                if float(r.get("ts", 0.0)) <= cutoff:
                    continue
                vol += float(r.get("notional_usd", 0.0))
                fees += float(r.get("fee_usd", 0.0))
                pnl += float(r.get("realized_pnl_usd", 0.0))
            except (TypeError, ValueError):
                continue
        return {"volume": vol, "fees": fees, "realized_pnl": pnl}

    def gauge(self, now_ts: float) -> dict:
        """Current gauge. Pure read over in-memory state; no file I/O."""
        now = float(now_ts)
        week_vol = self._window_volume(now, WEEK_S)
        vol_14d = self._window_volume(now, FORTNIGHT_S)
        tier_now, maker_r, taker_r = fee_tier_for_14d_volume(vol_14d)
        # Projected tier: the 14d window two current-pace weeks from now
        # would hold ~2x the current rolling week.
        tier_proj, _, _ = fee_tier_for_14d_volume(2.0 * week_vol)

        camp = self._pools.get(CAMPAIGN_POOL)
        net_cost: Optional[float] = None
        if camp and camp["volume"] >= NET_COST_ABSTAIN_VOLUME_USD:
            net_cost = round(
                (camp["fees"] - camp["realized_pnl"]) / camp["volume"] * 100_000.0,
                4)

        return {
            "ts": now,
            "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "cumulative_volume_usd": round(self._cumulative_volume, 2),
            "cumulative_fees_usd": round(self._cumulative_fees, 4),
            "volume_by_pool": {p: round(v["volume"], 2)
                               for p, v in self._pools.items()},
            "week_volume_usd": round(week_vol, 2),
            "pace_vs_target": (round(week_vol / self._weekly_target, 4)
                               if self._weekly_target > 0 else None),
            "weekly_target_usd": self._weekly_target,
            "volume_14d_usd": round(vol_14d, 2),
            "fee_tier_now": tier_now,
            "fee_tier_projected": tier_proj,
            "maker_rate_now": maker_r,
            "taker_rate_now": taker_r,
            "maker_rate_staked": discounted_rate(maker_r),
            "taker_rate_staked": discounted_rate(taker_r),
            "net_cost_per_100k": net_cost,
            "snapshot_countdown_s": max(
                0.0, next_tuesday_noon_utc(now) - now),
            "tuesday_snapshot_window": tuesday_snapshot_marker(now),
        }

    def tuesday_snapshot_marker(self, now_ts: float) -> bool:
        """True inside the 10-minute window after Tuesday 12:00 UTC."""
        return tuesday_snapshot_marker(now_ts)

    def consume_tier_change(self) -> Optional[str]:
        """One-shot read for the coordinator's volume_fee_tier_change emit.
        Returns the new tier name exactly once per transition."""
        t = self._pending_tier_change
        self._pending_tier_change = None
        return t

    def write_snapshot(self, now_ts: float) -> bool:
        """Explicit snapshot write (coordinator cadence, e.g. at the Tuesday
        marker or on a telemetry tick)."""
        return self._write_snapshot(self.gauge(float(now_ts)))


def tuesday_snapshot_marker(now_ts: float) -> bool:
    """Module-level pure: True inside [Tue 12:00:00, Tue 12:10:00) UTC."""
    now = float(now_ts)
    prev_noon = next_tuesday_noon_utc(now) - WEEK_S
    return 0.0 <= (now - prev_noon) < SNAPSHOT_WINDOW_S
