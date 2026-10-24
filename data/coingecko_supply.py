"""CoinGecko circulating-supply plane (data department, 2026-10-24).

The measurement plane for the squeeze radar: circulating market cap
(supply x price) per crypto symbol, so a downstream brain can compute
OI / circulating-cap ratios. This plane NEVER gates anything by itself —
it measures, never decides.

ENDPOINT STRATEGY (verified against the free-tier contract): one batched
`coins/markets` sweep of the top 1000 coins by market cap — 4 pages x
250 rows, ONCE per UTC day (4 calls/day, far under the free tier) —
instead of ~91 per-coin calls.

TICKER COLLISION DOCTRINE: CoinGecko tickers are not unique (many ids
share a ticker). When several ids share a ticker, the one with the
HIGHEST market_cap wins. Coins with null/zero circulating_supply (or a
missing price) are EXCLUDED — absent from the map, callers abstain.

Department-template shape (docs/DEPARTMENT_TEMPLATE.md):
  - Pure zero-I/O brains at module level (_base_symbol,
    _parse_markets_payload) — no network, no files, no logging.
  - One supervised fetcher (CoinGeckoSupplyFeed) doing all I/O; the HTTP
    layer is an injectable callable so tests never touch the network.
  - Kill switch: env COINGECKO_SUPPLY_ENABLED default "true", read at
    CALL TIME via coingecko_supply_enabled(); false → refresh no-ops and
    lookups return None (the pre-module system exactly).
  - Own telemetry namespace: coingecko_supply_* events.
  - Own test file: tests/test_coingecko_supply.py.

Freshness discipline: the whole map is persisted ATOMICALLY (tmp+replace)
to logs/coingecko_supply.json as {"fetched_at": epoch_s, "rows": {base:
{"supply_usd", "mcap", "cg_id"}}}. On boot the cache is loaded; it is
served while fetched_at is younger than max_age_s (default 20h). A
refresh is attempted only when stale, and a refresh failure keeps serving
the old cache up to hard_stale_s (default 72h) — beyond that,
circulating_supply_usd returns None (fail-closed abstain). A failed page
aborts the WHOLE refresh: a partial-day map is never served or persisted.
"""
import json
import os
import time
from typing import Callable, Dict, List, Optional

import structlog

logger = structlog.get_logger(__name__)

_BASE = "https://api.coingecko.com/api/v3/coins/markets"
_PAGES = (1, 2, 3, 4)           # top 1000 coins by market cap
_PER_PAGE = 250
_TIMEOUT_S = 15.0
_RETRY_BACKOFF_S = 2.0          # one retry per page on 429/5xx
_DEFAULT_MAX_AGE_S = 20 * 3600.0    # refresh only when older than this
_DEFAULT_HARD_STALE_S = 72 * 3600.0  # beyond this, lookups abstain


# ---------------------------------------------------------------- kill switch

def coingecko_supply_enabled() -> bool:
    """Read at CALL TIME — False = refresh no-ops and lookups return None
    (the pre-module system exactly)."""
    return os.environ.get("COINGECKO_SUPPLY_ENABLED", "true").strip().lower() != "false"


# ---------------------------------------------------------------- pure brains

def _base_symbol(symbol: str) -> str:
    """ARIA 'BTC-USD' -> CoinGecko ticker 'BTC'. Bare tickers pass through
    uppercased. Defensive against None/empty."""
    try:
        if symbol is None:
            return ""
        return str(symbol).split("-", 1)[0].strip().upper()
    except Exception:
        return ""


def _parse_markets_payload(pages: List[List[dict]]) -> Dict[str, dict]:
    """Fold the 4 markets pages into {BASE: {"supply_usd", "mcap",
    "cg_id"}}.

    - Ticker collisions resolve to the id with the HIGHEST market_cap
      (missing/None mcap counts as 0).
    - Coins with null/zero circulating_supply or a missing/non-positive
      current_price are EXCLUDED (never invent a supply).
    - One bad ROW kills only itself; a non-list PAGE raises (the caller
      aborts the whole refresh rather than serve a partial-day map).
    """
    best: Dict[str, dict] = {}
    for page in pages or []:
        if not isinstance(page, list):
            raise ValueError("malformed markets page")
        for row in page:
            if not isinstance(row, dict):
                continue
            ticker = _base_symbol(row.get("symbol") or "")
            if not ticker:
                continue
            try:
                supply = row.get("circulating_supply")
                price = row.get("current_price")
                if supply is None or price is None:
                    continue
                supply = float(supply)
                price = float(price)
            except (TypeError, ValueError):
                continue
            if supply <= 0.0 or price <= 0.0:
                continue
            try:
                mcap = float(row.get("market_cap") or 0.0)
            except (TypeError, ValueError):
                mcap = 0.0
            prev = best.get(ticker)
            if prev is not None and prev["mcap"] >= mcap:
                continue
            best[ticker] = {"supply_usd": supply * price,
                            "mcap": mcap,
                            "cg_id": str(row.get("id") or "")}
    return best


# ---------------------------------------------------------------- fetcher

class CoinGeckoSupplyFeed:
    """Supervised fetcher + cache for the circulating-supply plane.

    refresh() is the only I/O entry — call it once per UTC day (or via
    refresh_if_stale() on a slow cadence). All failures keep serving the
    old cache; nothing raises out of the public API. The HTTP layer is an
    injectable fetch_page_fn(page: int) -> list[dict] so tests never
    touch the network.
    """

    def __init__(self,
                 log_dir: str = "logs",
                 fetch_page_fn: Optional[Callable[[int], List[dict]]] = None,
                 time_fn: Callable[[], float] = time.time,
                 sleep_fn: Callable[[float], None] = time.sleep,
                 max_age_s: float = _DEFAULT_MAX_AGE_S,
                 hard_stale_s: float = _DEFAULT_HARD_STALE_S):
        self._log_dir = log_dir
        self._time = time_fn
        self._sleep = sleep_fn
        self._max_age_s = float(max_age_s)
        self._hard_stale_s = float(hard_stale_s)
        self._fetch_page = fetch_page_fn or self._http_fetch_page
        self._fetched_at: Optional[float] = None
        self._rows: Dict[str, dict] = {}
        self._load_cache()

    # -- paths / persistence ------------------------------------------

    def _cache_path(self) -> str:
        return os.path.join(self._log_dir, "coingecko_supply.json")

    def _load_cache(self) -> None:
        try:
            with open(self._cache_path()) as f:
                d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get("rows"), dict):
                rows = {str(k).upper(): v for k, v in d["rows"].items()
                        if isinstance(v, dict) and isinstance(v.get("supply_usd"), (int, float))}
                self._rows = rows
                fa = d.get("fetched_at")
                self._fetched_at = float(fa) if isinstance(fa, (int, float)) else None
        except Exception:
            self._rows = {}
            self._fetched_at = None

    def _persist(self) -> None:
        try:
            tmp = self._cache_path() + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"fetched_at": self._fetched_at, "rows": self._rows}, f)
            os.replace(tmp, self._cache_path())   # atomic
        except Exception as e:
            logger.warning("coingecko_supply_persist_failed", error=str(e)[:120])

    # -- HTTP (default fetch impl; overridden via fetch_page_fn) -------

    def _http_fetch_page(self, page: int) -> List[dict]:
        import httpx
        params = {"vs_currency": "usd", "order": "market_cap_desc",
                  "per_page": _PER_PAGE, "page": page}
        with httpx.Client(timeout=_TIMEOUT_S) as http:
            for attempt in (0, 1):
                r = http.get(_BASE, params=params)
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 429 or r.status_code >= 500:
                    if attempt == 0:
                        logger.warning("coingecko_supply_page_retry", page=page,
                                       status=r.status_code)
                        self._sleep(_RETRY_BACKOFF_S)
                        continue
                r.raise_for_status()
            r.raise_for_status()  # unreachable; defensive
            return []             # unreachable; defensive

    # -- refresh --------------------------------------------------------

    def refresh(self) -> bool:
        """Fetch all 4 pages and atomically replace the map. True on a
        full successful refresh. Any page failure aborts the WHOLE
        refresh — the old cache is preserved, a partial-day map is never
        served or persisted. No-op when the kill switch is off."""
        if not coingecko_supply_enabled():
            return False
        pages: List[List[dict]] = []
        for page in _PAGES:
            try:
                pages.append(self._fetch_page(page))
            except Exception as e:
                logger.warning("coingecko_supply_refresh_aborted", page=page,
                               error=str(e)[:120])
                return False
        try:
            rows = _parse_markets_payload(pages)
        except Exception as e:
            logger.warning("coingecko_supply_refresh_aborted", reason="malformed_page",
                           error=str(e)[:120])
            return False
        if not rows:
            logger.warning("coingecko_supply_refresh_aborted", reason="empty_map")
            return False
        self._rows = rows
        self._fetched_at = self._time()
        self._persist()
        logger.info("coingecko_supply_updated", symbols=len(rows))
        return True

    def refresh_if_stale(self) -> bool:
        """Refresh only when the cache is older than max_age_s (or
        absent). Fresh cache -> no network call at all."""
        age = self.last_fetch_age_s()
        if age is not None and age < self._max_age_s:
            return False
        return self.refresh()

    # -- public read API (the squeeze brain's interface) ---------------

    def last_fetch_age_s(self) -> Optional[float]:
        """Seconds since the last successful refresh; None when the plane
        has never fetched (no cache)."""
        if self._fetched_at is None:
            return None
        return max(0.0, self._time() - self._fetched_at)

    def circulating_supply_usd(self, symbol: str) -> Optional[float]:
        """Circulating market cap (supply x price) for an ARIA symbol
        ('BTC-USD'). None when the kill switch is off, the symbol is
        absent from the map, or the cache is older than hard_stale_s —
        fail-closed abstain, never raise."""
        if not coingecko_supply_enabled():
            return None
        age = self.last_fetch_age_s()
        if age is None or age > self._hard_stale_s:
            return None
        rec = self._rows.get(_base_symbol(symbol))
        if not rec:
            return None
        try:
            return float(rec["supply_usd"])
        except Exception:
            return None

    def symbol_map(self) -> Dict[str, dict]:
        """The raw rows for telemetry (shallow copy per row)."""
        return {k: dict(v) for k, v in self._rows.items()}


if __name__ == "__main__":
    # Best-effort smoke (tools/daily_digest.py doctrine): one real daily
    # sweep, print a summary, exit 0 no matter what.
    import sys
    try:
        feed = CoinGeckoSupplyFeed(log_dir="/tmp/coingecko_supply_smoke")
        os.makedirs("/tmp/coingecko_supply_smoke", exist_ok=True)
        ok = feed.refresh()
        print(f"refresh_ok={ok} symbols={len(feed.symbol_map())} "
              f"age_s={feed.last_fetch_age_s()}")
        for sym in ("BTC-USD", "ETH-USD", "SOL-USD"):
            v = feed.circulating_supply_usd(sym)
            print(f"{sym}: supply_usd={v if v is not None else 'ABSENT'}")
    except Exception as e:  # never crash a smoke run
        print(f"smoke_error: {e!r:.160}")
    sys.exit(0)
