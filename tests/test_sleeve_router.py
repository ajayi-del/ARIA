"""Pins for prop_harness.brains.sleeve_router — ownership + overflow."""

from __future__ import annotations

from prop_harness.brains import sleeve_router as sr


def _snap(**kw):
    base = {
        "sleeve": "crypto",
        "band": "ACTIVE",
        "breached": False,
        "open_n": 0,
        "max_positions": 4,
        "budget_remaining": 40.0,
        "headroom": 150.0,
        "ts": 1_000.0,
        "mult": 1.0,
        "cooloff": False,
    }
    base.update(kw)
    return base


def test_ownership_crypto_primary_is_turbo():
    primary, neighbours = sr.ownership_chain("crypto")
    assert primary == sr.SLEEVE_TURBO
    assert sr.SLEEVE_FT_CRYPTO in neighbours
    assert sr.SLEEVE_TURBO2 not in neighbours
    assert sr.SLEEVE_FT_STOCKS not in neighbours


def test_ownership_stocks_primary_is_turbo2():
    primary, neighbours = sr.ownership_chain("tradfi")
    assert primary == sr.SLEEVE_TURBO2
    assert sr.SLEEVE_FT_STOCKS in neighbours
    assert sr.SLEEVE_TURBO not in neighbours


def test_asset_class_xyz_is_tradfi():
    assert sr.asset_class_of("xyz:XYZ100") == sr.ASSET_TRADFI
    assert sr.asset_class_of("BTC") == sr.ASSET_CRYPTO


def test_crypto_allocates_turbo_when_fit():
    snaps = {
        sr.SLEEVE_TURBO: _snap(sleeve="crypto"),
        sr.SLEEVE_FT_CRYPTO: _snap(sleeve="crypto"),
    }
    out = sr.select_sleeve("BTC", 7.8, snaps, formation_unit="giant", now_s=1000.0)
    assert out["action"] == sr.ACTION_PRIMARY
    assert out["sleeve"] == sr.SLEEVE_TURBO


def test_crypto_overflow_to_ft_when_turbo_full():
    snaps = {
        sr.SLEEVE_TURBO: _snap(sleeve="crypto", open_n=4, max_positions=4),
        sr.SLEEVE_FT_CRYPTO: _snap(sleeve="crypto", open_n=0),
    }
    out = sr.select_sleeve("ETH", 6.2, snaps, now_s=1000.0)
    assert out["action"] == sr.ACTION_OVERFLOW
    assert out["sleeve"] == sr.SLEEVE_FT_CRYPTO
    assert out["primary"] == sr.SLEEVE_TURBO


def test_neighbour_does_not_front_run_free_turbo():
    """FT must not steal crypto when turbo still has capacity."""
    snaps = {
        sr.SLEEVE_TURBO: _snap(sleeve="crypto", open_n=1),
        sr.SLEEVE_FT_CRYPTO: _snap(sleeve="crypto", open_n=0),
    }
    out = sr.select_sleeve("SOL", 8.0, snaps, now_s=1000.0)
    assert out["action"] == sr.ACTION_PRIMARY
    assert out["sleeve"] == sr.SLEEVE_TURBO


def test_stocks_overflow_when_turbo2_locked():
    snaps = {
        sr.SLEEVE_TURBO2: _snap(
            sleeve="tradfi", band="LOCKED", mult=0.0, headroom=11.0),
        sr.SLEEVE_FT_STOCKS: _snap(sleeve="tradfi", open_n=1),
    }
    out = sr.select_sleeve("xyz:XYZ100", 4.2, snaps, now_s=1000.0)
    assert out["action"] == sr.ACTION_OVERFLOW
    assert out["sleeve"] == sr.SLEEVE_FT_STOCKS
    assert out["primary"] == sr.SLEEVE_TURBO2


def test_band_a_drops_not_overflowed():
    snaps = {
        sr.SLEEVE_TURBO2: _snap(sleeve="tradfi", open_n=4, max_positions=4),
        sr.SLEEVE_FT_STOCKS: _snap(sleeve="tradfi"),
    }
    out = sr.select_sleeve(
        "xyz:CBRS", 3.2, snaps, band_a=True, now_s=1000.0)
    assert out["action"] == sr.ACTION_DROP
    assert out["reason"] == sr.REASON_BAND_A


def test_drop_when_all_blocked():
    snaps = {
        sr.SLEEVE_TURBO: _snap(sleeve="crypto", band="LOCKED", mult=0.0),
        sr.SLEEVE_FT_CRYPTO: _snap(sleeve="crypto", budget_remaining=0.0),
    }
    out = sr.select_sleeve("BTC", 7.0, snaps, now_s=1000.0)
    assert out["action"] == sr.ACTION_DROP
    assert out["sleeve"] is None


def test_stale_snap_fail_closed():
    snaps = {
        sr.SLEEVE_TURBO: _snap(sleeve="crypto", ts=1.0),
        sr.SLEEVE_FT_CRYPTO: _snap(sleeve="crypto", ts=1.0),
    }
    out = sr.select_sleeve("BTC", 7.0, snaps, now_s=10_000.0, stale_s=90.0)
    assert out["action"] == sr.ACTION_DROP
    assert out["reason"] == sr.REASON_STALE
