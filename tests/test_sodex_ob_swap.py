"""SoDEX OB ownership swap pins (2026-10-20, Governor live-by-order).

The swap moves SoDEX-routed crypto symbols' canonical OB stores from the
Bybit feed (signal-plane book) to the SoDEX feed (execution-plane book) and
redirects Bybit writes to per-symbol shadow stores — the comparison plane.
Kill switch OFF = legacy bit-for-bit: the OFF-state pins here prove the
pre-swap ownership maps are reproduced exactly.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelligence.sodex_ob_swap import (  # noqa: E402
    bybit_ob_view, ob_mid_divergence_bps, ob_spread_bps, sodex_ob_swap_enabled,
    sodex_ob_view, swapped_symbols)

_BYBIT_MAP = {"BTC-USD": "BTCUSDT", "ETH-USD": "ETHUSDT",
              "SOL-USD": "SOLUSDT", "USTECH100-USD": "unknown"}


def _venue_for(mapping):
    def _vf(sym):
        return mapping.get(sym, "sodex")
    return _vf


class _Store:
    def __init__(self, bid=0.0, ask=0.0):
        self._b, self._a = bid, ask

    def top_of_book(self):
        return self._b, self._a, 0.0


class TestKillSwitch:
    def test_default_on(self, monkeypatch):
        monkeypatch.delenv("SODEX_OB_SWAP_ENABLED", raising=False)
        assert sodex_ob_swap_enabled() is True

    def test_explicit_off(self, monkeypatch):
        monkeypatch.setenv("SODEX_OB_SWAP_ENABLED", "false")
        assert sodex_ob_swap_enabled() is False

    def test_explicit_on(self, monkeypatch):
        monkeypatch.setenv("SODEX_OB_SWAP_ENABLED", "true")
        assert sodex_ob_swap_enabled() is True

    def test_env_read_at_call_time(self, monkeypatch):
        monkeypatch.setenv("SODEX_OB_SWAP_ENABLED", "false")
        assert sodex_ob_swap_enabled() is False
        monkeypatch.setenv("SODEX_OB_SWAP_ENABLED", "true")
        assert sodex_ob_swap_enabled() is True


class TestSwappedSymbols:
    def test_sodex_routed_bybit_mapped_included(self):
        out = swapped_symbols(list(_BYBIT_MAP), _BYBIT_MAP, _venue_for({}))
        assert out == ["BTC-USD", "ETH-USD", "SOL-USD"]

    def test_aster_routed_excluded(self):
        vf = _venue_for({"BTC-USD": "aster"})
        out = swapped_symbols(list(_BYBIT_MAP), _BYBIT_MAP, vf)
        assert "BTC-USD" not in out
        assert "ETH-USD" in out

    def test_arcus_routed_excluded(self):
        vf = _venue_for({"ETH-USD": "arcus"})
        out = swapped_symbols(list(_BYBIT_MAP), _BYBIT_MAP, vf)
        assert "ETH-USD" not in out

    def test_unknown_mapping_excluded(self):
        # The legacy _sodex_only class (equities/commodities) is already
        # SoDEX-owned — never "swapped".
        out = swapped_symbols(list(_BYBIT_MAP), _BYBIT_MAP, _venue_for({}))
        assert "USTECH100-USD" not in out

    def test_unmapped_symbol_excluded(self):
        out = swapped_symbols(["BTC-USD", "SPCX-USD"], _BYBIT_MAP, _venue_for({}))
        assert out == ["BTC-USD"]

    def test_garbage_fails_closed(self):
        assert swapped_symbols(None, _BYBIT_MAP, _venue_for({})) == []
        assert swapped_symbols(["BTC-USD"], None, _venue_for({})) == []

        def _boom(_s):
            raise RuntimeError("venue down")
        assert swapped_symbols(["BTC-USD"], _BYBIT_MAP, _boom) == []


class TestBybitObView:
    def test_swapped_redirected_to_shadow(self):
        canonical = {"BTC-USD": _Store(), "ETH-USD": _Store()}
        shadow = {"BTC-USD": _Store()}
        out = bybit_ob_view(canonical, shadow, _venue_for({}))
        assert out["BTC-USD"] is shadow["BTC-USD"]
        assert out["ETH-USD"] is canonical["ETH-USD"]

    def test_execution_venues_excluded(self):
        canonical = {"BTC-USD": _Store(), "TAO-USD": _Store(), "SPY-USD": _Store()}
        vf = _venue_for({"TAO-USD": "aster", "SPY-USD": "arcus"})
        out = bybit_ob_view(canonical, {}, vf)
        assert list(out) == ["BTC-USD"]

    def test_empty_shadow_is_legacy_bit_for_bit(self):
        canonical = {"BTC-USD": _Store(), "TAO-USD": _Store()}
        vf = _venue_for({"TAO-USD": "aster"})
        out = bybit_ob_view(canonical, {}, vf)
        legacy = {a: s for a, s in canonical.items()
                  if vf(a) not in ("aster", "arcus")}
        assert out.keys() == legacy.keys()
        assert all(out[a] is legacy[a] for a in legacy)


class TestSodexObView:
    def test_swapped_canonical_joined(self):
        legacy_ob = {"USTECH100-USD": _Store()}
        canonical = {"BTC-USD": _Store(), "USTECH100-USD": legacy_ob["USTECH100-USD"]}
        out = sodex_ob_view(legacy_ob, canonical, ["BTC-USD"])
        assert out["BTC-USD"] is canonical["BTC-USD"]
        assert out["USTECH100-USD"] is legacy_ob["USTECH100-USD"]

    def test_empty_swapped_is_legacy_bit_for_bit(self):
        legacy_ob = {"USTECH100-USD": _Store(), "SPCX-USD": _Store()}
        out = sodex_ob_view(legacy_ob, {"BTC-USD": _Store()}, [])
        assert out.keys() == legacy_ob.keys()
        assert all(out[a] is legacy_ob[a] for a in legacy_ob)

    def test_swapped_missing_from_canonical_skipped(self):
        legacy_ob = {"SPCX-USD": _Store()}
        out = sodex_ob_view(legacy_ob, {}, ["BTC-USD"])
        assert list(out) == ["SPCX-USD"]


class TestDivergence:
    def test_mid_divergence_signed_bps(self):
        sodex = _Store(101.0, 103.0)   # mid 102
        bybit = _Store(99.0, 101.0)    # mid 100
        assert ob_mid_divergence_bps(sodex, bybit) == 200.0
        assert ob_mid_divergence_bps(bybit, sodex) != 200.0

    def test_empty_book_abstains(self):
        assert ob_mid_divergence_bps(_Store(0.0, 0.0), _Store(99.0, 101.0)) is None
        assert ob_mid_divergence_bps(_Store(99.0, 101.0), _Store(0.0, 0.0)) is None

    def test_garbage_abstains(self):
        assert ob_mid_divergence_bps(None, _Store(1.0, 2.0)) is None
        assert ob_mid_divergence_bps(_Store(1.0, 2.0), "junk") is None

    def test_spread_bps(self):
        store = _Store(99.0, 101.0)    # mid 100, spread 2
        assert ob_spread_bps(store) == 200.0

    def test_spread_empty_or_garbage(self):
        assert ob_spread_bps(_Store(0.0, 0.0)) is None
        assert ob_spread_bps(None) is None
