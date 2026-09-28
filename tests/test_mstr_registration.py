"""
tests/test_mstr_registration.py — MSTR-USD full-universe registration pins.

Governor directive 2026-09-26: register MSTR-USD (MicroStrategy perp on
SoDEX) as a fully tradeable equity perp. Proven live: SoDEX lists it — the
operator traded it manually and the bot's SoDEX positions poll observed
"MSTR-USD" long 1.759 @ ~159.5 on 2026-09-25; non-universe meant
observed-only (never adopted/managed).

Template: 2026-09-11 equity-perp expansion (f028d6b) 10-map registration
pattern + 2026-09-22 AMD/DRAM add. Leverage 5/5 — high-vol single-name
(leveraged BTC proxy), UNITREE/DRAM class, NOT the 7/7 mega-cap class.
"""

import unittest


class TestMstrUniverseMembership(unittest.TestCase):
    """MSTR-USD present in every universe/registration map."""

    def setUp(self):
        from core.config import Settings
        self.cfg = Settings()

    def test_mstr_in_assets(self):
        self.assertIn("MSTR-USD", self.cfg.assets)

    def test_mstr_in_tradfi_assets(self):
        # HTF counter-trend gate skip list — BTC HTF is irrelevant for a
        # single-name equity perp (its own macro driver: BTC itself).
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

    def test_mstr_in_sodex_kline_assets(self):
        # Kline-owned from birth (the 09-11 wound class: Yahoo dies
        # overnight; the perp kline is the only honest 24/7 candle plane).
        self.assertIn("MSTR-USD", self.cfg.sodex_kline_assets)

    def test_mstr_in_sodex_supported(self):
        # Boot-seed list — 55 bars at boot, no cold-start ATR starvation.
        from data.sodex_feed import SODEX_SUPPORTED
        self.assertIn("MSTR-USD", SODEX_SUPPORTED)

    def test_mstr_atr_map(self):
        # atr_min_pct equity-class floor (same 0.3 as every 09-11/09-22 add).
        self.assertIn("MSTR-USD", self.cfg.atr_min_pct)
        self.assertEqual(self.cfg.atr_min_pct["MSTR-USD"], 0.3)


class TestMstrTradfiFeed(unittest.TestCase):
    """Yahoo mapping + maker-only single-name doctrine."""

    def test_mstr_tradfi_symbols_yahoo(self):
        from data.tradfi_feed import TRADFI_SYMBOLS
        self.assertEqual(TRADFI_SYMBOLS["MSTR-USD"], "MSTR")

    def test_mstr_single_name_maker_only(self):
        from data.tradfi_feed import TRADFI_SINGLE_NAMES
        self.assertIn("MSTR-USD", TRADFI_SINGLE_NAMES)


class TestMstrMarketHours(unittest.TestCase):
    """24/7 override — the SoDEX perp never closes."""

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
