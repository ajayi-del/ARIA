"""SoSoValue feed pins (2026-08-29): pure brains (verdict / size tilt / tide /
poll / macro) + fetcher window discipline + parser robustness."""
import json
import os

import pytest

from data.sosovalue_feed import (SoSoValueFeed, flow_verdict, flow_size_mult,
                                 flow_poll, tide_aligned, macro_due_today,
                                 _MATERIALITY_USD)

M = _MATERIALITY_USD  # 150e6


class TestTideVetoWiring:
    def test_shadow_gate_registered(self):
        from intelligence.shadow_journal import REJECTION_EVENTS
        assert REJECTION_EVENTS["signal_rejected_etf_tide"] == "etf_tide"

    def test_kill_switch_default(self):
        from core.config import Settings
        assert Settings().etf_tide_veto_enabled is True

    @staticmethod
    def _main_src():
        from pathlib import Path
        return (Path(__file__).resolve().parent.parent / "main.py").read_text()

    def test_explosive_path_vetoed(self):
        # 2026-08-30 audit hole: the explosive executor is long-only MARKET
        # IOC — a long into an opposed tide must block BEFORE leverage set.
        src = self._main_src()
        i_block = src.index('explosive_blocked", symbol=sym,\n'
                            '                                    reason="etf_tide"')
        i_lev = src.index("_lev_cap = (int(getattr(config, "
                          "\"explosive_graduated_leverage\", 7))")
        assert i_block < i_lev

    def test_whale_probe_entry_vetoed(self):
        # 2026-08-30 audit hole (material): 50x probes are BTC/ETH/SOL-only —
        # exactly the symbols with tide data. Entry must block BEFORE the
        # 50x leverage set; runner-conversion abstain alone was not enough.
        src = self._main_src()
        i_block = src.index('whale_probe_blocked", symbol=sym,\n'
                            '                                    reason="etf_tide"')
        i_lev = src.index("symbol=sym, leverage=int(getattr(config, "
                          "\"whale_probe_leverage\", 50)))")
        assert i_block < i_lev


def _rows(inflows, date="2026-08-28", net_assets=1e9):
    """Newest-first rows with the given daily inflows."""
    return [{"date": f"2026-08-{28 - i:02d}", "total_net_inflow": v,
             "total_value_traded": 1e9, "total_net_assets": net_assets,
             "cum_net_inflow": 5e9} for i, v in enumerate(inflows)]


class TestFlowVerdict:
    def test_empty(self):
        assert flow_verdict("BTC", []) == {"symbol": "BTC", "rows": 0}

    def test_latest_and_sum3d(self):
        v = flow_verdict("BTC", _rows([-201.8e6, 242.2e6, 100e6, 50e6, 5e6]))
        assert v["last_inflow_usd"] == -201.8e6
        assert v["sum_3d_usd"] == round(-201.8e6 + 242.2e6 + 100e6, 0)
        assert v["last_date"] == "2026-08-28"
        assert v["net_assets_usd"] == 1e9

    def test_streak_signed(self):
        v = flow_verdict("BTC", _rows([-1e6, -2e6, -3e6, 4e6]))
        assert v["streak_days"] == -3
        v = flow_verdict("ETH", _rows([1e6, 2e6, -1e6]))
        assert v["streak_days"] == 2

    def test_streak_breaks_on_zero(self):
        v = flow_verdict("SOL", _rows([1e6, 0.0, 1e6]))
        assert v["streak_days"] == 1

    def test_none_inflow_treated_zero(self):
        rows = [{"date": "2026-08-28", "total_net_inflow": None}]
        v = flow_verdict("BTC", rows)
        assert v["last_inflow_usd"] == 0.0
        assert v["streak_days"] == 0


class TestFlowSizeMult:
    def test_missing_data_abstains(self):
        assert flow_size_mult({}, "long") == 1.0
        assert flow_size_mult({"rows": 0}, "short") == 1.0

    def test_below_materiality_neutral(self):
        v = {"rows": 3, "sum_3d_usd": M - 1}
        assert flow_size_mult(v, "long") == 1.0
        assert flow_size_mult(v, "short") == 1.0

    def test_aligned_and_opposed(self):
        inflow = {"rows": 3, "sum_3d_usd": 2 * M}
        outflow = {"rows": 3, "sum_3d_usd": -2 * M}
        assert flow_size_mult(inflow, "long") == 1.1
        assert flow_size_mult(inflow, "short") == 0.9
        assert flow_size_mult(outflow, "short") == 1.1
        assert flow_size_mult(outflow, "long") == 0.9

    def test_buy_alias(self):
        assert flow_size_mult({"rows": 1, "sum_3d_usd": 2 * M}, "BUY") == 1.1

    def test_staleness_decay(self):
        inflow = {"rows": 3, "sum_3d_usd": 2 * M}
        assert flow_size_mult(inflow, "long", age_hours=20) == 1.1
        assert flow_size_mult(inflow, "long", age_hours=48) == pytest.approx(1.05)
        assert flow_size_mult(inflow, "long", age_hours=80) == 1.0
        # Opposed side decays toward 1.0 too
        assert flow_size_mult(inflow, "short", age_hours=48) == pytest.approx(0.95)
        assert flow_size_mult(inflow, "short", age_hours=80) == 1.0


class TestTideAligned:
    def test_matrix(self):
        inflow = {"rows": 3, "sum_3d_usd": 2 * M}
        outflow = {"rows": 3, "sum_3d_usd": -2 * M}
        assert tide_aligned(inflow, "long") == "aligned"
        assert tide_aligned(inflow, "short") == "opposed"
        assert tide_aligned(outflow, "short") == "aligned"
        assert tide_aligned(outflow, "long") == "opposed"

    def test_neutral_cases(self):
        assert tide_aligned({"rows": 0}, "long") == "neutral"
        assert tide_aligned({"rows": 3, "sum_3d_usd": M / 2}, "long") == "neutral"
        assert tide_aligned({"rows": 3, "sum_3d_usd": 2 * M}, "long",
                            age_hours=80) == "neutral"


class TestFlowPoll:
    def test_quadrants(self):
        big_in = {"rows": 1, "last_inflow_usd": 2 * M, "last_date": "2026-08-28"}
        big_out = {"rows": 1, "last_inflow_usd": -2 * M, "last_date": "2026-08-28"}
        assert flow_poll(big_in, -1.5)["posture"] == "accumulation"
        assert flow_poll(big_in, 1.5)["posture"] == "confirmed_risk_on"
        assert flow_poll(big_out, 1.5)["posture"] == "distribution"
        assert flow_poll(big_out, -1.5)["posture"] == "confirmed_risk_off"

    def test_neutral_and_unknown(self):
        small = {"rows": 1, "last_inflow_usd": M / 2, "last_date": "d"}
        assert flow_poll(small, 2.0)["posture"] == "neutral"
        assert flow_poll({}, 2.0)["posture"] == "unknown"
        stale = {"rows": 1, "last_inflow_usd": 2 * M, "last_date": "d"}
        assert flow_poll(stale, 2.0, age_hours=80)["posture"] == "unknown"


class TestMacroDueToday:
    def test_hit_and_miss(self):
        events = [{"date": "2026-09-10", "events": ["CPI (MoM)", "CPI (YoY)"]},
                  {"date": "2026-08-31", "events": ["ISM Manufacturing PMI"]}]
        assert macro_due_today(events, "2026-09-10") == ["CPI (MoM)", "CPI (YoY)"]
        assert macro_due_today(events, "2026-09-11") == []
        assert macro_due_today([], "2026-09-10") == []
        assert macro_due_today(None, "2026-09-10") == []


class TestFeedDiscipline:
    def _feed(self, tmp_path, now):
        return SoSoValueFeed("KEY", log_dir=str(tmp_path), time_fn=lambda: now)

    def test_no_key_never_due(self, tmp_path):
        f = SoSoValueFeed("", log_dir=str(tmp_path))
        assert f._due() is False

    def test_window_gating(self, tmp_path):
        import calendar as cal
        # 2026-08-29 is a Saturday; pick a 06:00 UTC instant.
        t6 = cal.timegm((2026, 8, 29, 6, 30, 0, 0, 0, 0))
        t3 = cal.timegm((2026, 8, 29, 3, 30, 0, 0, 0, 0))
        assert self._feed(tmp_path, t3)._due() is False   # outside windows
        f = self._feed(tmp_path, t6)
        assert f._due() is True                            # first window, cold

    def test_once_per_window(self, tmp_path):
        import calendar as cal
        t6 = cal.timegm((2026, 8, 29, 6, 30, 0, 0, 0, 0, 0, 0))
        f = self._feed(tmp_path, t6)
        import time as _t
        w = (_t.gmtime(t6).tm_year, _t.gmtime(t6).tm_yday, _t.gmtime(t6).tm_hour)
        for s in f._symbols:
            f._last_fetch_day[s] = w
        f._last_macro_day = "2026-08-29"
        assert f._due() is False                           # all fetched

    def test_macro_only_due(self, tmp_path):
        import calendar as cal
        t6 = cal.timegm((2026, 8, 29, 6, 30, 0, 0, 0, 0, 0, 0))
        f = self._feed(tmp_path, t6)
        import time as _t
        w = (_t.gmtime(t6).tm_year, _t.gmtime(t6).tm_yday, _t.gmtime(t6).tm_hour)
        for s in f._symbols:
            f._last_fetch_day[s] = w
        assert f._due() is True                            # macro still cold

    def test_cache_roundtrip_and_age(self, tmp_path):
        import calendar as cal
        f = self._feed(tmp_path, 0.0)
        f._rows = {"BTC": _rows([-201.8e6])}
        f._macro = [{"date": "2026-09-10", "events": ["CPI (MoM)"]}]
        f._persist()
        f2 = self._feed(tmp_path, cal.timegm((2026, 8, 29, 12, 0, 0, 0, 0, 0)))
        assert f2.verdict("BTC")["last_inflow_usd"] == -201.8e6
        assert f2.macro_events()[0]["date"] == "2026-09-10"
        # Age: latest date 2026-08-28 00:00 → 2026-08-29 12:00 = 36h
        assert f2.flow_age_hours("BTC") == pytest.approx(36.0, abs=0.01)
        assert f2.flow_age_hours("ETH") == 999.0

    def test_corrupt_cache_swallowed(self, tmp_path):
        with open(os.path.join(str(tmp_path), "sosovalue_flows.json"), "w") as fh:
            fh.write("{not json")
        f = self._feed(tmp_path, 0.0)
        assert f._rows == {}
        assert f.verdict("BTC")["rows"] == 0

    @pytest.mark.asyncio
    async def test_fetch_parser(self, tmp_path, monkeypatch):
        import calendar as cal
        t6 = cal.timegm((2026, 8, 29, 6, 30, 0, 0, 0, 0, 0, 0))
        f = self._feed(tmp_path, t6)

        class _Resp:
            def __init__(self, d):
                self._d = d

            def json(self):
                return self._d

        class _Client:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, params=None, headers=None):
                assert headers["x-soso-api-key"] == "KEY"
                if "summary-history" in url:
                    if params["symbol"] == "ETH":
                        return _Resp({"code": 40001, "message": "rate limited"})
                    return _Resp({"code": 0, "data": _rows([1e6])})
                return _Resp({"code": 0, "data": [
                    {"date": "2026-09-10", "events": ["CPI (MoM)"]}]})

        monkeypatch.setattr("data.sosovalue_feed.httpx.AsyncClient", _Client)
        ok = await f.fetch_due()
        assert ok is True
        assert f.verdict("BTC")["rows"] == 1
        assert f.verdict("ETH")["rows"] == 0          # api error → skipped
        assert f.macro_events()[0]["events"] == ["CPI (MoM)"]
        # Persisted both files + jsonl history
        assert os.path.exists(os.path.join(str(tmp_path), "sosovalue_flows.json"))
        assert os.path.exists(os.path.join(str(tmp_path), "sosovalue_macro.json"))
        with open(os.path.join(str(tmp_path), "sosovalue_flows.jsonl")) as fh:
            hist = [json.loads(l) for l in fh if l.strip()]
        assert hist and hist[-1]["macro_days"] == 1

    @pytest.mark.asyncio
    async def test_fetch_network_failure_swallowed(self, tmp_path, monkeypatch):
        import calendar as cal
        t6 = cal.timegm((2026, 8, 29, 6, 30, 0, 0, 0, 0, 0, 0))
        f = self._feed(tmp_path, t6)

        class _Client:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, params=None, headers=None):
                raise RuntimeError("dns boom")

        monkeypatch.setattr("data.sosovalue_feed.httpx.AsyncClient", _Client)
        ok = await f.fetch_due()
        assert ok is False                                # nothing fetched
        assert f.verdict("BTC")["rows"] == 0              # fail-closed
