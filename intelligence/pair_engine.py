"""
intelligence/pair_engine.py — Ratio Machine brain (Governor TASK #8, 2026-09-28)

The US500/USTECH100 ratio pair engine. "The netting problem is the hardest
constraint": SoDEX has no position IDs — a position is symbol + signed size —
so this brain carries a size-delta ledger (Decision-1): every leg snapshots
size_before / size_placed / size_expected at placement, exits reduce exactly
the placed delta, and any drift between the ledger and the live venue net is
a Cato-visible pair_engine_drift event, never silently absorbed.

Canon grounding (Governor directive — the trading-book doctrine is load-
bearing, not garnish):
  pairs   Gatev/Goetzmann/Rouwenhorst (distance method — trade the spread's
          excursion from its own center, not a forecast), Vidyamurthy
          (Pairs Trading — the ratio is the instrument; both legs are one
          position), Avellaneda & Lee (stat-arb on index/ETF legs — the
          USTECH/US500 ratio IS an equity stat-arb spread), Pole (the
          cointegration relationship is assumed UNTIL it breaks, and the
          break is a first-class event, not noise).
  process Chan (kill the cointegration assumption FIRST — the correlation-
          break check outranks every mode's exit logic), Lopez de Prado
          (backtest-overfitting discipline made live: abort_rate > 25% over
          the last 20 ledgers halts the machine — a placer that cannot
          assemble its pair is telling you the edge is not there), Carver
          (fixed session sizing — no module resizes mid-session; between-
          session steps are audit-gated), Aronson (every degree of freedom
          is bounded: band edges, valid range, tolerances, envelope, reserve
          — nothing floats free), Taleb (the correlation break is the
          dragon-king — exit BOTH legs at market, never average into a
          broken pair, lock out and reassess).

Department shape (docs/DEPARTMENT_TEMPLATE.md): zero-I/O brain. Ratio,
fleet state, whale age, clocks, and session stats are injected; the module
never reads param_store, mark stores, or the venue. The ONLY I/O in this
file is LedgerStore's own jsonl (logs/pair_ledger.jsonl) — the boot-
recovery plane, mirroring the _ant_fleet one-bad-line doctrine: a corrupt
line kills one record, never the boot.

Ratio source contract: ratio = ustech_mark / us500_mark, computed by the
CALLER from mark_price_stores behind the is_healthy gate. This brain takes
floats, never stores.

Governor-ruled values (2026-09-28) carry dated comments at the constant.
Brain-side calibration defaults (whale-trend shares, body/range minimums)
are bounded per Aronson and marked as such.
"""
from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

POOL_NAME = "pair_engine"
LEDGER_PATH = "logs/pair_ledger.jsonl"

# ── Governor-ruled geometry (2026-09-28 TASK #8 / Decision-1/2/3) ───────────
RATIO_CENTER = 3.935          # Governor 2026-09-28: USTECH100/US500 center
BAND_LO = 3.896               # Governor 2026-09-28: band lower edge
BAND_HI = 3.974               # Governor 2026-09-28: band upper edge
VALID_LO = 3.80               # Governor 2026-09-28: ratio valid range low
VALID_HI = 4.10               # Governor 2026-09-28: ratio valid range high
E1_TOL = 0.010                # Governor 2026-09-28: E1 convergence tolerance
E2_TRAIL_PCT = 0.5            # Governor 2026-09-28: E2 trail on USTECH leg (%)
E3_HALF_PCT = 1.5             # Governor 2026-09-28: E3 half-bank trigger (%)
WHALE_MAX_AGE_S = 1800.0      # Governor 2026-09-28: whale feed staleness cap
LOCKOUT_S = 28800.0           # Governor 2026-09-28: 2 x 14400s correlation lockout
ABORT_HALT_RATE = 0.25        # Governor 2026-09-28: abort-rate halt threshold
SESSION_LEG_MARGIN_USD = 45.0  # Governor 2026-09-28: session-fixed $45 margin/leg
POOL_DEFAULT_USD = 90.0       # Governor 2026-09-28: pair envelope (added capital)
MAX_TOTAL_MARGIN_USD = 280.0  # Governor 2026-09-28: deployed-margin invariant
RESERVE_USD = 20.0            # Governor 2026-09-28: untouchable reserve
STEP_UP_MULT = 1.25           # Governor 2026-09-28: between-session max step
STEP_DOWN_MULT = 0.80         # Governor 2026-09-28: auto −20% session step
STEP_UP_MIN_FILL_RATE = 0.05  # Governor 2026-09-28: Cato-audit gate
STEP_UP_MIN_NET_USD = -2.0    # Governor 2026-09-28: Cato-audit gate
STEP_DOWN_NET_USD = -5.0      # Governor 2026-09-28: auto-down trigger
STEP_DOWN_FILL_RATE = 0.02    # Governor 2026-09-28: auto-down trigger
MAX_OPEN_DD_PCT = 20.0        # Governor 2026-09-28: no step-up with >20% open DD
ABORT_WINDOW = 20             # Governor 2026-09-28: abort-rate window (ledgers)

# Brain-side calibration defaults (Aronson-bounded; NOT Governor-ruled —
# pending Cato calibration from shadow/live evidence).
_WHALE_TREND_SHARE = 0.65     # btc_long_share >= this (or <= 1-this) = trend
_BODY_MIN = 0.6               # |ustech_body_ratio| minimum for E2 amplifier
_RANGE_MIN = 0.03             # ratio_range minimum for E2 (range expansion)

_EPS = 1e-9


# ── Enums ────────────────────────────────────────────────────────────────────

class PairMode(str, Enum):
    E1_BAND = "E1_BAND"   # band reversion around the center
    E2_AMP = "E2_AMP"     # whale-confirmed trend amplifier
    E3_TRAP = "E3_TRAP"   # trap at band extremes


class PairStatus(str, Enum):
    INTENT = "INTENT"
    LEG_A_PENDING = "LEG_A_PENDING"
    BOTH_PENDING = "BOTH_PENDING"
    LIVE = "LIVE"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    ABORTED = "ABORTED"


# Legal status-machine edges. Everything else raises (the deliberate
# exception on the hot path — a corrupt state machine is worse than a crash).
_TRANSITIONS: Dict[PairStatus, set] = {
    PairStatus.INTENT: {PairStatus.LEG_A_PENDING, PairStatus.ABORTED},
    PairStatus.LEG_A_PENDING: {PairStatus.BOTH_PENDING, PairStatus.ABORTED},
    PairStatus.BOTH_PENDING: {PairStatus.LIVE, PairStatus.ABORTED},
    PairStatus.LIVE: {PairStatus.CLOSING, PairStatus.ABORTED},
    PairStatus.CLOSING: {PairStatus.CLOSED, PairStatus.ABORTED},
    PairStatus.CLOSED: set(),
    PairStatus.ABORTED: set(),
}


class IllegalTransitionError(ValueError):
    """Raised on any status transition outside _TRANSITIONS."""


# ── Size-delta ledger (Governor Decision-1) ──────────────────────────────────

@dataclass
class PairLeg:
    """One leg of the pair. size_* are SIGNED venue-net quantities
    (positive = net long) — the netting problem is the constraint."""
    symbol: str
    direction: str                 # "long" | "short"
    margin: float = 0.0
    leverage: int = 0
    notional: float = 0.0
    size_before: float = 0.0       # venue net snapshot at placement
    size_placed: float = 0.0       # the delta this pair placed
    size_expected: float = 0.0     # size_before + size_placed
    entry_price: float = 0.0
    filled: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol, "direction": self.direction,
            "margin": self.margin, "leverage": self.leverage,
            "notional": self.notional, "size_before": self.size_before,
            "size_placed": self.size_placed,
            "size_expected": self.size_expected,
            "entry_price": self.entry_price, "filled": self.filled,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PairLeg":
        return cls(
            symbol=str(d.get("symbol", "")),
            direction=str(d.get("direction", "")),
            margin=float(d.get("margin", 0.0)),
            leverage=int(d.get("leverage", 0)),
            notional=float(d.get("notional", 0.0)),
            size_before=float(d.get("size_before", 0.0)),
            size_placed=float(d.get("size_placed", 0.0)),
            size_expected=float(d.get("size_expected", 0.0)),
            entry_price=float(d.get("entry_price", 0.0)),
            filled=bool(d.get("filled", False)),
        )


@dataclass
class PairLedgerNetted:
    """The Decision-1 record: one pair = two legs = one instrument
    (Vidyamurthy). trail_anchor ratchets only (E2/E3); e3_half_done latches
    the E3 half-bank. Illegal status transitions raise."""
    mode: PairMode
    leg_a: PairLeg                 # USTECH leg (placed first per B3)
    leg_b: PairLeg                 # US500 leg
    pair_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    target_ratio: float = RATIO_CENTER
    stop_ratio: float = 0.0
    status: PairStatus = PairStatus.INTENT
    trail_anchor: Optional[float] = None
    e3_half_done: bool = False
    created_ts: float = 0.0

    def transition(self, new_status: PairStatus) -> None:
        if new_status not in _TRANSITIONS[self.status]:
            raise IllegalTransitionError(
                f"{self.status.value} -> {new_status.value}")
        self.status = new_status

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pair_id": self.pair_id, "mode": self.mode.value,
            "leg_a": self.leg_a.to_dict(), "leg_b": self.leg_b.to_dict(),
            "target_ratio": self.target_ratio, "stop_ratio": self.stop_ratio,
            "status": self.status.value, "trail_anchor": self.trail_anchor,
            "e3_half_done": self.e3_half_done, "created_ts": self.created_ts,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PairLedgerNetted":
        return cls(
            pair_id=str(d.get("pair_id", uuid.uuid4().hex)),
            mode=PairMode(str(d.get("mode", PairMode.E1_BAND.value))),
            leg_a=PairLeg.from_dict(d.get("leg_a") or {}),
            leg_b=PairLeg.from_dict(d.get("leg_b") or {}),
            target_ratio=float(d.get("target_ratio", RATIO_CENTER)),
            stop_ratio=float(d.get("stop_ratio", 0.0)),
            status=PairStatus(str(d.get("status", PairStatus.INTENT.value))),
            trail_anchor=d.get("trail_anchor"),
            e3_half_done=bool(d.get("e3_half_done", False)),
            created_ts=float(d.get("created_ts", 0.0)),
        )


# ── Ratio source contract ────────────────────────────────────────────────────

def compute_ratio(ustech_mark: Optional[float],
                  us500_mark: Optional[float]) -> Optional[float]:
    """ratio = ustech_mark / us500_mark. CALLER computes from healthy mark
    stores; this helper only enforces degenerate-input abstain (None)."""
    if ustech_mark is None or us500_mark is None:
        return None
    if ustech_mark <= 0.0 or us500_mark <= 0.0:
        return None
    return ustech_mark / us500_mark


# ── Fleet conflict detector (Layer 2) ────────────────────────────────────────

@dataclass(frozen=True)
class ConflictVerdict:
    """SAFE = no live fleet net on the symbol; ADDITIVE = fleet net same
    direction (record fleet_net as the leg's size_before context — netting
    IS the mechanic, Governor 2026-09-26 ruling); CONFLICT = opposite
    direction (caller skips, re-evaluates next 5m candle). Resting orders
    are context only — the verdict reads the LIVE net."""
    verdict: str                   # "SAFE" | "ADDITIVE" | "CONFLICT"
    size_before_hint: float = 0.0
    resting_context: int = 0


def _norm_direction(direction: Any) -> Optional[str]:
    d = str(direction or "").lower()
    if d in ("long", "buy"):
        return "long"
    if d in ("short", "sell"):
        return "short"
    return None


def detect_conflict(symbol: str, direction: str, fleet_net: Optional[float],
                    fleet_resting: Optional[list] = None) -> ConflictVerdict:
    """Layer-2 fleet conflict check. Fail-CLOSED: unknown direction or
    unknown fleet net reads CONFLICT (never place blind into the fleet)."""
    del symbol  # context for the caller's logs; the verdict is per-leg
    d = _norm_direction(direction)
    resting_n = len(fleet_resting) if fleet_resting else 0
    if d is None or fleet_net is None:
        return ConflictVerdict("CONFLICT", 0.0, resting_n)
    net = float(fleet_net)
    if abs(net) <= _EPS:
        return ConflictVerdict("SAFE", 0.0, resting_n)
    same = (net > 0.0) == (d == "long")
    if same:
        return ConflictVerdict("ADDITIVE", net, resting_n)
    return ConflictVerdict("CONFLICT", 0.0, resting_n)


# ── Size-delta exit math (Governor Decision-1) ───────────────────────────────

@dataclass(frozen=True)
class ExitSizePlan:
    """exit_size = exact size_placed on match; min(|live_net|, |size_placed|)
    on mismatch with drift=True (caller reports pair_engine_drift — Cato-
    visible). Degenerate inputs abstain (exit_size 0.0, never raise)."""
    exit_size: float
    drift: bool = False
    reason: str = "ok"


def exit_size_for_leg(live_net: Optional[float],
                      size_placed: Optional[float]) -> ExitSizePlan:
    if live_net is None or size_placed is None:
        return ExitSizePlan(0.0, drift=False, reason="degenerate")
    live = float(live_net)
    placed = float(size_placed)
    if abs(placed) <= _EPS:
        return ExitSizePlan(0.0, drift=False, reason="degenerate")
    if (live > 0.0) != (placed > 0.0):
        # The venue net flipped side vs the placed delta — nothing to
        # reduce against; this is maximal drift, not a close of 0.
        return ExitSizePlan(0.0, drift=True, reason="sign_flip")
    exit_size = min(abs(live), abs(placed))
    drift = abs(abs(live) - abs(placed)) > _EPS
    return ExitSizePlan(exit_size, drift=drift,
                        reason="drift" if drift else "ok")


def verify_post_close(net: Optional[float],
                      size_before: Optional[float]) -> bool:
    """Post-close restore check: venue net == the leg's size_before snapshot
    (fleet state restored). Degenerate inputs fail (False)."""
    if net is None or size_before is None:
        return False
    return math.isclose(float(net), float(size_before), abs_tol=_EPS)


# ── PairPool (B2 — Governor-funded envelope, FastCycleEngine shape) ─────────

class PairPool:
    """In-memory pool ledger for the SEPARATE pair envelope (default $90 —
    the ladder is NOT cut to fund the pair; Governor 2026-09-28 evening).
    Debit on leg placement, credit on close, rebuild(open_margins) at boot."""

    def __init__(self) -> None:
        self._debited: float = 0.0

    def debit(self, margin_usd: float) -> None:
        self._debited += max(0.0, float(margin_usd))

    def credit(self, margin_usd: float) -> None:
        self._debited = max(0.0, self._debited - max(0.0, float(margin_usd)))

    def rebuild(self, open_margins_usd: float) -> None:
        """Boot recovery: re-debit the margins of still-open pair legs."""
        self._debited = max(0.0, float(open_margins_usd))

    @property
    def debited(self) -> float:
        return self._debited

    def preflight(self, cfg, leg_margin_usd: float) -> bool:
        """B2 envelope preflight: debited + leg_margin x 2 <= pool.
        Fail-closed on degenerate margin (False)."""
        margin = float(leg_margin_usd or 0.0)
        if margin <= 0.0:
            return False
        pool = float(getattr(cfg, "pair_engine_pool_usd", POOL_DEFAULT_USD))
        return (self._debited + margin * 2.0) <= pool + _EPS


# ── Mode selector (Layer 4, pure) ────────────────────────────────────────────

def select_mode(ratio_now: Optional[float],
                ratio_range: Optional[float],
                btc_long_share: Optional[float],
                ustech_body_ratio: Optional[float],
                whale_age_s: Optional[float]) -> str:
    """E1_BAND | E2_AMP | E3_TRAP | NO_TRADE.

    Gate order (Chan — kill the relationship assumption FIRST):
      1. degenerate inputs or ratio outside [3.80, 4.10] -> NO_TRADE
      2. whale feed stale (> 1800s, Governor 2026-09-28) -> NO_TRADE
      3. ratio at/beyond the band edges (3.896/3.974) -> E3_TRAP
      4. whale-confirmed trend (strong share + body + range expansion)
         -> E2_AMP
      5. default -> E1_BAND (distance-method reversion to center 3.935)
    """
    if ratio_now is None or whale_age_s is None:
        return "NO_TRADE"
    ratio = float(ratio_now)
    if ratio <= 0.0 or ratio < VALID_LO or ratio > VALID_HI:
        return "NO_TRADE"
    if float(whale_age_s) < 0.0 or float(whale_age_s) > WHALE_MAX_AGE_S:
        return "NO_TRADE"
    if ratio <= BAND_LO or ratio >= BAND_HI:
        return "E3_TRAP"
    share = float(btc_long_share) if btc_long_share is not None else None
    body = float(ustech_body_ratio) if ustech_body_ratio is not None else None
    rng = float(ratio_range) if ratio_range is not None else None
    if (share is not None and body is not None and rng is not None
            and (share >= _WHALE_TREND_SHARE or share <= 1.0 - _WHALE_TREND_SHARE)
            and abs(body) >= _BODY_MIN and rng >= _RANGE_MIN):
        return "E2_AMP"
    return "E1_BAND"


# ── Exit engine (Layer 5, pure) ──────────────────────────────────────────────

@dataclass(frozen=True)
class ExitVerdict:
    """action: "hold" | "close_both" | "close_half". USTECH leg closes first
    (B5). lockout_until is set ONLY on correlation_break (caller stamps
    pair:lockout_until; Taleb — the dragon-king gets the full lockout)."""
    action: str
    reason: str = ""
    lockout_until: Optional[float] = None
    trail_anchor: Optional[float] = None
    close_fraction: float = 1.0
    ustech_first: bool = True


def _hold(reason: str, anchor: Optional[float] = None) -> ExitVerdict:
    return ExitVerdict("hold", reason=reason, trail_anchor=anchor)


def evaluate_exit(ledger: Optional[PairLedgerNetted],
                  ratio_now: Optional[float],
                  ustech_leg_move_pct: Optional[float],
                  now_ts: Optional[float],
                  *,
                  e1_tol: float = E1_TOL,
                  trail_pct: float = E2_TRAIL_PCT,
                  e3_half_pct: float = E3_HALF_PCT,
                  lockout_s: float = LOCKOUT_S) -> ExitVerdict:
    """Layer-5 exit evaluation. Correlation break (ratio < 3.80 or > 4.10)
    OUTRANKS every mode — close both legs + lockout signal (now + 28800s).
    E1: |ratio - 3.935| <= 0.010 -> close both (USTECH first).
    E2: 0.5% trail on the USTECH leg move, ratchet-only trail_anchor.
    E3: 50% bank at |move| >= 1.5%, then the same trail on the remainder.
    Degenerate inputs abstain (hold) — never raise on the tick loop."""
    if ledger is None or ratio_now is None or now_ts is None:
        return _hold("degenerate")
    ratio = float(ratio_now)
    if ratio <= 0.0:
        return _hold("degenerate")
    move = float(ustech_leg_move_pct) if ustech_leg_move_pct is not None else 0.0

    # Correlation break — Taleb's dragon-king; Pole's assumed relationship
    # is dead. Both legs, market, lockout. No averaging, no second chance.
    if ratio < VALID_LO or ratio > VALID_HI:
        return ExitVerdict("close_both", reason="correlation_break",
                           lockout_until=float(now_ts) + lockout_s,
                           trail_anchor=ledger.trail_anchor)

    if ledger.mode == PairMode.E1_BAND:
        if abs(ratio - RATIO_CENTER) <= e1_tol:
            return ExitVerdict("close_both", reason="e1_convergence",
                               trail_anchor=ledger.trail_anchor)
        return _hold("e1_open", ledger.trail_anchor)

    if ledger.mode == PairMode.E3_TRAP and not ledger.e3_half_done:
        if abs(move) >= e3_half_pct:
            ledger.e3_half_done = True
            ledger.trail_anchor = move
            return ExitVerdict("close_half", reason="e3_half_bank",
                               trail_anchor=move, close_fraction=0.5)
        return _hold("e3_armed", ledger.trail_anchor)

    # E2 trail / E3 post-half trail — ratchet-only anchor on the ledger.
    anchor = ledger.trail_anchor
    new_anchor = move if anchor is None else max(anchor, move)
    ledger.trail_anchor = new_anchor
    if move <= new_anchor - trail_pct:
        reason = "e3_trail" if ledger.mode == PairMode.E3_TRAP else "e2_trail"
        return ExitVerdict("close_both", reason=reason,
                           trail_anchor=new_anchor)
    return _hold("trail_open", new_anchor)


# ── Abort tracker (Layer 8 — Lopez de Prado live discipline) ────────────────

class AbortTracker:
    """abort_rate over the last ABORT_WINDOW (20) ledgers; rate STRICTLY
    greater than 0.25 halts (exactly 0.25 does NOT halt). A placer that
    cannot assemble its pair a quarter of the time is evidence the edge is
    not there — stand placements down (caller stamps pair:halted=1)."""

    def __init__(self, window: int = ABORT_WINDOW) -> None:
        self._window = max(1, int(window))
        self._outcomes: List[bool] = []   # True = aborted

    def record(self, aborted: bool) -> None:
        self._outcomes.append(bool(aborted))
        if len(self._outcomes) > self._window:
            del self._outcomes[: len(self._outcomes) - self._window]

    @property
    def abort_rate(self) -> float:
        if not self._outcomes:
            return 0.0
        return sum(1 for a in self._outcomes if a) / len(self._outcomes)

    @property
    def halted(self) -> bool:
        return self.abort_rate > ABORT_HALT_RATE


# ── LedgerStore (B1/B8 — the module's ONLY I/O plane) ───────────────────────

class LedgerStore:
    """jsonl append at logs/pair_ledger.jsonl + boot rebuild. One-bad-line
    doctrine (the _ant_fleet idiom): a corrupt line kills one record,
    never the boot."""

    def __init__(self, path: str = LEDGER_PATH) -> None:
        self._path = str(path)

    @property
    def path(self) -> str:
        return self._path

    def append(self, ledger: PairLedgerNetted) -> bool:
        """Append one ledger record. Best-effort: I/O failure returns
        False, never raises into the hot path."""
        try:
            with open(self._path, "a") as fh:
                fh.write(json.dumps(ledger.to_dict()) + "\n")
            return True
        except (OSError, TypeError, ValueError):
            return False

    def rebuild_from_jsonl(self, path: Optional[str] = None) -> List[PairLedgerNetted]:
        """Boot recovery: replay the jsonl into ledgers. Missing file =
        empty boot; each corrupt line is skipped alone."""
        out: List[PairLedgerNetted] = []
        try:
            with open(path or self._path) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(PairLedgerNetted.from_dict(json.loads(line)))
                    except (ValueError, TypeError, KeyError, AttributeError):
                        continue
        except OSError:
            return []
        return out


# ── Sizing doctrine (Governor Decision-3 — session-fixed, audit-gated) ──────

@dataclass(frozen=True)
class SizingVerdict:
    """Between-session sizing decision. action: "step_up" | "step_down" |
    "hold" | "blocked". Within a session the leg margin is FIXED (Carver)
    — this verdict is only evaluated at the session boundary, on stats the
    caller passes in; the brain never reads param_store."""
    action: str
    new_leg_margin_usd: float
    reason: str = ""


def session_boundary_verdict(*, fill_rate: Optional[float],
                             session_net_usd: Optional[float],
                             correlation_break: Optional[bool],
                             operator_confirmed: Optional[bool],
                             max_open_dd_pct: Optional[float],
                             current_leg_margin_usd: Optional[float]
                             ) -> SizingVerdict:
    """Cato-audit gates (ALL required for x1.25 max step): fill rate >= 5%,
    prior-session net >= -$2, no correlation break, explicit operator
    confirmation, and no open position >20% margin in drawdown.
    Auto -20% (no confirmation needed) on session net < -$5 or fill rate
    < 2%. The $280/$20 invariants clamp every outcome. Degenerate stats
    abstain (hold)."""
    if (fill_rate is None or session_net_usd is None
            or correlation_break is None or operator_confirmed is None
            or max_open_dd_pct is None or current_leg_margin_usd is None):
        return SizingVerdict("hold", float(current_leg_margin_usd or 0.0),
                             reason="degenerate")
    current = float(current_leg_margin_usd)
    if current <= 0.0 or float(fill_rate) < 0.0:
        return SizingVerdict("hold", max(0.0, current), reason="degenerate")

    leg_cap = MAX_TOTAL_MARGIN_USD / 2.0   # two legs <= $280 invariant

    # Auto-down outranks step-up: damage is repaired before offense resumes.
    if float(session_net_usd) < STEP_DOWN_NET_USD:
        return SizingVerdict("step_down",
                             round(current * STEP_DOWN_MULT, 8),
                             reason="session_net_below_-5")
    if float(fill_rate) < STEP_DOWN_FILL_RATE:
        return SizingVerdict("step_down",
                             round(current * STEP_DOWN_MULT, 8),
                             reason="fill_rate_below_2pct")

    gates = {
        "fill_rate": float(fill_rate) >= STEP_UP_MIN_FILL_RATE,
        "session_net": float(session_net_usd) >= STEP_UP_MIN_NET_USD,
        "no_correlation_break": not bool(correlation_break),
        "operator_confirmed": bool(operator_confirmed),
        "open_dd": float(max_open_dd_pct) <= MAX_OPEN_DD_PCT,
    }
    if all(gates.values()):
        return SizingVerdict(
            "step_up", round(min(current * STEP_UP_MULT, leg_cap), 8),
            reason="cato_audit_pass")
    failed = [k for k, ok in gates.items() if not ok]
    return SizingVerdict("hold", current,
                         reason="gates_failed:" + ",".join(failed))


def within_margin_invariant(total_margin_usd: Optional[float]) -> bool:
    """Invariant: max total margin deployed <= $280 (Governor 2026-09-28)."""
    if total_margin_usd is None:
        return False
    return float(total_margin_usd) <= MAX_TOTAL_MARGIN_USD + _EPS


def reserve_ok(available_usd: Optional[float]) -> bool:
    """The $20 reserve is untouchable (Governor 2026-09-28)."""
    if available_usd is None:
        return False
    return float(available_usd) >= RESERVE_USD - _EPS
