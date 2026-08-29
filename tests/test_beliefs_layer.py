"""Beliefs-layer repair pins (2026-08-29 journal-corruption audit).

Three corruption layers were proven from production data:
  1. Phantom SPCX closes — bimodal census: 561 real closes ALL <$5 pnl vs
     64 ghosts ALL >$100 (+$45,718.8 fake). Zero records between, so the
     $100 threshold separates the clusters exactly on ANY date.
  2. Regime-window contamination — rally-week records voting at full
     weight weeks later (skeptic 14d half-life decay).
  3. Direction blindness — ETH longs 19 @ 100% WR (+$3.21) pooled with
     ETH shorts 86 @ 17% WR (-$27.42) into ONE belief that throttled both.

Kill switches (all default true; False = legacy bit-for-bit):
  JOURNAL_PHANTOM_FILTER_ENABLED, SYMBOL_EDGE_DIRECTION_ENABLED,
  SKEPTIC_DIRECTION_ENABLED, SKEPTIC_DECAY_HALFLIFE_DAYS (0 = decay off).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelligence import skeptic as skeptic_mod  # noqa: E402
from intelligence.skeptic import Skeptic  # noqa: E402
from intelligence.symbol_edge import SymbolEdgeThrottler  # noqa: E402
from memory.trade_journal import TradeJournal, is_phantom_record  # noqa: E402


# ── Phantom predicate (generalized, any-date) ────────────────────────────────

def _spcx(pnl, day_ms=1787904000000):
    return {"symbol": "SPCX-USD", "pnl_usd": pnl, "outcome": "win",
            "closed_at_ms": day_ms}


def test_phantom_threshold_separates_bimodal_clusters():
    assert is_phantom_record(_spcx(635.0))
    assert is_phantom_record(_spcx(-792.4))
    assert is_phantom_record(_spcx(100.01))
    assert not is_phantom_record(_spcx(100.0))
    assert not is_phantom_record(_spcx(4.99))
    assert not is_phantom_record(_spcx(-0.07))


def test_phantom_is_any_date():
    # The 08-24 purge was date-bound to 08-21/22 and caught only 4; the
    # generalized predicate fires regardless of closed_at day.
    assert is_phantom_record(_spcx(500.0, day_ms=1756000000000))   # ~2025-08-24
    assert is_phantom_record(_spcx(500.0, day_ms=1798000000000))   # future


def test_phantom_only_spcx():
    assert not is_phantom_record({"symbol": "BTC-USD", "pnl_usd": 5000.0})
    assert not is_phantom_record({"symbol": "TSLA-USD", "pnl_usd": 500.0})


def test_phantom_pnl_net_fallback():
    assert is_phantom_record({"symbol": "SPCX-USD", "pnl_net_usd": 300.0})


def test_phantom_filter_kill_switch(monkeypatch, tmp_path):
    monkeypatch.setenv("JOURNAL_PHANTOM_FILTER_ENABLED", "false")
    j = TradeJournal(str(tmp_path))
    j.entries = [_spcx(635.0), _spcx(1.0)]
    assert len(j.get_closed()) == 2  # legacy raw read
    monkeypatch.setenv("JOURNAL_PHANTOM_FILTER_ENABLED", "true")
    assert len(j.get_closed()) == 1  # phantom filtered by default
    assert len(j.get_closed(filter_phantoms=False)) == 2  # explicit opt-out


# ── Symbol edge: direction conditioning ──────────────────────────────────────

def _eth_journal():
    # The audit case: 19 longs @ 100% (+$3.21 avg small) vs 86 shorts @ 17%.
    entries = []
    for i in range(19):
        entries.append({"symbol": "ETH-USD", "direction": "long",
                        "pnl_net_usd": 0.17, "hold_time_ms": 600_000,
                        "outcome": "win"})
    for i in range(86):
        won = i < 15  # ~17% WR
        entries.append({"symbol": "ETH-USD", "direction": "short",
                        "pnl_net_usd": 0.5 if won else -0.37,
                        "hold_time_ms": 600_000,
                        "outcome": "win" if won else "loss"})
    return entries


def test_direction_split_unpoisons_longs():
    th = SymbolEdgeThrottler()
    j = _eth_journal()
    long_e = th.get_symbol_edge("ETH-USD", list(j), direction="long")
    short_e = th.get_symbol_edge("ETH-USD", list(j), direction="short")
    assert long_e["win_rate"] == 1.0
    assert long_e["edge_mult"] == 1.5       # winners earn the boost
    assert short_e["edge_mult"] == 0.5      # losers get throttled
    assert "dir=long" in long_e["reason"]


def test_pooled_read_unchanged_when_no_direction():
    th = SymbolEdgeThrottler()
    j = _eth_journal()
    pooled = th.get_symbol_edge("ETH-USD", list(j))
    # 34/105 wins ≈ 32% WR, negative avg → moderate throttle (the poison)
    assert pooled["edge_mult"] == 0.75


def test_direction_fail_open_on_thin_sample():
    th = SymbolEdgeThrottler()
    j = _eth_journal()
    thin = th.get_symbol_edge("ETH-USD", list(j), direction="long")
    # drop to 4 longs — below min_trades: fail OPEN to 1.0, never pooled
    j4 = j[:4] + j[19:]
    thin = SymbolEdgeThrottler().get_symbol_edge("ETH-USD", list(j4),
                                                 direction="long")
    assert thin["edge_mult"] == 1.0
    assert "below_min" in thin["reason"]
    assert "dir_" in thin["reason"]


def test_direction_kill_switch_restores_pooling(monkeypatch):
    monkeypatch.setenv("SYMBOL_EDGE_DIRECTION_ENABLED", "false")
    th = SymbolEdgeThrottler()
    j = _eth_journal()
    got = th.get_symbol_edge("ETH-USD", list(j), direction="long")
    pooled = SymbolEdgeThrottler().get_symbol_edge("ETH-USD", list(j))
    assert got == pooled  # legacy bit-for-bit


def test_direction_cache_keys_isolated():
    th = SymbolEdgeThrottler()
    j = _eth_journal()
    a = th.get_symbol_edge("ETH-USD", list(j), direction="long")
    b = th.get_symbol_edge("ETH-USD", list(j), direction="short")
    c = th.get_symbol_edge("ETH-USD", list(j), direction="long")
    assert a is c          # cached per (symbol, direction)
    assert a is not b
    assert a["edge_mult"] != b["edge_mult"]


# ── Skeptic: direction + decay ───────────────────────────────────────────────

class _StubJournal:
    def __init__(self, records):
        self._r = records

    def scored_records(self):
        return list(self._r)


def _rec(direction, won, age_days, now=1_800_000_000.0):
    return {"symbol": "BTC-USD", "direction": direction, "won_24h": won,
            "ts": now - age_days * 86400.0, "regime": "trend"}


NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("SKEPTIC_DIRECTION_ENABLED", raising=False)
    monkeypatch.delenv("SKEPTIC_DECAY_HALFLIFE_DAYS", raising=False)


def test_skeptic_direction_splits_pool():
    # 20 fresh longs all winners, 20 fresh shorts all losers — pooled is
    # 50%, direction-split is decisive each way. Age 0 keeps decay weight
    # 1.0 so this pin isolates the direction dimension.
    recs = [_rec("long", True, 0) for _ in range(20)]
    recs += [_rec("short", False, 0) for _ in range(20)]
    sk = Skeptic(_StubJournal(recs))
    pooled_wr, pooled_n = sk.base_rate(symbol="BTC-USD", regime="trend",
                                       prior_wr=0.5, now=NOW)
    long_wr, long_n = sk.base_rate(symbol="BTC-USD", regime="trend",
                                   prior_wr=0.5, direction="long", now=NOW)
    short_wr, _ = sk.base_rate(symbol="BTC-USD", regime="trend",
                               prior_wr=0.5, direction="short", now=NOW)
    assert pooled_n == 40 and abs(pooled_wr - 0.5) < 0.01
    # k=20 shrinkage at n=20 = half prior: (20+10)/40 = 0.75 vs pooled 0.5
    assert long_n == 20 and long_wr == pytest.approx(0.75, abs=1e-6)
    assert short_wr == pytest.approx(0.25, abs=1e-6)


def test_skeptic_direction_kill_switch(monkeypatch):
    # Legacy bit-for-bit requires BOTH switches off (direction restore
    # pooling, decay 0 restores unweighted counts).
    monkeypatch.setenv("SKEPTIC_DIRECTION_ENABLED", "false")
    monkeypatch.setenv("SKEPTIC_DECAY_HALFLIFE_DAYS", "0")
    recs = [_rec("long", True, 1) for _ in range(20)]
    recs += [_rec("short", False, 1) for _ in range(20)]
    sk = Skeptic(_StubJournal(recs))
    wr, n = sk.base_rate(symbol="BTC-USD", regime="trend", prior_wr=0.5,
                         direction="long", now=NOW)
    assert n == 40  # pooled legacy read


def test_skeptic_decay_halves_weight_per_halflife():
    # 10 fresh wins (age 0) vs 10 losses exactly 14 days old: losses weigh
    # half → effective n = 15, wins_w = 10 → blended pulls above 2/3 prior.
    recs = [_rec("long", True, 0) for _ in range(10)]
    recs += [_rec("long", False, 14) for _ in range(10)]
    sk = Skeptic(_StubJournal(recs))
    wr, n = sk.base_rate(symbol="BTC-USD", regime="trend", prior_wr=0.5,
                         direction="long", now=NOW)
    assert n == 15  # 10×1.0 + 10×0.5, rounded
    assert wr == pytest.approx((10 + 20 * 0.5) / (15 + 20), abs=1e-6)


def test_skeptic_decay_off_is_legacy(monkeypatch):
    monkeypatch.setenv("SKEPTIC_DECAY_HALFLIFE_DAYS", "0")
    recs = [_rec("long", True, 0) for _ in range(10)]
    recs += [_rec("long", False, 14) for _ in range(10)]
    sk = Skeptic(_StubJournal(recs))
    wr, n = sk.base_rate(symbol="BTC-USD", regime="trend", prior_wr=0.5,
                         now=NOW)
    assert n == 20
    assert wr == pytest.approx((10 + 20 * 0.5) / (20 + 20), abs=1e-6)


def test_skeptic_decay_bad_env_falls_back(monkeypatch):
    monkeypatch.setenv("SKEPTIC_DECAY_HALFLIFE_DAYS", "garbage")
    assert skeptic_mod.skeptic_decay_halflife_days() == 14.0


def test_skeptic_missing_ts_counts_as_fresh():
    recs = [{"symbol": "BTC-USD", "direction": "long", "won_24h": True}
            for _ in range(10)]
    sk = Skeptic(_StubJournal(recs))
    _, n = sk.base_rate(symbol="BTC-USD", prior_wr=0.5, direction="long",
                        now=NOW)
    assert n == 10  # full weight, no crash
