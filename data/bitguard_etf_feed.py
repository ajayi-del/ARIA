"""TheBitGuard ETF-flow feed — SoSoValue replacement plane (2026-10-17,
Governor directive "our etf signals have also not worked that one is your fix
local node... wire TheBitGuard as primary with fallback chain").

SoSoValue has been Cloudflare-dark since 2026-10-13; its cached rows aged
past the 72h abstain horizon, so every tide consumer (sizing tilt, tide
vetoes, runner/pyramid abstains, TAC consensus) silently reads neutral.
TheBitGuard serves the same Farside-sourced daily ETF flows with no key:

    GET https://api.thebitguard.com/api/public/etf/snapshot
    {data: {generatedAt, asOfDate, sources,
            btc: {netFlowYesterdayUsd, netFlow7dUsd, netFlow30dUsd, perEtf},
            eth: {...}}}

Verified live 2026-10-17 (BTC 1d −$82.4M / 7d −$451.4M / 30d +$1.4376B).

Contract mirror: verdict(symbol) / flow_age_hours(symbol) are IDENTICAL in
shape and semantics to data/sosovalue_feed.SoSoValueFeed so the single
accessor _etf_flow (main.py) can compose a freshest-wins fallback chain
without touching any consumer brain. Rows are converted into the SoSoValue
row shape and flow_verdict (imported) produces the same keys.

HONEST-ABSTAIN DOCTRINE: the endpoint serves AGGREGATES (1d/7d/30d), not a
daily series. We accumulate one real row per asOfDate from daily polls —
sum_3d_usd is a true 3-day sum only after 3 days of history, accel after 4.
We NEVER decompose the 7d/30d aggregates into fake daily rows (that would
fabricate granularity the source does not have). The aggregates ride the
verdict as extra telemetry keys (sum_7d_usd / sum_30d_usd) for the
institutional_score co-vote design (RC7) — no existing consumer reads them.
BTC + ETH only; SOL has no plane on this source and abstains (falls through
to whatever the fallback chain serves).
"""

import json
import os
import time

import httpx
import structlog

from data.sosovalue_feed import flow_verdict, etf_calendar_adjusted_age

logger = structlog.get_logger(__name__)

_URL = "https://api.thebitguard.com/api/public/etf/snapshot"
_MIN_INTERVAL_S = 10800.0   # source refreshes ~6h; poll at most every 3h
_HISTORY_CAP = 10           # days of accumulated daily rows retained


def _num(d: dict, *keys) -> float:
    """Defensive numeric read across camelCase/snake_case spellings."""
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return 0.0


class BitguardEtfFeed:
    """Supervised poller. fetch_due() is the only I/O entry — call it
    hourly; it self-throttles to one snapshot fetch per _MIN_INTERVAL_S."""

    def __init__(self, symbols=("BTC", "ETH"), log_dir: str = "logs",
                 time_fn=time.time, url: str = _URL,
                 min_interval_s: float = _MIN_INTERVAL_S):
        self._symbols = tuple(symbols)
        self._log_dir = log_dir
        self._time = time_fn
        self._url = url
        self._min_interval = float(min_interval_s)
        self._rows: dict = {}     # symbol -> rows (newest-first, SoSoValue shape)
        self._agg: dict = {}      # symbol -> {sum_7d_usd, sum_30d_usd, as_of}
        self._last_fetch_ts: float = 0.0
        self._load_cache()

    # ---- persistence (atomic + append-only, one-bad-line doctrine) ----

    def _cache_path(self) -> str:
        return os.path.join(self._log_dir, "bitguard_etf_flows.json")

    def _load_cache(self) -> None:
        try:
            with open(self._cache_path()) as f:
                d = json.load(f)
            if isinstance(d, dict):
                rows = d.get("rows")
                agg = d.get("agg")
                if isinstance(rows, dict):
                    self._rows = rows
                if isinstance(agg, dict):
                    self._agg = agg
        except Exception:
            self._rows = {}
            self._agg = {}

    def _persist(self) -> None:
        try:
            tmp = self._cache_path() + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"rows": self._rows, "agg": self._agg}, f)
            os.replace(tmp, self._cache_path())
        except Exception as e:
            logger.warning("bitguard_etf_persist_failed", error=str(e)[:120])
        try:
            with open(os.path.join(self._log_dir, "bitguard_etf_flows.jsonl"), "a") as f:
                f.write(json.dumps({"ts": self._time(),
                                    "flows": {s: self.verdict(s)
                                              for s in self._symbols}}) + "\n")
        except Exception:
            pass

    # ---- brain surface (SoSoValue-identical contract) ----

    def verdicts(self) -> dict:
        return {s: self.verdict(s) for s in self._symbols}

    def verdict(self, symbol: str) -> dict:
        v = flow_verdict(symbol, self._rows.get(symbol) or [])
        agg = self._agg.get(symbol) or {}
        if v.get("rows"):
            v["source"] = "bitguard"
            if agg:
                v["sum_7d_usd"] = agg.get("sum_7d_usd")
                v["sum_30d_usd"] = agg.get("sum_30d_usd")
        return v

    def flow_age_hours(self, symbol: str) -> float:
        """Hours since the latest cached flow DATE (calendar-adjusted via the
        shared NYSE-holiday repair — same semantics as SoSoValue)."""
        rows = self._rows.get(symbol) or []
        if not rows:
            return 999.0
        try:
            d = str(rows[0].get("date") or "")
            y, m, dd = int(d[:4]), int(d[5:7]), int(d[8:10])
            import calendar as _cal
            day_epoch = _cal.timegm((y, m, dd, 0, 0, 0, 0, 0, 0))
            age = max(0.0, (self._time() - day_epoch) / 3600.0)
            if os.environ.get("ETF_TIDE_CALENDAR_AGE_ENABLED", "true").lower() != "false":
                age = etf_calendar_adjusted_age(d, age, self._time())
            return age
        except Exception:
            return 999.0

    # ---- fetch ----

    def _due(self) -> bool:
        if not self._url:
            return False
        return (self._time() - self._last_fetch_ts) >= self._min_interval

    async def fetch_due(self) -> bool:
        """One snapshot fetch per interval; serves every symbol leg in a
        single call. True when fresh rows were ingested."""
        if not self._due():
            return False
        self._last_fetch_ts = self._time()
        try:
            async with httpx.AsyncClient(timeout=15.0) as http:
                r = await http.get(self._url)
                if r.status_code != 200:
                    logger.warning("bitguard_etf_fetch_failed",
                                   status=r.status_code)
                    return False
                d = r.json()
        except Exception as e:
            logger.warning("bitguard_etf_fetch_failed", error=str(e)[:120])
            return False
        data = d.get("data") if isinstance(d, dict) else None
        if not isinstance(data, dict):
            logger.warning("bitguard_etf_fetch_failed", note="bad_shape")
            return False
        as_of = str(data.get("asOfDate") or data.get("as_of_date") or "")
        if len(as_of) != 10:
            logger.warning("bitguard_etf_fetch_failed", note="bad_asof",
                           as_of=as_of[:32])
            return False
        ingested = False
        for sym in self._symbols:
            leg = data.get(sym.lower()) or data.get(sym.upper()) or {}
            if not isinstance(leg, dict) or not leg:
                continue
            row = {"date": as_of,
                   "total_net_inflow": _num(leg, "netFlowYesterdayUsd",
                                            "net_flow_yesterday_usd"),
                   "total_value_traded": _num(leg, "totalValueTraded",
                                              "total_value_traded"),
                   "total_net_assets": _num(leg, "totalNetAssetsUsd",
                                            "total_net_assets_usd",
                                            "totalNetAssets"),
                   "cum_net_inflow": _num(leg, "cumNetInflowUsd",
                                          "cum_net_inflow_usd")}
            rows = list(self._rows.get(sym) or [])
            if rows and str(rows[0].get("date")) == as_of:
                rows[0] = row          # same date re-poll: refresh in place
            else:
                rows.insert(0, row)    # new trading date: prepend
            self._rows[sym] = rows[:_HISTORY_CAP]
            self._agg[sym] = {
                "sum_7d_usd": round(_num(leg, "netFlow7dUsd",
                                         "net_flow_7d_usd"), 0),
                "sum_30d_usd": round(_num(leg, "netFlow30dUsd",
                                          "net_flow_30d_usd"), 0),
                "as_of": as_of}
            ingested = True
        if not ingested:
            logger.warning("bitguard_etf_fetch_failed", note="no_legs")
            return False
        self._persist()
        logger.info("bitguard_etf_updated", flows=self.verdicts())
        return True
