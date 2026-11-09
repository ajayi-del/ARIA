"""Pins for 2026-11-10 mover-universe surgical registration.

Governor FIX: SNDK/MU/INTC/NBIS/MRVL/ZHIPU missing from ARIA universe while
prop SYMBOL_MAP already routed them to xyz: (turbo2). AMD was Arcus-only
with 0 signal_ready/outbox — restored to SoDEX peer path (DRAM/SKHX/LITE).
CBRS-USD is HL-listed (xyz:CBRS) but NOT SoDEX-listed → map-only; marks
plane handoff to sibling Arcus-dark→HL ship.
"""
from core.config import Settings, SYMBOL_MIN_QUANTITY, SYMBOL_QTY_PRECISION
from core.config import MIN_STOP_DISTANCE_PCT
from data.sodex_feed import SODEX_SUPPORTED
from data.tradfi_feed import TRADFI_SYMBOLS, TRADFI_SINGLE_NAMES
from prop_harness.io.signal_tap import SYMBOL_MAP
from prop_harness.families import family_of


SODEX_MOVERS = [
    "SNDK-USD", "MU-USD", "INTC-USD",
    "NBIS-USD", "MRVL-USD", "ZHIPU-USD", "AMD-USD",
]
OK_PEERS = ["DRAM-USD", "SKHX-USD", "LITE-USD", "AVAX-USD"]


def _cfg():
    return Settings()


class TestMoverUniverseMembership:
    def test_movers_in_assets_not_arcus(self):
        cfg = _cfg()
        for sym in SODEX_MOVERS:
            assert sym in cfg.assets, sym
            assert sym not in cfg.arcus_assets, sym

    def test_peers_unchanged(self):
        cfg = _cfg()
        for sym in ("DRAM-USD", "SKHX-USD", "LITE-USD"):
            assert sym in cfg.assets, sym
            assert sym not in cfg.arcus_assets, sym
        assert "AVAX-USD" in cfg.assets

    def test_cbrs_map_only_no_sodex_universe(self):
        # HL xyz:CBRS listed; SoDEX CBRS-USD absent — do NOT invent SoDEX
        # membership. Sibling owns HL marks plane before scoring can fire.
        cfg = _cfg()
        assert "CBRS-USD" not in cfg.assets
        assert "CBRS-USD" not in cfg.arcus_assets
        assert SYMBOL_MAP["CBRS-USD"] == "xyz:CBRS"

    def test_prop_map_xyz_not_crypto_sleeve(self):
        # Equities must stay xyz: → turbo2; never bare crypto coins.
        for sym in SODEX_MOVERS + ["CBRS-USD", "DRAM-USD", "SKHX-USD", "LITE-USD"]:
            coin = SYMBOL_MAP[sym]
            assert coin.startswith("xyz:"), (sym, coin)
        assert SYMBOL_MAP["AVAX-USD"] == "AVAX"  # turbo crypto peer


class TestMoverKlineAndSeed:
    def test_sodex_and_hl_kline_owned(self):
        cfg = _cfg()
        for sym in SODEX_MOVERS:
            assert sym in cfg.sodex_kline_assets, sym
            assert sym in cfg.hl_kline_assets, sym
            assert sym in SODEX_SUPPORTED, sym

    def test_step_precision_live_probe(self):
        assert SYMBOL_MIN_QUANTITY["SNDK-USD"] == 0.001
        assert SYMBOL_QTY_PRECISION["SNDK-USD"] == 3
        assert SYMBOL_MIN_QUANTITY["NBIS-USD"] == 0.01
        assert SYMBOL_QTY_PRECISION["NBIS-USD"] == 2
        assert SYMBOL_MIN_QUANTITY["MRVL-USD"] == 0.01
        assert SYMBOL_QTY_PRECISION["INTC-USD"] == 3


class TestMoverClassification:
    def test_tradfi_and_tier_b(self):
        cfg = _cfg()
        for sym in SODEX_MOVERS:
            assert sym in cfg.TRADFI_ASSETS, sym
            assert sym in cfg.TIER_B_ASSETS, sym
            ac = cfg.ASSET_CONFIG[sym]
            assert ac["category"] == "equity"
            assert ac["max_leverage"] <= 8
            assert ac["market_hours"] == "24h"
            assert MIN_STOP_DISTANCE_PCT[sym] >= 1.5

    def test_yahoo_mapped_except_zhipu(self):
        for sym, y in (("AMD-USD", "AMD"), ("SNDK-USD", "SNDK"),
                       ("MU-USD", "MU"), ("INTC-USD", "INTC"),
                       ("NBIS-USD", "NBIS"), ("MRVL-USD", "MRVL")):
            assert TRADFI_SYMBOLS[sym] == y
        assert "ZHIPU-USD" not in TRADFI_SYMBOLS  # pre-IPO, UNITREE doctrine
        for sym in SODEX_MOVERS:
            assert sym in TRADFI_SINGLE_NAMES, sym

    def test_family_turbo2(self):
        for coin in ("xyz:SNDK", "xyz:MU", "xyz:INTC", "xyz:MRVL",
                     "xyz:CBRS", "xyz:AMD"):
            assert family_of(coin) == "equity_semis", coin
        assert family_of("xyz:NBIS") == "equity_ai"
        assert family_of("xyz:ZHIPU") == "pre_ipo_ai"
