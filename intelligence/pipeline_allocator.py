"""
intelligence/pipeline_allocator.py — three-pipeline capital allocator
(2026-09-26 Governor doctrine).

Pipelines:
  A = Bybit/Aster crypto profit — 35% of account, Kelly band 12-24%/trade.
  B = SoDEX equity volume — 20%, Kelly 6-10%, margin $25-35 at 4-5x
      (Governor-raised from $10-20 so notional clears the $100 floor),
      target 20-50 trades/day, core hours 13:30-20:00 UTC.
  C = SoDEX metals anchor — 15%, <= 5x, 1-3 trades/week.
  Reserve = 30% — never deployed by any pipeline.

This allocator is a COARSE bucket layer ABOVE
intelligence/portfolio_allocator.py (whose _DEFAULT_WEIGHTS are per-candidate
class weights, not capital buckets). It consumes exposure snapshots
(intelligence/exposure_snapshot.py, loop at main.py:22957) — it reads
exposure, never computes it from positions.

Coordinator interaction note (binding at the splice):
  The SoDEX margin doctrine (RAISE-ONLY floor, 2026-09-20) may raise a
  pipeline-B margin WITHIN the B bucket, but the bucket cap binds LAST —
  a doctrine-raised margin that would exceed B's remaining capacity is
  refused here, not resized back down. The foreign $100 min-notional floor
  needs no exemption: B margins $25-35 x 4-5x = $100-175 notional, so the
  floor is cleared by construction.

Persistence: logs/pipeline_allocator.json (atomic tmp+replace state mirror)
+ logs/pipeline_allocator.jsonl (append-only audit ledger, one-bad-line
doctrine). Counters roll on UTC day / ISO-week boundaries and survive
restarts.

Kill switch: env PIPELINE_ALLOCATOR_ENABLED default "true"; false ->
allocate() returns 0.0 and can_trade() refuses with reason "kill_switch".
Bookkeeping (record_trade, clamp_kelly) stays honest under the switch.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, Optional, Tuple


# ── Kill switch ───────────────────────────────────────────────────────────────

def pipeline_allocator_enabled() -> bool:
    """Module-level kill switch (env PIPELINE_ALLOCATOR_ENABLED, default
    true). False = allocate() -> 0.0, can_trade() -> (False, "kill_switch")."""
    return os.environ.get("PIPELINE_ALLOCATOR_ENABLED", "true").strip().lower() in (
        "1", "true", "yes", "on")


# ── Shared helpers ────────────────────────────────────────────────────────────

def _f(x) -> Optional[float]:
    """Coerce to float; None on any garbage (fail-closed idiom)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return v


def _day_key(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()


def _week_key(ts: float) -> str:
    iso = datetime.fromtimestamp(ts, tz=timezone.utc).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


# ── Doctrine constants ────────────────────────────────────────────────────────

PIPELINE_CAPS: Dict[str, float] = {"A": 0.35, "B": 0.20, "C": 0.15}
RESERVE_FRACTION: float = 1.0 - sum(PIPELINE_CAPS.values())   # 0.30, derived

# Kelly margin-fraction clamps per pipeline (min, max).
KELLY_BANDS: Dict[str, Tuple[float, float]] = {
    "A": (0.12, 0.24),
    "B": (0.06, 0.10),
    "C": (0.0, 0.05),
}

B_MARGIN_MIN_USD = 25.0           # Governor-raised band ($25-35 at 4-5x)
B_MARGIN_MAX_USD = 35.0
B_DAILY_TRADE_CEILING = 50        # target 20-50 trades/day
C_WEEKLY_TRADE_CEILING = 3        # 1-3 trades/week
B_CORE_HOURS_UTC = (13.5, 20.0)   # doctrine context; enforcement is the
                                  # session gate's job, not this allocator's

DEFAULT_PERSIST_PATH = "logs/pipeline_allocator.json"


# ── Verdict dataclass ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PipelineBudget:
    pipeline: str
    cap_fraction: float
    deployed: float               # margin USD currently deployed in the bucket
    available: float              # margin USD headroom under the cap
    trades_today: int = 0
    trades_this_week: int = 0


# ── Persistence (stock_carry_plane / pair_spread precedent) ──────────────────

def _atomic_write_json(path: str, payload: dict) -> None:
    """tmp + os.replace — a torn state file must never read as flat."""
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except Exception:
        pass                            # state loss is non-fatal


def _append_jsonl(path: str, row: dict) -> None:
    """Append one JSON line + fsync — low-frequency audit ledger."""
    try:
        with open(path, "a") as f:
            f.write(json.dumps(row) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        pass


def _read_state(path: str) -> dict:
    """Tolerant state load; missing/corrupt -> empty pipelines."""
    try:
        with open(path) as f:
            doc = json.load(f)
        if isinstance(doc, dict) and isinstance(doc.get("pipelines"), dict):
            return doc
    except Exception:
        pass
    return {"pipelines": {}}


# ── Allocator ─────────────────────────────────────────────────────────────────

class PipelineAllocator:
    """Bucket bookkeeping + admission control for the three pipelines.

    time_fn is the injected clock (tests pin UTC boundaries with it).
    persist_path is the atomic JSON mirror; the .jsonl sibling is the
    append-only audit ledger. All I/O failures are swallowed — the
    allocator never raises into the caller's loop.
    """

    def __init__(self, time_fn: Callable[[], float] = time.time,
                 persist_path: str = DEFAULT_PERSIST_PATH) -> None:
        self._time = time_fn
        self._path = persist_path
        self._jsonl_path = (persist_path[:-5] + ".jsonl"
                            if persist_path.endswith(".json")
                            else persist_path + ".jsonl")
        self._state: Dict[str, Dict[str, object]] = {}
        saved = _read_state(persist_path)
        for pipe, rec in saved.get("pipelines", {}).items():
            if pipe in PIPELINE_CAPS and isinstance(rec, dict):
                self._state[pipe] = {
                    "deployed": _f(rec.get("deployed")) or 0.0,
                    "trades_today": int(_f(rec.get("trades_today")) or 0),
                    "day": str(rec.get("day") or ""),
                    "trades_this_week": int(_f(rec.get("trades_this_week")) or 0),
                    "week": str(rec.get("week") or ""),
                }

    # ── internal state ────────────────────────────────────────────────────

    def _rec(self, pipeline: str) -> Dict[str, object]:
        return self._state.setdefault(pipeline, {
            "deployed": 0.0, "trades_today": 0, "day": "",
            "trades_this_week": 0, "week": "",
        })

    def _roll(self, rec: Dict[str, object], ts: float) -> None:
        """Lazy UTC-boundary roll: a stale day/week key zeroes its counter."""
        day, week = _day_key(ts), _week_key(ts)
        if rec.get("day") != day:
            rec["day"], rec["trades_today"] = day, 0
        if rec.get("week") != week:
            rec["week"], rec["trades_this_week"] = week, 0

    def _persist(self) -> None:
        _atomic_write_json(self._path, {"pipelines": self._state})

    # ── reads ─────────────────────────────────────────────────────────────

    def _deployed(self, pipeline: str, exposure_snapshot) -> float:
        """Deployed margin for the bucket. An external exposure snapshot
        carrying the pipeline wins over the internal ledger (snapshots are
        the fresher plane); anything else falls back to record_trade
        bookkeeping."""
        if isinstance(exposure_snapshot, dict):
            dm = exposure_snapshot.get("deployed_margin")
            if isinstance(dm, dict) and pipeline in dm:
                v = _f(dm[pipeline])
                if v is not None:
                    return max(0.0, v)
            if pipeline in exposure_snapshot:
                v = _f(exposure_snapshot[pipeline])
                if v is not None:
                    return max(0.0, v)
        return max(0.0, _f(self._state.get(pipeline, {}).get("deployed")) or 0.0)

    def allocate(self, pipeline, account_equity,
                 exposure_snapshot=None) -> float:
        """Available margin-USD headroom for the pipeline:
        cap x equity - deployed, floored at 0. The reserve is not a
        pipeline — unknown/reserve names return 0.0, as does the kill
        switch and any bad equity read."""
        if not pipeline_allocator_enabled():
            return 0.0
        pipe = str(pipeline or "").upper()
        cap = PIPELINE_CAPS.get(pipe)
        if cap is None:
            return 0.0                    # reserve is never allocated
        equity = _f(account_equity)
        if equity is None or equity <= 0:
            return 0.0
        return max(0.0, cap * equity - self._deployed(pipe, exposure_snapshot))

    def budget(self, pipeline, account_equity,
               exposure_snapshot=None) -> PipelineBudget:
        """Full bucket snapshot for telemetry."""
        pipe = str(pipeline or "").upper()
        cap = PIPELINE_CAPS.get(pipe, 0.0)
        deployed = self._deployed(pipe, exposure_snapshot) if cap else 0.0
        available = self.allocate(pipe, account_equity, exposure_snapshot)
        now = self._now(None)
        rec = self._rec(pipe) if cap else {}
        if cap and now is not None:
            self._roll(rec, now)
        return PipelineBudget(
            pipeline=pipe,
            cap_fraction=cap,
            deployed=deployed,
            available=available,
            trades_today=int(rec.get("trades_today", 0) or 0),
            trades_this_week=int(rec.get("trades_this_week", 0) or 0),
        )

    def can_trade(self, pipeline, margin_usd, now=None,
                  account_equity=None, exposure_snapshot=None
                  ) -> Tuple[bool, str]:
        """Admission control. Checks in order: kill switch, known pipeline,
        margin validity, B's $25-35 margin band, B's 50/day ceiling, C's
        3/week ceiling, and (only when account_equity is supplied) bucket
        capacity. Every refusal is named."""
        if not pipeline_allocator_enabled():
            return (False, "kill_switch")
        pipe = str(pipeline or "").upper()
        if pipe not in PIPELINE_CAPS:
            return (False, "unknown_pipeline")
        margin = _f(margin_usd)
        if margin is None or margin <= 0:
            return (False, "bad_margin")

        ts = self._now(now)
        rec = self._rec(pipe)
        if ts is not None:
            self._roll(rec, ts)

        if pipe == "B":
            if margin < B_MARGIN_MIN_USD:
                return (False, "b_margin_below_band")
            if margin > B_MARGIN_MAX_USD:
                return (False, "b_margin_above_band")
            if int(rec.get("trades_today", 0) or 0) >= B_DAILY_TRADE_CEILING:
                return (False, "b_daily_ceiling")
        if pipe == "C":
            if int(rec.get("trades_this_week", 0) or 0) >= C_WEEKLY_TRADE_CEILING:
                return (False, "c_weekly_ceiling")

        equity = _f(account_equity)
        if equity is not None:
            if margin > self.allocate(pipe, equity, exposure_snapshot):
                return (False, "bucket_capacity")
        return (True, "ok")

    # ── writes ────────────────────────────────────────────────────────────

    def record_trade(self, pipeline, margin_usd, ts=None) -> None:
        """Book a filled trade: bump daily/weekly counters and the deployed
        ledger, then persist (atomic mirror + audit line). Unknown pipeline
        or bad margin = no-op. Works under the kill switch — bookkeeping
        must stay honest even when admission is closed."""
        pipe = str(pipeline or "").upper()
        if pipe not in PIPELINE_CAPS:
            return
        margin = _f(margin_usd)
        if margin is None or margin <= 0:
            return
        when = self._now(ts)
        if when is None:
            return
        rec = self._rec(pipe)
        self._roll(rec, when)
        rec["trades_today"] = int(rec.get("trades_today", 0) or 0) + 1
        rec["trades_this_week"] = int(rec.get("trades_this_week", 0) or 0) + 1
        rec["deployed"] = max(0.0, (_f(rec.get("deployed")) or 0.0) + margin)
        self._persist()
        _append_jsonl(self._jsonl_path, {
            "ts": when, "pipeline": pipe, "margin_usd": margin,
            "day": rec.get("day"), "week": rec.get("week"),
            "trades_today": rec.get("trades_today"),
            "trades_this_week": rec.get("trades_this_week"),
            "deployed": rec.get("deployed"),
        })

    def release(self, pipeline, margin_usd) -> None:
        """Book a close: return margin to the bucket's deployed ledger."""
        pipe = str(pipeline or "").upper()
        if pipe not in PIPELINE_CAPS:
            return
        margin = _f(margin_usd)
        if margin is None or margin <= 0:
            return
        rec = self._rec(pipe)
        rec["deployed"] = max(0.0, (_f(rec.get("deployed")) or 0.0) - margin)
        self._persist()

    def _now(self, ts) -> Optional[float]:
        if ts is not None:
            return _f(ts)
        try:
            return _f(self._time())
        except Exception:
            return None


# ── Kelly clamp (pure) ────────────────────────────────────────────────────────

def clamp_kelly(pipeline, fraction) -> float:
    """Clamp a Kelly margin fraction into the pipeline's band. Unknown
    pipeline / bad fraction -> 0.0 (fail-closed). Pure math; runs under
    the kill switch."""
    band = KELLY_BANDS.get(str(pipeline or "").upper())
    if band is None:
        return 0.0
    f = _f(fraction)
    if f is None:
        return 0.0
    lo, hi = band
    return min(max(f, lo), hi)
