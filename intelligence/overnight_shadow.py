"""Overnight resting-limit shadow simulator (Strategy 8 — 2026-09-22 Governor
offensive-stack directive). SHADOW-ONLY: zero live orders, zero live sizing.
This brain SIMULATES the "leave three GTC bids below the reference price
overnight" strategy so the journal can answer, with data, whether the ladder
would have made money and what the maker-vs-taker fee saving would have been
(the Governor cares about the fee ledger).

Zero-I/O brain per docs/DEPARTMENT_TEMPLATE.md: no network, no files, no
logging. The clock is injected. All knobs are constructor-injected with
defaults. The caller drives it from a price loop:

    shadow.arm(symbol, ref_price, now_ts, notional_usd=...)
    fills   = shadow.on_price(symbol, price, ts)      # -> fill record dicts
    stopped = shadow.mark(symbol, price, ts)          # -> time-stop records
    cancels = shadow.cancel_stale(now_ts)             # -> cancel records

Append-only JSONL persistence is the CALLER's job — this brain returns
record dicts and never writes.

Doctrine: long bids fill when price <= level (a touch fills a resting bid).
A filled level is SPENT — it can never fill twice. Arms older than
stale_s (48h default) are cancelled. Open fills older than time_stop_s
(72h default) are hypothetically exited at the caller's mark with
exit_reason "time_stop". Fee ledger: a resting maker fill saves
(taker_bps - maker_bps) vs the taker fallback; recorded per fill in bps
always and in USD when the caller arms with a notional.

Telemetry namespace (when spliced): overnight_shadow_*.
"""

import time

DEFAULT_LEVELS = (0.97, 0.94, 0.91)        # × ref_price
DEFAULT_FRACTIONS = (0.5, 0.75, 1.0)       # of the armed notional
DEFAULT_STALE_S = 48.0 * 3600.0            # arm lifetime
DEFAULT_TIME_STOP_S = 72.0 * 3600.0        # per-fill hypothetical hold
DEFAULT_TAKER_BPS = 5.5                    # the taker fee being avoided
DEFAULT_MAKER_BPS = 0.0


class OvernightShadow:
    def __init__(self, levels=DEFAULT_LEVELS, fractions=DEFAULT_FRACTIONS,
                 stale_s: float = DEFAULT_STALE_S,
                 time_stop_s: float = DEFAULT_TIME_STOP_S,
                 taker_bps: float = DEFAULT_TAKER_BPS,
                 maker_bps: float = DEFAULT_MAKER_BPS,
                 clock=time.time) -> None:
        if len(levels) != len(fractions):
            raise ValueError("levels and fractions must align")
        self._levels = tuple(float(l) for l in levels)
        self._fractions = tuple(float(f) for f in fractions)
        self._stale_s = float(stale_s)
        self._time_stop_s = float(time_stop_s)
        self._taker_bps = float(taker_bps)
        self._maker_bps = float(maker_bps)
        self._clock = clock
        self._arms = {}            # symbol -> arm dict
        self._fills = {}           # symbol -> [fill dicts]

    # ── arm / cancel ─────────────────────────────────────────────────────

    def arm(self, symbol: str, ref_price: float, now_ts: float,
            notional_usd: float = None):
        """Register the three hypothetical GTC bid levels. One arm per
        symbol at a time — arming an armed symbol is a no-op (None)."""
        if not symbol or not ref_price or ref_price <= 0:
            return None
        if symbol in self._arms:
            return None
        levels = [{"level": float(ref_price) * l, "fraction": f,
                   "filled": False}
                  for l, f in zip(self._levels, self._fractions)]
        self._arms[symbol] = {
            "symbol": symbol,
            "ref_price": float(ref_price),
            "armed_ts": float(now_ts),
            "notional_usd": (float(notional_usd)
                             if notional_usd is not None else None),
            "levels": levels,
        }
        return {"event": "armed", "symbol": symbol, "ref_price": float(ref_price),
                "ts": float(now_ts),
                "levels": [{"level": lv["level"], "fraction": lv["fraction"]}
                           for lv in levels]}

    def cancel_stale(self, now_ts: float):
        """Arms older than stale_s are cancelled (edge: age >= stale_s).
        Returns cancel record dicts for the caller's ledger."""
        out = []
        for sym, arm in list(self._arms.items()):
            if float(now_ts) - arm["armed_ts"] >= self._stale_s:
                out.append({"event": "cancelled", "reason": "stale",
                            "symbol": sym, "ref_price": arm["ref_price"],
                            "armed_ts": arm["armed_ts"], "ts": float(now_ts),
                            "levels_filled": sum(1 for lv in arm["levels"]
                                                 if lv["filled"])})
                self._arms.pop(sym, None)
        return out

    # ── fills ────────────────────────────────────────────────────────────

    def _fee_fields(self, arm, fraction):
        saving_bps = self._taker_bps - self._maker_bps
        notional = (arm["notional_usd"] * fraction
                    if arm["notional_usd"] is not None else None)
        return {
            "fee_saving_bps": saving_bps,
            "fee_saving_usd": (notional * saving_bps / 1e4
                               if notional is not None else None),
            "notional_usd": notional,
        }

    def on_price(self, symbol: str, price: float, ts: float):
        """A level fills when price <= level (long resting bids). Fill price
        is the LEVEL (the resting limit price), not the trade price. A spent
        level never fills again. Returns fill record dicts."""
        if not symbol or price is None or price <= 0:
            return []
        arm = self._arms.get(symbol)
        if arm is None:
            return []
        out = []
        for lv in arm["levels"]:
            if lv["filled"] or float(price) > lv["level"]:
                continue
            lv["filled"] = True
            rec = {"event": "fill", "symbol": symbol,
                   "level": lv["level"], "fraction": lv["fraction"],
                   "fill_price": lv["level"], "trigger_price": float(price),
                   "ts": float(ts), "fill_ts": float(ts),
                   "open": True, "roe_pct": None, "mark": None,
                   **self._fee_fields(arm, lv["fraction"])}
            self._fills.setdefault(symbol, []).append(rec)
            out.append(rec)
        return out

    # ── marking / time-stops ─────────────────────────────────────────────

    def open_fills(self, symbol: str = None):
        """Open shadow fills — the caller marks these against its CURRENT
        price via mark()."""
        out = []
        for sym, fills in self._fills.items():
            if symbol is not None and sym != symbol:
                continue
            out.extend(f for f in fills if f["open"])
        return out

    def mark(self, symbol: str, price: float, ts: float):
        """Caller-provided current price: updates hypothetical ROE on every
        open fill of the symbol (long: (mark - fill)/fill x 100) and
        time-stops fills older than time_stop_s (edge: age >= time_stop_s)
        at this mark. Returns time-stop resolution records."""
        if not symbol or price is None or price <= 0:
            return []
        out = []
        for f in self._fills.get(symbol, []):
            if not f["open"]:
                continue
            roe = (float(price) - f["fill_price"]) / f["fill_price"] * 100.0
            f["mark"] = float(price)
            f["roe_pct"] = roe
            if float(ts) - f["fill_ts"] >= self._time_stop_s:
                f["open"] = False
                rec = {**f,
                       "event": "time_stop", "exit_reason": "time_stop",
                       "exit_price": float(price), "exit_ts": float(ts),
                       "pnl_usd": ((roe / 100.0) * f["notional_usd"]
                                   if f["notional_usd"] is not None else None)}
                out.append(rec)
        return out

    # ── introspection (read-only) ────────────────────────────────────────

    def armed(self, symbol: str = None):
        if symbol is not None:
            return self._arms.get(symbol)
        return dict(self._arms)
