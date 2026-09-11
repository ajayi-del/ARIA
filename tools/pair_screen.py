"""Pair screen — cointegration scanner for mean-reverting pairs (observer-class).

Nightly/weekly screen feeding a FUTURE pair_meanrev shadow gate. Zero
trade-path wiring. Writes logs/pair_screen.json (atomic tmp+replace) and
appends one dense line to logs/pair_screen_history.jsonl.

Data planes — NEVER mixed in one pair (plane integrity):
  crypto — Bybit v5 public daily klines (pattern per tools/daily_digest.py)
  tradfi — Yahoo v8 chart API daily bars (pattern per data/tradfi_feed.py)

Math (stdlib + math only, no statsmodels/pandas):
  1. log-price series, min 200 daily bars (400 fetched), aligned by day,
     pairs dropped at >5% missing overlap.
  2. Engle-Granger: OLS hedge ratio y = a + b*x via manual normal
     equations; lag-1 ADF on the residuals (Δu_t = c + ρ·u_{t-1} + e);
     MacKinnon p from a hardcoded response-surface table for the
     constant-only case (asymptotic critical values ≈ −3.41 / −2.86 /
     −2.57 at 1% / 5% / 10%; linear interpolation between anchors).
     Pass: p < 0.05.
  3. OU/AR(1) fit on the spread: θ = −ln(φ), half-life = ln(2)/θ.
     Tradeable only if half-life < 5 days AND θ > 0.14.
  4. z-score of the spread (20d window).
  5. Funding-carry screen (crypto only): logs/funding_history.json
     (funding/history.py schema {sym: [{rate, timestamp_ms, source}]},
     one record per hour) — the daily |funding differential| (24 × mean
     per-record |diff|) × half-life days must exceed 2× round-trip cost
     (16 bps), else carry_ok=False (flag, not a gate).
  6. Kill/survival: previous candidates re-screened; a pair is dead at
     p > 0.25 or half-life > 2× its entry value.

Best-effort doctrine (like tools/macro_posture.py): exit code is always 0.
"""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
OUT_PATH = os.path.join(LOG_DIR, "pair_screen.json")
HISTORY_PATH = os.path.join(LOG_DIR, "pair_screen_history.jsonl")
FUNDING_PATH = os.path.join(LOG_DIR, "funding_history.json")

BYBIT_KLINE = "https://api.bybit.com/v5/market/kline"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{}"

MIN_BARS = 200
FETCH_BARS = 400
MAX_MISSING_OVERLAP = 0.05
ADF_PASS_P = 0.05
ADF_KILL_P = 0.25
MAX_HALF_LIFE = 5.0
MIN_THETA = 0.14
KILL_HALF_LIFE_MULT = 2.0
Z_WINDOW = 20
ROUND_TRIP_COST = 0.0016          # 16 bps (2× round-trip)
MAX_CRYPTO = 40                   # most-liquid cap (config order ≈ liquidity)

# MacKinnon response-surface anchors, constant-only case (large n):
# (adf t-stat, p-value). Linear interpolation; clamped at the ends.
_MACKINNON = [(-4.50, 0.0005), (-3.90, 0.002), (-3.41, 0.01),
              (-3.13, 0.025), (-2.86, 0.05), (-2.57, 0.10),
              (-2.33, 0.20), (-2.00, 0.35), (-1.60, 0.55),
              (-1.20, 0.75), (-0.50, 0.93)]


# ── Pure math (unit-tested, no I/O) ──────────────────────────────────────────

def log_prices(closes: list[float]) -> list[float]:
    return [math.log(c) for c in closes if c and c > 0]


def ols(x: list[float], y: list[float]) -> tuple[float, float, list[float]]:
    """y = a + b*x via normal equations. Returns (a, b, residuals)."""
    n = len(x)
    if n < 3 or len(y) != n:
        raise ValueError("ols: need >=3 paired points")
    sx, sy = sum(x), sum(y)
    sxx = sum(v * v for v in x)
    sxy = sum(a * b for a, b in zip(x, y))
    denom = n * sxx - sx * sx
    if denom == 0:
        raise ValueError("ols: degenerate variance")
    b = (n * sxy - sx * sy) / denom
    a = (sy - b * sx) / n
    resid = [yv - (a + b * xv) for xv, yv in zip(x, y)]
    return a, b, resid


def mackinnon_p(t: float) -> float:
    """Approximate MacKinnon p for the constant-only ADF case."""
    if t <= _MACKINNON[0][0]:
        return _MACKINNON[0][1]
    if t >= _MACKINNON[-1][0]:
        return _MACKINNON[-1][1]
    for (t0, p0), (t1, p1) in zip(_MACKINNON, _MACKINNON[1:]):
        if t0 <= t <= t1:
            w = (t - t0) / (t1 - t0)
            return p0 + w * (p1 - p0)
    return 0.99


def adf_test(u: list[float]) -> tuple[float, float]:
    """Lag-1 ADF with constant: Δu_t = c + ρ·u_{t-1} + e. Returns (t, p)."""
    n = len(u)
    if n < 10:
        return 0.0, 0.99
    x = u[:-1]
    y = [u[i + 1] - u[i] for i in range(n - 1)]
    try:
        _c, rho, e = ols(x, y)
    except ValueError:
        return 0.0, 0.99
    m = len(y)
    mx = sum(x) / m
    sxx = sum((v - mx) ** 2 for v in x)
    if sxx <= 0:
        return 0.0, 0.99
    sigma2 = sum(v * v for v in e) / (m - 2)
    if sigma2 <= 0:
        return -99.0, 0.0005          # perfect fit — residuals degenerate flat
    t = rho / math.sqrt(sigma2 / sxx)
    return t, mackinnon_p(t)


def ou_fit(u: list[float]) -> tuple[float, float]:
    """AR(1) on the spread: u_t = c + φ·u_{t-1}. θ = −ln(φ) per daily bar;
    half-life = ln(2)/θ in days. φ ≤ 0 → near-instant reversion;
    φ ≥ 1 → no reversion (half-life inf)."""
    if len(u) < 10:
        return 0.0, math.inf
    try:
        _c, phi, _e = ols(u[:-1], u[1:])
    except ValueError:
        return 0.0, math.inf
    if phi <= 0:
        return 10.0, math.log(2) / 10.0
    if phi >= 1:
        return 0.0, math.inf
    theta = -math.log(phi)
    return theta, math.log(2) / theta


def zscore_now(u: list[float], window: int = Z_WINDOW) -> float:
    w = u[-window:] if len(u) >= window else u[:]
    if len(w) < 2:
        return 0.0
    mean = sum(w) / len(w)
    var = sum((v - mean) ** 2 for v in w) / len(w)
    if var <= 0:
        return 0.0
    return (u[-1] - mean) / math.sqrt(var)


def align_days(a: dict[int, float], b: dict[int, float],
               min_bars: int = MIN_BARS,
               max_missing: float = MAX_MISSING_OVERLAP
               ) -> tuple[list[float], list[float]] | None:
    """Align two day→close maps on common days. Drop the pair when the
    overlap misses >5% of the shorter series or has <min_bars days."""
    common = sorted(set(a) & set(b))
    shorter = min(len(a), len(b))
    if shorter == 0:
        return None
    if len(common) < min_bars:
        return None
    if 1.0 - len(common) / shorter > max_missing:
        return None
    return [a[d] for d in common], [b[d] for d in common]


def screen_pair(closes_a: list[float], closes_b: list[float]) -> dict | None:
    """Engle-Granger + OU on two aligned close series. None on degenerate
    input. b is the hedge ratio (y = a + b·x with y = sym_a, x = sym_b)."""
    lx, ly = log_prices(closes_b), log_prices(closes_a)
    if len(lx) < MIN_BARS or len(lx) != len(ly):
        return None
    try:
        a, b, resid = ols(lx, ly)
    except ValueError:
        return None
    _t, p = adf_test(resid)
    theta, hl = ou_fit(resid)
    spread_std = (sum(r * r for r in resid) / len(resid)) ** 0.5
    return {"hedge_ratio": round(b, 5), "intercept": round(a, 6),
            "spread_std": round(spread_std, 8),
            "adf_p": round(p, 4),
            "theta": round(theta, 4),
            "half_life_days": (round(hl, 2) if math.isfinite(hl) else 9999.0),
            "z_now": round(zscore_now(resid), 3)}


def tradeable(p: float, half_life: float, theta: float) -> bool:
    return p < ADF_PASS_P and half_life < MAX_HALF_LIFE and theta > MIN_THETA


def kill_status(p: float, half_life: float, entry_half_life: float | None) -> str:
    """Survival rule: dead at p > 0.25 or half-life > 2× its entry value."""
    if p > ADF_KILL_P:
        return "dead"
    if entry_half_life and entry_half_life > 0 and \
            half_life > KILL_HALF_LIFE_MULT * entry_half_life:
        return "dead"
    return "candidate"


def carry_ok(mean_abs_diff_per_record: float, half_life_days: float,
             cost: float = ROUND_TRIP_COST) -> bool:
    """mean |funding differential| per hourly record → daily (×24) × the
    expected holding period (half-life days) must beat 2× round-trip cost."""
    return mean_abs_diff_per_record * 24.0 * half_life_days > cost


# ── I/O helpers ──────────────────────────────────────────────────────────────

def atomic_write_json(path: str, obj: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def append_history(path: str, row: dict) -> None:
    with open(path, "a") as f:
        f.write(json.dumps(row) + "\n")


def read_history(path: str) -> list[dict]:
    """One-bad-line doctrine: unparseable lines are skipped, never fatal."""
    out = []
    if not os.path.exists(path):
        return out
    with open(path, errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def load_json(path: str, default):
    try:
        return json.load(open(path))
    except Exception:
        return default


def funding_diff(funding: dict, sym_a: str, sym_b: str) -> float | None:
    """Mean |rate_a − rate_b| over hour-aligned records (fallback: |means|)."""
    ra, rb = funding.get(sym_a) or [], funding.get(sym_b) or []
    if not ra or not rb:
        return None
    by_h_a = {int(r.get("timestamp_ms", 0)) // 3600000: float(r.get("rate", 0.0))
              for r in ra}
    by_h_b = {int(r.get("timestamp_ms", 0)) // 3600000: float(r.get("rate", 0.0))
              for r in rb}
    common = sorted(set(by_h_a) & set(by_h_b))
    if common:
        return sum(abs(by_h_a[h] - by_h_b[h]) for h in common) / len(common)
    ma = sum(by_h_a.values()) / len(by_h_a)
    mb = sum(by_h_b.values()) / len(by_h_b)
    return abs(ma - mb)


# ── Network (best-effort) ────────────────────────────────────────────────────

def fetch_crypto_closes(symbol: str, limit: int = FETCH_BARS) -> dict[int, float]:
    """day-number → close from Bybit v5 daily klines (newest-first payload)."""
    import httpx
    out: dict[int, float] = {}
    try:
        r = httpx.get(BYBIT_KLINE, params={
            "category": "linear",
            "symbol": f"{symbol.replace('-USD', '')}USDT",
            "interval": "D", "limit": limit}, timeout=10.0)
        rows = (r.json().get("result") or {}).get("list") or []
        for k in rows:
            out[int(k[0]) // 86400000] = float(k[4])
    except Exception:
        pass
    return out


def fetch_tradfi_closes(yahoo_sym: str) -> dict[int, float]:
    """day-number → close from the Yahoo v8 chart API (daily bars)."""
    import httpx
    out: dict[int, float] = {}
    try:
        r = httpx.get(YAHOO_CHART.format(yahoo_sym),
                      params={"interval": "1d", "range": f"{FETCH_BARS}d"},
                      headers={"User-Agent": "Mozilla/5.0 (compatible; ARIA-pair-screen/1.0)"},
                      timeout=10.0)
        result = (r.json().get("chart", {}).get("result") or [None])[0]
        if result:
            ts = result.get("timestamp") or []
            closes = ((result.get("indicators", {}).get("quote") or [{}])[0]
                      .get("close") or [])
            for t, c in zip(ts, closes):
                if c is not None:
                    out[int(t) // 86400] = float(c)
    except Exception:
        pass
    return out


def universes() -> tuple[list[str], dict[str, str]]:
    """(crypto symbols capped at MAX_CRYPTO, {sodex_sym: yahoo_sym}).
    Lazy repo imports keep the module importable without pydantic/env."""
    try:
        from core.config import Settings
        from data.tradfi_feed import TRADFI_SYMBOLS
        cfg = Settings()
        crypto = [s for s in getattr(cfg, "assets", [])
                  if s not in TRADFI_SYMBOLS][:MAX_CRYPTO]
        return crypto, dict(TRADFI_SYMBOLS)
    except Exception:
        return ["BTC-USD", "ETH-USD", "SOL-USD"], {}


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    crypto_syms, tradfi_map = universes()
    funding = load_json(FUNDING_PATH, {}) or {}
    prev = load_json(OUT_PATH, {}) or {}
    prev_pairs = {(p.get("sym_a"), p.get("sym_b")): p
                  for p in prev.get("pairs", []) if isinstance(p, dict)}

    planes: list[tuple[str, dict[str, dict[int, float]]]] = []
    crypto_data = {s: fetch_crypto_closes(s) for s in crypto_syms}
    planes.append(("crypto", {s: d for s, d in crypto_data.items() if d}))
    tradfi_data = {s: fetch_tradfi_closes(y) for s, y in tradfi_map.items()}
    planes.append(("tradfi", {s: d for s, d in tradfi_data.items() if d}))

    pairs_out: list[dict] = []
    n_screened = 0
    for plane, series in planes:
        syms = sorted(series)
        for i, sa in enumerate(syms):
            for sb in syms[i + 1:]:
                aligned = align_days(series[sa], series[sb])
                if aligned is None:
                    continue
                n_screened += 1
                stats = screen_pair(aligned[0], aligned[1])
                if stats is None:
                    continue
                row = {"sym_a": sa, "sym_b": sb, "plane": plane, **stats}
                prev_row = prev_pairs.get((sa, sb))
                entry_hl = (prev_row or {}).get("entry_half_life_days")
                if plane == "crypto":
                    diff = funding_diff(funding, sa, sb)
                    row["carry_ok"] = (carry_ok(diff, stats["half_life_days"])
                                       if diff is not None else None)
                else:
                    row["carry_ok"] = None
                if prev_row is not None:
                    # Survival re-screen: kill rules bind previous candidates.
                    row["entry_half_life_days"] = entry_hl or stats["half_life_days"]
                    row["status"] = kill_status(
                        stats["adf_p"], stats["half_life_days"],
                        row["entry_half_life_days"])
                else:
                    if not tradeable(stats["adf_p"], stats["half_life_days"],
                                     stats["theta"]):
                        continue
                    row["entry_half_life_days"] = stats["half_life_days"]
                    row["status"] = "candidate"
                pairs_out.append(row)

    report = {"ts": datetime.now(timezone.utc).isoformat(),
              "n_screened": n_screened, "pairs": pairs_out}
    try:
        atomic_write_json(OUT_PATH, report)
    except Exception as e:
        print(f"pair_screen write failed: {e}")

    candidates = [f"{p['sym_a']}/{p['sym_b']}" for p in pairs_out
                  if p["status"] == "candidate"]
    dead = [f"{p['sym_a']}/{p['sym_b']}" for p in pairs_out
            if p["status"] == "dead"]
    try:
        append_history(HISTORY_PATH, {
            "ts": report["ts"], "n_screened": n_screened,
            "n_candidates": len(candidates), "n_dead": len(dead),
            "candidates": candidates, "dead": dead})
    except Exception:
        pass
    print(f"pair_screen: {n_screened} screened, {len(candidates)} candidates, "
          f"{len(dead)} dead")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"pair_screen failed (best-effort exit 0): {e}")
    sys.exit(0)
