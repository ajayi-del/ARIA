"""Token-unlock data plane (data department, 2026-10-10, Governor order).

The genuinely missing catalyst edge: intelligence/catalyst_calendar.py has
accepted token_unlock rows since birth, but logs/catalyst_events.json held
2 hand-seeded rows (stale since 09-22) and NO writer existed. This feed is
the writer.

Source (verified 2026-10-10): DeFiLlama emissions INDEX at
  https://defillama-datasets.llama.fi/emissionsIndex
Free, no key, ONE call returns ~370 protocols, each with:
  nextEventV2 {date(epoch s), unlockType, amount(tokens)}
  tokenPrice [{price, symbol, timestamp, confidence}]
  circSupply, mcap, name, protocolSlug
(api.llama.fi/emissions* went paid-only 2026; TokenUnlocks.app skipped — no
key anywhere in config/env per the Governor's no-hardcoded-credentials
rule.)

Cadence: once per UTC day (fetch_due called hourly, self-throttles) — unlock
schedules move slowly. Failure backoff: a failed day is retried next poll;
the last-good schedule stays live.

Store doctrine: logs/catalyst_events.json is merged ATOMICALLY (tmp +
os.replace) in exactly the JSONL schema load_catalysts expects. The feed
owns ONLY rows with source == "defillama_emissions" — hand-seeded and
foreign rows (governance votes, other writers) are preserved verbatim,
including unparseable lines (never destroy data). The mtime-cached reload
in main.py _offense_intel_loop picks the rewrite up on its next cadence.

Universe mapping: DeFiLlama tokenPrice symbol -> ARIA symbol for the
CURRENT book universe (injected). "PEPE" matches "1000PEPE-USD" when
"PEPE-USD" is not in the universe (1000-denominated perps). Ambiguous
double mappings keep the larger amount_usd. Entries without a price or
without a future nextEvent are skipped (amount_usd/pct unknowable).

Shadow-first: the schedule also lands in logs/unlock_schedule.json (atomic)
so the unlock_pressure_shadow hook in on_signal_ready can read
days_to_unlock / pct_supply / amount_usd without touching the calendar
store. ZERO live gating anywhere in this module.
"""
import json
import os
import time

import httpx
import structlog

logger = structlog.get_logger(__name__)

_SOURCE_URL = "https://defillama-datasets.llama.fi/emissionsIndex"
_FEED_SOURCE = "defillama_emissions"
_CONFIDENCE = 0.85   # published vesting schedule with exact amounts


def normalize_index(rows: list, universe: set, now_ts: float,
                    horizon_days: float = 45.0) -> dict:
    """Pure brain: emissionsIndex rows -> {aria_symbol: unlock info}.

    Only rows whose nextEventV2 lands within horizon_days of now, with a
    usable price, and whose ticker maps into the injected universe.
    """
    out: dict = {}
    horizon_s = float(horizon_days) * 86400.0
    for r in rows or []:
        try:
            nxt = r.get("nextEventV2") or r.get("nextEvent")
            if not nxt:
                continue
            ts = float(nxt.get("date") or 0)
            if ts <= now_ts or ts - now_ts > horizon_s:
                continue
            amount = float(nxt.get("amount") or nxt.get("toUnlock") or 0.0)
            if amount <= 0:
                continue
            prices = r.get("tokenPrice") or []
            px = None
            ticker = ""
            if prices and isinstance(prices, list):
                px = prices[0].get("price")
                ticker = str(prices[0].get("symbol") or "").upper()
            if not ticker:
                continue
            circ = float(r.get("circSupply") or 0.0)
            amount_usd = (amount * float(px)) if px else 0.0
            pct = (amount / circ * 100.0) if circ > 0 else 0.0
            sym = None
            if f"{ticker}-USD" in universe:
                sym = f"{ticker}-USD"
            elif f"1000{ticker}-USD" in universe:
                sym = f"1000{ticker}-USD"
            if sym is None:
                continue
            prev = out.get(sym)
            if prev is not None and prev.get("amount_usd", 0.0) >= amount_usd:
                continue   # ambiguous double mapping: keep the larger event
            out[sym] = {
                "symbol": sym,
                "unlock_ts": ts,
                "unlock_type": str(nxt.get("unlockType") or "unknown"),
                "amount_tokens": amount,
                "amount_usd": amount_usd,
                "pct_supply": pct,
                "protocol": str(r.get("name") or r.get("protocolSlug") or ""),
                "ticker": ticker,
            }
        except Exception:
            continue   # one bad row kills one row
    return out


def catalyst_rows(schedule: dict) -> list:
    """Pure: schedule map -> JSONL-ready catalyst rows (the feed's own)."""
    rows = []
    for sym, u in sorted(schedule.items()):
        iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(u["unlock_ts"]))
        rows.append({
            "symbol_or_cluster": sym,
            "event_type": "token_unlock",
            "ts_utc": iso,
            "source": _FEED_SOURCE,
            "confidence": _CONFIDENCE,
            "note": (f"{u['unlock_type']} {u['amount_tokens']:,.0f} {u['ticker']} "
                     f"(~${u['amount_usd']:,.0f}, {u['pct_supply']:.2f}% circ) — "
                     f"DeFiLlama emissions schedule; supply event, not direction"),
        })
    return rows


def merge_catalyst_store(existing_lines: list, feed_rows: list) -> str:
    """Pure: splice feed rows into the store, preserving everything else.

    Foreign rows pass through untouched (incl. unparseable lines verbatim —
    never destroy data); the feed's previous rows (source ==
    defillama_emissions) are replaced by feed_rows.
    """
    kept = []
    for line in existing_lines or []:
        s = line.strip() if isinstance(line, str) else ""
        if not s:
            continue
        try:
            row = json.loads(s)
            if isinstance(row, dict) and row.get("source") == _FEED_SOURCE:
                continue   # the feed re-owns its rows
        except Exception:
            pass         # unparseable: preserve verbatim below
        kept.append(s)
    kept.extend(json.dumps(r) for r in feed_rows)
    return "\n".join(kept) + ("\n" if kept else "")


class UnlockFeed:
    """Supervised daily fetcher. fetch_due() is the only I/O entry —
    call it hourly; it self-throttles to once per UTC day."""

    def __init__(self, universe: set, log_dir: str = "logs",
                 catalyst_path: str = "logs/catalyst_events.json",
                 horizon_days: float = 45.0, time_fn=time.time,
                 fetch_fn=None):
        self._universe = set(universe or set())
        self._log_dir = log_dir
        self._catalyst_path = catalyst_path
        self._horizon_days = float(horizon_days)
        self._time = time_fn
        self._fetch_fn = fetch_fn or self._default_fetch
        self._schedule: dict = {}
        self._last_fetch_day: str = ""
        self._load_cache()

    def _cache_path(self) -> str:
        return os.path.join(self._log_dir, "unlock_schedule.json")

    def _load_cache(self) -> None:
        try:
            with open(self._cache_path()) as f:
                d = json.load(f)
            if isinstance(d, dict):
                self._schedule = {k: v for k, v in d.items()
                                  if isinstance(v, dict) and "unlock_ts" in v}
        except Exception:
            self._schedule = {}

    def _persist(self) -> None:
        try:
            tmp = self._cache_path() + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._schedule, f)
            os.replace(tmp, self._cache_path())
        except Exception as e:
            logger.warning("unlock_feed_persist_failed", error=str(e)[:120])

    def _write_catalyst_store(self) -> None:
        """Atomic merge-write of the calendar store (tmp + os.replace)."""
        lines = []
        try:
            with open(self._catalyst_path, "r") as f:
                lines = f.readlines()
        except FileNotFoundError:
            lines = []
        merged = merge_catalyst_store(lines, catalyst_rows(self._schedule))
        tmp = self._catalyst_path + ".tmp"
        with open(tmp, "w") as f:
            f.write(merged)
        os.replace(tmp, self._catalyst_path)

    async def _default_fetch(self):
        async with httpx.AsyncClient(timeout=20.0) as http:
            r = await http.get(_SOURCE_URL)
            r.raise_for_status()
            d = r.json()
        return (d or {}).get("data") or []

    def due(self) -> bool:
        day = time.strftime("%Y-%m-%d", time.gmtime(self._time()))
        return self._last_fetch_day != day

    async def fetch_due(self) -> bool:
        """Fetch + normalize + merge-write, once per UTC day. True on fetch."""
        if not self.due():
            return False
        try:
            rows = await self._fetch_fn()
        except Exception as e:
            logger.warning("unlock_feed_fetch_failed", error=str(e)[:120])
            return False   # retry next hourly poll; last-good schedule stays
        self._schedule = normalize_index(
            rows, self._universe, self._time(), self._horizon_days)
        self._last_fetch_day = time.strftime(
            "%Y-%m-%d", time.gmtime(self._time()))
        try:
            self._write_catalyst_store()
        except Exception as e:
            logger.warning("unlock_feed_store_failed", error=str(e)[:120])
        self._persist()
        logger.info("unlock_feed_updated", symbols=len(self._schedule),
                    unlocks=sorted(self._schedule.keys()))
        return True

    def upcoming(self, symbol: str, now_ts: float,
                 horizon_days: float = 14.0) -> dict:
        """Unlock info for symbol within the shadow horizon, else None-ish {}."""
        u = self._schedule.get(symbol)
        if not u:
            return {}
        dt = float(u["unlock_ts"]) - float(now_ts)
        if dt < 0 or dt > float(horizon_days) * 86400.0:
            return {}
        return {**u, "days_to_unlock": dt / 86400.0}

    def schedule(self) -> dict:
        return dict(self._schedule)
