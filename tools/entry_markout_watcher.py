#!/usr/bin/env python3
"""Entry markout / adverse-selection watcher (SIG5 diagnostic, MEASURE-ONLY).

Tail-follows logs/aria.log for entry evidence:
  - primary: TRADE_PLACED alert events (monitoring.alerts) carrying
    symbol, side (LONG/SHORT), Entry price in the message text.
  - fallback: bracket_placed success events (__main__) carrying symbol +
    entry but no side; used only when no alert was seen for that symbol
    recently, and enriched if a matching alert arrives within 90s.

For each entry: captures venue mark at detection, +5s, +10s, +60s, then
appends one JSON line to logs/entry_markouts.jsonl with direction-adjusted
adverse selection in basis points (negative = adverse selection).

Venue routing: 54-symbol Aster list -> Aster premiumIndex; else SoDEX
batch mark-prices endpoint. Fail-silent: per-capture try/except, errors
recorded as mark=null with an error field; never raises, never retries
in a storm.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

ARIA_HOME = os.path.expanduser("~/ARIA")
LOG_PATH = os.path.join(ARIA_HOME, "logs", "aria.log")
OUT_PATH = os.path.join(ARIA_HOME, "logs", "entry_markouts.jsonl")

SODEX_MARK_URL = "https://mainnet-gw.sodex.dev/api/v1/perps/markets/mark-prices"
ASTER_MARK_URL = "https://fapi.asterdex.com/fapi/v3/premiumIndex"

ASTER_BARE_SYMBOLS = {
    "HYPE", "ADA", "UNI", "ONDO", "TAO", "ENA", "KAITO", "WIF", "ZEC",
    "VIRTUAL", "AAVE", "1000BONK", "SEI", "PENGU", "INJ", "TIA", "APT",
    "TRX", "BCH", "XLM", "FARTCOIN", "VELVET", "AKE", "CYS", "ASTER",
    "ACE", "MUBARAK", "DOS", "SNXX", "HEMI", "AIO", "ARIA", "XAUT",
    "CL", "TSM", "ORCL", "DOGE", "XRP", "1000PEPE", "SUI", "AVAX",
    "LINK", "LTC", "NEAR", "WLD", "BOME", "ICP", "XMR", "ORDI", "WLFI",
    "LIT", "PAXG", "FLOCK", "FF",
}
# Aster wire-symbol overrides (mirror execution/aster_client.py doctrine).
ASTER_SYM_OVERRIDE = {"XAUT-USD": "XAUUSDT"}

MAX_IN_FLIGHT = 32
CAPTURE_OFFSETS_S = (("mark_5s", 5.0), ("mark_10s", 10.0), ("mark_60s", 60.0))
DEDUP_WINDOW_S = 90.0
HTTP_TIMEOUT_S = 6.0
SODEX_CACHE_TTL_S = 2.0
POLL_IDLE_S = 0.25

# "ARIA placed *SHORT* VIRTUAL-USD\nEntry: 0.59 | Stop: 0.62"
RE_ALERT = re.compile(
    r"placed\s+\*(LONG|SHORT)\*\s+([A-Za-z0-9]+)-USD.*?Entry:\s*([0-9]*\.?[0-9]+)",
    re.DOTALL,
)


def log_note(msg: str) -> None:
    try:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        print(f"{ts} entry_markout_watcher: {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


def parse_iso_z(ts: str) -> float | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def iso_z(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


def parse_entry_line(line: str) -> dict | None:
    """Parse one log line into entry evidence, or None.

    Returns dict with keys: source, symbol, side (may be None),
    entry_price (may be None), entry_ts (epoch float, may be None).
    """
    line = line.strip()
    if not line:
        return None
    try:
        rec = json.loads(line)
    except Exception:
        rec = None

    if isinstance(rec, dict):
        msg = rec.get("message") or ""
        if rec.get("event") == "alert" and "TRADE_PLACED" in msg:
            m = RE_ALERT.search(msg)
            if m:
                side, sym_bare, entry = m.group(1), m.group(2), m.group(3)
                try:
                    entry_price = float(entry)
                except Exception:
                    entry_price = None
                return {
                    "source": "alert",
                    "symbol": f"{sym_bare}-USD",
                    "side": side,
                    "entry_price": entry_price,
                    "entry_ts": parse_iso_z(rec.get("timestamp", "")),
                }
            # Alert seen but regex ambiguous -> ambiguous evidence; the
            # bracket fallback path will pick the entry up if it exists.
            return None
        if rec.get("event") == "bracket_placed" and rec.get("symbol"):
            try:
                entry_price = float(rec.get("entry"))
            except Exception:
                entry_price = None
            return {
                "source": "bracket",
                "symbol": str(rec["symbol"]),
                "side": None,
                "entry_price": entry_price,
                "entry_ts": parse_iso_z(rec.get("timestamp", "")),
            }
        return None

    # Non-JSON fallback: raw text containing the alert body.
    if "TRADE_PLACED" in line:
        m = RE_ALERT.search(line)
        if m:
            side, sym_bare, entry = m.group(1), m.group(2), m.group(3)
            try:
                entry_price = float(entry)
            except Exception:
                entry_price = None
            return {
                "source": "alert_raw",
                "symbol": f"{sym_bare}-USD",
                "side": side,
                "entry_price": entry_price,
                "entry_ts": None,
            }
    return None


def venue_for(symbol: str) -> str:
    bare = symbol.split("-")[0].upper()
    return "aster" if bare in ASTER_BARE_SYMBOLS else "sodex"


def to_aster_symbol(canonical: str) -> str:
    if canonical in ASTER_SYM_OVERRIDE:
        return ASTER_SYM_OVERRIDE[canonical]
    return canonical.replace("-USD", "USDT").replace("-", "")


class SodexMarkCache:
    def __init__(self) -> None:
        self._ts = 0.0
        self._map: dict[str, float] = {}

    def get(self, symbol: str) -> tuple[float | None, str | None]:
        now = time.time()
        if now - self._ts > SODEX_CACHE_TTL_S:
            try:
                r = requests.get(SODEX_MARK_URL, timeout=HTTP_TIMEOUT_S)
                data = r.json().get("data") or []
                self._map = {
                    str(d["symbol"]): float(d["markPrice"])
                    for d in data
                    if d.get("symbol") and d.get("markPrice") not in (None, "")
                }
                self._ts = now
            except Exception as e:  # noqa: BLE001 - fail silent by design
                return None, f"sodex_fetch:{type(e).__name__}"
        if symbol in self._map:
            return self._map[symbol], None
        return None, "sodex_symbol_missing"


def fetch_aster_mark(symbol: str) -> tuple[float | None, str | None]:
    try:
        r = requests.get(
            ASTER_MARK_URL,
            params={"symbol": to_aster_symbol(symbol)},
            timeout=HTTP_TIMEOUT_S,
        )
        mp = r.json().get("markPrice")
        if mp in (None, ""):
            return None, "aster_mark_missing"
        return float(mp), None
    except Exception as e:  # noqa: BLE001
        return None, f"aster_fetch:{type(e).__name__}"


class EntryRecord:
    __slots__ = (
        "symbol", "side", "entry_price", "entry_ts", "detection_ts",
        "venue", "marks", "errors", "pending", "source",
    )

    def __init__(self, evidence: dict, detection_ts: float) -> None:
        self.symbol = evidence["symbol"]
        self.side = evidence.get("side")
        self.entry_price = evidence.get("entry_price")
        self.entry_ts = evidence.get("entry_ts") or detection_ts
        self.detection_ts = detection_ts
        self.venue = venue_for(self.symbol)
        self.marks: dict[str, float | None] = {}
        self.errors: dict[str, str] = {}
        self.pending: list[tuple[float, str]] = [
            (detection_ts + off, name) for name, off in CAPTURE_OFFSETS_S
        ]
        self.source = evidence.get("source", "?")

    @property
    def complete(self) -> bool:
        return not self.pending

    def capture(self, name: str, sodex_cache: SodexMarkCache) -> None:
        try:
            if self.venue == "aster":
                mark, err = fetch_aster_mark(self.symbol)
            else:
                mark, err = sodex_cache.get(self.symbol)
            self.marks[name] = mark
            if err:
                self.errors[name] = err
        except Exception as e:  # noqa: BLE001 - belt-and-suspenders
            self.marks[name] = None
            self.errors[name] = f"capture:{type(e).__name__}"

    def as_bp(self, mark: float | None) -> float | None:
        if mark is None or not self.entry_price or self.entry_price <= 0:
            return None
        sign = 1 if self.side == "LONG" else -1 if self.side == "SHORT" else None
        if sign is None:
            return None
        return round((mark - self.entry_price) / self.entry_price * 1e4 * sign, 3)

    def to_jsonl(self) -> str:
        detect_lag = round(self.detection_ts - self.entry_ts, 3)
        out = {
            "entry_ts": iso_z(self.entry_ts),
            "detection_ts": iso_z(self.detection_ts),
            "detect_lag_s": detect_lag,
            "symbol": self.symbol,
            "side": self.side,
            "venue": self.venue,
            "entry_price": self.entry_price,
            "mark_at_detection": self.marks.get("mark_at_detection"),
            "mark_5s": self.marks.get("mark_5s"),
            "mark_10s": self.marks.get("mark_10s"),
            "mark_60s": self.marks.get("mark_60s"),
            "as_5s_bp": self.as_bp(self.marks.get("mark_5s")),
            "as_10s_bp": self.as_bp(self.marks.get("mark_10s")),
            "as_60s_bp": self.as_bp(self.marks.get("mark_60s")),
            "source": self.source,
        }
        if self.errors:
            out["error"] = self.errors
        return json.dumps(out, separators=(",", ":"))


class LogTailer:
    """Tail-follow with rotation/truncation handling; starts at EOF."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._fh = None
        self._ino = None
        self._open_at_eof()

    def _open_at_eof(self) -> None:
        try:
            if self._fh:
                self._fh.close()
            self._fh = open(self.path, "r", encoding="utf-8", errors="replace")
            self._fh.seek(0, os.SEEK_END)
            self._ino = os.fstat(self._fh.fileno()).st_ino
        except Exception as e:  # noqa: BLE001
            self._fh = None
            self._ino = None
            log_note(f"open failed: {type(e).__name__}")

    def _maybe_reopen(self) -> None:
        try:
            st = os.stat(self.path)
            if self._fh is None or st.st_ino != self._ino:
                # Rotated/replaced: read the fresh file from its start.
                self._fh = open(self.path, "r", encoding="utf-8", errors="replace")
                self._ino = st.st_ino
                log_note("rotation detected; reopened new file")
            elif st.st_size < self._fh.tell():
                self._fh.seek(0)
                log_note("truncation detected; seeked to 0")
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            log_note(f"reopen check: {type(e).__name__}")

    def read_new_lines(self) -> list[str]:
        if self._fh is None:
            self._open_at_eof()
            return []
        self._maybe_reopen()
        try:
            return self._fh.readlines()
        except Exception:  # noqa: BLE001
            return []


def append_jsonl(line: str) -> None:
    try:
        with open(OUT_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception as e:  # noqa: BLE001
        log_note(f"jsonl append failed: {type(e).__name__}")


def selftest() -> int:
    """Parse recent history and print parsed entries; writes nothing.

    Scans the last ~64MB of the log (entry events are sparse in a very
    chatty log); drops the first partial line after the seek.
    """
    try:
        size = os.path.getsize(LOG_PATH)
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(max(0, size - 64 * 1024 * 1024))
            fh.readline()  # discard partial line
            lines = fh.readlines()
    except Exception as e:  # noqa: BLE001
        print(f"selftest: cannot read {LOG_PATH}: {e}")
        return 1
    parsed = []
    for line in lines:
        ev = parse_entry_line(line)
        if ev:
            parsed.append(ev)
    alerts = [p for p in parsed if p["source"].startswith("alert")]
    brackets = [p for p in parsed if p["source"] == "bracket"]
    print(f"selftest: scanned {len(lines)} lines -> "
          f"{len(alerts)} TRADE_PLACED alerts, {len(brackets)} bracket_placed")
    print("--- last 5 parsed alerts ---")
    for p in alerts[-5:]:
        print(json.dumps(p))
    print("--- last 3 parsed bracket_placed ---")
    for p in brackets[-3:]:
        print(json.dumps(p))
    print("--- venue routing check (parsed symbols) ---")
    for sym in sorted({p["symbol"] for p in parsed}):
        print(f"  {sym} -> {venue_for(sym)}")
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()

    log_note(f"starting; tailing {LOG_PATH} at EOF; out={OUT_PATH}")
    tailer = LogTailer(LOG_PATH)
    sodex_cache = SodexMarkCache()
    in_flight: list[EntryRecord] = []

    while True:
        try:
            now = time.time()

            for line in tailer.read_new_lines():
                try:
                    ev = parse_entry_line(line)
                except Exception:  # noqa: BLE001
                    ev = None
                if not ev:
                    continue
                det_ts = time.time()
                if ev["source"].startswith("alert"):
                    # Enrich a side-less bracket fallback if one is recent.
                    match = next(
                        (r for r in in_flight
                         if r.symbol == ev["symbol"] and r.side is None
                         and det_ts - r.detection_ts < DEDUP_WINDOW_S),
                        None,
                    )
                    if match:
                        match.side = ev["side"]
                        if ev.get("entry_price"):
                            match.entry_price = ev["entry_price"]
                        if ev.get("entry_ts"):
                            match.entry_ts = ev["entry_ts"]
                        match.source = "bracket+alert"
                        log_note(f"enriched {ev['symbol']} side={ev['side']}")
                        continue
                    rec = EntryRecord(ev, det_ts)
                else:  # bracket fallback
                    dup = any(
                        r.symbol == ev["symbol"]
                        and det_ts - r.detection_ts < DEDUP_WINDOW_S
                        for r in in_flight
                    )
                    if dup:
                        continue
                    rec = EntryRecord(ev, det_ts)
                rec.capture("mark_at_detection", sodex_cache)
                in_flight.append(rec)
                log_note(
                    f"entry {rec.symbol} side={rec.side} "
                    f"entry={rec.entry_price} venue={rec.venue} "
                    f"src={rec.source}"
                )
                # Cap in-flight: drop oldest beyond the cap.
                while len(in_flight) > MAX_IN_FLIGHT:
                    dropped = in_flight.pop(0)
                    log_note(f"cap drop: {dropped.symbol} "
                             f"(complete={dropped.complete})")

            now = time.time()
            for rec in list(in_flight):
                due = [p for p in rec.pending if p[0] <= now]
                if not due:
                    continue
                for due_ts, name in due:
                    rec.capture(name, sodex_cache)
                rec.pending = [p for p in rec.pending if p[0] > now]
                if rec.complete:
                    append_jsonl(rec.to_jsonl())
                    in_flight.remove(rec)

            time.sleep(POLL_IDLE_S)
        except Exception as e:  # noqa: BLE001 - fail silent, keep looping
            log_note(f"main loop: {type(e).__name__}: {e}")
            time.sleep(2.0)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:  # noqa: BLE001
        log_note(f"fatal: {type(e).__name__}: {e}")
        sys.exit(0)  # fail silent even on death
