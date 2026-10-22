"""intelligence/spoils_ladder.py — partial-profit banking brain (pure, zero-I/O).

Governor directives (2026-10-22):
  "It can be for only campaign trades or trades above 200USD SO WE DONT BANK DUST"
  "THE AIM IS TO LOCK IN MORE PROFITS SOME PARTS NEED TO BE DYNAMIC"

The September census verdict: the exit class is the leak — 91.6% of stopped
trades went green first. The roe_ratchet moves STOPS; this ladder moves
CASH. When a position proves itself at an ATR-scaled ROE rung, bank a
fraction as realized PnL and let the remainder ride under a tighten-only
floor. Freeman-Shor / LeBeau: winners must not round-trip — bank
mechanically, never by feel. Aronson: the dynamic degrees of freedom are
constructor-bounded (k1, clamps, fractions) — the replay agent refines them,
never the live loop.

Department contract (docs/DEPARTMENT_TEMPLATE.md):
  - Zero I/O: no network, no files, no logging. The tide read is an injected
    callable; the splice executor performs every order and log line.
  - Kill switch SPOILS_LADDER_ENABLED (env, default true) read at CALL TIME
    via spoils_ladder_enabled() — False reproduces the pre-module system
    exactly: on_open refuses registration, on_mark abstains "none", the
    tracker holds no state, so no splice site can ever act.
  - Telemetry namespace spoils_* (emitted at splice sites by the executor,
    never here): spoils_registered, spoils_bank, spoils_floor_close,
    spoils_standdown (kill-switch-off / ineligible sightings),
    spoils_on_mark_error.
  - Fee engine stays OUT: this module never imports or touches anything
    fee-related; banking decisions are gross-ROE geometric, costs are the
    executor's accounting problem (_record_partial_close owns them).

Mechanics:
  rung1_roe% = clamp(k1 × atr_pct × leverage, rung_lo, rung_hi); rung2 = 2×rung1.
  atr_pct is in PERCENT units (1.5 = 1.5%); atr None/<=0 falls back to the
  static mid-rungs (STATIC_RUNG1_ROE, STATIC_RUNG2_ROE) so a dark ATR plane
  can never disarm the ladder. leverage <= 0 reads as 1.
  Eligibility: qty × entry >= min_notional_usd OR tags ∩ campaign_tags —
  the campaign/fast_cycle/anticipator family banks at any size; everything
  else must clear the dust floor.
  At a rung: bank bank_frac of the REMAINING position (the executor resolves
  qty against the live Position); an "opposed" tide escalates the fraction
  to bank_frac_opposed. tide_fn None / returning None / raising all abstain
  to the default fraction (fail-closed to LESS banking, never more).
  Floor: arms only after rung1 at floor_frac × the highest rung ROE hit;
  tighten-only (max ratchet); a mark whose ROE falls to the floor yields
  floor_close for ALL remaining (fraction 1.0), emitted once.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Optional

# ── Telemetry event names (emitted at splice sites, never here) ─────────────
SPOILS_REGISTERED = "spoils_registered"
SPOILS_BANK = "spoils_bank"
SPOILS_FLOOR_CLOSE = "spoils_floor_close"
SPOILS_STANDDOWN = "spoils_standdown"
SPOILS_ON_MARK_ERROR = "spoils_on_mark_error"

STATIC_RUNG1_ROE = 3.0     # static fallback when the ATR plane is dark
STATIC_RUNG2_ROE = 6.0     # (= 2 × rung1, the rung2 invariant holds)

DEFAULT_CAMPAIGN_TAGS = frozenset({"campaign", "fast_cycle", "anticipator"})


def spoils_ladder_enabled() -> bool:
    """Kill switch, read at call time (never cached). False = the pre-module
    system bit-for-bit: no registration, no verdicts, no state."""
    return os.environ.get("SPOILS_LADDER_ENABLED", "true").strip().lower() != "false"


def compute_rungs(atr_pct, leverage, k1: float, rung_lo: float, rung_hi: float):
    """(rung1_roe%, rung2_roe%) — ATR-scaled with clamps; rung2 = 2×rung1.

    Fail-closed: atr None/<=0/unreadable → static mid-rungs; leverage
    <=0/unreadable → 1; degenerate clamps (lo > hi) → static mid-rungs."""
    try:
        _k1, _lo, _hi = float(k1), float(rung_lo), float(rung_hi)
    except (TypeError, ValueError):
        return STATIC_RUNG1_ROE, STATIC_RUNG2_ROE
    if _lo > _hi:
        return STATIC_RUNG1_ROE, STATIC_RUNG2_ROE
    try:
        _atr = float(atr_pct) if atr_pct is not None else 0.0
    except (TypeError, ValueError):
        _atr = 0.0
    if _atr <= 0:
        return STATIC_RUNG1_ROE, STATIC_RUNG2_ROE
    try:
        _lev = float(leverage)
    except (TypeError, ValueError):
        _lev = 1.0
    if _lev <= 0:
        _lev = 1.0
    r1 = min(max(_k1 * _atr * _lev, _lo), _hi)
    return r1, 2.0 * r1


def _roe_pct(side: str, entry_price, mark_price, leverage) -> Optional[float]:
    """House ROE% = price move % × leverage, direction-aware (roe_ratchet
    idiom). None on any degenerate input — never raises."""
    try:
        _e, _m, _l = float(entry_price), float(mark_price), float(leverage)
    except (TypeError, ValueError):
        return None
    if _e <= 0 or _m <= 0 or _l <= 0 or side not in ("long", "short"):
        return None
    _move = (_m - _e) / _e if side == "long" else (_e - _m) / _e
    return _move * _l * 100.0


@dataclass(frozen=True)
class SpoilsConfig:
    """Tunables — constructor-injected so the replay agent can refine without
    touching call sites. Defaults are the Governor's 2026-10-22 directive."""
    min_notional_usd: float = 200.0
    campaign_tags: frozenset = DEFAULT_CAMPAIGN_TAGS
    k1: float = 1.0
    rung_lo: float = 2.0
    rung_hi: float = 6.0
    floor_frac: float = 0.6
    bank_frac: float = 1.0 / 3.0
    bank_frac_opposed: float = 0.5


@dataclass(frozen=True)
class SpoilsVerdict:
    """One on_mark decision. fraction is the fraction of the REMAINING
    position to bank (1.0 on floor_close = exit everything left)."""
    action: str = "none"          # "none" | "bank" | "floor_close"
    fraction: float = 0.0
    reason: str = "none"          # "none" | "rung1" | "rung2" | "floor"
    rung_hit: int = 0             # highest rung hit so far (0/1/2)
    floor_roe: Optional[float] = None   # armed floor ROE%, None pre-rung1
    roe: Optional[float] = None   # current ROE% (telemetry convenience)
    position_id: str = ""
    symbol: str = ""


@dataclass
class _Track:
    symbol: str
    side: str
    qty: float
    entry_price: float
    leverage: float
    atr_pct: Optional[float]
    tags: frozenset
    rung1_roe: float
    rung2_roe: float
    opened_s: float
    rung_hit: int = 0
    floor_roe: Optional[float] = None
    banks: int = 0
    closed: bool = False          # floor_close emitted — track is spent


class SpoilsTracker:
    """Zero-I/O per-position spoils brain. Owns rung/floor state keyed by
    position_id; the splice executor gathers inputs, calls, and acts."""

    def __init__(self, cfg: SpoilsConfig,
                 tide_fn: Optional[Callable[[str], Optional[str]]] = None) -> None:
        self.cfg = cfg
        self._tide_fn = tide_fn
        self._tracks: dict[str, _Track] = {}

    def on_open(self, position_id: str, symbol: str, side: str, qty,
                entry_price, leverage, atr_pct, tags=None,
                now_s: float = 0.0) -> bool:
        """Register ONLY if eligible (notional >= floor OR campaign-family
        tag). Idempotent: a re-register of a live track returns False and
        never mutates. Fail-closed on every degenerate input."""
        if not spoils_ladder_enabled():
            return False
        if not position_id or position_id in self._tracks:
            return False
        try:
            _qty, _entry = float(qty), float(entry_price)
        except (TypeError, ValueError):
            return False
        if _qty <= 0 or _entry <= 0:
            return False
        _side = str(side or "").strip().lower()
        if _side not in ("long", "short"):
            return False
        _tags = frozenset(tags) if tags else frozenset()
        _notional = _qty * _entry
        if _notional < float(self.cfg.min_notional_usd) \
                and not (_tags & self.cfg.campaign_tags):
            return False
        try:
            _lev = float(leverage)
        except (TypeError, ValueError):
            _lev = 1.0
        if _lev <= 0:
            _lev = 1.0
        try:
            _atr = float(atr_pct) if atr_pct is not None else None
        except (TypeError, ValueError):
            _atr = None
        _r1, _r2 = compute_rungs(_atr, _lev, self.cfg.k1,
                                 self.cfg.rung_lo, self.cfg.rung_hi)
        self._tracks[position_id] = _Track(
            symbol=str(symbol or ""), side=_side, qty=_qty, entry_price=_entry,
            leverage=_lev, atr_pct=_atr, tags=_tags,
            rung1_roe=_r1, rung2_roe=_r2, opened_s=float(now_s or 0.0))
        return True

    def _bank_fraction(self, symbol: str) -> float:
        """Tide-escalated fraction. None fn / None verdict / exception all
        abstain to the default (fail-closed to LESS banking)."""
        tide = None
        if self._tide_fn is not None:
            try:
                tide = self._tide_fn(symbol)
            except Exception:
                tide = None
        if tide == "opposed":
            return float(self.cfg.bank_frac_opposed)
        return float(self.cfg.bank_frac)

    def on_mark(self, position_id: str, mark_price, now_s: float = 0.0
                ) -> SpoilsVerdict:
        """Rung banks then floor check. Unknown ids, bad marks, kill-switch
        off, and spent tracks all abstain "none" — never raises."""
        _none = SpoilsVerdict(position_id=str(position_id or ""))
        if not spoils_ladder_enabled():
            return _none
        t = self._tracks.get(position_id)
        if t is None or t.closed:
            return _none
        roe = _roe_pct(t.side, t.entry_price, mark_price, t.leverage)
        if roe is None:
            return _none

        def _v(action, fraction, reason):
            return SpoilsVerdict(action=action, fraction=fraction,
                                 reason=reason, rung_hit=t.rung_hit,
                                 floor_roe=t.floor_roe, roe=roe,
                                 position_id=position_id, symbol=t.symbol)

        if t.rung_hit < 1 and roe >= t.rung1_roe:
            t.rung_hit = 1
            t.floor_roe = float(self.cfg.floor_frac) * t.rung1_roe
            t.banks += 1
            return _v("bank", self._bank_fraction(t.symbol), "rung1")
        if t.rung_hit < 2 and roe >= t.rung2_roe:
            t.rung_hit = 2
            _new_floor = float(self.cfg.floor_frac) * t.rung2_roe
            # Tighten-only: the floor ratchets up, never down.
            t.floor_roe = max(t.floor_roe or 0.0, _new_floor)
            t.banks += 1
            return _v("bank", self._bank_fraction(t.symbol), "rung2")
        if t.floor_roe is not None and roe <= t.floor_roe:
            t.closed = True    # one floor_close per position, then spent
            return _v("floor_close", 1.0, "floor")
        return _v("none", 0.0, "none")

    def on_close(self, position_id: str) -> bool:
        """Cleanup. Unknown ids are a no-op (False)."""
        return self._tracks.pop(position_id, None) is not None

    def state_snapshot(self) -> dict:
        """Telemetry readout — the splice executor logs/digests this."""
        return {
            "enabled": spoils_ladder_enabled(),
            "n_tracked": len(self._tracks),
            "positions": {
                pid: {
                    "symbol": t.symbol, "side": t.side, "qty": t.qty,
                    "entry_price": t.entry_price, "leverage": t.leverage,
                    "rung1_roe": t.rung1_roe, "rung2_roe": t.rung2_roe,
                    "rung_hit": t.rung_hit, "floor_roe": t.floor_roe,
                    "banks": t.banks, "closed": t.closed,
                    "tags": sorted(t.tags),
                }
                for pid, t in self._tracks.items()
            },
            "config": {
                "min_notional_usd": float(self.cfg.min_notional_usd),
                "k1": float(self.cfg.k1),
                "rung_lo": float(self.cfg.rung_lo),
                "rung_hi": float(self.cfg.rung_hi),
                "floor_frac": float(self.cfg.floor_frac),
                "bank_frac": float(self.cfg.bank_frac),
                "bank_frac_opposed": float(self.cfg.bank_frac_opposed),
            },
        }
