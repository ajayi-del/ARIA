"""
Post-only shadow arm (2026-09-16 Governor Phase 0 — measure-only).

For every REAL bracket opened, register a shadow paper limit order at touch.
Resolution on subsequent mark updates for that symbol:
  filled — mark touches entry-or-better within 120s
           (long: mark <= entry; short: mark >= entry)
  missed — no touch within 120s
Markouts recorded at +10s and +60s (direction-adjusted bp vs entry).

One JSON line per resolved shadow appended to logs/postonly_shadow.jsonl.
Everything here is measure-only: no method may raise into trading code.
"""

import json
import time
from collections import OrderedDict
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

SHADOW_PATH = Path("logs/postonly_shadow.jsonl")
FILL_TIMEOUT_S = 120.0
_MARKOUT_HORIZONS_S = (10.0, 60.0)
_MAX_OPEN = 64


class PostOnlyShadow:
    def __init__(self, path=SHADOW_PATH, clock=time.monotonic) -> None:
        self._path = Path(path)
        self._clock = clock  # monotonic — the 120s timeout must survive wall-clock jumps
        self._open: "OrderedDict[str, dict]" = OrderedDict()
        self._seq = 0

    def register(self, symbol: str, side: str, entry: float, venue: str = None) -> None:
        """Register a shadow paper order at touch for a real bracket open."""
        try:
            if not symbol or side not in ("long", "short") or not entry or entry <= 0:
                return
            self._seq += 1
            sid = f"{symbol}_{int(time.time() * 1000)}_{self._seq}"
            self._open[sid] = {
                "id": sid,
                "ts": time.time(),
                "symbol": symbol,
                "side": side,
                "entry": float(entry),
                "venue": venue,
                "t0": self._clock(),
                "filled": False,
                "fill_delay_s": None,
                "marks": {},
            }
            while len(self._open) > _MAX_OPEN:
                self._open.popitem(last=False)  # drop oldest unresolved
        except Exception:
            pass

    def on_mark(self, symbol: str, mark_price: float) -> None:
        """Advance every open shadow on this symbol with a fresh mark."""
        try:
            if not symbol or mark_price is None or mark_price <= 0:
                return
            now = self._clock()
            resolved = []
            for sid, sh in self._open.items():
                if sh["symbol"] != symbol:
                    continue
                self._advance(sh, float(mark_price), now)
                if sh.pop("_resolved", False):
                    resolved.append(sid)
            for sid in resolved:
                self._open.pop(sid, None)
        except Exception:
            pass

    # ── Internals ─────────────────────────────────────────────────────────────

    def _advance(self, sh: dict, mark: float, now: float) -> None:
        elapsed = now - sh["t0"]
        if not sh["filled"] and elapsed <= FILL_TIMEOUT_S:
            touched = (mark <= sh["entry"]) if sh["side"] == "long" else (mark >= sh["entry"])
            if touched:
                sh["filled"] = True
                sh["fill_delay_s"] = round(elapsed, 3)
        for horizon in _MARKOUT_HORIZONS_S:
            if horizon not in sh["marks"] and elapsed >= horizon:
                sh["marks"][horizon] = self._markout_bp(sh, mark)
        if sh["filled"] and len(sh["marks"]) == len(_MARKOUT_HORIZONS_S):
            self._resolve(sh)
        elif elapsed >= FILL_TIMEOUT_S:
            for horizon in _MARKOUT_HORIZONS_S:
                sh["marks"].setdefault(horizon, self._markout_bp(sh, mark))
            self._resolve(sh)

    @staticmethod
    def _markout_bp(sh: dict, mark: float) -> float:
        direction = 1.0 if sh["side"] == "long" else -1.0
        return round(direction * (mark - sh["entry"]) / sh["entry"] * 1e4, 3)

    def _resolve(self, sh: dict) -> None:
        sh["_resolved"] = True
        m10 = sh["marks"].get(10.0)
        m60 = sh["marks"].get(60.0)
        rec = {
            "ts": sh["ts"],
            "symbol": sh["symbol"],
            "side": sh["side"],
            "entry": sh["entry"],
            "venue": sh["venue"],
            "filled": sh["filled"],
            "fill_delay_s": sh["fill_delay_s"],
            "markout_10s_bp": m10,
            "markout_60s_bp": m60,
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception as e:
            log.error("postonly_shadow_write_error", error=str(e))
        log.info("shadow_resolved",
                 symbol=sh["symbol"],
                 filled=sh["filled"],
                 fill_delay_s=sh["fill_delay_s"],
                 markout_10s_bp=m10,
                 markout_60s_bp=m60)
