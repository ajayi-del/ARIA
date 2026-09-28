"""Venue migration pins — Governor 2026-09-23: "migrate all coins on sodex
present on aster back to sodex, leave aster native on aster".

Intersection verified LIVE same-day from two independent sources:
  (a) SoDEX public REST GET /api/v1/perps/markets/symbols (98 markets,
      response ts 1790205284934) — status field read per symbol;
  (b) the live bot's exchange_info_fetched id map on aria-prod-v2
      (2026-09-23 16:45 UTC boot) — identical 24-name intersection, IDs
      match the REST payload exactly.

TAO-USD exception: SoDEX lists it (id 77) but status=HALT — not tradable
there today, so it stays Aster-routed until the venue re-opens the book.

Template: 2026-09-21 remigration (02f3e6d) — config list only; the list IS
the kill switch. Crypto alts ride the Bybit candle plane on both venues, so
NO kline-list changes (XRP/1000PEPE/SUI/AVAX/LINK/NEAR precedent).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import Settings  # noqa: E402

# 23 symbols migrating Aster -> SoDEX (SoDEX-listed, status TRADING).
MIGRATED = [
    "1000BONK-USD", "AAVE-USD", "ADA-USD", "APT-USD", "ASTER-USD",
    "BCH-USD", "DOGE-USD", "ENA-USD", "FARTCOIN-USD", "HYPE-USD",
    "LIT-USD", "LTC-USD", "ONDO-USD", "PENGU-USD", "TRX-USD",
    "UNI-USD", "VIRTUAL-USD", "WIF-USD", "WLD-USD", "WLFI-USD",
    "XLM-USD", "XMR-USD", "ZEC-USD",
]

# 21 symbols staying on Aster (Aster-native: NOT SoDEX-listed), plus TAO
# (SoDEX-listed but HALT).
ASTER_NATIVE = [
    "ACE-USD", "AIO-USD", "AKE-USD", "ARIA-USD", "BOME-USD",
    "CYS-USD", "DOS-USD", "FF-USD", "FLOCK-USD", "HEMI-USD",
    "ICP-USD", "INJ-USD", "KAITO-USD", "MUBARAK-USD", "ORDI-USD",
    "PAXG-USD", "SEI-USD", "SNXX-USD", "TIA-USD", "VELVET-USD",
]
TAO_EXCEPTION = "TAO-USD"   # SoDEX id 77, status HALT — stays Aster-routed


@pytest.fixture()
def cfg():
    return Settings()


def test_aster_assets_is_exactly_the_aster_native_set(cfg):
    assert sorted(cfg.aster_assets) == sorted(ASTER_NATIVE + [TAO_EXCEPTION])
    assert len(cfg.aster_assets) == 21


def test_migrated_symbols_leave_aster_routing(cfg):
    for sym in MIGRATED:
        assert sym not in cfg.aster_assets, f"{sym} must route back to SoDEX"
        # Universe membership unchanged — routing flips, trading doesn't stop.
        assert sym in cfg.assets, f"{sym} must stay in the universe"
        assert sym in cfg.ASSET_CONFIG, f"{sym} must keep ASSET_CONFIG"


def test_tao_exception_stays_aster(cfg):
    # SoDEX lists TAO (id 77) but the book is HALT — flipping routing to a
    # halted book would hard-block every TAO signal at the venue.
    assert TAO_EXCEPTION in cfg.aster_assets
    assert TAO_EXCEPTION in cfg.assets


def test_aster_native_stay_put(cfg):
    for sym in ASTER_NATIVE:
        assert sym in cfg.aster_assets, f"{sym} is Aster-native — must stay"


def test_no_kline_plane_changes(cfg):
    # Crypto alts ride the Bybit candle plane on both venues (2026-09-21
    # template). None of the 23 migrates into a venue-kline list.
    for sym in MIGRATED:
        assert sym not in cfg.sodex_kline_assets
        assert sym not in cfg.aster_kline_assets
    assert cfg.aster_kline_assets == []


def test_shadow_trio_untouched(cfg):
    assert cfg.aster_shadow_assets == ["BTC-USD", "ETH-USD", "SOL-USD"]


def test_reversibility_is_the_code_list(cfg):
    # Kill-switch doctrine: the code list is the ONLY routing source
    # (issue-#17 validator discards env/init-supplied universes), so
    # reverting = restoring symbols in core/config.py — one list edit, no
    # hidden state. Pin the validator so a future "env override" feature
    # cannot silently bypass the revert path.
    overridden = Settings(aster_assets=["HYPE-USD"])
    assert overridden.aster_assets == cfg.aster_assets
