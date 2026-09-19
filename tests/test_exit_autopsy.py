"""Tests for tools/exit_autopsy.py — the EXIT_AUTOPSY_MAX_FETCH knob and the
window_bounds/scored-population contract (CEO 'disclosure not filter' repair,
2026-09-19). Pure functions only; network sections untested by design."""
import importlib.util
import os

import pytest

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "tools", "exit_autopsy.py")


def _load():
    spec = importlib.util.spec_from_file_location("exit_autopsy", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ea = _load()


def _close(entry_id, closed_ms, symbol="ETH-USD", direction="long"):
    return {"symbol": symbol, "direction": direction, "outcome": "win",
            "entry_id": entry_id, "closed_at_ms": closed_ms,
            "entry_price": 100.0, "position_size": 1.0,
            "exit_reason": "software_stop", "pnl_usd": 1.0,
            "hold_time_ms": 600000}


# ── MAX_FETCH knob ───────────────────────────────────────────────────────────

def test_knob_default_500():
    os.environ.pop("EXIT_AUTOPSY_MAX_FETCH", None)
    assert _load().MAX_FETCH == 500


def test_knob_env_override():
    os.environ["EXIT_AUTOPSY_MAX_FETCH"] = "7"
    try:
        assert _load().MAX_FETCH == 7
    finally:
        os.environ.pop("EXIT_AUTOPSY_MAX_FETCH", None)


def test_knob_garbage_env_falls_back():
    os.environ["EXIT_AUTOPSY_MAX_FETCH"] = "not-a-number"
    try:
        assert _load().MAX_FETCH == 500
    finally:
        os.environ.pop("EXIT_AUTOPSY_MAX_FETCH", None)


# ── select_population: truncation + truthful bounds ──────────────────────────

def test_population_truncates_newest_first():
    closes = [_close("a", 3000), _close("b", 2000), _close("c", 1000)]
    pop, bounds = ea.select_population(closes, 2)
    assert [r["entry_id"] for r in pop] == ["a", "b"]


def test_population_bounds_describe_scored_set_not_superset():
    # Pre-fix defect: bounds spanned the whole day file while only the newest
    # MAX_FETCH were scored. Bounds must cover ONLY the scored population.
    closes = [_close("a", 86_400_000), _close("b", 43_200_000),
              _close("c", 1_000)]
    pop, bounds = ea.select_population(closes, 2)
    from datetime import datetime, timezone
    lo = int(datetime.fromisoformat(bounds[0]).timestamp() * 1000)
    hi = int(datetime.fromisoformat(bounds[1]).timestamp() * 1000)
    assert lo == 43_200_000 and hi == 86_400_000  # row c (1000) NOT in bounds


def test_population_under_cap_bounds_span_all():
    closes = [_close("a", 3000), _close("b", 1000)]
    pop, bounds = ea.select_population(closes, 500)
    assert len(pop) == 2
    from datetime import datetime, timezone
    lo = int(datetime.fromisoformat(bounds[0]).timestamp() * 1000)
    hi = int(datetime.fromisoformat(bounds[1]).timestamp() * 1000)
    assert lo == 1000 and hi == 3000


def test_population_empty_bounds_none():
    pop, bounds = ea.select_population([], 500)
    assert pop == [] and bounds is None


# ── schema pins: existing keys unchanged (downstream watchdog parses) ────────

def test_score_close_row_keys_unchanged():
    t0 = 1_700_000_000_000
    bars = [(t0 - 60_000, 101.0, 99.0, 100.0)] + [
        (t0 + i * 60_000, 102.0, 100.5, 101.0) for i in range(1, 1500)]
    now_ms = t0 + 25 * 3600 * 1000
    row = ea.score_close(_close("a", t0), bars, now_ms)
    assert set(row) == {"symbol", "direction", "exit_reason", "exit_class",
                        "outcome", "pnl_net_usd", "hold_time_min", "exit_ref",
                        "1h", "4h", "24h"}
    assert set(row["1h"]) == {"hold_usd", "delta_usd", "stop_would_hit",
                              "mfe_pct_after_exit"}


def test_aggregate_cell_keys_unchanged():
    t0 = 1_700_000_000_000
    bars = [(t0 - 60_000, 101.0, 99.0, 100.0)] + [
        (t0 + i * 60_000, 102.0, 100.5, 101.0) for i in range(1, 1500)]
    now_ms = t0 + 25 * 3600 * 1000
    rows = [ea.score_close(_close(str(i), t0), bars, now_ms)
            for i in range(10)]
    out = ea.aggregate(rows)
    cell = out["software_stop"]
    assert set(cell) == {"n", "realized_usd", "1h", "4h", "24h"}
    assert set(cell["1h"]) == {"scored", "sum_delta_usd", "mean_delta_usd",
                               "regret_rate", "saved_rate",
                               "stops_would_hit", "verdict"}
    assert cell["n"] == 10
