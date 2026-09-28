"""Maker-TP-at-structure shadow simulator (Strategy 3 — 2026-09-22 Governor
offensive-stack directive). SHADOW-ONLY: zero live orders, zero live sizing.

For every real entry the caller registers, this brain simulates a postOnly
GTC sell at tp1_price for a tranche of the quantity and records the
counterfactual the Governor actually cares about: did the maker TP fill, how
long did it take, and how much fee was saved vs the taker fallback.

Zero-I/O brain per docs/DEPARTMENT_TEMPLATE.md: no network, no files, no
logging. The clock is injected. All knobs are constructor-injected with
defaults. The caller drives it from its entry journal and price loop:

    shadow.on_entry(symbol, entry_price, qty, tp1_price, ts)
    records = shadow.on_price(symbol, high, low, ts)   # -> resolution dicts

Fill rule (conservative): the maker TP fills only when the bar HIGH EXCEEDS
tp1_price by fill_slop_bps (default 1.0 bps) — a touch is NOT a fill; a real
postOnly sell at tp1 needs trade-through to be honest about queue position.

Fallback: unresolved after fallback_s (72h default) -> hypothetical taker
exit at the current price proxy ((high+low)/2), exit_reason "taker_fallback",
fee_taker_bps charged.

Compare ledger: every resolution is a record dict —
    maker fill:      {symbol, would_fill_ts, maker_price, tranche_qty,
                      pnl_usd, fee_saved_usd, fee_charged_usd}
    taker fallback:  same shape, would_fill_ts=None, fee_saved_usd=0.0,
                      fee_charged_usd = tranche notional x taker_bps
The CALLER computes the counterfactual vs the real exit and owns the
append-only JSONL — this brain returns records and never writes.

Telemetry namespace (when spliced): maker_tp_shadow_*.
"""

import time

DEFAULT_TRANCHE = 0.5                  # fraction of qty sold at tp1
DEFAULT_FILL_SLOP_BPS = 1.0            # high must EXCEED tp1 by this much
DEFAULT_FALLBACK_S = 72.0 * 3600.0     # unresolved -> taker fallback
DEFAULT_TAKER_BPS = 5.5
DEFAULT_MAKER_BPS = 0.0


class MakerTpShadow:
    def __init__(self, tranche: float = DEFAULT_TRANCHE,
                 fill_slop_bps: float = DEFAULT_FILL_SLOP_BPS,
                 fallback_s: float = DEFAULT_FALLBACK_S,
                 taker_bps: float = DEFAULT_TAKER_BPS,
                 maker_bps: float = DEFAULT_MAKER_BPS,
                 clock=time.time) -> None:
        if not 0.0 < float(tranche) <= 1.0:
            raise ValueError("tranche must be in (0, 1]")
        self._tranche = float(tranche)
        self._slop_bps = float(fill_slop_bps)
        self._fallback_s = float(fallback_s)
        self._taker_bps = float(taker_bps)
        self._maker_bps = float(maker_bps)
        self._clock = clock
        self._open = {}            # symbol -> shadow dict

    # ── registration ─────────────────────────────────────────────────────

    def on_entry(self, symbol: str, entry_price: float, qty: float,
                 tp1_price: float, ts: float):
        """Register a hypothetical postOnly GTC sell at tp1_price for
        tranche x qty. One open shadow per symbol — registering over an open
        shadow is a no-op (None)."""
        if (not symbol or not entry_price or entry_price <= 0
                or not qty or qty <= 0 or not tp1_price or tp1_price <= 0):
            return None
        if symbol in self._open:
            return None
        sh = {"symbol": symbol,
              "entry_price": float(entry_price),
              "qty": float(qty),
              "tranche_qty": float(qty) * self._tranche,
              "tp1_price": float(tp1_price),
              "ts": float(ts),
              "filled": False}
        self._open[symbol] = sh
        return {"event": "registered", **{k: sh[k] for k in
                ("symbol", "entry_price", "qty", "tranche_qty",
                 "tp1_price", "ts")}}

    # ── resolution ───────────────────────────────────────────────────────

    def on_price(self, symbol: str, high: float, low: float, ts: float):
        """Advance the symbol's shadow with one price observation.
        Fill: high >= tp1 x (1 + slop_bps/1e4) (exceed, not touch).
        Fallback: unresolved and age >= fallback_s -> taker exit at the
        (high+low)/2 proxy. Returns a list of resolution record dicts
        (empty while the shadow is still open)."""
        if not symbol or high is None or low is None or high <= 0 or low <= 0:
            return []
        sh = self._open.get(symbol)
        if sh is None:
            return []
        tp1 = sh["tp1_price"]
        tq = sh["tranche_qty"]
        if float(high) >= tp1 * (1.0 + self._slop_bps / 1e4):
            rec = {"event": "resolved", "exit_reason": "maker_fill",
                   "symbol": symbol, "would_fill_ts": float(ts),
                   "maker_price": tp1, "tranche_qty": tq,
                   "hold_s": float(ts) - sh["ts"],
                   "pnl_usd": (tp1 - sh["entry_price"]) * tq,
                   "fee_saved_usd": tq * tp1 * (self._taker_bps
                                                - self._maker_bps) / 1e4,
                   "fee_charged_usd": tq * tp1 * self._maker_bps / 1e4}
            self._open.pop(symbol, None)
            return [rec]
        if float(ts) - sh["ts"] >= self._fallback_s:
            px = (float(high) + float(low)) / 2.0
            rec = {"event": "resolved", "exit_reason": "taker_fallback",
                   "symbol": symbol, "would_fill_ts": None,
                   "maker_price": tp1, "tranche_qty": tq,
                   "fallback_ts": float(ts), "fallback_price": px,
                   "hold_s": float(ts) - sh["ts"],
                   "pnl_usd": (px - sh["entry_price"]) * tq,
                   "fee_saved_usd": 0.0,
                   "fee_charged_usd": tq * px * self._taker_bps / 1e4}
            self._open.pop(symbol, None)
            return [rec]
        return []

    # ── introspection (read-only) ────────────────────────────────────────

    def open(self, symbol: str = None):
        """Open (unresolved) shadows — the no-fill open state the caller
        reports."""
        if symbol is not None:
            return self._open.get(symbol)
        return dict(self._open)
