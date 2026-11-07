"""intelligence/prop_trend_bridge.py — prop→SoDEX trend-sniper bridge brain.

Governor directive 2026-11-07 ("the scalper may not need to fight the trend
it can ride it", "snipe with large leverage or margin to force at least $5
continuously", "same weakness across similar market families, enter at cheap
prices"): the prop sleeves (demo crypto + Turbo tradfi) are the infantry —
their fills PROVE a trend is active. This brain reads the harness's
geometry-only position snapshots (prop_positions.json per state root,
15s cadence), maps the pullback band each leg is oscillating in from ARIA's
OWN candles since the leg opened, and emits SniperArm verdicts with
target-forcing geometry for the SoDEX scalper lane.

War map: prop leg holds the ground; the sniper fires when price retraces
INTO the mapped band top (cheap entry for the trend direction), exits at
the band bottom, re-arms while the leg lives. The leg dying (or the
snapshot going stale) is the retreat signal — arms carry armed_until and
the integrator never extends them.

Zero-I/O department shape (docs/DEPARTMENT_TEMPLATE.md):
  - Constructor takes cfg only. Every read — snapshots, marks, bars,
    family membership, time — arrives as a tick() argument. The brain
    never touches a file, socket, clock, or env var.
  - Kill switch: prop_trend_bridge_enabled (getattr-default True = the
    measurement path; the LIVE order gate sits at the integrator, this
    module emits intents never orders).
  - Fail-closed: dark marks / no bars / thin bands → standdown row, never
    an arm. Internal fault → ([], []) and the hot path never feels it.

Economics (measured live 2026-11-07 on the BCH/ZEC shorts): bands run
0.3-0.6% wide with 20-40 touches per level over hours. Target-forcing:
notional = PROFIT_TARGET / (band_pct - round-trip fees) — a 0.5% band
needs ~$1,220 notional (~$81 margin at 15x) to NET $5 after 9bps of
taker fees. Bands too thin to pay the target inside the margin cap
stand down (reason=band_too_thin) — the $5 is the point, not the trade.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

SCHEMA_VERSION = 1

# Arm lifecycle (the integrator writes param_store TTLs from armed_until).
SNAPSHOT_FRESH_S = 120.0     # harness publishes every 15s; 8 misses = dead
ARM_TTL_S = 120.0            # arms die 2min past the last fresh snapshot

# Trend confirmation: the prop leg must be winning to prove the trend.
MIN_R_CONFIRM = 1.0          # uPnL >= +1R (Governor: arm at +1R)

# Band mapping from ARIA's own candles since the leg opened.
BAND_BIN_PCT = 0.0015        # histogram bin width (0.15%)
MIN_TOUCHES = 6              # a mapped level needs evidence, not one visit
BAND_EDGE_PCT = 0.20         # top/bottom percentile band of the touch map
MIN_BAND_PCT = 0.0020        # below this the band can't pay fees — no arm

# Sniper economics (Governor 2026-11-07: force >= $5 continuously).
PROFIT_TARGET_USD = 5.0
TAKER_FEE_RATE = 0.00045     # per side, measured on the venue
DEFAULT_LEVERAGE = 15        # fast-cycle class; knob may raise to cap
MAX_LEVERAGE = 20
DEFAULT_MARGIN_CAP_USD = 70.0
RISK_CLAMP_USD = 8.0         # the scalper lane's per-trade risk clamp
STOP_BUFFER_FRAC = 0.25      # stop sits 0.25x band width past the zone
MIN_STOP_PCT = 0.003         # 0.3% stop floor — sniper tight, not dust

# Family propagation: >=2 same-direction prop legs in one family confirm a
# complex-wide weakness; lagging members arm from their OWN bands at half
# margin (derived evidence, not a primary fill).
FAMILY_MIN_LEGS = 2
FAMILY_MARGIN_MULT = 0.5


def _f(x) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:
        return None
    return v


@dataclass(frozen=True)
class SniperArm:
    """One armed sniper verdict. Frozen: an arm is a fact about a tick."""
    symbol: str                 # ARIA symbol, e.g. "BCH-USD"
    direction: str              # "long" | "short"
    source: str                 # "prop_leg" | "family"
    zone_top: float             # expensive edge of the band (short entry)
    zone_bottom: float          # cheap edge (short target)
    stop_price: float
    target_price: float
    notional_usd: float
    margin_usd: float
    leverage: int
    risk_usd: float
    band_pct: float
    touches: int
    r_multiple: float           # the prop leg's live uPnL-R at arm time
    family_size: int            # same-direction prop legs in the family
    armed_until: float


def compute_band(bars: Sequence[Sequence[float]], direction: str,
                 mark: float) -> Optional[Tuple[float, float, int]]:
    """Map the pullback band from (ts,o,h,l,c) bars since the leg opened.

    Touch histogram at BAND_BIN_PCT resolution; zone edges = the BAND_EDGE
    percentile of weighted touches; touches = count at the denser edge.
    For a short the entry zone is the EXPENSIVE edge (zone_top), the
    target the cheap edge (zone_bottom) — mirrored for longs.
    None when the map is too thin to trust (fail-closed).
    """
    if direction not in ("long", "short"):
        return None
    mk = _f(mark)
    if mk is None or mk <= 0 or not bars:
        return None
    step = mk * BAND_BIN_PCT
    if step <= 0:
        return None
    bins: Dict[int, int] = {}
    for bar in bars:
        try:
            _, o, h, l, c = bar
            for px in (o, h, l, c):
                v = _f(px)
                if v is not None and v > 0:
                    b = round(v / step)
                    bins[b] = bins.get(b, 0) + 1
        except (TypeError, ValueError):
            continue
    if len(bins) < 2:
        return None
    total = sum(bins.values())
    if total < MIN_TOUCHES * 2:
        return None
    # edge percentiles of the touch-weighted distribution
    ordered = sorted(bins.items())
    lo_cut = total * (BAND_EDGE_PCT / 100.0)
    hi_cut = total * (1.0 - BAND_EDGE_PCT / 100.0)
    acc, lo_bin, hi_bin = 0, ordered[0][0], ordered[-1][0]
    for b, n in ordered:
        acc += n
        if acc <= lo_cut:
            lo_bin = b
        if acc >= hi_cut:
            hi_bin = b
            break
    zone_bottom, zone_top = lo_bin * step, hi_bin * step
    if zone_top <= zone_bottom:
        return None
    band_pct = (zone_top - zone_bottom) / mk
    if band_pct < MIN_BAND_PCT:
        return None
    touches = max(bins.get(round(zone_top / step), 0),
                  bins.get(round(zone_bottom / step), 0))
    if touches < MIN_TOUCHES:
        return None
    return zone_top, zone_bottom, touches


def sniper_geometry(direction: str, zone_top: float, zone_bottom: float,
                    mark: float, *, leverage: int, margin_cap: float,
                    target_usd: float = PROFIT_TARGET_USD
                    ) -> Optional[dict]:
    """Target-forcing geometry. notional = target / (band - fees); stands
    down (None) when the band can't net the target inside the margin cap
    or the risk clamp. For a short: entry at zone_top, target zone_bottom,
    stop buffer above. Mirrored for longs."""
    if direction not in ("long", "short"):
        return None
    mk = _f(mark)
    top, bot = _f(zone_top), _f(zone_bottom)
    if mk is None or top is None or bot is None or top <= bot or mk <= 0:
        return None
    band_pct = (top - bot) / mk
    net_edge = band_pct - 2.0 * TAKER_FEE_RATE
    if net_edge <= 0:
        return None
    lev = max(1, min(MAX_LEVERAGE, int(leverage)))
    notional = target_usd / net_edge
    margin = notional / lev
    if margin > margin_cap:
        return None  # band_too_thin vs capital — stand down, don't shrink
    stop_pct = max(MIN_STOP_PCT, STOP_BUFFER_FRAC * band_pct)
    risk = notional * stop_pct
    if risk > RISK_CLAMP_USD:
        # Provably unresolvable: risk/target ratio is fixed by the band
        # geometry, so shrinking notional to the clamp always nets less
        # than the target. Stand down like the margin cap — never shrink.
        return None
    if direction == "short":
        entry_zone = top
        stop = top * (1.0 + stop_pct)
        target = bot
    else:
        entry_zone = bot
        stop = bot * (1.0 - stop_pct)
        target = top
    return {
        "entry_zone": entry_zone, "stop_price": stop, "target_price": target,
        "notional_usd": notional, "margin_usd": margin, "leverage": lev,
        "risk_usd": risk, "band_pct": band_pct,
    }


class PropTrendBridge:
    """The brain. cfg via getattr-with-default; zero state beyond the last
    tick's journal rows."""

    def __init__(self, cfg) -> None:
        self._cfg = cfg
        self._last_rows: List[dict] = []

    def journal_rows(self) -> List[dict]:
        return [dict(r) for r in self._last_rows]

    def tick(self, *, snapshots: Sequence[Mapping],
             mark_fn: Callable[[str], Optional[float]],
             bars_fn: Callable[[str, float], Sequence[Sequence[float]]],
             family_fn: Callable[[str], Sequence[str]],
             symbol_map: Mapping[str, str],
             now: float) -> List[SniperArm]:
        """Grade every open prop leg (+ family extensions) into arms.
        family_fn(symbol) → the symbol's family members (ARIA symbols);
        two legs sharing a member list are one family. Never raises:
        kill-switch off → []; fault → [] with the rows from the last good
        tick left intact."""
        try:
            if not bool(getattr(self._cfg, "prop_trend_bridge_enabled", True)):
                return []
            ts = _f(now)
            if ts is None or ts <= 0:
                return []
            leverage = int(getattr(self._cfg, "prop_sniper_leverage",
                                   DEFAULT_LEVERAGE) or DEFAULT_LEVERAGE)
            margin_cap = _f(getattr(self._cfg, "prop_sniper_margin_cap_usd",
                                    DEFAULT_MARGIN_CAP_USD))
            if margin_cap is None or margin_cap <= 0:
                margin_cap = DEFAULT_MARGIN_CAP_USD

            rows: List[dict] = []
            arms: List[SniperArm] = []
            legs = self._legs(snapshots, symbol_map, ts, rows)

            confirmed: List[dict] = []
            for leg in legs:
                arm = self._arm_leg(leg, mark_fn, bars_fn, ts,
                                    leverage, margin_cap, rows,
                                    source="prop_leg", family_size=1,
                                    margin_mult=1.0)
                if arm is not None:
                    arms.append(arm)
                    confirmed.append(leg)

            # Family propagation: legs whose family_fn member lists agree
            # are one family; >=2 same-direction legs arm the laggards at
            # their own bands, half margin.
            fam_groups: Dict[Tuple, List[dict]] = {}
            for leg in confirmed:
                members = tuple(sorted(
                    str(m) for m in (family_fn(leg["symbol"]) or ())))
                key = (members or (leg["symbol"],), leg["direction"])
                fam_groups.setdefault(key, []).append(leg)
            armed_syms = {a.symbol for a in arms}
            for (members, direction), legs_in in fam_groups.items():
                if len(legs_in) < FAMILY_MIN_LEGS:
                    continue
                oldest = min(l["opened_ts"] for l in legs_in)
                n = len(legs_in)
                for member in members:
                    if not member or member in armed_syms:
                        continue
                    leg = {"symbol": member, "direction": direction,
                           "opened_ts": oldest}
                    arm = self._arm_leg(leg, mark_fn, bars_fn, ts,
                                        leverage, margin_cap, rows,
                                        source="family", family_size=n,
                                        margin_mult=FAMILY_MARGIN_MULT)
                    if arm is not None:
                        arms.append(arm)
                        armed_syms.add(member)

            self._last_rows = rows
            return arms
        except Exception:
            return []

    # ── internals ──────────────────────────────────────────────────────

    def _legs(self, snapshots, symbol_map, ts, rows) -> List[dict]:
        """Flatten snapshots into leg rows with freshness + symbol mapping."""
        legs: List[dict] = []
        for snap in snapshots or ():
            if not isinstance(snap, Mapping):
                continue
            age = ts - (_f(snap.get("ts")) or 0.0)
            sleeve = snap.get("sleeve", "")
            if age > SNAPSHOT_FRESH_S:
                rows.append({"type": "prop_sniper_standdown",
                             "reason": "stale_snapshot", "sleeve": sleeve,
                             "age_s": round(age, 1)})
                continue
            for pos in snap.get("positions") or ():
                if not isinstance(pos, Mapping):
                    continue
                asset = str(pos.get("asset") or "")
                sym = symbol_map.get(asset)
                if not sym:
                    rows.append({"type": "prop_sniper_standdown",
                                 "reason": "unmapped_asset", "asset": asset})
                    continue
                side = pos.get("side")
                direction = {"buy": "long", "sell": "short"}.get(side)
                if direction is None:
                    rows.append({"type": "prop_sniper_standdown",
                                 "reason": "bad_side", "asset": asset})
                    continue
                legs.append({
                    "symbol": sym, "asset": asset, "direction": direction,
                    "entry_ref": _f(pos.get("entry_ref")),
                    "stop_price": _f(pos.get("stop_price")),
                    "opened_ts": _f(pos.get("opened_ts")) or ts,
                    "sleeve": sleeve,
                })
        return legs

    def _arm_leg(self, leg, mark_fn, bars_fn, ts, leverage, margin_cap,
                 rows, *, source, family_size, margin_mult) -> Optional[SniperArm]:
        sym, direction = leg["symbol"], leg["direction"]
        stand = lambda reason, **kw: rows.append({
            "type": "prop_sniper_standdown", "symbol": sym,
            "direction": direction, "source": source,
            "reason": reason, **kw}) or None

        mark = _f(mark_fn(sym))
        if mark is None or mark <= 0:
            return stand("mark_dark")

        # Trend confirmation: prop legs need +1R; family extensions are
        # already confirmed by the family's legs (r gate skipped).
        r_mult = 0.0
        entry, stop = leg.get("entry_ref"), leg.get("stop_price")
        if source == "prop_leg":
            if not entry or not stop or entry == stop:
                return stand("bad_geometry")
            risk_px = abs(entry - stop)
            move = ((entry - mark) if direction == "short"
                    else (mark - entry))
            r_mult = move / risk_px
            if r_mult < MIN_R_CONFIRM:
                return stand("below_r_confirm", r=round(r_mult, 3))

        bars = bars_fn(sym, leg["opened_ts"]) or ()
        band = compute_band(bars, direction, mark)
        if band is None:
            return stand("band_unmapped")
        zone_top, zone_bottom, touches = band

        geo = sniper_geometry(direction, zone_top, zone_bottom, mark,
                              leverage=leverage,
                              margin_cap=margin_cap * margin_mult)
        if geo is None:
            return stand("band_too_thin",
                         band_pct=round((zone_top - zone_bottom) / mark, 5))

        rows.append({
            "type": "prop_sniper_armed", "symbol": sym,
            "direction": direction, "source": source,
            "zone": [round(zone_bottom, 6), round(zone_top, 6)],
            "target": round(geo["target_price"], 6),
            "stop": round(geo["stop_price"], 6),
            "notional": round(geo["notional_usd"], 2),
            "margin": round(geo["margin_usd"], 2),
            "leverage": geo["leverage"], "risk": round(geo["risk_usd"], 2),
            "band_pct": round(geo["band_pct"], 5), "touches": touches,
            "r": round(r_mult, 3), "family_size": family_size,
        })
        return SniperArm(
            symbol=sym, direction=direction, source=source,
            zone_top=zone_top, zone_bottom=zone_bottom,
            stop_price=geo["stop_price"], target_price=geo["target_price"],
            notional_usd=geo["notional_usd"], margin_usd=geo["margin_usd"],
            leverage=geo["leverage"], risk_usd=geo["risk_usd"],
            band_pct=geo["band_pct"], touches=touches,
            r_multiple=r_mult, family_size=family_size,
            armed_until=ts + ARM_TTL_S)
