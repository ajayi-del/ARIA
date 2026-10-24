"""intelligence/sodex_liq_engine_watch.py — SoDEX liquidation-engine slice detector (pure brain, zero I/O).

VENUE MECHANICS (Governor-verified): SoDEX's liquidation engine force-reduces
large positions in ~20% slices with a ~30s cooldown between slices. Each slice
prints as aggressive same-side market flow (and often a pulled/refreshing book
level on the consumed side). A full liquidation of size X therefore shows as
>=2 same-side prints of nearly-equal notional (±3%) spaced ~28-32s apart —
giving observers ~2.5 minutes of advance warning before the cascade leg
completes. Nothing else in ARIA watches for this regularity today.

DOCTRINE:
  - Freeman-Shor: detection is MECHANICAL, never by feel — the regularity is
    the signal; the module emits verdicts, never narratives.
  - Aronson: every degree of freedom is constructor-bounded (LiqWatchConfig);
    no hidden tunables, no env reads except the master kill switch.
  - Advisory only: this brain emits VERDICTS. It never sizes, never orders,
    never closes. The splice (a later integration pass) decides what a
    confirmed cascade-in-progress is worth.

ZERO-I/O CONTRACT (docs/DEPARTMENT_TEMPLATE.md):
  - No network, no files, no logging, no time.time() — now_s is injected.
  - Inputs are PLAIN values (symbol, side, notional_usd, price, depths,
    now_s) — not feed/store objects. data/sodex_feed.py and
    data/orderbook_store.py shapes are matched at the FUTURE splice, not here.
  - Fail-closed everywhere: bad inputs are ignored, nothing raises. Kill
    switch off = total abstention with zero state mutation (pre-module
    bit-for-bit).

TELEMETRY (documented for the future splice; NEVER emitted here):
  sodex_liq_slice_candidate  — a run forms but has not reached min_slices_confirm
  sodex_liq_confirmed        — a run reaches/extends past min_slices_confirm
  sodex_liq_decayed          — a confirmed run goes quiet past the decay deadline
  sodex_liq_watch_error      — fail-closed exception swallow (counted in snapshot)

DIRECTION SEMANTICS: aggressive SELL prints = longs being liquidated ->
side="long_liq"; aggressive BUY prints = "short_liq".
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from statistics import median
from typing import Dict, List, Optional, Tuple

SLICE_FRACTION = 0.20          # venue mechanic: ~20% of the position per slice
EST_TOTAL_SLICES = 1.0 / SLICE_FRACTION  # ~5 slices per full liquidation
BOOK_CONF_ADJ = 0.1            # book-imbalance confidence nudge (bounded)

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


def sodex_liq_watch_enabled() -> bool:
    """Master kill switch. Env SODEX_LIQ_WATCH_ENABLED, read AT CALL TIME,
    default true. False = on_trade/on_book no-op returning None and the
    tracker holds no state (pre-module bit-for-bit)."""
    try:
        raw = os.environ.get("SODEX_LIQ_WATCH_ENABLED")
        if raw is None:
            return True
        v = raw.strip().lower()
        if v in _FALSY:
            return False
        if v in _TRUTHY:
            return True
        return True  # unrecognised value -> default on (fail to legacy-on)
    except Exception:
        return True


@dataclass(frozen=True)
class LiqWatchConfig:
    """Tunables — constructor-injected, bounded (Aronson)."""
    slice_interval_s: float = 30.0        # venue cooldown between slices
    interval_tolerance_s: float = 5.0     # ± band on the inter-slice interval
    size_tolerance_pct: float = 0.03      # ± band on slice notional vs run median
    min_slice_notional_usd: float = 20000.0  # dust floor — slices below this are noise
    min_slices_confirm: int = 2           # run length needed for a confirmed verdict
    window_s: float = 300.0               # rolling print memory per symbol
    side_imbalance_min: float = 0.7       # one-sided book share needed for the ±0.1 nudge


@dataclass(frozen=True)
class LiqWatchVerdict:
    symbol: str
    side: str                    # "long_liq" | "short_liq"
    n_slices: int
    slice_notional_usd: float    # run median slice notional
    est_remaining_usd: float     # slice/0.2 total estimate minus what has printed
    first_seen_s: float
    last_seen_s: float
    advance_warning_s: float     # slice_interval_s × remaining slice-count estimate
    confidence: float            # bounded 0-1: slice regularity × size regularity (± book nudge)
    state: str                   # "candidate" | "confirmed" | "decayed"


class _Run:
    """One candidate slice-run for one (symbol, liquidation side)."""
    __slots__ = ("prints", "last_emitted_n", "confirmed")

    def __init__(self, ts: float, notional: float) -> None:
        self.prints: List[Tuple[float, float]] = [(ts, notional)]
        self.last_emitted_n: int = 0
        self.confirmed: bool = False

    @property
    def n(self) -> int:
        return len(self.prints)

    @property
    def last_ts(self) -> float:
        return self.prints[-1][0]

    @property
    def first_ts(self) -> float:
        return self.prints[0][0]

    @property
    def median_notional(self) -> float:
        return median(n for _, n in self.prints)


class _SymState:
    __slots__ = ("prints", "runs", "book")

    def __init__(self) -> None:
        self.prints: List[Tuple[float, str, float]] = []  # (ts, liq_side, notional)
        self.runs: Dict[str, _Run] = {}                    # keyed by "long_liq"/"short_liq"
        self.book: Optional[Tuple[float, float, float]] = None  # (bid_usd, ask_usd, ts)


def _finite_positive(x) -> bool:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return False
    return math.isfinite(v) and v > 0.0


def _finite_nonneg(x) -> bool:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return False
    return math.isfinite(v) and v >= 0.0


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


class SodexLiqEngineWatch:
    """Rolling per-symbol detector for the ~20%/~30s liquidation-slice cadence."""

    def __init__(self, cfg: LiqWatchConfig = LiqWatchConfig()) -> None:
        self._cfg = cfg
        self._symbols: Dict[str, _SymState] = {}
        self._verdicts_emitted = 0
        self._decayed_emitted = 0
        self._errors = 0

    # ── public API ──────────────────────────────────────────────────────────

    def on_trade(
        self,
        symbol: str,
        side: str,
        notional_usd: float,
        price: float,
        now_s: float,
    ) -> Optional[LiqWatchVerdict]:
        """Feed one aggressive print. Returns a confirmed verdict on the call
        where a run reaches min_slices_confirm, and again only when a later
        slice materially extends the run. Otherwise None."""
        try:
            if not sodex_liq_watch_enabled():
                return None
            if not isinstance(symbol, str) or not symbol:
                return None
            liq_side = self._liq_side(side)
            if liq_side is None:
                return None
            if not _finite_positive(notional_usd):
                return None
            if not _finite_positive(price):
                return None
            if not _finite_nonneg(now_s):
                return None
            cfg = self._cfg
            notional = float(notional_usd)
            now = float(now_s)
            if notional < cfg.min_slice_notional_usd:
                return None

            st = self._symbols.setdefault(symbol, _SymState())
            # window eviction of stale prints
            cutoff = now - cfg.window_s
            st.prints = [p for p in st.prints if p[0] >= cutoff]
            st.prints.append((now, liq_side, notional))

            run = st.runs.get(liq_side)
            if run is not None:
                dt = now - run.last_ts
                upper = cfg.slice_interval_s + cfg.interval_tolerance_s
                lower = cfg.slice_interval_s - cfg.interval_tolerance_s
                if dt > upper:
                    # cadence broken — the old run is done; anchor a fresh one
                    run = _Run(now, notional)
                    st.runs[liq_side] = run
                elif dt >= lower:
                    med = run.median_notional
                    if med > 0 and abs(notional - med) / med <= cfg.size_tolerance_pct:
                        run.prints.append((now, notional))
                    else:
                        # same cadence, different size class — a new episode
                        run = _Run(now, notional)
                        st.runs[liq_side] = run
                else:
                    # faster than the venue cooldown: unrelated flow, run untouched
                    pass
            else:
                run = _Run(now, notional)
                st.runs[liq_side] = run

            if run.n >= cfg.min_slices_confirm and run.last_emitted_n < run.n:
                run.last_emitted_n = run.n
                run.confirmed = True
                self._verdicts_emitted += 1
                return self._build_verdict(symbol, liq_side, run, st.book, "confirmed")
            return None
        except Exception:
            self._errors += 1
            return None

    def on_book(
        self,
        symbol: str,
        bid_depth_usd: float,
        ask_depth_usd: float,
        now_s: float,
    ) -> None:
        """Optional secondary evidence: store the latest top-of-book depth
        split. Used ONLY to nudge confidence ±0.1 (bounded) at verdict time —
        a liquidation consuming bids tends to leave an ask-heavy book."""
        try:
            if not sodex_liq_watch_enabled():
                return None
            if not isinstance(symbol, str) or not symbol:
                return None
            if not (_finite_nonneg(bid_depth_usd) and _finite_nonneg(ask_depth_usd)):
                return None
            if not _finite_nonneg(now_s):
                return None
            st = self._symbols.setdefault(symbol, _SymState())
            st.book = (float(bid_depth_usd), float(ask_depth_usd), float(now_s))
            return None
        except Exception:
            self._errors += 1
            return None

    def collect_decayed(self, now_s: float) -> List[LiqWatchVerdict]:
        """Decay sweep: a run with no new slice beyond interval + tolerance +
        grace (grace = one full extra slice cadence) is dead. Confirmed runs
        are returned ONCE as state="decayed" verdicts, then evicted;
        unconfirmed runs are evicted silently."""
        try:
            if not sodex_liq_watch_enabled():
                return []
            if not _finite_nonneg(now_s):
                return []
            cfg = self._cfg
            deadline = cfg.slice_interval_s + cfg.interval_tolerance_s + cfg.slice_interval_s
            out: List[LiqWatchVerdict] = []
            for symbol, st in list(self._symbols.items()):
                for liq_side, run in list(st.runs.items()):
                    if float(now_s) - run.last_ts > deadline:
                        if run.confirmed:
                            out.append(
                                self._build_verdict(symbol, liq_side, run, st.book, "decayed")
                            )
                            self._decayed_emitted += 1
                        del st.runs[liq_side]
                if not st.runs and not st.prints and st.book is None:
                    del self._symbols[symbol]
            return out
        except Exception:
            self._errors += 1
            return []

    def decay(self, now_s: float) -> List[LiqWatchVerdict]:
        """Alias for collect_decayed — performs the transition + eviction."""
        return self.collect_decayed(now_s)

    def state_snapshot(self) -> dict:
        """Telemetry readout for the future splice. Never raises."""
        try:
            active = 0
            confirmed = 0
            prints = 0
            for st in self._symbols.values():
                prints += len(st.prints)
                for run in st.runs.values():
                    active += 1
                    if run.confirmed:
                        confirmed += 1
            return {
                "enabled": sodex_liq_watch_enabled(),
                "symbols": len(self._symbols),
                "prints_tracked": prints,
                "active_runs": active,
                "confirmed_runs": confirmed,
                "verdicts_emitted": self._verdicts_emitted,
                "decayed_emitted": self._decayed_emitted,
                "errors": self._errors,
            }
        except Exception:
            return {"enabled": sodex_liq_watch_enabled(), "errors": -1}

    # ── internals ───────────────────────────────────────────────────────────

    @staticmethod
    def _liq_side(side) -> Optional[str]:
        if not isinstance(side, str):
            return None
        s = side.strip().lower()
        if s == "sell":
            return "long_liq"   # aggressive sells = longs being force-reduced
        if s == "buy":
            return "short_liq"  # aggressive buys = shorts being force-reduced
        return None

    def _book_adjust(self, liq_side: str, book) -> float:
        if not book:
            return 0.0
        bid, ask, _ts = book
        total = bid + ask
        if total <= 0.0:
            return 0.0
        ask_share = ask / total
        # long_liq: slices consume bids -> ask-heavy book agrees
        agree = ask_share if liq_side == "long_liq" else (1.0 - ask_share)
        if agree >= self._cfg.side_imbalance_min:
            return BOOK_CONF_ADJ
        if (1.0 - agree) >= self._cfg.side_imbalance_min:
            return -BOOK_CONF_ADJ
        return 0.0

    def _build_verdict(
        self,
        symbol: str,
        liq_side: str,
        run: _Run,
        book,
        state: str,
    ) -> LiqWatchVerdict:
        cfg = self._cfg
        med = run.median_notional
        n = run.n

        # size regularity: mean relative deviation from the run median
        if med > 0 and cfg.size_tolerance_pct > 0:
            dev = sum(abs(p[1] - med) for p in run.prints) / n / med
            size_reg = _clamp01(1.0 - dev / cfg.size_tolerance_pct)
        else:
            size_reg = 0.0

        # slice regularity: mean deviation of inter-print intervals from the cadence
        if n >= 2 and cfg.interval_tolerance_s > 0:
            intervals = [
                run.prints[i + 1][0] - run.prints[i][0] for i in range(n - 1)
            ]
            idev = sum(abs(dt - cfg.slice_interval_s) for dt in intervals) / len(intervals)
            slice_reg = _clamp01(1.0 - idev / cfg.interval_tolerance_s)
        else:
            slice_reg = 0.0

        conf = _clamp01(slice_reg * size_reg + self._book_adjust(liq_side, book))

        est_total = med / SLICE_FRACTION if med > 0 else 0.0
        est_remaining = max(0.0, est_total - n * med)
        advance = cfg.slice_interval_s * max(0.0, EST_TOTAL_SLICES - n)

        return LiqWatchVerdict(
            symbol=symbol,
            side=liq_side,
            n_slices=n,
            slice_notional_usd=med,
            est_remaining_usd=est_remaining,
            first_seen_s=run.first_ts,
            last_seen_s=run.last_ts,
            advance_warning_s=advance,
            confidence=conf,
            state=state,
        )
