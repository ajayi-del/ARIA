"""SoSoValue feed pins (2026-08-29): pure brains (verdict / size tilt / tide /
poll / macro) + fetcher window discipline + parser robustness."""
import json
import os

import pytest

from data.sosovalue_feed import (SoSoValueFeed, flow_verdict, flow_size_mult,
                                 flow_poll, tide_aligned, tide_accel_state,
                                 etf_tide_accel_veto_enabled, macro_due_today,
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

    def test_accel_fields_four_rows(self):
        # rows[0:3] sum − rows[1:4] sum = newest day − dropped day
        v = flow_verdict("BTC", _rows([-250e6, -300e6, -200e6, -500e6, 1e6]))
        assert v["sum_3d_usd"] == -750e6
        assert v["prev_3d_usd"] == -1000e6
        assert v["accel_3d_usd"] == 250e6   # outflow decelerating

    def test_accel_none_below_four_rows(self):
        v = flow_verdict("BTC", _rows([100e6, 100e6, 100e6]))
        assert v["prev_3d_usd"] is None
        assert v["accel_3d_usd"] is None


class TestTideAccelState:
    """The leading read on the lagging tide (operator directive 2026-09-03)."""

    def test_negative_tide_decelerating_is_toward_zero(self):
        # the operator's example: -250M yesterday vs -300M the day before —
        # outflow slowing, tide about to flip → opposed veto loosens
        v = flow_verdict("BTC", _rows([-250e6, -300e6, -200e6, -500e6]))
        assert tide_accel_state(v) == "toward_zero"

    def test_negative_tide_accelerating_away(self):
        v = flow_verdict("BTC", _rows([-500e6, -200e6, -100e6, +400e6]))
        assert v["accel_3d_usd"] == -900e6
        assert tide_accel_state(v) == "away_from_zero"

    def test_positive_tide_fading_is_toward_zero(self):
        v = flow_verdict("BTC", _rows([100e6, 200e6, 300e6, 800e6]))
        assert tide_accel_state(v) == "toward_zero"

    def test_flat_inside_materiality(self):
        v = flow_verdict("BTC", _rows([-200e6, -210e6, -190e6, -240e6]))
        assert abs(v["accel_3d_usd"]) < M
        assert tide_accel_state(v) == "flat"

    def test_unknown_when_window_or_tide_missing(self):
        assert tide_accel_state(flow_verdict("BTC", [])) == "unknown"
        assert tide_accel_state(flow_verdict("BTC", _rows([1e6, 2e6, 3e6]))) == "unknown"
        # tide below materiality → no tide to accelerate
        v = flow_verdict("BTC", _rows([10e6, -20e6, 30e6, -400e6]))
        assert tide_accel_state(v) == "unknown"

    def test_kill_switch(self, monkeypatch):
        monkeypatch.setenv("ETF_TIDE_ACCEL_VETO_ENABLED", "false")
        assert etf_tide_accel_veto_enabled() is False
        monkeypatch.setenv("ETF_TIDE_ACCEL_VETO_ENABLED", "true")
        assert etf_tide_accel_veto_enabled() is True

    def test_veto_modulation_wired_before_hugo(self):
        # call-site pin: the accel check must precede the Hugo downgrade —
        # a decelerating opposed tide loosens even when Hugo is silent.
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "main.py").read_text()
        i_accel = src.index('tide_accel_state(_fv_t)')
        i_hugo = src.index('elif _hugo_sym_aligned(symbol, _sig_direction):',
                           src.index('signal_rejected_etf_tide') - 4000)
        assert i_accel < i_hugo


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
        # Age: latest date Friday 2026-08-28 00:00 → Saturday 2026-08-29 12:00
        # raw = 36h, minus 1 non-trading date (Saturday) = 12h effective
        # (calendar-adjusted age, 2026-08-31 — ETF_TIDE_CALENDAR_AGE_ENABLED
        # default true).
        assert f2.flow_age_hours("BTC") == pytest.approx(12.0, abs=0.01)
        assert f2.flow_age_hours("ETH") == 999.0

    def test_calendar_age_kill_switch_legacy(self, tmp_path, monkeypatch):
        import calendar as cal
        monkeypatch.setenv("ETF_TIDE_CALENDAR_AGE_ENABLED", "false")
        f = self._feed(tmp_path, 0.0)
        f._rows = {"BTC": _rows([-201.8e6])}
        f._persist()
        f2 = self._feed(tmp_path, cal.timegm((2026, 8, 29, 12, 0, 0, 0, 0, 0)))
        assert f2.flow_age_hours("BTC") == pytest.approx(36.0, abs=0.01)  # raw

    def test_calendar_age_monday_morning_friday_data(self, tmp_path):
        # The 2026-08-31 05:24 UTC leak: Friday 08-28 data read 77h old on
        # Monday morning → spurious >72h abstain → opposed-tide ETH short
        # leaked. Effective age must be ~29h (one trading session).
        import calendar as cal
        f = self._feed(tmp_path, cal.timegm((2026, 8, 31, 5, 24, 0, 0, 0, 0)))
        f._rows = {"ETH": _rows([529.0e6], date="2026-08-28")}
        age = f.flow_age_hours("ETH")
        assert age == pytest.approx(29.4, abs=0.1)
        assert age < 72.0   # veto stays ARMED — the whole point

    def test_calendar_age_holiday_monday(self, tmp_path):
        # Labor Day Mon 2026-09-07: Friday 09-04 data on Tuesday morning =
        # Sat+Sun+holiday = 3 non-trading days → ~29h effective, not 101.
        import calendar as cal
        f = self._feed(tmp_path, cal.timegm((2026, 9, 8, 5, 0, 0, 0, 0, 0)))
        f._rows = {"BTC": [{"date": "2026-09-04", "total_net_inflow": 1.0}]}
        assert f.flow_age_hours("BTC") == pytest.approx(29.0, abs=0.1)

    def test_calendar_age_fail_closed_stale_feed(self, tmp_path):
        # A week-dead feed still abstains: weekend+holiday subtraction (3d)
        # cannot rescue 7.5×24h raw → 108h effective > 72h.
        import calendar as cal
        f = self._feed(tmp_path, cal.timegm((2026, 9, 7, 12, 0, 0, 0, 0, 0)))
        f._rows = {"BTC": [{"date": "2026-08-31", "total_net_inflow": 1.0}]}
        assert f.flow_age_hours("BTC") > 72.0

    def test_calendar_age_unknown_year_fail_closed(self):
        # Dates outside the holiday-table years: weekdays count as TRADING
        # (fail-closed). 2030-08-30 is a Friday; Monday 2030-09-02 raw 77h →
        # only the weekend subtracts (holidays unknown), never more.
        from data.sosovalue_feed import etf_calendar_adjusted_age
        import calendar as cal
        now = cal.timegm((2030, 9, 2, 5, 0, 0, 0, 0, 0))
        age = etf_calendar_adjusted_age("2030-08-30", 77.0, now)
        assert age == pytest.approx(29.0, abs=0.01)

    def test_calendar_age_bad_input_returns_raw(self):
        from data.sosovalue_feed import etf_calendar_adjusted_age
        assert etf_calendar_adjusted_age("garbage", 50.0, 0.0) == 50.0

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
