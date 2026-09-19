"""Fee actuals at close (2026-09-19 audit repair) — no network.

Audit defect: fee_usd was always null in trade records; fee_est_usd was
recorded but never subtracted; "net_pnl" basis varied by close path. Fix:
SoDEX GET /accounts/{addr}/trades at close → ADDITIVE fee_actual_usd /
fee_maker_usd / fee_taker_usd / fee_fill_count on the trade record.
Fail-open: any client error → null fields + fee_actuals_unavailable.
Knob: config.fee_actuals_enabled (False = no query, pre-change behavior).
"""

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.sodex_client import (  # noqa: E402
    SoDEXClient, SoDEXAPIError, fetch_user_trades, summarize_user_trade_fees)
from main import _build_trade_record, _lookup_fee_actuals  # noqa: E402


# ── Fixtures ──────────────────────────────────────────────────────────────────

_WALLET = "0xdb87899000000000000000000000000000000001"

_TRADES_PAYLOAD = {
    "code": 0,
    "data": {"trades": [
        {"symbol": "SOL-USD", "tradeID": "t1", "orderID": "111",
         "clOrdID": "", "side": "BUY", "price": "100.5", "quantity": "1.0",
         "fee": "0.010050", "feeCoin": "USDC", "isMaker": False,
         "time": 1_700_000_000_000},
        {"symbol": "SOL-USD", "tradeID": "t2", "orderID": "222",
         "clOrdID": "c222", "side": "SELL", "price": "101.0", "quantity": "1.0",
         "fee": "0.005050", "feeCoin": "USDC", "isMaker": True,
         "time": 1_700_000_100_000},
        # Optional-field absences: no fee, no feeCoin, no isMaker.
        {"symbol": "SOL-USD", "tradeID": "t3", "orderID": "333",
         "side": "SELL", "price": "101.0", "quantity": "0.5",
         "time": 1_700_000_200_000},
    ]},
}


def _resp(payload, status=200):
    return SimpleNamespace(status_code=status, text="err" if status != 200 else "",
                           json=lambda: payload)


def _client(**cfg):
    defaults = dict(sodex_account_id=_WALLET, account_id="",
                    sodex_mainnet=True,
                    sodex_rest_perps="https://mainnet-gw.sodex.dev/api/v1/perps")
    defaults.update(cfg)
    signer = Mock()
    signer.get_address.return_value = "0x36C54F0000000000000000000000000000000001"
    c = SoDEXClient(SimpleNamespace(**defaults), signer, Mock())
    c.client = SimpleNamespace(get=AsyncMock())
    return c


# ── Client method ─────────────────────────────────────────────────────────────

class TestGetUserTrades:
    def test_parses_representative_payload(self):
        c = _client()
        c.client.get.return_value = _resp(_TRADES_PAYLOAD)
        out = asyncio.run(c.get_user_trades(
            symbol="SOL-USD", start_time_ms=1_700_000_000_000, limit=1000))
        assert len(out) == 3
        assert out[0]["orderID"] == "111"
        assert out[1]["isMaker"] is True
        # Optional-field absence survives parsing (no KeyError, no fabrication).
        assert "fee" not in out[2]
        # Wallet in URL path (GET convention — no X-API-Key), params wired.
        url = c.client.get.await_args.args[0]
        assert url.endswith(f"/accounts/{_WALLET}/trades")
        params = c.client.get.await_args.kwargs["params"]
        assert params["symbol"] == "SOL-USD"
        assert params["startTime"] == 1_700_000_000_000
        assert params["limit"] == 1000

    def test_list_form_payload(self):
        c = _client()
        c.client.get.return_value = _resp({"data": [_TRADES_PAYLOAD["data"]["trades"][0]]})
        out = asyncio.run(c.get_user_trades(symbol="SOL-USD"))
        assert len(out) == 1

    def test_limit_clamped_and_default(self):
        c = _client()
        c.client.get.return_value = _resp({"data": {"trades": []}})
        asyncio.run(c.get_user_trades(limit=5000))
        assert c.client.get.await_args.kwargs["params"]["limit"] == 1000

    def test_non_200_raises(self):
        c = _client()
        c.client.get.return_value = _resp({}, status=500)
        with pytest.raises(SoDEXAPIError):
            asyncio.run(c.get_user_trades(symbol="SOL-USD"))

    def test_module_level_fetch_uses_given_address(self):
        http = SimpleNamespace(get=AsyncMock(return_value=_resp({"data": []})))
        out = asyncio.run(fetch_user_trades(http, "https://x", "0xdead",
                                            symbol="BTC-USD"))
        assert out == []
        assert "/accounts/0xdead/trades" in http.get.await_args.args[0]


# ── Pure summarizer ───────────────────────────────────────────────────────────

class TestSummarize:
    def _trades(self):
        return _TRADES_PAYLOAD["data"]["trades"]

    def test_maker_taker_split_scoped_to_order_ids(self):
        out = summarize_user_trade_fees(
            self._trades(), order_ids={"111", "222"},
            start_ms=1_700_000_000_000)
        assert out["fee_fill_count"] == 2
        assert out["fee_actual_usd"] == round(0.010050 + 0.005050, 6)
        assert out["fee_taker_usd"] == 0.01005
        assert out["fee_maker_usd"] == 0.00505
        assert out["reason"] is None

    def test_window_excludes_stale_fills(self):
        out = summarize_user_trade_fees(
            self._trades(), order_ids=set(), start_ms=1_700_000_100_000)
        # t1 falls before the window; t3 kept but has no fee field.
        assert out["fee_fill_count"] == 2
        assert out["fee_actual_usd"] == 0.00505

    def test_no_fills(self):
        out = summarize_user_trade_fees([], order_ids={"111"})
        assert out["reason"] == "no_fills"
        assert out["fee_actual_usd"] is None

    def test_no_fee_fields(self):
        t3 = self._trades()[2]
        out = summarize_user_trade_fees([t3], order_ids=set())
        assert out["reason"] == "no_fee_fields"
        assert out["fee_actual_usd"] is None
        assert out["fee_fill_count"] == 1

    def test_non_usdc_feecoin_raw_recorded_no_conversion(self):
        trades = [dict(self._trades()[0], feeCoin="SOSO", fee="2.5")]
        out = summarize_user_trade_fees(trades, order_ids=set())
        assert out["fee_actual_usd"] is None
        assert out["fee_maker_usd"] is None
        assert out["fee_taker_usd"] is None
        assert out["raw_fee_total"] == 2.5
        assert out["fee_coin"] == ["SOSO"]
        assert out["reason"] == "non_usdc_feecoin"

    def test_order_id_fallback_to_window_when_no_match(self):
        # No fill matches the tracked ids → time-window set wins (best-effort).
        out = summarize_user_trade_fees(
            self._trades(), order_ids={"zzz"},
            start_ms=1_700_000_000_000)
        assert out["fee_fill_count"] == 3
        assert out["fee_actual_usd"] == round(0.010050 + 0.005050, 6)


# ── Close-path lookup (fail-open) ─────────────────────────────────────────────

def _pos():
    return SimpleNamespace(opened_at_ms=1_700_000_000_000,
                           order_ids={"entry": "111", "stop": "222"})


def _cfg(**kw):
    base = dict(fee_actuals_enabled=True,
                sodex_rest_perps="https://mainnet-gw.sodex.dev/api/v1/perps",
                sodex_account_id=_WALLET, account_id="")
    base.update(kw)
    return SimpleNamespace(**base)


_SUMM = {"fee_actual_usd": 0.0151, "fee_maker_usd": 0.00505,
         "fee_taker_usd": 0.01005, "fee_fill_count": 2,
         "reason": None, "fee_coin": ["USDC"], "raw_fee_total": 0.0151}


class TestClosePathLookup:
    def test_books_summed_fees(self, monkeypatch):
        calls = []
        monkeypatch.setattr("main._query_fee_actuals_thread",
                            lambda *a, **k: calls.append(a) or dict(_SUMM))
        out = _lookup_fee_actuals("SOL-USD", _pos(), "sodex", _cfg())
        assert out["fee_actual_usd"] == 0.0151
        assert out["fee_maker_usd"] == 0.00505
        assert out["fee_taker_usd"] == 0.01005
        assert out["fee_fill_count"] == 2
        # Query ran once with wallet + window from the position open time.
        assert len(calls) == 1
        assert calls[0][1] == _WALLET and calls[0][3] == 1_700_000_000_000
        assert calls[0][4] == {"111", "222"}

    def test_record_carries_additive_fields(self, monkeypatch):
        monkeypatch.setattr("main._query_fee_actuals_thread",
                            lambda *a, **k: dict(_SUMM))
        res = _lookup_fee_actuals("SOL-USD", _pos(), "sodex", _cfg())
        rec = _build_trade_record(
            _full_pos(), exit_price=101.0, exit_reason="software_tp",
            net_pnl=0.5, fee_actual_usd=res["fee_actual_usd"],
            fee_maker_usd=res["fee_maker_usd"],
            fee_taker_usd=res["fee_taker_usd"],
            fee_fill_count=res["fee_fill_count"])
        assert rec.fee_actual_usd == 0.0151
        assert rec.fee_maker_usd == 0.00505
        assert rec.fee_taker_usd == 0.01005
        assert rec.fee_fill_count == 2
        # Semantics untouched: net_pnl/fee_est_usd not re-based.
        assert rec.net_pnl == 0.5
        assert rec.fee_usd is None

    def test_client_failure_fail_open(self, monkeypatch):
        # Client raises/timeout → thread helper returns None → null fields,
        # close accounting completes.
        monkeypatch.setattr("main._query_fee_actuals_thread",
                            lambda *a, **k: None)
        out = _lookup_fee_actuals("SOL-USD", _pos(), "sodex", _cfg())
        assert out["reason"] == "query_failed"
        assert out["fee_actual_usd"] is None
        assert out["fee_fill_count"] is None
        rec = _build_trade_record(
            _full_pos(), exit_price=101.0, exit_reason="software_tp",
            net_pnl=0.5, fee_actual_usd=out["fee_actual_usd"],
            fee_maker_usd=out["fee_maker_usd"],
            fee_taker_usd=out["fee_taker_usd"],
            fee_fill_count=out["fee_fill_count"])
        assert rec.fee_actual_usd is None
        assert rec.fee_fill_count is None

    def test_thread_helper_swallows_exceptions(self):
        # Bad endpoint → fetch raises inside the thread → None, never raises.
        out = __import__("main")._query_fee_actuals_thread(
            "http://127.0.0.1:1", _WALLET, "SOL-USD", 1, set())
        assert out is None

    def test_knob_off_no_query(self, monkeypatch):
        sentinel = Mock(side_effect=AssertionError("query must not run"))
        monkeypatch.setattr("main._query_fee_actuals_thread", sentinel)
        assert _lookup_fee_actuals("SOL-USD", _pos(), "sodex",
                                   _cfg(fee_actuals_enabled=False)) is None
        sentinel.assert_not_called()

    def test_non_sodex_venue_no_query(self, monkeypatch):
        sentinel = Mock(side_effect=AssertionError("query must not run"))
        monkeypatch.setattr("main._query_fee_actuals_thread", sentinel)
        assert _lookup_fee_actuals("SOL-USD", _pos(), "aster", _cfg()) is None
        sentinel.assert_not_called()

    def test_no_open_ts_fail_open(self, monkeypatch):
        monkeypatch.setattr("main._query_fee_actuals_thread", lambda *a, **k: dict(_SUMM))
        out = _lookup_fee_actuals("SOL-USD",
                                  SimpleNamespace(opened_at_ms=0, order_ids={}),
                                  "sodex", _cfg())
        assert out["reason"] == "no_open_ts"
        assert out["fee_actual_usd"] is None


def _full_pos():
    return SimpleNamespace(
        symbol="SOL-USD", side="long", entry_price=100.5, size=1.0,
        opened_at_ms=1_700_000_000_000, entry_coherence=7.0,
        tiers_fired=[], entry_htf="up", entry_session="us",
        entry_session_mult=1.0, leverage=6, stop_price=99.0,
        tp1_price=102.0, atr=0.5, max_adverse_excursion=0.1,
        max_favourable_excursion=0.6,
    )


# ── Fire-and-forget scheduler (2026-09-19 event-loop repair) ─────────────────
# The blocking _lookup_fee_actuals stalled the synchronous close path up to
# 5s per close. _schedule_fee_actuals returns immediately; the worker thread
# delivers to logs/fee_actuals.jsonl + the booked/unavailable event later.

import json as _json
import threading as _threading
import time as _time

import main as _main_mod
from main import _schedule_fee_actuals  # noqa: E402


def _reset_inflight():
    with _main_mod._fee_actuals_lock:
        _main_mod._fee_actuals_inflight = 0


def _drain(timeout=3.0):
    end = _time.time() + timeout
    while _time.time() < end:
        with _main_mod._fee_actuals_lock:
            if _main_mod._fee_actuals_inflight == 0:
                return True
        _time.sleep(0.02)
    return False


def _wait_for(pred, timeout=3.0):
    end = _time.time() + timeout
    while _time.time() < end:
        if pred():
            return True
        _time.sleep(0.02)
    return False


class TestFireAndForget:
    def setup_method(self):
        _reset_inflight()

    def teardown_method(self):
        _reset_inflight()

    def test_close_path_returns_fast_with_hanging_query(self, monkeypatch, tmp_path):
        # A hanging venue query must NOT stall the close path (was: 5s block).
        monkeypatch.setattr(_main_mod, "_FEE_ACTUALS_SIDECAR", str(tmp_path / "fee.jsonl"))
        release = _threading.Event()

        def _hang(*a, **k):
            release.wait(30)
            return dict(_SUMM)

        monkeypatch.setattr(_main_mod, "_query_fee_actuals_thread", _hang)
        t0 = _time.monotonic()
        scheduled = _schedule_fee_actuals("SOL-USD", _pos(), "sodex", _cfg())
        elapsed = _time.monotonic() - t0
        try:
            assert scheduled is True
            assert elapsed < 1.0, f"close path stalled {elapsed:.2f}s (old shape: up to 5s)"
        finally:
            release.set()
            assert _drain()

    def test_completion_delivers_sidecar_row_and_booked_event(self, monkeypatch, tmp_path):
        sidecar = tmp_path / "fee.jsonl"
        monkeypatch.setattr(_main_mod, "_FEE_ACTUALS_SIDECAR", str(sidecar))
        events = []
        monkeypatch.setattr(_main_mod, "logger",
                            SimpleNamespace(info=lambda ev, **kw: events.append((ev, kw))))
        monkeypatch.setattr(_main_mod, "_query_fee_actuals_thread",
                            lambda *a, **k: dict(_SUMM))
        assert _schedule_fee_actuals("SOL-USD", _pos(), "sodex", _cfg()) is True
        assert _wait_for(lambda: sidecar.exists() and sidecar.read_text().strip())
        row = _json.loads(sidecar.read_text().strip().splitlines()[-1])
        # Joinable at analysis time on the trade_db trade_id key.
        assert row["trade_id"] == "SOL-USD_1700000000000"
        assert row["symbol"] == "SOL-USD"
        assert row["fee_actual_usd"] == 0.0151
        assert row["fee_maker_usd"] == 0.00505
        assert row["fee_taker_usd"] == 0.01005
        assert row["fee_fill_count"] == 2
        assert _wait_for(lambda: any(e == "fee_actuals_booked" for e, _ in events))
        ev = dict(events)[ "fee_actuals_booked"]
        assert ev["fee_actual_usd"] == 0.0151

    def test_query_failure_delivers_unavailable(self, monkeypatch, tmp_path):
        sidecar = tmp_path / "fee.jsonl"
        monkeypatch.setattr(_main_mod, "_FEE_ACTUALS_SIDECAR", str(sidecar))
        events = []
        monkeypatch.setattr(_main_mod, "logger",
                            SimpleNamespace(info=lambda ev, **kw: events.append((ev, kw))))
        monkeypatch.setattr(_main_mod, "_query_fee_actuals_thread",
                            lambda *a, **k: None)
        assert _schedule_fee_actuals("SOL-USD", _pos(), "sodex", _cfg()) is True
        assert _wait_for(lambda: sidecar.exists() and sidecar.read_text().strip())
        row = _json.loads(sidecar.read_text().strip().splitlines()[-1])
        assert row["reason"] == "query_failed"
        assert row["fee_actual_usd"] is None
        assert _wait_for(lambda: any(e == "fee_actuals_unavailable" for e, _ in events))
        assert _drain()

    def test_inflight_cap_abandons_without_queue(self, monkeypatch, tmp_path):
        # Stale/slow lookups are abandoned — no unbounded thread/future growth.
        monkeypatch.setattr(_main_mod, "_FEE_ACTUALS_SIDECAR", str(tmp_path / "fee.jsonl"))
        events = []
        monkeypatch.setattr(_main_mod, "logger",
                            SimpleNamespace(info=lambda ev, **kw: events.append((ev, kw))))
        release = _threading.Event()
        monkeypatch.setattr(_main_mod, "_query_fee_actuals_thread",
                            lambda *a, **k: release.wait(30) or dict(_SUMM))
        try:
            assert _schedule_fee_actuals("SOL-USD", _pos(), "sodex", _cfg()) is True
            assert _schedule_fee_actuals("ETH-USD", _pos(), "sodex", _cfg()) is True
            # Third lookup hits the cap: abandoned, never queued.
            assert _schedule_fee_actuals("BTC-USD", _pos(), "sodex", _cfg()) is False
            assert any(e == "fee_actuals_unavailable" and kw.get("reason") == "inflight_cap"
                       for e, kw in events)
            with _main_mod._fee_actuals_lock:
                assert _main_mod._fee_actuals_inflight == 2
        finally:
            release.set()
            assert _drain()

    def test_knob_off_schedules_nothing(self, monkeypatch, tmp_path):
        sidecar = tmp_path / "fee.jsonl"
        monkeypatch.setattr(_main_mod, "_FEE_ACTUALS_SIDECAR", str(sidecar))
        sentinel = Mock(side_effect=AssertionError("query must not run"))
        monkeypatch.setattr(_main_mod, "_query_fee_actuals_thread", sentinel)
        assert _schedule_fee_actuals("SOL-USD", _pos(), "sodex",
                                     _cfg(fee_actuals_enabled=False)) is False
        sentinel.assert_not_called()
        assert not sidecar.exists()

    def test_non_sodex_schedules_nothing(self, monkeypatch, tmp_path):
        sidecar = tmp_path / "fee.jsonl"
        monkeypatch.setattr(_main_mod, "_FEE_ACTUALS_SIDECAR", str(sidecar))
        sentinel = Mock(side_effect=AssertionError("query must not run"))
        monkeypatch.setattr(_main_mod, "_query_fee_actuals_thread", sentinel)
        assert _schedule_fee_actuals("SOL-USD", _pos(), "aster", _cfg()) is False
        sentinel.assert_not_called()
        assert not sidecar.exists()

    def test_no_open_ts_sync_null_row(self, monkeypatch, tmp_path):
        sidecar = tmp_path / "fee.jsonl"
        monkeypatch.setattr(_main_mod, "_FEE_ACTUALS_SIDECAR", str(sidecar))
        events = []
        monkeypatch.setattr(_main_mod, "logger",
                            SimpleNamespace(info=lambda ev, **kw: events.append((ev, kw))))
        out = _schedule_fee_actuals(
            "SOL-USD", SimpleNamespace(opened_at_ms=0, order_ids={}), "sodex", _cfg())
        assert out is False
        row = _json.loads(sidecar.read_text().strip().splitlines()[-1])
        assert row["reason"] == "no_open_ts"
        assert row["fee_actual_usd"] is None
        assert any(e == "fee_actuals_unavailable" for e, _ in events)

    def test_close_path_source_is_nonblocking(self):
        # Structural pin: _record_close must schedule, never call the blocking
        # _lookup_fee_actuals (the 5s-stall shape this repair removes).
        with open(os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "main.py")) as f:
            src = f.read()
        start = src.index("def _record_close(")
        end = src.index("\n    def ", start + 1)
        body = src[start:end]
        assert "_schedule_fee_actuals(" in body
        assert "_lookup_fee_actuals(" not in body
