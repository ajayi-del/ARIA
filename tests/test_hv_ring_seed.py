"""HV ring seed + health (intelligence/hv_ring_seed.py) — rvr warmup-leg repair.

The rvr pyramid leg reads rv_rank over _BC_HV_HIST, which needs >=10 prints
and is fed only inside on_signal_ready at >=900s spacing — a signal drought
starves it silently and a fresh boot restarts the ~2.5h warm-up from the
handful of prints the persistence layer happened to capture. The repair
seeds the ring from the already-seeded 15m boot buffer (sliding-window
Parkinson prints) and adds a ring-health verdict for stall detection.
"""
import math

from intelligence import breakout_coherence as bc
from intelligence import hv_ring_seed as hrs


# ── helpers ─────────────────────────────────────────────────────────────

def _bars(n, start_ts=1_000_000.0, step=900.0, hi=101.0, lo=99.0, close=100.0):
    """n synthetic 15m bars: (ts, high, low, close), 1% constant range."""
    return [(start_ts + i * step, hi, lo, close) for i in range(n)]


# ── parkinson parity with the canonical estimator ───────────────────────

def test_parkinson_parity_with_breakout_coherence():
    bars = _bars(40, hi=101.3, lo=98.7)
    highs = [b[1] for b in bars]
    lows = [b[2] for b in bars]
    closes = [b[3] for b in bars]
    got = hrs.parkinson_hv_bar(highs, lows, closes, periods_per_year=35040)
    want = bc.parkinson_hv(highs, lows, closes, periods_per_year=35040)
    assert got is not None and want is not None
    assert math.isclose(got, want, rel_tol=1e-12)


def test_parkinson_degenerate_inputs():
    assert hrs.parkinson_hv_bar([], [], []) is None
    assert hrs.parkinson_hv_bar([1.0], [1.0], [1.0]) is None      # <2 bars
    assert hrs.parkinson_hv_bar([0.0, 1.0], [0.5, 0.9], [1, 1]) is None  # h<=0
    assert hrs.parkinson_hv_bar([1.0, 1.0], [2.0, 0.5], [1, 1]) is None  # h<l
    assert hrs.parkinson_hv_bar([1, 1], [1, 1], [1]) is None      # length mismatch
    # all zero ranges -> acc 0 -> hv 0.0 (a real print, not None)
    assert hrs.parkinson_hv_bar([5.0, 5.0], [5.0, 5.0], [5.0, 5.0]) == 0.0


# ── hv_prints_from_bars ─────────────────────────────────────────────────

def test_prints_normal_path_55_bars():
    """The boot seed case: 55 bars -> 55-12+1 = 44 prints, past the rv_rank
    floor of 10 — the 2.5h starvation dies at boot."""
    prints = hrs.hv_prints_from_bars(_bars(55))
    assert len(prints) == 44
    # timestamps are the window's last bar ts, monotone
    ts = [p[0] for p in prints]
    assert ts == sorted(ts)
    assert ts[0] == 1_000_000.0 + 11 * 900.0
    assert all(hv > 0 for _, hv in prints)


def test_prints_empty_and_short_buffers():
    assert hrs.hv_prints_from_bars([]) == []
    assert hrs.hv_prints_from_bars(_bars(11)) == []        # below window
    assert len(hrs.hv_prints_from_bars(_bars(12))) == 1    # exactly window


def test_prints_zero_range_bars_yield_zero_hv_print():
    prints = hrs.hv_prints_from_bars(_bars(12, hi=100.0, lo=100.0))
    assert len(prints) == 1 and prints[0][1] == 0.0


def test_prints_invalid_bars_skipped_not_fatal():
    bars = _bars(12)
    bars[5] = (bars[5][0], 90.0, 95.0, 92.0)   # h < l inside one window
    prints = hrs.hv_prints_from_bars(bars)
    # windows containing the bad bar still compute (bad bar skipped inside
    # the estimator as long as >=2 usable bars remain) — never raises
    assert prints and all(hv is not None for _, hv in prints)


def test_prints_malformed_rows_return_empty():
    assert hrs.hv_prints_from_bars([("x", 1, 2, 3)]) == []
    assert hrs.hv_prints_from_bars(None or []) == []


def test_prints_unsorted_bars_sorted_first():
    bars = _bars(15)
    scrambled = list(reversed(bars))
    assert hrs.hv_prints_from_bars(scrambled) == hrs.hv_prints_from_bars(bars)


# ── seed_ring: merge + idempotency + cap ────────────────────────────────

def test_seed_into_empty_ring():
    ring = hrs.seed_ring([], _bars(55))
    assert len(ring) == 44
    assert [p[0] for p in ring] == sorted(p[0] for p in ring)


def test_seed_idempotent_same_bars_twice():
    once = hrs.seed_ring([], _bars(55))
    twice = hrs.seed_ring(once, _bars(55))
    assert twice == once


def test_seed_skips_prints_near_existing_live_print():
    """A live print for the same bar lands seconds after bar close — inside
    the half-interval dedupe bucket — so re-seeding must not duplicate it."""
    bars = _bars(20)
    live_ts = bars[11][0] + 0.3          # live print just after bar close
    ring = [(live_ts, 0.42)]
    seeded = hrs.seed_ring(ring, bars)
    # 9 windows total; the one whose ts matches the live print is skipped
    assert len(seeded) == 9
    assert (live_ts, 0.42) in seeded
    assert not any(abs(ts - bars[11][0]) < 1e-9 for ts, _ in seeded)


def test_seed_backfills_history_around_existing_ring():
    existing = [(1_000_000.0 + 30 * 900.0, 0.5)]
    seeded = hrs.seed_ring(existing, _bars(40))
    assert len(seeded) == 29               # 29 windows, none near the keeper
    assert existing[0] in seeded
    assert [p[0] for p in seeded] == sorted(p[0] for p in seeded)


def test_seed_drops_future_prints_when_now_given():
    bars = _bars(20, start_ts=1_000_000.0)
    now = bars[14][0]                      # only windows ending <= bar14 kept
    seeded = hrs.seed_ring([], bars, now=now)
    assert all(ts <= now for ts, _ in seeded)


def test_seed_future_boundary_exact():
    bars = _bars(20, start_ts=1_000_000.0)
    now = bars[14][0]
    seeded = hrs.seed_ring([], bars, now=now)
    assert len(seeded) == 4
    # ts exactly at now is kept (inclusive)
    assert seeded[-1][0] == now


def test_seed_ring_cap_keeps_newest():
    seeded = hrs.seed_ring([], _bars(200))          # 189 windows
    assert len(seeded) == hrs.RING_CAP
    bars = _bars(200)
    assert seeded[-1][0] == bars[-1][0]             # newest retained
    assert seeded[0][0] == bars[len(bars) - hrs.RING_CAP][0]


def test_seed_malformed_ring_rows_skipped():
    seeded = hrs.seed_ring([("bad", 1), None, (1_000_000.0, 0.3)],
                           _bars(12, start_ts=2_000_000.0))
    assert (1_000_000.0, 0.3) in seeded
    assert len(seeded) == 2


def test_seed_empty_bars_returns_cleaned_ring():
    ring = [(2.0, 0.1), (1.0, 0.2)]
    assert hrs.seed_ring(ring, []) == [(1.0, 0.2), (2.0, 0.1)]


# ── boundary counts vs the rv_rank floor ────────────────────────────────

def test_boundary_10_prints_warms_rv_rank():
    bars = _bars(21)                       # 21-12+1 = 10 prints exactly
    seeded = hrs.seed_ring([], bars)
    assert len(seeded) == 10
    assert bc.rv_rank([v for _, v in seeded], 0.5) is not None


def test_boundary_9_prints_rv_rank_still_none():
    seeded = hrs.seed_ring([], _bars(20))  # 9 prints
    assert len(seeded) == 9
    assert bc.rv_rank([v for _, v in seeded], 0.5) is None


# ── ring_health verdicts ────────────────────────────────────────────────

def test_health_empty_ring():
    h = hrs.ring_health([], now=1_000.0)
    assert h["verdict"] == "empty" and h["prints"] == 0
    assert h["warm"] is False and h["stalled"] is False
    assert h["last_print_ts"] is None


def test_health_seeding_below_floor():
    ring = [(900.0, 0.3)] * 5
    h = hrs.ring_health(ring, now=1000.0)
    assert h["verdict"] == "seeding" and h["prints"] == 5
    assert h["warm"] is False and h["stalled"] is False
    assert h["last_age_s"] == 100.0


def test_health_warm_fresh_ring():
    ring = [(900.0 + i * 900.0, 0.3) for i in range(12)]
    now = ring[-1][0] + 300.0
    h = hrs.ring_health(ring, now=now)
    assert h["verdict"] == "warm" and h["warm"] is True
    assert h["stalled"] is False and h["last_age_s"] == 300.0


def test_health_stalled_ring_detected():
    """The 2026-09-21 7h-silence case: prints exist but the newest is
    ancient — the feed stopped and nothing noticed."""
    ring = [(900.0 + i * 900.0, 0.3) for i in range(12)]
    now = ring[-1][0] + 7 * 3600.0
    h = hrs.ring_health(ring, now=now)
    assert h["verdict"] == "stalled" and h["stalled"] is True
    assert h["warm"] is True          # warm but stale — distinct signals


def test_health_stall_boundary_inclusive():
    ring = [(0.0, 0.3)]
    at = hrs.ring_health(ring, now=hrs.STALL_AFTER_S)
    assert at["stalled"] is False                       # exactly at = fresh
    past = hrs.ring_health(ring, now=hrs.STALL_AFTER_S + 0.1)
    assert past["stalled"] is True


def test_health_malformed_ring_is_empty_not_fatal():
    h = hrs.ring_health([("x",)], now=1.0)
    assert h["verdict"] == "empty" and h["prints"] == 0


def test_health_uses_newest_print_not_last_row():
    ring = [(5000.0, 0.3), (100.0, 0.2)]               # unsorted input
    h = hrs.ring_health(ring, now=5100.0)
    assert h["last_print_ts"] == 5000.0 and h["last_age_s"] == 100.0
