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

HISTORY BOOTSTRAP (2026-10-18 Governor BUILD ORDER, ETF acceleration): the
etf_flow_ratio percentile needs >=20 daily points but passive accumulation
serves one row per asOfDate (self-heals ~Oct 22). bootstrap_history()
probes the base URL for a history-shaped endpoint ({base}/history?days=30)
ONCE at boot; >=20 usable daily points per asset pre-populates the _rows
cache (the percentile history buffer the splice reads via _etf_rows) and
emits etf_percentile_bootstrapped with the point count. 404 / unsupported /
short / unparseable = debug log, no-op — passive accumulation continues.
NEVER fabricates a point: only real rows the endpoint actually served are
ingested. Kill switch BITGUARD_ETF_BOOTSTRAP_ENABLED default "true".
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
_HISTORY_CAP = 35           # days of daily rows retained — 35 holds a 30-day
                            # bootstrap backfill plus daily accrual headroom;
                            # the etf_flow_ratio percentile needs >=20 points
                            # (was 10 — the percentile could never go live)


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


_FLOW_KEYS = ("netFlowUsd", "netFlowYesterdayUsd", "net_flow_usd",
              "totalNetInflowUsd", "total_net_inflow", "netFlow")
_AUM_KEYS = ("totalNetAssetsUsd", "netAssetsUsd", "net_assets_usd",
             "totalNetAssets", "aumUsd", "total_net_assets")


def _history_day_list(payload) -> list:
    """Unwrap a history-shaped payload to the list of day entries. Accepts a
    bare list or dict wrappers (data / history / days / rows, one level of
    nesting inside data). [] on anything else — fail-closed."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("data", "history", "days", "rows"):
        v = payload.get(key)
        if isinstance(v, list):
            return v
    d = payload.get("data")
    if isinstance(d, dict):
        for key in ("history", "days", "rows", "items"):
            v = d.get(key)
            if isinstance(v, list):
                return v
    return []


def _leg_to_row(date: str, leg: dict) -> dict:
    """One history leg dict -> the SoSoValue row shape (same conversion as
    the snapshot path). The aum field is required > 0 by the caller — a
    percentile point needs a real denominator."""
    return {"date": date,
            "total_net_inflow": _num(leg, *_FLOW_KEYS),
            "total_value_traded": _num(leg, "totalValueTraded",
                                       "total_value_traded"),
            "total_net_assets": _num(leg, *_AUM_KEYS),
            "cum_net_inflow": _num(leg, "cumNetInflowUsd",
                                   "cum_net_inflow")}


def _parse_history_days(payload, symbols) -> dict:
    """Parse the probe payload to {SYM: [row, ...]} (newest-first, usable
    points only: valid date + aum > 0). Handles per-day nested legs
    ({date, btc: {...}}) and flat per-asset rows ({date, asset, ...}).
    Never fabricates — only rows the endpoint actually served."""
    days = _history_day_list(payload)
    out: dict = {s: {} for s in symbols}          # sym -> {date: row}
    for entry in days:
        if not isinstance(entry, dict):
            continue
        date = str(entry.get("date") or entry.get("asOfDate")
                    or entry.get("as_of_date") or entry.get("day") or "")
        if len(date) != 10:
            continue
        # Shape A: nested per-symbol legs on one day entry.
        nested = False
        for sym in symbols:
            leg = entry.get(sym.lower()) or entry.get(sym.upper())
            if isinstance(leg, dict) and leg:
                row = _leg_to_row(date, leg)
                if row["total_net_assets"] > 0:
                    out[sym][date] = row
                    nested = True
        if nested:
            continue
        # Shape B: flat per-asset row.
        asset = str(entry.get("asset") or entry.get("symbol") or "").upper()
        if asset in out:
            row = _leg_to_row(date, entry)
            if row["total_net_assets"] > 0:
                out[asset][date] = row
    return {s: sorted(rows.values(), key=lambda r: r["date"], reverse=True)
            for s, rows in out.items() if rows}


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

    # ---- history bootstrap (boot-time percentile acceleration) ----

    def _history_url(self) -> str:
        """The history-shaped endpoint derived from the snapshot URL —
        {base}/history. "" when the configured URL has no /snapshot tail
        to replace (fail-closed: no guess at an unknown path layout)."""
        tail = "/snapshot"
        if self._url and self._url.endswith(tail):
            return self._url[:-len(tail)] + "/history"
        return ""

    async def bootstrap_history(self, days: int = 30, min_points: int = 20,
                                fetch_fn=None) -> int:
        """One-shot boot probe for a daily-flow history endpoint. When the
        endpoint serves >= min_points usable daily points for an asset, the
        asset's _rows cache is backfilled (dates the live snapshot already
        owns win — freshest-wins) and etf_percentile_bootstrapped is
        logged with the point count. 404 / unsupported / short /
        unparseable = debug log, no-op (passive accumulation continues).
        NEVER fabricates a point. Returns the per-asset populated total."""
        if os.environ.get("BITGUARD_ETF_BOOTSTRAP_ENABLED", "true") \
                .strip().lower() == "false":
            return 0
        url = self._history_url()
        if not url:
            logger.debug("bitguard_history_probe_skipped", note="no_history_url")
            return 0
        try:
            if fetch_fn is not None:
                payload = await fetch_fn(url, {"days": days})
            else:
                async with httpx.AsyncClient(timeout=15.0) as http:
                    r = await http.get(url, params={"days": days})
                    if r.status_code != 200:
                        logger.debug("bitguard_history_probe_unsupported",
                                     status=r.status_code)
                        return 0
                    payload = r.json()
        except Exception as e:
            logger.debug("bitguard_history_probe_failed", error=str(e)[:120])
            return 0
        parsed = _parse_history_days(payload, self._symbols)
        if not parsed:
            logger.debug("bitguard_history_probe_short", points=0)
            return 0
        populated = 0
        for sym in self._symbols:
            hist = parsed.get(sym) or []
            if len(hist) < min_points:
                logger.debug("bitguard_history_probe_short",
                             asset=sym, points=len(hist))
                continue
            by_date = {str(r.get("date")): r
                       for r in (self._rows.get(sym) or [])
                       if isinstance(r, dict)}
            for row in hist:                    # live rows win on a date tie
                by_date.setdefault(row["date"], row)
            merged = sorted(by_date.values(),
                            key=lambda r: str(r.get("date") or ""),
                            reverse=True)[:_HISTORY_CAP]
            self._rows[sym] = merged
            populated += len(hist)
            logger.info("etf_percentile_bootstrapped",
                        asset=sym, points=len(hist))
        if populated:
            self._persist()
        return populated
