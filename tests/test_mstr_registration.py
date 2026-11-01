"""
tests/test_mstr_registration.py — MSTR-USD registration pins, ARCUS ERA.

Governor directive 2026-09-26: register MSTR-USD (MicroStrategy perp on
SoDEX) as a fully tradeable equity perp. Proven live: SoDEX lists it — the
operator traded it manually and the bot's SoDEX positions poll observed
"MSTR-USD" long 1.759 @ ~159.5 on 2026-09-25; non-universe meant
observed-only (never adopted/managed).

Re-encoded 2026-11-01 (Governor stock-migration order): MSTR is now
ARCUS-ROUTED (arcus_assets) and REMOVED from the SoDEX execution-facing
maps (config.assets, sodex_kline_assets, TRADFI_SYMBOLS,
TRADFI_SINGLE_NAMES) so the execution/venue.py:76 net-new guard lets it
route to Arcus. Pure reference maps (ASSET_CONFIG, MIN_STOP_DISTANCE_PCT,
step/precision, SODEX_SUPPORTED seed list, atr_min_pct, 24h override,
asset class, RS category, colony subfamily) are kept vestigial — zero
behavior for a non-universe SoDEX symbol, and the pins preserve the
registration record.

Template: 2026-09-11 equity-perp expansion (f028d6b) 10-map registration
pattern + 2026-09-22 AMD/DRAM add. Leverage 5/5 — high-vol single-name
(leveraged BTC proxy), UNITREE/DRAM class, NOT the 7/7 mega-cap class.
"""

import unittest


class TestMstrUniverseMembership(unittest.TestCase):
    """Arcus-era membership: routed to Arcus, off the SoDEX trade universe."""

    def setUp(self):
        from core.config import Settings
        self.cfg = Settings()

    def test_mstr_in_arcus_assets(self):
        # 2026-11-01 stock migration — MSTR trades on Arcus now.
        self.assertIn("MSTR-USD", self.cfg.arcus_assets)

    def test_mstr_not_in_assets(self):
        # Removed from config.assets so the venue.py:76 net-new guard lets
        # it route to Arcus (config.assets membership blocks Arcus routing).
        self.assertNotIn("MSTR-USD", self.cfg.assets)

    def test_mstr_in_tradfi_assets(self):
        # HTF counter-trend gate skip list — vestigial for an Arcus symbol,
        # kept per the map-disposition doctrine (reference data, zero
        # behavior off the SoDEX universe).
        self.assertIn("MSTR-USD", self.cfg.TRADFI_ASSETS)

    def test_mstr_in_tier_b(self):
        self.assertIn("MSTR-USD", self.cfg.TIER_B_ASSETS)

    def test_mstr_asset_config(self):
        ac = self.cfg.ASSET_CONFIG["MSTR-USD"]
        self.assertEqual(ac["tick_size"], 0.01)
        self.assertEqual(ac["min_size"], 0.001)
        self.assertEqual(ac["max_leverage"], 5)
        self.assertEqual(ac["preferred_leverage"], 5)
        self.assertEqual(ac["category"], "equity")
        self.assertEqual(ac["market_hours"], "24h")

    def test_mstr_min_stop(self):
        from core.config import MIN_STOP_DISTANCE_PCT
        self.assertEqual(MIN_STOP_DISTANCE_PCT["MSTR-USD"], 1.5)

    def test_mstr_step_and_precision(self):
        from core.config import SYMBOL_MIN_QUANTITY, SYMBOL_QTY_PRECISION
        self.assertEqual(SYMBOL_MIN_QUANTITY["MSTR-USD"], 0.001)
        self.assertEqual(SYMBOL_QTY_PRECISION["MSTR-USD"], 3)

    def test_mstr_not_in_sodex_kline_assets(self):
        # Off the SoDEX kline plane — Arcus venue-native feeds own its
        # books/marks now.
        self.assertNotIn("MSTR-USD", self.cfg.sodex_kline_assets)

    def test_mstr_in_sodex_supported(self):
        # Boot-seed list — vestigial (MSTR is no longer in config.assets so
        # nothing consumes its SoDEX candles), kept per the disposition
        # doctrine: pure reference data, zero behavior.
        from data.sodex_feed import SODEX_SUPPORTED
        self.assertIn("MSTR-USD", SODEX_SUPPORTED)

    def test_mstr_atr_map(self):
        # atr_min_pct equity-class floor (same 0.3 as every 09-11/09-22 add).
        self.assertIn("MSTR-USD", self.cfg.atr_min_pct)
        self.assertEqual(self.cfg.atr_min_pct["MSTR-USD"], 0.3)


class TestMstrTradfiFeed(unittest.TestCase):
    """Yahoo mapping + maker-only doctrine — both GONE in the Arcus era."""

    def test_mstr_not_in_tradfi_symbols(self):
        # No Yahoo polling or basis-guard writes for an Arcus-routed name.
        from data.tradfi_feed import TRADFI_SYMBOLS
        self.assertNotIn("MSTR-USD", TRADFI_SYMBOLS)

    def test_mstr_not_single_name_maker_only(self):
        # The SoDEX maker-only doctrine no longer applies — maker-first
        # doctrine lives on the Arcus side now.
        from data.tradfi_feed import TRADFI_SINGLE_NAMES
        self.assertNotIn("MSTR-USD", TRADFI_SINGLE_NAMES)


class TestMstrMarketHours(unittest.TestCase):
    """24/7 override — kept: Arcus reality is 24/7 wherever is_open is read."""

    def test_mstr_24h_override(self):
        from intelligence.market_hours import _SODEX_24H_OVERRIDE
        self.assertIn("MSTR-USD", _SODEX_24H_OVERRIDE)

    def test_mstr_market_hours_gate_open_overnight(self):
        from intelligence.market_hours import MarketHoursGate
        gate = MarketHoursGate()
        # 03:00 UTC Sunday — underlying NASDAQ closed, perp still trades.
        import datetime
        ts = datetime.datetime(2026, 9, 27, 3, 0, tzinfo=datetime.timezone.utc)
        self.assertTrue(gate.is_open("MSTR-USD", ts))


class TestMstrClassification(unittest.TestCase):
    """Asset class + relative-strength category."""

    def test_mstr_asset_class_equity(self):
        from core.asset_classes import ASSET_CLASS
        self.assertEqual(ASSET_CLASS["MSTR-USD"], "equity")

    def test_mstr_category_index_tech(self):
        from intelligence.relative_strength import ASSET_CATEGORIES
        self.assertEqual(ASSET_CATEGORIES["MSTR-USD"], "index_tech")

    def test_mstr_in_colony_crypto_adj(self):
        # Crypto-adjacent equity subfamily (COIN/HOOD class — BTC proxy).
        from intelligence.equity_colony import SUBFAMILIES
        self.assertIn("MSTR-USD", SUBFAMILIES["CRYPTO_ADJ"])


if __name__ == "__main__":
    unittest.main()
