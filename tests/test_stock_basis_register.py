"""Tests for tools/stock_basis_register.py — no network.

Fixtures are the real probe responses recorded 2026-09-15:
  SoDEX GET /markets/mark-prices?symbol=ORCL-USD ->
    {"code":0,"timestamp":1789458599416,"data":[{"symbol":"ORCL-USD",
     "openInterest":"200","markPrice":"143.74","indexPrice":"143.69",
     "fundingRate":"0.00000625","nextFundingTime":1789459200000}]}
  Yahoo v8 chart ORCL -> chart.result[0].meta.regularMarketPrice = 144.79
  funding/history.py persists {sym: [{symbol, rate, timestamp_ms, source}]}.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import stock_basis_register as sbr


# ── Recorded fixtures (2026-09-15 probes) ────────────────────────────────────

SODEX_MARK_ORCL = {
    "code": 0,
    "timestamp": 1789458599416,
    "data": [{
        "symbol": "ORCL-USD",
        "openInterest": "200",
        "markPrice": "143.74",
        "indexPrice": "143.69",
        "fundingRate": "0.00000625",
        "nextFundingTime": 1789459200000,
    }],
}

SODEX_ORDERBOOK = {
    "code": 0,
    "data": {
        "bids": [["143.70", "5.0"], ["143.65", "3.0"]],
        "asks": [["143.78", "4.0"], ["143.82", "2.0"]],
    },
}

YAHOO_ORCL = {
    "chart": {
        "result": [{
            "meta": {"currency": "USD", "symbol": "ORCL",
                     "regularMarketPrice": 144.79, "chartPreviousClose": 140.1},
            "timestamp": [1789458540, 1789458600],
            "indicators": {"quote": [{"open": [144.7, 144.8],
                                      "high": [144.9, 144.85],
                                      "low": [144.6, 144.7],
                                      "close": [144.8, 144.79],
                                      "volume": [1000, 900]}]},
        }],
        "error": None,
    }
}


def funding_fixture(symbol="ORCL-USD", rates=(-0.0001, -0.00012, -0.00008),
                    base_ts_ms=1789450000000, step_ms=3600_000):
    return {symbol: [
        {"symbol": symbol, "rate": r, "timestamp_ms": base_ts_ms + i * step_ms,
         "source": "live"}
        for i, r in enumerate(rates)
    ]}


import calendar as _cal
from datetime import datetime as _dt, timezone as _tz

# 2026-09-15 (Tuesday) 15:00 UTC — inside RTH (13:30-20:00 UTC).
RTH_NOW = float(_cal.timegm(_dt(2026, 9, 15, 15, 0, tzinfo=_tz.utc).timetuple()))


# ── Basis math pins ──────────────────────────────────────────────────────────

class TestBasisMath:
    def test_basis_bps_positive(self):
        assert sbr.basis_bps(101.0, 100.0) == pytest.approx(100.0)

    def test_basis_bps_negative(self):
        assert sbr.basis_bps(99.5, 100.0) == pytest.approx(-50.0)

    def test_basis_bps_probe_values(self):
        # ORCL probe: mark 143.74 vs underlying 144.79
        assert sbr.basis_bps(143.74, 144.79) == pytest.approx(-72.519, abs=1e-3)

    def test_basis_bps_rejects_bad_prices(self):
        for perp, und in ((0, 100), (-1, 100), (100, 0), (100, -5)):
            with pytest.raises(ValueError):
                sbr.basis_bps(perp, und)


# ── Endpoint shape pins (recorded fixtures) ──────────────────────────────────

class TestEndpointShapes:
    def test_parse_sodex_mark(self):
        out = sbr.parse_sodex_mark(SODEX_MARK_ORCL)
        assert out["mark"] == pytest.approx(143.74)
        assert out["index"] == pytest.approx(143.69)
        assert out["funding_rate"] == pytest.approx(0.00000625)

    def test_parse_sodex_mark_empty(self):
        with pytest.raises(ValueError):
            sbr.parse_sodex_mark({"code": 0, "data": []})

    def test_parse_sodex_mark_zero_price(self):
        bad = {"data": [{"symbol": "X", "markPrice": "0"}]}
        with pytest.raises(ValueError):
            sbr.parse_sodex_mark(bad)

    def test_parse_yahoo_price(self):
        assert sbr.parse_yahoo_price(YAHOO_ORCL) == pytest.approx(144.79)

    def test_parse_yahoo_empty(self):
        with pytest.raises(ValueError):
            sbr.parse_yahoo_price({"chart": {"result": [], "error": None}})

    def test_parse_spread_bps_list_rows(self):
        spread = sbr.parse_spread_bps(SODEX_ORDERBOOK)
        mid = (143.70 + 143.78) / 2
        assert spread == pytest.approx((143.78 - 143.70) / mid * 1e4)

    def test_parse_spread_bps_dict_rows(self):
        payload = {"data": {"bids": [{"price": "100.0", "qty": "1"}],
                            "asks": [{"price": "100.1", "qty": "1"}]}}
        assert sbr.parse_spread_bps(payload) == pytest.approx(0.1 / 100.05 * 1e4)

    def test_parse_spread_bps_missing(self):
        assert sbr.parse_spread_bps({"data": {"bids": [], "asks": []}}) is None
        assert sbr.parse_spread_bps({}) is None


# ── Z-score windowing ────────────────────────────────────────────────────────

class TestZScore:
    def test_none_below_min_n(self):
        assert sbr.zscore(1.0, [0.5] * (sbr.Z_MIN_N - 1)) is None

    def test_none_zero_stdev(self):
        assert sbr.zscore(1.0, [0.5] * sbr.Z_MIN_N) is None

    def test_value_at_mean_is_zero(self):
        hist = [10.0, 20.0] * 10
        mean = sum(hist) / len(hist)
        assert sbr.zscore(mean, hist) == pytest.approx(0.0)

    def test_window_capped(self):
        # Oldest values far away; only the last Z_WINDOW should count.
        hist = [1e6] * 500 + [10.0, 20.0] * (sbr.Z_WINDOW // 2)
        z = sbr.zscore(30.0, hist)
        window = [10.0, 20.0] * (sbr.Z_WINDOW // 2)
        mean = sum(window) / len(window)
        var = sum((x - mean) ** 2 for x in window) / len(window)
        assert z == pytest.approx((30.0 - mean) / (var ** 0.5))

    def test_significant_deviation(self):
        hist = [0.0, 2.0] * 10   # mean 1.0, sd 1.0
        assert sbr.zscore(3.0, hist) == pytest.approx(2.0)


# ── Session segmentation ─────────────────────────────────────────────────────

class TestSegments:
    def _ts(self, y, m, d, hh, mm):
        import calendar
        from datetime import datetime, timezone
        return calendar.timegm(datetime(y, m, d, hh, mm, tzinfo=timezone.utc).timetuple())

    def test_rth_mid_session(self):
        # 2026-09-15 is a Tuesday
        assert sbr.segment_for(self._ts(2026, 9, 15, 15, 0)) == "RTH"

    def test_rth_open_boundary_inclusive(self):
        assert sbr.segment_for(self._ts(2026, 9, 15, 13, 30)) == "RTH"

    def test_rth_close_boundary_exclusive(self):
        assert sbr.segment_for(self._ts(2026, 9, 15, 20, 0)) == "OFF_HOURS"

    def test_pre_open_off_hours(self):
        assert sbr.segment_for(self._ts(2026, 9, 15, 13, 29)) == "OFF_HOURS"

    def test_overnight_off_hours(self):
        assert sbr.segment_for(self._ts(2026, 9, 15, 2, 0)) == "OFF_HOURS"

    def test_weekend_off_hours(self):
        # 2026-09-19 Saturday, would be RTH on a weekday
        assert sbr.segment_for(self._ts(2026, 9, 19, 15, 0)) == "OFF_HOURS"
        # 2026-09-20 Sunday
        assert sbr.segment_for(self._ts(2026, 9, 20, 15, 0)) == "OFF_HOURS"

    def test_winter_est_dst_aware(self):
        # 2026-01-15 is a Thursday, US on EST (UTC-5): true RTH = 14:30-21:00
        # UTC. The hardcoded 13:30-20:00 UTC window mis-tags BOTH edges here.
        if sbr._ET is None:
            pytest.skip("tz database unavailable — fallback path active")
        assert sbr.segment_for(self._ts(2026, 1, 15, 14, 0)) == "OFF_HOURS"  # 09:00 ET
        assert sbr.segment_for(self._ts(2026, 1, 15, 14, 30)) == "RTH"       # 09:30 ET
        assert sbr.segment_for(self._ts(2026, 1, 15, 15, 0)) == "RTH"        # 10:00 ET
        assert sbr.segment_for(self._ts(2026, 1, 15, 20, 30)) == "RTH"       # 15:30 ET
        assert sbr.segment_for(self._ts(2026, 1, 15, 21, 0)) == "OFF_HOURS"  # 16:00 ET


# ── Trailing run / episode state machine ─────────────────────────────────────

class TestTrailingRun:
    def _p(self, ts, z):
        return {"ts": ts, "z": z, "basis_bps": (z or 0) * 10}

    def test_run_of_three(self):
        prints = [self._p(0, 0.2), self._p(100, 1.5), self._p(200, 1.8),
                  self._p(300, 2.2)]
        run = sbr.trailing_run(prints, 1000)
        assert [r["ts"] for r in run] == [100, 200, 300]

    def test_sign_flip_breaks(self):
        prints = [self._p(0, 1.5), self._p(100, 1.8), self._p(200, -1.2)]
        run = sbr.trailing_run(prints, 1000)
        assert [r["ts"] for r in run] == [200]

    def test_insignificant_breaks(self):
        prints = [self._p(0, 1.5), self._p(100, 0.4), self._p(200, 1.7)]
        run = sbr.trailing_run(prints, 1000)
        assert [r["ts"] for r in run] == [200]

    def test_none_z_breaks(self):
        prints = [self._p(0, 1.5), self._p(100, None), self._p(200, 1.7)]
        run = sbr.trailing_run(prints, 1000)
        assert [r["ts"] for r in run] == [200]

    def test_gap_breaks(self):
        prints = [self._p(0, 1.5), self._p(100, 1.8), self._p(200 + 10_000, 1.9)]
        run = sbr.trailing_run(prints, 1000)
        assert [r["ts"] for r in run] == [10200]

    def test_funding_threshold_zero(self):
        prints = [{"ts": 0, "rate": 0.0}, {"ts": 100, "rate": 1e-6},
                  {"ts": 200, "rate": 2e-6}, {"ts": 300, "rate": 3e-6}]
        run = sbr.trailing_run(prints, 4000, key="rate", threshold=0.0)
        assert [r["ts"] for r in run] == [100, 200, 300]


class TestEpisodeStateMachine:
    def _run_prints(self, n, sign=1, start=0, step=900):
        return [{"ts": start + i * step, "z": sign * (1.5 + i * 0.1),
                 "basis_bps": sign * (15 + i)} for i in range(n)]

    def test_opens_at_third_print(self):
        reg = {"open": {}, "closed": []}
        run = self._run_prints(3)
        ev = sbr.reconcile_episode(reg, "ORCL-USD|OFF_HOURS|basis", run, 5000,
                                   lambda r: sbr.basis_episode_record("ORCL-USD", "OFF_HOURS", r))
        assert ev == "opened"
        ep = reg["open"]["ORCL-USD|OFF_HOURS|basis"]
        assert ep["n_prints"] == 3 and ep["sign"] == 1 and ep["status"] == "open"
        assert ep["opened_ts"] == run[0]["ts"]

    def test_no_open_below_three(self):
        reg = {"open": {}, "closed": []}
        ev = sbr.reconcile_episode(reg, "K", self._run_prints(2), 5000,
                                   lambda r: {})
        assert ev is None and reg["open"] == {}

    def test_extend_then_close_on_break(self):
        reg = {"open": {}, "closed": []}
        run = self._run_prints(3)
        mk = lambda r: sbr.basis_episode_record("ORCL-USD", "OFF_HOURS", r)
        sbr.reconcile_episode(reg, "K", run, 5000, mk)
        run4 = self._run_prints(4)
        ev = sbr.reconcile_episode(reg, "K", run4, 6000, mk)
        assert ev == "extended" and reg["open"]["K"]["n_prints"] == 4
        # run breaks (latest two prints insignificant)
        ev = sbr.reconcile_episode(reg, "K", run4[:2], 7000, mk)
        assert ev == "closed"
        assert reg["open"] == {}
        closed = reg["closed"][0]
        assert closed["status"] == "closed" and closed["end_ts"] == run4[-1]["ts"]

    def test_prune_closed_90d(self):
        reg = {"open": {}, "closed": [
            {"end_ts": 1000}, {"end_ts": 2000}]}
        now = 1000 + sbr.CLOSED_RETENTION_S + 10   # cutoff = 1010
        sbr.prune_closed(reg, now)
        assert reg["closed"] == [{"end_ts": 2000}]

    def test_sign_flip_closes_old_leg_opens_new(self):
        # Missed cron ticks hid the break: the open episode must not silently
        # change sign in place while keeping its original opened_ts.
        reg = {"open": {}, "closed": []}
        mk = lambda r: sbr.basis_episode_record("ORCL-USD", "RTH", r)
        pos = [{"ts": t, "z": 1.5, "basis_bps": 15.0} for t in (0, 900, 1800)]
        assert sbr.reconcile_episode(reg, "K", pos, 2000, mk) == "opened"
        neg = [{"ts": t, "z": -1.7, "basis_bps": -18.0}
               for t in (3600, 4500, 5400)]
        assert sbr.reconcile_episode(reg, "K", neg, 6000, mk) == "opened"
        assert reg["open"]["K"]["sign"] == -1
        assert reg["open"]["K"]["opened_ts"] == 3600
        old = reg["closed"][0]
        assert old["sign"] == 1 and old["status"] == "closed"
        assert old["end_ts"] == 1800     # closes on its own last print


# ── JSONL tolerance + atomic writes ──────────────────────────────────────────

class TestIo:
    def test_one_bad_line_tolerated(self, tmp_path):
        p = tmp_path / "x.jsonl"
        p.write_text('{"a": 1}\nNOT-JSON\n{"a": 2}\n{"broken": \n')
        rows = sbr.read_jsonl_tolerant(str(p))
        assert rows == [{"a": 1}, {"a": 2}]

    def test_missing_file_returns_empty(self, tmp_path):
        assert sbr.read_jsonl_tolerant(str(tmp_path / "nope.jsonl")) == []

    def test_atomic_write(self, tmp_path):
        p = str(tmp_path / "ep.json")
        sbr.atomic_write_json(p, {"open": {"K": {}}, "closed": []})
        with open(p) as f:
            assert json.load(f)["open"] == {"K": {}}
        assert not os.path.exists(p + ".tmp")


# ── Funding episodes ─────────────────────────────────────────────────────────

class TestFundingEpisodes:
    def test_three_same_sign_opens_with_carry(self, tmp_path):
        fx = funding_fixture(rates=(-0.0001, -0.00012, -0.00008))
        fp = str(tmp_path / "funding.json")
        with open(fp, "w") as f:
            json.dump(fx, f)
        reg = {"open": {}, "closed": []}
        now = fx["ORCL-USD"][-1]["timestamp_ms"] / 1000 + 60
        errors = {}
        events = sbr.funding_section(now, fp, reg, errors)
        assert events == ["ORCL-USD|funding:opened"]
        ep = reg["open"]["ORCL-USD|funding"]
        assert ep["kind"] == "funding" and ep["sign"] == -1
        assert ep["receiving_side"] == "longs"   # negative rate: longs receive
        mean_rate = (-0.0001 - 0.00012 - 0.00008) / 3
        assert ep["annualized_carry"] == pytest.approx(mean_rate * 24 * 365, abs=1e-9)

    def test_positive_rate_shorts_receive(self, tmp_path):
        fx = funding_fixture(rates=(0.0002, 0.0001, 0.0003))
        fp = str(tmp_path / "funding.json")
        with open(fp, "w") as f:
            json.dump(fx, f)
        reg = {"open": {}, "closed": []}
        now = fx["ORCL-USD"][-1]["timestamp_ms"] / 1000 + 60
        sbr.funding_section(now, fp, reg, {})
        assert reg["open"]["ORCL-USD|funding"]["receiving_side"] == "shorts"

    def test_mixed_signs_no_episode(self, tmp_path):
        fx = funding_fixture(rates=(-0.0001, 0.00012, -0.00008))
        fp = str(tmp_path / "funding.json")
        with open(fp, "w") as f:
            json.dump(fx, f)
        reg = {"open": {}, "closed": []}
        now = fx["ORCL-USD"][-1]["timestamp_ms"] / 1000 + 60
        events = sbr.funding_section(now, fp, reg, {})
        assert events == [] and reg["open"] == {}

    def test_missing_file_self_errors(self, tmp_path):
        reg = {"open": {}, "closed": []}
        errors = {}
        events = sbr.funding_section(1000, str(tmp_path / "nope.json"), reg, errors)
        assert events == [] and errors["funding"].startswith("read:")

    def test_stale_file_self_errors(self, tmp_path):
        fx = funding_fixture(rates=(-0.0001,) * 4)
        fp = str(tmp_path / "funding.json")
        with open(fp, "w") as f:
            json.dump(fx, f)
        reg = {"open": {}, "closed": []}
        errors = {}
        stale_now = fx["ORCL-USD"][-1]["timestamp_ms"] / 1000 + 10 * 3600
        events = sbr.funding_section(stale_now, fp, reg, errors)
        assert events == [] and errors["funding"].startswith("stale:")


# ── End-to-end with injected fetchers ────────────────────────────────────────

class FakeResp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def json(self):
        return self._payload


class FakeClient:
    """Routes by URL to recorded fixture payloads."""

    def __init__(self, mark_px=143.74, yahoo_px=144.79):
        self.mark_px, self.yahoo_px = mark_px, yahoo_px

    def get(self, url, params=None):
        if "mark-prices" in url:
            payload = json.loads(json.dumps(SODEX_MARK_ORCL))
            payload["data"][0]["markPrice"] = str(self.mark_px)
            return FakeResp(payload)
        if "orderbook" in url:
            return FakeResp(SODEX_ORDERBOOK)
        if "yahoo" in url:
            payload = json.loads(json.dumps(YAHOO_ORCL))
            payload["chart"]["result"][0]["meta"]["regularMarketPrice"] = self.yahoo_px
            return FakeResp(payload)
        raise AssertionError(f"unexpected url {url}")


class TestEndToEnd:
    def _paths(self, tmp_path):
        return (str(tmp_path / "prints.jsonl"), str(tmp_path / "episodes.json"),
                str(tmp_path / "funding.json"))

    def _fresh_funding(self):
        # Timestamps anchored just before RTH_NOW so the staleness guard passes.
        return funding_fixture(base_ts_ms=int((RTH_NOW - 3 * 3600) * 1000))

    def test_first_run_prints_and_no_episode(self, tmp_path):
        pp, ep, fp = self._paths(tmp_path)
        with open(fp, "w") as f:
            json.dump(self._fresh_funding(), f)
        # RTH_NOW = 2026-09-15 15:00 UTC, inside RTH
        now = RTH_NOW
        summary = sbr.run(now=now, client=FakeClient(), prints_path=pp,
                          episodes_path=ep, funding_path=fp, verbose=False)
        # 20 mapped symbols fetched (UNITREE skipped)
        assert summary["prints"] == 20
        assert summary["unmapped_skipped"] == ["UNITREE-USD"]
        assert summary["errors"] == {}
        rows = sbr.read_jsonl_tolerant(pp)
        assert len(rows) == 20
        orcl = next(r for r in rows if r["symbol"] == "ORCL-USD")
        assert orcl["segment"] == "RTH"
        assert orcl["basis_bps"] == pytest.approx(-72.519, abs=1e-3)
        assert orcl["z"] is None            # no history yet
        assert orcl["spread_bps"] is not None
        # z None -> no episode
        with open(ep) as f:
            reg = json.load(f)
        assert all(ep_["kind"] != "basis" for ep_ in reg["open"].values())

    def test_dedup_same_minute(self, tmp_path):
        pp, ep, fp = self._paths(tmp_path)
        with open(fp, "w") as f:
            json.dump(self._fresh_funding(), f)
        now = RTH_NOW
        sbr.run(now=now, client=FakeClient(), prints_path=pp,
                episodes_path=ep, funding_path=fp, verbose=False)
        summary = sbr.run(now=now + 30, client=FakeClient(), prints_path=pp,
                          episodes_path=ep, funding_path=fp, verbose=False)
        assert summary["prints"] == 0       # same ts_minute -> deduped
        assert len(sbr.read_jsonl_tolerant(pp)) == 20

    def test_episode_opens_after_persistent_history(self, tmp_path):
        pp, ep, fp = self._paths(tmp_path)
        with open(fp, "w") as f:
            json.dump(self._fresh_funding(), f)
        now = RTH_NOW
        # Seed 10 RTH prints with flat basis ~-70, then the live print
        # deviates hard negative -> z significant, same sign as the last two
        # seeded prints -> trailing run of 3 -> episode opens.
        seed = []
        for i in range(10):
            seed.append({"ts": int(now) - (11 - i) * 900, "symbol": "ORCL-USD",
                         "segment": "RTH", "perp_mark": 143.7,
                         "underlying": 144.79, "basis_bps": -70.0 + (i % 2),
                         "z": None, "spread_bps": None})
        # last two seed prints already deviating -> build the run
        seed[-2]["basis_bps"] = -200.0
        seed[-2]["z"] = -30.0
        seed[-1]["basis_bps"] = -210.0
        seed[-1]["z"] = -32.0
        with open(pp, "w") as f:
            for rec in seed:
                f.write(json.dumps(rec) + "\n")
        # Live fetch: mark crashes vs underlying -> strongly negative basis
        summary = sbr.run(now=now, client=FakeClient(mark_px=141.0, yahoo_px=144.79),
                          prints_path=pp, episodes_path=ep, funding_path=fp,
                          verbose=False)
        assert "ORCL-USD|RTH|basis:opened" in summary["episode_events"]
        with open(ep) as f:
            reg = json.load(f)
        ep_rec = reg["open"]["ORCL-USD|RTH|basis"]
        assert ep_rec["n_prints"] == 3 and ep_rec["sign"] == -1
        assert ep_rec["kind"] == "basis" and ep_rec["segment"] == "RTH"

    def test_per_symbol_self_error_does_not_kill_run(self, tmp_path):
        class HalfDeadClient(FakeClient):
            def get(self, url, params=None):
                if "yahoo" in url and "TSM" in url:
                    return FakeResp({}, status=502)
                return super().get(url, params)

        pp, ep, fp = self._paths(tmp_path)
        with open(fp, "w") as f:
            json.dump(self._fresh_funding(), f)
        summary = sbr.run(now=RTH_NOW, client=HalfDeadClient(),
                          prints_path=pp, episodes_path=ep, funding_path=fp,
                          verbose=False)
        assert summary["prints"] == 19      # TSM-USD self-errored
        assert "TSM-USD" in summary["errors"]

    def test_rebase_step_resets_z_window_and_persists(self, tmp_path):
        pp, ep, fp = self._paths(tmp_path)
        with open(fp, "w") as f:
            json.dump(self._fresh_funding(), f)
        now = RTH_NOW
        # Seed 10 RTH prints near zero basis.
        seed = [{"ts": int(now) - (11 - i) * 900, "symbol": "ORCL-USD",
                 "segment": "RTH", "perp_mark": 144.7 + (i % 2) * 0.1,
                 "underlying": 144.79,
                 "basis_bps": -6.0 + (i % 2), "z": 0.1 * (i % 2),
                 "spread_bps": None} for i in range(10)]
        with open(pp, "w") as f:
            for rec in seed:
                f.write(json.dumps(rec) + "\n")
        # Live fetch: the perp mark jumped 6x (synthetic rebase).
        summary = sbr.run(now=now, client=FakeClient(mark_px=868.0,
                                                     yahoo_px=144.79),
                          prints_path=pp, episodes_path=ep, funding_path=fp,
                          verbose=False)
        rows = sbr.read_jsonl_tolerant(pp)
        orcl = [r for r in rows if r["symbol"] == "ORCL-USD"]
        reset = orcl[-1]
        assert reset.get("rebase_reset") is True
        assert reset["z"] is None               # window wiped, abstain
        # No fabricated episode off the structural break.
        with open(ep) as f:
            reg = json.load(f)
        assert all(e.get("kind") != "basis" for e in reg["open"].values())
        # Persistence: next run must NOT reload the pre-rebase rows — the
        # marker in the jsonl excludes them from the z window again.
        summary2 = sbr.run(now=now + 900, client=FakeClient(mark_px=868.0,
                                                            yahoo_px=144.79),
                           prints_path=pp, episodes_path=ep, funding_path=fp,
                           verbose=False)
        rows2 = [r for r in sbr.read_jsonl_tolerant(pp)
                 if r["symbol"] == "ORCL-USD"]
        assert rows2[-1]["z"] is None           # n=1 post-reset history < 8
        assert not rows2[-1].get("rebase_reset")  # steady at the new level

    def test_symbol_map_snapshot_fallback(self, monkeypatch):
        import builtins
        real_import = builtins.__import__

        def broken_import(name, *a, **kw):
            if name.startswith("data."):
                raise ImportError("repo broken")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", broken_import)
        m, source = sbr.load_symbol_map()
        assert source == "snapshot_2026-09-15"
        assert m["ORCL-USD"] == "ORCL" and "UNITREE-USD" not in m

    def test_symbol_map_repo_import(self):
        m, source = sbr.load_symbol_map()
        assert source == "repo"
        assert m["SAMSUNG-USD"] == "005930.KS"
