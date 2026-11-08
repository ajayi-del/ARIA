"""Pure-brain pins for tools/xyz_posture.py — no network. Fixtures only:
verdict math (crowding / dollar_tide / breadth), ratio computation from an
HL metaAndAssetCtxs fixture, atomic write, one-bad-line history read, and
429/HTTP-error fail-soft via fake transport objects."""
import json
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from tools import xyz_posture as xp


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _hl_payload():
    """metaAndAssetCtxs(dex=xyz) shape: [ {universe:[...]}, [ctx, ...] ]."""
    names = ["xyz:GOLD", "xyz:COPPER", "xyz:DXY", "xyz:SILVER",
             "xyz:SP500", "xyz:NVDA"]
    ctxs = [
        {"markPx": "4000.0", "prevDayPx": "3980.0", "funding": "0.00001",
         "openInterest": "1000", "dayNtlVlm": "5000000"},
        {"markPx": "5.0", "prevDayPx": "4.9", "funding": "-0.0002",
         "openInterest": "2000", "dayNtlVlm": "3000000"},
        {"markPx": "100.5", "prevDayPx": "100.0", "funding": "0.0",
         "openInterest": "500", "dayNtlVlm": "9000000"},
        {"markPx": "48.0", "prevDayPx": "49.0", "funding": "0.00005",
         "openInterest": "800", "dayNtlVlm": "1000000"},
        {"markPx": "7300.0", "prevDayPx": "7250.0", "funding": "-0.00001",
         "openInterest": "3000", "dayNtlVlm": "12000000"},
        {"markPx": "180.0", "prevDayPx": "185.0", "funding": "0.0004",
         "openInterest": "4000", "dayNtlVlm": "2000000"},
    ]
    return [{"universe": [{"name": n} for n in names]}, ctxs]


class _FakeResp:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeClient:
    """Programmable transport: post_queue for HL, get_queue for Variational."""
    def __init__(self, post_queue=(), get_queue=()):
        self._post = list(post_queue)
        self._get = list(get_queue)
        self.posts = 0
        self.gets = 0

    def post(self, url, json=None):
        self.posts += 1
        return self._post.pop(0)

    def get(self, url):
        self.gets += 1
        return self._get.pop(0)


# ── Crowding verdicts ────────────────────────────────────────────────────────

class TestCrowdingVerdict:
    def test_crowded_long_above_threshold(self):
        assert xp.crowding_verdict(0.66) == "crowded_long"
        assert xp.crowding_verdict(0.99) == "crowded_long"

    def test_crowded_short_below_threshold(self):
        assert xp.crowding_verdict(0.34) == "crowded_short"
        assert xp.crowding_verdict(0.01) == "crowded_short"

    def test_boundaries_are_neutral(self):
        assert xp.crowding_verdict(0.65) == "neutral"
        assert xp.crowding_verdict(0.35) == "neutral"
        assert xp.crowding_verdict(0.5) == "neutral"

    def test_no_data_abstains(self):
        assert xp.crowding_verdict(None) is None


# ── Dollar tide ──────────────────────────────────────────────────────────────

class TestDollarTide:
    def test_strengthening(self):
        assert xp.dollar_tide(0.31) == "strengthening"
        assert xp.dollar_tide(1.2) == "strengthening"

    def test_weakening(self):
        assert xp.dollar_tide(-0.31) == "weakening"
        assert xp.dollar_tide(-2.0) == "weakening"

    def test_flat_and_boundaries(self):
        assert xp.dollar_tide(0.3) == "flat"
        assert xp.dollar_tide(-0.3) == "flat"
        assert xp.dollar_tide(0.0) == "flat"

    def test_no_data(self):
        assert xp.dollar_tide(None) == "no_data"


# ── Breadth regime ───────────────────────────────────────────────────────────

class TestBreadthRegime:
    def test_broad_risk_on(self):
        assert xp.breadth_regime(0.61) == "broad_risk_on"
        assert xp.breadth_regime(0.95) == "broad_risk_on"

    def test_broad_risk_off(self):
        assert xp.breadth_regime(0.39) == "broad_risk_off"
        assert xp.breadth_regime(0.05) == "broad_risk_off"

    def test_mixed_and_boundaries(self):
        assert xp.breadth_regime(0.6) == "mixed"
        assert xp.breadth_regime(0.4) == "mixed"
        assert xp.breadth_regime(0.5) == "mixed"

    def test_no_data(self):
        assert xp.breadth_regime(None) == "no_data"


# ── Metals risk tone (rolling history) ───────────────────────────────────────

class TestMetalsRiskTone:
    def test_insufficient_history(self):
        assert xp.metals_risk_tone(1.25, []) == "insufficient_history"
        assert xp.metals_risk_tone(1.25, [1.2, 1.3]) == "insufficient_history"

    def test_copper_leading(self):
        assert xp.metals_risk_tone(1.01, [1.0, 1.0, 1.0]) == "copper_leading"

    def test_gold_leading(self):
        assert xp.metals_risk_tone(0.99, [1.0, 1.0, 1.0]) == "gold_leading"

    def test_balanced(self):
        assert xp.metals_risk_tone(1.002, [1.0, 1.0, 1.0]) == "balanced"

    def test_no_data(self):
        assert xp.metals_risk_tone(None, [1.0, 1.0, 1.0]) == "no_data"

    def test_junk_history_entries_ignored(self):
        assert xp.metals_risk_tone(1.01, [1.0, "x", None, 1.0, 1.0]) == \
            "copper_leading"


# ── HL snapshot parse + derived complexes ────────────────────────────────────

class TestHLPlane:
    def test_parse_fixture(self):
        snap = xp.parse_xyz_snapshot(_hl_payload())
        assert len(snap) == 6
        assert snap["xyz:GOLD"]["mark"] == 4000.0
        assert snap["xyz:COPPER"]["prev_day_px"] == 4.9
        assert snap["xyz:NVDA"]["funding"] == 0.0004

    def test_parse_bad_shape_fail_closed(self):
        assert xp.parse_xyz_snapshot(None) == {}
        assert xp.parse_xyz_snapshot({"not": "a list"}) == {}
        assert xp.parse_xyz_snapshot([{"universe": []}]) == {}
        assert xp.parse_xyz_snapshot([{"universe": []}, "junk"]) == {}

    def test_copper_gold_ratio(self):
        d = xp.derived_complexes(xp.parse_xyz_snapshot(_hl_payload()))
        assert d["copper_gold_ratio"] == pytest.approx(5.0 / 4000.0, abs=1e-6)

    def test_dxy_anchor(self):
        d = xp.derived_complexes(xp.parse_xyz_snapshot(_hl_payload()))
        assert d["dxy"]["mark"] == 100.5
        assert d["dxy"]["chg_24h_pct"] == pytest.approx(0.5, abs=1e-4)

    def test_breadth(self):
        d = xp.derived_complexes(xp.parse_xyz_snapshot(_hl_payload()))
        # GOLD, COPPER, DXY, SP500 advance; SILVER, NVDA decline -> 4/6.
        assert d["breadth"]["advancing"] == 4
        assert d["breadth"]["total"] == 6
        assert d["breadth"]["advancing_share"] == pytest.approx(4 / 6, abs=1e-4)

    def test_funding_extremes_top5(self):
        d = xp.derived_complexes(xp.parse_xyz_snapshot(_hl_payload()))
        ex = d["funding_extremes"]
        assert len(ex) == 5
        assert ex[0]["asset"] == "xyz:NVDA"          # |0.0004| largest
        assert ex[1]["asset"] == "xyz:COPPER"        # |-0.0002| second

    def test_metals_table(self):
        d = xp.derived_complexes(xp.parse_xyz_snapshot(_hl_payload()))
        assert set(d["metals"]) == {"GOLD", "SILVER", "COPPER"}
        assert d["metals"]["SILVER"]["chg_24h_pct"] == pytest.approx(
            (48.0 - 49.0) / 49.0 * 100, abs=1e-2)

    def test_missing_legs_abstain(self):
        snap = {"xyz:GOLD": {"mark": 4000.0, "funding": None, "oi": None,
                             "day_ntl_vlm": None, "prev_day_px": None}}
        d = xp.derived_complexes(snap)
        assert d["copper_gold_ratio"] is None
        assert d["dxy"]["mark"] is None
        assert d["breadth"]["advancing_share"] is None
        assert d["funding_extremes"] == []

    def test_fetch_fail_soft_http_error(self):
        client = _FakeClient(post_queue=[_FakeResp(500, {})])
        with pytest.raises(RuntimeError):
            xp.fetch_xyz_snapshot(client=client)


# ── Variational plane ────────────────────────────────────────────────────────

class TestVariationalPlane:
    def test_parse_list_shape(self):
        payload = [
            {"symbol": "GOLD", "open_interest": {"long": "700", "short": "300"}},
            {"symbol": "silver-usd", "open_interest_long": 200,
             "open_interest_short": 800},
        ]
        stats = xp.parse_variational_stats(payload)
        assert stats["GOLD"]["long_share"] == pytest.approx(0.7)
        assert stats["SILVER"]["long_share"] == pytest.approx(0.2)
        assert stats["SILVER"]["oi_short"] == 800.0

    def test_parse_wrapped_and_keyed_shapes(self):
        wrapped = {"data": [{"ticker": "DXY", "long_share": 55}]}
        stats = xp.parse_variational_stats(wrapped)
        assert stats["DXY"]["long_share"] == pytest.approx(0.55)
        keyed = {"GOLD": {"open_interest_long": 60, "open_interest_short": 40}}
        stats = xp.parse_variational_stats(keyed)
        assert stats["GOLD"]["long_share"] == pytest.approx(0.6)

    def test_parse_junk_fail_closed(self):
        assert xp.parse_variational_stats(None) == {}
        assert xp.parse_variational_stats({"unexpected": 1}) == {}
        assert xp.parse_variational_stats([["not", "a", "dict"], 42]) == {}

    def test_429_retry_then_success(self):
        client = _FakeClient(get_queue=[
            _FakeResp(429, {"retry_after_seconds": 0}),
            _FakeResp(200, [{"symbol": "GOLD",
                             "open_interest": {"long": 70, "short": 30}}]),
        ])
        stats = xp.fetch_variational_stats(client=client, sleep_fn=lambda s: None)
        assert stats["GOLD"]["long_share"] == pytest.approx(0.7)
        assert client.gets == 2

    def test_429_retry_after_header_fallback(self):
        client = _FakeClient(get_queue=[
            _FakeResp(429, ValueError("not json"), headers={"Retry-After": "0"}),
            _FakeResp(200, [{"symbol": "DXY",
                             "open_interest": {"long": 50, "short": 50}}]),
        ])
        stats = xp.fetch_variational_stats(client=client, sleep_fn=lambda s: None)
        assert "DXY" in stats

    def test_429_persistent_skips_plane(self):
        client = _FakeClient(get_queue=[
            _FakeResp(429, {"retry_after_seconds": 0}),
            _FakeResp(429, {"retry_after_seconds": 0}),
        ])
        with pytest.raises(RuntimeError, match="429"):
            xp.fetch_variational_stats(client=client, sleep_fn=lambda s: None)
        assert client.gets == 2   # exactly one retry, then skip

    def test_http_error_skips_plane(self):
        client = _FakeClient(get_queue=[_FakeResp(503, {})])
        with pytest.raises(RuntimeError, match="503"):
            xp.fetch_variational_stats(client=client, sleep_fn=lambda s: None)
        assert client.gets == 1   # no retry on non-429

    def test_crowding_section_join(self):
        snap = xp.parse_xyz_snapshot(_hl_payload())
        stats = {"GOLD": {"oi_long": 700.0, "oi_short": 300.0, "long_share": 0.7},
                 "SILVER": {"oi_long": 200.0, "oi_short": 800.0,
                            "long_share": 0.2},
                 "NVDA": {"oi_long": 1.0, "oi_short": 1.0, "long_share": 0.5}}
        sec = xp.crowding_section(stats, snap)
        assert sec["coverage"] == 3
        assert [r["symbol"] for r in sec["crowded_long"]] == ["GOLD"]
        assert [r["symbol"] for r in sec["crowded_short"]] == ["SILVER"]
        assert sec["neutral"] == 1


# ── IO doctrine ──────────────────────────────────────────────────────────────

class TestIODoctrine:
    def test_atomic_write(self, tmp_path):
        p = str(tmp_path / "out.json")
        xp._atomic_write(p, {"a": 1})
        assert json.load(open(p)) == {"a": 1}
        assert not os.path.exists(p + ".tmp")
        xp._atomic_write(p, {"a": 2})
        assert json.load(open(p)) == {"a": 2}

    def test_one_bad_line_history_read(self, tmp_path):
        p = tmp_path / "hist.jsonl"
        p.write_text('{"ok": 1}\nNOT_JSON\n{"ok": 2}\n\n{"ok": 3}\n')
        rows = xp._load_jsonl(str(p))
        assert [r["ok"] for r in rows] == [1, 2, 3]

    def test_load_missing_and_corrupt(self, tmp_path):
        assert xp._load(str(tmp_path / "nope.json"), {}) == {}
        bad = tmp_path / "bad.json"
        bad.write_text("{corrupt")
        assert xp._load(str(bad), {"d": 1}) == {"d": 1}


# ── Degradation: Variational absent, HL-only run still produces output ──────

class TestDegradedRun:
    def test_hl_only_run_writes_output(self, tmp_path, monkeypatch):
        monkeypatch.setattr(xp, "fetch_xyz_snapshot",
                            lambda: xp.parse_xyz_snapshot(_hl_payload()))
        def _boom(*a, **k):
            raise RuntimeError("http_429_after_retry")
        monkeypatch.setattr(xp, "fetch_variational_stats", _boom)
        monkeypatch.setattr(xp, "_ROOT", str(tmp_path))
        (tmp_path / "logs").mkdir()
        rc = xp.main()
        assert rc == 0
        out = json.load(open(tmp_path / "logs" / "xyz_posture.json"))
        assert out["hl_xyz"]["asset_count"] == 6
        assert out["hl_xyz"]["copper_gold_ratio"] is not None
        assert out["positioning_crowding"]["degraded"] == "hl_only"
        assert out["verdicts"]["dollar_tide"] == "strengthening"  # +0.5%
        assert out["verdicts"]["breadth_regime"] == "broad_risk_on"  # 4/6
        assert out["verdicts"]["metals_risk_tone"]["verdict"] == \
            "insufficient_history"   # first run
        # history line appended
        lines = (tmp_path / "logs" / "xyz_posture_history.jsonl") \
            .read_text().strip().splitlines()
        assert len(lines) == 1
        hist = json.loads(lines[0])
        assert hist["dollar_tide"] == "strengthening"

    def test_second_run_ratio_history_tone(self, tmp_path, monkeypatch):
        # Seed a prior state with a ratio history below today's ratio.
        (tmp_path / "logs").mkdir()
        prior = {"verdicts": {"metals_risk_tone": {
            "ratio_history": [0.001, 0.001, 0.001]}}}
        (tmp_path / "logs" / "xyz_posture.json").write_text(json.dumps(prior))
        monkeypatch.setattr(xp, "fetch_xyz_snapshot",
                            lambda: xp.parse_xyz_snapshot(_hl_payload()))
        monkeypatch.setattr(xp, "fetch_variational_stats",
                            lambda: {"GOLD": {"oi_long": 1.0, "oi_short": 1.0,
                                              "long_share": 0.5}})
        monkeypatch.setattr(xp, "_ROOT", str(tmp_path))
        assert xp.main() == 0
        out = json.load(open(tmp_path / "logs" / "xyz_posture.json"))
        tone = out["verdicts"]["metals_risk_tone"]
        assert tone["verdict"] == "copper_leading"  # 0.00125 > 0.001 x 1.005
        assert tone["ratio_history"][-1] == pytest.approx(5.0 / 4000.0)
        assert out["positioning_crowding"]["coverage"] == 1

    def test_total_failure_still_exit_zero(self, tmp_path, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("plane_down")
        monkeypatch.setattr(xp, "fetch_xyz_snapshot", _boom)
        monkeypatch.setattr(xp, "fetch_variational_stats", _boom)
        monkeypatch.setattr(xp, "_ROOT", str(tmp_path))
        (tmp_path / "logs").mkdir()
        assert xp.main() == 0
        out = json.load(open(tmp_path / "logs" / "xyz_posture.json"))
        assert "error" in out["hl_xyz"]
        assert "error" in out["verdicts"]["positioning_crowding"]
        assert "dollar_tide" not in out["verdicts"]
        assert "metals_risk_tone" not in out["verdicts"]
