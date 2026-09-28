"""
intelligence/market_families.py — Market-family leader→member propagation
engine (zero-I/O brain, 2026-09-26 Governor doctrine).

Doctrine (Governor, locked 2026-09-26): "The moment we understand market
families and price drifts we can calculate what price move we need and what
we don't. Spot that metals are rising and send limit orders anticipating the
flip." Families move together; the LEADER prints first and the members
follow on a minutes-scale lag. The lag is the edge: a member that has NOT
yet moved ≥ 50% of the leader's move in the same direction is an
anticipation candidate (limit order ahead of the propagation); a member
that already moved is skipped — the propagation already happened.

Sibling modules: intelligence/relative_strength.py ASSET_CATEGORIES (hours-
days regime taxonomy) and intelligence/cross_asset_lag.py (pairwise
equity→crypto lag, hours-days windows). THIS engine is MINUTES scale and
N×N dense within a family — leader event → every member gets an ETA from
the lag matrix.

Department shape (docs/DEPARTMENT_TEMPLATE.md): all market reads arrive as
arguments (injected candles/ticks via update_price); decisions leave as
frozen verdict dataclasses. main.py owns the I/O and the splice.

Kill switch: config family_engine_enabled=False → on_leader_move returns
None and propagation_targets returns [] — the pre-module system bit-for-bit.
State still updates (flipping the switch mid-run loses nothing).

Telemetry events (emitted by the coordinator at the splice, fields carried
on the verdicts): family_leader_move, family_propagation_signal.

V2 FAMILY SCHEMA (below, 2026-09-26 Governor doctrine): seven signal-plane
families (whale/ETF/anchor-propagation taxonomies) carried as PURE DATA
tables with pure lookup functions. ADVISORY ONLY: every leverage / hold /
cage field in the V2 campaign_setup / scalp_setup profiles is swing-stack
candidate metadata. They NEVER override the fast-cycle pool's 15-38x cage
doctrine, the $100 final-notional floor, or the fee floor — no live code
path consumes them yet; consumption is a future Governor-gated build.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple


# ── Family map (module constant; overridable via cfg.family_map) ─────────────

FAMILIES: Dict[str, Dict] = {
    "crypto_majors": {
        "leader": "BTC-USD",
        "members": ["ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD"],
        "lag_minutes": {"ETH-USD": 3, "SOL-USD": 5, "XRP-USD": 8, "DOGE-USD": 10},
    },
    "crypto_beta": {
        "leader": "ETH-USD",
        "members": ["AVAX-USD", "LINK-USD", "NEAR-USD", "UNI-USD"],
        "lag_minutes": {"AVAX-USD": 5, "LINK-USD": 7, "NEAR-USD": 10, "UNI-USD": 12},
    },
    "tech_equity": {
        "leader": "USTECH100-USD",
        "members": ["AMD-USD", "NVDA-USD", "MSFT-USD", "AAPL-USD"],
        "lag_minutes": {"AMD-USD": 1, "NVDA-USD": 2, "MSFT-USD": 3, "AAPL-USD": 4},
    },
    "broad_index": {
        "leader": "US500-USD",
        "members": ["USTECH100-USD"],
        "lag_minutes": {"USTECH100-USD": 2},
    },
    "metals": {
        "leader": "XAUT-USD",
        "members": ["SILVER-USD", "COPPER-USD"],
        "lag_minutes": {"SILVER-USD": 2, "COPPER-USD": 15},
    },
    "energy": {
        "leader": "CL-USD",
        "members": [],
        "lag_minutes": {},
    },
}

_RETURN_RING_MINUTES = 30        # bounded 1m-return ring depth per symbol
_CONVICTION_CAP = 2.0


def family_map(cfg) -> Dict[str, Dict]:
    """cfg.family_map override wins; module FAMILIES is the default."""
    override = getattr(cfg, "family_map", None)
    if isinstance(override, dict) and override:
        return override
    return FAMILIES


def _f(x) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:
        return None
    return v


# ── Verdict dataclasses (frozen) ─────────────────────────────────────────────

@dataclass(frozen=True)
class PropagationSignal:
    """A leader move that cleared the arming threshold. eta_ts per member is
    carried in member_etas (member → epoch ts the propagation is expected)."""
    family: str
    leader: str
    direction: str                 # "up" | "down"
    leader_move_pct: float         # signed, percent scale (0.4 = 0.4%)
    window_minutes: int
    conviction: float              # leader_move_pct / 0.4, capped 2.0
    ts: float
    member_etas: Tuple[Tuple[str, float], ...] = ()


@dataclass(frozen=True)
class PropagationTarget:
    """A member that has NOT yet propagated — the anticipation candidate."""
    symbol: str
    family: str
    direction: str                 # leader's direction — members follow
    eta_ts: float
    lag_minutes: int
    member_move_pct: float         # member's own move so far (signed %)
    remaining_pct: float           # leader move − |member moved in direction|


# ── Pure helper ───────────────────────────────────────────────────────────────

def required_move(symbol, target_roe_pct, leverage) -> Optional[float]:
    """"What price move we need": move% = target_roe / leverage.
    Pure. None on degenerate leverage/inputs (fail-closed, never divide
    by zero). Percent scale in, percent scale out."""
    roe, lev = _f(target_roe_pct), _f(leverage)
    if roe is None or lev is None or lev <= 0:
        return None
    return roe / lev


# ── Engine ────────────────────────────────────────────────────────────────────

class FamilyEngine:
    """Rolling 1-minute return rings per symbol (bounded deques, injected
    prices). on_leader_move detects an arming leader move over a window;
    propagation_targets selects members still behind the move."""

    def __init__(self, clock=time.time, ring_minutes: int = _RETURN_RING_MINUTES):
        self._clock = clock
        self._ring_minutes = max(int(ring_minutes), 2)
        # symbol -> deque[(ts, price)] — price ticks; returns derive on read
        self._prices: Dict[str, Deque[Tuple[float, float]]] = {}

    # ── ingest ───────────────────────────────────────────────────────────

    def update_price(self, symbol: str, price: float, ts: float) -> None:
        """Inject a tick/candle close. Garbage fails closed (no state)."""
        p, t = _f(price), _f(ts)
        if not symbol or p is None or p <= 0 or t is None:
            return
        dq = self._prices.setdefault(symbol, deque())
        dq.append((t, p))
        cutoff = t - self._ring_minutes * 60.0
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    # ── measurement ──────────────────────────────────────────────────────

    def move_pct(self, symbol: str, window_minutes: int,
                 now_ts: Optional[float] = None) -> Optional[float]:
        """Signed percent move of `symbol` over the last `window_minutes`.
        None when the window has no anchor observation (abstain, no fake
        precision). Anchor = newest observation at/before now−window; the
        current price is the newest observation."""
        w = _f(window_minutes)
        if w is None or w <= 0:
            return None
        n = _f(now_ts) if now_ts is not None else _f(self._clock())
        if n is None:
            return None
        dq = self._prices.get(symbol)
        if not dq:
            return None
        cutoff = n - w * 60.0
        anchor: Optional[float] = None
        for t, p in dq:
            if t <= cutoff:
                anchor = p
            else:
                break
        if anchor is None:
            # no observation at/before the window start — fall back to the
            # oldest observation we DO have if it is inside the window
            t0, p0 = dq[0]
            if t0 > cutoff:
                anchor = p0
            else:
                return None
        cur = dq[-1][1]
        if anchor <= 0:
            return None
        return (cur / anchor - 1.0) * 100.0

    # ── signals ──────────────────────────────────────────────────────────

    def on_leader_move(self, cfg, *, family: str, leader_move_pct: float,
                       window_minutes: int, now_ts: float) -> Optional[PropagationSignal]:
        """Leader |move| ≥ family_min_leader_move_pct (default 0.4%) inside
        the window → PropagationSignal with per-member ETAs from the lag
        matrix. conviction = |leader_move_pct| / 0.4 capped at 2.0.
        Kill switch off / unknown family / sub-threshold → None."""
        if not getattr(cfg, "family_engine_enabled", False):
            return None
        mv, t = _f(leader_move_pct), _f(now_ts)
        w = _f(window_minutes)
        if mv is None or t is None or w is None or w <= 0:
            return None
        fmap = family_map(cfg)
        spec = fmap.get(family)
        if not spec:
            return None
        leader = spec.get("leader")
        if not leader:
            return None
        min_move = _f(getattr(cfg, "family_min_leader_move_pct", 0.4))
        if min_move is None or min_move <= 0:
            min_move = 0.4
        if abs(mv) < min_move:
            return None
        direction = "up" if mv > 0 else "down"
        conviction = min(_CONVICTION_CAP, abs(mv) / min_move)
        lag = spec.get("lag_minutes") or {}
        etas = tuple(
            (m, t + _f(lag.get(m, 0)) * 60.0)
            for m in (spec.get("members") or [])
        )
        return PropagationSignal(
            family=family, leader=leader, direction=direction,
            leader_move_pct=mv, window_minutes=int(w),
            conviction=conviction, ts=t, member_etas=etas,
        )

    def propagation_targets(self, cfg, *, signal: PropagationSignal,
                            member_states: Dict[str, float]) -> List[PropagationTarget]:
        """Members that have NOT yet moved ≥ family_member_moved_frac
        (default 0.5) of the leader's move in the same direction = the
        anticipation candidates. Members already moved are skipped (the
        propagation already happened). member_states: symbol → signed
        move_pct over the same window. Missing member state = abstain that
        member (never fabricate)."""
        if not getattr(cfg, "family_engine_enabled", False):
            return []
        if signal is None:
            return []
        frac = _f(getattr(cfg, "family_member_moved_frac", 0.5))
        if frac is None or frac <= 0:
            frac = 0.5
        spec = family_map(cfg).get(signal.family) or {}
        lag = spec.get("lag_minutes") or {}
        eta_by_member = dict(signal.member_etas)
        out: List[PropagationTarget] = []
        for member in (spec.get("members") or []):
            raw = (member_states or {}).get(member)
            mm = _f(raw)
            if mm is None:
                continue                     # thin data — abstain this member
            # directional progress toward the leader's move
            if signal.direction == "up":
                progressed = mm
                remaining = signal.leader_move_pct - mm
            else:
                progressed = -mm
                remaining = (-signal.leader_move_pct) - (-mm)
            if progressed >= frac * abs(signal.leader_move_pct):
                continue                     # already propagated — skip
            out.append(PropagationTarget(
                symbol=member, family=signal.family,
                direction=signal.direction,
                eta_ts=eta_by_member.get(member, signal.ts),
                lag_minutes=int(_f(lag.get(member, 0)) or 0),
                member_move_pct=mm,
                remaining_pct=remaining,
            ))
        return out


# ══════════════════════════════════════════════════════════════════════════════
# V2 FAMILY SCHEMA (2026-09-26 Governor doctrine) — pure data + pure lookups.
#
# Seven signal-plane families. All numbers are DEFAULTS the Governor will
# calibrate — data tables, not truth. Zero-I/O: no clock, no network, no
# file reads; everything is module-level data plus pure functions.
#
# ADVISORY-ONLY LAW: campaign_setup / scalp_setup leverage / hold / cage
# fields NEVER override the fast-cycle pool's 15-38x cage doctrine, the
# $100 final-notional floor, or the fee doctrine. They are consumed by
# future swing-stack candidates only — nothing live reads them yet.
# ══════════════════════════════════════════════════════════════════════════════

# MSTR beta-by-NAV-premium ladder (checked top-down, first match wins):
#   premium > 1.5        -> 2.8
#   1.0 <= premium <=1.5 -> 1.8
#   0.7 <= premium < 1.0 -> 1.3
#   premium < 0.7        -> 1.1
_MSTR_NAV_BETA_LADDER: Tuple[Tuple[float, float], ...] = (
    (1.5, 2.8),   # premium strictly above the rung -> this beta
    (1.0, 1.8),
    (0.7, 1.3),
    (float("-inf"), 1.1),
)
_MSTR_NAV_FLOOR_BETA = 1.1

_COIN_BETA_BY_BTC_REGIME = {"btc_bull": 1.6, "btc_neutral": 1.1, "btc_bear": 1.9}
_HOOD_BETA_BY_BTC_REGIME = {"btc_bull": 1.4, "btc_neutral": 0.9, "btc_bear": 1.6}

# ALT_L1 whale tier ladder (ratio >= rung -> tier, top-down):
_ALT_L1_WHALE_TIERS: Tuple[Tuple[float, str], ...] = (
    (4.0, "TIER_1"),
    (3.0, "TIER_2"),
    (2.0, "TIER_3"),
    (1.5, "TIER_4"),
)

# DEFI eth_whale_bonus ladder (whale score >= rung -> bonus multiplier):
_DEFI_ETH_WHALE_BONUS: Tuple[Tuple[float, float], ...] = (
    (2.0, 1.20),
    (1.5, 1.10),
    (1.0, 1.00),
)

FAMILIES_V2: Dict[str, Dict] = {
    "CRYPTO_INSTITUTIONAL": {
        "members": ["BTC", "ETH"],
        "signal_type": "whale_plus_etf",
        "whale_weight": 0.55,
        "etf_weight": 0.45,
        "etf_source": "IBIT_FBTC_ARKB_daily_flow",
        "campaign_setup": {
            "entry_method": "DEEP_LIMIT",
            "spike": 0.012,
            "min_cage_ratio": 3.5,
            "leverage": 15,
            "hold_hours": 24,
        },
        "scalp_setup": {
            "entry_method": "SPREAD_CONFIRM",
            "spike": 0.005,
            "min_cage_ratio": 2.0,
            "leverage": 30,
            "hold_minutes": 15,
        },
    },
    "CRYPTO_LEVERED_PROXY": {
        "members": ["MSTR", "COIN", "HOOD"],
        "signal_type": "btc_beta_navpremium",
        "member_profiles": {
            "MSTR": {
                "beta_by_nav_premium": list(_MSTR_NAV_BETA_LADDER),
                "range_fade": {          # DATA ONLY — advisory, no logic yet
                    "week_high": 171.0,
                    "week_low": 123.0,
                    "fade_top_at": 0.85,
                    "fade_bot_at": 0.15,
                    "cage_ratio": 4.0,
                },
            },
            "COIN": {"beta_by_btc_regime": dict(_COIN_BETA_BY_BTC_REGIME)},
            "HOOD": {"beta_by_btc_regime": dict(_HOOD_BETA_BY_BTC_REGIME)},
        },
        "campaign_setup": {
            "entry_method": "RANGE_FADE",
            "leverage": 10,
            "hold_hours": 48,
        },
        "scalp_setup": {
            "entry_method": "BTC_LAG",
            "leverage": 15,
            "hold_minutes": 30,
        },
    },
    "SEMICONDUCTOR": {
        "members": ["NVDA", "AMD"],
        # SOX is NOT venue-listed. The on-venue semis cluster
        # SAMSUNG / SKHY / DRAM is the standing SOX proxy — they are family
        # members tagged proxy_for="SOX".
        "anchor": "SOX",
        "proxy_members": {
            "SAMSUNG": {"proxy_for": "SOX"},
            "SKHY": {"proxy_for": "SOX"},
            "DRAM": {"proxy_for": "SOX"},
        },
        "signal_type": "sox_plus_earnings_plus_etfflow",
        "member_profiles": {
            "NVDA": {
                "sox_beta": 0.60,
                "earnings_catalyst": 3.50,
                "ai_capex_news": 2.50,
                "nasdaq_beta": 0.85,
                "analyst_upgrade": 1.20,
                "compression_bonus": 1.00,
                "soxl_etf_modifier": 1.15,
            },
            "AMD": {
                "sox_beta": 1.15,
                "earnings_catalyst": 2.20,
                "ai_capex_news": 1.20,
                "nasdaq_beta": 1.40,
                "analyst_upgrade": 2.80,
                "compression_bonus": 1.25,
                "soxl_etf_modifier": 1.30,
                "soxs_contrarian": 1.15,
            },
        },
        "campaign_setup": {
            "entry_method": "POST_CATALYST_DIP",
            "spike": 0.008,
            "min_cage_ratio": 3.0,
            "leverage": 3,
            "hold_hours": 168,            # 7 days
        },
        "scalp_setup": {
            "entry_method": "SOX_LAG",
            "leverage": 5,
            "hold_minutes": 60,
            "session_gate": "13:30-20:00 UTC",
        },
    },
    "ALT_L1": {
        "members": ["SOL", "SUI", "AVAX", "TIA"],
        "signal_type": "pure_whale",
        # Whale signal is complete for this family — no ETF dilution/boost.
        "etf_modifier": 0,
        "whale_tiers": list(_ALT_L1_WHALE_TIERS),
        "propagation": {
            "BTC_ETF": {"lag_hours": 12, "multiplier": 1.20},
            "SOL": {
                "SUI": {"multiplier": 0.75, "lag_h": 6},
                "AVAX": {"multiplier": 0.65, "lag_h": 8},
                "TIA": {"multiplier": 0.55, "lag_h": 10},
            },
        },
        "cascade_risk": {
            "etf_outflow_trigger": True,
            "narrative_day_gate": 5,
            "max_funding_for_long": 0.0008,
        },
        "campaign_setup": {
            "entry_method": "SPIKE_HUNT",
            "min_cage_ratio": 4.0,
            "leverage": 15,
            "hold_hours": 48,
        },
        "scalp_setup": {
            "entry_method": "FUNDING_FLIP",
            "leverage": 20,
            "hold_minutes": 30,
        },
    },
    "DEFI": {
        "members": ["UNI", "AAVE", "SNX", "PENDLE"],
        "anchor": "ETH",
        "signal_type": "eth_propagation_plus_whale",
        "propagation": {
            "ETH": {
                "UNI": {"multiplier": 1.40, "lag_h": 3},
                "AAVE": {"multiplier": 1.30, "lag_h": 4},
                "SNX": {"multiplier": 1.60, "lag_h": 5},
                "PENDLE": {"multiplier": 1.20, "lag_h": 6},
            },
        },
        "eth_whale_bonus": list(_DEFI_ETH_WHALE_BONUS),
        "campaign_setup": {
            "entry_method": "ETH_CONFIRM_THEN_ENTER",
            "min_cage_ratio": 3.5,
            "leverage": 12,
            "hold_hours": 36,
        },
        "scalp_setup": {
            "entry_method": "ETH_MOMENTUM",
            "leverage": 18,
            "hold_minutes": 45,
        },
    },
    "L2_BRIDGE": {
        "members": ["ARB", "OP", "ZRO"],
        "anchor": "ETH",
        "signal_type": "eth_propagation_plus_whale",
        "propagation": {
            "ETH": {
                "ARB": {"multiplier": 1.25, "lag_h": 2},
                "OP": {"multiplier": 1.20, "lag_h": 2},
                "ZRO": {"multiplier": 1.35, "lag_h": 3},
            },
        },
    },
    "AI_CRYPTO": {
        "members": ["FET", "RENDER", "TAO"],
        "anchors": ["NVDA", "ETH"],
        "signal_type": "dual_anchor_plus_whale",
        "propagation": {
            "NVDA": {
                "FET": {"multiplier": 0.60, "lag_h": 6},
                "RENDER": {"multiplier": 0.55, "lag_h": 7},
                "TAO": {"multiplier": 0.45, "lag_h": 8},
            },
            "ETH": {
                "FET": {"multiplier": 0.40, "lag_h": 3},
                "RENDER": {"multiplier": 0.35, "lag_h": 4},
            },
        },
        "combined_anchor_boost": 1.35,
        "campaign_setup": {
            "min_cage_ratio": 4.0,
            "leverage": 15,
            "hold_hours": 48,
        },
    },
}


def _norm_symbol(symbol) -> Optional[str]:
    """Normalize any venue-style symbol to its base ticker: BTC-USD -> BTC,
    btc -> BTC, amd-usd -> AMD, ETH_USDT -> ETH. None on garbage."""
    if not isinstance(symbol, str):
        return None
    s = symbol.strip().upper()
    if not s:
        return None
    for sep in ("-", "_", "/"):
        if sep in s:
            s = s.split(sep, 1)[0]
    return s or None


def _v2_spec(family_name) -> Optional[Dict]:
    if not isinstance(family_name, str):
        return None
    return FAMILIES_V2.get(family_name.strip().upper())


def _v2_member_family(base: str) -> Optional[str]:
    for name, spec in FAMILIES_V2.items():
        if base in (spec.get("members") or []):
            return name
        if base in (spec.get("proxy_members") or {}):
            return name
    return None


# ── V2 lookup API (pure; None / empty on unknown) ────────────────────────────

def family_v2(symbol) -> Optional[str]:
    """symbol -> V2 family name (normalizes BTC-USD/btc -> BTC)."""
    base = _norm_symbol(symbol)
    if base is None:
        return None
    return _v2_member_family(base)


def member_profile(symbol) -> Optional[Dict]:
    """Per-member beta/bonus profile where defined (NVDA, AMD, MSTR, COIN,
    HOOD) plus semis proxy tags (SAMSUNG/SKHY/DRAM -> {proxy_for: SOX})."""
    base = _norm_symbol(symbol)
    if base is None:
        return None
    fam = _v2_member_family(base)
    if fam is None:
        return None
    spec = FAMILIES_V2[fam]
    prof = (spec.get("member_profiles") or {}).get(base)
    if prof is not None:
        return dict(prof)
    proxy = (spec.get("proxy_members") or {}).get(base)
    if proxy is not None:
        return dict(proxy)
    return None


def propagation_table(family_name, anchor) -> Optional[Dict]:
    """(family, anchor) -> member -> {multiplier, lag_h} propagation table.
    E.g. ("DEFI", "ETH") -> the DEFI ETH-propagation table; ("ALT_L1", "SOL")
    -> the SOL-propagation table; ("ALT_L1", "BTC_ETF") -> the ETF lag spec."""
    spec = _v2_spec(family_name)
    if spec is None or not isinstance(anchor, str):
        return None
    table = (spec.get("propagation") or {}).get(anchor.strip().upper())
    if table is None:
        return None
    return dict(table)


def signal_type(symbol) -> Optional[str]:
    """symbol -> its V2 family's signal_type string."""
    fam = family_v2(symbol)
    if fam is None:
        return None
    return FAMILIES_V2[fam].get("signal_type")


def whale_tier(symbol, whale_ratio) -> Optional[str]:
    """ALT_L1 members only: whale ratio -> tier name (4.0->TIER_1 ...
    1.5->TIER_4). Below the lowest rung or outside ALT_L1 -> None."""
    base = _norm_symbol(symbol)
    if base is None or _v2_member_family(base) != "ALT_L1":
        return None
    r = _f(whale_ratio)
    if r is None:
        return None
    for rung, tier in _ALT_L1_WHALE_TIERS:
        if r >= rung:
            return tier
    return None


def levered_proxy_beta(symbol, *, nav_premium=None, btc_regime=None) -> Optional[float]:
    """CRYPTO_LEVERED_PROXY beta resolution. MSTR: NAV-premium ladder
    (premium 1.5 exactly -> 1.8 rung; 0.7 exactly -> 1.3). COIN/HOOD:
    btc_bull / btc_neutral / btc_bear regime table. Unknown inputs -> None."""
    base = _norm_symbol(symbol)
    if base == "MSTR":
        p = _f(nav_premium)
        if p is None:
            return None
        if p > 1.5:
            return 2.8
        if p >= 1.0:
            return 1.8
        if p >= 0.7:
            return 1.3
        return _MSTR_NAV_FLOOR_BETA
    if base in ("COIN", "HOOD"):
        if not isinstance(btc_regime, str):
            return None
        table = _COIN_BETA_BY_BTC_REGIME if base == "COIN" else _HOOD_BETA_BY_BTC_REGIME
        return table.get(btc_regime.strip().lower())
    return None
