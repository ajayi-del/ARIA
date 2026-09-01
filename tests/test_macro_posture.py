"""Macro posture precompute (tools/macro_posture.py, 2026-09-01, operator
directive "ultrathink and wire it", deterministic + cost-effective).

Observer-class plane: ETF flow breadth from the bot's SoSoValue caches,
stablecoin net issuance from DefiLlama (date-disciplined cache, kill
switch), macro calendar cache, and a deterministic aria.log tail parse
crossed with the tide state. The watchdog spends tokens on judgment, not
arithmetic. Every section self-errors; missing data fails open."""
import calendar
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.macro_posture as mp  # noqa: E402

_NOW = calendar.timegm((2026, 9, 1, 12, 0, 0, 0, 0, 0))  # Tue 2026-09-01 12:00 UTC


def _row(date, inflow, assets=100e9):
    return {"date": date, "total_net_inflow": inflow,
            "total_value_traded": 1e9, "total_net_assets": assets,
            "cum_net_inflow": 50e9}


def _flows_cache():
    return {
        "BTC": [_row("2026-08-31", -202e6), _row("2026-08-28", -50e6),
                _row("2026-08-27", 80e6)],
        "ETH": [_row("2026-08-31", 102e6), _row("2026-08-28", 150e6),
                _row("2026-08-27", 200e6), _row("2026-08-26", 90e6),
                _row("2026-08-25", 60e6)],
        "SOL": [_row("2026-08-31", 154e6), _row("2026-08-28", 30e6),
                _row("2026-08-27", 20e6)],
    }


class TestBreadthShares:
    def test_abs3d_shares_sum_to_one(self):
        from data.sosovalue_feed import flow_verdict
        vs = {s: flow_verdict(s, r) for s, r in _flows_cache().items()}
        shares = mp.breadth_shares(vs)
        assert abs(sum(shares.values()) - 1.0) < 0.01
        # ETH |3d| = 452M, BTC = 172M, SOL = 204M → ETH dominates
        assert shares["ETH"] > shares["SOL"] > shares["BTC"]

    def test_rows_less_entries_skipped(self):
        assert mp.breadth_shares({"BTC": {"rows": 0}}) == {}
        shares = mp.breadth_shares({"BTC": {"rows": 0},
                                    "ETH": {"rows": 3, "sum_3d_usd": -200e6}})
        assert shares == {"ETH": 1.0}


class TestFlowSection:
    def test_full_section_with_wow_history(self, tmp_path):
        (tmp_path / "sosovalue_flows.json").write_text(json.dumps(_flows_cache()))
        prior = {"ts": _NOW - 7 * 86400, "flows": {
            "BTC": {"rows": 5, "sum_3d_usd": 500e6},
            "ETH": {"rows": 5, "sum_3d_usd": 300e6},
            "SOL": {"rows": 5, "sum_3d_usd": 100e6}}}
        (tmp_path / "sosovalue_flows.jsonl").write_text(json.dumps(prior) + "\n")
        out = mp.flow_section(str(tmp_path), _NOW)
        assert set(out["symbols"]) == {"BTC", "ETH", "SOL"}
        assert out["symbols"]["BTC"]["sum_3d_usd"] == -172e6
        assert out["symbols"]["ETH"]["streak_days"] == 5
        assert "age_hours" in out["symbols"]["BTC"]
        assert "tide_long" in out["symbols"]["ETH"]
        assert abs(sum(out["breadth_abs3d_now"].values()) - 1.0) < 0.01
        assert out["sol_share_abs3d_now"] is not None
        # 7d ago SOL was 100/900 = 0.1111
        assert out["sol_share_abs3d_7d_ago"] == 0.1111
        assert "XRP/HYPE" in out["coverage_note"]

    def test_missing_cache_fails_open(self, tmp_path):
        out = mp.flow_section(str(tmp_path), _NOW)
        assert out["symbols"] == {}
        assert out["breadth_abs3d_now"] == {}
        assert out["sol_share_abs3d_7d_ago"] is None

    def test_one_bad_history_line(self, tmp_path):
        (tmp_path / "sosovalue_flows.json").write_text(json.dumps(_flows_cache()))
        bad = "not json\n" + json.dumps({"ts": _NOW - 7 * 86400, "flows": {
            "BTC": {"rows": 1, "sum_3d_usd": 1e6}}}) + "\n"
        (tmp_path / "sosovalue_flows.jsonl").write_text(bad)
        out = mp.flow_section(str(tmp_path), _NOW)
        assert out["breadth_abs3d_now"]  # section survived the bad line


class TestStablecoinVerdict:
    def test_thresholds(self):
        assert mp.stablecoin_verdict(5e8) == "expanding"
        assert mp.stablecoin_verdict(-5e8) == "contracting"
        assert mp.stablecoin_verdict(4.99e8) == "flat"
        assert mp.stablecoin_verdict(0.0) == "flat"


_FAKE_LLAMA = {"peggedAssets": [
    {"symbol": "USDT",
     "circulating": {"peggedUSD": 183.3e9},
     "circulatingPrevDay": {"peggedUSD": 183.1e9},
     "circulatingPrevWeek": {"peggedUSD": 182.8e9},
     "circulatingPrevMonth": {"peggedUSD": 180.0e9}},
    {"symbol": "USDC",
     "circulating": {"peggedUSD": 73.46e9},
     "circulatingPrevDay": {"peggedUSD": 73.40e9},
     "circulatingPrevWeek": {"peggedUSD": 73.36e9},
     "circulatingPrevMonth": {"peggedUSD": 72.0e9}},
    {"symbol": "DAI",
     "circulating": {"peggedUSD": 5e9}},
]}


class TestFetchStablecoins:
    def test_parses_usdt_usdc_only(self, monkeypatch):
        class _R:
            def json(self):
                return _FAKE_LLAMA
        monkeypatch.setattr("httpx.get", lambda *a, **k: _R())
        out = mp._fetch_stablecoins()
        assert set(out) == {"USDT", "USDC"}
        assert out["USDT"]["circulating"] == 183.3e9
        assert out["USDC"]["prev_week"] == 73.36e9


class TestStablecoinSection:
    def test_fetch_then_cache_hit(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mp, "_fetch_stablecoins", lambda: {
            s: {"circulating": a["circulating"]["peggedUSD"],
                "prev_day": a["circulatingPrevDay"]["peggedUSD"],
                "prev_week": a["circulatingPrevWeek"]["peggedUSD"],
                "prev_month": a["circulatingPrevMonth"]["peggedUSD"]}
            for s, a in ((x["symbol"], x) for x in _FAKE_LLAMA["peggedAssets"])
            if s in ("USDT", "USDC")})
        out = mp.stablecoin_section(str(tmp_path), _NOW)
        assert out["fetch"] == "fetched"
        assert out["delta_7d"] == 6.0e8        # +500M USDT +100M USDC
        assert out["verdict"] == "expanding"
        assert (tmp_path / "stablecoin_liquidity.json").exists()

        def _boom():
            raise RuntimeError("network down")
        monkeypatch.setattr(mp, "_fetch_stablecoins", _boom)
        out2 = mp.stablecoin_section(str(tmp_path), _NOW)
        assert out2["fetch"] == "cache_hit" and out2["stale"] is False

    def test_stale_cache_on_fetch_failure(self, tmp_path, monkeypatch):
        yesterday = {"date": "2026-08-31", "fetched_ts": _NOW - 86400,
                     "data": {"USDT": {"circulating": 1e9, "prev_day": 1e9,
                                       "prev_week": 1e9, "prev_month": 1e9},
                              "USDC": {"circulating": 1e9, "prev_day": 1e9,
                                       "prev_week": 1e9, "prev_month": 1e9}}}
        (tmp_path / "stablecoin_liquidity.json").write_text(json.dumps(yesterday))

        def _boom():
            raise RuntimeError("network down")
        monkeypatch.setattr(mp, "_fetch_stablecoins", _boom)
        out = mp.stablecoin_section(str(tmp_path), _NOW)
        assert out["fetch"] == "stale_cache" and out["stale"] is True
        assert out["verdict"] == "flat"

    def test_kill_switch_and_no_data(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MACRO_POSTURE_DEFILLAMA_ENABLED", "false")
        out = mp.stablecoin_section(str(tmp_path), _NOW)
        assert out == {"error": "no_data"}


class TestPositioningSection:
    def _write_log(self, tmp_path):
        lines = [
            {"timestamp": "2026-08-31T23:59:00Z", "event": "pnl_attribution",
             "breakdown": "BTC-USD:L@80000x0.001"},   # yesterday — ignored
            {"timestamp": "2026-09-01T09:00:00Z", "event": "signal_rejected_etf_tide",
             "symbol": "ETH-USD"},
            {"timestamp": "2026-09-01T10:00:00Z", "event": "execution_decision",
             "symbol": "SOL-USD", "direction": "long", "decision": "approved"},
            {"timestamp": "2026-09-01T10:05:00Z", "event": "execution_decision",
             "symbol": "XRP-USD", "direction": "short", "decision": "rejected"},
            {"timestamp": "2026-09-01T11:00:00Z", "event": "pnl_attribution",
             "breakdown": "BTC-USD:L@81000x0.001; ETH-USD:S@4300x0.05"},
        ]
        (tmp_path / "aria.log").write_text(
            "\n".join(json.dumps(l) for l in lines) + "\n")

    def test_deviation_table(self, tmp_path):
        self._write_log(tmp_path)
        flows = {"symbols": {
            "BTC": {"tide_long": "aligned", "tide_short": "opposed"},
            "ETH": {"tide_long": "opposed", "tide_short": "aligned"},
            "SOL": {"tide_long": "aligned", "tide_short": "opposed"}}}
        out = mp.positioning_section(str(tmp_path), _NOW, flows)
        # Latest pnl_attribution only (today's), yesterday's line ignored
        assert out["open_positions"] == [
            {"symbol": "BTC-USD", "direction": "long"},
            {"symbol": "ETH-USD", "direction": "short"}]
        assert out["entries_today"] == 1          # rejected one not counted
        assert out["tide_vetoes_today"] == 1
        kinds = {(r["symbol"], r["kind"]): r["tide"]
                 for r in out["deviation_table"]}
        assert kinds[("BTC-USD", "open_position")] == "aligned"
        assert kinds[("ETH-USD", "open_position")] == "aligned"
        assert kinds[("SOL-USD", "execution_decision")] == "aligned"
        assert out["opposed_count"] == 0

    def test_opposed_counted(self, tmp_path):
        self._write_log(tmp_path)
        flows = {"symbols": {
            "BTC": {"tide_long": "opposed", "tide_short": "aligned"}}}
        out = mp.positioning_section(str(tmp_path), _NOW, flows)
        assert out["opposed_count"] == 1          # BTC long vs opposed tide
        sol = [r for r in out["deviation_table"] if r["symbol"] == "SOL-USD"]
        assert sol[0]["tide"] == "no_data"

    def test_missing_log_fails_open(self, tmp_path):
        out = mp.positioning_section(str(tmp_path), _NOW, {"symbols": {}})
        assert out["open_positions"] == []
        assert out["entries_today"] == 0
        assert out["deviation_table"] == []


class TestStructureVerdicts:
    def test_divergence_and_cross(self):
        from data.sosovalue_feed import flow_verdict
        syms = {s: flow_verdict(s, r) for s, r in _flows_cache().items()}
        out = mp.structure_verdicts({"symbols": syms},
                                    {"verdict": "expanding"})
        assert out["flow_signs_last_day"] == {"BTC": -1, "ETH": 1, "SOL": 1}
        assert out["flow_divergence"] == "btc_outflow_eth_inflow_alts_too"
        assert out["persistence_max_streak_days"] == 5
        # 3d total = -172M + 452M + 204M = 484M > 150M materiality
        assert out["liquidity_x_flows"] == "supportive_expansion"

    def test_liquidity_flow_divergence(self):
        flows = {"symbols": {"BTC": {"last_inflow_usd": -300e6,
                                     "sum_3d_usd": -900e6, "streak_days": -3}}}
        out = mp.structure_verdicts(flows, {"verdict": "expanding"})
        assert out["liquidity_x_flows"] == "liquidity_vs_flow_divergence"
        out2 = mp.structure_verdicts(flows, {"verdict": "contracting"})
        assert out2["liquidity_x_flows"] == "neutral"

    def test_empty_inputs(self):
        out = mp.structure_verdicts({}, {})
        assert out["flow_signs_last_day"] == {}
        assert out["flow_divergence"] is None
        assert out["persistence_max_streak_days"] == 0


class TestMain:
    def test_main_writes_outputs_and_exits_zero(self, tmp_path, monkeypatch,
                                                capsys):
        monkeypatch.setattr(mp, "_fetch_stablecoins", lambda: {
            "USDT": {"circulating": 183.3e9, "prev_day": 183.1e9,
                     "prev_week": 182.8e9, "prev_month": 180e9},
            "USDC": {"circulating": 73.46e9, "prev_day": 73.4e9,
                     "prev_week": 73.36e9, "prev_month": 72e9}})
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "sosovalue_flows.json").write_text(json.dumps(_flows_cache()))
        monkeypatch.setattr(mp, "_ROOT", str(tmp_path))
        monkeypatch.setattr(mp.time, "time", lambda: _NOW)
        assert mp.main() == 0
        out = json.loads((log_dir / "macro_posture.json").read_text())
        assert out["structure"]["flow_divergence"] == \
            "btc_outflow_eth_inflow_alts_too"
        assert out["stablecoin_liquidity"]["verdict"] == "expanding"
        hist = (log_dir / "macro_posture_history.jsonl").read_text().strip()
        assert json.loads(hist)["stable_verdict"] == "expanding"
        printed = json.loads(capsys.readouterr().out)
        assert printed["etf_flows"]["sol_share_abs3d_now"] is not None
