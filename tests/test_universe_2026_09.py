"""Pins for the 2026-09-05 Saturday-mover universe expansion.

Operator directive + CEO staged endorsement (proposals.jsonl
universe-expansion-2026-09): FLOCK/FF carry Aster execution; TRIA/NOM/ZEN/ICX
are data-plane incubation via bybit_assets (bybit_enabled=False → signals
die at order_blocked_no_symbol_id, shadow journal scores gate "no_venue").
"""
from core.config import Settings
from data.bybit_feed import BYBIT_SYMBOL_MAP, SUPPORTED_ASSETS
from intelligence.relative_strength import ASSET_CATEGORIES

NEW = ["FLOCK-USD", "FF-USD", "TRIA-USD", "NOM-USD", "ZEN-USD", "ICX-USD"]
EXECUTION = ["FLOCK-USD", "FF-USD"]
INCUBATION = ["TRIA-USD", "NOM-USD", "ZEN-USD", "ICX-USD"]


def _cfg():
    return Settings()


def test_all_six_in_universe():
    assets = _cfg().assets
    for sym in NEW:
        assert sym in assets, f"{sym} missing from config.assets"


def test_execution_stage_partition():
    cfg = _cfg()
    for sym in EXECUTION:
        assert sym in cfg.aster_assets, f"{sym} must route to Aster"
        assert sym not in cfg.bybit_assets, f"{sym} must not be incubated"


def test_incubation_stage_partition():
    cfg = _cfg()
    for sym in INCUBATION:
        assert sym in cfg.bybit_assets, f"{sym} must incubate via bybit_assets"
        assert sym not in cfg.aster_assets, f"{sym} must not execute on Aster"


def test_signal_path_registered():
    for sym in NEW:
        assert sym in BYBIT_SYMBOL_MAP, f"{sym} missing Bybit candle path"
        assert sym in SUPPORTED_ASSETS, f"{sym} missing OI/funding subscription"


def test_categories_registered():
    for sym in NEW:
        assert ASSET_CATEGORIES.get(sym) not in (None, "unknown"), \
            f"{sym} missing category (rotation coherence reads this)"


def test_asset_config_registered_and_leverage_capped():
    ac = _cfg().ASSET_CONFIG
    for sym in NEW:
        assert sym in ac, f"{sym} missing ASSET_CONFIG"
        assert ac[sym]["max_leverage"] <= 8, "leverage cap doctrine (8x max)"
        assert ac[sym]["market_hours"] == "24h"


def test_bybit_assets_incubation_warns_on_flip():
    """The incubation semantics depend on bybit_enabled staying False;
    pin the default so a flip is a deliberate, reviewed act."""
    assert _cfg().bybit_enabled is False
