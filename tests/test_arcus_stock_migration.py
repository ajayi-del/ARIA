"""Arcus stock-migration pins (2026-11-01, Governor order).

"add meta tsla nvda amd to arcus orcl tsm" + "all stocks that have not
executed in the last 4 weeks should be added to arcus" + "arcus runs 24/7,
the weekend close should not affect after migration".

The migrated 14 (Arcus-listing-verified): META, TSLA, NVDA, AMD, ORCL,
MSFT, AAPL, AMZN, GOOGL, HOOD, COIN, CRCL, MSTR, SPCX.
Stay SoDEX (active, 4-week-trade census): USTECH100 (492 trades/28d),
SKHX (40), TSM (trades — Governor named it but it is NOT Arcus-listed).
Stay SoDEX (not Arcus-listed — migration impossible): LITE, SMCI,
SAMSUNG, UNITREE.

Routing mechanism: execution/venue.py:76 `arcus_route_candidates` drops
any symbol still in config.assets from the Arcus route — so removal from
config.assets is what UNLOCKS the Arcus assignment at boot
(main.py:3929-3968). The 24/7 requirement is met by construction:
_arcus_scalp_loop has no market-hours gates, and _SODEX_24H_OVERRIDE
keeps the 14 open for every is_open consumer.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MIGRATED = [
    "META-USD", "TSLA-USD", "NVDA-USD", "AMD-USD", "ORCL-USD",
    "MSFT-USD", "AAPL-USD", "AMZN-USD", "GOOGL-USD", "HOOD-USD",
    "COIN-USD", "CRCL-USD", "MSTR-USD", "SPCX-USD",
]
STAY_ACTIVE = ["USTECH100-USD", "SKHX-USD", "TSM-USD"]
STAY_UNLISTED = ["LITE-USD", "SMCI-USD", "SAMSUNG-USD", "UNITREE-USD"]


def _cfg():
    from core.config import Settings
    return Settings()


class TestMigratedSymbols:
    def test_all_14_in_arcus_assets(self):
        for sym in MIGRATED:
            assert sym in _cfg().arcus_assets, sym

    def test_none_in_config_assets(self):
        # config.assets membership BLOCKS Arcus routing (net-new guard).
        assets = set(_cfg().assets)
        for sym in MIGRATED:
            assert sym not in assets, sym

    def test_none_in_sodex_kline_assets(self):
        owned = set(_cfg().sodex_kline_assets)
        for sym in MIGRATED:
            assert sym not in owned, sym

    def test_none_in_tradfi_maps(self):
        from data.tradfi_feed import TRADFI_SYMBOLS, TRADFI_SINGLE_NAMES
        for sym in MIGRATED:
            assert sym not in TRADFI_SYMBOLS, sym
            assert sym not in TRADFI_SINGLE_NAMES, sym

    def test_all_14_keep_24h_override(self):
        # Arcus runs 24/7 — the override must hold wherever is_open is read.
        from intelligence.market_hours import _SODEX_24H_OVERRIDE
        for sym in MIGRATED:
            assert sym in _SODEX_24H_OVERRIDE, sym

    def test_spcx_out_of_core_assets(self):
        # No SoDEX WS priority subscription for an Arcus-routed symbol.
        assert "SPCX-USD" not in _cfg().core_assets


class TestStayers:
    def test_active_stay_sodex(self):
        cfg = _cfg()
        for sym in STAY_ACTIVE:
            assert sym in cfg.assets, sym
            assert sym not in cfg.arcus_assets, sym

    def test_unlisted_stay_sodex(self):
        # Not Arcus-listed — the migration is impossible for these; they
        # keep their full SoDEX registration (kline ownership included).
        cfg = _cfg()
        for sym in STAY_UNLISTED:
            assert sym in cfg.assets, sym
            assert sym not in cfg.arcus_assets, sym
            assert sym in cfg.sodex_kline_assets, sym


class TestCampaignAfterMigration:
    def test_campaign_symbols_exclude_arcus_routed(self):
        # SPCX/AMD were campaign members; both are Arcus-routed now. The
        # campaign bracket path is untested on the Arcus executor.
        members = set(_cfg().campaign_symbols)
        assert "SPCX-USD" not in members
        assert "AMD-USD" not in members
        assert members == {"ETH-USD", "XRP-USD", "UNI-USD"}

    def test_campaign_legacy_fallback_not_arcus_routed(self):
        # The single-string fallback must never name an Arcus symbol —
        # mark stores exist for arcus symbols, so it would FIRE on the
        # untested Arcus executor path.
        cfg = _cfg()
        assert cfg.campaign_symbol not in set(cfg.arcus_assets)
        assert cfg.campaign_symbol == "ETH-USD"


class TestNetNewGuardRouting:
    def test_migrated_symbols_pass_net_new_guard(self):
        # venue.py:76 — a migrated symbol (in arcus_assets, NOT in
        # config.assets) must survive the candidate filter.
        from execution.venue import arcus_route_candidates
        cfg = _cfg()
        cands = arcus_route_candidates(list(cfg.arcus_assets),
                                       default_universe=set(cfg.assets))
        for sym in MIGRATED:
            assert sym in cands, sym

    def test_stayers_blocked_from_arcus_route(self):
        from execution.venue import arcus_route_candidates
        cfg = _cfg()
        probe = STAY_ACTIVE + STAY_UNLISTED
        cands = arcus_route_candidates(probe,
                                       default_universe=set(cfg.assets))
        assert cands == []
