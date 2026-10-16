#!/usr/bin/env python3
"""guardian_replay — causal, cost-aware replay of journaled trades vs MFE-floor exit policies.

CAUSALITY RULE (the whole point — auditors start here):
  For each bar, in strict order:
    1. EXIT CHECK against state computed from PRIOR bars only
       (floor armed from prior running MFE; partial target from entry geometry).
    2. UPDATE running MFE with THIS bar's high/low.
    3. (Re)arm / tighten the floor from the updated MFE.
  Consequence: a bar that simultaneously prints a new MFE high AND dips below the
  floor that MFE would imply can NOT trigger a fill — the exit check saw the
  pre-bar floor (or no floor at all). No lookahead. Floor fills happen at the
  earliest on the bar AFTER the arming bar.

  Fill realism: a floor breach fills at min(bar_open, floor) for longs
  (max(bar_open, floor) for shorts) — gap-throughs fill at the open, never
  better than the floor — then slippage is applied adversely. Floor/stop fills
  are MARKET: taker fee + slippage. Limit TP / partial fills: maker fee.

FEES: repo constants from core/fee_engine.py tier-0 (PERPS_TAKER[0]=4.0bps,
PERPS_MAKER[0]=1.2bps). Entry fee is assumed taker (conservative; documented in
output metadata). Control pnl comes from the journal (pnl_net_usd, fallback
pnl_usd) and is already net of the real fees ARIA paid.

CACHE: klines persist to {logs_dir}/replay_cache/{symbol}_{interval}_{exitdate}.json
(JSONL, one [t,o,h,l,c] per line, atomic tmp+rename writes, one-bad-line
tolerance on read). This cache is the reusable asset for future replays.

Best-effort doctrine: per-symbol/per-trade failures are logged and skipped;
the CLI exits 0 always.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import ssl
import statistics
import sys
import tempfile
import urllib.request
from datetime import datetime, timedelta, timezone


def _ssl_context() -> ssl.SSLContext:
    """certifi-backed context when available (macOS venvs ship no system certs)."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_SSL_CTX = _ssl_context()

# ── Fee constants (repo: core/fee_engine.py tier-0 schedule) ──────────────────
TAKER_BPS = 4.0   # PERPS_TAKER[0] = 0.00040
MAKER_BPS = 1.2   # PERPS_MAKER[0] = 0.00012
FEE_SOURCE = "core/fee_engine.py PERPS_TAKER[0]=0.00040 / PERPS_MAKER[0]=0.00012 (tier 0)"

SODEX_KLINES_URL = "https://mainnet-gw.sodex.dev/api/v1/perps/markets/{symbol}/klines"
INTERVAL_MS = {"1m": 60_000, "5m": 300_000}
HOLD_8H_MS = 8 * 3600 * 1000
BOOTSTRAP_SEED = 42
BOOTSTRAP_RESAMPLES = 10_000


# ── Journal loading ───────────────────────────────────────────────────────────
def load_journals(logs_dir: str, days: int, today: datetime | None = None) -> list[dict]:
    """Load trailing `days` journal files. Missing files are skipped."""
    today = today or datetime.now(timezone.utc)
    out: list[dict] = []
    for i in range(days):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        path = os.path.join(logs_dir, f"trade_journal_{d}.json")
        try:
            with open(path) as fh:
                recs = json.load(fh)
            if isinstance(recs, list):
                for r in recs:
                    if isinstance(r, dict):
                        r["_source_file"] = path
                        out.append(r)
        except (OSError, json.JSONDecodeError):
            continue
    return out


def window_bounds(since: str | None, until: str | None) -> tuple[float | None, float | None]:
    """UTC date strings -> [since_ms, until_ms) epoch-ms bounds. `until` is INCLUSIVE of its day.

    WHY THIS EXISTS (#82): `--days N` selects N journal FILES, and each file is a rolling
    `_MAX_IN_MEMORY=500` buffer (trade_journal.py:563-568), not a day. Measured 2026-10-16:
    `--days 2` returns 99 deduped closes spanning NINE close days (10-08..10-16). The window
    is therefore a property of trade VOLUME, not of the flag. Declare it on close time."""
    def _p(s: str | None) -> float | None:
        if not s:
            return None
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000.0
    lo, hi = _p(since), _p(until)
    return lo, (hi + 86_400_000.0 if hi is not None else None)


def apply_window(records: list[dict], since_ms: float | None,
                 until_ms: float | None) -> list[dict]:
    """Close-time predicate on the row's OWN closed_at_ms (the #66 patch, same shape)."""
    out = records
    if since_ms is not None:
        out = [r for r in out if float(r["closed_at_ms"]) >= since_ms]
    if until_ms is not None:
        out = [r for r in out if float(r["closed_at_ms"]) < until_ms]
    return out


def closes_per_day(records: list[dict]) -> dict[str, int]:
    """The census that makes the window auditable — never publish an n without it."""
    hist: dict[str, int] = {}
    for r in records:
        d = datetime.fromtimestamp(float(r["closed_at_ms"]) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        hist[d] = hist.get(d, 0) + 1
    return dict(sorted(hist.items()))


def filter_closed(records: list[dict]) -> list[dict]:
    """Real closes only: outcome not in (None,'rejected'), closed_at_ms, entry>0."""
    out = []
    for r in records:
        if r.get("outcome") in (None, "rejected"):
            continue
        if not r.get("closed_at_ms"):
            continue
        if (r.get("entry_price") or 0) <= 0:
            continue
        if r.get("direction") not in ("long", "short"):
            continue
        out.append(r)
    return out


def dedup(records: list[dict]) -> list[dict]:
    """Dedup on (entry_id, closed_at_ms) — first occurrence wins."""
    seen: set[tuple] = set()
    out = []
    for r in records:
        key = (r.get("entry_id"), r.get("closed_at_ms"))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def control_pnl(trade: dict) -> float:
    pnl = trade.get("pnl_net_usd")
    if pnl is None:
        pnl = trade.get("pnl_usd") or 0.0
    return float(pnl)


def trade_size(trade: dict) -> float | None:
    sz = trade.get("position_size")
    if sz and sz > 0:
        return float(sz)
    margin = trade.get("initial_margin")
    lev = trade.get("leverage")
    entry = trade.get("entry_price")
    if margin and lev and entry:
        return float(margin) * float(lev) / float(entry)
    return None


def hold_class(hold_ms: float) -> str:
    if hold_ms < 6 * 60_000:
        return "<6min"
    if hold_ms <= 60 * 60_000:
        return "6-60min"
    return ">60min"


# ── Kline cache ───────────────────────────────────────────────────────────────
def cache_path(cache_dir: str, symbol: str, interval: str, exit_date: str) -> str:
    safe = symbol.replace("/", "-")
    return os.path.join(cache_dir, f"{safe}_{interval}_{exit_date}.json")


def _coerce_bar(item) -> list[float] | None:
    try:
        t, o, h, l, c = item[:5]
        return [float(t), float(o), float(h), float(l), float(c)]
    except (TypeError, ValueError, IndexError, KeyError):
        return None


def load_cache(path: str) -> list[list[float]]:
    """Tolerant read: whole-file JSON array OR JSONL; one bad line never kills the file."""
    try:
        with open(path) as fh:
            text = fh.read()
    except OSError:
        return []
    items: list = []
    try:
        data = json.loads(text)
        if isinstance(data, list):
            items = data
    except json.JSONDecodeError:
        items = []
    if not items:  # JSONL fallback (also the salvage path for one bad line)
        for line in text.splitlines():
            line = line.strip().rstrip(",")
            if not line or line in ("[", "]"):
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # the one-bad-line tolerance
    bars = []
    seen_t: set[float] = set()
    for it in items:
        if isinstance(it, (list, tuple)):
            b = _coerce_bar(it)
        elif isinstance(it, dict):
            b = _coerce_bar([it.get("t"), it.get("o"), it.get("h"), it.get("l"), it.get("c")])
        else:
            b = None
        if b is None or b[0] in seen_t:
            continue
        seen_t.add(b[0])
        bars.append(b)
    bars.sort(key=lambda b: b[0])
    return bars


def save_cache(path: str, bars: list[list[float]]) -> None:
    """Atomic write: tmp file in same dir + os.replace. JSONL, one bar per line."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            for b in bars:
                fh.write(json.dumps([b[0], b[1], b[2], b[3], b[4]]) + "\n")
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def merge_bars(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    by_t = {bar[0]: bar for bar in a}
    for bar in b:
        by_t.setdefault(bar[0], bar)
    return [by_t[t] for t in sorted(by_t)]


# ── Kline fetch (SoDEX REST) ──────────────────────────────────────────────────
def fetch_klines(symbol: str, interval: str, start_ms: float, end_ms: float,
                 limit: int = 1000, max_pages: int = 10, timeout: float = 20.0
                 ) -> tuple[list[list[float]], list[str]]:
    """Newest-first API; page backwards via the verified `endTime` param
    (probed 2026-10-16: end/start/to/before are ignored; endTime windows as
    (endTime - limit*step, endTime])."""
    bars: list[list[float]] = []
    gaps: list[str] = []
    seen: set[float] = set()
    step = INTERVAL_MS[interval]
    end_param = int(end_ms + step)
    for _ in range(max_pages):
        url = (SODEX_KLINES_URL.format(symbol=symbol)
               + f"?interval={interval}&limit={limit}&endTime={end_param}")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "aria-guardian-replay/1.0"})
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
                payload = json.loads(resp.read().decode())
        except Exception as exc:  # best-effort: log and stop
            gaps.append(f"{symbol}:{interval}: fetch error: {exc}")
            break
        data = payload.get("data") or []
        parsed = []
        for d in data:
            if isinstance(d, dict):
                b = _coerce_bar([d.get("t"), d.get("o"), d.get("h"), d.get("l"), d.get("c")])
                if b:
                    parsed.append(b)
        if not parsed:
            break
        new = [b for b in parsed if b[0] not in seen and b[0] <= end_param]
        for b in new:
            seen.add(b[0])
        bars.extend(new)
        oldest = min(b[0] for b in parsed)
        if oldest <= start_ms:
            break
        if not new or len(parsed) < limit:
            if oldest > start_ms:
                gaps.append(f"{symbol}:{interval}: span not fully covered "
                            f"(oldest={int(oldest)} > needed={int(start_ms)})")
            break
        end_param = int(oldest) - step
    bars.sort(key=lambda b: b[0])
    return bars, gaps


def get_bars(symbol: str, interval: str, start_ms: float, end_ms: float,
             cache_dir: str, exit_date: str, fetcher=fetch_klines
             ) -> tuple[list[list[float]], list[str]]:
    """Cache-first: reuse when the cached series covers the span, fetch-merge otherwise."""
    path = cache_path(cache_dir, symbol, interval, exit_date)
    cached = load_cache(path)
    step = INTERVAL_MS[interval]
    covers = bool(cached) and cached[0][0] <= start_ms and cached[-1][0] + step >= end_ms
    gaps: list[str] = []
    if covers:
        bars = cached
    else:
        fetched, gaps = fetcher(symbol, interval, start_ms, end_ms)
        bars = merge_bars(cached, fetched) if cached else fetched
        if fetched:
            save_cache(path, bars)
    out = [b for b in bars if b[0] <= end_ms and b[0] + step > start_ms]
    if not out:
        gaps.append(f"{symbol}:{interval}: no bars for span {int(start_ms)}-{int(end_ms)}")
    return out, gaps


# ── Policy replay (CAUSAL — see module docstring) ─────────────────────────────
def replay_floor(bars: list[list[float]], entry: float, direction: str,
                 r: float, f: float, slippage_bps: float,
                 fee_buffer_bps: float = 0.0) -> dict:
    """floor(r, f): arm once running MFE% >= r; floor = entry*(1 +/- (f*mfe% + fee_buffer)/100);
    tighten-only. Exit check uses ONLY prior-bar state (step 1 before steps 2-3).

    Returns dict: filled, fill_price (slippage-adjusted), fill_idx, armed, mfe_pct,
    trace (per-bar post-update state, for audit/tests)."""
    is_long = direction == "long"
    slip = slippage_bps / 1e4
    mfe = 0.0
    armed = False
    floor: float | None = None
    trace: list[dict] = []
    for i, b in enumerate(bars):
        _t, o, h, l, _c = b
        # 1) EXIT CHECK against PRIOR state — never this bar's excursion
        if armed and floor is not None:
            breach = (l <= floor) if is_long else (h >= floor)
            if breach:
                base = min(o, floor) if is_long else max(o, floor)
                fill = base * (1 - slip) if is_long else base * (1 + slip)
                trace.append({"i": i, "mfe": mfe, "armed": armed, "floor": floor,
                              "fill": fill, "filled": True})
                return {"filled": True, "fill_price": fill, "fill_idx": i,
                        "armed": True, "mfe_pct": mfe, "trace": trace}
        # 2) UPDATE running MFE with this bar
        exc = ((h - entry) / entry * 100.0) if is_long else ((entry - l) / entry * 100.0)
        if exc > mfe:
            mfe = exc
        # 3) (Re)arm / tighten — takes effect from the NEXT bar's exit check
        if mfe >= r:
            armed = True
            pct = f * mfe + fee_buffer_bps / 100.0  # bps -> percent
            new_floor = entry * (1 + pct / 100.0) if is_long else entry * (1 - pct / 100.0)
            if floor is None:
                floor = new_floor
            else:
                floor = max(floor, new_floor) if is_long else min(floor, new_floor)
        trace.append({"i": i, "mfe": mfe, "armed": armed, "floor": floor, "filled": False})
    return {"filled": False, "fill_price": None, "fill_idx": None,
            "armed": armed, "mfe_pct": mfe, "trace": trace}


def replay_partial(bars: list[list[float]], entry: float, direction: str,
                   tp1: float, frac_of_tp1: float, size_frac: float) -> dict:
    """partial_tp: first bar touching entry + frac_of_tp1*(tp1-entry) banks size_frac
    AT THE TOUCH PRICE (maker fee). Target is entry geometry — no lookahead possible."""
    target = entry + (tp1 - entry) * frac_of_tp1
    is_long = direction == "long"
    for i, b in enumerate(bars):
        _t, _o, h, l, _c = b
        if (h >= target) if is_long else (l <= target):
            return {"touched": True, "target": target, "touch_idx": i,
                    "banked_frac": size_frac}
    return {"touched": False, "target": target, "touch_idx": None, "banked_frac": 0.0}


def tp1_touched(bars: list[list[float]], direction: str, tp1: float) -> bool:
    if not tp1 or tp1 <= 0:
        return False
    is_long = direction == "long"
    return any((b[2] >= tp1) if is_long else (b[3] <= tp1) for b in bars)


def exit_bar_close(bars: list[list[float]], closed_at_ms: float) -> float | None:
    closes = [b[4] for b in bars if b[0] <= closed_at_ms]
    return closes[-1] if closes else None


# ── PnL accounting ────────────────────────────────────────────────────────────
def fill_pnl(entry: float, exit_price: float, size: float, direction: str,
             exit_fee_bps: float, entry_fee_bps: float) -> float:
    """Gross price pnl minus exit fee (on exit notional) minus entry fee (on entry notional)."""
    sign = 1.0 if direction == "long" else -1.0
    gross = sign * (exit_price - entry) * size
    return gross - exit_price * size * exit_fee_bps / 1e4 - entry * size * entry_fee_bps / 1e4


def signed_pct(entry: float, price: float, direction: str) -> float:
    sign = 1.0 if direction == "long" else -1.0
    return sign * (price - entry) / entry * 100.0


# ── Policy evaluation over one trade ──────────────────────────────────────────
def eval_trade_policy(policy: dict, trade: dict, bars: list[list[float]],
                      slippage_bps: float) -> dict | None:
    """Returns per-trade result row or None if unevaluable (logged by caller)."""
    entry = float(trade["entry_price"])
    direction = trade["direction"]
    c_pnl = control_pnl(trade)
    closed_at = float(trade["closed_at_ms"])
    size = trade_size(trade)
    kind = policy["kind"]

    realized_exit_close = exit_bar_close(bars, closed_at)
    realized_pct = (signed_pct(entry, realized_exit_close, direction)
                    if realized_exit_close else None)

    if kind == "control":
        return {"pnl": c_pnl, "control_pnl": c_pnl, "fills": 0, "armed": None,
                "floor_filled": None, "mfe_pct": None, "realized_pct": realized_pct}

    if size is None or size <= 0:
        return None  # cannot price policy fills

    if kind in ("floor", "breakeven_arm"):
        fee_buffer = MAKER_BPS if kind == "breakeven_arm" else 0.0
        res = replay_floor(bars, entry, direction, policy["r"], policy.get("f", 0.0),
                           slippage_bps, fee_buffer_bps=fee_buffer)
        if res["filled"]:
            pnl = fill_pnl(entry, res["fill_price"], size, direction,
                           exit_fee_bps=TAKER_BPS, entry_fee_bps=TAKER_BPS)
            realized = signed_pct(entry, res["fill_price"], direction)
            fills = 1
        else:
            pnl = c_pnl  # control tail: never armed or never breached
            realized = realized_pct
            fills = 0
        return {"pnl": pnl, "control_pnl": c_pnl, "fills": fills,
                "armed": res["armed"], "floor_filled": res["filled"],
                "mfe_pct": res["mfe_pct"], "realized_pct": realized}

    if kind == "partial_tp":
        tp1 = float(trade.get("tp1_price") or 0.0)
        if tp1 <= 0:
            return None
        res = replay_partial(bars, entry, direction, tp1,
                             policy["frac_of_tp1"], policy["size_frac"])
        if res["touched"]:
            frac = policy["size_frac"]
            banked = fill_pnl(entry, res["target"], size * frac, direction,
                              exit_fee_bps=MAKER_BPS, entry_fee_bps=0.0)
            # remainder rides the control exit (scaled journal pnl — documented approx)
            pnl = banked + c_pnl * (1.0 - frac)
        else:
            pnl = c_pnl
        return {"pnl": pnl, "control_pnl": c_pnl, "fills": 1 if res["touched"] else 0,
                "armed": None, "floor_filled": None, "mfe_pct": None,
                "realized_pct": realized_pct, "partial_touched": res["touched"]}

    if kind == "joint":
        # TWO-RUNG BANK (the Governor's question): partial at frac_of_tp1 banks size_frac,
        # the REMAINDER rides the MFE floor. Ordering is causal — whichever fires first owns
        # the size it claims. Entry fee is charged ONCE on full notional in every branch
        # (conservative: the standalone partial cell charges none on its banked leg).
        tp1 = float(trade.get("tp1_price") or 0.0)
        if tp1 <= 0:
            return None
        frac = policy["size_frac"]
        fl = replay_floor(bars, entry, direction, policy["r"], policy["f"], slippage_bps)
        pt = replay_partial(bars, entry, direction, tp1, policy["frac_of_tp1"], frac)
        banked = pt["touched"] and (not fl["filled"] or pt["touch_idx"] < fl["fill_idx"])
        if not banked:
            # the floor reached the size first (or nothing fired): the pure-floor outcome
            pnl = (fill_pnl(entry, fl["fill_price"], size, direction, TAKER_BPS, TAKER_BPS)
                   if fl["filled"] else c_pnl)
            realized = (signed_pct(entry, fl["fill_price"], direction)
                        if fl["filled"] else realized_pct)
        else:
            sign = 1.0 if direction == "long" else -1.0
            bank = (sign * (pt["target"] - entry) * size * frac
                    - pt["target"] * size * frac * MAKER_BPS / 1e4)
            entry_fee = entry * size * TAKER_BPS / 1e4
            if fl["filled"]:
                rest = (sign * (fl["fill_price"] - entry) * size * (1.0 - frac)
                        - fl["fill_price"] * size * (1.0 - frac) * TAKER_BPS / 1e4)
                pnl, realized = bank + rest - entry_fee, signed_pct(entry, fl["fill_price"], direction)
            else:  # remainder rides the control exit; c_pnl already carries (1-frac) of entry fee
                pnl, realized = bank - entry_fee * frac + c_pnl * (1.0 - frac), realized_pct
        return {"pnl": pnl, "control_pnl": c_pnl,
                "fills": (1 if banked else 0) + (1 if fl["filled"] else 0),
                "armed": fl["armed"], "floor_filled": fl["filled"],
                "mfe_pct": fl["mfe_pct"], "realized_pct": realized,
                "partial_touched": banked}
    return None


# ── Metrics ───────────────────────────────────────────────────────────────────
def bootstrap_ci(deltas: list[float], resamples: int = BOOTSTRAP_RESAMPLES,
                 seed: int = BOOTSTRAP_SEED) -> tuple[float, float]:
    """95% percentile CI on the mean per-trade delta. Deterministic for fixed seed."""
    if not deltas:
        return (0.0, 0.0)
    rng = random.Random(seed)
    n = len(deltas)
    means = []
    for _ in range(resamples):
        s = sum(deltas[rng.randrange(n)] for _ in range(n))
        means.append(s / n)
    means.sort()
    lo = means[int(0.025 * resamples)]
    hi = means[min(resamples - 1, int(0.975 * resamples))]
    return (lo, hi)


def max_seq_drawdown(pnls: list[float]) -> float:
    peak = cum = 0.0
    mdd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    return mdd


def _split(rows: list[dict], key_fn) -> dict:
    out: dict[str, dict] = {}
    for row in rows:
        k = key_fn(row)
        bucket = out.setdefault(k, {"n": 0, "net_pnl": 0.0})
        bucket["n"] += 1
        bucket["net_pnl"] += row["pnl"]
    for b in out.values():
        b["net_pnl"] = round(b["net_pnl"], 6)
    return out


def compute_metrics(policy_name: str, rows: list[dict]) -> dict:
    """rows: per-trade dicts with pnl, control_pnl, fills, armed, floor_filled,
    mfe_pct, realized_pct, tp1_touch, exit_reason, hold_ms, closed_at_ms."""
    n = len(rows)
    pnls = [r["pnl"] for r in rows]
    deltas = [r["pnl"] - r["control_pnl"] for r in rows]
    ordered = sorted(rows, key=lambda r: r["closed_at_ms"])
    armed_rows = [r for r in rows if r.get("armed")]
    givebacks = [(r["mfe_pct"] - r["realized_pct"]) for r in armed_rows
                 if r.get("mfe_pct") is not None and r.get("realized_pct") is not None]
    floor_fills = [r for r in rows if r.get("floor_filled")]
    ci_lo, ci_hi = bootstrap_ci(deltas)
    return {
        "policy": policy_name,
        "n": n,
        "net_pnl": round(sum(pnls), 6),
        "mean_pnl": round(statistics.fmean(pnls), 6) if n else 0.0,
        "median_pnl": round(statistics.median(pnls), 6) if n else 0.0,
        "delta_vs_control": round(sum(deltas), 6),
        "delta_ci95": [round(ci_lo, 6), round(ci_hi, 6)],
        "expectancy": round(statistics.fmean(pnls), 6) if n else 0.0,
        "floor_fill_rate": round(len(floor_fills) / n, 4) if n else 0.0,
        "arm_rate": round(len(armed_rows) / n, 4) if n else 0.0,
        "tp1_touch_rate": round(sum(1 for r in rows if r.get("tp1_touch")) / n, 4) if n else 0.0,
        "giveback_after_arm_pct": round(statistics.fmean(givebacks), 4) if givebacks else None,
        "turnover_fills": sum(r["fills"] for r in rows),
        "max_seq_drawdown": round(max_seq_drawdown([r["pnl"] for r in ordered]), 6),
        "per_exit_reason": _split(rows, lambda r: r["exit_reason"]),
        "per_hold_class": _split(rows, lambda r: hold_class(r["hold_ms"])),
    }


# ── Policy grid ───────────────────────────────────────────────────────────────
def default_grid() -> list[dict]:
    grid = [{"kind": "control", "name": "control"}]
    for r in (0.30, 0.40, 0.50, 0.75):
        for f in (0.25, 0.50):
            grid.append({"kind": "floor", "name": f"floor_r{r}_f{f}", "r": r, "f": f})
    for r in (0.30, 0.50):
        grid.append({"kind": "breakeven_arm", "name": f"breakeven_arm_r{r}", "r": r, "f": 0.0})
    grid.append({"kind": "partial_tp", "name": "partial_tp_0.5tp1_50pct",
                 "frac_of_tp1": 0.5, "size_frac": 0.5})
    for r in (0.30, 0.40):  # the two-rung bank: partial + floor on the remainder
        grid.append({"kind": "joint", "name": f"joint_r{r}_f0.5_ptp0.5tp1_50pct",
                     "r": r, "f": 0.50, "frac_of_tp1": 0.5, "size_frac": 0.5})
    return grid


# ── Main ──────────────────────────────────────────────────────────────────────
def choose_interval(hold_ms: float) -> str:
    return "1m" if hold_ms < HOLD_8H_MS else "5m"


def run(args) -> dict:
    logs_dir = args.logs_dir
    cache_dir = os.path.join(logs_dir, "replay_cache")
    records = filter_closed(dedup(load_journals(logs_dir, args.days)))
    n_loaded = len(records)
    since_ms, until_ms = window_bounds(getattr(args, "since", None), getattr(args, "until", None))
    records = apply_window(records, since_ms, until_ms)
    if args.symbols:
        wanted = {s.strip() for s in args.symbols.split(",") if s.strip()}
        records = [r for r in records if r.get("symbol") in wanted]

    gaps: list[str] = []
    bars_by_trade: dict[int, list[list[float]]] = {}
    kept: list[dict] = []
    for idx, tr in enumerate(records):
        sym = tr.get("symbol")
        hold = float(tr["closed_at_ms"]) - float(tr["timestamp_ms"])
        interval = choose_interval(hold)
        exit_date = datetime.fromtimestamp(float(tr["closed_at_ms"]) / 1000,
                                           tz=timezone.utc).strftime("%Y%m%d")
        try:
            bars, g = get_bars(sym, interval, float(tr["timestamp_ms"]),
                               float(tr["closed_at_ms"]), cache_dir, exit_date)
            gaps.extend(g)
        except Exception as exc:  # per-symbol failure: log and skip
            gaps.append(f"{sym}: kline failure: {exc}")
            continue
        if not bars:
            continue
        tr["_hold_ms"] = hold
        bars_by_trade[idx] = bars
        kept.append((idx, tr))

    grid = default_grid()
    per_policy: dict[str, list[dict]] = {p["name"]: [] for p in grid}
    for idx, tr in kept:
        bars = bars_by_trade[idx]
        tp1_touch = tp1_touched(bars, tr["direction"], float(tr.get("tp1_price") or 0.0))
        for pol in grid:
            try:
                row = eval_trade_policy(pol, tr, bars, args.slippage_bps)
            except Exception as exc:
                gaps.append(f"{tr.get('symbol')}:{pol['name']}: eval error: {exc}")
                continue
            if row is None:
                continue
            row.update({"exit_reason": tr.get("exit_reason") or "unknown",
                        "hold_ms": tr["_hold_ms"],
                        "closed_at_ms": float(tr["closed_at_ms"]),
                        "tp1_touch": tp1_touch,
                        "symbol": tr.get("symbol")})
            per_policy[pol["name"]].append(row)

    metrics = [compute_metrics(name, rows) for name, rows in per_policy.items()]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "date": today,
        "params": {"days": args.days, "logs_dir": logs_dir,
                   "symbols": args.symbols, "slippage_bps": args.slippage_bps,
                   "since": getattr(args, "since", None), "until": getattr(args, "until", None)},
        "window": {"declared": [getattr(args, "since", None), getattr(args, "until", None)],
                   "n_loaded": n_loaded, "n_in_window": len(records),
                   "n_evaluated": len(kept),
                   "closes_per_day": closes_per_day(records),
                   "note": "--days selects FILES (rolling 500-row buffers), NOT a window. "
                           "n_in_window is the close-time population; read it, never n_loaded."},
        "fee_assumptions": {
            "taker_bps": TAKER_BPS, "maker_bps": MAKER_BPS, "source": FEE_SOURCE,
            "slippage_bps": args.slippage_bps,
            "notes": ["floor/stop MARKET fills: taker + slippage",
                      "limit TP / partial fills: maker",
                      "entry fee assumed taker (conservative)",
                      "control pnl = journal pnl_net_usd (fallback pnl_usd), net of real fees",
                      "partial_tp remainder = control pnl scaled by (1-size_frac) — approximation"]},
        "policy_grid": [{k: v for k, v in p.items()} for p in grid],
        "trades_loaded": len(records), "trades_evaluated": len(kept),
        "data_gaps": gaps,
        "metrics": metrics,
    }


def print_table(result: dict) -> None:
    hdr = f"{'policy':<26}{'n':>4}{'net_pnl':>10}{'d_vs_ctl':>10}{'ci95_lo':>9}{'ci95_hi':>9}{'fill%':>7}{'arm%':>7}{'tp1%':>7}{'gb_pct':>8}{'maxDD':>9}"
    print(hdr)
    print("-" * len(hdr))
    for m in result["metrics"]:
        gb = f"{m['giveback_after_arm_pct']:.2f}" if m["giveback_after_arm_pct"] is not None else "-"
        print(f"{m['policy']:<26}{m['n']:>4}{m['net_pnl']:>10.2f}"
              f"{m['delta_vs_control']:>10.2f}{m['delta_ci95'][0]:>9.2f}"
              f"{m['delta_ci95'][1]:>9.2f}{m['floor_fill_rate'] * 100:>6.1f}"
              f"{m['arm_rate'] * 100:>6.1f}{m['tp1_touch_rate'] * 100:>6.1f}"
              f"{gb:>8}{m['max_seq_drawdown']:>9.2f}")
    print(f"\ntrades loaded={result['trades_loaded']} evaluated={result['trades_evaluated']} "
          f"gaps={len(result['data_gaps'])}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Causal, cost-aware exit-policy replay "
                                             "against journaled ARIA trades.")
    ap.add_argument("--days", type=int, default=2, help="trailing journal files (default 2)")
    ap.add_argument("--logs-dir", default="logs", help="journal dir (default logs/)")
    ap.add_argument("--symbols", default=None, help="comma-separated symbol filter")
    ap.add_argument("--since", default=None, help="UTC date YYYY-MM-DD: close-time window start")
    ap.add_argument("--until", default=None, help="UTC date YYYY-MM-DD: close-time window end (inclusive)")
    ap.add_argument("--slippage-bps", type=float, default=2.0)
    ap.add_argument("--out", default=None, help="output JSON path "
                    "(default {logs_dir}/guardian_replay_{date}.json)")
    args = ap.parse_args(argv)
    try:
        result = run(args)
        out = args.out or os.path.join(
            args.logs_dir, f"guardian_replay_{result['date']}.json")
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(out) or ".", suffix=".tmp")
        with os.fdopen(fd, "w") as fh:
            json.dump(result, fh, indent=2)
        os.replace(tmp, out)
        print_table(result)
        print(f"\nartifact: {out}")
    except Exception as exc:  # best-effort doctrine: exit 0 always
        print(f"guardian_replay failed (exit 0 anyway): {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
