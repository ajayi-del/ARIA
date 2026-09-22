"""Pins for data/fear_greed_feed.py — the Fear & Greed observer plane
(Strategy 9 F&G leg, 2026-09-22). Verdict edges, hysteresis state machine,
date-disciplined cache, one-bad-line history, fetch fail-open."""
import json
import time

import pytest

from data.fear_greed_feed import (FearGreedState, daily_update,
                                  fetch_fear_greed, load_history, verdict)


# ── verdict edges ────────────────────────────────────────────────────────────

def test_verdict_edges():
    assert verdict(100) == "extreme_greed"
    assert verdict(80) == "extreme_greed"     # >= 80
    assert verdict(79) == "greed"
    assert verdict(60) == "greed"             # 60-79
    assert verdict(59) == "neutral"
    assert verdict(40) == "neutral"           # 40-59
    assert verdict(39) == "fear"
    assert verdict(20) == "fear"              # 20-39
    assert verdict(19) == "extreme_fear"
    assert verdict(0) == "extreme_fear"


def test_verdict_degenerate_inputs():
    assert verdict(None) == "unknown"
    assert verdict("x") == "unknown"


# ── hysteresis state machine ─────────────────────────────────────────────────

def test_yellow_cross_and_sticky_dearm():
    st = FearGreedState()                     # yellow 80, hyst 2.0
    r = st.update(79, 1000.0)
    assert r["zone"] == 0 and not r["crossed_above_yellow"]
    r = st.update(80, 1001.0)
    assert r["zone"] == 1 and r["crossed_above_yellow"] and r["changed"]
    # 79 >= 80 - 2.0 -> sticky yellow, no re-cross, no change
    r = st.update(79, 1002.0)
    assert r["zone"] == 1 and not r["changed"]
    # 77 < 78 -> de-arm
    r = st.update(77, 1003.0)
    assert r["zone"] == 0 and r["crossed_below_yellow"] and r["changed"]


def test_red_cross_and_sticky_dearm():
    st = FearGreedState()                     # red 85, hyst 2.0
    st.update(80, 1000.0)
    r = st.update(85, 1001.0)
    assert r["zone"] == 2 and r["crossed_above_red"]
    # 84 >= 83 -> sticky red
    r = st.update(84, 1002.0)
    assert r["zone"] == 2 and not r["changed"]
    # 82 < 83 -> drop to yellow (82 >= 80), crossed_below_red only
    r = st.update(82, 1003.0)
    assert r["zone"] == 1 and r["crossed_below_red"]
    assert not r["crossed_below_yellow"]


def test_fng_leg_armed_only_in_red():
    st = FearGreedState()
    assert st.regime_flip_fng_leg()["armed"] is False
    st.update(84, 1000.0)
    assert st.regime_flip_fng_leg()["armed"] is False   # yellow, not red
    st.update(85, 1001.0)
    leg = st.regime_flip_fng_leg()
    assert leg["armed"] is True and leg["since_ts"] == 1001.0
    st.update(70, 1002.0)                              # full de-arm
    assert st.regime_flip_fng_leg()["armed"] is False
    assert st.regime_flip_fng_leg()["since_ts"] is None


def test_current_safe_on_fresh_brain():
    st = FearGreedState()
    cur = st.current()
    assert cur["value"] is None and cur["zone"] == 0
    assert cur["verdict"] == "unknown"


def test_injectable_thresholds():
    st = FearGreedState(yellow=70, red=75, hysteresis=1.0)
    r = st.update(70, 1.0)
    assert r["zone"] == 1
    r = st.update(75, 2.0)
    assert r["zone"] == 2
    r = st.update(74.5, 3.0)                   # >= 75 - 1.0 -> sticky red
    assert r["zone"] == 2
    r = st.update(73.9, 4.0)
    assert r["zone"] == 1


def test_red_below_yellow_rejected():
    with pytest.raises(ValueError):
        FearGreedState(yellow=90, red=80)


# ── fetch fail-open (mock httpx) ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_fail_open_on_network_error(monkeypatch):
    import httpx

    class _Boom:
        def __init__(self, *a, **k):
            raise ConnectionError("dark plane")

    monkeypatch.setattr(httpx, "AsyncClient", _Boom)
    assert await fetch_fear_greed() is None


@pytest.mark.asyncio
async def test_fetch_fail_open_on_bad_payload(monkeypatch):
    import httpx

    class _Resp:
        def json(self):
            return {"unexpected": True}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    assert await fetch_fear_greed() is None


@pytest.mark.asyncio
async def test_fetch_parses_real_shape(monkeypatch):
    import httpx

    class _Resp:
        def json(self):
            return {"data": [{"value": "87",
                              "value_classification": "Extreme Greed",
                              "timestamp": "1758000000"},
                             {"value": "79"}]}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    d = await fetch_fear_greed()
    assert d["value"] == 87 and d["verdict"] == "extreme_greed"
    assert d["prev_value"] == 79


# ── date-disciplined cache ───────────────────────────────────────────────────

def _utc(y, m, d):
    import calendar
    return float(calendar.timegm((y, m, d, 12, 0, 0, 0, 0, 0)))


@pytest.mark.asyncio
async def test_cache_hit_serves_without_fetch(tmp_path):
    today = _utc(2026, 9, 22)
    with open(tmp_path / "fear_greed.json", "w") as f:
        json.dump({"date": "2026-09-22", "fetched_ts": today,
                   "data": {"value": 50, "verdict": "neutral"}}, f)

    async def _boom():
        raise AssertionError("must not fetch on a cache hit")

    out = await daily_update(str(tmp_path), now=today, fetch_fn=_boom)
    assert out["origin"] == "cache_hit" and out["data"]["value"] == 50


@pytest.mark.asyncio
async def test_stale_date_triggers_fetch_and_rewrites(tmp_path):
    yesterday, today = _utc(2026, 9, 21), _utc(2026, 9, 22)
    with open(tmp_path / "fear_greed.json", "w") as f:
        json.dump({"date": "2026-09-21", "fetched_ts": yesterday,
                   "data": {"value": 30}}, f)

    async def _fetch():
        return {"value": 88, "classification": "Extreme Greed",
                "verdict": "extreme_greed"}

    out = await daily_update(str(tmp_path), now=today, fetch_fn=_fetch)
    assert out["origin"] == "fetched" and out["data"]["value"] == 88
    cache = json.load(open(tmp_path / "fear_greed.json"))
    assert cache["date"] == "2026-09-22" and cache["data"]["value"] == 88
    hist = load_history(str(tmp_path / "fear_greed_history.jsonl"))
    assert len(hist) == 1 and hist[0]["value"] == 88


@pytest.mark.asyncio
async def test_failed_fetch_falls_back_to_stale_cache(tmp_path):
    yesterday, today = _utc(2026, 9, 21), _utc(2026, 9, 22)
    with open(tmp_path / "fear_greed.json", "w") as f:
        json.dump({"date": "2026-09-21", "fetched_ts": yesterday,
                   "data": {"value": 30}}, f)

    async def _none():
        return None

    out = await daily_update(str(tmp_path), now=today, fetch_fn=_none)
    assert out["origin"] == "stale_cache" and out["stale"] is True
    assert out["data"]["value"] == 30


@pytest.mark.asyncio
async def test_kill_switch_cache_only(tmp_path):
    today = _utc(2026, 9, 22)

    async def _boom():
        raise AssertionError("kill switch must suppress the fetch")

    out = await daily_update(str(tmp_path), now=today, fetch_fn=_boom,
                             enabled=False)
    assert out["origin"] == "none" and out["data"] is None


# ── one-bad-line history ─────────────────────────────────────────────────────

def test_one_bad_line_doctrine(tmp_path):
    p = tmp_path / "fear_greed_history.jsonl"
    p.write_text('{"value": 50}\nNOT JSON\n{"value": 88}\n')
    rows = load_history(str(p))
    assert rows == [{"value": 50}, {"value": 88}]


def test_missing_history_is_empty(tmp_path):
    assert load_history(str(tmp_path / "nope.jsonl")) == []
