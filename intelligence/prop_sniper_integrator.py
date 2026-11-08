"""intelligence/prop_sniper_integrator.py — prop→SoDEX sniper integrator.

SHIP-WITH-GATES (Governor / learning-loops audit 2026-11-08): prop sleeves
are infantry; this module turns PropTrendBridge arms into Grade-A scalper
BOOSTS on the SoDEX path — never Kant/Nietzsche overrides, never Arcus,
never harness feedback.

Gates (all fail-closed):
  1. prop_sniper_enabled            shadow/brain path; False = bit-for-bit
  2. prop_sniper_live_enabled       LIVE boost/inject; default False
  3. Arm ≥ +1R                      brain MIN_R_CONFIRM (be_armed alone ≠
                                    confirmation — fee-concession sealed)
  4. Fire only on pullback          mark in entry half of mapped band
  5. TTL on prop stop-out           armed_until / missing leg / stale snap
  6. $8 risk clamp                  brain RISK_CLAMP_USD
  7. SoDEX venue only               venue != "sodex" → stand down
  8. ρ gate                         open SoDEX same-dir (symbol or family)
                                    → stand down (correlated ruin)
  9. BOOST never override           never flips a conflicting Grade-A;
                                    same-dir Grade-A gets score boost;
                                    inject only when no opposing grade

Zero-I/O department shape: cfg via getattr; snapshots/marks/bars/open
book arrive as tick arguments. File reads of prop_positions.json live
at the main.py splice. Never writes harness state / loss_veto / budget.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from intelligence.parasite_scalper import ScalpSignalGrade
from intelligence.prop_trend_bridge import (
    ARM_TTL_S,
    PropTrendBridge,
    RISK_CLAMP_USD,
    SniperArm,
)

SCHEMA_VERSION = 1

# Boost doctrine: lift an existing same-dir Grade-A; inject floor score
# when the parasite produced no opposing grade for the symbol.
BOOST_POINTS = 15
INJECT_SCORE = 78
INJECT_SOURCE = "prop_sniper"
BOOST_SOURCE_TAG = "prop_sniper_boost"

# Pullback: fire only in the cheap-entry half of the band.
# short → expensive half (zone_top side); long → cheap half (zone_bottom).


def _f(x) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:
        return None
    return v


def default_symbol_map() -> Dict[str, str]:
    """Prop asset → ARIA symbol. xyz: prefix strips to equity/commodity
    -USD; crypto bare tickers get -USD. Integrator callers may override."""
    return {}


def map_prop_asset(asset: str, symbol_map: Mapping[str, str]) -> Optional[str]:
    if not asset:
        return None
    if asset in symbol_map:
        return symbol_map[asset]
    if asset.startswith("xyz:"):
        base = asset.split(":", 1)[1]
        return f"{base}-USD"
    if "-" in asset:
        return asset
    return f"{asset}-USD"


def in_entry_zone(direction: str, mark: float, zone_top: float,
                  zone_bottom: float) -> bool:
    """True when mark sits in the pullback entry half of the band."""
    if direction not in ("long", "short"):
        return False
    if zone_top <= zone_bottom or mark <= 0:
        return False
    if not (zone_bottom <= mark <= zone_top):
        return False
    mid = 0.5 * (zone_top + zone_bottom)
    if direction == "short":
        return mark >= mid
    return mark <= mid


def rho_blocked(symbol: str, direction: str,
                open_sodex: Sequence[Mapping],
                family_fn: Callable[[str], Sequence[str]]) -> bool:
    """Same-dir open on symbol OR any family peer → correlated ruin."""
    if direction not in ("long", "short"):
        return True
    peers = set(str(m) for m in (family_fn(symbol) or ()) if m)
    peers.add(symbol)
    for pos in open_sodex or ():
        if not isinstance(pos, Mapping):
            continue
        sym = str(pos.get("symbol") or "")
        side = str(pos.get("direction") or pos.get("side") or "")
        if side in ("buy", "long"):
            side = "long"
        elif side in ("sell", "short"):
            side = "short"
        else:
            continue
        if side == direction and sym in peers:
            return True
    return False


def _arm_geometry_pcts(arm: SniperArm, mark: float
                       ) -> Optional[Tuple[float, float]]:
    """(tp_pct, stop_pct) from arm geometry vs mark. None if degenerate."""
    mk = _f(mark)
    if mk is None or mk <= 0:
        return None
    if arm.direction == "short":
        entry = arm.zone_top
        tp_pct = (entry - arm.target_price) / mk
        stop_pct = (arm.stop_price - entry) / mk
    else:
        entry = arm.zone_bottom
        tp_pct = (arm.target_price - entry) / mk
        stop_pct = (entry - arm.stop_price) / mk
    if tp_pct <= 0 or stop_pct <= 0:
        return None
    return tp_pct, stop_pct


def _grade_for_symbol(grades: Sequence[ScalpSignalGrade],
                      symbol: str) -> Optional[ScalpSignalGrade]:
    best = None
    for g in grades or ():
        if g is None or g.grade == "none":
            continue
        if g.primary_symbol != symbol:
            continue
        if best is None or g.prediction_score > best.prediction_score:
            best = g
    return best


def _live_prop_symbols(snapshots: Sequence[Mapping],
                       symbol_map: Mapping[str, str],
                       now: float) -> Dict[str, Mapping]:
    """Fresh snapshot legs keyed by ARIA symbol — stop-out = missing key."""
    from intelligence.prop_trend_bridge import SNAPSHOT_FRESH_S
    out: Dict[str, Mapping] = {}
    for snap in snapshots or ():
        if not isinstance(snap, Mapping):
            continue
        age = now - (_f(snap.get("ts")) or 0.0)
        if age > SNAPSHOT_FRESH_S:
            continue
        for pos in snap.get("positions") or ():
            if not isinstance(pos, Mapping):
                continue
            asset = str(pos.get("asset") or "")
            sym = symbol_map.get(asset) or map_prop_asset(asset, symbol_map)
            if sym:
                out[sym] = pos
    return out


class PropSniperIntegrator:
    """Fascia between PropTrendBridge arms and the SoDEX scalper path.

    Arm cache: the brain confirms at ≥+1R, but the fire is a later
    pullback (when R has often dipped). Once confirmed, the arm stays
    cached until prop stop-out (leg absent), stale snapshot, or TTL.
    """

    def __init__(self, cfg) -> None:
        self._cfg = cfg
        self._bridge = PropTrendBridge(cfg)
        self._last_rows: List[dict] = []
        self._armed: Dict[str, SniperArm] = {}

    def journal_rows(self) -> List[dict]:
        return [dict(r) for r in self._last_rows]

    def integrate(
        self,
        *,
        snapshots: Sequence[Mapping],
        grades: Sequence[ScalpSignalGrade],
        mark_fn: Callable[[str], Optional[float]],
        bars_fn: Callable[[str, float], Sequence[Sequence[float]]],
        family_fn: Callable[[str], Sequence[str]],
        symbol_map: Mapping[str, str],
        open_sodex: Sequence[Mapping],
        now: float,
        venue: str = "sodex",
    ) -> List[ScalpSignalGrade]:
        """Return boosted/injected grades. Never raises into the hot path."""
        try:
            if not bool(getattr(self._cfg, "prop_sniper_enabled", False)):
                self._last_rows = []
                self._armed.clear()
                return list(grades or ())

            rows: List[dict] = []
            ts = _f(now)
            if ts is None or ts <= 0:
                self._last_rows = [{"type": "prop_sniper_standdown",
                                   "reason": "bad_now"}]
                return list(grades or ())

            if str(venue or "").lower() != "sodex":
                rows.append({"type": "prop_sniper_standdown",
                             "reason": "venue_not_sodex", "venue": venue})
                self._last_rows = rows
                return list(grades or ())

            live = bool(getattr(self._cfg, "prop_sniper_live_enabled", False))
            fresh_legs = _live_prop_symbols(snapshots, symbol_map, ts)
            arms = self._bridge.tick(
                snapshots=snapshots, mark_fn=mark_fn, bars_fn=bars_fn,
                family_fn=family_fn, symbol_map=symbol_map, now=ts)
            rows.extend(self._bridge.journal_rows())

            # Refresh cache from new ≥+1R confirms; extend TTL.
            for a in arms:
                self._armed[a.symbol] = a

            # Prop stop-out / stale / TTL: drop missing or expired arms.
            for sym in list(self._armed.keys()):
                arm = self._armed[sym]
                if sym not in fresh_legs:
                    rows.append({"type": "prop_sniper_standdown",
                                 "symbol": sym, "reason": "prop_stop_out"})
                    self._armed.pop(sym, None)
                    continue
                if arm.armed_until < ts:
                    rows.append({"type": "prop_sniper_standdown",
                                 "symbol": sym, "reason": "ttl_expired",
                                 "armed_until": arm.armed_until})
                    self._armed.pop(sym, None)

            active = list(self._armed.values())

            out: List[ScalpSignalGrade] = list(grades or ())
            by_sym: Dict[str, ScalpSignalGrade] = {}
            for g in out:
                if g.grade == "none" or not g.primary_symbol:
                    continue
                prev = by_sym.get(g.primary_symbol)
                if prev is None or g.prediction_score > prev.prediction_score:
                    by_sym[g.primary_symbol] = g

            for arm in active:
                mark = _f(mark_fn(arm.symbol))
                if mark is None or mark <= 0:
                    rows.append({"type": "prop_sniper_standdown",
                                 "symbol": arm.symbol,
                                 "reason": "mark_dark"})
                    continue
                if arm.risk_usd > RISK_CLAMP_USD + 1e-9:
                    rows.append({"type": "prop_sniper_standdown",
                                 "symbol": arm.symbol,
                                 "reason": "risk_clamp",
                                 "risk": round(arm.risk_usd, 4)})
                    continue
                if not in_entry_zone(arm.direction, mark, arm.zone_top,
                                     arm.zone_bottom):
                    rows.append({"type": "prop_sniper_standdown",
                                 "symbol": arm.symbol,
                                 "reason": "awaiting_pullback",
                                 "mark": round(mark, 6),
                                 "zone": [round(arm.zone_bottom, 6),
                                          round(arm.zone_top, 6)]})
                    continue
                if rho_blocked(arm.symbol, arm.direction, open_sodex,
                               family_fn):
                    rows.append({"type": "prop_sniper_standdown",
                                 "symbol": arm.symbol,
                                 "direction": arm.direction,
                                 "reason": "rho_same_dir_open"})
                    continue

                existing = by_sym.get(arm.symbol)
                if (existing is not None and existing.direction
                        and existing.direction != arm.direction):
                    # BOOST never override — refuse to flip the parasite.
                    rows.append({"type": "prop_sniper_standdown",
                                 "symbol": arm.symbol,
                                 "reason": "override_refused",
                                 "existing_dir": existing.direction,
                                 "arm_dir": arm.direction})
                    continue

                pcts = _arm_geometry_pcts(arm, mark)
                if pcts is None:
                    rows.append({"type": "prop_sniper_standdown",
                                 "symbol": arm.symbol,
                                 "reason": "bad_arm_geometry"})
                    continue
                tp_pct, stop_pct = pcts

                if existing is not None and existing.direction == arm.direction:
                    boosted = replace(
                        existing,
                        prediction_score=min(
                            100, int(existing.prediction_score) + BOOST_POINTS),
                        source=BOOST_SOURCE_TAG,
                        # Keep parasite geometry; boost is score-only so we
                        # never silently replace a graded scalp's risk shape.
                    )
                    rows.append({
                        "type": "prop_sniper_boost",
                        "symbol": arm.symbol,
                        "direction": arm.direction,
                        "score_from": existing.prediction_score,
                        "score_to": boosted.prediction_score,
                        "r": round(arm.r_multiple, 3),
                        "live": live,
                    })
                    if live:
                        by_sym[arm.symbol] = boosted
                    continue

                # No same-dir grade: inject Grade-A from the prop arm.
                injected = ScalpSignalGrade(
                    grade="A",
                    direction=arm.direction,
                    source=INJECT_SOURCE,
                    primary_symbol=arm.symbol,
                    ecosystem_symbols=tuple(
                        str(m) for m in (family_fn(arm.symbol) or ()) if m),
                    prediction_score=INJECT_SCORE,
                    leverage=max(1, min(int(arm.leverage), 20)),
                    margin_per_position=round(float(arm.margin_usd), 6),
                    tp_pct=float(tp_pct),
                    stop_pct=float(stop_pct),
                )
                rows.append({
                    "type": "prop_sniper_inject",
                    "symbol": arm.symbol,
                    "direction": arm.direction,
                    "score": INJECT_SCORE,
                    "margin": injected.margin_per_position,
                    "risk": round(arm.risk_usd, 4),
                    "r": round(arm.r_multiple, 3),
                    "live": live,
                    "ttl_s": ARM_TTL_S,
                })
                if live:
                    by_sym[arm.symbol] = injected

            if not live:
                # Shadow: grades byte-for-bit unchanged; rows carry would-be.
                self._last_rows = rows
                return list(grades or ())

            # Rebuild list: replace matching symbols, keep others + news.
            kept = []
            seen = set()
            for g in grades or ():
                if g.grade == "none" or not g.primary_symbol:
                    kept.append(g)
                    continue
                repl = by_sym.get(g.primary_symbol)
                if repl is not None and repl is not g:
                    if g.primary_symbol not in seen:
                        kept.append(repl)
                        seen.add(g.primary_symbol)
                else:
                    kept.append(g)
                    seen.add(g.primary_symbol)
            for sym, g in by_sym.items():
                if sym not in seen:
                    kept.append(g)
            self._last_rows = rows
            return kept
        except Exception:
            self._last_rows = [{"type": "prop_sniper_standdown",
                               "reason": "integrator_fault"}]
            return list(grades or ())


def load_snapshots(paths: Iterable[str],
                   reader: Callable[[str], Optional[Mapping]]
                   ) -> List[Mapping]:
    """Integrator-owned path helper (pure over injected reader)."""
    out: List[Mapping] = []
    for p in paths or ():
        p = str(p or "").strip()
        if not p:
            continue
        try:
            snap = reader(p)
        except Exception:
            continue
        if isinstance(snap, Mapping):
            out.append(snap)
    return out
