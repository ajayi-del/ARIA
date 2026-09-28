"""intelligence/whale_proxy.py — The Governor's WhaleProxy (pure brain, zero-I/O).

2026-09-26 (Governor directive): a composite institutional-presence score
(0.0 retail/bearish → 1.0 strong institutional) replacing the unavailable
external whale long/short ratio. Four evidence planes, weighted:

  1. FUNDING DISCIPLINE (0–0.35) — sustained LOW funding is the signature
     of spot-driven institutional accumulation; funding pinned at the
     exchange cap is either a retail FOMO spike or a structural bid
     (disambiguated below).
  2. OI GROWTH (0–0.35) — expanding open interest beside disciplined
     funding = new institutional positions; collapsing OI = distribution.
  3. SPIKE-VS-SUSTAINED (0–0.20) — the max/avg funding ratio separates a
     one-off retail spike (ratio > 3) from a sustained regime (~1).
  4. ACCOUNT RATIO (0–0.10) — the Bybit account-count long share; a tilt,
     not evidence. Dark = 0.05 neutral, ALWAYS present.

UNITS LAW: all funding values are BPS internally (raw Bybit decimal rates
× 1e4). The Governor's source pseudocode mixed decimal thresholds with bps
labels (off by 10×) — this module takes bps everywhere:
funding_avg_bps / funding_latest_bps / funding_max_bps / funding_min_bps.

DARK-PLANE DOCTRINE: a dark leg ABSTAINS — it does not score zero. The
composite is computed over the available leg weights and RENORMALIZED
(documented in notes as oi_dark_renormalized etc.). Zero would confuse
"no data" with "bearish data". Exception: the account-ratio tilt scores a
fixed 0.05 neutral when dark (it is a tilt, not evidence — and alone it
can never mint a score: with BOTH funding and OI dark, compute() returns
None).

STRUCTURAL MAX-CAP OVERRIDE (the Governor's FET rule): funding_avg_bps
>= 8 AND max/min spread < 2× → sustained funding at the exchange cap is
an INSTITUTIONAL BID, not a retail spike — force score >= 0.80, tier 1,
note structural_institutional_bid.

RETAIL-SPIKE OVERRIDE (mirror rule, the Governor's LINK case): latest
funding AT the cap (>= 10bps) while the window average is low (< 8bps) =
a FRESH retail FOMO spike, not a regime — cap the score at 0.20, tier 5,
note retail_fomo_spike_at_cap. Sustained-high (FET) and fresh-spike (LINK)
are the two ways to read "funding at max cap"; avg-vs-latest is the
separator.

Fail-closed law: nothing raises on bad input — every leg fails toward
abstain/neutral, and compute() returns None when no evidence plane is lit.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Mapping, Optional

import structlog

logger = structlog.get_logger(__name__)


# ── Kill switch ───────────────────────────────────────────────────────────────

def whale_proxy_enabled() -> bool:
    """Module-level kill switch (env WHALE_PROXY_ENABLED, default true).
    False = WhaleProxyFeed.score() returns None (inert)."""
    return os.environ.get("WHALE_PROXY_ENABLED", "true").strip().lower() in (
        "1", "true", "yes", "on")


# ── Weights ───────────────────────────────────────────────────────────────────

W_FUNDING = 0.35
W_OI = 0.35
W_SPIKE = 0.20
W_RATIO = 0.10

STRUCTURAL_AVG_MIN_BPS = 8.0      # FET rule: avg at/near cap
STRUCTURAL_SPREAD_MAX = 2.0       # max/min < 2× = sustained, not spiky
RETAIL_SPIKE_LATEST_BPS = 10.0    # latest AT the exchange cap
RETAIL_SPIKE_SCORE_CAP = 0.20
STRUCTURAL_SCORE_FLOOR = 0.80


def _f(x) -> Optional[float]:
    """Coerce to float; None on any garbage (fail-closed idiom)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return v


# ── Legs (pure, individually pinned) ─────────────────────────────────────────

def funding_discipline_score(avg_bps, max_bps=None) -> Optional[float]:
    """Funding discipline 0–0.35: avg < 3bps → 0.35; < 6 → 0.25;
    < 10 → 0.10; >= 10 → 0.0. Structural-cap bonus: |avg − max| < 0.01bps
    (the avg==max==min pinned class) adds +0.10, capped at 0.35.
    avg None → abstain (None)."""
    avg = _f(avg_bps)
    if avg is None:
        return None
    if avg < 3.0:
        s = 0.35
    elif avg < 6.0:
        s = 0.25
    elif avg < 10.0:
        s = 0.10
    else:
        s = 0.0
    mx = _f(max_bps)
    if mx is not None and abs(avg - mx) < 0.01:
        s = min(s + 0.10, 0.35)
    return s


def oi_growth_score(oi_growth_pct) -> Optional[float]:
    """OI growth 0–0.35: >20% → 0.35; >10 → 0.25; >0 → 0.15;
    >-5 → 0.05; else 0.0. None (dark OI plane) → ABSTAIN (None) — the
    caller renormalizes over the remaining weights; dark is not zero."""
    g = _f(oi_growth_pct)
    if g is None:
        return None
    if g > 20.0:
        return 0.35
    if g > 10.0:
        return 0.25
    if g > 0.0:
        return 0.15
    if g > -5.0:
        return 0.05
    return 0.0


def spike_score(avg_bps, max_bps) -> Optional[float]:
    """Spike-vs-sustained 0–0.20: spike_ratio = max / max(avg, 0.01bps);
    > 3.0 → 0.0 (retail FOMO); < 1.5 → 0.20 (sustained); else 0.10.
    Either input None → abstain (None)."""
    avg, mx = _f(avg_bps), _f(max_bps)
    if avg is None or mx is None:
        return None
    ratio = mx / max(avg, 0.01)
    if ratio > 3.0:
        return 0.0
    if ratio < 1.5:
        return 0.20
    return 0.10


def account_ratio_score(long_share) -> float:
    """Account ratio 0–0.10 (a tilt, always present): None (dark) → 0.05
    neutral; long_share > 0.6 → 0.10; > 0.5 → 0.07; else 0.02."""
    ls = _f(long_share)
    if ls is None:
        return 0.05
    if ls > 0.6:
        return 0.10
    if ls > 0.5:
        return 0.07
    return 0.02


# ── Verdict ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ProxyScore:
    """Composite institutional-presence verdict. score 0.0–1.0, tier 1–5
    (1 = strong institutional, 5 = reject), components carries each leg's
    contribution (None = abstained), notes names renormalizations and
    overrides."""
    score: float
    tier: int
    components: Dict[str, Optional[float]] = field(default_factory=dict)
    notes: str = ""


def proxy_tier(score) -> int:
    """Score → tier: >=0.80 → 1; >=0.60 → 2; >=0.40 → 3; >=0.25 → 4
    (watch only); else 5 (reject)."""
    s = _f(score)
    if s is None:
        return 5
    if s >= 0.80:
        return 1
    if s >= 0.60:
        return 2
    if s >= 0.40:
        return 3
    if s >= 0.25:
        return 4
    return 5


def tier_to_axiom(tier) -> int:
    """Proxy tier → axiom-stack entry tier: T1→1, T2→2, T3→3,
    T4/T5→0 (watch/reject both = no entry)."""
    try:
        t = int(tier)
    except (TypeError, ValueError):
        return 0
    if t in (1, 2, 3):
        return t
    return 0


def compute(funding_avg_bps=None, funding_max_bps=None, funding_min_bps=None,
            oi_growth_pct=None, account_ratio=None,
            funding_latest_bps=None) -> Optional[ProxyScore]:
    """The composite. All funding inputs in BPS (UNITS LAW).

    Returns None when NO evidence plane is lit (funding avg AND OI both
    dark) — the account-ratio tilt alone can never mint a score.
    Dark legs abstain and the composite is renormalized over the
    available weights; the account-ratio leg always scores (dark = 0.05
    neutral) but only ever counts beside real evidence."""
    avg, mx, mn, latest = (_f(funding_avg_bps), _f(funding_max_bps),
                           _f(funding_min_bps), _f(funding_latest_bps))
    oi = _f(oi_growth_pct)

    f_leg = funding_discipline_score(avg, mx)
    o_leg = oi_growth_score(oi)
    s_leg = spike_score(avg, mx)
    r_leg = account_ratio_score(account_ratio)

    if f_leg is None and o_leg is None:
        return None                       # no evidence plane lit

    legs = [("funding_discipline", f_leg, W_FUNDING),
            ("oi_growth", o_leg, W_OI),
            ("spike_vs_sustained", s_leg, W_SPIKE),
            ("account_ratio", r_leg, W_RATIO)]
    notes = []
    total = 0.0
    weight = 0.0
    components: Dict[str, Optional[float]] = {}
    for name, val, w in legs:
        components[name] = val
        if val is None:
            notes.append(f"{name}_dark_renormalized")
            continue
        total += val
        weight += w
    score = total / weight if weight > 0 else 0.0

    # Retail-spike override (the LINK case): fresh spike AT the cap from a
    # low average = retail FOMO, not a regime. Applied before the
    # structural floor; the two are mutually exclusive by construction
    # (avg < 8 vs avg >= 8).
    if (latest is not None and avg is not None
            and latest >= RETAIL_SPIKE_LATEST_BPS and avg < STRUCTURAL_AVG_MIN_BPS):
        if score > RETAIL_SPIKE_SCORE_CAP:
            score = RETAIL_SPIKE_SCORE_CAP
        notes.append("retail_fomo_spike_at_cap")

    # Structural max-cap override (the FET rule): avg >= 8bps with a
    # tight max/min spread = sustained institutional bid at the cap.
    if (avg is not None and mx is not None and mn is not None
            and avg >= STRUCTURAL_AVG_MIN_BPS and mn > 0.0
            and (mx / mn) < STRUCTURAL_SPREAD_MAX):
        if score < STRUCTURAL_SCORE_FLOOR:
            score = STRUCTURAL_SCORE_FLOOR
        notes.append("structural_institutional_bid")

    score = round(score, 4)
    return ProxyScore(score=score, tier=proxy_tier(score),
                      components=components, notes=";".join(notes))


# ── Convenience feed (the only I/O-adjacent class; brains stay pure) ─────────

_BPS = 1e4


def _bps(x) -> Optional[float]:
    """Raw Bybit decimal rate → bps (×1e4). None passes through."""
    v = _f(x)
    return v * _BPS if v is not None else None


def ratio_long_share_provider(whale_ratio_feed) -> Callable[[str], Optional[float]]:
    """Adapter: WhaleRatioFeed → ratio_provider callable for
    WhaleProxyFeed. Dark/stale symbols yield None (the neutral tilt)."""
    def _provider(symbol: str) -> Optional[float]:
        try:
            r = whale_ratio_feed.get_reading(symbol)
        except Exception:
            return None
        return r.long_share if r is not None else None
    return _provider


class WhaleProxyFeed:
    """Convenience aggregator over the three injected planes. Pure
    compute stays in compute() — this class only gathers.

    Provider contracts (all callables, symbol → value, exceptions = dark):
      funding_provider(symbol) -> Optional[Mapping] of RAW DECIMAL Bybit
        rates with keys "avg" / "max" / "min" / "latest" (any may be
        absent); converted ×1e4 to bps at the boundary (UNITS LAW).
      oi_provider(symbol) -> Optional[float] OI growth % (None = dark;
        OIHistoryFeed.get_growth plugs in directly).
      ratio_provider(symbol) -> Optional[float] long share 0..1 (None =
        dark; ratio_long_share_provider adapts a WhaleRatioFeed).

    score(symbol) -> ProxyScore | None. None when the kill switch is off
    or every plane is dark."""

    def __init__(self,
                 funding_provider: Optional[Callable[[str], Optional[Mapping]]] = None,
                 oi_provider: Optional[Callable[[str], Optional[float]]] = None,
                 ratio_provider: Optional[Callable[[str], Optional[float]]] = None,
                 time_fn: Callable[[], float] = time.time):
        self._funding_provider = funding_provider
        self._oi_provider = oi_provider
        self._ratio_provider = ratio_provider
        self._time = time_fn

    def _funding_bps(self, symbol: str) -> Dict[str, Optional[float]]:
        out: Dict[str, Optional[float]] = {"avg": None, "max": None,
                                           "min": None, "latest": None}
        if self._funding_provider is None:
            return out
        try:
            raw = self._funding_provider(symbol)
        except Exception as e:
            logger.warning("whale_proxy_funding_provider_failed", symbol=symbol,
                           error=str(e)[:120])
            return out
        if not isinstance(raw, Mapping):
            return out
        for k in out:
            out[k] = _bps(raw.get(k))
        return out

    def score(self, symbol: str) -> Optional[ProxyScore]:
        if not whale_proxy_enabled():
            return None
        sym = str(symbol)
        fr = self._funding_bps(sym)
        oi = None
        if self._oi_provider is not None:
            try:
                oi = _f(self._oi_provider(sym))
            except Exception as e:
                logger.warning("whale_proxy_oi_provider_failed", symbol=sym,
                               error=str(e)[:120])
        ratio = None
        if self._ratio_provider is not None:
            try:
                ratio = _f(self._ratio_provider(sym))
            except Exception as e:
                logger.warning("whale_proxy_ratio_provider_failed", symbol=sym,
                               error=str(e)[:120])
        if all(v is None for v in fr.values()) and oi is None and ratio is None:
            return None                   # every plane dark
        return compute(funding_avg_bps=fr["avg"], funding_max_bps=fr["max"],
                       funding_min_bps=fr["min"], oi_growth_pct=oi,
                       account_ratio=ratio, funding_latest_bps=fr["latest"])
