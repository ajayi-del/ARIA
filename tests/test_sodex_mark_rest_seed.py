"""Pins for intelligence/sodex_mark_rest_seed.py — QNT thin-book P0.

Covers: wire parser (bare LIST + wrappers), seed write-through, kill switch
OFF = no seed + legacy 500ms slack, aster exclude, assets allowlist, and
main.py / interpreter.py wiring pins (funding_loop piggyback + slack bind).
"""
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from data.mark_price_store import MarkPriceStore  # noqa: E402
from intelligence import sodex_mark_rest_seed as _m  # noqa: E402

_MOD_SRC = (_ROOT / "intelligence" / "sodex_mark_rest_seed.py").read_text()
_MAIN_SRC = (_ROOT / "main.py").read_text()
_INTERP_SRC = (_ROOT / "intelligence" / "interpreter.py").read_text()


# ── parser ────────────────────────────────────────────────────────────────────

class TestParser:
    def test_bare_list_governor_verified_shape(self):
        rows = _m.parse_mark_price_rows([
            {"symbol": "QNT-USD", "markPrice": "199.58",
             "indexPrice": "199.50", "openInterest": "311"},
            {"symbol": "BTC-USD", "markPrice": "100000.0"},
        ])
        assert rows == [
            ("QNT-USD", 199.58, 199.50),
            ("BTC-USD", 100000.0, 100000.0),  # index falls back to mark
        ]

    def test_data_wrapper_and_short_keys(self):
        rows = _m.parse_mark_price_rows(
            {"data": [{"s": "ETH-USD", "p": "3500.0", "i": "3499.0"}]})
        assert rows == [("ETH-USD", 3500.0, 3499.0)]

    def test_garbage_drops_individually(self):
        rows = _m.parse_mark_price_rows([
            {"symbol": "QNT-USD", "markPrice": "199.58"},
            {"symbol": "BAD-USD", "markPrice": "not_a_number"},
            {"symbol": "", "markPrice": "5"},
            {"no_symbol": True, "markPrice": "5"},
            {"symbol": "ZERO-USD", "markPrice": "0"},
            {"symbol": "NEG-USD", "markPrice": "-3"},
            "not_a_dict",
        ])
        assert rows == [("QNT-USD", 199.58, 199.58)]

    def test_non_list_non_dict_empty(self):
        assert _m.parse_mark_price_rows(None) == []
        assert _m.parse_mark_price_rows("junk") == []
        assert _m.parse_mark_price_rows(42) == []


# ── seed + kill switch ────────────────────────────────────────────────────────

class TestSeed:
    def test_seed_writes_store_and_is_healthy(self, monkeypatch):
        monkeypatch.setenv("SODEX_MARK_REST_SEED_ENABLED", "true")
        monkeypatch.setenv("SODEX_MARK_REST_SEED_SLACK_MS", "310000")
        store = MarkPriceStore("QNT-USD")
        assert store.is_healthy(500) is False
        n = _m.seed_mark_stores(
            {"QNT-USD": store},
            [{"symbol": "QNT-USD", "markPrice": "199.58",
              "indexPrice": "199.50"}],
            now_ms=int(time.time() * 1000),
        )
        assert n == 1
        assert store.mark_price == 199.58
        assert store.last_price == 199.50
        assert store.is_healthy(_m.sodex_mark_slack_ms()) is True

    def test_kill_switch_off_no_seed_legacy_slack(self, monkeypatch):
        monkeypatch.setenv("SODEX_MARK_REST_SEED_ENABLED", "false")
        store = MarkPriceStore("QNT-USD")
        n = _m.seed_mark_stores(
            {"QNT-USD": store},
            [{"symbol": "QNT-USD", "markPrice": "199.58"}],
        )
        assert n == 0
        assert store.mark_price is None
        assert _m.sodex_mark_slack_ms() == 500

    def test_exclude_aster_assets(self, monkeypatch):
        monkeypatch.setenv("SODEX_MARK_REST_SEED_ENABLED", "true")
        qnt = MarkPriceStore("QNT-USD")
        aster = MarkPriceStore("ASTER-USD")
        n = _m.seed_mark_stores(
            {"QNT-USD": qnt, "ASTER-USD": aster},
            [
                {"symbol": "QNT-USD", "markPrice": "199.58"},
                {"symbol": "ASTER-USD", "markPrice": "1.23"},
            ],
            exclude={"ASTER-USD"},
        )
        assert n == 1
        assert qnt.mark_price == 199.58
        assert aster.mark_price is None

    def test_assets_allowlist(self, monkeypatch):
        monkeypatch.setenv("SODEX_MARK_REST_SEED_ENABLED", "true")
        qnt = MarkPriceStore("QNT-USD")
        btc = MarkPriceStore("BTC-USD")
        n = _m.seed_mark_stores(
            {"QNT-USD": qnt, "BTC-USD": btc},
            [
                {"symbol": "QNT-USD", "markPrice": "199.58"},
                {"symbol": "BTC-USD", "markPrice": "100000"},
            ],
            assets={"QNT-USD"},
        )
        assert n == 1
        assert qnt.mark_price == 199.58
        assert btc.mark_price is None

    def test_default_slack_when_enabled(self, monkeypatch):
        monkeypatch.setenv("SODEX_MARK_REST_SEED_ENABLED", "true")
        monkeypatch.delenv("SODEX_MARK_REST_SEED_SLACK_MS", raising=False)
        assert _m.sodex_mark_slack_ms() == 310_000


# ── wiring pins ───────────────────────────────────────────────────────────────

class TestWiringPins:
    def test_kill_switch_env_pinned(self):
        assert "SODEX_MARK_REST_SEED_ENABLED" in _MOD_SRC
        assert '"true"' in _MOD_SRC
        assert "SODEX_MARK_REST_SEED_SLACK_MS" in _MOD_SRC

    def test_funding_loop_piggyback_present(self):
        assert "sodex_mark_rest_seed" in _MAIN_SRC
        assert "seed_mark_stores(" in _MAIN_SRC
        assert "latest_mark_price_rows" in _MAIN_SRC
        assert "EVENT_SEEDED" in _MAIN_SRC

    def test_interpreter_slack_bind(self):
        assert "sodex_mark_slack_ms" in _INTERP_SRC
        assert "_sodex_mark_slack_ms()" in _INTERP_SRC
        # Aster path unchanged
        assert "5000 if symbol in getattr(self.config, \"aster_assets\"" in _INTERP_SRC

    def test_telemetry_contract_names(self):
        assert _m.EVENT_SEEDED == "sodex_mark_rest_seeded"
        assert _m.EVENT_FAILED == "sodex_mark_rest_seed_failed"
