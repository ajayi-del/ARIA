"""
intelligence/oracle_spread.py — Cross-venue mark-divergence brain (zero-I/O,
Governor venue-asymmetry axiom 2026-10-24).

THE ALPHA (Governor doctrine): Bybit is the signal plane (deepest book,
leads), Arcus is the new maker-0% venue, SoDEX is the carrier (thinner,
lags). When the same asset's mark diverges across venues beyond fee+friction
thresholds, the LAGGING venue converges to the leader — a short-horizon
arb/lead signal. All marks already exist in-process; nothing compared them
until this module. This brain only COMPARES — it never touches an exchange,
a file, or a logger; the splice executor (later integration pass) owns cadence
and telemetry.

Governor's locked spec (2026-10-24):
  - SPREAD_ALERT = 0.20% (20 bps), SPREAD_STRONG = 0.50% (50 bps).
  - Venue priority (who to trust as leader): bybit > arcus > sodex.
  - Direction-hint mapping table — IMPLEMENTED EXACTLY AS DIRECTED:
        lagger=arcus, arcus PREMIUM vs leader (spread > 0)  -> "long_arcus"
        lagger=sodex, sodex DISCOUNT vs leader (spread < 0) -> "long_sodex"
        lagger=sodex, sodex PREMIUM vs leader (spread > 0)  -> "fade_sodex"
    Every other combination (arcus discount, bybit-as-lagger) -> "none".
    Note the bybit-lagger case can only exist when bybit's mark is stale or
    absent (a fresh bybit mark is always the leader under the default
    priority), hence the mapping assigns it no hint.

Department shape (docs/DEPARTMENT_TEMPLATE.md):
  - Pure brain: zero I/O — no network, no files, no logging; now_s injected.
  - Exactly one splice point lands later (a thin executor feeding update()
    from the in-process mark stores and calling observe() on a cadence).
  - Kill switch: env ORACLE_SPREAD_ENABLED, read AT CALL TIME, default true.
    "false" = observe() returns [] and update() keeps NO state — the
    pre-module system bit-for-bit.
  - Own telemetry namespace (future splice, names reserved here):
        oracle_spread_alert        — |spread| >= alert_bps verdict emitted
        oracle_spread_strong       — |spread| >= strong_bps verdict emitted
        oracle_spread_leader_flip  — pair-spread EMA sign regime flipped
        oracle_spread_error        — fail-closed swallow inside the splice

Fail-closed everywhere: bad prices ignored, stale marks excluded
(> staleness_s), per-symbol defects never poison other symbols, and no code
path raises.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

STRENGTH_ALERT = "alert"
STRENGTH_STRONG = "strong"

HINT_LONG_ARCUS = "long_arcus"
HINT_LONG_SODEX = "long_sodex"
HINT_FADE_SODEX = "fade_sodex"
HINT_NONE = "none"


def oracle_spread_enabled() -> bool:
    """Env kill switch, read at call time. Default true; 'false' (any case)
    disables. A garbled value fails CLOSED to disabled? No — doctrine for
    this env class: only an explicit 'false' disables; anything else is on."""
    try:
        return os.environ.get("ORACLE_SPREAD_ENABLED", "true").strip().lower() != "false"
    except Exception:
        return True


def _f(x) -> Optional[float]:
    """Coerce to a finite float; None on any defect."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v or v in (math.inf, -math.inf):
        return None
    return v


@dataclass(frozen=True)
class OracleSpreadConfig:
    """All tunables in one frozen object (template §1: config injected)."""
    alert_bps: float = 20.0
    strong_bps: float = 50.0
    venue_priority: Tuple[str, ...] = ("bybit", "arcus", "sodex")
    staleness_s: float = 10.0        # a mark older than this is excluded
    ema_alpha: float = 0.2           # pair-spread EMA for leader-shift detect
    min_observations: int = 3        # EMA regime established after this many


@dataclass(frozen=True)
class SpreadVerdict:
    """One cross-venue divergence observation at or above alert threshold.

    spread_bps is SIGNED: (lagger_price - leader_price) / leader_price * 1e4.
    Positive = lagger marks ABOVE the leader (lagger premium).
    leader_flipped is additive (default False): True when the pair-spread
    EMA's sign regime flipped on this observation (post min_observations).
    """
    symbol: str
    leader_venue: str
    lagger_venue: str
    spread_bps: float
    strength: str                    # "alert" | "strong"
    direction_hint: str              # "long_arcus"|"long_sodex"|"fade_sodex"|"none"
    leader_price: float
    lagger_price: float
    ts_s: float
    leader_flipped: bool = False


def _direction_hint(lagger_venue: str, spread_bps: float) -> str:
    """Governor's mapping table 2026-10-24 — exact, nothing extrapolated."""
    if lagger_venue == "arcus" and spread_bps > 0:
        return HINT_LONG_ARCUS
    if lagger_venue == "sodex":
        if spread_bps < 0:
            return HINT_LONG_SODEX
        if spread_bps > 0:
            return HINT_FADE_SODEX
    return HINT_NONE


class OracleSpread:
    """Per-(symbol, venue) latest-mark table + per-(symbol, pair) spread EMA.

    Never raises. Disabled = no state kept, observe() returns [].
    """

    def __init__(self, config: Optional[OracleSpreadConfig] = None):
        self._cfg = config or OracleSpreadConfig()
        # (symbol, venue) -> (price, ts_s)
        self._marks: Dict[Tuple[str, str], Tuple[float, float]] = {}
        # (symbol, leader, lagger) -> {"ema": float, "regime": int, "n": int}
        self._pair_ema: Dict[Tuple[str, str, str], Dict[str, float]] = {}

    @property
    def config(self) -> OracleSpreadConfig:
        return self._cfg

    def update(self, symbol, venue, price, now_s) -> None:
        """Store the latest mark for (symbol, venue). price <= 0 or
        non-finite ignored. Disabled kill switch = no state kept."""
        try:
            if not oracle_spread_enabled():
                return
            p = _f(price)
            t = _f(now_s)
            if p is None or p <= 0 or t is None:
                return
            sym = str(symbol)
            ven = str(venue)
            if not sym or not ven:
                return
            self._marks[(sym, ven)] = (p, t)
        except Exception:
            return

    def observe(self, symbols, now_s) -> list:
        """Compare fresh marks per symbol against the priority leader.

        For each symbol with >= 2 fresh venue marks: leader = the
        highest-priority venue holding a fresh mark; every other fresh venue
        is a lagger; a verdict is emitted when |spread| >= alert_bps
        (strength "strong" at >= strong_bps). The pair EMA updates on EVERY
        computed spread (even sub-threshold) so the leader-shift regime is
        tracked on the full series; leader_flipped only rides emitted
        verdicts. Never raises; disabled -> []."""
        try:
            if not oracle_spread_enabled():
                return []
        except Exception:
            return []
        out = []
        try:
            t_now = _f(now_s)
            if t_now is None:
                return []
            for symbol in list(symbols or []):
                try:
                    out.extend(self._observe_one(str(symbol), t_now))
                except Exception:
                    continue
        except Exception:
            return out
        return out

    def _observe_one(self, symbol: str, t_now: float) -> list:
        cfg = self._cfg
        fresh: Dict[str, Tuple[float, float]] = {}
        for ven in cfg.venue_priority:
            rec = self._marks.get((symbol, ven))
            if rec is None:
                continue
            price, ts = rec
            if t_now - ts > cfg.staleness_s:
                continue
            fresh[ven] = rec
        # A venue outside the priority list still gets compared (never
        # leads — unknown venues sort after every configured one).
        for (sym, ven), rec in self._marks.items():
            if sym != symbol or ven in fresh or ven in cfg.venue_priority:
                continue
            price, ts = rec
            if t_now - ts <= cfg.staleness_s:
                fresh[ven] = rec
        if len(fresh) < 2:
            return []
        leader = None
        for ven in cfg.venue_priority:
            if ven in fresh:
                leader = ven
                break
        if leader is None:
            return []
        leader_price, _ = fresh[leader]
        if leader_price <= 0:
            return []
        verdicts = []
        for ven, (lagger_price, _) in fresh.items():
            if ven == leader:
                continue
            spread_bps = (lagger_price - leader_price) / leader_price * 10_000.0
            flipped = self._update_pair_ema(symbol, leader, ven, spread_bps)
            if abs(spread_bps) < cfg.alert_bps:
                continue
            strength = (STRENGTH_STRONG if abs(spread_bps) >= cfg.strong_bps
                        else STRENGTH_ALERT)
            verdicts.append(SpreadVerdict(
                symbol=symbol,
                leader_venue=leader,
                lagger_venue=ven,
                spread_bps=spread_bps,
                strength=strength,
                direction_hint=_direction_hint(ven, spread_bps),
                leader_price=leader_price,
                lagger_price=lagger_price,
                ts_s=t_now,
                leader_flipped=flipped,
            ))
        return verdicts

    def _update_pair_ema(self, symbol: str, leader: str, lagger: str,
                         spread_bps: float) -> bool:
        """EMA of the pair spread; returns True on a sign-regime flip once
        the regime is established (n >= min_observations)."""
        cfg = self._cfg
        key = (symbol, leader, lagger)
        st = self._pair_ema.get(key)
        if st is None:
            st = {"ema": spread_bps, "regime": 0, "n": 0}
        else:
            st["ema"] = cfg.ema_alpha * spread_bps + (1.0 - cfg.ema_alpha) * st["ema"]
        st["n"] += 1
        self._pair_ema[key] = st
        ema = st["ema"]
        sign = 1 if ema > 0 else (-1 if ema < 0 else 0)
        if st["n"] < cfg.min_observations:
            return False
        flipped = False
        if st["regime"] == 0:
            st["regime"] = sign
        elif sign != 0 and sign != st["regime"]:
            st["regime"] = sign
            flipped = True
        return flipped

    def state_snapshot(self) -> dict:
        """Dense reproducible snapshot (density law) — never raises."""
        try:
            return {
                "enabled": oracle_spread_enabled(),
                "config": {
                    "alert_bps": self._cfg.alert_bps,
                    "strong_bps": self._cfg.strong_bps,
                    "venue_priority": list(self._cfg.venue_priority),
                    "staleness_s": self._cfg.staleness_s,
                    "ema_alpha": self._cfg.ema_alpha,
                    "min_observations": self._cfg.min_observations,
                },
                "marks": {
                    f"{sym}|{ven}": {"price": p, "ts_s": t}
                    for (sym, ven), (p, t) in self._marks.items()
                },
                "pair_ema": {
                    f"{sym}|{leader}|{lagger}": {
                        "ema": st["ema"], "regime": st["regime"], "n": st["n"],
                    }
                    for (sym, leader, lagger), st in self._pair_ema.items()
                },
            }
        except Exception:
            return {"enabled": False, "marks": {}, "pair_ema": {}}
