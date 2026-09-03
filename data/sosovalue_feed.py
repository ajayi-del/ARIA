"""SoSoValue institutional-flow feed (data department, 2026-08-29).

The ETF net-flow gauge: US spot-ETF daily net inflow for BTC/ETH/SOL, from
the SoSoValue market-data API (SoDEX's sister company). This is the closest
free proxy for institutional demand — the 2026-08-28 dump printed -$201.8M
BTC net outflow the same day.

Budget doctrine (operator: demo plan, use wisely): 10k calls/month, 10rpm.
ETF flows are DAILY data finalized after US close — polling faster than
twice a day buys nothing. This module spends ≤ 2 fetches/day/symbol
(≈6 calls/day ≈ 2% of budget). fetch_due() is called hourly and only
fetches when the cached rows are stale for the current UTC hour window.

Shadow-first (Aronson): the feed measures and journals. No gate reads it
yet — the digest/watchdog correlates flow days with ARIA performance
before any wiring proposal.

Storage: logs/sosovalue_flows.json (atomic, latest per symbol) +
logs/sosovalue_flows.jsonl (append-only history) + logs/sosovalue_macro.json
(forward macro calendar). One-bad-line doctrine.

Consumers (operator directive 2026-08-29, "sharper offense"): bounded LIVE
sizing modifier (±10%, shadow-scored), cascade-aftermath tide haircut, whale
runner tide note, shadow-journal cohorts, Chancellor-facing flow poll
(telemetry — the Chancellor engine itself is never modified, rule #2).
Self-tuning runs through the existing loop: cohort expectancy in the digest →
watchdog proposals with n + effect size → deepening. Nothing auto-retunes.
"""
import json
import os
import time

import httpx
import structlog

logger = structlog.get_logger(__name__)

_BASE = "https://openapi.sosovalue.com/api/v1"
_FETCH_HOURS_UTC = (6, 22)   # after US-close data lands; twice a day max
_MACRO_FETCH_HOUR = 6        # forward macro calendar: once a day is plenty
_MATERIALITY_USD = 150_000_000   # |3d flow| below this is noise, not tide

# NYSE holiday calendar for the ETF flow dates (2025-2027). US spot ETFs do
# not trade on these days — no flow row can exist for them. A weekday NOT in
# the table is treated as a TRADING day (fail-closed: calendar uncertainty
# keeps the staleness decay running, never suppresses it).
_ETF_HOLIDAYS = frozenset({
    # 2025
    "2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26",
    "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
    # 2026
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    # 2027
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
})


def etf_calendar_adjusted_age(last_date: str, raw_age_hours: float,
                              now_ts: float) -> float:
    """Subtract 24h per non-trading date in (last_date, today_utc].

    The raw age counts wall-clock hours since the latest flow date's 00:00
    UTC — but no new data can land on weekends/holidays, so a Friday print
    reads 77h old on Monday morning and trips the >72h abstain exactly when
    the veto is needed (2026-08-31 05:24 UTC: opposed-tide ETH short leaked).
    Friday data on Monday morning is ONE trading session old (~29h), not 77.
    """
    try:
        from datetime import date, timedelta
        y, m, d = int(last_date[:4]), int(last_date[5:7]), int(last_date[8:10])
        cur = date(y, m, d) + timedelta(days=1)
        import time as _t
        today = _t.gmtime(now_ts)
        end = date(today.tm_year, today.tm_mon, today.tm_mday)
        nontrading = 0
        for _ in range(60):   # guard: beyond 60d the raw age abstains anyway
            if cur > end:
                break
            if cur.weekday() >= 5 or cur.isoformat() in _ETF_HOLIDAYS:
                nontrading += 1
            cur += timedelta(days=1)
        return max(0.0, raw_age_hours - 24.0 * nontrading)
    except Exception:
        return raw_age_hours   # fail-closed: legacy raw age


def flow_verdict(symbol: str, rows: list) -> dict:
    """Pure brain: cached ETF rows (newest-first) → evidence bundle.
    rows: [{date, total_net_inflow, total_value_traded, total_net_assets,
    cum_net_inflow}]. No gating — measurement only.

    accel_3d_usd / prev_3d_usd (2026-09-03, operator directive): the RATE OF
    CHANGE of the 3d tide — day-over-day delta of the rolling 3d sum
    (rows[0:3] vs rows[1:4] = newest day minus the day that fell out). A
    negative tide with positive accel is decelerating toward zero — the
    outflow is slowing, the tide is about to flip. None when rows < 4."""
    if not rows:
        return {"symbol": symbol, "rows": 0}
    inflows = [float(r.get("total_net_inflow") or 0.0) for r in rows[:5]]
    sign = 1 if inflows[0] > 0 else (-1 if inflows[0] < 0 else 0)
    streak = 0
    for v in inflows:
        s = 1 if v > 0 else (-1 if v < 0 else 0)
        if s == 0 or s != sign:
            break
        streak += 1
    last = rows[0]
    out = {"symbol": symbol, "rows": len(rows),
           "last_date": last.get("date"),
           "last_inflow_usd": round(inflows[0], 0),
           "sum_3d_usd": round(sum(inflows[:3]), 0),
           "streak_days": streak * sign,
           "net_assets_usd": round(float(last.get("total_net_assets") or 0.0), 0)}
    if len(inflows) >= 4:
        out["prev_3d_usd"] = round(sum(inflows[1:4]), 0)
        out["accel_3d_usd"] = round(out["sum_3d_usd"] - out["prev_3d_usd"], 0)
    else:
        out["prev_3d_usd"] = None
        out["accel_3d_usd"] = None
    return out


def tide_accel_state(verdict: dict,
                     materiality_usd: float = _MATERIALITY_USD) -> str:
    """'toward_zero' | 'away_from_zero' | 'flat' | 'unknown' — the leading
    read on the lagging tide (operator directive 2026-09-03). A tide moving
    toward zero is decelerating: an OPPOSED tide's veto premise is expiring
    (outflow slowing → flip risk); a tide moving away is strengthening.
    'flat' inside ±materiality day-over-day; 'unknown' when the 4-day window
    or a material tide is missing. Measurement only — consumers decide."""
    if not verdict or not verdict.get("rows"):
        return "unknown"
    tide = verdict.get("sum_3d_usd")
    accel = verdict.get("accel_3d_usd")
    if tide is None or accel is None:
        return "unknown"
    tide, accel = float(tide), float(accel)
    if abs(tide) < materiality_usd or abs(accel) < materiality_usd:
        return "flat" if abs(tide) >= materiality_usd else "unknown"
    toward = (tide > 0 and accel < 0) or (tide < 0 and accel > 0)
    return "toward_zero" if toward else "away_from_zero"


def etf_tide_accel_veto_enabled() -> bool:
    import os
    return os.environ.get("ETF_TIDE_ACCEL_VETO_ENABLED", "true").strip().lower() != "false"


def flow_size_mult(verdict: dict, side: str,
                   materiality_usd: float = _MATERIALITY_USD,
                   aligned_mult: float = 1.1, opposed_mult: float = 0.9,
                   age_hours: float = 0.0) -> float:
    """The tide as a bounded sizing modifier (Aronson: bound every new degree
    of freedom). 3-day summed flow ≥ +materiality = institutional bid;
    ≤ −materiality = institutional offer. Aligned trades earn aligned_mult,
    opposed trades pay opposed_mult, everything in between is 1.0. Daily-
    lagged data NEVER vetoes — it only tilts size. Missing data abstains.
    Fallback doctrine (operator: professional-grade dead-feed behavior):
    effect decays to half-strength past 36h and abstains past 72h — a stale
    tide is noise, never a crash and never a hard trade."""
    if not verdict or not verdict.get("rows"):
        return 1.0
    tide = float(verdict.get("sum_3d_usd") or 0.0)
    if abs(tide) < materiality_usd:
        return 1.0
    long_side = str(side).lower() in ("long", "buy")
    mult = aligned_mult if (tide > 0) == long_side else opposed_mult
    if age_hours > 72.0:
        return 1.0
    if age_hours > 36.0:
        return 1.0 + (mult - 1.0) * 0.5
    return mult


def tide_aligned(verdict: dict, side: str,
                 materiality_usd: float = _MATERIALITY_USD,
                 age_hours: float = 0.0) -> str:
    """'aligned' | 'opposed' | 'neutral' — the duration-class read: runners,
    pyramid adds and swing holds consult this before committing margin to
    time. Opposed tide = margin earns more elsewhere (Goldratt: the
    constraint is capital attention, not entries)."""
    if not verdict or not verdict.get("rows") or age_hours > 72.0:
        return "neutral"
    tide = float(verdict.get("sum_3d_usd") or 0.0)
    if abs(tide) < materiality_usd:
        return "neutral"
    long_side = str(side).lower() in ("long", "buy")
    return "aligned" if (tide > 0) == long_side else "opposed"


def flow_poll(verdict: dict, day_move_pct: float,
              materiality_usd: float = _MATERIALITY_USD,
              age_hours: float = 0.0) -> dict:
    """Flow/price divergence quadrant — the Chancellor's opportunity poll
    (telemetry class; the engine stays untouched). Yesterday's flow is the
    last completed institutional vote; today's day move is the price answer.
      flow ≥ +M, price down  → accumulation       (buying the dip)
      flow ≤ −M, price up    → distribution       (selling the rip)
      flow ≥ +M, price up    → confirmed_risk_on
      flow ≤ −M, price down  → confirmed_risk_off
      |flow| < M             → neutral
      no data / stale >72h   → unknown"""
    if not verdict or not verdict.get("rows") or age_hours > 72.0:
        return {"posture": "unknown", "flow_usd": 0.0,
                "day_move_pct": round(float(day_move_pct or 0.0), 3)}
    flow = float(verdict.get("last_inflow_usd") or 0.0)
    dm = float(day_move_pct or 0.0)
    if abs(flow) < materiality_usd:
        posture = "neutral"
    elif flow > 0:
        posture = "confirmed_risk_on" if dm > 0 else "accumulation"
    else:
        posture = "confirmed_risk_off" if dm < 0 else "distribution"
    return {"posture": posture, "flow_usd": round(flow, 0),
            "day_move_pct": round(dm, 3),
            "flow_date": verdict.get("last_date")}


def macro_due_today(events: list, utc_day: str) -> list:
    """Names of macro events landing on utc_day ('2026-08-31'). Pure."""
    for row in events or []:
        if row.get("date") == utc_day:
            return list(row.get("events") or [])
    return []


class SoSoValueFeed:
    """Supervised daily fetcher. fetch_due() is the only I/O entry —
    call it hourly; it self-throttles to the fetch windows."""

    def __init__(self, api_key: str, symbols=("BTC", "ETH", "SOL"),
                 country: str = "US", log_dir: str = "logs",
                 time_fn=time.time):
        self._key = api_key
        self._symbols = tuple(symbols)
        self._country = country
        self._log_dir = log_dir
        self._time = time_fn
        self._rows: dict = {}        # symbol -> rows (newest-first)
        self._macro: list = []       # forward macro calendar rows
        self._last_fetch_day: dict = {}  # (symbol, window) -> utc day fetched
        self._last_macro_day: str = ""
        self._load_cache()

    def _cache_path(self) -> str:
        return os.path.join(self._log_dir, "sosovalue_flows.json")

    def _macro_path(self) -> str:
        return os.path.join(self._log_dir, "sosovalue_macro.json")

    def _load_cache(self) -> None:
        try:
            with open(self._cache_path()) as f:
                self._rows = json.load(f)
        except Exception:
            self._rows = {}
        try:
            with open(self._macro_path()) as f:
                self._macro = json.load(f)
        except Exception:
            self._macro = []

    def _persist(self) -> None:
        try:
            tmp = self._cache_path() + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._rows, f)
            os.replace(tmp, self._cache_path())
        except Exception as e:
            logger.warning("sosovalue_persist_failed", error=str(e)[:120])
        try:
            tmp = self._macro_path() + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._macro, f)
            os.replace(tmp, self._macro_path())
        except Exception as e:
            logger.warning("sosovalue_persist_failed", error=str(e)[:120])
        try:
            with open(os.path.join(self._log_dir, "sosovalue_flows.jsonl"), "a") as f:
                f.write(json.dumps({"ts": self._time(),
                                    "flows": {s: flow_verdict(s, r)
                                              for s, r in self._rows.items()},
                                    "macro_days": len(self._macro)}) + "\n")
        except Exception:
            pass

    def verdicts(self) -> dict:
        return {s: flow_verdict(s, r) for s, r in self._rows.items()}

    def verdict(self, symbol: str) -> dict:
        return flow_verdict(symbol, self._rows.get(symbol) or [])

    def macro_events(self) -> list:
        return list(self._macro)

    def flow_age_hours(self, symbol: str) -> float:
        """Hours since the latest cached flow DATE (daily data, stamped at the
        US close it describes). 999 = no data. Consumers decay on this."""
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

    def _due(self) -> bool:
        if not self._key:
            return False
        lt = time.gmtime(self._time())
        if lt.tm_hour not in _FETCH_HOURS_UTC:
            return False
        window = (lt.tm_year, lt.tm_yday, lt.tm_hour)
        if any(self._last_fetch_day.get(s) != window for s in self._symbols):
            return True
        day = time.strftime("%Y-%m-%d", lt)
        return lt.tm_hour == _MACRO_FETCH_HOUR and self._last_macro_day != day

    async def fetch_due(self) -> bool:
        """Fetch each symbol once per fetch window (+ macro calendar once a
        day, morning window). True if any fetch ran."""
        if not self._due():
            return False
        lt = time.gmtime(self._time())
        window = (lt.tm_year, lt.tm_yday, lt.tm_hour)
        day = time.strftime("%Y-%m-%d", lt)
        fetched = False
        async with httpx.AsyncClient(timeout=15.0) as http:
            for sym in self._symbols:
                if self._last_fetch_day.get(sym) == window:
                    continue
                try:
                    r = await http.get(f"{_BASE}/etfs/summary-history",
                                       params={"symbol": sym,
                                               "country_code": self._country,
                                               "limit": 5},
                                       headers={"x-soso-api-key": self._key})
                    d = r.json()
                    if d.get("code") != 0:
                        logger.warning("sosovalue_api_error", symbol=sym,
                                       message=str(d.get("message"))[:120])
                        continue
                    rows = d.get("data") or []
                    if rows:
                        self._rows[sym] = rows
                        fetched = True
                    self._last_fetch_day[sym] = window
                except Exception as e:
                    logger.warning("sosovalue_fetch_failed", symbol=sym,
                                   error=str(e)[:120])
            if lt.tm_hour == _MACRO_FETCH_HOUR and self._last_macro_day != day:
                try:
                    r = await http.get(f"{_BASE}/macro/events",
                                       headers={"x-soso-api-key": self._key})
                    d = r.json()
                    if d.get("code") == 0 and d.get("data"):
                        self._macro = list(d["data"])
                        fetched = True
                    self._last_macro_day = day
                except Exception as e:
                    logger.warning("sosovalue_fetch_failed", symbol="MACRO",
                                   error=str(e)[:120])
        if fetched:
            self._persist()
            logger.info("sosovalue_etf_updated", flows=self.verdicts(),
                        macro_days=len(self._macro))
        return fetched
