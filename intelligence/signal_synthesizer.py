"""intelligence/signal_synthesizer.py — Three-type signal-class synthesizer
(pure brain, zero I/O).

Governor doctrine (locked 2026-09-26): instruments split into THREE signal
classes, and the institutional-demand picture each class can honestly see
is different:

  TYPE A "etf_backed" (BTC, ETH, NVDA): true institutional demand = perp
    whale signal x (1 + ETF-flow modifier). The spot-ETF demand pool is
    INVISIBLE to perp data — a $433M ETF inflow day is real institutional
    buying the whale feeds cannot see, so the perp whale signal must be
    boosted by the flow leg, never read alone.
  TYPE B "etf_amplified_indirect" (AMD, MSTR, COIN, HOOD): ETF flows reach
    the name through sector/leveraged wrappers (SOXL, MSTU, crypto-equity
    baskets). Some legs have NO data plane yet — those legs abstain.
  TYPE C "pure_whale" (all alts: SUI, ARB, FET, ZRO, TIA, RENDER, UNI,
    AAVE, SNX, PENDLE, OP, AVAX, SOL, ...): ZERO ETF coverage. The perp
    whale signal IS the complete institutional picture.

THE ALT-PURITY LAW (pinned hard): a pure_whale symbol's modifier is 1.00
ALWAYS — never diluted for lacking an ETF leg it cannot have, never boosted
by a data plane that does not exist for it. Alt whale signal is trusted
MORE per unit, not less. composite_modifier() on a pure_whale symbol
returns 1.00 REGARDLESS of the legs handed in, and effective_whale_signal()
returns the whale_ratio verbatim.

BOUNDED-CLAMP RATIONALE: this module outputs a bounded CONFIDENCE MODIFIER
for candidate scoring / anticipator arming. It NEVER sizes, NEVER sets
leverage, NEVER gates entries. The composite is clamped to
[synth_composite_floor 0.55, synth_composite_cap 1.40] — the doctrine's own
extremes (ETF-outflow x0.55 floor; max single boost +0.40) — because naively
multiplying stacked legs compounds unboundedly: 1.15 x 1.25 x 1.30 x 1.15 =
2.28x confidence amplification, which would let four weak correlated hints
out-vote the risk stack. Confidence is bounded; conviction is earned
elsewhere.

DARK-PLANE ABSTAIN LAW: a leg whose feed does not exist (or is stale /
unknown) contributes 1.00 (identity) — never synthesize conviction from a
feed that isn't there. Current dark planes, ALL abstaining until a feed
exists: SOXL/SOXS flows, MSTU flows, SOX index level, MSTR NAV premium,
narrative_day. A measured 0.0 is NOT dark — None means abstain, 0.0 means
the measured-zero rung of the ladder.

Department shape (docs/DEPARTMENT_TEMPLATE.md): zero-I/O brain. No network,
no files, no clock — every input injected per call, every tunable a getattr
knob on the injected cfg. Stateless except config.

Kill switches (config getattr, False = identity, zero state mutation):
  signal_synthesizer_enabled       True  — master gate; False returns every
                                     method's identity (1.00 modifiers,
                                     whale_ratio verbatim, None
                                     classifications stay None)
  synth_compression_enabled        True
  synth_compression_threshold_pct  10.0  — below-ATH distance that arms the
                                     compression bonus
  synth_compression_bonus_mult     1.25
  synth_composite_floor            0.55
  synth_composite_cap              1.40

Telemetry: none emitted here (pure modifier math); the splice logs
synth_* events where the modifiers are consumed.
"""
from __future__ import annotations

from typing import Dict, Optional

# ── Classification tables (defaults; cfg may override via knobs) ──────────
ETF_BACKED = frozenset({"BTC", "ETH", "NVDA"})
ETF_AMPLIFIED_INDIRECT = frozenset({"AMD", "MSTR", "COIN", "HOOD"})

# ── ETF flow ladder (USD/day → additive boost b; modifier = 1.00 + b) ─────
ETF_FLOW_STRONG_USD = 500_000_000.0     # strictly greater → +0.40
ETF_FLOW_MEDIUM_USD = 100_000_000.0     # strictly greater → +0.20
ETF_BOOST_STRONG = 0.40
ETF_BOOST_MEDIUM = 0.20
ETF_BOOST_ZERO = 0.0                    # measured 0-100M rung
ETF_BOOST_OUTFLOW = -0.35               # negative flow

# ── MSTR NAV-premium → beta table (dark plane — abstains until fed) ───────
NAV_BETA_HIGH = 2.8                     # premium > 1.5
NAV_BETA_MID = 1.8                      # 1.0 < premium <= 1.5
NAV_BETA_LOW = 1.3                      # 0.7 <= premium <= 1.0
NAV_BETA_DEEP = 1.1                     # premium < 0.7
NAV_PREMIUM_HIGH = 1.5
NAV_PREMIUM_MID = 1.0
NAV_PREMIUM_LOW = 0.7

# ── Alt whale-tier ladder (pure_whale only) ───────────────────────────────
ALT_TIER_LADDER = (
    (4.0, "TIER_1"),
    (3.0, "TIER_2"),
    (2.0, "TIER_3"),
    (1.5, "TIER_4"),
)


def _normalize(symbol) -> Optional[str]:
    """'BTC-USD' / 'btc' / 'btc-usd' → 'BTC'. Empty/non-string → None."""
    if not isinstance(symbol, str) or not symbol.strip():
        return None
    return symbol.strip().upper().split("-")[0] or None


def _num(x) -> Optional[float]:
    """Strict numeric coercion — NaN/inf/non-numeric → None (fail-closed)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


class SignalSynthesizer:
    """Stateless signal-class modifier brain. All state lives in the cfg
    and the per-call arguments; the instance carries nothing."""

    # ── 1. classification ─────────────────────────────────────────────────

    def signal_class(self, symbol) -> Optional[str]:
        """'etf_backed' | 'etf_amplified_indirect' | 'pure_whale' | None.
        Unknown/unnormalizable symbols → None (fail-closed: never guess a
        class for an instrument the doctrine has not placed)."""
        base = _normalize(symbol)
        if base is None:
            return None
        if base in ETF_BACKED:
            return "etf_backed"
        if base in ETF_AMPLIFIED_INDIRECT:
            return "etf_amplified_indirect"
        return "pure_whale"

    # ── 2. ETF flow modifier (TYPE A only) ────────────────────────────────

    def etf_modifier(self, cfg, *, symbol,
                     etf_daily_flow_usd: Optional[float]) -> float:
        """1.00 + additive flow boost. Only etf_backed symbols have an ETF
        leg; every other class returns 1.00 (identity — a leg the symbol
        cannot have contributes nothing). None flow = DARK plane → abstain
        1.00; a MEASURED 0.0 is the 0-100M rung (modifier 1.00 via +0.0).
        Ladder boundaries are strictly-greater: exactly $500M → +0.20."""
        if not getattr(cfg, "signal_synthesizer_enabled", True):
            return 1.00
        if self.signal_class(symbol) != "etf_backed":
            return 1.00
        if etf_daily_flow_usd is None:
            return 1.00                     # dark plane abstains
        flow = _num(etf_daily_flow_usd)
        if flow is None:
            return 1.00                     # degenerate input = dark
        if flow < 0:
            return 1.00 + ETF_BOOST_OUTFLOW
        if flow > ETF_FLOW_STRONG_USD:
            return 1.00 + ETF_BOOST_STRONG
        if flow > ETF_FLOW_MEDIUM_USD:
            return 1.00 + ETF_BOOST_MEDIUM
        return 1.00 + ETF_BOOST_ZERO

    # ── 3. Compression bonus ──────────────────────────────────────────────

    def compression_bonus(self, cfg, *, symbol, current_price,
                          ath_reference) -> float:
        """Deep-below-ATH compression → synth_compression_bonus_mult.
        Reclaim above the threshold line reverses it (falls out naturally).
        ath None/<=0 or price <=0 → 1.00 (abstain — no honest reference)."""
        if not getattr(cfg, "signal_synthesizer_enabled", True):
            return 1.00
        if not getattr(cfg, "synth_compression_enabled", True):
            return 1.00
        try:
            threshold = float(getattr(cfg, "synth_compression_threshold_pct",
                                      10.0))
            mult = float(getattr(cfg, "synth_compression_bonus_mult", 1.25))
        except (TypeError, ValueError):
            return 1.00
        price = _num(current_price)
        ath = _num(ath_reference)
        if price is None or ath is None or price <= 0 or ath <= 0:
            return 1.00
        if mult <= 0:
            return 1.00
        if price < ath * (1.0 - threshold / 100.0):
            return mult
        return 1.00

    # ── 4. MSTR NAV-premium beta ──────────────────────────────────────────

    def nav_premium_beta(self, cfg, *, symbol,
                         nav_premium: Optional[float]) -> Optional[float]:
        """MSTR-only table. Non-MSTR → None. nav_premium None → None: NO
        NAV-premium feed exists yet (dark plane) — the dark plane abstains,
        it never fabricates a beta."""
        if not getattr(cfg, "signal_synthesizer_enabled", True):
            return None
        if _normalize(symbol) != "MSTR":
            return None
        if nav_premium is None:
            return None
        prem = _num(nav_premium)
        if prem is None:
            return None
        if prem > NAV_PREMIUM_HIGH:
            return NAV_BETA_HIGH
        if prem > NAV_PREMIUM_MID:
            return NAV_BETA_MID
        if prem >= NAV_PREMIUM_LOW:
            return NAV_BETA_LOW
        return NAV_BETA_DEEP

    # ── 5. Bounded composite ──────────────────────────────────────────────

    def composite_modifier(self, cfg, *, symbol, legs: dict) -> float:
        """Product of the non-None legs, clamped [floor, cap].

        ALT PURITY LAW: pure_whale → 1.00 REGARDLESS of legs. An alt has no
        ETF/compression data planes; handing it legs cannot manufacture
        them. None legs are dropped (abstained legs contribute identity).
        Empty/all-abstained → 1.00. The clamp is the doctrine's own bound:
        stacked legs never compound past [0.55, 1.40]."""
        if not getattr(cfg, "signal_synthesizer_enabled", True):
            return 1.00
        if self.signal_class(symbol) == "pure_whale":
            return 1.00                     # alt purity — pinned hard
        try:
            floor = float(getattr(cfg, "synth_composite_floor", 0.55))
            cap = float(getattr(cfg, "synth_composite_cap", 1.40))
        except (TypeError, ValueError):
            return 1.00
        if floor > cap:                     # degenerate config: fail-closed
            return 1.00
        product = 1.0
        seen = False
        for v in (legs or {}).values():
            if v is None:
                continue                    # abstained leg — identity
            val = _num(v)
            if val is None or val <= 0:
                continue                    # degenerate leg — never multiply
            product *= val                  # poison into the product
            seen = True
        if not seen:
            return 1.00
        return min(cap, max(floor, product))

    # ── 6. Headline: effective whale signal ───────────────────────────────

    def effective_whale_signal(self, cfg, *, symbol, whale_ratio: float,
                               etf_daily_flow_usd=None,
                               compression_legs: Optional[dict] = None
                               ) -> Optional[float]:
        """whale_ratio x composite of the symbol's honest legs. For
        pure_whale: whale_ratio VERBATIM (alt purity). Degenerate ratio
        (<=0, non-numeric) → None — a dead whale feed abstains, it never
        manufactures zero-confidence."""
        if not getattr(cfg, "signal_synthesizer_enabled", True):
            ratio = _num(whale_ratio)
            return ratio if (ratio is not None and ratio > 0) else None
        ratio = _num(whale_ratio)
        if ratio is None or ratio <= 0:
            return None
        cls = self.signal_class(symbol)
        if cls == "pure_whale":
            return ratio                    # the whale signal IS the picture
        legs: Dict[str, Optional[float]] = {}
        if cls == "etf_backed":
            legs["etf"] = self.etf_modifier(
                cfg, symbol=symbol, etf_daily_flow_usd=etf_daily_flow_usd)
        for k, v in (compression_legs or {}).items():
            legs[k] = v
        return ratio * self.composite_modifier(cfg, symbol=symbol, legs=legs)

    # ── 7. Propagation deadline (anticipator arming window) ───────────────

    def propagation_deadline(self, cfg, *, trigger_ts: float,
                             lag_hours: float) -> Optional[float]:
        """trigger_ts + lag_hours x 3600. The anticipator windows ETF→alt
        propagation arms inside this deadline (12-24h doctrine: a confirmed
        BTC ETF flow prints, alt limits sit inside the lag window)."""
        if not getattr(cfg, "signal_synthesizer_enabled", True):
            return None
        ts = _num(trigger_ts)
        lag = _num(lag_hours)
        if ts is None or lag is None:
            return None
        return ts + lag * 3600.0

    # ── 8. Alt whale tier ─────────────────────────────────────────────────

    def alt_whale_tier(self, cfg, *, symbol,
                       whale_ratio: float) -> Optional[str]:
        """pure_whale only: 4.0→TIER_1, 3.0→TIER_2, 2.0→TIER_3,
        1.5→TIER_4, below → None. Non-alt → None (tiers are the alt-
        confidence ladder; majors run the ETF-composite path instead)."""
        if not getattr(cfg, "signal_synthesizer_enabled", True):
            return None
        if self.signal_class(symbol) != "pure_whale":
            return None
        ratio = _num(whale_ratio)
        if ratio is None:
            return None
        for rung, tier in ALT_TIER_LADDER:
            if ratio >= rung:
                return tier
        return None
