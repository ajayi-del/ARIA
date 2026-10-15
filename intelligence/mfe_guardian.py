"""intelligence/mfe_guardian.py — Universal MFE tracking + one-writer floor
registry (pure brain, department template).

2026-10-15 (Governor-locked priority "only work on number 2"; September 2026
census): the exit class is the leak — 91.6% of stopped-out positions went
green FIRST (MFE > 0 before the stop) and coh-pnl ≈ 0. The machine gives
back open profit systematically. intelligence/roe_ratchet.py arms its ladder
at 3% ROE; the T1a early arm (0.3%) is still env-gated OFF pending shadow
proof — so the sub-3% MFE band where the round-trip cohort lives is
unguarded, and no universal MFE instrumentation proves where the giveback
happens.

This module closes the 0→3% gap and measures everything:

1. MfeTracker — universal MFE/MAE bookkeeping per position. on_open /
   on_mark / on_close; a close emits one complete mfe_roundtrip record dict
   (the integrator logs + journals it). Zero I/O: every input injected,
   every output returned.
2. ONE-WRITER floor registry — update_floor() is the ONLY mutation path,
   tighten-only by construction (MFE is a peak; the policy ladder's lock
   fraction is non-decreasing in MFE; and the stored floor is explicitly
   max/min'ed against the candidate). The floor covers the band BELOW the
   roe_ratchet ladder; the splice composes the two (max for longs, min for
   shorts — tighten-only) so there is never a second writer fighting the
   ratchet.
3. Regime × day-type policy ladder as DATA (DEFAULT_POLICY dict) — retuning
   is a config diff, not a code branch (Aronson: bounded degrees of freedom;
   the whole ladder is kill-switchable via policy["enabled"]=False).
4. Treasury/roe-ratchet handshake — treasury-managed and roe_ratchet-active
   positions ABSTAIN (floor_verdict → None): one position, one stop owner.
   Never two writers.

Freeman-Shor / LeBeau: winners must not become losers — bank mechanically,
never by feel. The census number this brain exists to shrink: the 91.6%
class (MFE +0.8% → closed −0.1% = roundtrip, giveback 0.9).
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

ROUNDTRIP_MFE_THRESHOLD_PCT = 0.5
"""A position whose peak favorable excursion reached this band and that
closed at or below breakeven is a giveback roundtrip. Matches the scalp
ladder's first rung — the smallest MFE the floor ever protects."""

SCALP_CLASSES = frozenset({"scalp", "scout"})
"""Fast classes bank early — the round-trip cohort is concentrated here."""

TREND_DAY_TYPES = frozenset({"trend", "trend_day", "trend_aligned", "aligned"})
"""Day-type labels the integrator may pass for a trend-day-ALIGNED position
(unknown/counter verdicts must never be passed as these)."""

DEFAULT_POLICY: Dict = {
    "enabled": True,                    # kill switch — False abstains ALL floors
    "roundtrip_mfe_threshold_pct": ROUNDTRIP_MFE_THRESHOLD_PCT,
    "classes": {
        # (mfe_pct_rung, floor_fraction_of_mfe) — ascending rungs, last wins.
        "scalp": {"rungs": ((0.5, 0.25), (1.0, 0.50))},
        "trend_aligned": {"rungs": ((1.0, 0.25), (2.0, 0.50))},
        "default": {"rungs": ((0.75, 0.25), (1.5, 0.50))},
    },
}
"""Regime × day-type ladder, as data. scalp/SCOUT banks faster (MFE ≥0.5% →
floor at 25% of MFE; ≥1.0% → 50%); trend-day-aligned positions get MORE room
(≥1.0% → 25%; ≥2.0% → 50%); everything else sits between. Fractions are the
share of the PEAK favorable move the floor protects."""


def _signed_move_pct(side: str, entry: float, price: float) -> Optional[float]:
    """Direction-signed price move % (favorable positive). None on degenerate."""
    try:
        _e, _p = float(entry), float(price)
    except (TypeError, ValueError):
        return None
    if _e <= 0 or _p <= 0 or side not in ("long", "short"):
        return None
    return ((_p - _e) / _e if side == "long" else (_e - _p) / _e) * 100.0


def _class_bucket(day_type: Optional[str], trade_class: Optional[str]) -> str:
    _tc = (trade_class or "").strip().lower()
    if _tc in SCALP_CLASSES:
        return "scalp"
    _dt = (day_type or "").strip().lower()
    if _dt in TREND_DAY_TYPES:
        return "trend_aligned"
    return "default"


def _policy_lookup(mfe_pct: float, regime: Optional[str],
                   day_type: Optional[str], trade_class: Optional[str],
                   policy: Optional[Dict] = None
                   ) -> Optional[Tuple[float, float, str]]:
    """(floor_fraction, rung_mfe_pct, bucket) or None (abstain / below first
    rung). Regime rides the record (telemetry + counters) — the v1 ladder
    keys on class/day-type per the Governor-locked default policy."""
    _p = policy or DEFAULT_POLICY
    if not _p.get("enabled", True):
        return None
    try:
        _mfe = float(mfe_pct)
    except (TypeError, ValueError):
        return None
    _bucket = _class_bucket(day_type, trade_class)
    _rungs = (_p.get("classes") or {}).get(_bucket, {}).get("rungs") or ()
    _hit: Optional[Tuple[float, float]] = None
    for _rung, _frac in _rungs:
        if _mfe >= float(_rung):
            _hit = (float(_rung), float(_frac))
    if _hit is None:
        return None
    return (_hit[1], _hit[0], _bucket)


def floor_verdict(mfe_pct: float, regime: Optional[str] = None,
                  day_type: Optional[str] = None,
                  trade_class: Optional[str] = None,
                  treasury_managed: bool = False,
                  roe_ratchet_active: bool = False,
                  policy: Optional[Dict] = None) -> Optional[float]:
    """Floor fraction of MFE (e.g. 0.25 = protect 25% of the peak move), or
    None to abstain. Pure + deterministic.

    Abstentions (ownership doctrine — never two writers):
      - treasury_managed: the treasury's own trailing-lock owns them.
      - roe_ratchet_active: the ≥3% ladder owns them — no double-tightening.
      - policy disabled / below the first rung / degenerate input."""
    if treasury_managed or roe_ratchet_active:
        return None
    _hit = _policy_lookup(mfe_pct, regime, day_type, trade_class, policy)
    return _hit[0] if _hit else None


class _Track:
    __slots__ = ("position_id", "side", "entry", "stop", "notional",
                 "regime", "day_type", "trade_class", "treasury_managed",
                 "roe_ratchet_active", "opened_at_s", "mfe_pct", "mae_pct",
                 "mfe_reached_at_s", "floor", "floor_rung")

    def __init__(self, position_id: str, side: str, entry: float, **kw):
        self.position_id = position_id
        self.side = side
        self.entry = entry
        self.stop = kw.get("stop")
        self.notional = kw.get("notional")
        self.regime = kw.get("regime")
        self.day_type = kw.get("day_type")
        self.trade_class = kw.get("trade_class")
        self.treasury_managed = bool(kw.get("treasury_managed", False))
        self.roe_ratchet_active = bool(kw.get("roe_ratchet_active", False))
        self.opened_at_s = kw.get("now_s")
        self.mfe_pct = 0.0
        self.mae_pct = 0.0
        self.mfe_reached_at_s = None
        self.floor = None
        self.floor_rung = None


class MfeTracker:
    """Universal MFE/MAE tracker + one-writer floor registry.

    Position state lives in memory (restart = fresh book; marks for unknown
    ids are restart-orphans and ignored). No I/O anywhere — the integrator
    injects every input and consumes every returned record."""

    def __init__(self, policy: Optional[Dict] = None):
        self._policy = policy or DEFAULT_POLICY
        self._open: Dict[str, _Track] = {}
        self._roundtrips: Dict[Tuple[Optional[str], Optional[str],
                                     Optional[str]], int] = {}
        self._closes = 0
        self._roundtrip_total = 0

    # ── lifecycle ────────────────────────────────────────────────────────

    def on_open(self, position_id: str, entry: float, side: str,
                stop: Optional[float] = None,
                notional: Optional[float] = None,
                regime: Optional[str] = None,
                day_type: Optional[str] = None,
                trade_class: Optional[str] = None,
                treasury_managed: bool = False,
                roe_ratchet_active: bool = False,
                now_s: Optional[float] = None) -> bool:
        """Register a position. False on degenerate input or a duplicate id
        (a live id is never silently reset mid-flight)."""
        try:
            _e = float(entry)
        except (TypeError, ValueError):
            return False
        if not position_id or _e <= 0 or side not in ("long", "short"):
            return False
        if position_id in self._open:
            return False
        self._open[position_id] = _Track(
            position_id, side, _e, stop=stop, notional=notional,
            regime=regime, day_type=day_type, trade_class=trade_class,
            treasury_managed=treasury_managed,
            roe_ratchet_active=roe_ratchet_active, now_s=now_s)
        return True

    def on_mark(self, position_id: str, mark: float,
                now_s: Optional[float] = None) -> Optional[Dict]:
        """Feed a mark. Updates MFE/MAE and attempts a floor raise via the
        single writer. Restart-orphan (unknown id) → ignored (None)."""
        _t = self._open.get(position_id)
        if _t is None:
            return None
        _move = _signed_move_pct(_t.side, _t.entry, mark)
        if _move is None:
            return None
        if _move > _t.mfe_pct:
            _t.mfe_pct = _move
            _t.mfe_reached_at_s = now_s
        if _move < _t.mae_pct:
            _t.mae_pct = _move
        return self.update_floor(position_id, mark)

    def on_close(self, position_id: str, exit_price: float, reason: str,
                 now_s: Optional[float] = None) -> Optional[Dict]:
        """Close out a position and emit the complete mfe_roundtrip record
        (integrator logs + journals it). Unknown id → None."""
        _t = self._open.pop(position_id, None)
        if _t is None:
            return None
        _realized = _signed_move_pct(_t.side, _t.entry, exit_price)
        if _realized is None:
            _realized = 0.0
        _thresh = float(self._policy.get(
            "roundtrip_mfe_threshold_pct", ROUNDTRIP_MFE_THRESHOLD_PCT))
        _roundtrip = _t.mfe_pct >= _thresh and _realized <= 0.0
        self._closes += 1
        if _roundtrip:
            self._roundtrip_total += 1
            _key = (_t.regime, _t.day_type, _t.trade_class)
            self._roundtrips[_key] = self._roundtrips.get(_key, 0) + 1
        return {
            "position_id": position_id,
            "side": _t.side,
            "entry": _t.entry,
            "exit_price": exit_price,
            "reason": reason,
            "mfe_pct": round(_t.mfe_pct, 4),
            "mae_pct": round(_t.mae_pct, 4),
            "realized_pct": round(_realized, 4),
            "giveback_pct": round(_t.mfe_pct - _realized, 4),
            "roundtrip": _roundtrip,
            "mfe_reached_at_s": _t.mfe_reached_at_s,
            "opened_at_s": _t.opened_at_s,
            "closed_at_s": now_s,
            "regime": _t.regime,
            "day_type": _t.day_type,
            "trade_class": _t.trade_class,
            "floor_at_close": _t.floor,
            "floor_rung": _t.floor_rung,
        }

    # ── the ONE floor writer ─────────────────────────────────────────────

    def update_floor(self, position_id: str,
                     mark: float) -> Optional[Dict]:
        """THE ONLY floor mutation path. Computes the policy floor from the
        PEAK (MFE never decays), enforces tighten-only against the stored
        floor, and returns the telemetry record on a raise — else None.

        A candidate already crossed by the mark (long floor ≥ mark, short
        floor ≤ mark) does NOT raise — the exit stack owns the position from
        there (mirrors roe_ratchet's mark-side guard). The stored floor is
        never removed or loosened by this path."""
        _t = self._open.get(position_id)
        if _t is None:
            return None
        _hit = _policy_lookup(_t.mfe_pct, _t.regime, _t.day_type,
                              _t.trade_class, self._policy)
        if _hit is None:
            return None
        if _t.treasury_managed or _t.roe_ratchet_active:
            return None                     # ownership handshake
        _frac, _rung, _bucket = _hit
        try:
            _m = float(mark)
        except (TypeError, ValueError):
            return None
        if _m <= 0:
            return None
        _move = _frac * _t.mfe_pct / 100.0
        _cand = (_t.entry * (1.0 + _move) if _t.side == "long"
                 else _t.entry * (1.0 - _move))
        # Tighten-only vs the stored floor.
        if _t.floor is not None:
            if _t.side == "long" and _cand <= _t.floor:
                return None
            if _t.side == "short" and _cand >= _t.floor:
                return None
        # Mark-side guard: never install/raise a floor the mark has crossed.
        if _t.side == "long" and _cand >= _m:
            return None
        if _t.side == "short" and _cand <= _m:
            return None
        _rec = {"position_id": position_id,
                "old_floor": _t.floor,
                "new_floor": _cand,
                "mfe_pct": round(_t.mfe_pct, 4),
                "policy_rung": _rung,
                "floor_fraction": _frac,
                "policy_bucket": _bucket,
                "regime": _t.regime,
                "day_type": _t.day_type}
        _t.floor = _cand
        _t.floor_rung = _rung
        return _rec

    # ── reads (never mutate) ─────────────────────────────────────────────

    def floor_for(self, position_id: str) -> Optional[float]:
        """The registered floor for the stop-placement splice. Read-only."""
        _t = self._open.get(position_id)
        return _t.floor if _t else None

    def mfe_for(self, position_id: str) -> Optional[float]:
        _t = self._open.get(position_id)
        return _t.mfe_pct if _t else None

    def tracked(self) -> int:
        return len(self._open)

    def counters(self) -> Dict:
        """Aggregate snapshot: roundtrips by (regime, day_type, trade_class)
        plus totals. Read-only copy."""
        return {"tracked": len(self._open),
                "closes": self._closes,
                "roundtrips": self._roundtrip_total,
                "roundtrips_by_key": dict(self._roundtrips)}
