"""Pins for the 2026-09-04 leverage directive: cap regular-trade leverage at
8 (was 10) — more margin per unit notional (+25%), smaller ROE swings, fewer
early stop-outs. Whale-probe 50x and the dormant bybit_max_leverage are
deliberately untouched."""
from core.config import Settings


def test_aster_max_leverage_is_8():
    assert Settings().aster_max_leverage == 8


def test_directive_symbols_capped_at_8():
    cfg = Settings()
    for sym in ("ETH-USD", "SOL-USD", "BNB-USD", "CRCL-USD", "COIN-USD",
                "DOGE-USD", "USTECH100-USD", "SPCX-USD"):
        assert cfg.ASSET_CONFIG[sym]["max_leverage"] == 8, sym


def test_spcx_preferred_leverage_is_8():
    assert Settings().ASSET_CONFIG["SPCX-USD"]["preferred_leverage"] == 8


def test_no_regular_trade_symbol_above_8():
    cfg = Settings()
    for sym, ac in cfg.ASSET_CONFIG.items():
        assert ac.get("max_leverage", 0) <= 8, sym
