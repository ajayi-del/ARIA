"""Bybit long/short account-ratio feed (data department, 2026-09-26).

The retail/whale positioning plane: Bybit's v5 `account-ratio` endpoint
serves the fraction of ACCOUNTS net long vs net short per linear-perp
symbol (buyRatio/sellRatio, account COUNT — not notional). This is the
crowd-positioning read the axiom-stack entry gate will consume: extreme
account-count skews mark crowded sides (fade fuel) and lopsided
positioning regimes.

COVERAGE CAVEAT (verified live 2026-09-26): the endpoint covers majors
plus select liquid alts only — thin alts (e.g. FETUSDT) return an EMPTY
list. Empty/absent = DARK PLANE: record dark, abstain (fail-open
neutral), never invent a ratio.

Department-template shape (docs/DEPARTMENT_TEMPLATE.md):
  - Pure zero-I/O brains at module level (long_short_ratio,
    ratio_percentile, classify_skew) — no network, no files, no logging.
  - One supervised fetcher (WhaleRatioFeed) doing all I/O.
  - Kill switch: env WHALE_RATIO_FEED_ENABLED default "true"; false →
    run() returns immediately (inert, indistinguishable from absent).
  - Own telemetry namespace: whale_ratio_* events.
  - Own test file: tests/test_whale_ratio_feed.py (HTTP mocked by an
    injected fetch callable — never live network in tests).

Calibration doctrine: this account-COUNT ratio reads compressed
(~1.0-1.4 typical on majors), so classify_skew keys on the rolling
PERCENTILE of the current reading vs its own trailing history
(plane-native calibration), with absolute ratio bands as the fallback
when history is too thin. Downstream tiers read the percentile.

Storage: logs/whale_ratio.json (atomic tmp+replace, latest per symbol)
+ logs/whale_ratio_history.jsonl (append-only, one line per symbol per
poll, one-bad-line read doctrine). Staleness: a reading older than 4h
is abstained (None), never served.

The poll symbol list is INJECTED (callable or list) — the main.py splice
(later phase, coordinator-owned) passes universe ∩ Bybit-perp symbols.
Default: BTC/ETH/SOL.
"""
import json
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Union

import structlog

logger = structlog.get_logger(__name__)

_BASE = "https://api.bybit.com/v5/market/account-ratio"
_PERIOD = "1h"            # hourly buckets; limit 24 = 24h rolling window per poll
_LIMIT = 24
_STALE_S = 4 * 3600.0     # reading older than 4h = abstain
_HISTORY_CAP = 168        # 7 days of hourly reads per symbol
_MIN_PCT_SAMPLES = 5      # below this, percentile abstains → absolute fallback


# ---------------------------------------------------------------- pure brains

def long_short_ratio(buy: float, sell: float) -> Optional[float]:
    """buy/sell account ratio. sell→0 guard returns None (never divide by
    zero, never invent infinity). Degenerate/negative inputs abstain."""
    try:
        buy = float(buy)
        sell = float(sell)
    except (TypeError, ValueError):
        return None
    if sell <= 0.0 or buy < 0.0:
        return None
    return buy / sell


@dataclass(frozen=True)
class RatioReading:
    """One account-ratio observation. age_s is stamped at read time."""
    symbol: str
    ratio: float            # buy/sell account ratio (>1 = more longs)
    long_share: float       # buy / (buy + sell), 0..1
    ts_ms: int              # exchange bucket timestamp (ms)
    age_s: float            # seconds since ts_ms at read time


def ratio_percentile(symbol_history: Sequence[float], current: float) -> Optional[float]:
    """Percentile (0-100) of `current` within the trailing history window.

    Plane-native calibration: account-count ratios are compressed, so the
    meaningful question is 'how extreme is this vs its own recent
    distribution', not 'is it above 1.2'. Returns None when the window is
    too thin (< _MIN_PCT_SAMPLES) or inputs are degenerate — callers fall
    back to absolute bands.
    """
    vals = []
    for v in symbol_history or []:
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if fv > 0.0:
            vals.append(fv)
    if len(vals) < _MIN_PCT_SAMPLES:
        return None
    try:
        cur = float(current)
    except (TypeError, ValueError):
        return None
    if cur <= 0.0:
        return None
    below = sum(1 for v in vals if v < cur)
    ties = sum(1 for v in vals if v == cur)
    # midpoint-of-ties percentile: robust on a compressed discrete plane
    return round(100.0 * (below + 0.5 * ties) / len(vals), 1)


def classify_skew(ratio: Optional[float], percentile: Optional[float],
                  pct_strong: float = 90.0, pct_mild: float = 70.0,
                  abs_strong_long: float = 1.30, abs_mild_long: float = 1.15,
                  abs_mild_short: float = 0.87, abs_strong_short: float = 0.77
                  ) -> str:
    """Skew verdict: strong_long | long | neutral | short | strong_short.

    Percentile-PRIMARY: with a valid percentile the bands are symmetric
    (≥pct_strong strong_long, ≥pct_mild long, ≤100−pct_strong strong_short,
    ≤100−pct_mild short). Absolute-FALLBACK when the percentile abstains
    (thin history): fixed ratio bands on the compressed plane
    (defaults ~±15%/±30% around 1.0, mirrored). ratio None → neutral
    (dark-plane abstain — the caller never even gets here; defensive).
    """
    if ratio is None:
        return "neutral"
    if percentile is not None:
        if percentile >= pct_strong:
            return "strong_long"
        if percentile >= pct_mild:
            return "long"
        if percentile <= 100.0 - pct_strong:
            return "strong_short"
        if percentile <= 100.0 - pct_mild:
            return "short"
        return "neutral"
    if ratio >= abs_strong_long:
        return "strong_long"
    if ratio >= abs_mild_long:
        return "long"
    if ratio <= abs_strong_short:
        return "strong_short"
    if ratio <= abs_mild_short:
        return "short"
    return "neutral"


# ---------------------------------------------------------------- fetcher

class WhaleRatioFeed:
    """Supervised fetcher. run() is the only I/O entry — call it on the
    poll cadence (hourly matches the 1h bucket period). Per-symbol errors
    are isolated; an empty list is a DARK symbol (recorded, never fatal);
    retCode != 0 is an isolated error. The HTTP layer is an injectable
    callable so tests never touch the network."""

    def __init__(self,
                 symbols: Union[Sequence[str], Callable[[], Sequence[str]], None] = None,
                 log_dir: str = "logs",
                 fetch_fn: Optional[Callable[[str], dict]] = None,
                 time_fn: Callable[[], float] = time.time):
        self._symbols_spec = symbols if symbols is not None else ("BTCUSDT", "ETHUSDT", "SOLUSDT")
        self._log_dir = log_dir
        self._time = time_fn
        self._fetch_fn = fetch_fn or self._http_fetch
        # symbol -> {"ratio","long_share","ts_ms","fetched_ts"}
        self._latest: Dict[str, dict] = {}
        # symbol -> [ratio, ...] rolling (oldest-first), cap _HISTORY_CAP
        self._history: Dict[str, List[float]] = {}
        self._dark: Dict[str, float] = {}     # symbol -> last dark ts
        self._load_cache()
        self._load_history()

    # -- paths / persistence ------------------------------------------

    def _cache_path(self) -> str:
        return os.path.join(self._log_dir, "whale_ratio.json")

    def _history_path(self) -> str:
        return os.path.join(self._log_dir, "whale_ratio_history.jsonl")

    def _load_cache(self) -> None:
        try:
            with open(self._cache_path()) as f:
                d = json.load(f)
            if isinstance(d, dict):
                self._latest = {k: v for k, v in d.items() if isinstance(v, dict)}
        except Exception:
            self._latest = {}

    def _load_history(self) -> None:
        """One-bad-line doctrine: a corrupt line kills only itself."""
        try:
            with open(self._history_path()) as f:
                for line in f:
                    try:
                        row = json.loads(line)
                        sym = row.get("symbol")
                        ratio = row.get("ratio")
                        if sym and isinstance(ratio, (int, float)) and ratio > 0:
                            self._history.setdefault(sym, []).append(float(ratio))
                    except Exception:
                        continue
            for sym in self._history:
                self._history[sym] = self._history[sym][-_HISTORY_CAP:]
        except Exception:
            pass

    def _persist(self) -> None:
        try:
            tmp = self._cache_path() + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._latest, f)
            os.replace(tmp, self._cache_path())   # atomic
        except Exception as e:
            logger.warning("whale_ratio_persist_failed", error=str(e)[:120])

    def _append_history(self, rows: List[dict]) -> None:
        try:
            with open(self._history_path(), "a") as f:
                for row in rows:
                    f.write(json.dumps(row) + "\n")
        except Exception as e:
            logger.warning("whale_ratio_history_failed", error=str(e)[:120])

    # -- symbols ---------------------------------------------------------

    def symbols(self) -> List[str]:
        spec = self._symbols_spec
        if callable(spec):
            try:
                return [str(s) for s in (spec() or [])]
            except Exception as e:
                logger.warning("whale_ratio_symbols_fn_failed", error=str(e)[:120])
                return []
        return [str(s) for s in spec]

    # -- HTTP (default fetch impl; overridden via fetch_fn in tests) ----

    def _http_fetch(self, symbol: str) -> dict:
        import httpx
        with httpx.Client(timeout=15.0) as http:
            r = http.get(_BASE, params={"category": "linear", "symbol": symbol,
                                        "period": _PERIOD, "limit": _LIMIT})
            r.raise_for_status()
            return r.json()

    # -- main entry ------------------------------------------------------

    def run(self) -> bool:
        """One poll pass over the symbol list. True if any symbol updated.
        Inert when the kill switch is off."""
        if os.environ.get("WHALE_RATIO_FEED_ENABLED", "true").strip().lower() == "false":
            return False
        now = self._time()
        updated = False
        hist_rows: List[dict] = []
        for sym in self.symbols():
            try:
                payload = self._fetch_fn(sym)
            except Exception as e:
                logger.warning("whale_ratio_fetch_failed", symbol=sym, error=str(e)[:120])
                continue
            try:
                if int(payload.get("retCode", -1)) != 0:
                    logger.warning("whale_ratio_api_error", symbol=sym,
                                   retCode=payload.get("retCode"),
                                   retMsg=str(payload.get("retMsg"))[:120])
                    continue
                rows = ((payload.get("result") or {}).get("list")) or []
                if not rows:
                    # DARK PLANE — coverage gap. Record, never invent.
                    self._dark[sym] = now
                    logger.info("whale_ratio_dark", symbol=sym)
                    continue
                # rows newest-first; newest = current reading, all rows feed history
                parsed = []
                for row in rows:
                    ratio = long_short_ratio(row.get("buyRatio"), row.get("sellRatio"))
                    if ratio is None:
                        continue
                    try:
                        ts_ms = int(row.get("timestamp"))
                    except (TypeError, ValueError):
                        continue
                    parsed.append((ts_ms, ratio, float(row["buyRatio"]), float(row["sellRatio"])))
                if not parsed:
                    self._dark[sym] = now
                    logger.info("whale_ratio_dark", symbol=sym, reason="unparseable")
                    continue
                parsed.sort(key=lambda t: t[0])
                ts_ms, ratio, buy, sell = parsed[-1]
                self._latest[sym] = {"ratio": round(ratio, 6),
                                     "long_share": round(buy / (buy + sell), 6) if (buy + sell) > 0 else None,
                                     "ts_ms": ts_ms, "fetched_ts": now}
                self._dark.pop(sym, None)
                hist = self._history.setdefault(sym, [])
                for h_ts, h_ratio, _, _ in parsed:
                    hist.append(h_ratio)
                    hist_rows.append({"ts": now, "symbol": sym, "ratio": round(h_ratio, 6),
                                      "ts_ms": h_ts})
                del hist[:max(0, len(hist) - _HISTORY_CAP)]
                updated = True
            except Exception as e:
                logger.warning("whale_ratio_parse_failed", symbol=sym, error=str(e)[:120])
        if updated:
            self._persist()
            if hist_rows:
                self._append_history(hist_rows)
            logger.info("whale_ratio_updated", symbols=sorted(self._latest.keys()))
        return updated

    # -- public read API (the axiom brain's interface) -------------------

    def get_reading(self, symbol: str) -> Optional[RatioReading]:
        """Latest reading, or None when dark / stale (>4h) / unknown."""
        rec = self._latest.get(symbol)
        if not rec:
            return None
        try:
            ts_ms = int(rec["ts_ms"])
            age_s = max(0.0, self._time() - ts_ms / 1000.0)
            if age_s > _STALE_S:
                return None
            return RatioReading(symbol=symbol, ratio=float(rec["ratio"]),
                                long_share=float(rec["long_share"]),
                                ts_ms=ts_ms, age_s=round(age_s, 1))
        except Exception:
            return None

    def get_percentile(self, symbol: str) -> Optional[float]:
        """Percentile of the latest ratio vs the symbol's trailing window.
        None when the reading is dark/stale or history is too thin — the
        consumer falls back to absolute bands via classify_skew."""
        rec = self.get_reading(symbol)
        if rec is None:
            return None
        return ratio_percentile(self._history.get(symbol) or [], rec.ratio)

    def is_dark(self, symbol: str) -> bool:
        return symbol in self._dark


if __name__ == "__main__":
    # Best-effort smoke (tools/daily_digest.py doctrine): one real fetch of
    # BTC/ETH/SOL, print a summary, exit 0 no matter what.
    import sys
    try:
        feed = WhaleRatioFeed(log_dir="/tmp/whale_ratio_smoke")
        os.makedirs("/tmp/whale_ratio_smoke", exist_ok=True)
        ok = feed.run()
        print(f"fetch_ok={ok}")
        for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
            r = feed.get_reading(sym)
            if r is None:
                print(f"{sym}: DARK/STALE")
                continue
            pct = feed.get_percentile(sym)
            skew = classify_skew(r.ratio, pct)
            print(f"{sym}: ratio={r.ratio:.4f} long_share={r.long_share:.4f} "
                  f"age_s={r.age_s:.0f} percentile={pct} skew={skew}")
    except Exception as e:  # never crash a smoke run
        print(f"smoke_error: {e!r:.160}")
    sys.exit(0)
