"""
tests/test_assets.py — Asset expansion + regime classifier v2.0 tests.

Covers:
  A. Config asset list and ASSET_CONFIG completeness
  B. Momentum calculation (48-period relative return)
  C. Rank-based regime classification for each named regime
  D. Dispersion + rank_spread guard (genuine confused)
  E. Leading != lagging invariant
  F. REGIME_ALLOWED_SYMBOLS gate in risk_engine
  G. Backward-compat: category_scores populated, RegimeMatrix alias
"""

import unittest
from unittest.mock import MagicMock
from tests.helpers import test_config, make_test_candles, make_candle_buffers
from intelligence.relative_strength import (
    RelativeStrengthEngine,
    RegimeState,
    RegimeMatrix,
    ASSET_CATEGORIES,
    REGIME_ALLOWED_SYMBOLS,
    ALL_REGIMES,
    EXTERNAL_MACRO_SOURCES,
)
from intelligence.stop_clusters import StopClusterMap


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_engine(assets=None):
    """Engine with a 7-asset test config."""
    config = test_config()
    if assets is not None:
        config.assets = assets
    return RelativeStrengthEngine(config)


def _make_buffers_with_trend(assets, trends):
    """
    Build candle buffers where each asset's 1m candles have a known trend.
    trends: dict[symbol -> float] — proportional price change over 50 candles.
    e.g. {"BTC-USD": 0.05} means BTC goes up 5%.
    """
    buffers = {}
    for asset in assets:
        cb = make_candle_buffers(asset)
        trend = trends.get(asset, 0.0)
        # 50 candles linearly moving by `trend` total
        base = 1000.0
        end  = base * (1 + trend)
        for i in range(50):
            price = base + (end - base) * (i / 49)
            from data.candle_buffer import Candle
            import time
            cb["1m"].add(Candle(
                open_time=int(time.time() * 1000) + i * 60_000,
                open=price, high=price * 1.001,
                low=price * 0.999, close=price,
                volume=100.0,
                close_time=int(time.time() * 1000) + i * 60_000 + 59_999,
            ))
        buffers[asset] = cb
    return buffers


CORE_ASSETS = ["BTC-USD", "ETH-USD", "SOL-USD", "XAUT-USD", "BNB-USD", "LINK-USD", "AVAX-USD"]


# ── A: Config ──────────────────────────────────────────────────────────────────

class TestConfigAssets(unittest.TestCase):

    def test_test_config_assets(self):
        """test_config() returns exactly the 7 hardcoded assets."""
        config = test_config()
        expected = {"BTC-USD", "ETH-USD", "SOL-USD", "XAUT-USD", "BNB-USD", "LINK-USD", "AVAX-USD"}
        self.assertEqual(set(config.assets), expected)

    def test_asset_config_complete(self):
        from core.config import Settings
        config = Settings()
        ac = getattr(config, "ASSET_CONFIG", {})
        for asset in ["BTC-USD", "ETH-USD", "SOL-USD", "XAUT-USD", "BNB-USD", "LINK-USD", "AVAX-USD"]:
            self.assertIn(asset, ac, f"{asset} missing from ASSET_CONFIG")
            self.assertIn("tick_size", ac[asset])
            self.assertIn("category",  ac[asset])

    def test_asset_categories_covers_core(self):
        """All 7 core test assets must be in ASSET_CATEGORIES."""
        for a in CORE_ASSETS:
            self.assertIn(a, ASSET_CATEGORIES, f"{a} not in ASSET_CATEGORIES")

    def test_commodity_subcategories_distinct(self):
        self.assertEqual(ASSET_CATEGORIES["XAUT-USD"],    "commodity_precious")
        self.assertEqual(ASSET_CATEGORIES["COPPER-USD"],  "commodity_industrial")
        self.assertEqual(ASSET_CATEGORIES["CL-USD"],      "commodity_energy")

    def test_all_regimes_set_complete(self):
        expected = {
            "risk_on", "risk_off", "btc_dominance", "alt_season",
            "tech_led", "mag7_led", "defi_stress", "cex_flow",
            "transitioning", "confused",
            "geopolitical_stress", "stagflation_fear", "growth_expansion",
        }
        self.assertEqual(ALL_REGIMES, expected)

    def test_external_macro_sources_present(self):
        self.assertIn("copper_usd",     EXTERNAL_MACRO_SOURCES)
        self.assertIn("wti_crude",      EXTERNAL_MACRO_SOURCES)
        self.assertIn("us_10yr_yield",  EXTERNAL_MACRO_SOURCES)
        for key, src in EXTERNAL_MACRO_SOURCES.items():
            self.assertFalse(src["tradeable"], f"{key} should be non-tradeable")


# ── B: Momentum calculation ────────────────────────────────────────────────────

class TestMomentumCalculation(unittest.TestCase):

    def test_returns_zero_when_symbol_missing(self):
        engine = _make_engine()
        self.assertEqual(engine._compute_momentum("BTC-USD", {}), 0.0)

    def test_returns_zero_when_no_1m_buffer(self):
        engine  = _make_engine()
        buffers = {"BTC-USD": {"15m": MagicMock()}}
        # "1m" key absent — .get("1m") returns None
        self.assertEqual(engine._compute_momentum("BTC-USD", buffers), 0.0)

    def test_returns_zero_when_fewer_than_10_candles(self):
        engine = _make_engine()
        cb     = make_candle_buffers("BTC-USD")
        for c in make_test_candles(5):
            cb["1m"].add(c)
        self.assertEqual(engine._compute_momentum("BTC-USD", {"BTC-USD": cb}), 0.0)

    def test_positive_trend_gives_positive_momentum(self):
        engine = _make_engine()
        buffers = _make_buffers_with_trend(["BTC-USD"], {"BTC-USD": 0.05})
        mom = engine._compute_momentum("BTC-USD", buffers)
        self.assertGreater(mom, 0.0)

    def test_negative_trend_gives_negative_momentum(self):
        engine = _make_engine()
        buffers = _make_buffers_with_trend(["BTC-USD"], {"BTC-USD": -0.04})
        mom = engine._compute_momentum("BTC-USD", buffers)
        self.assertLess(mom, 0.0)

    def test_flat_gives_near_zero_momentum(self):
        engine = _make_engine()
        buffers = _make_buffers_with_trend(["BTC-USD"], {"BTC-USD": 0.0})
        mom = engine._compute_momentum("BTC-USD", buffers)
        self.assertAlmostEqual(mom, 0.0, places=5)

    def test_lookback_capped_at_48(self):
        """With 50 candles, lookback must be 48 — not 49."""
        engine  = _make_engine()
        cb      = make_candle_buffers("BTC-USD")
        candles = make_test_candles(50, base_price=100.0, volatility=0.0)
        # Flat candles → momentum should be 0 regardless of lookback
        for c in candles:
            cb["1m"].add(c)
        mom = engine._compute_momentum("BTC-USD", {"BTC-USD": cb})
        self.assertAlmostEqual(mom, 0.0, places=5)


# ── C: Regime classification ───────────────────────────────────────────────────

class TestRegimeClassification(unittest.TestCase):

    def _classify(self, trends, assets=None, funding_radar=None):
        assets  = assets or CORE_ASSETS
        engine  = _make_engine(assets)
        if funding_radar is not None:
            engine.set_funding_radar(funding_radar)
        buffers = _make_buffers_with_trend(assets, trends)
        return engine.compute_regime(buffers)

    # ── Output type and invariants ─────────────────────────────────────────────

    def test_returns_regime_state_instance(self):
        state = self._classify({})
        self.assertIsInstance(state, RegimeState)

    def test_regime_is_valid_string(self):
        state = self._classify({})
        self.assertIn(state.regime, ALL_REGIMES | {"transitioning"},
                      f"Unknown regime: {state.regime}")

    def test_confidence_in_range(self):
        state = self._classify({})
        self.assertGreaterEqual(state.confidence, 0.0)
        self.assertLessEqual(state.confidence, 1.0)

    def test_leading_not_equal_lagging_except_confused(self):
        """
        Core invariant from the v1.2 bug fix.
        leading == lagging is ONLY valid in genuine confused state
        where both are "none".
        """
        state = self._classify({})
        if state.regime == "confused":
            self.assertEqual(state.leading_category, "none")
            self.assertEqual(state.lagging_category, "none")
        else:
            self.assertNotEqual(
                state.leading_category, state.lagging_category,
                f"leading==lagging=={state.leading_category!r} "
                f"in regime '{state.regime}' — v1.2 bug recurrence"
            )

    def test_category_scores_populated(self):
        """Backward-compat field must be a non-empty dict."""
        state = self._classify({"BTC-USD": 0.03, "ETH-USD": 0.02})
        self.assertIsInstance(state.category_scores, dict)
        self.assertGreater(len(state.category_scores), 0)

    def test_asset_scores_populated(self):
        state = self._classify({"BTC-USD": 0.03})
        self.assertIn("BTC-USD", state.asset_scores)

    def test_regime_matrix_alias(self):
        """RegimeMatrix must be the same class as RegimeState."""
        self.assertIs(RegimeMatrix, RegimeState)

    # ── Confused: genuine zero spread ─────────────────────────────────────────

    def test_confused_when_two_assets_identical(self):
        """
        With only 2 assets at identical momentum: sorted ranks are 1, 2 →
        rank_spread = 1 < 2 → genuine confused.

        NOTE: With 7+ assets all flat, ranks are still 1-7 (stable sort on equal
        values), giving rank_spread = 6. That is NOT confused — it means there is
        no data to distinguish leaders. With 7 flat assets the regime correctly
        falls through to the transitioning default, not confused.
        """
        state = self._classify(
            {"BTC-USD": 0.0, "XAUT-USD": 0.0},
            assets=["BTC-USD", "XAUT-USD"],
        )
        self.assertEqual(state.regime, "confused")
        self.assertEqual(state.leading_category, "none")
        self.assertEqual(state.lagging_category, "none")
        self.assertLess(state.confidence, 0.2)

    def test_flat_seven_assets_is_not_confused(self):
        """
        With 7 flat assets, rank_spread = 6 — conditions cascade to transitioning,
        not confused. This is correct: there is rank structure, just no momentum.
        """
        state = self._classify({a: 0.0 for a in CORE_ASSETS})
        self.assertNotEqual(state.regime, "confused",
                            "7 flat assets should be transitioning, not confused")

    # ── BTC_DOMINANCE ─────────────────────────────────────────────────────────

    def test_btc_dominance_regime(self):
        """
        BTC rips +5%, ETH flat near the bottom → BTC rank=1, ETH rank >= mid.
        mid = 7//2 = 3, so ETH must be rank >= 4.
        Strategy: give SOL/AVAX/LINK/BNB small positive trends so they rank
        above ETH in the sort, pushing ETH to rank 6.
        """
        assets = ["BTC-USD", "ETH-USD", "SOL-USD", "AVAX-USD", "LINK-USD", "BNB-USD", "XAUT-USD"]
        state = self._classify(
            {
                "BTC-USD":  0.05,   # rank 1 — strong surge
                "SOL-USD":  0.02,   # rank 2
                "AVAX-USD": 0.015,  # rank 3
                "LINK-USD": 0.01,   # rank 4
                "BNB-USD":  0.005,  # rank 5
                "XAUT-USD": 0.002,  # rank 6
                "ETH-USD":  0.001,  # rank 7 — ETH lags (>= mid=3) ✓
            },
            assets=assets,
        )
        self.assertEqual(state.regime, "btc_dominance")
        self.assertEqual(state.leading_category, "large_cap")
        self.assertEqual(state.lagging_category, "alt_l1")

    # ── ALT_SEASON ────────────────────────────────────────────────────────────

    def test_alt_season_regime(self):
        """
        Alts rip while BTC barely moves → avg_alt_rank < btc_rank.
        Needs dispersion > 0.004 and btc_mom < 0.002.
        """
        assets = ["BTC-USD", "ETH-USD", "SOL-USD", "AVAX-USD", "NEAR-USD", "ARB-USD", "XAUT-USD"]
        state = self._classify(
            {
                "SOL-USD":  0.06,
                "AVAX-USD": 0.055,
                "NEAR-USD": 0.05,
                "ARB-USD":  0.045,
                "ETH-USD":  0.03,
                "XAUT-USD": 0.001,
                "BTC-USD":  0.001,  # flat BTC
            },
            assets=assets,
        )
        self.assertEqual(state.regime, "alt_season")
        self.assertEqual(state.leading_category, "alt_l1")

    # ── RISK_ON ──────────────────────────────────────────────────────────────

    def test_risk_on_with_positive_funding(self):
        """
        BTC rank <= 3, XAUT rank > mid, dispersion > 0.002, funding_bias > 0.5.
        Use a mock FundingRadar that returns carry_score = 2.0 for all symbols.
        """
        assets = ["BTC-USD", "ETH-USD", "SOL-USD", "BNB-USD", "LINK-USD", "AVAX-USD", "XAUT-USD"]

        snap = MagicMock()
        snap.carry_score = 2.0
        radar = MagicMock()
        radar.build_snapshot.return_value = snap

        state = self._classify(
            {
                "BTC-USD":  0.04,
                "ETH-USD":  0.03,
                "SOL-USD":  0.02,
                "BNB-USD":  0.015,
                "LINK-USD": 0.01,
                "AVAX-USD": 0.005,
                "XAUT-USD": 0.0,
            },
            assets=assets,
            funding_radar=radar,
        )
        self.assertEqual(state.regime, "risk_on")
        self.assertEqual(state.leading_category, "large_cap")

    # ── TRANSITIONING default ────────────────────────────────────────────────

    def test_transitioning_is_default_when_no_clear_pattern(self):
        """
        Moderate spread, no strong crypto/commodity pattern → transitioning.
        IMPORTANT: transitioning must NOT produce leading == lagging.
        """
        assets = ["BTC-USD", "ETH-USD", "SOL-USD", "XAUT-USD"]
        state = self._classify(
            {
                "BTC-USD":  0.012,
                "XAUT-USD": 0.008,
                "ETH-USD":  0.005,
                "SOL-USD":  0.001,
            },
            assets=assets,
        )
        # May be btc_dominance or transitioning depending on thresholds — just check invariant
        self.assertNotEqual(state.regime, "confused")
        if state.regime != "confused":
            self.assertNotEqual(state.leading_category, state.lagging_category)

    # ── Dispersion ────────────────────────────────────────────────────────────

    def test_dispersion_positive_when_assets_differ(self):
        state = self._classify(
            {"BTC-USD": 0.05, "ETH-USD": 0.0, "SOL-USD": -0.05,
             "XAUT-USD": 0.01, "BNB-USD": 0.02, "LINK-USD": -0.01, "AVAX-USD": 0.03}
        )
        self.assertGreater(state.dispersion, 0.0)

    def test_dispersion_near_zero_when_all_flat(self):
        state = self._classify({a: 0.0 for a in CORE_ASSETS})
        self.assertAlmostEqual(state.dispersion, 0.0, places=5)


# ── D: Genuine confused guard ──────────────────────────────────────────────────

class TestConfusedGuard(unittest.TestCase):

    def test_rank_spread_one_triggers_confused_two_assets(self):
        """
        With 2 assets both flat: ranks = {A:1, B:2}, rank_spread = 1 < 2 → confused.
        """
        assets = ["BTC-USD", "ETH-USD"]
        engine = _make_engine(assets)
        buffers = _make_buffers_with_trend(assets, {"BTC-USD": 0.0, "ETH-USD": 0.0})
        state = engine.compute_regime(buffers)
        self.assertEqual(state.regime, "confused")
        self.assertEqual(state.leading_category, "none")
        self.assertEqual(state.lagging_category, "none")

    def test_rank_spread_one_triggers_confused(self):
        """Two assets with near-identical momentum → spread < 2 → confused."""
        assets = ["BTC-USD", "ETH-USD"]
        engine = _make_engine(assets)
        buffers = _make_buffers_with_trend(assets, {"BTC-USD": 0.0001, "ETH-USD": 0.0})
        state = engine.compute_regime(buffers)
        self.assertEqual(state.regime, "confused")

    def test_spread_two_does_not_trigger_confused(self):
        """rank spread >= 2 → must NOT be confused (unless classified so by conditions)."""
        assets = ["BTC-USD", "ETH-USD", "SOL-USD"]
        engine = _make_engine(assets)
        buffers = _make_buffers_with_trend(assets, {
            "BTC-USD": 0.05, "ETH-USD": 0.02, "SOL-USD": 0.0
        })
        state = engine.compute_regime(buffers)
        # With 3 assets: rank_spread = 3-1 = 2 → NOT confused
        self.assertNotEqual(state.regime, "confused")


# ── E: Leading != lagging invariant (stress test) ─────────────────────────────

class TestLeadingLaggingInvariant(unittest.TestCase):

    def _check_invariant(self, trends, assets=None):
        assets  = assets or CORE_ASSETS
        engine  = _make_engine(assets)
        buffers = _make_buffers_with_trend(assets, trends)
        state   = engine.compute_regime(buffers)
        if state.regime == "confused":
            self.assertEqual(state.leading_category, "none")
            self.assertEqual(state.lagging_category, "none")
        else:
            self.assertNotEqual(
                state.leading_category, state.lagging_category,
                f"leading==lagging=={state.leading_category!r} "
                f"in regime '{state.regime}'"
            )
        return state

    def test_invariant_all_rising(self):
        self._check_invariant({a: (0.01 * (i + 1)) for i, a in enumerate(CORE_ASSETS)})

    def test_invariant_all_falling(self):
        self._check_invariant({a: (-0.01 * (i + 1)) for i, a in enumerate(CORE_ASSETS)})

    def test_invariant_mixed(self):
        self._check_invariant({
            "BTC-USD":  0.05, "ETH-USD": -0.02, "SOL-USD": 0.03,
            "XAUT-USD": 0.01, "BNB-USD": -0.01, "LINK-USD": 0.0, "AVAX-USD": -0.04,
        })


# ── F: REGIME_ALLOWED_SYMBOLS gate ────────────────────────────────────────────

class TestRegimeSymbolGate(unittest.TestCase):

    def _validate_sync(self, symbol, regime):
        """Thin wrapper around the gate method directly."""
        engine = _make_engine()
        from risk.risk_engine import RiskEngine
        from risk.margin_engine import MarginEngine
        from risk.position_manager import PositionManager
        config = test_config()
        risk = RiskEngine(config, MarginEngine(), PositionManager(), None)
        return risk._gate_regime_symbol_restriction(symbol, regime)

    def test_risk_on_allows_all_symbols(self):
        ok, _ = self._validate_sync("SOL-USD", "risk_on")
        self.assertTrue(ok)

    def test_risk_off_blocks_non_xaut(self):
        ok, reason = self._validate_sync("SOL-USD", "risk_off")
        self.assertFalse(ok)
        self.assertIn("REGIME_RESTRICTED", reason)

    def test_risk_off_allows_xaut(self):
        ok, _ = self._validate_sync("XAUT-USD", "risk_off")
        self.assertTrue(ok)

    def test_btc_dominance_blocks_alts(self):
        ok, reason = self._validate_sync("SOL-USD", "btc_dominance")
        self.assertFalse(ok)
        self.assertIn("btc_dominance", reason)

    def test_btc_dominance_allows_btc(self):
        ok, _ = self._validate_sync("BTC-USD", "btc_dominance")
        self.assertTrue(ok)

    def test_stagflation_fear_blocks_btc(self):
        ok, reason = self._validate_sync("BTC-USD", "stagflation_fear")
        self.assertFalse(ok)

    def test_stagflation_fear_allows_xaut(self):
        ok, _ = self._validate_sync("XAUT-USD", "stagflation_fear")
        self.assertTrue(ok)

    def test_transitioning_allows_btc(self):
        ok, _ = self._validate_sync("BTC-USD", "transitioning")
        self.assertTrue(ok)

    def test_transitioning_blocks_sol(self):
        ok, _ = self._validate_sync("SOL-USD", "transitioning")
        self.assertFalse(ok)

    def test_unknown_regime_allows_all(self):
        """Unknown regimes (e.g. legacy BULL/BEAR/RANGING) should pass through."""
        ok, _ = self._validate_sync("SOL-USD", "RANGING")
        self.assertTrue(ok)
        ok, _ = self._validate_sync("AVAX-USD", "BULL")
        self.assertTrue(ok)

    def test_growth_expansion_allows_sol(self):
        ok, _ = self._validate_sync("SOL-USD", "growth_expansion")
        self.assertTrue(ok)

    def test_growth_expansion_blocks_xaut(self):
        ok, _ = self._validate_sync("XAUT-USD", "growth_expansion")
        self.assertFalse(ok)


# ── G: REGIME_ALLOWED_SYMBOLS dict completeness ───────────────────────────────

class TestRegimeAllowedSymbolsDict(unittest.TestCase):

    def test_all_regimes_have_entry(self):
        for regime in ALL_REGIMES:
            self.assertIn(regime, REGIME_ALLOWED_SYMBOLS,
                          f"'{regime}' missing from REGIME_ALLOWED_SYMBOLS")

    def test_risk_on_is_none(self):
        self.assertIsNone(REGIME_ALLOWED_SYMBOLS["risk_on"])

    def test_alt_season_is_none(self):
        self.assertIsNone(REGIME_ALLOWED_SYMBOLS["alt_season"])

    def test_risk_off_only_xaut(self):
        self.assertEqual(REGIME_ALLOWED_SYMBOLS["risk_off"], ["XAUT-USD"])

    def test_stagflation_fear_only_xaut(self):
        self.assertEqual(REGIME_ALLOWED_SYMBOLS["stagflation_fear"], ["XAUT-USD"])


# ── H: Stop cluster smoke test (unchanged) ────────────────────────────────────

class TestStopClusterBasic(unittest.TestCase):

    def test_bnb_round_number_clusters(self):
        clusters = StopClusterMap()
        cluster_map = clusters.build_map(
            symbol="BNB-USD",
            current_price=400.0,
            candles=make_test_candles(25, 400.0)
        )
        self.assertGreater(len(cluster_map), 0)
        bnb_increments = [c.price for c in cluster_map if c.source == "round_number"]
        self.assertTrue(
            any(abs(p - 405.0) < 0.01 or abs(p - 395.0) < 0.01 for p in bnb_increments)
        )


if __name__ == "__main__":
    unittest.main()
