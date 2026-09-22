"""HV ring seed + health — repair for the rvr pyramid warmup leg (2026-09-22).

Zero-I/O brain (docs/DEPARTMENT_TEMPLATE.md): no network, no file, no logging
imports; all data injected; the main.py splice owns every side effect.

THE WOUND (verified against live telemetry): the pyramid add path defers on
warmup_frac < 0.80 (intelligence/pyramid.py WARMUP_MIN_FRAC, checked in
add_verdict). Since 6fa9ba1 the counter is venue-scoped to 3 legs —
(fr, rvr, coh)/3 via main.py _pyramid_warmup_legs — and every
pyramid_add_blocked event since shows fr_ok=true, rvr_ok=false,
coh_ok=false: warmup frozen at 0.33, adds structurally dead.

rvr is None because rv_rank (intelligence/breakout_coherence.py:184)
demands >=10 prints in the symbol's _BC_HV_HIST ring, and the ring is fed
ONLY inside on_signal_ready (main.py:8817-8822) — one print per symbol per
>=900s, and ONLY while signals for that symbol flow. A signal drought (or
any exception swallowed by the bare except at main.py:8894) starves the
ring with zero telemetry; 6fa9ba1's persistence only saves prints appended
while it was live, so a fresh boot after a stalled session restores a
handful of prints and the ~2.5h ring warm-up restarts from near zero.

THE REPAIR, two pure halves:

  1. Boot/warmup seeding: the bot already seeds ~55 15m bars per symbol at
     boot before the gather (main.py fetch_historical → historical_complete).
     A sliding SEED_WINDOW_BARS window over those bars yields
     len(bars) - SEED_WINDOW_BARS + 1 HV prints instantly — for 55 bars,
     44 prints, well past the rv_rank floor of 10. seed_ring MERGES the
     synthetic prints with the persisted ring: dedupe by a half-interval
     ts bucket makes re-seeding idempotent (a live print for the same bar
     lands within seconds of the synthetic one's bar-close ts), the merge
     stays sorted, and the RING_CAP keeps the newest prints — identical
     retention semantics to the live append path (main.py:8821-8822).

  2. ring_health: a verdict the pyramid loop (or any supervised check) can
     call to distinguish "seeding" from "stalled" — a ring whose newest
     print is older than STALL_AFTER_S has silently stopped feeding, which
     today is invisible (the feed's failure modes are all swallowed).

The Parkinson estimator is re-implemented locally (8 lines) instead of
importing intelligence.breakout_coherence: that module imports the
kill-switch plane, which does file I/O — importing it here would break
this module's zero-I/O contract. parkinson_hv_bar is semantics-identical
(same formula, same skip rules, same fail-silent None) and the test file
pins parity against the canonical implementation.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence, Tuple

# Knob defaults (main.py splice may override via config; these pin the spec).
SEED_WINDOW_BARS = 12      # bars per HV print — matches the >=12-candle gate
                           # at main.py:8810 and main.py:16003
MIN_RVR_PRINTS = 10        # rv_rank history floor (breakout_coherence.py:196)
RING_CAP = 96              # ring capacity — matches main.py:8821
PRINT_INTERVAL_S = 900.0   # live append spacing — matches main.py:8819
STALL_AFTER_S = 2700.0     # 3 missed prints = stalled feed
PERIODS_PER_YEAR = 35040   # 15m bars/yr — matches both main.py call sites

_DEDUPE_BUCKET_S = PRINT_INTERVAL_S / 2.0

Bar = Tuple[float, float, float, float]          # (ts_s, high, low, close)
Print = Tuple[float, float]                      # (ts_s, hv)


def parkinson_hv_bar(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    periods_per_year: int = PERIODS_PER_YEAR,
) -> Optional[float]:
    """Annualized Parkinson HV — semantics-identical to
    intelligence.breakout_coherence.parkinson_hv (kept local for zero-I/O;
    parity pinned in tests). None on <2 usable bars, non-positive prices,
    or length mismatch."""
    try:
        n = min(len(highs), len(lows), len(closes))
        if n < 2:
            return None
        acc = 0.0
        used = 0
        for i in range(n):
            h, l = float(highs[i]), float(lows[i])
            if h <= 0.0 or l <= 0.0 or h < l:
                continue
            r = math.log(h / l)
            acc += r * r
            used += 1
        if used < 2:
            return None
        var = acc / (4.0 * math.log(2.0) * used)
        return math.sqrt(var * periods_per_year)
    except (TypeError, ValueError, OverflowError):
        return None


def hv_prints_from_bars(
    bars: Sequence[Bar],
    window: int = SEED_WINDOW_BARS,
    periods_per_year: int = PERIODS_PER_YEAR,
) -> list[Print]:
    """Sliding-window HV prints over (ts, high, low, close) bars.

    One print per window, timestamped at the window's LAST bar ts (the bar
    close the print describes — aligns synthetic prints with live prints,
    which fire just after bar close). Windows whose HV is None (degenerate
    ranges) are skipped, never fabricated. Bars are sorted by ts first so a
    scrambled buffer cannot corrupt the series. Empty/short input → []."""
    try:
        clean = sorted(
            (float(b[0]), float(b[1]), float(b[2]), float(b[3]))
            for b in bars
        )
    except (TypeError, ValueError, IndexError):
        return []
    if window < 2 or len(clean) < window:
        return []
    out: list[Print] = []
    for i in range(window - 1, len(clean)):
        w = clean[i - window + 1: i + 1]
        hv = parkinson_hv_bar(
            [b[1] for b in w], [b[2] for b in w], [b[3] for b in w],
            periods_per_year=periods_per_year)
        if hv is not None:
            out.append((w[-1][0], hv))
    return out


def seed_ring(
    existing_ring: Sequence[Print],
    bars: Sequence[Bar],
    now: Optional[float] = None,
    window: int = SEED_WINDOW_BARS,
    cap: int = RING_CAP,
) -> list[Print]:
    """Merge synthetic HV prints from seeded bars into the existing ring.

    Returns the NEW full ring contents (the splice assigns it back and
    persists). Idempotent: a candidate print is dropped when any ring print
    sits within half a print interval of its ts — re-seeding the same
    buffer, or seeding over bars a live print already covered, adds
    nothing. Prints in the future (ts > now, when now is given) are
    dropped. Result is ts-sorted and capped to the newest ``cap`` prints —
    the same retention as the live append path. Malformed ring rows are
    skipped (one-bad-line doctrine)."""
    ring: list[Print] = []
    for row in existing_ring or []:
        try:
            ring.append((float(row[0]), float(row[1])))
        except (TypeError, ValueError, IndexError):
            continue
    merged = list(ring)
    for ts, hv in hv_prints_from_bars(bars, window=window):
        if now is not None and ts > float(now):
            continue
        if any(abs(ts - rts) < _DEDUPE_BUCKET_S for rts, _ in merged):
            continue
        merged.append((ts, hv))
    merged.sort(key=lambda p: p[0])
    if cap > 0 and len(merged) > cap:
        merged = merged[-cap:]
    return merged


def ring_health(
    ring: Sequence[Print],
    now: float,
    min_prints: int = MIN_RVR_PRINTS,
    stall_after_s: float = STALL_AFTER_S,
) -> dict:
    """Ring-health verdict for stall detection — pure, never raises.

    verdict:
      "empty"   — no prints: rv_rank abstains, rvr leg dark.
      "seeding" — prints but fewer than min_prints: rv_rank still None.
      "stalled" — newest print older than stall_after_s: the feed stopped
                  silently (signal drought / swallowed exception); the ring
                  may be warm but is going stale.
      "warm"    — >= min_prints and fresh: rv_rank computable.
    """
    out = {"prints": 0, "warm": False, "stalled": False,
           "last_print_ts": None, "last_age_s": None, "verdict": "empty"}
    try:
        rows = [(float(r[0]), float(r[1])) for r in (ring or [])]
    except (TypeError, ValueError, IndexError):
        rows = []
    out["prints"] = len(rows)
    if not rows:
        return out
    last_ts = max(r[0] for r in rows)
    age = float(now) - last_ts
    out["last_print_ts"] = last_ts
    out["last_age_s"] = age
    out["warm"] = len(rows) >= min_prints
    out["stalled"] = age > stall_after_s
    if out["stalled"]:
        out["verdict"] = "stalled"
    elif out["warm"]:
        out["verdict"] = "warm"
    else:
        out["verdict"] = "seeding"
    return out
