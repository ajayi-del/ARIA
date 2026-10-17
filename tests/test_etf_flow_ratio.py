"""Behavioral pins for intelligence/etf_flow_ratio.py (2026-10-17,
Governor directive "STOCK EXECUTION ARCHITECTURE" piece 1). SHADOW-FIRST
brain — tests cover normal fire, below-threshold, zero-AUM abstain,
thin-history abstain, percentile boundary, direction sign, and textual
pins for the env kill switch + splice event name."""
import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SRC = (_ROOT / "intelligence" / "etf_flow_ratio.py").read_text()

import sys
sys.path.insert(0, str(_ROOT))
from intelligence.etf_flow_ratio import (  # noqa: E402
    ETF_FLOW_RATIO_EVENT, FLOW_FIRE_PERCENTILE, MIN_HISTORY_POINTS,
    etf_flow_ratio_enabled, flow_percentile, flow_ratio, flow_signal)


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
