"""Pins for intelligence/hl_equity_plane.py (ATS-72 Arcus→HL equity plane)."""

from __future__ import annotations

import os

import pytest

from intelligence import hl_equity_plane as hep


class _Cfg:
    def __init__(self, *, assets=None, arcus=None, hl=None, enabled=True):
        self.assets = list(assets or [])
        self.arcus_assets = list(arcus or [])
        self.hl_kline_assets = list(hl or ["DRAM-USD", "LITE-USD"])
        self.hl_equity_plane_enabled = enabled


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("HL_EQUITY_PLANE_ENABLED", raising=False)


class TestKillSwitch:
    def test_default_on(self):
        assert hep.plane_enabled(_Cfg()) is True

    def test_config_off(self):
        assert hep.plane_enabled(_Cfg(enabled=False)) is False

    def test_env_off_overrides_config(self, monkeypatch):
        monkeypatch.setenv("HL_EQUITY_PLANE_ENABLED", "0")
        assert hep.plane_enabled(_Cfg(enabled=True)) is False

    def test_env_on_overrides_config_off(self, monkeypatch):
        monkeypatch.setenv("HL_EQUITY_PLANE_ENABLED", "true")
        assert hep.plane_enabled(_Cfg(enabled=False)) is True


class TestResolve:
    def test_plane_off_returns_legacy_only(self, monkeypatch):
        monkeypatch.setenv("HL_EQUITY_PLANE_ENABLED", "0")
        cfg = _Cfg(assets=["AMD-USD", "SNDK-USD"],
                   arcus=["NVDA-USD"],
                   hl=["DRAM-USD"])
        assert hep.resolve_hl_kline_symbols(cfg) == ["DRAM-USD"]

    def test_includes_seed_movers_when_mapped(self):
        cfg = _Cfg(assets=[], arcus=[], hl=["DRAM-USD"])
        got = hep.resolve_hl_kline_symbols(cfg)
        assert "DRAM-USD" in got
        # Seed names that are prop-mapped xyz: must appear
        for sym in ("AMD-USD", "SNDK-USD", "MU-USD", "INTC-USD",
                    "NBIS-USD", "MRVL-USD", "ZHIPU-USD"):
            assert sym in got, sym

    def test_includes_assets_and_arcus_xyz(self):
        cfg = _Cfg(assets=["SKHX-USD"], arcus=["NVDA-USD"], hl=["DRAM-USD"])
        got = hep.resolve_hl_kline_symbols(cfg)
        assert "SKHX-USD" in got
        assert "NVDA-USD" in got
        assert "DRAM-USD" in got

    def test_skips_non_xyz_crypto(self):
        cfg = _Cfg(assets=["BTC-USD", "ETH-USD"], hl=["DRAM-USD"])
        got = hep.resolve_hl_kline_symbols(cfg)
        assert "BTC-USD" not in got
        assert "ETH-USD" not in got
        assert "DRAM-USD" in got

    def test_dedupes(self):
        cfg = _Cfg(assets=["AMD-USD"], arcus=["AMD-USD"],
                   hl=["AMD-USD", "DRAM-USD"])
        got = hep.resolve_hl_kline_symbols(cfg)
        assert got.count("AMD-USD") == 1

    def test_hl_symbols_filter_fail_closed(self):
        cfg = _Cfg(hl=["DRAM-USD"])
        # Empty hl_symbols → nothing passes the membership gate
        assert hep.resolve_hl_kline_symbols(cfg, hl_symbols={}) == []


class TestCandleBuffers:
    def test_needs_buffers_for_missing(self):
        cfg = _Cfg(hl=["DRAM-USD"])
        existing = {"DRAM-USD": {"1m": object()}}
        need = hep.symbols_needing_candle_buffers(
            cfg, existing, resolved=["DRAM-USD", "AMD-USD"])
        assert need == ["AMD-USD"]

    def test_needs_none_when_present(self):
        cfg = _Cfg()
        existing = {"AMD-USD": {"1m": object(), "15m": object()}}
        need = hep.symbols_needing_candle_buffers(
            cfg, existing, resolved=["AMD-USD"])
        assert need == []


class TestWiring:
    def test_main_splices_present(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        main = open(os.path.join(root, "main.py")).read()
        assert "hl_equity_plane" in main
        assert "resolve_hl_kline_symbols" in main
        assert "hl_equity_plane_buffers" in main

    def test_config_knob_default_on(self):
        from core.config import Settings
        s = Settings()
        assert s.hl_equity_plane_enabled is True
        # Legacy static list unchanged (kill-switch bit-for-bit)
        assert "AMD-USD" not in s.hl_kline_assets
        assert "SNDK-USD" not in s.hl_kline_assets
        assert "DRAM-USD" in s.hl_kline_assets

    def test_resolve_default_settings_includes_movers(self):
        from core.config import Settings
        got = hep.resolve_hl_kline_symbols(Settings())
        for sym in ("AMD-USD", "SNDK-USD", "MU-USD", "INTC-USD",
                    "NBIS-USD", "MRVL-USD", "ZHIPU-USD", "DRAM-USD"):
            assert sym in got, sym

    def test_no_variational_wiring(self):
        src = open(hep.__file__).read()
        assert "import variational" not in src
        assert "variational.io" not in src
        assert "fetch_variational" not in src
