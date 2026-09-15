"""Stock-carry shadow plane — data adapters + paper ledger for the B4
register consumer (register Stocks 1 & 2, 2026-09-15; closes audit P0-1 /
P0-2 / P1-4).

The brains live in intelligence/stock_carry.py (SHADOW_ONLY=True by
declaration). This module owns everything the brains deliberately do NOT:
  - SoDEX 4h kline parsing (the bot's 1m feed plane never serves the ORCL
    volume/range/breakout legs — SoDEX REST serves 4h bars directly,
    probe-verified 2026-09-15: GET /markets/{sym}/klines?interval=4h
    returns newest-first rows with string numerics {"t","o","h","l","c",
    "v","q","n"}; a bar is CLOSED only when open_time + 4h <= now).
  - Hourly funding series construction over the trailing 12 UTC hours
    (funding_history.json records; one slot per UTC hour, None for a dark
    hour — the brain's streak semantics treat None as unknown, never zero).
  - Basis z trailing series from the register prints file
    (logs/stock_basis.jsonl — one-bad-line doctrine).
  - Trade direction derivation (the brains emit no direction — P1-4):
    ORCL is strictly long (the breakout leg is upward by spec); META rides
    the receiving side of funding (positive rate -> shorts receive).
  - Shadow state machine: open/close row construction, direction-adjusted
    pnl_bps, funding_bps accrued over full hours held with sign by
    receiving side, atomic state persistence.

Decision logic is pure (zero I/O); ledger persistence helpers follow the
intelligence/pair_spread.py precedent (append + atomic-replace, fsync).
None = dark; entries fail closed; nothing is ever fabricated.
"""

from __future__ import annotations

import json
import os
from typing import Optional, Sequence

HOUR_MS = 3_600_000
BAR_4H_MS = 4 * HOUR_MS
BASELINE_BARS = 20          # vol/range baseline window (bars before the latest)
CLOSES_TAIL = 21            # last-21 closes (brain breakout_bars + 1)
FUNDING_HOURS = 12
BASIS_Z_TAIL = 10


# ── 4h kline plane ───────────────────────────────────────────────────────────

def parse_klines_4h(rows, now_ms: int) -> list:
    """SoDEX 4h kline rows (newest-first, string numerics) -> ascending list
    of closed (open_time, open, high, low, close, volume) tuples. The forming
    bar is excluded: closed iff open_time + 4h <= now_ms (the boundary bar
    whose close time IS now counts as closed). Malformed rows are skipped —
    one bad row kills one bar, never the plane."""
    bars = []
    for row in rows or []:
        try:
            ot = int(row["t"])
            o, h = float(row["o"]), float(row["h"])
            l, c = float(row["l"]), float(row["c"])
            v = float(row["v"])
        except (KeyError, TypeError, ValueError):
            continue
        if ot + BAR_4H_MS > now_ms:
            continue            # forming bar — never evidence
        bars.append((ot, o, h, l, c, v))
    bars.sort(key=lambda b: b[0])
    return bars


def ohlc_legs(bars: Sequence) -> dict:
    """Brain inputs from closed 4h bars. Baselines are the mean of the 20
    closed bars BEFORE the latest; closes are the last 21. Any insufficiency
    -> the relevant fields None (dark), never fabricated."""
    bs = list(bars or [])
    vol_now = bs[-1][5] if bs else None
    range_now = (bs[-1][2] - bs[-1][3]) if bs else None
    if len(bs) >= BASELINE_BARS + 1:
        prior = bs[-(BASELINE_BARS + 1):-1]
        vol_baseline = sum(b[5] for b in prior) / BASELINE_BARS
        range_baseline = sum(b[2] - b[3] for b in prior) / BASELINE_BARS
    else:
        vol_baseline = None
        range_baseline = None
    closes = [b[4] for b in bs[-CLOSES_TAIL:]]
    return {"vol_now": vol_now, "vol_baseline": vol_baseline,
            "range_now": range_now, "range_baseline": range_baseline,
            "closes": closes}


def newest_bar_age_ms(bars: Sequence, now_ms: int) -> Optional[int]:
    """Age of the newest closed bar's OPEN time; None when no bars."""
    if not bars:
        return None
    return int(now_ms) - int(bars[-1][0])


# ── Funding plane ────────────────────────────────────────────────────────────

def _hour_buckets(records) -> dict:
    """hour_bucket -> (timestamp_ms, rate); same-hour dedup keeps the LATEST
    record (the producer emits one record per UTC hour post-fix, but a
    pre-fix file can carry several — the freshest print is the truth)."""
    buckets: dict = {}
    for r in records or []:
        try:
            ts = int(r["timestamp_ms"])
            rate = float(r["rate"])
        except (KeyError, TypeError, ValueError):
            continue
        b = ts // HOUR_MS
        if b not in buckets or ts >= buckets[b][0]:
            buckets[b] = (ts, rate)
    return buckets


def hourly_funding_series(records, now_ms: int,
                          hours: int = FUNDING_HOURS) -> list:
    """Ascending list of Optional[float], one slot per UTC hour over the
    trailing `hours` hours ending at the current bucket. A missing hour is
    None — a dark print, which breaks streaks per the brain's semantics."""
    buckets = _hour_buckets(records)
    cur = int(now_ms) // HOUR_MS
    return [buckets.get(cur - i, (0, None))[1]
            for i in range(hours - 1, -1, -1)]


# ── Register prints plane (logs/stock_basis.jsonl) ──────────────────────────

def _symbol_prints(jsonl_lines, symbol: str) -> list:
    rows = []
    for line in jsonl_lines or []:
        try:
            rec = json.loads(line)
        except (TypeError, ValueError):
            continue            # one-bad-line doctrine
        if not isinstance(rec, dict) or rec.get("symbol") != symbol:
            continue
        try:
            rec_ts = int(rec["ts"])
        except (KeyError, TypeError, ValueError):
            continue
        rows.append((rec_ts, rec))
    rows.sort(key=lambda x: x[0])
    return rows


def basis_z_series(jsonl_lines, symbol: str,
                   tail: int = BASIS_Z_TAIL) -> list:
    """Ascending list of Optional[float] z prints for the symbol (None z
    stays None — thin history is dark, not zero)."""
    return [rec.get("z") for _, rec in _symbol_prints(jsonl_lines, symbol)[-tail:]]


def latest_perp_mark(jsonl_lines, symbol: str) -> Optional[float]:
    """Newest perp_mark print for the symbol — the META pricing plane
    (funding-only brain, no klines). None when the plane is dark."""
    rows = _symbol_prints(jsonl_lines, symbol)
    for _, rec in reversed(rows):
        try:
            px = float(rec.get("perp_mark"))
            if px > 0:
                return px
        except (TypeError, ValueError):
            continue
    return None


def latest_spread_bps(jsonl_lines, symbol: str) -> Optional[float]:
    """Newest spread_bps print (best-effort orderbook probe). None = no
    opinion — the brain's spread kill leg abstains on a dark book."""
    rows = _symbol_prints(jsonl_lines, symbol)
    for _, rec in reversed(rows):
        v = rec.get("spread_bps")
        if v is None:
            return None         # newest print is the opinion; dark is dark
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    return None


# ── Direction (P1-4 — the brains emit none) ──────────────────────────────────

def direction_for(strategy: str, trigger_rate: Optional[float] = None) -> str:
    """ORCL: strictly long (the breakout leg is close > prior-20-bar high —
    an upward-only structure). META: the receiving side of funding — a
    positive trigger rate means shorts receive, so the carry seat is short;
    non-positive -> long."""
    if strategy == "orcl":
        return "long"
    if strategy == "meta":
        return "short" if (trigger_rate is not None and trigger_rate > 0) else "long"
    raise ValueError(f"unknown strategy {strategy!r}")


# ── Shadow state machine ─────────────────────────────────────────────────────

def pnl_bps(direction: str, entry_px: Optional[float],
            exit_px: Optional[float]) -> Optional[float]:
    """Direction-adjusted price return in bps on the entry base. None when
    either price is dark/invalid (fail closed — a phantom pnl is worse than
    none)."""
    try:
        e, x = float(entry_px), float(exit_px)
    except (TypeError, ValueError):
        return None
    if e <= 0 or x <= 0:
        return None
    r = (x / e - 1.0) * 1e4
    return round(r if direction == "long" else -r, 4)


def funding_bps_accrued(direction: str, records, opened_ts_ms: int,
                        closed_ts_ms: int) -> float:
    """Funding carry in bps over the FULL UTC hours held: sum of hourly
    rates over hour buckets entirely inside [opened, closed] x 1e4, signed
    by the receiving side (positive rate -> shorts receive, longs pay). A
    dark hour contributes nothing (unknown is never claimed)."""
    try:
        opened, closed = int(opened_ts_ms), int(closed_ts_ms)
    except (TypeError, ValueError):
        return 0.0
    if closed <= opened:
        return 0.0
    sign = 1.0 if direction == "short" else -1.0
    total = 0.0
    for b, (_, rate) in _hour_buckets(records).items():
        if b * HOUR_MS >= opened and (b + 1) * HOUR_MS <= closed:
            total += rate
    return round(sign * total * 1e4, 6)


def open_shadow(strategy: str, symbol: str, direction: str, entry_px: float,
                now: float, context: Optional[dict] = None) -> dict:
    return {
        "id": f"stock_carry_{strategy}_{int(now * 1000)}",
        "event": "stock_carry_shadow_open", "ts": now,
        "opened_ms": int(now * 1000),
        "strategy": strategy, "symbol": symbol, "direction": direction,
        "entry_px": entry_px,
        "context": context or {},
    }


def close_shadow(row: dict, reason: str, exit_px: float,
                 pnl: Optional[float], funding_bps: float,
                 now: float) -> dict:
    net = round((pnl or 0.0) + (funding_bps or 0.0), 4) if pnl is not None else None
    return {
        "id": row["id"], "event": "stock_carry_shadow_close", "ts": now,
        "strategy": row["strategy"], "symbol": row["symbol"],
        "direction": row["direction"], "reason": reason,
        "entry_px": row["entry_px"], "exit_px": exit_px,
        "age_hours": round((now - float(row["ts"])) / 3600.0, 2),
        "pnl_bps": pnl, "funding_bps": funding_bps, "net_bps": net,
    }


def open_slot(state: dict, strategy: str) -> Optional[dict]:
    row = (state.get("open") or {}).get(strategy)
    return row if isinstance(row, dict) else None


def with_open(state: dict, row: dict) -> dict:
    st = {"open": dict(state.get("open") or {})}
    st["open"][row["strategy"]] = row
    return st


def with_closed(state: dict, strategy: str) -> dict:
    st = {"open": dict(state.get("open") or {})}
    st["open"].pop(strategy, None)
    return st


# ── Ledger persistence (pair_spread precedent) ───────────────────────────────

def append_jsonl(path: str, row: dict) -> None:
    """Append one JSON line + fsync — the lifecycle ledger is low-frequency
    (a few rows/week), atomic-rename buys nothing here."""
    with open(path, "a") as f:
        f.write(json.dumps(row) + "\n")
        f.flush()
        os.fsync(f.fileno())


def atomic_write_json(path: str, payload: dict) -> None:
    """tmp + os.replace — a torn state file must never read as flat."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, path)


def read_state(path: str) -> dict:
    """Tolerant state load; missing/corrupt -> flat book."""
    try:
        with open(path) as f:
            doc = json.load(f)
        if isinstance(doc, dict) and isinstance(doc.get("open"), dict):
            return doc
    except Exception:
        pass
    return {"open": {}}
