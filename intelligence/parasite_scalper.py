"""
intelligence/parasite_scalper.py — parasite scalper brain (Three-Layer
Network Phase 1, Governor order 2026-10-10). SHADOW-ONLY FROM BIRTH.

The parasite trades the wake of the fleet: the anticipator/campaign book
moves size and leaves footprints (fills, stops, evictions, OI shifts,
cross-asset sympathy) — this brain grades whether the footprint predicts
the next 240s. It consumes fleet EVENTS only (the fleet is sacred; no
fleet behavior changes) and returns frozen ScalpSignalGrade verdicts.
The integrator's loop journals the verdicts; execution stays dark until
the Governor flips scalper_enabled.

Zero-I/O department shape (docs/DEPARTMENT_TEMPLATE.md, lancer.py idiom):
  - Constructor takes (cfg, ecosystem_router) only. Every market read —
    fleet events, L4 imbalance, regime, OI deltas, funding, cross-asset
    moves, open scalps, narrative direction, time — arrives as a typed
    tick() argument. The brain never reads a clock, file, socket, or
    env var.
  - Kill switches (getattr-with-default — missing config = pre-build
    bit-for-bit):
        scalper_shadow_enabled   True   the measurement; safe from birth
        scalper_enabled          False  LIVE path; dark, Governor-gated
    Shadow-False → tick() returns [] with zero state mutation. The brain
    never reads scalper_enabled — that gate lives at the integrator's
    order-placement site (this module emits intents, never orders).
  - Fail-closed: every input None → that component scores 0 and is
    journaled dark; an internal fault → [] and the hot path never feels
    this module.

Prediction score (0-100), five pure sub-scorers with documented
normalization; direction agreement is measured against the candidate
direction (long reads positive imbalance/OI/cross moves as agreement,
short mirrors; funding reads as contrarian positioning):
  L4 imbalance        25   |imb| normalized at 0.30 = full
  OI delta            20   |ΔOI%| normalized at 2.0% = full
  fleet velocity      20   fills in window, 3 = full
  cross-asset sync    20   fraction of live family members moving ≥0.2%
                           in the candidate direction
  funding velocity    15   |rate| normalized at 5bps = full, contrarian
                           sign (positive funding = crowded longs =
                           short-friendly)
Grade ladder: ≥70 A, ≥45 B, else none. A "none" verdict is still a
verdict — every tick returns grades so the shadow journal sees the
full distribution, not a survivor sample.

Loss-as-signal: a fleet stop_hit is the fleet paying for information.
Each stop_hit inside the window emits a Grade-A reversal candidate
(direction flips, source="loss_reversal", TP 1.5% / stop 0.8%,
prediction_score 75) routed through the ecosystem router by the
integrator. Three consecutive same-symbol losses (tracked on the
_outcome_chain via record_outcome) CONFIRM the reversal: the candidate's
score lifts to 85 — the reversal-confirm signal the spec schedules.

Cycle discipline (constants for the integrator's loop — the brain only
grades): 240s scan/generate/place cycle, fill-check at 90s, force-close
at 480s.

Admission envelope (binds the shadow too — the cohort must match what a
live path could take): max_concurrent 10; margin_per_scalp and budget
from cfg via getattr with safe test-scale defaults ($2 / $20),
hard-clamped — margin never exceeds the remaining budget share.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from intelligence.fleet_events import FleetEvent

SCHEMA_VERSION = 1

# ── Cycle discipline constants (the integrator's loop owns the clock) ───
CYCLE_PERIOD_S = 240          # scan → generate → place
FILL_CHECK_S = 90             # fill-check after placement
FORCE_CLOSE_S = 480           # force-close an unfilled/unmoved scalp

# ── Prediction score weights (sum = 100) ────────────────────────────────
W_L4_IMBALANCE = 25
W_OI_DELTA = 20
W_FLEET_VELOCITY = 20
W_CROSS_ASSET_SYNC = 20
W_FUNDING_VELOCITY = 15

# Normalization anchors (documented, doctrine constants):
L4_IMB_FULL = 0.30            # depth imbalance 0.30 = full 25 pts
OI_DELTA_FULL_PCT = 2.0       # 2% OI shift in-window = full 20 pts
FLEET_FILLS_FULL = 3          # 3 fleet fills in-window = full 20 pts
CROSS_MOVE_MIN_PCT = 0.2      # a family member "moves" at ≥0.2%
FUNDING_FULL = 0.0005         # 5bps funding = full 15 pts

# Grade ladder.
GRADE_A_MIN = 70
GRADE_B_MIN = 45

# Loss-as-signal reversal geometry (doctrine constants).
REVERSAL_TP_PCT = 0.015
REVERSAL_STOP_PCT = 0.008
REVERSAL_SCORE = 75
REVERSAL_CONFIRM_SCORE = 85     # with the 3-consecutive-loss chain
REVERSAL_CONFIRM_LOSSES = 3

# Default scalp geometry for predictive (non-reversal) candidates.
DEFAULT_TP_PCT = 0.012
DEFAULT_STOP_PCT = 0.006

# Fleet-event window the brain grades against.
FLEET_WINDOW_S = 300.0

# Admission envelope hard caps (cfg knobs override DOWNWARD only via
# getattr; these constants are the ceiling of last resort).
HARD_MAX_CONCURRENT = 10
DEFAULT_MARGIN_PER_SCALP = 2.0
DEFAULT_BUDGET_USD = 20.0

_OUTCOME_CHAIN_LEN = 20


def _f(x) -> Optional[float]:
    """Coerce to float; None on any garbage (fail-closed idiom)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:
        return None
    return v


@dataclass(frozen=True)
class ScalpSignalGrade:
    """One scalp verdict. Frozen: a grade is a fact about a tick, not a
    state. grade "none" carries direction "" and zeroed economics."""
    grade: str                      # "A" | "B" | "none"
    direction: str                  # "long" | "short" | ""
    source: str                     # "predictive" | "loss_reversal"
    primary_symbol: str
    ecosystem_symbols: Tuple[str, ...]
    prediction_score: int
    leverage: int
    margin_per_position: float
    tp_pct: float
    stop_pct: float


NONE_GRADE = ScalpSignalGrade(
    grade="none", direction="", source="predictive", primary_symbol="",
    ecosystem_symbols=(), prediction_score=0, leverage=0,
    margin_per_position=0.0, tp_pct=0.0, stop_pct=0.0)


# ── Sub-scorers (pure, documented normalization; None → 0.0) ────────────

def score_l4_imbalance(imbalance, direction: str) -> float:
    """0..25. imbalance ∈ [-1, 1] signed (bid-heavy positive). Agreement:
    long wants bid-heavy, short wants ask-heavy. |imb| / 0.30, clamped.
    None/garbage → 0.0 (dark)."""
    imb = _f(imbalance)
    if imb is None or direction not in ("long", "short"):
        return 0.0
    signed = imb if direction == "long" else -imb
    if signed <= 0:
        return 0.0
    return W_L4_IMBALANCE * min(1.0, signed / L4_IMB_FULL)


def score_oi_delta(oi_delta_pct, direction: str) -> float:
    """0..20. OI pct change over the window, signed. New positioning in
    the candidate direction agrees; |Δ| / 2.0%, clamped. None → 0.0."""
    d = _f(oi_delta_pct)
    if d is None or direction not in ("long", "short"):
        return 0.0
    signed = d if direction == "long" else -d
    if signed <= 0:
        return 0.0
    return W_OI_DELTA * min(1.0, signed / OI_DELTA_FULL_PCT)


def score_fleet_velocity(fill_count) -> float:
    """0..20. Count of fleet fills in the window; 3 = full. Direction-
    blind: the fleet moving size is the footprint either way. None → 0."""
    n = _f(fill_count)
    if n is None or n <= 0:
        return 0.0
    return W_FLEET_VELOCITY * min(1.0, n / float(FLEET_FILLS_FULL))


def score_cross_asset_sync(moves: Optional[Mapping[str, float]],
                           direction: str) -> float:
    """0..20. moves: family member → pct move in-window. Fraction of
    live members moving ≥0.2% in the candidate direction × 20. Empty /
    None → 0.0 (dark)."""
    if not isinstance(moves, Mapping) or not moves:
        return 0.0
    if direction not in ("long", "short"):
        return 0.0
    total, agree = 0, 0
    for mv in moves.values():
        v = _f(mv)
        if v is None:
            continue
        total += 1
        signed = v if direction == "long" else -v
        if signed >= CROSS_MOVE_MIN_PCT:
            agree += 1
    if total == 0:
        return 0.0
    return W_CROSS_ASSET_SYNC * (agree / total)


def score_funding_velocity(funding_rate, direction: str) -> float:
    """0..15. Contrarian positioning: positive funding = longs paying =
    crowded long = short-friendly (and mirrored). |rate| / 5bps,
    clamped. None → 0.0 (dark)."""
    r = _f(funding_rate)
    if r is None or direction not in ("long", "short"):
        return 0.0
    favorable = -r if direction == "long" else r
    if favorable <= 0:
        return 0.0
    return W_FUNDING_VELOCITY * min(1.0, favorable / FUNDING_FULL)


def grade_for_score(score: float) -> str:
    if score >= GRADE_A_MIN:
        return "A"
    if score >= GRADE_B_MIN:
        return "B"
    return "none"


class ParasiteScalperEngine:
    """The brain. cfg via getattr-with-default; router injected. State is
    the outcome chain and the last tick's journal rows — nothing else."""

    def __init__(self, cfg, ecosystem_router) -> None:
        self._cfg = cfg
        self._router = ecosystem_router
        self._outcome_chain: Dict[str, deque] = {}
        self._last_rows: List[dict] = []

    # ── outcome chain (the 3-consecutive-losses reversal-confirm) ──────

    def record_outcome(self, symbol: str, outcome: str) -> bool:
        """Register a closed scalp's result ("win"|"loss"). Never
        raises; foreign input drops silently."""
        try:
            sym = str(symbol or "").strip()
            oc = str(outcome or "").strip().lower()
            if not sym or oc not in ("win", "loss"):
                return False
            chain = self._outcome_chain.get(sym)
            if chain is None:
                chain = deque(maxlen=_OUTCOME_CHAIN_LEN)
                self._outcome_chain[sym] = chain
            chain.append(oc)
            return True
        except Exception:
            return False

    def consecutive_losses(self, symbol: str) -> int:
        """Trailing run of losses on the symbol's outcome chain."""
        try:
            chain = self._outcome_chain.get(str(symbol or "").strip())
            if not chain:
                return 0
            n = 0
            for oc in reversed(chain):
                if oc != "loss":
                    break
                n += 1
            return n
        except Exception:
            return 0

    # ── shadow journaling ──────────────────────────────────────────────

    def journal_rows(self) -> List[dict]:
        """Dense one-line dicts from the last tick (density law:
        reproducible records, not prose). Each row carries every
        component score with None marking a dark leg."""
        return [dict(r) for r in self._last_rows]

    # ── the tick ───────────────────────────────────────────────────────

    def tick(self, *, fleet_events: Sequence[FleetEvent],
             l4_signals, regime, oi_deltas, funding_rates,
             cross_asset_moves, open_scalps, narrative_direction,
             now) -> List[ScalpSignalGrade]:
        """Grade every candidate symbol this tick. Returns verdicts
        (grade "none" included). Never raises: shadow-disabled → [];
        internal fault → [] with zero state mutation."""
        try:
            if not bool(getattr(self._cfg, "scalper_shadow_enabled", True)):
                return []
            ts = _f(now)
            if ts is None or ts <= 0:
                return []

            events = [e for e in (fleet_events or ())
                      if isinstance(e, FleetEvent)]

            # Admission envelope — binds the shadow cohort too.
            max_conc = int(getattr(self._cfg, "scalper_max_concurrent",
                                   HARD_MAX_CONCURRENT)
                           or HARD_MAX_CONCURRENT)
            max_conc = max(1, min(HARD_MAX_CONCURRENT, max_conc))
            try:
                n_open = len(open_scalps or ())
            except Exception:
                n_open = 0
            margin = _f(getattr(self._cfg, "scalper_margin_usd",
                                DEFAULT_MARGIN_PER_SCALP))
            if margin is None or margin <= 0:
                margin = DEFAULT_MARGIN_PER_SCALP
            budget = _f(getattr(self._cfg, "scalper_budget_usd",
                                DEFAULT_BUDGET_USD))
            if budget is None or budget <= 0:
                budget = DEFAULT_BUDGET_USD
            remaining = max(0.0, budget - n_open * margin)
            margin = min(margin, remaining)  # hard clamp

            leverage = int(getattr(self._cfg, "scalper_leverage", 8) or 8)
            leverage = max(1, min(8, leverage))

            rows: List[dict] = []
            out: List[ScalpSignalGrade] = []

            # ── loss-as-signal: fleet stops pay for information ──────
            stops = [e for e in events if e.event_type == "stop_hit"]
            for ev in stops:
                flipped = "short" if ev.direction == "long" else "long"
                confirmed = (self.consecutive_losses(ev.symbol)
                             >= REVERSAL_CONFIRM_LOSSES)
                score = (REVERSAL_CONFIRM_SCORE if confirmed
                         else REVERSAL_SCORE)
                eco = tuple(self._router.get_family(ev.symbol))
                admitted = n_open + len([g for g in out if g.grade != "none"]) < max_conc
                grade = ScalpSignalGrade(
                    grade="A", direction=flipped, source="loss_reversal",
                    primary_symbol=ev.symbol,
                    ecosystem_symbols=eco,
                    prediction_score=score,
                    leverage=leverage,
                    margin_per_position=round(margin, 6),
                    tp_pct=REVERSAL_TP_PCT, stop_pct=REVERSAL_STOP_PCT,
                )
                rows.append({
                    "ts": ts, "symbol": ev.symbol, "grade": "A",
                    "source": "loss_reversal", "score": score,
                    "direction": flipped, "reversal_confirmed": confirmed,
                    "components": None, "admitted": admitted,
                })
                if admitted:
                    out.append(grade)

            # ── predictive grading per primary symbol ─────────────────
            symbols = sorted({e.symbol for e in events if e.event_type == "fill"})
            for sym in symbols:
                side = self._direction_for(sym, narrative_direction,
                                           l4_signals, oi_deltas,
                                           cross_asset_moves)
                if side is None:
                    rows.append({"ts": ts, "symbol": sym, "grade": "none",
                                 "source": "predictive", "score": 0,
                                 "direction": "", "components": None,
                                 "reason": "no_direction",
                                 "admitted": False})
                    out.append(NONE_GRADE)
                    continue
                comps, dark = self._components(
                    sym, side, events, l4_signals, oi_deltas,
                    funding_rates, cross_asset_moves, ts)
                score = int(round(sum(comps.values())))
                g = grade_for_score(score)
                eco = tuple(self._router.get_family(sym))
                admitted = (g != "none"
                            and n_open + len([x for x in out
                                              if x.grade != "none"]) < max_conc)
                rows.append({
                    "ts": ts, "symbol": sym, "grade": g,
                    "source": "predictive", "score": score,
                    "direction": side if g != "none" else "",
                    "components": {k: (None if k in dark else round(v, 3))
                                   for k, v in comps.items()},
                    "admitted": admitted,
                })
                if g == "none":
                    out.append(NONE_GRADE)
                elif admitted:
                    out.append(ScalpSignalGrade(
                        grade=g, direction=side, source="predictive",
                        primary_symbol=sym, ecosystem_symbols=eco,
                        prediction_score=score, leverage=leverage,
                        margin_per_position=round(margin, 6),
                        tp_pct=DEFAULT_TP_PCT, stop_pct=DEFAULT_STOP_PCT,
                    ))
            self._last_rows = rows
            return out
        except Exception:
            return []

    # ── internals ──────────────────────────────────────────────────────

    def _direction_for(self, sym: str, narrative_direction,
                       l4_signals, oi_deltas, cross_asset_moves
                       ) -> Optional[str]:
        """narrative_direction wins when it names a side for the symbol
        (str or mapping); else the majority vote of available signed
        evidence; a tie or all-dark → None (fail-closed)."""
        nd = narrative_direction
        if isinstance(nd, str):
            d = nd.strip().lower()
            if d in ("long", "short"):
                return d
        elif isinstance(nd, Mapping):
            d = str(nd.get(sym, "") or nd.get(sym.split("-")[0], "")
                   or "").strip().lower()
            if d in ("long", "short"):
                return d
        votes = 0
        imb = self._dig(l4_signals, sym)
        if _f(imb) is not None:
            votes += 1 if _f(imb) > 0 else -1 if _f(imb) < 0 else 0
        oi = self._dig(oi_deltas, sym)
        if _f(oi) is not None:
            votes += 1 if _f(oi) > 0 else -1 if _f(oi) < 0 else 0
        if isinstance(cross_asset_moves, Mapping):
            mv = self._dig(cross_asset_moves, sym)
            if _f(mv) is not None:
                votes += 1 if _f(mv) > 0 else -1 if _f(mv) < 0 else 0
        if votes > 0:
            return "long"
        if votes < 0:
            return "short"
        return None

    @staticmethod
    def _dig(mapping, sym: str):
        """mapping[sym] with base-name fallback; None when foreign."""
        if not isinstance(mapping, Mapping):
            return None
        v = mapping.get(sym)
        if v is None:
            v = mapping.get(sym.split("-")[0])
        return v

    def _components(self, sym: str, side: str,
                    events: Sequence[FleetEvent],
                    l4_signals, oi_deltas, funding_rates,
                    cross_asset_moves, ts: float
                    ) -> Tuple[Dict[str, float], set]:
        """Score components + the set of dark legs (input None)."""
        comps: Dict[str, float] = {}
        dark: set = set()

        imb = self._dig(l4_signals, sym)
        if _f(imb) is None:
            dark.add("l4")
        comps["l4"] = score_l4_imbalance(imb, side)

        oi = self._dig(oi_deltas, sym)
        if _f(oi) is None:
            dark.add("oi")
        comps["oi"] = score_oi_delta(oi, side)

        fills = sum(1 for e in events
                    if e.event_type == "fill" and e.symbol == sym)
        comps["fleet"] = score_fleet_velocity(fills)

        fam_moves = None
        if isinstance(cross_asset_moves, Mapping):
            fam_moves = {m: cross_asset_moves.get(m)
                         for m in self._router.get_family(sym)
                         if m != sym.split("-")[0] and m != sym
                         and cross_asset_moves.get(m) is not None}
            if not fam_moves:
                fam_moves = None
        if fam_moves is None:
            dark.add("cross")
        comps["cross"] = score_cross_asset_sync(fam_moves, side)

        fr = self._dig(funding_rates, sym)
        if _f(fr) is None:
            dark.add("funding")
        comps["funding"] = score_funding_velocity(fr, side)
        return comps, dark
