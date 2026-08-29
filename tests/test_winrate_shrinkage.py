"""
tests/test_winrate_shrinkage.py — Workstream F pins.

F1: _win_rate_band empirical-Bayes shrinkage (k=20) — an n=1 sample must not
    slam the 0.25 floor; n=None stays legacy bit-for-bit.
F2: is_phantom_record predicate (2026-08-21/22 SPCX scale-mismatch ghosts),
    cross-file dedup in restore_from_journal, and the purge script's
    subtraction on fixture data.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from intelligence.nietzsche_engine import _win_rate_band, _WIN_RATE_SHRINK_K
from memory.performance import PerformanceTracker, is_phantom_record
from tools.purge_phantom_personality_stats import collect_phantoms


# ── F1: _win_rate_band shrinkage ─────────────────────────────────────────────


def _shrunk(wr: float, n: int) -> float:
    return (wr * n + 0.5 * _WIN_RATE_SHRINK_K) / (n + _WIN_RATE_SHRINK_K)


class TestWinRateBandShrinkage:
    def test_n_none_legacy_bit_for_bit(self):
        # The standard path (Skeptic already k=20-shrunk) passes n=None.
        for wr in (0.0, 0.10, 0.24, 0.25, 0.34, 0.35, 0.44, 0.45, 0.54, 0.55, 0.9):
            assert _win_rate_band(wr) == _win_rate_band(wr, n_trades=None)

    def test_legacy_band_curve_pinned(self):
        assert _win_rate_band(0.60) == 1.0
        assert _win_rate_band(0.55) == 1.0
        assert _win_rate_band(0.549) == 0.75
        assert _win_rate_band(0.45) == 0.75
        assert _win_rate_band(0.449) == 0.50
        assert _win_rate_band(0.35) == 0.50
        assert _win_rate_band(0.349) == 0.35
        assert _win_rate_band(0.25) == 0.35
        assert _win_rate_band(0.249) == 0.25
        assert _win_rate_band(0.00) == 0.25

    def test_n0_shrinks_to_prior(self):
        # n=0 → shrunk rate is exactly the 0.5 prior → 0.75 band
        # (matches pre-fix "unknown personality" behavior: get_win_rate
        # returns 0.5 for unknown, which bands at 0.75).
        assert _win_rate_band(0.0, n_trades=0) == 0.75

    def test_n1_loss_no_longer_slams_floor(self):
        # The 2026-08-24 incident: APEX 0W/1L → raw 0.0 → 0.25 floor capped
        # every cascade entry at 25% of venue equity on one -$0.44 loss.
        assert _win_rate_band(0.0, n_trades=1) == 0.75
        shrunk = _shrunk(0.0, 1)
        assert shrunk == pytest.approx(0.5 * 20 / 21)  # ≈ 0.4762

    def test_n15_partial_shrink(self):
        shrunk = _shrunk(0.20, 15)  # ≈ 0.371
        assert shrunk == pytest.approx((0.20 * 15 + 10.0) / 35)
        assert _win_rate_band(0.20, n_trades=15) == 0.50

    def test_large_n_barely_moves(self):
        # Away from band boundaries, a large-n rate stays in its band.
        # 0.30 raw, n=354 → shrunk ≈ 0.311 — same 0.35 band, no jump.
        assert _win_rate_band(0.30, n_trades=354) == _win_rate_band(0.30)
        shrunk = _shrunk(0.30, 354)
        assert shrunk == pytest.approx((0.30 * 354 + 10.0) / 374)

    def test_shrink_pulls_toward_prior_at_band_edge(self):
        # Documented boundary effect: 0.347 sits just below the 0.35 cut;
        # shrinkage toward 0.5 lifts it into the 0.50 band. Intended — the
        # prior says "probably average" until n proves otherwise.
        assert _win_rate_band(0.347, n_trades=354) == 0.50
        assert _win_rate_band(0.347) == 0.35

    def test_shrink_monotone_in_n(self):
        # For a below-prior raw rate, more samples → lower shrunk rate.
        rates = [_shrunk(0.10, n) for n in (0, 1, 5, 20, 100, 1000)]
        assert all(a >= b for a, b in zip(rates, rates[1:]))
        assert rates[-1] == pytest.approx(0.10, abs=0.02)

    def test_true_zero_edge_still_floors(self):
        # With enough evidence of a terrible rate, the floor still binds —
        # shrinkage guards small samples, it does not forgive proven losers.
        assert _win_rate_band(0.05, n_trades=200) == 0.25


# ── F2: phantom predicate ────────────────────────────────────────────────────


def _ms(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def _entry(**kw):
    base = {
        "symbol": "SPCX-USD",
        "outcome": "win",
        "pnl_usd": 700.0,
        "personality": "AFTERMATH",
        "entry_id": "e1",
        "closed_at_ms": _ms("2026-08-22"),
    }
    base.update(kw)
    return base


class TestPhantomPredicate:
    def test_positive_exact_match(self):
        assert is_phantom_record(_entry())
        assert is_phantom_record(_entry(pnl_usd=-649.4253, outcome="loss",
                                        closed_at_ms=_ms("2026-08-21")))
        # timestamp_ms fallback accepted
        e = _entry()
        del e["closed_at_ms"]
        e["timestamp_ms"] = _ms("2026-08-21")
        assert is_phantom_record(e)

    def test_negative_wrong_symbol(self):
        assert not is_phantom_record(_entry(symbol="BTC-USD"))

    def test_any_date_after_generalization(self):
        # 2026-08-29 journal-corruption audit: the census is perfectly
        # bimodal (561 real closes ALL <$5 vs 64 ghosts ALL >$100, zero
        # between), so the date bound was dropped — a >$100 SPCX close is
        # phantom on ANY date. The 08-24 date-bound purge caught only 4 of
        # the 64 ghosts; these days were previously asserted NOT phantom.
        assert is_phantom_record(_entry(closed_at_ms=_ms("2026-08-23")))
        assert is_phantom_record(_entry(closed_at_ms=_ms("2026-08-20")))

    def test_negative_small_pnl(self):
        assert not is_phantom_record(_entry(pnl_usd=99.99))
        assert not is_phantom_record(_entry(pnl_usd=-100.0))
        assert not is_phantom_record(_entry(pnl_usd=0.0))

    def test_no_timestamp_still_phantom(self):
        # The generalized predicate needs no date — the $100 bimodal split
        # is sufficient evidence on its own.
        e = _entry()
        del e["closed_at_ms"]
        assert is_phantom_record(e)


# ── F2: restore_from_journal dedup + phantom filter ──────────────────────────


class TestRestoreFromJournal:
    def _write(self, log_dir: Path, name: str, records: list) -> None:
        (log_dir / name).write_text(json.dumps(records))

    def test_phantoms_excluded_and_dupes_deduped(self, tmp_path):
        phantom = _entry(pnl_usd=792.6534, entry_id="ph1")
        real = _entry(symbol="BTC-USD", pnl_usd=1.5, entry_id="r1",
                      closed_at_ms=_ms("2026-08-22"))
        # Journal day-files are rolling windows — later files re-contain
        # earlier records. Same (entry_id, closed_at_ms) in two files.
        self._write(tmp_path, "trade_journal_2026-08-21.json", [real, phantom])
        self._write(tmp_path, "trade_journal_2026-08-22.json", [real, phantom])

        pt = PerformanceTracker()
        pt.restore_from_journal(str(tmp_path))

        stats = pt.get_personality_stats("AFTERMATH")
        assert stats is not None
        # 1 real record, counted once (not twice); 0 phantom contribution
        assert stats.total_trades == 1
        assert stats.total_pnl_usd == pytest.approx(1.5)

    def test_distinct_records_same_id_different_ts_kept(self, tmp_path):
        a = _entry(symbol="BTC-USD", pnl_usd=1.0, entry_id="x",
                   closed_at_ms=_ms("2026-08-22"))
        b = _entry(symbol="BTC-USD", pnl_usd=2.0, entry_id="x",
                   closed_at_ms=_ms("2026-08-22") + 60_000, outcome="loss")
        self._write(tmp_path, "trade_journal_2026-08-22.json", [a, b])
        pt = PerformanceTracker()
        pt.restore_from_journal(str(tmp_path))
        stats = pt.get_personality_stats("AFTERMATH")
        assert stats.total_trades == 2

    def test_no_data_returns_cleanly(self, tmp_path):
        pt = PerformanceTracker()
        pt.restore_from_journal(str(tmp_path))  # no files — must not raise
        assert pt.get_personality_stats("AFTERMATH") is None


# ── F2: purge script collect_phantoms ────────────────────────────────────────


class TestPurgeCollect:
    def test_collects_per_personality_aggregates(self, tmp_path):
        ph_win = _entry(pnl_usd=635.088, entry_id="p1", closed_at_ms=_ms("2026-08-22"))
        ph_loss = _entry(pnl_usd=-649.4253, outcome="loss", entry_id="p2",
                         closed_at_ms=_ms("2026-08-21"))
        real = _entry(symbol="BTC-USD", pnl_usd=500.0, entry_id="r1")
        (tmp_path / "trade_journal_a.json").write_text(json.dumps([ph_win, ph_loss, real]))
        # rolling-window duplicate of ph_win in a second file — counted once
        (tmp_path / "trade_journal_b.json").write_text(json.dumps([ph_win]))

        agg = collect_phantoms(tmp_path)
        assert set(agg) == {"AFTERMATH"}
        a = agg["AFTERMATH"]
        assert a["n"] == 2
        assert a["wins"] == 1
        assert a["losses"] == 1
        assert a["pnl"] == pytest.approx(635.088 - 649.4253)

    def test_no_phantoms_empty(self, tmp_path):
        real = _entry(symbol="BTC-USD", pnl_usd=3.0, entry_id="r1")
        (tmp_path / "trade_journal_a.json").write_text(json.dumps([real]))
        assert collect_phantoms(tmp_path) == {}

    def test_non_outcome_records_ignored(self, tmp_path):
        open_rec = _entry(entry_id="o1")
        del open_rec["outcome"]
        (tmp_path / "trade_journal_a.json").write_text(json.dumps([open_rec]))
        assert collect_phantoms(tmp_path) == {}
