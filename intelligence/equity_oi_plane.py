"""intelligence/equity_oi_plane.py — SoDEX equity-cohort open-interest plane
(2026-10-18 Governor BUILD ORDER: "BUILD ORDER: YES. Build equity_oi_plane.py
next... Build it live. The oi_velocity brain already has fail-closed 0.0. If
SoDEX REST returns unexpected data: brain abstains. No risk to existing
signals. No shadow period needed.")

The missing input plane for the two dark OI detectors in
intelligence/equity_outliers.py (oi_velocity, pre_earnings_oi_build). The
Bybit OI plane (data/oi_history_feed.py) covers crypto perps only; this
plane serves the SoDEX mark-prices cohort (equities, indices, commodities
AND crypto — symbol classification is the brains' job via ASSET_CONFIG,
never this plane's).

VERIFIED WIRE TRUTH (Governor live probe 2026-10-18):
  GET {sodex_rest_perps}/markets/mark-prices returns a JSON LIST of
  {"symbol": "DRAM-USD", "openInterest": "481.6", "markPrice": "46.035",
   "indexPrice": "45.876", "fundingRate": "0.000039...",
   "nextFundingTime": 1792306800000}
  openInterest is a string number, consistent units per symbol — the
  detectors consume z-scores/ratios, which are unit-invariant, so NO unit
  conversion is ever applied. The parser ALSO accepts the historical
  {"data": [...]} wrapper (the funding fetch's original read) — one shape
  surprise must never kill the plane.
  /markets/symbols is STATIC specs only (caps, precisions) — no live OI.
  It is NEVER used here.

MECHANISM CHOICE — PIGGYBACK, not a self-poll:
  SoDEXFeed.fetch_funding_rates (data/sodex_feed.py) already GETs exactly
  this endpoint every 300s inside the supervised funding_loop
  (main.py:22680). The same response carries openInterest alongside
  fundingRate, so the plane rides that existing poll — ZERO new API cost,
  zero new failure modes, and the 300s cadence already equals the faster
  poll the spec budgets for US market hours. The standalone self-poll
  variant (300s US-hours / 900s off-hours against the same endpoint) was
  rejected: it would double the endpoint load for zero new information,
  and a second poller is a second thing that can die. The deque cap (96)
  holds ~8h at the 300s cadence; the detectors' own abstain laws
  (<7 points, >48h staleness) bind regardless of sampling interval.

Department-template shape (docs/DEPARTMENT_TEMPLATE.md):
  - Zero-I/O brain (EquityOIPlane): per-symbol deque(maxlen=96), oldest-
    first series getter, snapshot/restore. No network, no logging inside
    record/series — the splice owns telemetry.
  - One splice point: the funding_loop piggyback in main.py feeds
    ingest_poll(); the detector splice reads plane.series via the bound
    oi_series_fn.
  - Kill switch: env EQUITY_OI_PLANE_ENABLED default "true"; "false" =
    series() returns None for every symbol — the pre-plane abstain
    behavior bit-for-bit (the detectors skip at debug level, bonuses 0.0).
  - Own telemetry namespace: equity_oi_polled / equity_oi_poll_failed
    (emitted by the splice via the helpers below, never by the brains).
  - Own test file: tests/test_equity_oi_plane.py.

Doctrine (mirrors data/oi_history_feed.py):
  - Stale = abstain: a series whose newest point is older than 48h is
    never served (None). Thin (<6 points) is never served. Fail-CLOSED on
    every garbage input — a failed poll keeps the last good state; the
    plane NEVER fabricates a point.
  - Persistence: logs/equity_oi_history.json (atomic tmp+replace) written
    on each poll that ingested anything; restored at boot so a restart
    does not zero the detectors' z-score baselines.
"""
from __future__ import annotations

import json
import math
import os
import time
from collections import deque
from typing import Callable, Dict, List, Optional, Tuple

import structlog

logger = structlog.get_logger(__name__)

# ── Kill switch ───────────────────────────────────────────────────────────────

def equity_oi_plane_enabled() -> bool:
    """Env kill switch (EQUITY_OI_PLANE_ENABLED, default "true"). "false" =
    series() returns None for everything — today's abstain behavior
    bit-for-bit."""
    return os.environ.get("EQUITY_OI_PLANE_ENABLED", "true").strip().lower() in (
        "1", "true", "yes", "on")


# ── Telemetry contract (the splice emits exactly these) ───────────────────────

EVENT_POLLED = "equity_oi_polled"
EVENT_FAILED = "equity_oi_poll_failed"


# ── Constants ─────────────────────────────────────────────────────────────────

_DEQUE_CAP = 96               # 96 x 300s piggyback cadence ≈ 8h of history
_MIN_POINTS = 6               # below this a series is never served
_STALE_S = 48 * 3600.0        # newest point older than 48h = abstain (feed law)
_CACHE_NAME = "equity_oi_history.json"


# ── Wire parser (pure) ────────────────────────────────────────────────────────

def parse_mark_prices(payload) -> List[Tuple[str, float]]:
    """Normalize the mark-prices response to [(symbol, oi_float)].

    Accepts the Governor-verified bare JSON LIST and the {"data": [...]} /
    {"rows": [...]} wrappers — a shape surprise must never kill the plane.
    openInterest arrives as a STRING number; units are per-symbol consistent
    and the consumers are unit-invariant, so no conversion beyond float().
    One bad row kills only itself (one-bad-line doctrine)."""
    if isinstance(payload, dict):
        rows = payload.get("data") or payload.get("rows") or payload.get("result") or []
    elif isinstance(payload, (list, tuple)):
        rows = payload
    else:
        rows = []
    out: List[Tuple[str, float]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sym = row.get("symbol") or row.get("s")
        try:
            oi = float(row.get("openInterest", row.get("oi")))
        except (TypeError, ValueError):
            continue
        if not sym or not math.isfinite(oi) or oi < 0.0:
            continue
        out.append((str(sym).strip(), oi))
    return out


# ── The plane ─────────────────────────────────────────────────────────────────

class EquityOIPlane:
    """Zero-I/O per-symbol OI history brain.

    record(symbol, oi, ts_ms) ingests one point into the symbol's
    deque(maxlen=96); series(symbol) returns the oldest-first
    [(ts_ms, oi)] list, or None when the kill switch is off, the series is
    thin (<6 points), or the newest point is stale (>48h) — stale = abstain,
    never serve. snapshot()/restore() are the persistence contract."""

    def __init__(self,
                 log_dir: str = "logs",
                 time_fn: Callable[[], float] = time.time,
                 enabled_fn: Callable[[], bool] = equity_oi_plane_enabled):
        self._log_dir = log_dir
        self._time = time_fn
        self._enabled_fn = enabled_fn
        self._series: Dict[str, deque] = {}

    # -- ingest ------------------------------------------------------------

    def record(self, symbol, oi, ts_ms) -> bool:
        """One point. True when a NEW point was appended. Equal-ts refreshes
        in place (same poll re-delivered); older ts is ignored — the series
        is append-ordered, never trusted to the caller's order. Garbage is
        dropped individually (fail-closed, one bad point kills only itself)."""
        try:
            sym = str(symbol).strip()
            oi_f = float(oi)
            ts_f = float(ts_ms)
        except (TypeError, ValueError):
            return False
        if not sym or not math.isfinite(oi_f) or not math.isfinite(ts_f) \
                or oi_f < 0.0 or ts_f <= 0.0:
            return False
        dq = self._series.setdefault(sym, deque(maxlen=_DEQUE_CAP))
        if dq and ts_f <= dq[-1][0]:
            if ts_f == dq[-1][0]:
                dq[-1] = (ts_f, oi_f)          # same poll re-delivered
            return False
        dq.append((ts_f, oi_f))
        return True

    def ingest_rows(self, rows, ts_ms=None) -> int:
        """One piggybacked poll: ingest a batch at one timestamp (default:
        now). Items may be raw mark-price dicts (the funding-loop stash)
        or parsed (symbol, oi) pairs — both normalize through the same
        fail-closed path. Returns the new-point count — 0 on an
        empty/garbage batch, the last good state untouched."""
        ts = float(ts_ms) if ts_ms is not None else self._time() * 1000.0
        n = 0
        for item in (rows or []):
            try:
                if isinstance(item, dict):
                    pairs = parse_mark_prices([item])
                    if not pairs:
                        continue
                    sym, oi = pairs[0]
                else:
                    sym, oi = item[0], item[1]
                if self.record(sym, oi, ts):
                    n += 1
            except Exception:
                continue
        return n

    # -- read (the detectors' interface) ------------------------------------

    def series(self, symbol) -> Optional[List[Tuple[float, float]]]:
        """Oldest-first [(ts_ms, oi)] or None — kill switch off / unknown
        symbol / thin (<6 points) / stale (>48h) all abstain identically."""
        try:
            if not self._enabled_fn():
                return None
            dq = self._series.get(str(symbol).strip())
            if not dq or len(dq) < _MIN_POINTS:
                return None
            age_s = max(0.0, self._time() - dq[-1][0] / 1000.0)
            if age_s > _STALE_S:
                return None
            return list(dq)
        except Exception:
            return None

    def symbols(self) -> List[str]:
        return sorted(self._series.keys())

    def freshest_age_s(self) -> Optional[float]:
        """Age (seconds) of the newest point across all symbols; None when
        the plane is empty. Telemetry surface for equity_oi_polled."""
        newest = None
        for dq in self._series.values():
            if dq and (newest is None or dq[-1][0] > newest):
                newest = dq[-1][0]
        if newest is None:
            return None
        return round(max(0.0, self._time() - newest / 1000.0), 1)

    # -- persistence ---------------------------------------------------------

    def snapshot(self) -> dict:
        """JSON-serializable {symbol: [[ts_ms, oi], ...]} oldest-first."""
        return {sym: [[ts, oi] for ts, oi in dq]
                for sym, dq in self._series.items()}

    def restore(self, rows) -> int:
        """Rebuild from snapshot() output. Returns the symbol count restored
        BY THIS CALL. One bad symbol/row kills only itself; caps and ordering
        re-applied. Blank symbol keys are dropped like any other garbage."""
        if not isinstance(rows, dict):
            return 0
        restored = 0
        for sym, pts in rows.items():
            sym_s = str(sym).strip()
            if not sym_s or not isinstance(pts, (list, tuple)):
                continue
            clean = []
            for pt in pts:
                try:
                    ts_f, oi_f = float(pt[0]), float(pt[1])
                except (TypeError, ValueError, IndexError):
                    continue
                if math.isfinite(ts_f) and math.isfinite(oi_f) \
                        and oi_f >= 0.0 and ts_f > 0.0:
                    clean.append((ts_f, oi_f))
            if not clean:
                continue
            clean.sort(key=lambda t: t[0])
            self._series[sym_s] = deque(clean[-_DEQUE_CAP:],
                                        maxlen=_DEQUE_CAP)
            restored += 1
        return restored

    def _cache_path(self) -> str:
        return os.path.join(self._log_dir, _CACHE_NAME)

    def persist(self) -> None:
        """Atomic tmp+replace write. Best-effort — a persist failure must
        never kill the poll loop (last-good state keeps serving)."""
        try:
            os.makedirs(self._log_dir, exist_ok=True)
            tmp = self._cache_path() + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.snapshot(), f)
            os.replace(tmp, self._cache_path())
        except Exception as e:
            logger.warning("equity_oi_persist_failed", error=str(e)[:120])

    def load(self) -> int:
        """Restore from the persisted cache at boot. Returns the symbol
        count restored; 0 on any error (a corrupt cache = cold start, never
        a crash)."""
        try:
            with open(self._cache_path()) as f:
                d = json.load(f)
            return self.restore(d)
        except Exception:
            return 0


# ── Splice helper (telemetry-owning, one call per piggybacked poll) ───────────

def ingest_poll(plane: EquityOIPlane, rows) -> int:
    """One poll: ingest a batch (raw mark-price dicts or parsed pairs),
    persist when anything landed, emit equity_oi_polled exactly once.
    Returns the new-point count. Never raises — a poll failure keeps the
    last good state; the caller owns equity_oi_poll_failed when its own
    fetch failed (it has the error string; the plane never fabricates)."""
    try:
        n = plane.ingest_rows(rows)
        if n:
            plane.persist()
        logger.info(EVENT_POLLED,
                    symbols=len(plane.symbols()),
                    ingested=n,
                    freshest_age_s=plane.freshest_age_s())
        return n
    except Exception as e:
        logger.warning(EVENT_FAILED, error=str(e)[:120])
        return 0
