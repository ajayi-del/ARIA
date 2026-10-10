"""
intelligence/ecosystem_router.py — ecosystem family router (Three-Layer
Network Phase 1, Governor order 2026-10-10). SHADOW-ONLY from birth.

One signal fans out to N family orders: the primary keeps its grade, the
proxies follow at demoted conviction. Families are a module-level map —
config-driven in SHAPE (the caller may inject a different map at
construction) but never read from config files by this module (zero-I/O
contract: intelligence/ brains import nothing from the environment).

Corrections folded into today's doctrine (2026-10-10):
  - RWA_MACRO must NOT include SILVER-USD — removed from the universe
    today (30d n=9, 0/9, −$39.18; see LEVERAGE_CAPS note in
    intelligence/fast_cycle_engine.py:80-83). Absent by construction.
  - 1000BONK / WIF / PENGU carry data_dark: True — the interpreter-
    silence RCA is in flight; the router EXPOSES the darkness (the
    family definition is honest) and active_family() lets the caller
    filter to symbols it says are alive. The router never routes into a
    symbol the caller's live_universe omits.

Leverage doctrine: ARIA caps this class at ≤8x. The router takes a
leverage_map argument (symbol → max leverage) — nothing is hardcoded to
the 15-40x exchange tiers; the clamp is min(requested, map value,
DEFAULT_MAX_LEVERAGE=8) unless the caller supplies a looser/tighter map.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

SCHEMA_VERSION = 1

# ARIA rule for this trade class: never above 8x absent an explicit
# caller map entry (Governor 2026-09-04 directive, still binding).
DEFAULT_MAX_LEVERAGE = 8

# Proxy demotion ladder (doctrine constants, not knobs): an A-grade
# primary fans B-grade proxies; a B-grade primary fans B-grade proxies
# (never promote). Prediction score decays a flat 10 points per hop.
PROXY_GRADE_DEMOTION = {"A": "B", "B": "B"}
PROXY_SCORE_DECAY = 10
PROXY_BETA_GEOMETRY_MULT = 1.5   # tp/stop ×1.5 for high-beta proxies

# ── Family map (2026-10-10 doctrine) ─────────────────────────────────────
# symbols:  leader first, proxies after. data_dark: symbols under an open
#           interpreter-silence RCA — routed only when the caller's
#           live_universe still claims them (active_family is the filter;
#           the darkness is exposed, never silently enforced).
DEFAULT_FAMILIES: Dict[str, dict] = {
    "BTC": {
        "symbols": ("BTC", "COIN", "HOOD", "MSTR"),
        "data_dark": (),
    },
    "ETH_DEFI": {
        "symbols": ("ETH", "LINK", "UNI", "AAVE"),
        "data_dark": (),
    },
    "SOLANA": {
        "symbols": ("SOL", "1000BONK", "WIF", "PENGU", "NEAR", "SUI"),
        "data_dark": ("1000BONK", "WIF", "PENGU"),
    },
    "AI_TECH": {
        "symbols": ("USTECH100", "NVDA", "AMD", "SPCX"),
        "data_dark": (),
    },
    # SILVER-USD deliberately absent (universe removal 2026-10-10).
    "RWA_MACRO": {
        "symbols": ("XAUT", "COPPER", "CL"),
        "data_dark": (),
    },
}

# High-beta proxy set — the proxies whose geometry widens ×1.5 (thin-book
# memes and single-name beta follow through wider than the leader's own
# stop). Constructor-injectable; this default is the doctrine starting
# point, not a measurement.
DEFAULT_HIGH_BETA = frozenset({
    "1000BONK", "WIF", "PENGU", "NEAR", "SUI",
    "COIN", "HOOD", "NVDA", "AMD",
})


def base_name(symbol: str) -> str:
    """BTC-USD / btc / BTC-USD.P → BTC (venue suffixes stripped)."""
    try:
        s = str(symbol)
    except Exception:
        return ""
    if not s or s == "None":
        return ""
    return s.split("-")[0].split(".")[0].strip().upper()


def _f(x) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:
        return None
    return v


@dataclass(frozen=True)
class RoutedOrder:
    """One fanned-out order intent (shadow verdict — nothing here places
    anything). Immutable once routed."""
    symbol: str
    role: str                    # "primary" | "proxy"
    grade: str                   # "A" | "B"
    direction: str               # "long" | "short"
    prediction_score: int
    leverage: int
    margin_per_position: float
    tp_pct: float
    stop_pct: float
    family: str


class EcosystemRouter:
    """Family map + fan-out. Constructor takes an optional families map
    and high-beta set (config-driven shape, zero file I/O). All methods
    fail closed: unknown symbol → singleton family; garbage grade → no
    routes."""

    def __init__(self,
                 families: Optional[Mapping[str, Mapping]] = None,
                 high_beta: Optional[Iterable[str]] = None) -> None:
        try:
            fam = families if isinstance(families, Mapping) \
                else DEFAULT_FAMILIES
            self._families: Dict[str, dict] = {
                str(name): {
                    "symbols": tuple(base_name(s) for s in
                                     (spec.get("symbols") or ())
                                     if base_name(s)),
                    "data_dark": tuple(base_name(s) for s in
                                       (spec.get("data_dark") or ())
                                       if base_name(s)),
                }
                for name, spec in fam.items() if isinstance(spec, Mapping)
            }
        except Exception:
            self._families = {k: dict(v) for k, v in DEFAULT_FAMILIES.items()}
        try:
            self._high_beta = frozenset(
                base_name(s) for s in (high_beta
                                       if high_beta is not None
                                       else DEFAULT_HIGH_BETA)
                if base_name(s))
        except Exception:
            self._high_beta = DEFAULT_HIGH_BETA

    # ── introspection ──────────────────────────────────────────────────

    def families(self) -> Dict[str, dict]:
        return {k: {"symbols": tuple(v["symbols"]),
                    "data_dark": tuple(v["data_dark"])}
                for k, v in self._families.items()}

    def family_name(self, symbol: str) -> Optional[str]:
        base = base_name(symbol)
        for name, spec in self._families.items():
            if base in spec["symbols"]:
                return name
        return None

    def get_family(self, symbol: str) -> List[str]:
        """Family membership (leader first) or [base] when unmapped —
        an unmapped symbol is its own ecosystem (doctrine: never refuse
        a signal for want of a family)."""
        base = base_name(symbol)
        if not base:
            return []
        for spec in self._families.values():
            if base in spec["symbols"]:
                return list(spec["symbols"])
        return [base]

    def data_dark_symbols(self, symbol: str) -> List[str]:
        """Family members under the interpreter-silence RCA. Exposure
        only — enforcement belongs to active_family's live_universe."""
        name = self.family_name(symbol)
        if name is None:
            return []
        return list(self._families[name]["data_dark"])

    def active_family(self, symbol: str,
                      live_universe: Optional[Iterable[str]]) -> List[str]:
        """Family filtered to symbols the caller says are alive. None =
        no filter (caller asserts the whole plane is live); an empty
        iterable = nothing alive → []. The primary is always first when
        alive; when the primary itself is dark the family still routes
        (the remaining live members, primary slot preserved at index 0
        by omission — the caller grades the verdict)."""
        members = self.get_family(symbol)
        if live_universe is None:
            return members
        try:
            live = {base_name(s) for s in live_universe if base_name(s)}
        except Exception:
            return members
        return [m for m in members if m in live]

    def is_high_beta(self, symbol: str) -> bool:
        return base_name(symbol) in self._high_beta

    # ── fan-out ────────────────────────────────────────────────────────

    def route_signal(self, grade, *,
                     leverage_map: Optional[Mapping[str, int]] = None,
                     live_universe: Optional[Iterable[str]] = None,
                     max_leverage: int = DEFAULT_MAX_LEVERAGE
                     ) -> List[RoutedOrder]:
        """1 signal → N family orders (shadow intents).

        grade is a duck-typed verdict (attributes: grade, direction,
        primary_symbol, prediction_score, leverage, margin_per_position,
        tp_pct, stop_pct) — the parasite scalper's ScalpSignalGrade
        satisfies it; the router never imports the scalper (no cycles).

        Rules: primary keeps its grade; proxies demote A→B; proxy
        prediction_score −10; proxy tp/stop ×1.5 for high-beta members;
        leverage clamped to min(requested, leverage_map[symbol],
        max_leverage). Degenerate grade → [] (fail-closed)."""
        try:
            g = str(getattr(grade, "grade", "") or "").upper()
            if g not in ("A", "B"):
                return []
            side = str(getattr(grade, "direction", "") or "").lower()
            if side not in ("long", "short"):
                return []
            primary = base_name(getattr(grade, "primary_symbol", ""))
            if not primary:
                return []
            score = int(getattr(grade, "prediction_score", 0) or 0)
            req_lev = int(getattr(grade, "leverage", 0) or 0)
            margin = max(0.0, _f(getattr(grade, "margin_per_position", 0.0))
                         or 0.0)
            tp = _f(getattr(grade, "tp_pct", 0.0)) or 0.0
            stop = _f(getattr(grade, "stop_pct", 0.0)) or 0.0
            if margin <= 0 or tp <= 0 or stop <= 0:
                return []

            try:
                ceiling = int(max_leverage)
            except (TypeError, ValueError):
                ceiling = DEFAULT_MAX_LEVERAGE
            ceiling = max(1, ceiling)

            members = self.active_family(primary, live_universe)
            if not members:
                return []
            fam_name = self.family_name(primary) or "singleton"

            out: List[RoutedOrder] = []
            for member in members:
                is_primary = (member == primary)
                if is_primary:
                    m_grade, m_score = g, score
                    m_tp, m_stop = tp, stop
                    role = "primary"
                else:
                    m_grade = PROXY_GRADE_DEMOTION.get(g, "B")
                    m_score = max(0, score - PROXY_SCORE_DECAY)
                    if member in self._high_beta:
                        m_tp = tp * PROXY_BETA_GEOMETRY_MULT
                        m_stop = stop * PROXY_BETA_GEOMETRY_MULT
                    else:
                        m_tp, m_stop = tp, stop
                    role = "proxy"
                map_cap = ceiling
                if leverage_map is not None:
                    try:
                        v = leverage_map.get(member)
                        if v is None:
                            v = leverage_map.get(member + "-USD")
                        if v is not None:
                            map_cap = max(1, int(v))
                    except Exception:
                        map_cap = ceiling
                lev = max(1, min(req_lev if req_lev > 0 else ceiling,
                                 map_cap, ceiling))
                out.append(RoutedOrder(
                    symbol=member,
                    role=role,
                    grade=m_grade,
                    direction=side,
                    prediction_score=int(m_score),
                    leverage=int(lev),
                    margin_per_position=round(margin, 6),
                    tp_pct=round(m_tp, 6),
                    stop_pct=round(m_stop, 6),
                    family=fam_name,
                ))
            return out
        except Exception:
            return []
