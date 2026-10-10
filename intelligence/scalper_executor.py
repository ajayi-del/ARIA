"""
intelligence/scalper_executor.py — parasite-scalper EXECUTION GLUE
(Governor order 2026-10-10: the scalper goes LIVE alongside campaign
trades — not shadow-only).

The brain (intelligence/parasite_scalper.py, audited CONFORMANT zero-I/O)
emits intents, never orders — its contract explicitly leaves order
placement out: "that gate lives at the integrator's order-placement
site". This module IS that site. It consumes ScalpSignalGrade verdicts
(or equivalent dicts) and turns Grade-A intents into real SoDEX orders
inside a hard admission envelope.

Live envelope (all knobs getattr-with-default on the injected cfg; the
integrator owns the config block and the main.py call site — neither
lives in this module):
  scalper_enabled              False  LIVE kill switch — False = every
                                intent dropped with reason="disabled",
                                zero venue calls (pre-module bit-for-bit)
  scalper_min_score_live       70     Grade-A floor for live execution
  scalper_max_concurrent       3      open scalper positions cap
  scalper_min_notional_usd     5.0    notional clamp floor
  scalper_max_notional_usd     15.0   notional clamp ceiling
  scalper_margin_budget_usd    8.0    total scalper margin budget;
                                projected margin (notional/leverage) that
                                would exceed budget-minus-open-margin is
                                a HARD clamp — the intent is dropped,
                                never resized past the budget
  scalper_max_leverage         8      hard leverage cap

Order shape (mirrors the campaign/fast-cycle idiom, main.py:24624 +
sodex_client.py:2121):
  - Maker-first resting LIMIT (order_type=1, tif=1 GTC) at the touch —
    NEVER market. One venue call carries the full bracket as a 3-item
    batch: entry + reduce-only TP limit + reduce-only stop (stop_type=1
    STOP_LOSS, trigger_type=2 MARK_PRICE — no wick fills).
  - TP = entry x (1 +/- tp_pct), stop = entry x (1 -/+ stop_pct) per
    intent direction; tp_pct/stop_pct ride the intent (brain doctrine:
    1.5%/0.8% reversal, 1.2%/0.6% predictive) with 1.5%/0.8% defaults.
  - Client-order-id namespaced "scalper_<symbol>_<ts_ms>" (36-char cap,
    venue rule) so startup-sync adoption and reconcile() can classify
    scalper-owned positions exchange-side.
  - strategy_type="parasite_scalper" is stamped on every journal row so
    outcomes.db attribution treats the scalper as a first-class managed
    strategy.

Fail-closed doctrine:
  - ANY exception or rejection from the venue -> the intent is dropped,
    a dense journal row {event:"scalper_exec_dropped", reason:...} is
    returned, and the exception NEVER propagates into the hot path.
  - No retry inside the same call — EXCEPT one retry with a fresh
    client-order-id for explicit nonce-stale rejections ("nonce" in the
    venue error). Everything else drops on first failure.
  - The brain's Grade-B intents are never executed (they stay
    shadow-journaled brain-side); a grade-B intent reaching this module
    is dropped with reason="grade".

Journal rows (density law — one-line reproducible records, no prose):
  scalper_exec_placed   one per executed intent (full geometry + ids)
  scalper_exec_dropped  one per dropped intent (reason-coded)

reconcile(open_positions) is a PURE function: given venue-open position
dicts/objects it tags which are scalper-owned (client-order-id prefix
"scalper_" or strategy_type "parasite_scalper") so the integrator's
startup-sync adoption can classify them. Zero I/O, never raises.

Integration note (integrator-owned, NOT in this module): the one-line
main.py splice shape is
    rows = await _scalper_executor.execute_intents(
        grades, _open_scalper_positions(),
        {"account_id": NUMERIC_ACCOUNT_ID, "marks": _marks})
    for _r in rows: logger.info(_r.pop("event"), **_r)
Leverage setting is integrator-owned (campaign hold idiom) — this module
computes margin from the intent's leverage but never calls the venue's
leverage endpoint.
"""
from __future__ import annotations

import math
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

SCHEMA_VERSION = 1

STRATEGY_TYPE = "parasite_scalper"
CLORD_PREFIX = "scalper_"
_CLORD_MAX = 36                     # venue client-order-id cap (wire rule)

# Grade / geometry doctrine (mirrors parasite_scalper.py constants).
GRADE_A = "A"
DEFAULT_TP_PCT = 0.015              # brain reversal geometry
DEFAULT_STOP_PCT = 0.008
HARD_MAX_LEVERAGE = 8

# SoDEX order-item wire constants (sodex_client.py idiom, cited above).
_SIDE_BUY = 1
_SIDE_SELL = 2
_TYPE_LIMIT = 1
_TIF_GTC = 1
_STOP_TYPE_LOSS = 1                 # STOP_LOSS
_TRIGGER_MARK = 2                   # MARK_PRICE — no wick fills
_POSITION_SIDE_BOTH = 1             # SoDEX one-way mode


def _f(x) -> Optional[float]:
    """Coerce to float; None on any garbage (fail-closed idiom)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return v


def _ig(intent, key: str, default=None):
    """Intent get: mapping key first, then attribute (ScalpSignalGrade is
    a frozen dataclass; the integrator may also hand us plain dicts)."""
    if isinstance(intent, Mapping):
        return intent.get(key, default)
    return getattr(intent, key, default)


def _fmt_decimal(d: Decimal) -> str:
    """Canonical decimal string, no trailing zeros (venue wire rule —
    SoDEX rejects "0.710", accepts "0.71")."""
    s = format(d.normalize(), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _round_price(price: float, tick: float, side: str = "") -> str:
    """Mirror of execution/sodex_client.py:_round_price — Decimal tick
    rounding, HALF_UP default, floor for buy-side posts, ceil for sells.
    Self-contained so the module never imports the venue client (fakes in
    tests carry no module). tick <= 0 -> plain canonical format."""
    if tick is None or tick <= 0:
        return _fmt_decimal(Decimal(str(price)))
    d_price, d_tick = Decimal(str(price)), Decimal(str(tick))
    rounding = (ROUND_FLOOR if side == "long"
                else ROUND_CEILING if side == "short" else ROUND_HALF_UP)
    ticks = (d_price / d_tick).to_integral_value(rounding=rounding)
    return _fmt_decimal(ticks * d_tick)


def _round_qty(qty: float, step: float, reduce_only: bool = False) -> str:
    """Mirror of execution/sodex_client.py:_round_qty — floor to step;
    reduce-only dust guard: at least 1 step (the venue caps the fill at
    position size). step <= 0 -> plain canonical format."""
    if step is None or step <= 0:
        return _fmt_decimal(Decimal(str(qty)))
    rounded = math.floor(qty / step) * step
    if reduce_only and rounded <= 0 and qty > 0:
        rounded = step
    return _fmt_decimal(Decimal(str(rounded)))


def _build_item_fallback(*, cl_ord_id: str, side: int, order_type: int,
                         tif: int, quantity: str, price: Optional[str],
                         reduce_only: bool, stop_price: Optional[str] = None,
                         stop_type: Optional[int] = None,
                         trigger_type: Optional[int] = None) -> Dict[str, Any]:
    """Canonical-field-order order item — byte-shape mirror of
    sodex_client._build_order_item (insertion order IS the signing order).
    stop_price with modifier==1 coerces to modifier 2 (STOP) exactly like
    the venue client's own guard."""
    modifier = 1
    if stop_price is not None:
        modifier = 2
    item: Dict[str, Any] = {
        "clOrdID": cl_ord_id,
        "modifier": modifier,
        "side": side,
        "type": order_type,
        "timeInForce": tif,
    }
    if price is not None:
        item["price"] = str(price)
    item["quantity"] = str(quantity)
    if stop_price is not None:
        item["stopPrice"] = str(stop_price)
    if stop_type is not None:
        item["stopType"] = stop_type
    if trigger_type is not None:
        item["triggerType"] = trigger_type
    item["reduceOnly"] = reduce_only
    item["positionSide"] = _POSITION_SIDE_BOTH
    return item


class ScalperExecutor:
    """Execution glue for the parasite scalper. Constructor-injected with
    every side-effecting handle — no module-level state, no hidden I/O:

      cfg       config object, read via getattr-with-defaults only
      client    the SoDEX order client the campaign path uses (duck-typed:
                async place_order(params); optional _build_order_item and
                get_tick_step for venue-exact item shape/rounding)
      registry  position-book/registry handle — symbol-id and account-id
                resolution (duck-typed: symbol_id_for()/symbol_id()/
                symbol_ids/SYMBOL_IDS mapping, account_id attr)
      now_ms    clock function returning epoch milliseconds
    """

    def __init__(self, cfg, client, registry, now_ms: Callable[[], int]
                 ) -> None:
        self._cfg = cfg
        self._client = client
        self._registry = registry
        self._now_ms = now_ms

    # ── injected-handle resolution (duck-typed, fail-closed) ──────────

    def _ts_ms(self) -> int:
        try:
            ts = int(self._now_ms())
            return ts if ts > 0 else 0
        except Exception:
            return 0

    def _symbol_id(self, symbol: str) -> int:
        reg = self._registry
        for attr in ("symbol_id_for", "symbol_id"):
            fn = getattr(reg, attr, None)
            if callable(fn):
                try:
                    sid = int(fn(symbol))
                    if sid > 0:
                        return sid
                except Exception:
                    pass
        ids = (getattr(reg, "symbol_ids", None)
               or getattr(reg, "SYMBOL_IDS", None))
        if isinstance(ids, Mapping):
            try:
                return int(ids.get(symbol, 0) or 0)
            except Exception:
                return 0
        if isinstance(reg, Mapping):
            try:
                return int(reg.get(symbol, 0) or 0)
            except Exception:
                return 0
        return 0

    def _account_id(self, account_state) -> int:
        if isinstance(account_state, Mapping):
            try:
                aid = int(account_state.get("account_id", 0) or 0)
                if aid > 0:
                    return aid
            except Exception:
                pass
        for attr in ("account_id", "NUMERIC_ACCOUNT_ID"):
            try:
                aid = int(getattr(self._registry, attr, 0) or 0)
                if aid > 0:
                    return aid
            except Exception:
                pass
        return 0

    def _tick_step(self, symbol: str, symbol_id: int):
        """(tick, step) from the venue client when it exposes the spec
        plane; (0, 0) = rounding degrades to plain canonical format."""
        fn = getattr(self._client, "get_tick_step", None)
        if callable(fn):
            try:
                tick, step = fn(symbol, symbol_id)
                return _f(tick) or 0.0, _f(step) or 0.0
            except Exception:
                pass
        return 0.0, 0.0

    def _make_item(self, **kwargs) -> Dict[str, Any]:
        """Prefer the venue client's own item builder (signing-order
        truth); fall back to the local byte-shape mirror for fakes."""
        fn = getattr(self._client, "_build_order_item", None)
        if callable(fn):
            try:
                return fn(**kwargs)
            except Exception:
                pass
        return _build_item_fallback(**kwargs)

    # ── margin accounting ──────────────────────────────────────────────

    @staticmethod
    def _open_margin(open_scalper_positions) -> float:
        """Margin already committed to open scalper positions."""
        used = 0.0
        for pos in (open_scalper_positions or ()):
            try:
                m = _f(_ig(pos, "margin_usd"))
                if m is None:
                    notional = _f(_ig(pos, "notional_usd")) \
                        or _f(_ig(pos, "notional"))
                    lev = _f(_ig(pos, "leverage"))
                    if notional is not None and lev:
                        m = notional / lev
                if m is not None and m > 0:
                    used += m
            except Exception:
                continue
        return used

    # ── the live path ──────────────────────────────────────────────────

    async def execute_intents(self, intents, open_scalper_positions,
                              account_state) -> List[dict]:
        """Execute Grade-A intents inside the admission envelope. Returns
        one dense journal row per intent (placed or dropped). Never
        raises — the hot path must never feel this module."""
        rows: List[dict] = []
        try:
            live = bool(getattr(self._cfg, "scalper_enabled", False))
            min_score = int(getattr(self._cfg, "scalper_min_score_live", 70)
                            or 70)
            max_conc = max(1, int(getattr(self._cfg, "scalper_max_concurrent",
                                          3) or 3))
            min_not = _f(getattr(self._cfg, "scalper_min_notional_usd", 5.0))
            min_not = min_not if (min_not and min_not > 0) else 5.0
            max_not = _f(getattr(self._cfg, "scalper_max_notional_usd", 15.0))
            max_not = max_not if (max_not and max_not > 0) else 15.0
            if max_not < min_not:
                max_not = min_not
            budget = _f(getattr(self._cfg, "scalper_margin_budget_usd", 8.0))
            budget = budget if (budget and budget > 0) else 8.0
            max_lev = int(getattr(self._cfg, "scalper_max_leverage",
                                  HARD_MAX_LEVERAGE) or HARD_MAX_LEVERAGE)
            max_lev = max(1, min(HARD_MAX_LEVERAGE, max_lev))

            try:
                # Concurrency counts PRIMARIES only: an ecosystem fan-out is
                # one signal routed to N legs; the legs share the primary's
                # slot. Missing/foreign role = primary (fail-closed).
                n_open = 0
                for _p in (open_scalper_positions or ()):
                    try:
                        _role = (_p.get("role", "primary")
                                 if isinstance(_p, Mapping)
                                 else getattr(_p, "role", "primary"))
                        n_open += 1 if str(_role or "primary") == "primary" \
                            else 0
                    except Exception:
                        n_open += 1
            except Exception:
                n_open = 0
            margin_used = self._open_margin(open_scalper_positions)
            admitted = 0
            admitted_margin = 0.0

            for intent in (intents or ()):
                try:
                    ts = self._ts_ms()
                    base = {
                        "schema": SCHEMA_VERSION,
                        "ts": ts,
                        "symbol": str(_ig(intent, "primary_symbol",
                                          _ig(intent, "symbol", "")) or ""),
                        "direction": str(_ig(intent, "direction", "") or ""),
                        "grade": str(_ig(intent, "grade", "") or ""),
                        "score": int(_f(_ig(intent, "prediction_score",
                                            _ig(intent, "score", 0))) or 0),
                        "strategy_type": STRATEGY_TYPE,
                        "role": str(_ig(intent, "role", "primary")
                                    or "primary"),
                    }
                    row = await self._execute_one(
                        intent, base, live=live, min_score=min_score,
                        max_conc=max_conc, n_open=n_open, admitted=admitted,
                        min_not=min_not, max_not=max_not, budget=budget,
                        margin_used=margin_used,
                        admitted_margin=admitted_margin, max_lev=max_lev,
                        account_state=account_state)
                    if row.get("event") == "scalper_exec_placed":
                        # Only primaries consume a concurrency slot; fan-out
                        # legs ride the primary's. The margin budget clamps
                        # ALL legs — it is the real risk ceiling.
                        if str(row.get("role", "primary")) == "primary":
                            admitted += 1
                        admitted_margin += _f(row.get("margin_usd")) or 0.0
                    rows.append(row)
                except Exception as _ie:
                    # Per-intent fail-closed: a fault — even a poisonous
                    # intent object — costs this intent only, never the
                    # batch, never the hot path.
                    rows.append({"schema": SCHEMA_VERSION, "ts": self._ts_ms(),
                                 "symbol": "", "direction": "", "grade": "",
                                 "score": 0, "strategy_type": STRATEGY_TYPE,
                                 "event": "scalper_exec_dropped",
                                 "reason": "internal_error",
                                 "error": str(_ie)[:120]})
        except Exception:
            pass
        return rows

    async def _execute_one(self, intent, base: dict, *, live: bool,
                           min_score: int, max_conc: int, n_open: int,
                           admitted: int, min_not: float, max_not: float,
                           budget: float, margin_used: float,
                           admitted_margin: float, max_lev: int,
                           account_state) -> dict:
        """Gate → envelope → geometry → one venue call. Returns the
        journal row; raises only into execute_intents' per-intent guard."""

        def _drop(reason: str, **extra) -> dict:
            return {**base, "event": "scalper_exec_dropped",
                    "reason": reason, **extra}

        # 1. Live gate — kill switch first, then grade, then score.
        if not live:
            return _drop("disabled")
        if base["grade"] != GRADE_A:
            return _drop("grade")
        if base["score"] < min_score:
            return _drop("score", min_score=min_score)

        side = base["direction"]
        if side not in ("long", "short"):
            return _drop("no_direction")
        symbol = base["symbol"]
        if not symbol:
            return _drop("no_symbol")

        # 2. Admission envelope — concurrency first (primaries only; fan-out
        #    legs ride their primary's slot), then the margin budget (hard
        #    clamp: drop, never oversize — binds EVERY leg).
        if base["role"] == "primary" and n_open + admitted >= max_conc:
            return _drop("concurrency", open=n_open + admitted,
                         max_concurrent=max_conc)

        lev = int(_f(_ig(intent, "leverage", max_lev)) or max_lev)
        lev = max(1, min(max_lev, lev))
        notional = _f(_ig(intent, "notional_usd", _ig(intent, "notional")))
        if notional is None:
            margin_intent = _f(_ig(intent, "margin_per_position"))
            notional = (margin_intent * lev) if margin_intent else None
        if notional is None or notional <= 0:
            return _drop("bad_size")
        notional = max(min_not, min(max_not, notional))
        margin = notional / lev
        if margin_used + admitted_margin + margin > budget:
            return _drop("margin_budget",
                         margin_used=round(margin_used + admitted_margin, 4),
                         projected=round(margin, 4), budget=budget)

        # 3. Geometry — entry at the touch (intent price first, then the
        #    injected marks plane); TP/SL from the intent's own doctrine
        #    pcts with the reversal defaults.
        entry = _f(_ig(intent, "entry_price", _ig(intent, "price",
                                                  _ig(intent, "mark"))))
        if entry is None and isinstance(account_state, Mapping):
            marks = account_state.get("marks")
            if isinstance(marks, Mapping):
                entry = _f(marks.get(symbol))
                if entry is None:
                    entry = _f(marks.get(symbol.split("-")[0]))
        if entry is None or entry <= 0:
            return _drop("no_price")

        tp_pct = _f(_ig(intent, "tp_pct")) or DEFAULT_TP_PCT
        stop_pct = _f(_ig(intent, "stop_pct")) or DEFAULT_STOP_PCT
        sign = 1.0 if side == "long" else -1.0
        tp_price = entry * (1.0 + sign * tp_pct)
        stop_price = entry * (1.0 - sign * stop_pct)
        if tp_price <= 0 or stop_price <= 0:
            return _drop("bad_geometry")

        symbol_id = self._symbol_id(symbol)
        if symbol_id <= 0:
            return _drop("no_symbol_id")
        account_id = self._account_id(account_state)
        if account_id <= 0:
            return _drop("no_account_id")

        qty = notional / entry
        tick, step = self._tick_step(symbol, symbol_id)
        entry_side = _SIDE_BUY if side == "long" else _SIDE_SELL
        exit_side = _SIDE_SELL if side == "long" else _SIDE_BUY
        ts = self._ts_ms() or base["ts"]
        cl_entry = f"{CLORD_PREFIX}{symbol}_{ts}"[:_CLORD_MAX]

        items = [
            self._make_item(
                cl_ord_id=cl_entry, side=entry_side, order_type=_TYPE_LIMIT,
                tif=_TIF_GTC, quantity=_round_qty(qty, step),
                price=_round_price(entry, tick, side), reduce_only=False),
            self._make_item(
                cl_ord_id=f"{cl_entry}tp"[:_CLORD_MAX], side=exit_side,
                order_type=_TYPE_LIMIT, tif=_TIF_GTC,
                quantity=_round_qty(qty, step, reduce_only=True),
                price=_round_price(tp_price, tick), reduce_only=True),
            self._make_item(
                cl_ord_id=f"{cl_entry}sl"[:_CLORD_MAX], side=exit_side,
                order_type=_TYPE_LIMIT, tif=_TIF_GTC,
                quantity=_round_qty(qty, step, reduce_only=True),
                price=_round_price(stop_price, tick), reduce_only=True,
                stop_price=_round_price(stop_price, tick),
                stop_type=_STOP_TYPE_LOSS, trigger_type=_TRIGGER_MARK),
        ]
        params = {"accountID": account_id, "symbolID": symbol_id,
                  "orders": items}

        # 4. The venue call — fail-closed; one retry ONLY on an explicit
        #    nonce-stale rejection, with a fresh client-order-id.
        result = await self._place(params)
        if result is None:                      # exception already dropped
            return _drop("venue_exception")
        ok = bool(getattr(result, "success", False))
        err = str(getattr(result, "error", "") or "")
        if not ok and "nonce" in err.lower():
            ts2 = self._ts_ms()
            if ts2 > 0 and ts2 != ts:
                cl_retry = f"{CLORD_PREFIX}{symbol}_{ts2}"[:_CLORD_MAX]
                for it, suffix in zip(params["orders"], ("", "tp", "sl")):
                    if isinstance(it, dict):
                        it["clOrdID"] = f"{cl_retry}{suffix}"[:_CLORD_MAX]
                result = await self._place(params)
                if result is None:
                    return _drop("venue_exception")
                ok = bool(getattr(result, "success", False))
                err = str(getattr(result, "error", "") or "")
        if not ok:
            return _drop("venue_rejected", error=err[:120])

        return {**base, "event": "scalper_exec_placed",
                "source": str(_ig(intent, "source", "") or ""),
                "entry": round(entry, 8), "tp": round(tp_price, 8),
                "stop": round(stop_price, 8),
                "tp_pct": round(tp_pct, 6), "stop_pct": round(stop_pct, 6),
                "qty": round(qty, 8),
                "notional_usd": round(notional, 4),
                "margin_usd": round(margin, 4), "leverage": lev,
                "cl_ord_id": cl_entry,
                "order_id": str(getattr(result, "order_id", "") or "")}

    async def _place(self, params):
        """One venue call. Returns the OrderResult, or None when the venue
        raised (the exception is swallowed HERE — fail-closed, no retry
        storm, nothing propagates into the hot path)."""
        try:
            return await self._client.place_order(params)
        except Exception:
            return None

    # ── startup-sync classification (pure) ─────────────────────────────

    def reconcile(self, open_positions) -> List[dict]:
        """Tag venue-open positions as scalper-owned or not. Scalper
        ownership basis: client-order-id prefix "scalper_" (the wire
        namespace) or strategy_type/strategy_tag == "parasite_scalper".
        Pure function — zero I/O, never raises; the integrator's
        startup-sync adoption consumes these rows to classify managed
        positions."""
        rows: List[dict] = []
        try:
            for pos in (open_positions or ()):
                try:
                    symbol = str(_ig(pos, "symbol", _ig(pos, "coin", ""))
                                 or "")
                    cl_id = str(_ig(pos, "cl_ord_id",
                                    _ig(pos, "client_order_id",
                                        _ig(pos, "clOrdID",
                                            _ig(pos, "tag", "")))) or "")
                    stag = str(_ig(pos, "strategy_type",
                                   _ig(pos, "strategy_tag", "")) or "")
                    basis = ""
                    if cl_id.startswith(CLORD_PREFIX):
                        basis = "cl_ord_id"
                    elif stag == STRATEGY_TYPE:
                        basis = "strategy_type"
                    rows.append({
                        "schema": SCHEMA_VERSION,
                        "symbol": symbol,
                        "side": str(_ig(pos, "side",
                                        _ig(pos, "direction", "")) or ""),
                        "size": _f(_ig(pos, "size", _ig(pos, "qty"))) or 0.0,
                        "entry": _f(_ig(pos, "entry",
                                        _ig(pos, "avgEntryPrice",
                                            _ig(pos, "entry_price")))) or 0.0,
                        "scalper_owned": bool(basis),
                        "basis": basis,
                        "strategy_type": STRATEGY_TYPE if basis else "",
                    })
                except Exception:
                    continue
        except Exception:
            pass
        return rows
