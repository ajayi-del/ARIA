"""Bybit daily open-interest history feed (data department, 2026-09-26).

The OI plane for the WhaleProxy: Bybit's v5 `open-interest` endpoint serves
daily-bucket open interest per linear-perp symbol. The brain
(intelligence/whale_proxy.py) consumes OI GROWTH over the trailing window —
sustained OI expansion beside disciplined funding is the institutional-
accumulation signature; collapsing OI is distribution.

SHAPE CAVEAT (verified against the v5 contract): `result.list` arrives
REVERSE-chronological (newest first). parse_series normalizes to
oldest-first before any growth math — never trust the wire order.

Department-template shape (docs/DEPARTMENT_TEMPLATE.md):
  - Pure zero-I/O brains at module level (parse_series, oi_growth) — no
    network, no files, no logging.
  - One supervised fetcher (OIHistoryFeed) doing all I/O.
  - Kill switch: env OI_HISTORY_FEED_ENABLED default "true"; false →
    run() returns immediately (inert, indistinguishable from absent).
  - Own telemetry namespace: oi_history_* events.
  - Own test file: tests/test_oi_history_feed.py (HTTP mocked by an
    injected fetch callable — never live network in tests).

Storage: logs/oi_history.json (atomic tmp+replace, latest 30-bar series
per symbol) + logs/oi_history.jsonl (append-only, one growth row per
symbol per poll, one-bad-line read doctrine). Staleness: a series whose
newest daily bar is older than 48h is abstained (None), never served —
daily bars go stale slowly, 48h gives the incomplete current bar room.
"""
import json
import os
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union

import structlog

logger = structlog.get_logger(__name__)

_BASE = "https://api.bybit.com/v5/market/open-interest"
_INTERVAL = "1d"          # daily bars
_LIMIT = 30               # 30-day trailing window per poll
_STALE_S = 48 * 3600.0    # newest bar older than 48h = abstain
_HISTORY_CAP = 90         # 90 daily growth rows per symbol


# ---------------------------------------------------------------- pure brains

def parse_series(rows) -> List[Tuple[int, float]]:
    """Normalize raw API rows to an oldest-first [(ts_ms, oi), ...] series.

    The wire order is REVERSE-chronological (newest first) — this function
    is the single place that invariant is repaired. Unparseable or
    negative-OI rows are dropped individually (one bad row kills only
    itself)."""
    out: List[Tuple[int, float]] = []
    for row in rows or []:
        try:
            ts_ms = int(row.get("timestamp"))
            oi = float(row.get("openInterest"))
        except (TypeError, ValueError, AttributeError):
            continue
        if oi < 0.0:
            continue
        out.append((ts_ms, oi))
    out.sort(key=lambda t: t[0])
    return out


def oi_growth(series) -> Optional[float]:
    """Growth % of the newest value vs the oldest in the window
    (oldest-first series of plain floats). None on a thin window (<2
    values), degenerate/negative values, or the oldest == 0 division
    guard — never divide by zero, never invent growth."""
    vals: List[float] = []
    for v in series or []:
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if fv < 0.0:
            continue
        vals.append(fv)
    if len(vals) < 2:
        return None
    oldest, current = vals[0], vals[-1]
    if oldest <= 0.0:
        return None
    return round((current - oldest) / oldest * 100.0, 4)


# ---------------------------------------------------------------- fetcher

class OIHistoryFeed:
    """Supervised fetcher. run() is the only I/O entry — call it on a
    slow cadence (hourly is ample for daily bars; the series only moves
    at day roll). Per-symbol errors are isolated; an empty list is a DARK
    symbol (recorded, never fatal); retCode != 0 is an isolated error.
    The HTTP layer is an injectable callable so tests never touch the
    network."""

    def __init__(self,
                 symbols: Union[Sequence[str], Callable[[], Sequence[str]], None] = None,
                 log_dir: str = "logs",
                 fetch_fn: Optional[Callable[[str], dict]] = None,
                 time_fn: Callable[[], float] = time.time):
        self._symbols_spec = symbols if symbols is not None else ("BTCUSDT", "ETHUSDT", "SOLUSDT")
        self._log_dir = log_dir
        self._time = time_fn
        self._fetch_fn = fetch_fn or self._http_fetch
        # symbol -> {"series": [[ts_ms, oi], ...] oldest-first, "fetched_ts": float}
        self._latest: Dict[str, dict] = {}
        # symbol -> [growth_pct, ...] rolling (oldest-first), cap _HISTORY_CAP
        self._growth_hist: Dict[str, List[float]] = {}
        self._dark: Dict[str, float] = {}     # symbol -> last dark ts
        self._load_cache()
        self._load_history()

    # -- paths / persistence ------------------------------------------

    def _cache_path(self) -> str:
        return os.path.join(self._log_dir, "oi_history.json")

    def _history_path(self) -> str:
        return os.path.join(self._log_dir, "oi_history.jsonl")

    def _load_cache(self) -> None:
        try:
            with open(self._cache_path()) as f:
                d = json.load(f)
            if isinstance(d, dict):
                self._latest = {k: v for k, v in d.items()
                                if isinstance(v, dict) and isinstance(v.get("series"), list)}
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
                        g = row.get("growth_pct")
                        if sym and isinstance(g, (int, float)):
                            self._growth_hist.setdefault(sym, []).append(float(g))
                    except Exception:
                        continue
            for sym in self._growth_hist:
                self._growth_hist[sym] = self._growth_hist[sym][-_HISTORY_CAP:]
        except Exception:
            pass

    def _persist(self) -> None:
        try:
            tmp = self._cache_path() + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._latest, f)
            os.replace(tmp, self._cache_path())   # atomic
        except Exception as e:
            logger.warning("oi_history_persist_failed", error=str(e)[:120])

    def _append_history(self, rows: List[dict]) -> None:
        try:
            with open(self._history_path(), "a") as f:
                for row in rows:
                    f.write(json.dumps(row) + "\n")
        except Exception as e:
            logger.warning("oi_history_history_failed", error=str(e)[:120])

    # -- symbols ---------------------------------------------------------

    def symbols(self) -> List[str]:
        spec = self._symbols_spec
        if callable(spec):
            try:
                return [str(s) for s in (spec() or [])]
            except Exception as e:
                logger.warning("oi_history_symbols_fn_failed", error=str(e)[:120])
                return []
        return [str(s) for s in spec]

    # -- HTTP (default fetch impl; overridden via fetch_fn in tests) ----

    def _http_fetch(self, symbol: str) -> dict:
        import httpx
        with httpx.Client(timeout=15.0) as http:
            r = http.get(_BASE, params={"category": "linear", "symbol": symbol,
                                        "intervalTime": _INTERVAL, "limit": _LIMIT})
            r.raise_for_status()
            return r.json()

    # -- main entry ------------------------------------------------------

    def run(self) -> bool:
        """One poll pass over the symbol list. True if any symbol updated.
        Inert when the kill switch is off."""
        if os.environ.get("OI_HISTORY_FEED_ENABLED", "true").strip().lower() == "false":
            return False
        now = self._time()
        updated = False
        hist_rows: List[dict] = []
        for sym in self.symbols():
            try:
                payload = self._fetch_fn(sym)
            except Exception as e:
                logger.warning("oi_history_fetch_failed", symbol=sym, error=str(e)[:120])
                continue
            try:
                if int(payload.get("retCode", -1)) != 0:
                    logger.warning("oi_history_api_error", symbol=sym,
                                   retCode=payload.get("retCode"),
                                   retMsg=str(payload.get("retMsg"))[:120])
                    continue
                rows = ((payload.get("result") or {}).get("list")) or []
                if not rows:
                    # DARK PLANE — coverage gap. Record, never invent.
                    self._dark[sym] = now
                    logger.info("oi_history_dark", symbol=sym)
                    continue
                series = parse_series(rows)
                if not series:
                    self._dark[sym] = now
                    logger.info("oi_history_dark", symbol=sym, reason="unparseable")
                    continue
                self._latest[sym] = {"series": [[ts, oi] for ts, oi in series],
                                     "fetched_ts": now}
                self._dark.pop(sym, None)
                g = oi_growth([oi for _, oi in series])
                if g is not None:
                    hist = self._growth_hist.setdefault(sym, [])
                    hist.append(g)
                    del hist[:max(0, len(hist) - _HISTORY_CAP)]
                    hist_rows.append({"ts": now, "symbol": sym, "growth_pct": g,
                                      "oi_oldest": series[0][1], "oi_latest": series[-1][1],
                                      "bars": len(series), "ts_ms": series[-1][0]})
                updated = True
            except Exception as e:
                logger.warning("oi_history_parse_failed", symbol=sym, error=str(e)[:120])
        if updated:
            self._persist()
            if hist_rows:
                self._append_history(hist_rows)
            logger.info("oi_history_updated", symbols=sorted(self._latest.keys()))
        return updated

    # -- public read API (the whale-proxy brain's interface) -------------

    def get_series(self, symbol: str) -> Optional[List[Tuple[int, float]]]:
        """Latest oldest-first series, or None when dark / stale (>48h) /
        unknown. Staleness reads the newest BAR timestamp (daily bars:
        the incomplete current bar keeps a healthy series under 48h)."""
        rec = self._latest.get(symbol)
        if not rec:
            return None
        try:
            series = [(int(ts), float(oi)) for ts, oi in rec["series"]]
            if not series:
                return None
            age_s = max(0.0, self._time() - series[-1][0] / 1000.0)
            if age_s > _STALE_S:
                return None
            return series
        except Exception:
            return None

    def get_growth(self, symbol: str) -> Optional[float]:
        """OI growth % over the trailing window. None when dark/stale —
        the proxy brain treats None as an ABSTAIN leg (renormalizes over
        the remaining planes), never as zero."""
        series = self.get_series(symbol)
        if series is None:
            return None
        return oi_growth([oi for _, oi in series])

    def get_growth_history(self, symbol: str) -> List[float]:
        return list(self._growth_hist.get(symbol) or [])

    def is_dark(self, symbol: str) -> bool:
        return symbol in self._dark


if __name__ == "__main__":
    # Best-effort smoke (tools/daily_digest.py doctrine): one real fetch of
    # BTC/ETH/SOL, print a summary, exit 0 no matter what.
    import sys
    try:
        feed = OIHistoryFeed(log_dir="/tmp/oi_history_smoke")
        os.makedirs("/tmp/oi_history_smoke", exist_ok=True)
        ok = feed.run()
        print(f"fetch_ok={ok}")
        for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
            g = feed.get_growth(sym)
            if g is None:
                print(f"{sym}: DARK/STALE")
                continue
            series = feed.get_series(sym) or []
            print(f"{sym}: growth_pct={g:+.2f} bars={len(series)} "
                  f"oldest={series[0][1] if series else '?'} latest={series[-1][1] if series else '?'}")
    except Exception as e:  # never crash a smoke run
        print(f"smoke_error: {e!r:.160}")
    sys.exit(0)
