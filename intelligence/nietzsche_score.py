"""intelligence/nietzsche_score.py — Nietzsche conviction composite (P4).

Governor 2026-09-29 ("PHILOSOPHY CAN ALSO WORK HERE KANT AND NEITSCHE FOR
BOTH SIZING AND STRUCTURE" + "ALSO BUIILD P4"): after Kant approves the
structure, Nietzsche asks "how convicted am I?" — a weighted composite of
SIX external evidence planes drives the size scalar:

    N1 whale long/short ratio   w 0.25  (crowd positioning, Bybit accounts)
    N2 OI trend                 w 0.15  (% growth newest vs oldest in window)
    N3 funding crowding         w 0.20  (crowded funding kills chase entries)
    N4 Fear & Greed             w 0.15  (contrarian scalar)
    N5 SoDEX macro              w 0.15  (USTECH100 + MAG7SSI day moves)
    N6 ETF flow (SoSoValue)     w 0.10  (3d institutional flow, USD)

Design rulings (extraction amendments, Governor-endorsed 2026-09-29):

  * FIXED DENOMINATOR Σw = 1.0. A missing input contributes ZERO — the
    score is "conviction proven so far", never renormalized upward. This
    reproduces the Governor's worked examples exactly (ETH at market with
    N3 zeroed by crowded funding = 0.25+0.09+0+0.06 = 0.40 → half size).
    Fail direction on a dark plane is DOWN toward standdown — smaller or
    no trades, never phantom conviction.

  * DIRECTION MIRRORING for shorts: N1 reads 1/ratio (crowd long = bad
    short... inverted: crowd short = conviction for OUR short), N3 reads
    −rate (positive funding = longs pay = crowded longs = good short),
    N4 reads 100−fg, N5/N6 mirror their tables.

  * CROWDING FAMILY CAP: N1+N3+N4 (Σw 0.60) are three reads of the SAME
    thing — crowd positioning. When all three are present AND agree (all
    ≥0.6 or all ≤0.4) their combined weighted contribution shrinks ×0.75
    so one crowding read is never triple-counted.

  * N3 ENTRY-TYPE RULE: a "limit" entry (pullback at structure) floors at
    0.85 — the crowding penalty exists to kill CHASE entries; a limit
    resting at structure IS the disciplined response to crowded funding
    (his ETH $2,657 example: market 0.40 → pullback 0.57+).

  * MAJORS SCOPE: the caller scopes to BTC/ETH/SOL/XAUT. Data-poor alts
    never reach this module (fixed denominator would halve every alt).

Zero-I/O brain (department template): pure functions over injected values.
No network, no files, no logging, no clock. The caller gathers planes.

Score → size scalar ladder (his table):
    < 0.30        standdown (scalar 0.0, veto at the call site)
    [0.30, 0.50)  0.5×
    [0.50, 0.70)  0.75×
    [0.70, 0.85)  1.0×
    ≥ 0.85        1.25×
"""

from __future__ import annotations

WEIGHTS = {"n1": 0.25, "n2": 0.15, "n3": 0.20, "n4": 0.15, "n5": 0.15,
           "n6": 0.10}
CROWDING_FAMILY = ("n1", "n3", "n4")
CROWDING_SHRINK = 0.75
FAMILY_AGREE_HI = 0.6
FAMILY_AGREE_LO = 0.4

STANDDOWN_LT = 0.30
N3_LIMIT_FLOOR = 0.85
N6_MATERIALITY_USD = 150_000_000.0


def _interp(x: float, anchors) -> float:
    """Piecewise-linear interp over (x, y) anchors sorted by x, clamped at
    both ends. Anchors may descend in y (N3 funding)."""
    pts = sorted(anchors)
    if x <= pts[0][0]:
        return float(pts[0][1])
    if x >= pts[-1][0]:
        return float(pts[-1][1])
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return float(y1)
            return float(y0 + (y1 - y0) * (x - x0) / (x1 - x0))
    return float(pts[-1][1])


# ── per-signal scorers (all return 0..1) ─────────────────────────────────────

def n1_whale(ratio, side: str):
    """Whale account long/short ratio. Long anchors: 1.0→0.0, 1.5→0.5,
    2.0→0.75, 2.5+→1.0 (crowd long conviction). Shorts read 1/ratio."""
    try:
        r = float(ratio)
    except (TypeError, ValueError):
        return None
    if r <= 0:
        return None
    eff = r if side == "long" else 1.0 / r
    return round(_interp(eff, [(1.0, 0.0), (1.5, 0.5), (2.0, 0.75),
                               (2.5, 1.0)]), 4)


def n2_oi(growth_pct):
    """OI trend % (newest vs oldest in the feed window). Direction-free —
    rising OI = participation, whichever side we take. Anchors: ≤−3→0.0
    (declining), 3→0.4 (flat band upper edge), 5→0.7, ≥10→1.0."""
    try:
        g = float(growth_pct)
    except (TypeError, ValueError):
        return None
    return round(_interp(g, [(-3.0, 0.0), (3.0, 0.4), (5.0, 0.7),
                             (10.0, 1.0)]), 4)


def n3_funding(rate_pct, side: str, entry_type: str = "market"):
    """Funding crowding, rate in PERCENT per interval (0.0028 = 0.0028%).
    Longs: >0.0075→0.0, 0.004→0.5, 0.001→0.85, ≤0→1.0. Shorts mirror on
    −rate. entry_type "limit" floors at 0.85 — pullback-at-structure is
    exempt from the crowding penalty (his ETH $2,657 example)."""
    try:
        r = float(rate_pct)
    except (TypeError, ValueError):
        return None
    eff = r if side == "long" else -r
    s = _interp(eff, [(0.0, 1.0), (0.001, 0.85), (0.004, 0.5),
                      (0.0075, 0.0)])
    if entry_type == "limit":
        s = max(s, N3_LIMIT_FLOOR)
    return round(s, 4)


def n4_fear_greed(value, side: str):
    """Fear & Greed contrarian scalar. Longs: 0-24→1.0, 25-44→0.8,
    45-54→0.6, 55-74→0.4, 75-89→0.2, 90+→0.0. Shorts read 100−v."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    eff = v if side == "long" else 100.0 - v
    if eff <= 24:
        return 1.0
    if eff <= 44:
        return 0.8
    if eff <= 54:
        return 0.6
    if eff <= 74:
        return 0.4
    if eff <= 89:
        return 0.2
    return 0.0


def n5_macro(tech_day_pct, mag7_day_pct, side: str):
    """SoDEX macro: USTECH100 + MAG7SSI day moves (%). Longs: both>+1→1.0,
    both>0→0.7, mixed→0.4, tech<0→0.2, tech<−1.5→0.0. Shorts mirror."""
    try:
        t = float(tech_day_pct)
        m = float(mag7_day_pct)
    except (TypeError, ValueError):
        return None
    if side == "short":
        t, m = -t, -m
    if t <= -1.5:
        return 0.0
    if t < 0:
        return 0.2
    if t > 1.0 and m > 1.0:
        return 1.0
    if t > 0 and m > 0:
        return 0.7
    return 0.4


def n6_etf_flow(sum_3d_usd, side: str):
    """3d ETF flow (USD, SoSoValue). Longs: ≥+$150M→1.0 (accumulation),
    ≤−$150M→0.0 (distribution), else 0.5 (neutral). Shorts mirror."""
    try:
        f = float(sum_3d_usd)
    except (TypeError, ValueError):
        return None
    eff = f if side == "long" else -f
    if eff >= N6_MATERIALITY_USD:
        return 1.0
    if eff <= -N6_MATERIALITY_USD:
        return 0.0
    return 0.5


def size_scalar(score, standdown_lt: float = STANDDOWN_LT) -> float:
    """His ladder. 0.0 below the standdown threshold (the call site owns
    the veto); 0.5 / 0.75 / 1.0 / 1.25 rungs above."""
    try:
        s = float(score)
    except (TypeError, ValueError):
        return 1.0
    if s < standdown_lt:
        return 0.0
    if s < 0.50:
        return 0.5
    if s < 0.70:
        return 0.75
    if s < 0.85:
        return 1.0
    return 1.25


def conviction_score(side: str, *, whale_ratio=None, oi_growth_pct=None,
                     funding_rate_pct=None, fear_greed=None,
                     tech_day_pct=None, mag7_day_pct=None,
                     etf_flow_3d_usd=None, entry_type: str = "market",
                     crowding_cap: bool = True,
                     standdown_lt: float = STANDDOWN_LT) -> dict:
    """The composite. Fixed denominator Σw = 1.0 — a missing (None) signal
    contributes ZERO, never renormalized. Returns the verdict dict:
    score, size_scalar, standdown, signals {n1..n6}, crowding_capped."""
    side = "short" if str(side).lower() == "short" else "long"
    signals = {
        "n1": n1_whale(whale_ratio, side),
        "n2": n2_oi(oi_growth_pct),
        "n3": n3_funding(funding_rate_pct, side, entry_type),
        "n4": n4_fear_greed(fear_greed, side),
        "n5": n5_macro(tech_day_pct, mag7_day_pct, side),
        "n6": n6_etf_flow(etf_flow_3d_usd, side),
    }
    fam_vals = [signals[k] for k in CROWDING_FAMILY]
    fam_present = all(v is not None for v in fam_vals)
    fam_agree = fam_present and (
        all(v >= FAMILY_AGREE_HI for v in fam_vals)
        or all(v <= FAMILY_AGREE_LO for v in fam_vals))
    capped = bool(crowding_cap and fam_agree)

    score = 0.0
    for k, w in WEIGHTS.items():
        v = signals[k]
        if v is None:
            continue
        contrib = w * v
        if capped and k in CROWDING_FAMILY:
            contrib *= CROWDING_SHRINK
        score += contrib
    score = round(min(1.0, max(0.0, score)), 4)
    return {
        "score": score,
        "size_scalar": size_scalar(score, standdown_lt),
        "standdown": score < standdown_lt,
        "signals": signals,
        "crowding_capped": capped,
        "side": side,
        "entry_type": entry_type,
    }
