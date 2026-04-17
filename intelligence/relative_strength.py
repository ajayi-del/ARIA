"""
intelligence/relative_strength.py — Market Regime Classifier v2.0

Rank-based regime detection using 48-period relative momentum, cross-asset
dispersion, funding bias, and commodity subcategory flows.

WHY v1.2 WAS BROKEN
───────────────────
v1.2 used 24h category-average returns. When BTC and ETH both moved flat,
cat_scores["large_cap"] == 0.0. max() and min() on identical values both
returned "large_cap", producing leading == lagging == "large_cap" — a logical
contradiction falsely labelled "confused" by the fallback regime.

v2.0 FIX
────────
Rank all assets individually by 48-period relative momentum on 1m candles.
A rank spread of < 2 (truly identical movement) is the ONLY valid "confused"
state. Everything else produces a meaningful regime. Default fallback is
"transitioning" — which correctly communicates uncertainty without asserting
a contradiction.

Architecture:
  1. Compute 48-period relative momentum per symbol (1m candles)
  2. Rank all assets 1..n by momentum (1 = fastest)
  3. Compute cross-sectional dispersion (std of momentum values)
  4. Get funding bias from FundingRadar carry_score average (optional)
  5. Compute commodity subcategory rank averages
  6. Classify regime via ordered conditions (do not reorder)
  7. Fallback: "transitioning" (rank spread exists, no clear pattern)
"""

from __future__ import annotations

import structlog
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field

logger = structlog.get_logger(__name__)


# ── Asset taxonomy ─────────────────────────────────────────────────────────────

ASSET_CATEGORIES: Dict[str, str] = {
    # Large cap crypto
    "BTC-USD":       "large_cap",
    "ETH-USD":       "large_cap",
    "XRP-USD":       "large_cap",
    # Alt L1
    "SOL-USD":       "alt_l1",
    "AVAX-USD":      "alt_l1",
    "NEAR-USD":      "alt_l1",
    "ARB-USD":       "alt_l1",
    "OP-USD":        "alt_l1",
    "SUI-USD":       "alt_l1",
    "MNT-USD":       "alt_l1",
    # DeFi infrastructure
    "LINK-USD":      "defi_infra",
    "AAVE-USD":      "defi_infra",     # Not yet on SoDEX — reserved
    "DEFISSI-USD":   "defi_infra",     # Not yet on SoDEX — reserved
    # CEX ecosystem
    "BNB-USD":       "cex_ecosystem",
    "HYPE-USD":      "cex_ecosystem",  # Not yet on SoDEX — reserved
    # Commodities — real world assets (three subcategories)
    "XAUT-USD":      "commodity_precious",
    "COPPER-USD":    "commodity_industrial",   # Live on SoDEX
    "WTI-USD":       "commodity_energy",       # Future
    "BRENT-USD":     "commodity_energy",       # Future
    "CL-USD":        "commodity_energy",       # Live on SoDEX (crude oil contract)
    # Equity indices and MAG7 proxies
    "USTECH-USD":    "index_tech",
    "SPX-USD":       "index_broad",
    "MAG7-USD":      "index_tech",
    "MAG7SSI-USD":   "index_tech",     # Not yet on SoDEX — reserved
    "TSM-USD":       "index_tech",
    "ORCL-USD":      "index_tech",
    # Meme / narrative
    "TRUMP-USD":     "meme",
    "DOGE-USD":      "meme",
    "PEPE-USD":      "meme",
    "1000PEPE-USD":  "meme",
    "BASED-USD":     "meme",
    "MEMESSI-USD":   "meme",           # Not yet on SoDEX — reserved
}


# ── External macro context ─────────────────────────────────────────────────────
# These assets are NOT in the trading universe but feed the regime classifier
# as non-tradeable context signals. When CL-USD / COPPER-USD are unavailable
# as live SoDEX perps, Yahoo Finance data fills the gap.

EXTERNAL_MACRO_SOURCES: Dict[str, Dict[str, Any]] = {
    "copper_usd": {
        "source":        "yahoo_finance",
        "ticker":        "HG=F",
        "category":      "commodity_industrial",
        "regime_weight": 0.3,
        "tradeable":     False,
    },
    "wti_crude": {
        "source":        "yahoo_finance",
        "ticker":        "CL=F",
        "category":      "commodity_energy",
        "regime_weight": 0.3,
        "tradeable":     False,
    },
    "us_10yr_yield": {
        "source":        "yahoo_finance",
        "ticker":        "^TNX",
        "category":      "rates",
        "regime_weight": 0.4,
        "tradeable":     False,
    },
}


# ── Regime → allowed symbols gate ──────────────────────────────────────────────
# Imported by risk_engine.py for the symbol restriction gate.
# None = all symbols allowed. List = whitelist.

REGIME_ALLOWED_SYMBOLS: Dict[str, Optional[List[str]]] = {
    "risk_on":             None,                                           # all
    "btc_dominance":       ["BTC-USD", "ETH-USD"],
    "alt_season":          None,                                           # all
    "risk_off":            ["XAUT-USD"],
    "tech_led":            ["USTECH-USD", "BTC-USD", "ETH-USD"],
    "mag7_led":            ["BTC-USD", "ETH-USD", "USTECH-USD"],
    "defi_stress":         ["BTC-USD", "ETH-USD", "XAUT-USD"],
    "cex_flow":            ["BNB-USD", "BTC-USD"],
    "transitioning":       ["BTC-USD", "ETH-USD"],
    "confused":            ["BTC-USD", "ETH-USD"],
    "geopolitical_stress": ["XAUT-USD", "BTC-USD"],
    "stagflation_fear":    ["XAUT-USD"],
    "growth_expansion":    ["BTC-USD", "ETH-USD", "SOL-USD", "AVAX-USD"],
}


# ── RegimeState ────────────────────────────────────────────────────────────────

@dataclass
class RegimeState:
    """
    Output of the regime classifier.

    Fields
    ──────
    regime           One of: risk_on, risk_off, btc_dominance, alt_season,
                     tech_led, mag7_led, defi_stress, cex_flow, transitioning,
                     geopolitical_stress, stagflation_fear, growth_expansion,
                     confused
    leading_category Category outperforming. "none" in genuine confused state.
    lagging_category Category underperforming. "none" in genuine confused state.
    dispersion       Cross-sectional std of 48-period momentum values.
    confidence       0.0–1.0 classifier confidence estimate.
    category_scores  Per-category momentum averages (backward compat for
                     market_context.py confidence estimation).
    asset_scores     Per-asset momentum scores.
    """
    regime:           str
    leading_category: str
    lagging_category: str
    dispersion:       float
    confidence:       float = 0.5
    category_scores:  Dict[str, float] = field(default_factory=dict)
    asset_scores:     Dict[str, float] = field(default_factory=dict)


# Backward-compat alias — market_context.py and older code imports this name.
RegimeMatrix = RegimeState

# All valid regime strings — useful for validation / tests.
ALL_REGIMES = frozenset({
    "risk_on", "risk_off", "btc_dominance", "alt_season",
    "tech_led", "mag7_led", "defi_stress", "cex_flow",
    "transitioning", "confused",
    "geopolitical_stress", "stagflation_fear", "growth_expansion",
})


# ── Engine ─────────────────────────────────────────────────────────────────────

class RelativeStrengthEngine:
    """
    v2.0 Relative Strength & Regime Classifier.

    Rank-based cross-asset momentum with dispersion and funding layers.
    Eliminates the v1.2 bug where leading==lagging==large_cap on every tick
    because 24h category averages were identical when all assets moved together.

    Parameters
    ──────────
    config          Settings — provides config.assets
    funding_radar   Optional FundingRadar — adds carry_score bias layer.
                    Safe to wire in post-construction via set_funding_radar().
    ssi_engine      Optional SSI engine — provides mag7_ssi_score for MAG7_LED.
                    Safe to wire in post-construction via set_ssi_engine().
    """

    def __init__(
        self,
        config,
        funding_radar: Optional[Any] = None,
        ssi_engine:    Optional[Any] = None,
    ) -> None:
        self.config        = config
        self.funding_radar = funding_radar
        self.ssi_engine    = ssi_engine
        self.symbols: List[str] = list(config.assets)

    def set_funding_radar(self, funding_radar: Any) -> None:
        """Wire in FundingRadar after construction (avoids circular imports)."""
        self.funding_radar = funding_radar

    def set_ssi_engine(self, ssi_engine: Any) -> None:
        """Wire in SSI engine after construction."""
        self.ssi_engine = ssi_engine

    # ── Step 1: Momentum ────────────────────────────────────────────────────────

    def _compute_momentum(
        self,
        symbol:         str,
        candle_buffers: Dict[str, Any],
    ) -> float:
        """
        48-period relative return on 1m candles.

        Returns 0.0 when:
          - symbol not in candle_buffers
          - no 1m buffer available
          - fewer than 10 candles (insufficient history)
          - start_price is zero (data error guard)
        """
        sym_bufs = candle_buffers.get(symbol)
        if sym_bufs is None:
            return 0.0
        buf_1m = sym_bufs.get("1m")
        if buf_1m is None:
            return 0.0
        candles = buf_1m.latest(50)
        if len(candles) < 10:
            return 0.0
        lookback    = min(48, len(candles) - 1)
        start_price = candles[-lookback].close
        end_price   = candles[-1].close
        if start_price == 0:
            return 0.0
        return (end_price - start_price) / start_price

    # ── Public interface ────────────────────────────────────────────────────────

    def compute_regime(self, candle_buffers: Dict[str, Any]) -> RegimeState:
        """
        Full rank-based regime classifier.

        Steps 1-5 as documented in module docstring.
        Returns a RegimeState with leading != lagging (except genuine confused).
        """
        # ── Step 1+2 — Momentum + Ranks ─────────────────────────────────────────
        momentum_scores: Dict[str, float] = {
            s: self._compute_momentum(s, candle_buffers)
            for s in self.symbols
        }

        if not momentum_scores:
            return RegimeState(
                regime="confused", leading_category="none",
                lagging_category="none", dispersion=0.0, confidence=0.1,
            )

        sorted_assets = sorted(
            momentum_scores.items(), key=lambda x: x[1], reverse=True
        )
        ranks: Dict[str, int] = {
            asset: rank + 1
            for rank, (asset, _) in enumerate(sorted_assets)
        }

        # ── Step 3 — Dispersion ──────────────────────────────────────────────────
        values   = list(momentum_scores.values())
        mean_m   = sum(values) / len(values)
        variance = (
            sum((v - mean_m) ** 2 for v in values) / len(values)
            if len(values) >= 2 else 0.0
        )
        dispersion = variance ** 0.5

        # ── Step 4 — Funding bias ────────────────────────────────────────────────
        funding_bias = 0.0
        if self.funding_radar is not None:
            funding_scores: Dict[str, float] = {}
            for s in self.symbols:
                try:
                    snap = self.funding_radar.build_snapshot(s)
                    if snap is not None:
                        funding_scores[s] = snap.carry_score
                except Exception:
                    pass
            if funding_scores:
                funding_bias = sum(funding_scores.values()) / len(funding_scores)

        # ── Step 5 — Classify ────────────────────────────────────────────────────
        regime_state = self._classify_regime(
            ranks=ranks,
            momentum_scores=momentum_scores,
            dispersion=dispersion,
            funding_bias=funding_bias,
        )

        # Populate backward-compat fields
        regime_state.asset_scores    = momentum_scores
        regime_state.category_scores = self._compute_category_scores(momentum_scores)

        logger.info(
            "regime_calculated",
            regime=regime_state.regime,
            leading=regime_state.leading_category,
            lagging=regime_state.lagging_category,
            dispersion=round(dispersion, 6),
            confidence=round(regime_state.confidence, 3),
            funding_bias=round(funding_bias, 4),
            n_assets=len(ranks),
        )
        return regime_state

    def get_regime(self, candle_buffers: Dict[str, Any]) -> RegimeState:
        """Public alias — identical to compute_regime()."""
        return self.compute_regime(candle_buffers)

    # ── Internal classifier ─────────────────────────────────────────────────────

    def _classify_regime(
        self,
        ranks:           Dict[str, int],
        momentum_scores: Dict[str, float],
        dispersion:      float,
        funding_bias:    float,
    ) -> RegimeState:
        """
        Classify market regime from rank structure.
        Conditions are evaluated in strict priority order — do NOT reorder.
        """
        n   = len(ranks)
        mid = max(1, n // 2)

        # ── Safe rank lookups ────────────────────────────────────────────────────
        btc_rank    = ranks.get("BTC-USD", 4)
        eth_rank    = ranks.get("ETH-USD", 4)
        xaut_rank   = ranks.get("XAUT-USD", 4)
        # High default (8) prevents tech_led / mag7_led from firing when
        # USTECH is not in the universe.
        ustech_rank = ranks.get("USTECH-USD", 8)
        bnb_rank    = ranks.get("BNB-USD", 4)
        link_rank   = ranks.get("LINK-USD", 4)

        sol_rank  = ranks.get("SOL-USD",  mid)
        avax_rank = ranks.get("AVAX-USD", mid)
        arb_rank  = ranks.get("ARB-USD",  mid)
        near_rank = ranks.get("NEAR-USD", mid)

        alt_rank_values = [r for r in [sol_rank, avax_rank, arb_rank, near_rank] if r]
        avg_alt_rank = (
            sum(alt_rank_values) / len(alt_rank_values)
            if alt_rank_values else float(mid)
        )

        btc_mom  = momentum_scores.get("BTC-USD",  0.0)

        # ── Derive leading/lagging category labels ───────────────────────────────
        top_assets    = [a for a, r in ranks.items() if r <= 3]
        bottom_assets = [a for a, r in ranks.items() if r >= n - 2]

        def most_common_cat(assets: List[str]) -> str:
            cats = [ASSET_CATEGORIES.get(a, "unknown") for a in assets]
            if not cats:
                return "unknown"
            return max(set(cats), key=cats.count)

        leading_cat = most_common_cat(top_assets)
        lagging_cat = most_common_cat(bottom_assets)

        # ── Commodity subcategory rank averages ──────────────────────────────────
        # Defaults to mid when the asset is absent — conditions won't fire falsely.
        precious_ranks   = [ranks.get(s, mid) for s in self.symbols
                            if ASSET_CATEGORIES.get(s) == "commodity_precious"]
        industrial_ranks = [ranks.get(s, mid) for s in self.symbols
                            if ASSET_CATEGORIES.get(s) == "commodity_industrial"]
        energy_ranks     = [ranks.get(s, mid) for s in self.symbols
                            if ASSET_CATEGORIES.get(s) == "commodity_energy"]

        # Default n+1 when category absent from universe: makes "must-lead" checks
        # (avg <= threshold) safely FALSE — prevents phantom regime classification
        # when there are no commodity assets to observe.
        precious_avg   = sum(precious_ranks)   / len(precious_ranks)   if precious_ranks   else float(n + 1)
        industrial_avg = sum(industrial_ranks) / len(industrial_ranks) if industrial_ranks else float(n + 1)
        energy_avg     = sum(energy_ranks)     / len(energy_ranks)     if energy_ranks     else float(n + 1)

        # ── MAG7 SSI score ───────────────────────────────────────────────────────
        mag7_ssi_score = 0.0
        if self.ssi_engine is not None:
            try:
                mag7_ssi_score = float(self.ssi_engine.get_latest_score())
            except Exception:
                mag7_ssi_score = 0.0

        # ── Guard: rank spread < 2 → genuinely confused ──────────────────────────
        rank_spread = max(ranks.values()) - min(ranks.values())
        if rank_spread < 2:
            return RegimeState(
                regime="confused",
                leading_category="none",
                lagging_category="none",
                dispersion=dispersion,
                confidence=0.1,
            )

        # ════════════════════════════════════════════════════════════════════════
        # ORDERED CONDITIONS — do NOT reorder
        # New macro regimes (geopolitical/stagflation/growth) placed first so
        # commodity leadership is identified before crypto-internal regimes.
        # ════════════════════════════════════════════════════════════════════════

        # ── GEOPOLITICAL_STRESS ──────────────────────────────────────────────────
        # Energy + precious both leading while crypto lags.
        # Requires BOTH energy and precious assets in universe — won't fire on
        # absent categories (energy/precious default to n+1, safely above threshold).
        # Trade implication: reduce crypto longs, XAUT arb opportunity.
        if (energy_ranks and precious_ranks
                and energy_avg <= 3 and precious_avg <= 3 and btc_rank >= mid):
            return RegimeState(
                regime="geopolitical_stress",
                leading_category="commodity_energy",
                lagging_category="large_cap",
                dispersion=dispersion,
                confidence=0.75,
            )

        # ── STAGFLATION_FEAR ─────────────────────────────────────────────────────
        # Gold leads (inflation hedge) while industrial metals lag (demand fear).
        # Requires precious AND industrial in universe — the lagging check
        # (industrial_avg >= n-2) would spuriously fire on n+1 sentinel otherwise.
        # Trade implication: XAUT long setup, avoid alts entirely.
        if (precious_ranks and industrial_ranks
                and precious_avg <= 3 and industrial_avg >= n - 2 and energy_avg <= mid):
            return RegimeState(
                regime="stagflation_fear",
                leading_category="commodity_precious",
                lagging_category="commodity_industrial",
                dispersion=dispersion,
                confidence=0.70,
            )

        # ── GROWTH_EXPANSION ─────────────────────────────────────────────────────
        # Industrial metals lead (demand signal), gold lags, BTC in top half.
        # Requires industrial assets in universe (industrial_avg default n+1 already
        # prevents firing, but explicit guard makes intent clear).
        # Trade implication: risk-on, alts valid, BTC long bias.
        if (industrial_ranks
                and industrial_avg <= 3 and precious_avg >= mid and btc_rank <= 4):
            return RegimeState(
                regime="growth_expansion",
                leading_category="commodity_industrial",
                lagging_category="commodity_precious",
                dispersion=dispersion,
                confidence=0.70,
            )

        # ── RISK_ON ──────────────────────────────────────────────────────────────
        # BTC leading, gold lagging, funding net positive, dispersion elevated.
        if (btc_rank <= 3
                and xaut_rank > mid
                and dispersion > 0.002
                and funding_bias > 0.5):
            return RegimeState(
                regime="risk_on",
                leading_category="large_cap",
                lagging_category=lagging_cat,
                dispersion=dispersion,
                confidence=min(1.0, dispersion * 200 + funding_bias * 0.2),
            )

        # ── RISK_OFF ─────────────────────────────────────────────────────────────
        # Gold top-2, BTC past midpoint, funding net negative.
        if (xaut_rank <= 2
                and btc_rank > mid
                and funding_bias < -0.5):
            return RegimeState(
                regime="risk_off",
                leading_category="commodity_precious",
                lagging_category="large_cap",
                dispersion=dispersion,
                confidence=0.8,
            )

        # ── BTC_DOMINANCE ────────────────────────────────────────────────────────
        # BTC top-2, ETH struggling past midpoint, BTC actively moving up.
        if (btc_rank <= 2
                and eth_rank >= mid
                and btc_mom > 0.003):
            return RegimeState(
                regime="btc_dominance",
                leading_category="large_cap",
                lagging_category="alt_l1",
                dispersion=dispersion,
                confidence=0.7,
            )

        # ── ALT_SEASON ───────────────────────────────────────────────────────────
        # Alt basket outpacing BTC, dispersion elevated, BTC momentum flat.
        if (avg_alt_rank < btc_rank
                and dispersion > 0.004
                and btc_mom < 0.002):
            return RegimeState(
                regime="alt_season",
                leading_category="alt_l1",
                lagging_category="large_cap",
                dispersion=dispersion,
                confidence=min(1.0, dispersion * 150),
            )

        # ── TECH_LED ─────────────────────────────────────────────────────────────
        # Equity tech index top-3, BTC also in top-4, funding positive.
        if (ustech_rank <= 3
                and btc_rank <= 4
                and funding_bias > 0):
            return RegimeState(
                regime="tech_led",
                leading_category="index_tech",
                lagging_category="commodity_precious",
                dispersion=dispersion,
                confidence=0.65,
            )

        # ── MAG7_LED ─────────────────────────────────────────────────────────────
        # USTECH top-2 + high SSI institutional inflow → BTC long lag signal.
        # Also trade MAG7SSI directly if available.
        if (ustech_rank <= 2
                and mag7_ssi_score > 0.6
                and btc_rank <= 5):
            return RegimeState(
                regime="mag7_led",
                leading_category="index_tech",
                lagging_category="commodity_precious",
                dispersion=dispersion,
                confidence=0.70,
            )

        # ── DEFI_STRESS ──────────────────────────────────────────────────────────
        # DeFi infra (LINK) in the bottom-1 slot while BTC holds the mid-pack.
        if (link_rank >= n - 1
                and btc_rank <= mid):
            return RegimeState(
                regime="defi_stress",
                leading_category="large_cap",
                lagging_category="defi_infra",
                dispersion=dispersion,
                confidence=0.6,
            )

        # ── CEX_FLOW ─────────────────────────────────────────────────────────────
        # BNB top-2 → exchange volume rotation signal.
        if bnb_rank <= 2:
            return RegimeState(
                regime="cex_flow",
                leading_category="cex_ecosystem",
                lagging_category="alt_l1",
                dispersion=dispersion,
                confidence=0.6,
            )

        # ── TRANSITIONING ────────────────────────────────────────────────────────
        # Rank spread exists but no clear macro pattern. Leading/lagging are
        # derived from actual top/bottom assets so they are NEVER identical.
        # This replaces the broken "confused" default from v1.2.
        return RegimeState(
            regime="transitioning",
            leading_category=leading_cat,
            lagging_category=lagging_cat,
            dispersion=dispersion,
            confidence=0.3,
        )

    # ── Helpers ─────────────────────────────────────────────────────────────────

    def _compute_category_scores(
        self, momentum_scores: Dict[str, float]
    ) -> Dict[str, float]:
        """
        Category-level momentum averages for backward compatibility with
        market_context.py's spread-based confidence estimation.
        """
        totals: Dict[str, List[float]] = {}
        for symbol, score in momentum_scores.items():
            cat = ASSET_CATEGORIES.get(symbol, "unknown")
            totals.setdefault(cat, []).append(score)
        return {
            cat: sum(scores) / len(scores)
            for cat, scores in totals.items()
        }
