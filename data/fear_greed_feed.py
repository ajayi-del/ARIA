"""Fear & Greed index plane (Strategy 9 regime-flip readiness leg — 2026-09-22
Governor offensive-stack directive). OBSERVER-CLASS: zero trade-path wiring,
zero gates, zero live sizing. Live use of this plane is a separate proposal
requiring shadow evidence (López de Prado / Aronson: context is not signal
until measured).

Source: https://api.alternative.me/fng/?limit=2 — free, no key. Budget: ONE
fetch per UTC day, date-disciplined cache (same doctrine as
tools/macro_posture.py stablecoin_section): today's cache serves all readers;
a failed fetch falls back to the stale cache and never raises.

Shape (department template):
  * FearGreedState — zero-I/O brain. update(value, ts) / current() /
    threshold crosses with hysteresis / the Strategy-9 F&G leg. No network,
    no files, no logging.
  * fetch_fear_greed — the ONLY network function (async httpx, 10s timeout,
    fail-open None on ANY error).
  * daily_update — the date-disciplined cache writer the future splice loop
    calls: atomic logs/fear_greed.json (tmp+replace) + append-only
    logs/fear_greed_history.jsonl (one-bad-line doctrine).

Strategy 9 boundary: flip_confirmed requires a SEPARATE BTC drop signal —
that is NOT this module's job. This plane exposes only the F&G leg
(regime_flip_fng_leg); the caller composes it with the BTC leg.

Telemetry namespace (when spliced): fear_greed_*.
Kill switch (env, default true): FEAR_GREED_ENABLED=false = cache-only,
never fetches.
"""

import json
import os
import time
from collections import deque

FNG_URL = "https://api.alternative.me/fng/?limit=2"
FETCH_TIMEOUT_S = 10.0

# Zone thresholds (injectable on the brain; module defaults for the caller).
YELLOW_THRESHOLD = 80    # extreme-greed watch
RED_THRESHOLD = 85       # extreme-greed armed (the Strategy-9 F&G leg)
HYSTERESIS = 2.0         # index points — a zone de-arms only below thr-hyst

_ZONE_CLEAR = 0
_ZONE_YELLOW = 1
_ZONE_RED = 2


def verdict(value) -> str:
    """Pure band classification. Edges: >=80 extreme_greed, 60-79 greed,
    40-59 neutral, 20-39 fear, <20 extreme_fear."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "unknown"
    if v >= 80:
        return "extreme_greed"
    if v >= 60:
        return "greed"
    if v >= 40:
        return "neutral"
    if v >= 20:
        return "fear"
    return "extreme_fear"


class FearGreedState:
    """Zero-I/O brain: the hysteresis state machine over the daily prints.

    Zones escalate the moment a threshold is crossed and de-escalate only
    below (threshold - hysteresis) — a one-day flicker at 84.9/85.1 does not
    machine-gun the Strategy-9 readiness flag.
    """

    def __init__(self, yellow: float = YELLOW_THRESHOLD,
                 red: float = RED_THRESHOLD,
                 hysteresis: float = HYSTERESIS,
                 maxlen: int = 400) -> None:
        if red < yellow:
            raise ValueError("red threshold must be >= yellow")
        self._yellow = float(yellow)
        self._red = float(red)
        self._hyst = float(hysteresis)
        self._zone = _ZONE_CLEAR
        self._zone_since_ts = None
        self._value = None
        self._ts = None
        self._history = deque(maxlen=maxlen)

    # ── core update ──────────────────────────────────────────────────────

    def update(self, value: int, ts: float) -> dict:
        """Register one print. Returns the transition record (telemetry the
        caller may log; the brain itself logs nothing)."""
        v = float(value)
        old = self._zone
        if v >= self._red:
            new = _ZONE_RED
        elif v >= self._red - self._hyst and old == _ZONE_RED:
            new = _ZONE_RED                       # sticky red
        elif v >= self._yellow:
            new = _ZONE_YELLOW
        elif v >= self._yellow - self._hyst and old >= _ZONE_YELLOW:
            new = _ZONE_YELLOW                    # sticky yellow
        else:
            new = _ZONE_CLEAR
        if new != old:
            self._zone_since_ts = ts
        self._zone = new
        self._value = v
        self._ts = ts
        self._history.append({"value": v, "ts": ts, "zone": new})
        return {
            "zone": new,
            "zone_name": ("clear" if new == _ZONE_CLEAR else
                          "yellow" if new == _ZONE_YELLOW else "red"),
            "changed": new != old,
            "crossed_above_yellow": old < _ZONE_YELLOW <= new,
            "crossed_below_yellow": old >= _ZONE_YELLOW > new,
            "crossed_above_red": old < _ZONE_RED <= new,
            "crossed_below_red": old == _ZONE_RED > new,
            "value": v,
            "ts": ts,
            "verdict": verdict(v),
        }

    # ── reads ────────────────────────────────────────────────────────────

    def current(self) -> dict:
        """Latest state; safe on a fresh brain (None value)."""
        return {
            "value": self._value,
            "ts": self._ts,
            "zone": self._zone,
            "zone_name": ("clear" if self._zone == _ZONE_CLEAR else
                          "yellow" if self._zone == _ZONE_YELLOW else "red"),
            "verdict": verdict(self._value) if self._value is not None else "unknown",
        }

    def regime_flip_fng_leg(self) -> dict:
        """The Strategy-9 F&G leg ONLY. armed == the index sits in the red
        zone (>= red threshold with hysteresis). flip_confirmed needs the
        separate BTC drop leg — composed by the CALLER, never here."""
        return {
            "armed": self._zone == _ZONE_RED,
            "zone": self._zone,
            "value": self._value,
            "since_ts": self._zone_since_ts if self._zone == _ZONE_RED else None,
            "red_threshold": self._red,
        }


# ── fetcher (the only I/O in this module) ───────────────────────────────────

async def fetch_fear_greed(url: str = FNG_URL,
                           timeout: float = FETCH_TIMEOUT_S):
    """One fetch. Fail-open None on ANY error (network, shape, parse) — a
    dark sentiment plane must never raise into the caller."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url)
            d = r.json()
        data = d.get("data") or []
        if not data:
            return None
        head = data[0]
        value = int(head["value"])
        return {
            "value": value,
            "classification": head.get("value_classification"),
            "verdict": verdict(value),
            "timestamp": head.get("timestamp"),
            "prev_value": (int(data[1]["value"]) if len(data) > 1 else None),
        }
    except Exception:
        return None


# ── persistence (date-disciplined, atomic, one-bad-line) ───────────────────

def _load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def load_history(path):
    """Append-only JSONL read under the one-bad-line doctrine: a corrupt
    line kills one record, never the store."""
    rows = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return rows


def _atomic_write(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, path)


async def daily_update(log_dir: str, now: float = None, fetch_fn=None,
                       enabled: bool = None) -> dict:
    """ONE fetch per UTC day, date-disciplined (macro_posture doctrine):
    today's cache serves; otherwise fetch -> atomic cache + history line;
    failure -> stale-cache fallback; kill switch FEAR_GREED_ENABLED=false =
    cache-only. Never raises; origin field says what happened."""
    now = time.time() if now is None else float(now)
    if fetch_fn is None:
        fetch_fn = fetch_fear_greed
    if enabled is None:
        enabled = os.environ.get("FEAR_GREED_ENABLED", "true").lower() != "false"
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    cache_path = os.path.join(log_dir, "fear_greed.json")
    cache = _load_json(cache_path, {})
    if cache.get("date") == today and cache.get("data"):
        return {"origin": "cache_hit", "date": today, "data": cache["data"],
                "stale": False}
    if enabled:
        try:
            data = await fetch_fn()
        except Exception:
            data = None
        if data:
            _atomic_write(cache_path,
                          {"date": today, "fetched_ts": now, "data": data})
            try:
                hist_path = os.path.join(log_dir, "fear_greed_history.jsonl")
                with open(hist_path, "a") as f:
                    f.write(json.dumps({
                        "ts": now, "date": today,
                        "value": data.get("value"),
                        "classification": data.get("classification"),
                        "verdict": data.get("verdict"),
                    }) + "\n")
            except Exception:
                pass
            return {"origin": "fetched", "date": today, "data": data,
                    "stale": False}
    if cache.get("data"):
        return {"origin": "stale_cache", "date": cache.get("date"),
                "data": cache["data"], "stale": True}
    return {"origin": "none", "date": today, "data": None, "stale": True}
