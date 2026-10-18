"""Behavioral pins for intelligence/etf_flow_ratio.py (2026-10-17,
Governor directive "STOCK EXECUTION ARCHITECTURE" piece 1). SHADOW-FIRST
brain — tests cover normal fire, below-threshold, zero-AUM abstain,
thin-history abstain, percentile boundary, direction sign, and textual
pins for the env kill switch + splice event name.

2026-10-18 (Governor BUILD ORDER): + Tier-1 flow_direction_signal pins
(fires positive-only, inert negative/zero, splice inert when percentile
live, knob off = abstain, no double-count with Tier-2) and the BitGuard
history bootstrap probe pins (>=20 real points populate, short/absent =
no-op, never fabricate).
"""
import asyncio
import os
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SRC = (_ROOT / "intelligence" / "etf_flow_ratio.py").read_text()

import sys
sys.path.insert(0, str(_ROOT))
from intelligence.etf_flow_ratio import (  # noqa: E402
    ETF_FLOW_DIRECTION_EVENT, ETF_FLOW_RATIO_EVENT, FLOW_FIRE_PERCENTILE,
    MIN_HISTORY_POINTS, etf_flow_ratio_enabled, flow_direction_signal,
    flow_percentile, flow_ratio, flow_signal)


def _hist(n=100, lo=0.0001, step=0.0001):
    """Deterministic ascending magnitude history."""
    return [lo + i * step for i in range(n)]


# ---- flow_ratio ----

def test_ratio_normal():
    r, reason = flow_ratio("BTC", 3.0e8, 6.0e10)
    assert reason is None
    assert abs(r - 0.005) < 1e-12


def test_ratio_zero_aum_abstains():
    r, reason = flow_ratio("BTC", 3.0e8, 0.0)
    assert r is None and reason == "zero_aum"


def test_ratio_negative_aum_abstains():
    r, reason = flow_ratio("BTC", 3.0e8, -1.0e10)
    assert r is None and reason == "zero_aum"


def test_ratio_missing_aum_abstains():
    r, reason = flow_ratio("ETH", 1.0e8, None)
    assert r is None and reason == "no_aum"


def test_ratio_missing_flow_abstains():
    r, reason = flow_ratio("ETH", None, 1.0e10)
    assert r is None and reason == "no_flow"


# ---- flow_percentile ----

def test_percentile_normal_high():
    hist = _hist()
    pct, reason = flow_percentile("BTC", 0.02, hist)   # above all history
    assert reason is None
    assert pct == 100.0


def test_percentile_median():
    hist = _hist()
    pct, _ = flow_percentile("BTC", 0.00505, hist)     # mid-pack
    assert 40.0 <= pct <= 60.0


def test_percentile_thin_history_abstains():
    hist = _hist(n=MIN_HISTORY_POINTS - 1)
    pct, reason = flow_percentile("BTC", 0.02, hist)
    assert pct is None and reason == "thin_history"


def test_percentile_min_history_allowed():
    hist = _hist(n=MIN_HISTORY_POINTS)
    pct, reason = flow_percentile("BTC", 0.02, hist)
    assert reason is None and pct == 100.0


def test_percentile_no_ratio_abstains():
    pct, reason = flow_percentile("BTC", None, _hist())
    assert pct is None and reason == "no_ratio"


# ---- flow_signal: fire / below-threshold / boundary ----

def test_signal_normal_fire_inflow():
    hist = _hist()
    out = flow_signal("BTC", 3.0e9, 6.0e10, hist)      # ratio .05, top of hist
    assert out["fired"] is True
    assert out["direction"] == "inflow"
    assert out["percentile"] >= FLOW_FIRE_PERCENTILE
    assert out["ratio"] is not None


def test_signal_normal_fire_outflow():
    hist = _hist()
    out = flow_signal("ETH", -3.0e9, 6.0e10, hist)     # magnitude ranks, sign directs
    assert out["fired"] is True
    assert out["direction"] == "outflow"


def test_signal_below_threshold_not_fired():
    hist = _hist()
    out = flow_signal("BTC", 3.0e7, 6.0e10, hist)      # ratio 5e-4, low rank
    assert out["fired"] is False
    assert out["percentile"] < FLOW_FIRE_PERCENTILE


def test_signal_boundary_at_threshold():
    # 20 history points: 18 below => pct 90.0 exactly => fires at >= 90.
    hist = [0.001] * 18 + [0.01] * 2
    out = flow_signal("BTC", 0.005 * 1e10, 1e10, hist)  # ratio .005: 18 below
    assert out["percentile"] == 90.0
    assert out["fired"] is True
    # 17 below => pct 85.0 => does not fire.
    hist2 = [0.001] * 17 + [0.01] * 3
    out2 = flow_signal("BTC", 0.005 * 1e10, 1e10, hist2)
    assert out2["percentile"] == 85.0
    assert out2["fired"] is False


def test_signal_zero_aum_abstains():
    out = flow_signal("BTC", 3.0e8, 0.0, _hist())
    assert out["fired"] is False
    assert out["ratio"] is None
    assert out["reason"] == "zero_aum"


def test_signal_thin_history_abstains():
    out = flow_signal("BTC", 3.0e9, 6.0e10, _hist(n=5))
    assert out["fired"] is False
    assert out["percentile"] is None
    assert out["reason"] == "thin_history"


def test_signal_zero_flow_direction_unknown():
    out = flow_signal("BTC", 0.0, 6.0e10, _hist())
    assert out["direction"] == "unknown"


# ---- textual pins: env kill switch + event name ----

def test_kill_switch_env_pinned():
    assert 'ETF_FLOW_RATIO_ENABLED' in _SRC
    assert '"true"' in _SRC                       # default "true"


def test_kill_switch_helper_behavior(monkeypatch):
    monkeypatch.delenv("ETF_FLOW_RATIO_ENABLED", raising=False)
    assert etf_flow_ratio_enabled() is True
    monkeypatch.setenv("ETF_FLOW_RATIO_ENABLED", "false")
    assert etf_flow_ratio_enabled() is False


def test_event_name_pinned():
    assert ETF_FLOW_RATIO_EVENT == "etf_flow_ratio_signal"
    assert '"etf_flow_ratio_signal"' in _SRC


# ════════════════════════════════════════════════════════════════════════════
# Tier-1 flow direction (2026-10-18 Governor BUILD ORDER): sign-only fallback
# while the percentile path abstains on thin history. ADDITIVE-ONLY — fires
# ONLY for positive flow; outflows grant nothing, never a penalty.
# ════════════════════════════════════════════════════════════════════════════

class TestFlowDirectionSignal:
    def test_fires_on_positive_flow(self):
        out = flow_direction_signal("BTC", 2.5e8)
        assert out["fired"] is True
        assert out["direction"] == "inflow"
        assert out["tier"] == 1
        assert out["flow_usd"] == 2.5e8

    def test_inert_on_negative_flow(self):
        out = flow_direction_signal("BTC", -2.5e8)
        assert out["fired"] is False
        assert out["direction"] == "outflow"     # sign recorded, never fires

    def test_inert_on_zero_flow(self):
        out = flow_direction_signal("ETH", 0.0)
        assert out["fired"] is False
        assert out["direction"] == "unknown"

    def test_inert_on_missing_or_garbage(self):
        assert flow_direction_signal("ETH", None)["fired"] is False
        out = flow_direction_signal("ETH", "junk")
        assert out["fired"] is False and out["direction"] == "unknown"

    def test_event_name_pinned(self):
        assert ETF_FLOW_DIRECTION_EVENT == "etf_flow_direction_signal"
        assert '"etf_flow_direction_signal"' in _SRC


# ── Tier-1 splice behavior (main.py _equity_detector_bonus) ──────────────────

import main as _main  # noqa: E402

_TIER1_MAIN_SRC = _SRC  # brain source; splice pins read _main below


def _splice_ctx(rows, today_flow):
    """ETF plane ctx for _equity_detector_bonus: freshest-wins verdict +
    raw rows (newest-first). rows[0] must be today."""
    verdict = {"rows": len(rows),
               "last_inflow_usd": today_flow,
               "net_assets_usd": rows[0]["total_net_assets"]}
    return {"candle_buffers": {}, "funding_rates": {},
            "etf_verdict_fn": lambda _s: (verdict, 5.0),
            "etf_rows_fn": lambda _a: rows,
            "oi_series_fn": None,
            "category_fn": lambda _s: None}


def _thin_rows(today_flow=2.5e8):
    """5 rows only → 4 history points → percentile abstains thin_history."""
    today = {"date": "2026-10-16", "total_net_inflow": today_flow,
             "total_net_assets": 1.0e10}
    hist = [{"date": f"2026-10-{d:02d}", "total_net_inflow": 1.0e4,
             "total_net_assets": 1.0e10} for d in (15, 14, 13, 12)]
    return [today] + hist


def _live_rows_not_fired(today_flow=1.0e4):
    """21 rows; today's ratio equals history dust → percentile live at 0,
    never fired — Tier-1 must stay inert even on positive flow."""
    today = {"date": "2026-10-16", "total_net_inflow": today_flow,
             "total_net_assets": 1.0e10}
    hist = [{"date": f"2026-09-{d:02d}", "total_net_inflow": 1.0e4,
             "total_net_assets": 1.0e10} for d in range(26, 6, -1)]
    return [today] + hist


class TestTier1Splice:
    def test_thin_history_inflow_grants_long_bonus(self, monkeypatch):
        monkeypatch.delenv("ETF_FLOW_TIER1_BONUS", raising=False)
        c = _splice_ctx(_thin_rows(today_flow=2.5e8), 2.5e8)
        assert _main._equity_detector_bonus(
            "BTC-USD", "long", ctx=c, now_ts=1760710000.0) == 0.2

    def test_thin_history_inflow_grants_nothing_to_short(self):
        c = _splice_ctx(_thin_rows(today_flow=2.5e8), 2.5e8)
        assert _main._equity_detector_bonus(
            "BTC-USD", "short", ctx=c, now_ts=1760710000.0) == 0.0

    def test_thin_history_outflow_never_fires(self):
        c = _splice_ctx(_thin_rows(today_flow=-2.5e8), -2.5e8)
        assert _main._equity_detector_bonus(
            "BTC-USD", "long", ctx=c, now_ts=1760710000.0) == 0.0
        c2 = _splice_ctx(_thin_rows(today_flow=-2.5e8), -2.5e8)
        assert _main._equity_detector_bonus(
            "BTC-USD", "short", ctx=c2, now_ts=1760710000.0) == 0.0

    def test_inert_when_percentile_live(self):
        # Percentile computed (not fired) → Tier-1 inert despite inflow.
        c = _splice_ctx(_live_rows_not_fired(today_flow=1.0e4), 1.0e4)
        assert _main._equity_detector_bonus(
            "BTC-USD", "long", ctx=c, now_ts=1760710000.0) == 0.0

    def test_no_double_count_when_tier2_fires(self):
        # 21 rows with a top-decile inflow → Tier-2 0.4 only, never 0.4+0.2.
        today = {"date": "2026-10-16", "total_net_inflow": 1.0e9,
                 "total_net_assets": 1.0e10}
        hist = [{"date": f"2026-09-{d:02d}", "total_net_inflow": 1.0e4,
                 "total_net_assets": 1.0e10} for d in range(26, 6, -1)]
        c = _splice_ctx([today] + hist, 1.0e9)
        assert _main._equity_detector_bonus(
            "BTC-USD", "long", ctx=c, now_ts=1760710000.0) == pytest.approx(0.4)

    def test_knob_off_abstains(self, monkeypatch):
        monkeypatch.setenv("ETF_FLOW_TIER1_BONUS", "0")
        c = _splice_ctx(_thin_rows(today_flow=2.5e8), 2.5e8)
        assert _main._equity_detector_bonus(
            "BTC-USD", "long", ctx=c, now_ts=1760710000.0) == 0.0

    def test_splice_textual_pins(self):
        src = open(_main.__file__).read()
        assert "ETF_FLOW_TIER1_BONUS" in src
        assert '"ETF_FLOW_TIER1_BONUS", 0.2' in src
        assert "_eq_etf_flow_ratio.ETF_FLOW_DIRECTION_EVENT" in src
        assert "flow_direction_signal" in src


# ════════════════════════════════════════════════════════════════════════════
# History bootstrap probe (data/bitguard_etf_feed.py): >=20 real daily points
# pre-populate the percentile history buffer; short/absent = no-op, never
# fabricate.
# ════════════════════════════════════════════════════════════════════════════

from data.bitguard_etf_feed import (  # noqa: E402
    BitguardEtfFeed, _HISTORY_CAP, _parse_history_days)

_BG_SRC = (_ROOT / "data" / "bitguard_etf_feed.py").read_text()


def _history_payload(n_days=30):
    """Nested-leg history shape: [{date, btc: {...}, eth: {...}}, ...]."""
    days = []
    for i in range(n_days):
        d = f"2026-09-{i + 1:02d}"
        days.append({"date": d,
                     "btc": {"netFlowUsd": 1.0e6 * (i + 1),
                             "totalNetAssetsUsd": 5.0e10},
                     "eth": {"netFlowUsd": -5.0e5 * (i + 1),
                             "totalNetAssetsUsd": 2.0e10}})
    return {"data": {"history": days}}


class _StubFetch:
    def __init__(self, payload=None, exc=None):
        self._payload, self._exc = payload, exc
        self.calls = []

    async def __call__(self, url, params):
        self.calls.append((url, params))
        if self._exc:
            raise self._exc
        return self._payload


class TestBootstrapProbe:
    def test_populates_on_20_plus_points(self, tmp_path):
        feed = BitguardEtfFeed(log_dir=str(tmp_path))
        stub = _StubFetch(payload=_history_payload(30))
        n = asyncio.run(feed.bootstrap_history(fetch_fn=stub))
        assert n == 60                                  # 30 BTC + 30 ETH
        assert len(feed._rows["BTC"]) == 30
        assert len(feed._rows["ETH"]) == 30
        # The percentile history buffer the splice reads is now live:
        rows = feed._rows["BTC"]
        assert rows[0]["date"] >= rows[-1]["date"]      # newest-first
        assert all(r["total_net_assets"] > 0 for r in rows)
        # Persisted — a restart keeps the baseline.
        assert (tmp_path / "bitguard_etf_flows.json").exists()
        # Probe hit the derived history URL with days=30.
        assert stub.calls[0][0].endswith("/history")
        assert stub.calls[0][1] == {"days": 30}

    def test_short_history_no_op_no_fabrication(self, tmp_path):
        feed = BitguardEtfFeed(log_dir=str(tmp_path))
        stub = _StubFetch(payload=_history_payload(10))   # < 20 points
        n = asyncio.run(feed.bootstrap_history(fetch_fn=stub))
        assert n == 0
        assert feed._rows.get("BTC") is None              # nothing invented
        assert not (tmp_path / "bitguard_etf_flows.json").exists()

    def test_fetch_failure_no_op(self, tmp_path):
        feed = BitguardEtfFeed(log_dir=str(tmp_path))
        stub = _StubFetch(exc=RuntimeError("404"))
        n = asyncio.run(feed.bootstrap_history(fetch_fn=stub))
        assert n == 0 and feed._rows.get("BTC") is None

    def test_unparseable_payload_no_op(self, tmp_path):
        feed = BitguardEtfFeed(log_dir=str(tmp_path))
        stub = _StubFetch(payload={"unexpected": "shape"})
        n = asyncio.run(feed.bootstrap_history(fetch_fn=stub))
        assert n == 0

    def test_live_rows_win_date_ties(self, tmp_path):
        feed = BitguardEtfFeed(log_dir=str(tmp_path))
        feed._rows["BTC"] = [{"date": "2026-09-01",
                              "total_net_inflow": 9.9e9,
                              "total_net_assets": 5.5e10}]
        stub = _StubFetch(payload=_history_payload(30))
        asyncio.run(feed.bootstrap_history(fetch_fn=stub))
        by_date = {r["date"]: r for r in feed._rows["BTC"]}
        assert by_date["2026-09-01"]["total_net_inflow"] == 9.9e9

    def test_kill_switch_off(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BITGUARD_ETF_BOOTSTRAP_ENABLED", "false")
        feed = BitguardEtfFeed(log_dir=str(tmp_path))
        stub = _StubFetch(payload=_history_payload(30))
        n = asyncio.run(feed.bootstrap_history(fetch_fn=stub))
        assert n == 0 and stub.calls == []

    def test_parser_flat_asset_shape(self):
        payload = [{"date": "2026-09-01", "asset": "BTC",
                    "netFlowUsd": 1.0e6, "totalNetAssetsUsd": 5.0e10}]
        out = _parse_history_days(payload, ("BTC", "ETH"))
        assert out["BTC"][0]["total_net_inflow"] == 1.0e6
        assert "ETH" not in out

    def test_parser_requires_positive_aum(self):
        payload = [{"date": "2026-09-01",
                    "btc": {"netFlowUsd": 1.0e6, "totalNetAssetsUsd": 0}}]
        assert _parse_history_days(payload, ("BTC",)) == {}

    def test_history_cap_holds_bootstrap(self):
        # The buffer must retain a 30-day backfill (was 10 — the percentile
        # could never reach 20 points).
        assert _HISTORY_CAP >= 35
        assert '"etf_percentile_bootstrapped"' in _BG_SRC
        assert "BITGUARD_ETF_BOOTSTRAP_ENABLED" in _BG_SRC

