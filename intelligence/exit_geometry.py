"""Exit geometry: R:R shadow cohort + price-confirm trim gate (2026-09-17).

Two measure-first instruments for two live incidents:

(a) R:R COLLAPSE — the vol-stop floor widens stops 1% -> 4.5-8.5% and
    re-sizes to constant $ risk, but TP never re-rungs (every
    vol_stop_floored row shows tp1_before == tp1_after). HYPE realized
    0.53:1 vs the designed 2.5:1 — a geometry needing >65% WR to break
    even. RrShadowCohort runs a measure-only shadow twin per entry: same
    stop, TP re-rung to stop_pct * rr_target. On the real close it pairs
    real vs shadow and appends one JSONL row; report() answers "what
    would re-rung TPs have earned?" with n, not narrative.

(b) FALSE TRIM — a coherence-decay 50% trim cut a WINNING long at a
    session transition while price moved in our favor (session
    decorrelation, not thesis failure). TrimGate demands adverse-price
    confirmation before a trim executes: the trim thesis must be
    confirmed by price (pnl <= -(atr_pct * confirm_fraction)) or the
    trim is BLOCKed / DEFERred per trim type.

THRESHOLD PROVENANCE (UNSOURCED HYPOTHESES — module constants, not
derived from journal data; they ship so the cohort/gate can be measured
and retuned against evidence, per Aronson: bound every new degree of
freedom and score it from birth):
  - rr_target default 2.5 matches cfg.vol_stop_tp_rr (the designed R:R).
  - SHADOW_TIMEOUT_S 6h: a shadow unresolved after 6h resolves at last
    price (bounds counterfactual horizon; the modal hold is ~10 min).
  - session_transition confirm_fraction 0.30: session hand-offs decorrelate
    coherence without thesis failure, so the bar is widest here.
  - coherence_structural_break confirm_fraction 0.15 + defer_s 1800: a
    structural break is a stronger prior than a session transition, so a
    thinner price bar and a 30-min thesis-prove window instead of a block.
  - MAX_OPEN 256: far above observed concurrency (<=7 positions); bounds
    memory if on_realized is never called.

Percent scale throughout: stop_pct / tp_pct / atr_pct / pnl are PERCENT
(4.52 = 4.52%), never decimals — hard rule #7.

Kill switches (intelligence/kill_switch.py):
  rr_shadow_cohort   default ON  (measure-only)
  trim_price_confirm default OFF (gates nothing until armed)

Both classes are fail-silent: no public method may raise into trading
code. Department template: zero-I/O brain, injected clock/path, one
splice point per hook, kill switch False = pre-module system bit-for-bit.
"""

from __future__ import annotations

import json
import time
from collections import OrderedDict, deque
from enum import Enum
from pathlib import Path
from typing import Deque, Dict, List, Optional

import structlog

from intelligence import kill_switch

log = structlog.get_logger(__name__)

# ── Module constants (UNSOURCED hypotheses — see module docstring) ───────────
RR_SHADOW_PATH = Path("logs/rr_shadow_cohort.jsonl")
RR_TARGET_DEFAULT: float = 2.5          # matches cfg.vol_stop_tp_rr
SHADOW_TIMEOUT_S: float = 6.0 * 3600.0  # 6h counterfactual horizon
MAX_OPEN: int = 256                     # open-shadow cap, evict oldest
_MAX_RESOLVED: int = 256                # resolved-but-unpaired cap
_REPORT_MAXLEN: int = 4096              # in-memory pair window for report()


class TrimDecision(str, Enum):
    """TrimGate verdicts. str-Enum so structlog serializes them raw."""
    TRIM = "TRIM"
    BLOCK = "BLOCK"
    DEFER = "DEFER"


# Trim-type policy table (UNSOURCED hypotheses — see module docstring).
#   confirm_fraction: adverse-price bar = atr_pct * fraction (percent scale)
#   on_unconfirmed:   TrimDecision when price has NOT confirmed the trim
#   defer_s:          thesis-prove window before the single recheck
#   confirm_required False: price never consulted (cascades outrank geometry)
TRIM_TYPES: Dict[str, dict] = {
    "session_transition": {
        "confirm_fraction": 0.30,
        "on_unconfirmed": TrimDecision.BLOCK,
    },
    "coherence_structural_break": {
        "confirm_fraction": 0.15,
        "on_unconfirmed": TrimDecision.DEFER,
        "defer_s": 1800.0,
    },
    "hard_stop_cascade": {
        "confirm_required": False,
    },
}


def normalize_direction(direction: str) -> Optional[str]:
    """Normalize side spellings to "long"/"short"; None when unknowable.

    Both classes key all price geometry on this; callers pass raw Position.side
    or order sides ("BUY"/"SELL"). Fail-silent: unknown input -> None, and the
    caller treats that as invalid (register/evaluate bail out fail-open).
    """
    try:
        d = str(direction or "").strip().lower()
    except Exception:
        return None
    if d in ("long", "buy", "b"):
        return "long"
    if d in ("short", "sell", "s"):
        return "short"
    return None


# ── Kill-switch helpers (the wiring guard reads these) ───────────────────────

def rr_shadow_enabled() -> bool:
    """kill_switch 'rr_shadow_cohort' — default ON (measure-only)."""
    try:
        return kill_switch.enabled("rr_shadow_cohort")
    except Exception:
        return True


def trim_confirm_enabled() -> bool:
    """kill_switch 'trim_price_confirm' — default OFF (legacy trims)."""
    try:
        return kill_switch.enabled("trim_price_confirm")
    except Exception:
        return False


class RrShadowCohort:
    """Shadow twin per entry: same stop, TP re-rung to stop_pct * rr_target.

    Lifecycle: on_entry registers -> on_price resolves on stop/TP touch or
    6h timeout (last price) -> on_realized pairs real vs shadow and appends
    one JSONL row. If the real trade closes first, the shadow resolves at
    its last seen price at pairing time. Fail-silent throughout.
    """

    def __init__(self, path: Path = RR_SHADOW_PATH,
                 clock=time.time) -> None:
        self._path = Path(path)
        self._clock = clock
        self._open: "OrderedDict[str, dict]" = OrderedDict()
        self._resolved: "OrderedDict[str, dict]" = OrderedDict()
        self._pairs: "Deque[dict]" = deque(maxlen=_REPORT_MAXLEN)

    # ── Public API ──────────────────────────────────────────────────────────

    def on_entry(self, trade_id: str, symbol: str, direction: str,
                 entry_price: float, stop_pct: float,
                 tp_pct_original: float,
                 rr_target: float = RR_TARGET_DEFAULT,
                 now: Optional[float] = None) -> None:
        """Register a shadow twin. stop_pct / tp_pct_original in PERCENT."""
        try:
            side = normalize_direction(direction)
            entry = float(entry_price or 0.0)
            stop = float(stop_pct or 0.0)
            tp_orig = float(tp_pct_original or 0.0)
            rr = float(rr_target or 0.0)
            if (not trade_id or side is None or entry <= 0.0
                    or stop <= 0.0 or rr <= 0.0):
                return
            if trade_id in self._open or trade_id in self._resolved:
                return  # idempotent re-registration (duplicate fill events)
            tp_rerung = stop * rr
            self._open[trade_id] = {
                "trade_id": trade_id,
                "ts": float(now if now is not None else self._clock()),
                "symbol": symbol,
                "direction": side,
                "entry_price": entry,
                "stop_pct": stop,
                "tp_pct_original": tp_orig,
                "tp_pct_rerung": tp_rerung,
                "last_price": entry,
                "shadow_pnl_pct": None,  # set at resolution
            }
            while len(self._open) > MAX_OPEN:
                self._open.popitem(last=False)  # evict oldest unresolved
        except Exception:
            return

    def on_price(self, trade_id: str, price: float,
                 now: Optional[float] = None) -> None:
        """Advance one shadow with a fresh price; resolve on touch/timeout."""
        try:
            sh = self._open.get(trade_id)
            if sh is None:
                return
            px = float(price or 0.0)
            if px <= 0.0:
                return
            ts = float(now if now is not None else self._clock())
            sh["last_price"] = px
            entry = sh["entry_price"]
            stop = sh["stop_pct"] / 100.0
            tp = sh["tp_pct_rerung"] / 100.0
            if sh["direction"] == "long":
                if px <= entry * (1.0 - stop):
                    self._resolve(trade_id, sh, -sh["stop_pct"])
                    return
                if px >= entry * (1.0 + tp):
                    self._resolve(trade_id, sh, +sh["tp_pct_rerung"])
                    return
            else:
                if px >= entry * (1.0 + stop):
                    self._resolve(trade_id, sh, -sh["stop_pct"])
                    return
                if px <= entry * (1.0 - tp):
                    self._resolve(trade_id, sh, +sh["tp_pct_rerung"])
                    return
            if ts - sh["ts"] >= SHADOW_TIMEOUT_S:
                self._resolve(trade_id, sh, self._pnl_at(sh, px))
        except Exception:
            return

    def on_realized(self, trade_id: str, real_pnl_pct: float,
                    now: Optional[float] = None) -> None:
        """Pair the real close against the shadow and append one JSONL row.

        real_pnl_pct in PERCENT, signed in the position's favor direction.
        Shadow still open -> resolve at last seen price (the counterfactual
        "hold to this mark" answer). Unknown trade_id -> fail-silent no-op.
        """
        try:
            real = float(real_pnl_pct)
        except (TypeError, ValueError):
            return
        try:
            sh = self._open.pop(trade_id, None)
            if sh is not None:
                sh["shadow_pnl_pct"] = self._pnl_at(sh, sh["last_price"])
            else:
                sh = self._resolved.pop(trade_id, None)
            if sh is None or sh.get("shadow_pnl_pct") is None:
                return
            shadow = float(sh["shadow_pnl_pct"])
            pair = {
                "ts": float(now if now is not None else self._clock()),
                "trade_id": trade_id,
                "symbol": sh["symbol"],
                "direction": sh["direction"],
                "stop_pct": sh["stop_pct"],
                "tp_pct_original": sh["tp_pct_original"],
                "tp_pct_rerung": sh["tp_pct_rerung"],
                "real_pnl_pct": real,
                "shadow_pnl_pct": shadow,
                "ev_delta": shadow - real,
            }
            self._pairs.append(pair)
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._path, "a") as f:
                    f.write(json.dumps(pair) + "\n")
            except Exception as e:
                log.error("rr_shadow_cohort_write_error", error=str(e)[:120])
            log.info("rr_shadow_paired",
                     trade_id=trade_id, symbol=sh["symbol"],
                     real_pnl_pct=round(real, 4),
                     shadow_pnl_pct=round(shadow, 4),
                     ev_delta=round(pair["ev_delta"], 4))
        except Exception:
            return

    def report(self) -> dict:
        """Cohort stats over the in-memory pair window (last 4096 pairs).

        {n, mean_real, mean_shadow, mean_delta, win_rate_real,
        win_rate_shadow} — percents; empty cohort -> zeroed report.
        """
        try:
            pairs: List[dict] = list(self._pairs)
            n = len(pairs)
            if n == 0:
                return {"n": 0, "mean_real": 0.0, "mean_shadow": 0.0,
                        "mean_delta": 0.0, "win_rate_real": 0.0,
                        "win_rate_shadow": 0.0}
            reals = [p["real_pnl_pct"] for p in pairs]
            shadows = [p["shadow_pnl_pct"] for p in pairs]
            return {
                "n": n,
                "mean_real": sum(reals) / n,
                "mean_shadow": sum(shadows) / n,
                "mean_delta": sum(p["ev_delta"] for p in pairs) / n,
                "win_rate_real": sum(1 for r in reals if r > 0) / n,
                "win_rate_shadow": sum(1 for s in shadows if s > 0) / n,
            }
        except Exception:
            return {"n": 0, "mean_real": 0.0, "mean_shadow": 0.0,
                    "mean_delta": 0.0, "win_rate_real": 0.0,
                    "win_rate_shadow": 0.0}

    # ── Internals ───────────────────────────────────────────────────────────

    @staticmethod
    def _pnl_at(sh: dict, price: float) -> float:
        """Signed PERCENT pnl vs entry, in the position's favor direction."""
        entry = sh["entry_price"]
        if entry <= 0:
            return 0.0
        raw = (price - entry) / entry * 100.0
        return raw if sh["direction"] == "long" else -raw

    def _resolve(self, trade_id: str, sh: dict, shadow_pnl_pct: float) -> None:
        sh["shadow_pnl_pct"] = float(shadow_pnl_pct)
        self._open.pop(trade_id, None)
        self._resolved[trade_id] = sh
        while len(self._resolved) > _MAX_RESOLVED:
            self._resolved.popitem(last=False)  # drop oldest unpaired


class TrimGate:
    """Adverse-price confirmation gate for exit trims.

    The caller passes SIGNED pnl in the position's favor direction
    (negative = adverse). The trim executes only when price confirms the
    trim thesis: pnl_pct_vs_entry <= -(atr_pct * confirm_fraction).
    Unconfirmed trims follow the trim-type policy: session_transition
    BLOCKs outright (session decorrelation is not thesis failure);
    coherence_structural_break DEFERs once — recheck_due() arms a single
    30-min recheck and a still-unconfirmed recheck returns TRIM (a thesis-
    prove window, never a permanent hold). hard_stop_cascade never
    consults price. Every decision logs the trim_gate_decision event.
    Fail-silent: any internal error -> TRIM (fail-open legacy behavior).
    """

    def __init__(self, clock=time.time) -> None:
        self._clock = clock
        # position_id -> recheck epoch (one outstanding deferral each)
        self._deferred: Dict[str, float] = {}

    # ── Public API ──────────────────────────────────────────────────────────

    def evaluate(self, trim_type: str, position_id: str, direction: str,
                 pnl_pct_vs_entry: float, atr_pct: float,
                 now: Optional[float] = None) -> TrimDecision:
        """Gate one proposed trim. See class docstring for the policy."""
        try:
            ts = float(now if now is not None else self._clock())
            side = normalize_direction(direction) or str(direction or "")
            spec = TRIM_TYPES.get(str(trim_type or ""))
            if spec is None:
                # Unknown trim class: fail-open to legacy (trim proceeds).
                return self._decide(TrimDecision.TRIM, trim_type, position_id,
                                    side, pnl_pct_vs_entry, atr_pct,
                                    threshold=None, note="unknown_trim_type")
            if not spec.get("confirm_required", True):
                self._deferred.pop(position_id, None)
                return self._decide(TrimDecision.TRIM, trim_type, position_id,
                                    side, pnl_pct_vs_entry, atr_pct,
                                    threshold=None, note="confirm_not_required")
            try:
                pnl = float(pnl_pct_vs_entry)
                atr = float(atr_pct)
            except (TypeError, ValueError):
                return self._decide(TrimDecision.TRIM, trim_type, position_id,
                                    side, pnl_pct_vs_entry, atr_pct,
                                    threshold=None, note="invalid_inputs")
            if atr <= 0.0:
                # Degenerate vol plane: no honest bar — fail-open legacy.
                return self._decide(TrimDecision.TRIM, trim_type, position_id,
                                    side, pnl, atr, threshold=None,
                                    note="degenerate_atr")
            threshold = atr * float(spec["confirm_fraction"])

            # A due recheck resolves NOW: confirmed or not, the thesis-prove
            # window has expired — never a permanent hold, never a 2nd defer.
            recheck_at = self._deferred.get(position_id)
            if recheck_at is not None and ts >= recheck_at:
                self._deferred.pop(position_id, None)
                return self._decide(TrimDecision.TRIM, trim_type, position_id,
                                    side, pnl, atr, threshold=threshold,
                                    note="recheck_window_expired")

            if pnl <= -threshold:
                self._deferred.pop(position_id, None)
                return self._decide(TrimDecision.TRIM, trim_type, position_id,
                                    side, pnl, atr, threshold=threshold,
                                    note="price_confirmed")

            verdict = spec["on_unconfirmed"]
            if verdict is TrimDecision.DEFER:
                if position_id not in self._deferred:
                    self._deferred[position_id] = ts + float(
                        spec.get("defer_s", 1800.0))
                return self._decide(TrimDecision.DEFER, trim_type,
                                    position_id, side, pnl, atr,
                                    threshold=threshold,
                                    note="unconfirmed_deferred")
            self._deferred.pop(position_id, None)
            return self._decide(TrimDecision.BLOCK, trim_type, position_id,
                                side, pnl, atr, threshold=threshold,
                                note="unconfirmed_blocked")
        except Exception:
            # Fail-silent AND fail-open: a broken gate must never suppress a
            # legacy trim the risk brain already ordered.
            try:
                log.warning("trim_gate_error", trim_type=str(trim_type),
                            position_id=str(position_id))
            except Exception:
                pass
            return TrimDecision.TRIM

    def recheck_due(self, position_id: str,
                    now: Optional[float] = None) -> bool:
        """True when a DEFERred position's single recheck has come due."""
        try:
            recheck_at = self._deferred.get(position_id)
            if recheck_at is None:
                return False
            ts = float(now if now is not None else self._clock())
            return ts >= recheck_at
        except Exception:
            return False

    def forget(self, position_id: str) -> None:
        """Drop any pending deferral (position closed / trim executed)."""
        try:
            self._deferred.pop(position_id, None)
        except Exception:
            pass

    # ── Internals ───────────────────────────────────────────────────────────

    @staticmethod
    def _decide(verdict: TrimDecision, trim_type: str, position_id: str,
                direction: str, pnl_pct: float, atr_pct: float,
                threshold: Optional[float], note: str) -> TrimDecision:
        try:
            log.info("trim_gate_decision",
                     trim_type=str(trim_type),
                     position_id=str(position_id),
                     direction=direction,
                     decision=verdict.value,
                     pnl_pct_vs_entry=(round(float(pnl_pct), 4)
                                       if pnl_pct is not None else None),
                     atr_pct=(round(float(atr_pct), 4)
                              if atr_pct is not None else None),
                     adverse_threshold_pct=(round(threshold, 4)
                                            if threshold is not None else None),
                     note=note)
        except Exception:
            pass
        return verdict


# ── Module-level singletons (the main.py splice points read these) ───────────
_rr_cohort: Optional[RrShadowCohort] = None
_trim_gate: Optional[TrimGate] = None


def rr_cohort() -> RrShadowCohort:
    """Process-wide RrShadowCohort (lazy; measure-only so always safe)."""
    global _rr_cohort
    if _rr_cohort is None:
        _rr_cohort = RrShadowCohort()
    return _rr_cohort


def trim_gate() -> TrimGate:
    """Process-wide TrimGate (lazy)."""
    global _trim_gate
    if _trim_gate is None:
        _trim_gate = TrimGate()
    return _trim_gate
