"""Pins for intelligence/hl_equity_plane.py (ATS-72 Arcus→HL equity plane)."""

from __future__ import annotations

import os

import pytest

from intelligence import hl_equity_plane as hep


class _Cfg:
    def __init__(self, *, assets=None, arcus=None, hl=None, enabled=True,
                 map_only=None, map_surplus=True):
        self.assets = list(assets or [])
        self.arcus_assets = list(arcus or [])
        self.hl_kline_assets = list(hl or ["DRAM-USD", "LITE-USD"])
        self.hl_equity_plane_enabled = enabled
        self.hl_map_only_assets = list(
            map_only if map_only is not None else ["CBRS-USD"])
        self.hl_map_only_surplus_enabled = map_surplus
        self.hl_map_only_surplus_rate_s = 0.0  # no rate limit in unit tests
        self.prop_capital_surplus_enabled = True


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("HL_EQUITY_PLANE_ENABLED", raising=False)
    monkeypatch.delenv("HL_MAP_ONLY_SURPLUS_ENABLED", raising=False)
    hep._MAP_ONLY_EMIT_TS.clear()


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
        for sym in ("AMD-USD", "SNDK-USD", "MU-USD", "INTC-USD",
                    "NBIS-USD", "MRVL-USD", "ZHIPU-USD", "CBRS-USD"):
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


class TestMapOnly:
    def test_cbrs_map_only_default(self):
        cfg = _Cfg(assets=["AMD-USD"], arcus=[])
        assert hep.map_only_symbols(cfg) == ["CBRS-USD"]

    def test_map_only_excluded_when_in_assets(self):
        cfg = _Cfg(assets=["CBRS-USD"], map_only=["CBRS-USD"])
        assert hep.map_only_symbols(cfg) == []

    def test_write_marks_for_map_only(self):
        cfg = _Cfg()
        assert hep.write_marks_to_store("CBRS-USD", cfg) is True
        assert hep.write_marks_to_store("AMD-USD", cfg) is False

    def test_direction_from_closes(self):
        xs = [100.0] * 15 + [100.5]
        got = hep.direction_from_closes(xs, lookback=15, move_pct=0.3)
        assert got is not None and got[0] == "long"
        dn = [100.0] * 15 + [99.5]
        got2 = hep.direction_from_closes(dn, lookback=15, move_pct=0.3)
        assert got2 is not None and got2[0] == "short"
        flat = [100.0] * 16
        assert hep.direction_from_closes(flat, lookback=15, move_pct=0.3) is None

    def test_direction_multi_horizon_prefers_short(self):
        # Flat on 15/60, +0.7% on 240 → long via 240@0.50 gate
        xs = [100.0] * 241
        xs[-1] = 100.7
        got = hep.direction_from_closes(xs)
        assert got is not None and got[0] == "long" and got[1] >= 0.5

    def test_emit_map_only_surplus(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PROP_CAPITAL_SURPLUS", "1")
        monkeypatch.setenv("PROP_OUTBOX_ENABLED", "true")
        cfg = _Cfg()
        out = tmp_path / "outbox.jsonl"
        closes = [100.0] * 15 + [100.6]
        rec = hep.emit_map_only_surplus(
            "CBRS-USD", mark=100.6, closes=closes,
            config=cfg, outbox_path=str(out))
        assert rec is not None
        assert rec["symbol"] == "CBRS-USD"
        assert rec["side"] == "long"
        assert rec["capital_reason"] == "hl_map_only"
        assert rec.get("block_class") == "aria_capital_block"
        assert out.read_text().strip()

    def test_emit_refuses_non_map_only(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PROP_CAPITAL_SURPLUS", "1")
        cfg = _Cfg()
        out = tmp_path / "outbox.jsonl"
        closes = [100.0] * 15 + [101.0]
        rec = hep.emit_map_only_surplus(
            "AMD-USD", mark=101.0, closes=closes,
            config=cfg, outbox_path=str(out))
        assert rec is None
        assert not out.exists() or out.read_text() == ""


class TestWiring:
    def test_main_splices_present(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        main = open(os.path.join(root, "main.py")).read()
        assert "hl_equity_plane" in main
        assert "resolve_hl_kline_symbols" in main
        assert "hl_equity_plane_buffers" in main
        assert "hl_map_only_surplus" in main

    def test_config_knob_default_on(self):
        from core.config import Settings
        s = Settings()
        assert s.hl_equity_plane_enabled is True
        assert s.hl_map_only_surplus_enabled is True
        assert "CBRS-USD" in s.hl_map_only_assets
        # #32 seeded movers into static hl_kline_assets
        assert "AMD-USD" in s.hl_kline_assets
        assert "SNDK-USD" in s.hl_kline_assets
        assert "DRAM-USD" in s.hl_kline_assets

    def test_resolve_default_settings_includes_movers_and_cbrs(self):
        from core.config import Settings
        got = hep.resolve_hl_kline_symbols(Settings())
        for sym in ("AMD-USD", "SNDK-USD", "MU-USD", "INTC-USD",
                    "NBIS-USD", "MRVL-USD", "ZHIPU-USD", "DRAM-USD",
                    "CBRS-USD"):
            assert sym in got, sym

    def test_no_variational_wiring(self):
        src = open(hep.__file__).read()
        assert "import variational" not in src
        assert "variational.io" not in src
        assert "fetch_variational" not in src

    def test_feed_marks_carve_out_wired(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "data", "hyperliquid_feed.py")).read()
        assert "write_marks_to_store" in src
