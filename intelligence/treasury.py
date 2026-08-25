"""Treasury — ARIA's accounting department (2026-08-19).

Single owner of profit realization across all venues. Replaces the
position-blind basket TP loop whose harvest never fired (basket_harvest=0
all-time) while its escape valve clipped winners at 7% and losers rode to
time_stop — a machine-built disposition effect.

Design doctrine:
  Goldratt (The Goal)        — margin is the constraint; recycle it from
                               stale-flat positions, never let losers hold
                               capital hostage while winners get clipped.
  Meadows (Systems)          — one owner per stock; cooldowns block re-firing,
                               they never erase a position from the ledger.
  Grinold & Kahn (Active PM) — transfer coefficient: no structural inert
                               zones (per-cluster activation, no range-day
                               3-position minimum, no SoDEX-only settlement).
  Taleb (Dynamic Hedging)    — manage correlated clusters as single books:
                               crypto beta converges to one position in a
                               cascade; equities and commodities are separate
                               economies with their own trailing locks.

Pure logic only — every external dependency (marks, venues, day types,
depth, HTF bias, step sizes) is injected. The main loop executes the
returned orders; this module never touches the exchange.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

CLUSTER_CRYPTO = "crypto_beta"
CLUSTER_EQUITY = "equity"
CLUSTER_COMMODITY = "commodity"

_EQUITY_CATEGORIES = {"equity", "equity_index"}
_COMMODITY_CATEGORIES = {"commodity"}

# Threshold stack bases (ported from the basket loop's tuned values).
# Operator directive 2026-08-25: portfolio ROE harvest → 15% (was 4-8 base,
# 6 small-account cap). The 4-6% first harvest was cutting winners at
# 6-38 min — the disposition-effect class this module exists to repair.
# "Most coins eventually run into winners" — let them run. TP2 re-laddered
# above the new TP1; runaway trim aligned (7% bank-half was the same early
# harvest one level down; trend escape mult still applies, ×1.5 → 22.5%).
_TP1_BASE = 15.0
_TP2_BASE = 25.0
_HARVEST_BASE = 0.60
_TREND_TP1, _TREND_TP2, _TREND_HARVEST = 15.0, 25.0, 0.50
_CHOP_TP1, _CHOP_TP2, _CHOP_HARVEST = 15.0, 20.0, 0.85
_SMALL_ACCT_TP1_CAP = 15.0
_SMALL_ACCT_TP2_CAP = 25.0
_SMALL_ACCT_BALANCE = 1000.0
_TRAIL_GIVEBACK = 0.6          # fire TP1-level harvest at 60% of peak ROE
_RUNAWAY_ROE = 15.0            # personal ROE where a runaway gets trimmed
_LOSS_CUT_BOOK_ROE = -3.0      # book bleeding → cut worst performer
_LOSS_CUT_MIN_HOLD_MS = 5 * 60 * 1000
_LOSS_CUT_COOLDOWN_S = 300.0
_RECYCLE_COOLDOWN_S = 300.0
_DEFAULT_LEVERAGE = 5.0        # margin reconstruction when initial_margin==0


def cluster_of(category: str) -> str:
    if category in _EQUITY_CATEGORIES:
        return CLUSTER_EQUITY
    if category in _COMMODITY_CATEGORIES:
        return CLUSTER_COMMODITY
    return CLUSTER_CRYPTO


@dataclass
class LedgerEntry:
    symbol: str
    venue: str
    side: str
    size: float
    entry_price: float
    mark: float
    initial_margin: float
    pnl: float
    roe: float
    age_ms: int
    cluster: str
    day_type: str = "unknown"
    depth_ratio: float = 1.0
    htf_bias: str = "neutral"


@dataclass
class CloseOrder:
    symbol: str
    venue: str
    side: str
    size: float
    mark: float
    pnl: float
    roe: float
    reason: str
    partial: bool


@dataclass
class Decision:
    orders: list = field(default_factory=list)          # list[CloseOrder]
    book_pnl: float = 0.0
    book_margin: float = 0.0
    book_roe: float = 0.0
    loss_cut_grace: bool = False                        # bleeding but all too young
    telemetry: dict = field(default_factory=dict)       # per-cluster heartbeat data


@dataclass
class Thresholds:
    tp1_pct: float
    tp2_pct: float
    harvest_ratio: float
    day_type: str


def compute_thresholds(day_type: str, cascade_phase: str, avg_depth: float,
                       htf_alignment: float, meta_tp_mult: float | None,
                       small_account: bool) -> Thresholds:
    """The tuned basket stack, per cluster. Margin-weighted inputs are the
    caller's job; this is the pure multiplier chain."""
    if day_type == "trend":
        tp1, tp2, harvest = _TREND_TP1, _TREND_TP2, _TREND_HARVEST
    elif day_type == "chop":
        tp1, tp2, harvest = _CHOP_TP1, _CHOP_TP2, _CHOP_HARVEST
    else:
        tp1, tp2, harvest = _TP1_BASE, _TP2_BASE, _HARVEST_BASE

    if cascade_phase == "momentum":
        tp1 *= 1.25; tp2 *= 1.25; harvest = min(0.95, harvest * 1.15)
    elif cascade_phase == "primed":
        tp1 *= 1.50; tp2 *= 1.50; harvest *= 0.90

    if avg_depth < 0.3:
        tp1 *= 0.75; tp2 *= 0.67; harvest = min(0.95, harvest * 1.33)
    elif avg_depth >= 0.6:
        tp1 *= 1.25; tp2 *= 1.20; harvest *= 0.80

    if htf_alignment >= 0.6:
        tp1 *= 1.20; tp2 *= 1.25; harvest *= 0.75
    elif htf_alignment <= -0.6:
        tp1 *= 0.80; tp2 *= 0.75; harvest = min(0.95, harvest * 1.25)

    if meta_tp_mult is not None:
        tp1 *= meta_tp_mult; tp2 *= meta_tp_mult

    if small_account:
        tp1 = min(tp1, _SMALL_ACCT_TP1_CAP)
        tp2 = min(tp2, _SMALL_ACCT_TP2_CAP)

    return Thresholds(tp1_pct=tp1, tp2_pct=tp2,
                      harvest_ratio=max(0.05, min(0.95, harvest)),
                      day_type=day_type)


class Treasury:
    def __init__(self, cfg):
        self.enabled = bool(getattr(cfg, "treasury_enabled", True))
        self.trim_ratio = float(getattr(cfg, "treasury_runaway_trim_ratio", 0.5))
        self.recycle_enabled = bool(getattr(cfg, "treasury_recycle_enabled", True))
        self.recycle_util = float(getattr(cfg, "treasury_recycle_margin_util", 0.75))
        self.recycle_min_age_ms = float(getattr(cfg, "treasury_recycle_min_age_s", 2700.0)) * 1000.0
        self.recycle_flat_band = float(getattr(cfg, "treasury_recycle_flat_roe_band", 1.5))
        self.trend_room_enabled = bool(getattr(cfg, "trend_day_tp_room_enabled", True))
        self.trend_escape_mult = float(getattr(cfg, "trend_day_winner_escape_mult", 1.5))
        self._peak_roe: dict[str, float] = {}
        self._depth_ema: dict[str, float] = {}
        self._loss_cut_last = 0.0
        self._recycle_last = 0.0

    def reset(self) -> None:
        """Book went flat — drop all trailing state."""
        self._peak_roe.clear()
        self._depth_ema.clear()

    def prune(self, active_cluster_names: set[str]) -> None:
        for store in (self._peak_roe, self._depth_ema):
            for name in list(store):
                if name not in active_cluster_names:
                    store.pop(name, None)

    def build_ledger(self, positions, *, mark_fn, venue_fn, category_fn,
                     day_type_fn, depth_fn, htf_fn, now_ms: int,
                     skip_symbols=frozenset()) -> list[LedgerEntry]:
        ledger: list[LedgerEntry] = []
        for pos in positions:
            sym = pos.symbol
            if sym in skip_symbols:
                continue
            try:
                mark = float(mark_fn(sym) or 0.0)
                entry = float(pos.entry_price or 0.0)
                size = float(pos.size or 0.0)
            except (TypeError, ValueError):
                continue
            if mark <= 0 or entry <= 0 or size <= 0:
                continue
            im = float(getattr(pos, "initial_margin", 0.0) or 0.0)
            if im <= 0:
                # Ghost repair: adopted positions may lack margin — reconstruct
                # at default leverage so they are never invisible to the book.
                im = size * entry / _DEFAULT_LEVERAGE
            pnl = (mark - entry) * size if pos.side == "long" else (entry - mark) * size
            roe = (pnl / im) * 100.0
            age_ms = now_ms - int(getattr(pos, "opened_at_ms", now_ms) or now_ms)
            venue = "sodex"
            try:
                venue = venue_fn(sym) or "sodex"
            except Exception:
                pass
            try:
                cluster = cluster_of(category_fn(sym) or "unknown")
            except Exception:
                cluster = CLUSTER_CRYPTO
            entry_row = LedgerEntry(
                symbol=sym, venue=venue, side=pos.side, size=size,
                entry_price=entry, mark=mark, initial_margin=im,
                pnl=pnl, roe=roe, age_ms=age_ms, cluster=cluster,
            )
            for fn, attr, default in ((day_type_fn, "day_type", "unknown"),
                                      (depth_fn, "depth_ratio", 1.0),
                                      (htf_fn, "htf_bias", "neutral")):
                try:
                    val = fn(sym, pos.side) if attr == "depth_ratio" else fn(sym)
                    if val is not None:
                        setattr(entry_row, attr, val)
                except Exception:
                    pass
            ledger.append(entry_row)
        return ledger

    @staticmethod
    def group_active(ledger: list[LedgerEntry], excluded: set[str]) -> dict[str, list[LedgerEntry]]:
        """Clusters with >=2 managed (non-age-expired) members."""
        groups: dict[str, list[LedgerEntry]] = {}
        for e in ledger:
            if e.symbol in excluded:
                continue
            groups.setdefault(e.cluster, []).append(e)
        return {name: members for name, members in groups.items() if len(members) >= 2}

    @staticmethod
    def _margin_weighted_day_type(members: list[LedgerEntry]) -> str:
        weights: dict[str, float] = {}
        for e in members:
            if e.day_type != "unknown":
                weights[e.day_type] = weights.get(e.day_type, 0.0) + e.initial_margin
        return max(weights, key=weights.get) if weights else "range"

    @staticmethod
    def _margin_weighted_htf(members: list[LedgerEntry]) -> float:
        score = 0.0
        total = 0.0
        for e in members:
            w = e.initial_margin
            if (e.side == "long" and e.htf_bias == "bullish") or \
               (e.side == "short" and e.htf_bias == "bearish"):
                score += w
            elif e.htf_bias == "neutral":
                pass
            else:
                score -= w
            total += w
        return score / total if total > 0 else 0.0

    def _cluster_depth(self, name: str, members: list[LedgerEntry]) -> float:
        total = sum(e.initial_margin for e in members)
        raw = sum(e.depth_ratio * e.initial_margin for e in members) / max(total, 0.01)
        prev = self._depth_ema.get(name)
        ema = raw if prev is None else 0.8 * prev + 0.2 * raw
        self._depth_ema[name] = ema
        return ema

    def _trim_size(self, e: LedgerEntry, ratio: float, step_fn, min_notional_fn) -> tuple[float, bool]:
        """Step-rounded trim size; falls back to full close when the remainder
        would be dust. Returns (size, partial)."""
        step = max(step_fn(e.symbol, e.venue), 1e-12)
        close_size = math.floor(e.size * ratio / step) * step
        remaining = e.size - close_size
        min_notional = min_notional_fn(e.symbol, e.venue)
        if close_size < step or close_size * e.mark < min_notional:
            return 0.0, True
        if remaining * e.mark < min_notional or remaining < step:
            return e.size, False   # remainder dust → close whole position
        return close_size, True

    def decide(self, ledger: list[LedgerEntry], active: dict[str, list[LedgerEntry]],
               *, cascade_phase: str, meta_tp_mult, balance: float,
               cooldowns: dict, now: float, now_ms: int,
               step_fn, min_notional_fn) -> Decision:
        d = Decision()
        d.book_pnl = sum(e.pnl for e in ledger)
        d.book_margin = sum(e.initial_margin for e in ledger)
        d.book_roe = (d.book_pnl / d.book_margin) * 100.0 if d.book_margin > 0 else 0.0
        small_account = balance < _SMALL_ACCT_BALANCE

        # ── Goldratt loss-cut: book bleeding → cut the worst performer ────
        if d.book_roe < _LOSS_CUT_BOOK_ROE and len(ledger) >= 2 \
                and now >= self._loss_cut_last:
            eligible = [e for e in ledger if e.age_ms >= _LOSS_CUT_MIN_HOLD_MS
                        and e.symbol not in cooldowns]
            if not eligible:
                d.loss_cut_grace = True
            else:
                worst = min(eligible, key=lambda e: e.roe)
                d.orders.append(CloseOrder(
                    symbol=worst.symbol, venue=worst.venue, side=worst.side,
                    size=worst.size, mark=worst.mark, pnl=worst.pnl, roe=worst.roe,
                    reason="portfolio_loss_cut", partial=False))
                self._loss_cut_last = now + _LOSS_CUT_COOLDOWN_S
                return d   # one decisive action per tick when bleeding

        # ── Cluster harvests (Taleb: each cluster is its own book) ────────
        for name, members in sorted(active.items()):
            margin = sum(e.initial_margin for e in members)
            pnl = sum(e.pnl for e in members)
            roe = (pnl / margin) * 100.0 if margin > 0 else 0.0
            depth = self._cluster_depth(name, members)
            th = compute_thresholds(
                self._margin_weighted_day_type(members), cascade_phase, depth,
                self._margin_weighted_htf(members), meta_tp_mult, small_account)

            peak = max(self._peak_roe.get(name, 0.0), roe)
            self._peak_roe[name] = peak
            trail_fired = peak >= _TP1_BASE and 0.0 < roe <= peak * _TRAIL_GIVEBACK

            d.telemetry[name] = {
                "roe": round(roe, 2), "peak": round(peak, 2), "n": len(members),
                "tp1": round(th.tp1_pct, 2), "tp2": round(th.tp2_pct, 2),
                "harvest": round(th.harvest_ratio, 2), "day_type": th.day_type,
                "trail": trail_fired,
            }

            min_harvest_pnl = max(1.0, margin * 0.02)
            level = None
            if roe >= th.tp2_pct:
                level = "tp2"
            elif roe >= th.tp1_pct and pnl >= min_harvest_pnl:
                level = "tp1"
            elif trail_fired and pnl >= min_harvest_pnl:
                level = "trail_lock"

            if level:
                profitable = sorted((e for e in members
                                     if e.roe > 0 and e.symbol not in cooldowns),
                                    key=lambda e: -e.roe)
                for e in profitable:
                    if level == "tp2":
                        d.orders.append(CloseOrder(
                            symbol=e.symbol, venue=e.venue, side=e.side,
                            size=e.size, mark=e.mark, pnl=e.pnl, roe=e.roe,
                            reason="treasury_tp2", partial=False))
                    else:
                        size, _partial = self._trim_size(
                            e, th.harvest_ratio, step_fn, min_notional_fn)
                        if size > 0:
                            d.orders.append(CloseOrder(
                                symbol=e.symbol, venue=e.venue, side=e.side,
                                size=size, mark=e.mark, pnl=e.pnl, roe=e.roe,
                                reason="treasury_tp1" if level == "tp1"
                                       else "treasury_trail_lock",
                                partial=size < e.size))
                if d.orders:
                    self._peak_roe[name] = 0.0   # trailing lock re-arms fresh

        # ── Runaway trim: bank half, let the rest run (repairs the old 7%
        # full-close escape valve's right-tail clip) ──────────────────────
        managed = {sym for members in active.values() for sym in (e.symbol for e in members)}
        ordered = {o.symbol for o in d.orders}
        for e in ledger:
            if e.symbol not in managed or e.symbol in cooldowns or e.symbol in ordered:
                continue
            threshold = _RUNAWAY_ROE
            if self.trend_room_enabled and e.day_type == "trend":
                threshold *= self.trend_escape_mult
            if e.roe >= threshold:
                size, partial = self._trim_size(e, self.trim_ratio, step_fn, min_notional_fn)
                if size > 0:
                    d.orders.append(CloseOrder(
                        symbol=e.symbol, venue=e.venue, side=e.side,
                        size=size, mark=e.mark, pnl=e.pnl, roe=e.roe,
                        reason="treasury_runaway_trim", partial=partial))

        # ── Margin recycling: stale-flat positions release the constraint ──
        if self.recycle_enabled and balance > 0 and now >= self._recycle_last \
                and d.book_margin >= self.recycle_util * balance:
            candidates = [e for e in ledger
                          if e.age_ms >= self.recycle_min_age_ms
                          and abs(e.roe) <= self.recycle_flat_band
                          and e.symbol not in cooldowns
                          and e.symbol not in ordered]
            if candidates:
                pick = max(candidates, key=lambda e: e.age_ms)
                d.orders.append(CloseOrder(
                    symbol=pick.symbol, venue=pick.venue, side=pick.side,
                    size=pick.size, mark=pick.mark, pnl=pick.pnl, roe=pick.roe,
                    reason="treasury_margin_recycle", partial=False))
                self._recycle_last = now + _RECYCLE_COOLDOWN_S

        return d
