"""Behavioral + wiring pins for intelligence/equity_oi_plane.py (2026-10-18
Governor BUILD ORDER: "Build equity_oi_plane.py next... Build it live. The
oi_velocity brain already has fail-closed 0.0. If SoDEX REST returns
unexpected data: brain abstains. No risk to existing signals. No shadow
period needed.")

Covers: series shape/ordering, <6-point abstain, >48h stale abstain, kill
switch off = None, persistence round-trip (tmp_path), poll-failure keeps
last good state, the defensive wire parser (bare LIST + {"data": [...]}),
and the main.py wiring pins (oi_series_fn no longer the None lambda,
EQUITY_OI_PLANE_ENABLED present, funding_loop piggyback present).
"""
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from intelligence import equity_oi_plane as _p  # noqa: E402

_PLANE_SRC = (_ROOT / "intelligence" / "equity_oi_plane.py").read_text()
_MAIN_SRC = (_ROOT / "main.py").read_text()
_SODEX_SRC = (_ROOT / "data" / "sodex_feed.py").read_text()
_BYBIT_SRC = (_ROOT / "data" / "bybit_feed.py").read_text()


# ── helpers ───────────────────────────────────────────────────────────────────

class _Clock:
    def __init__(self, t=1_760_700_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _mk_plane(clock, **kw):
    return _p.EquityOIPlane(time_fn=clock, **kw)


def _fill(plane, n=8, step_ms=300_000, base_oi=100.0, clock=None):
    """Ingest n points spaced step_ms apart ending at the clock's now."""
    now_ms = (clock.t if clock else 1_760_700_000.0) * 1000.0
    for i in range(n):
        ts = now_ms - (n - 1 - i) * step_ms
        plane.record("AMD-USD", base_oi + i, ts)


# ── parser ────────────────────────────────────────────────────────────────────

class TestParser:
    def test_bare_list_governor_verified_shape(self):
        rows = _p.parse_mark_prices([
            {"symbol": "DRAM-USD", "openInterest": "481.6",
             "markPrice": "46.035", "fundingRate": "0.000039"},
            {"symbol": "AMD-USD", "openInterest": "1200.5"},
        ])
        assert rows == [("DRAM-USD", 481.6), ("AMD-USD", 1200.5)]

    def test_data_wrapper_accepted(self):
        rows = _p.parse_mark_prices(
            {"data": [{"symbol": "SPCX-USD", "openInterest": "55.0"}]})
        assert rows == [("SPCX-USD", 55.0)]

    def test_garbage_drops_individually(self):
        rows = _p.parse_mark_prices([
            {"symbol": "AMD-USD", "openInterest": "10"},
            {"symbol": "BAD-USD", "openInterest": "not_a_number"},
            {"symbol": "", "openInterest": "5"},
            {"no_symbol": True, "openInterest": "5"},
            {"symbol": "NEG-USD", "openInterest": "-3"},
            "not_a_dict",
        ])
        assert rows == [("AMD-USD", 10.0)]

    def test_non_dict_non_list_empty(self):
        assert _p.parse_mark_prices(None) == []
        assert _p.parse_mark_prices("junk") == []
        assert _p.parse_mark_prices(42) == []


# ── series contract ───────────────────────────────────────────────────────────

class TestSeries:
    def test_series_shape_and_oldest_first_ordering(self):
        clock = _Clock()
        plane = _mk_plane(clock)
        _fill(plane, n=8, clock=clock)
        s = plane.series("AMD-USD")
        assert s is not None and len(s) == 8
        ts = [t for t, _ in s]
        assert ts == sorted(ts)                      # oldest-first
        assert all(isinstance(v, float) for _, v in s)
        assert s[-1][1] == 107.0                     # newest last

    def test_thin_series_abstains(self):
        clock = _Clock()
        plane = _mk_plane(clock)
        _fill(plane, n=5, clock=clock)               # < 6 points
        assert plane.series("AMD-USD") is None
        plane.record("AMD-USD", 106.0, clock.t * 1000.0 + 300_000)
        clock.t += 300.0
        assert plane.series("AMD-USD") is not None   # 6th point frees it

    def test_stale_series_abstains(self):
        clock = _Clock()
        plane = _mk_plane(clock)
        _fill(plane, n=8, clock=clock)
        clock.t += 48 * 3600.0 + 1.0                 # newest point > 48h old
        assert plane.series("AMD-USD") is None

    def test_stale_boundary_fresh_at_48h(self):
        clock = _Clock()
        plane = _mk_plane(clock)
        _fill(plane, n=8, clock=clock)
        clock.t += 48 * 3600.0 - 10.0
        assert plane.series("AMD-USD") is not None

    def test_kill_switch_off_returns_none(self, monkeypatch):
        clock = _Clock()
        plane = _mk_plane(clock)
        _fill(plane, n=8, clock=clock)
        monkeypatch.setenv("EQUITY_OI_PLANE_ENABLED", "false")
        assert plane.series("AMD-USD") is None
        monkeypatch.setenv("EQUITY_OI_PLANE_ENABLED", "true")
        assert plane.series("AMD-USD") is not None

    def test_unknown_symbol_none(self):
        clock = _Clock()
        plane = _mk_plane(clock)
        _fill(plane, n=8, clock=clock)
        assert plane.series("TSLA-USD") is None

    def test_record_dedup_and_order_integrity(self):
        clock = _Clock()
        plane = _mk_plane(clock)
        now_ms = clock.t * 1000.0
        assert plane.record("AMD-USD", 100.0, now_ms) is True
        assert plane.record("AMD-USD", 101.0, now_ms) is False   # same ts refresh
        assert plane.record("AMD-USD", 99.0, now_ms - 1) is False  # older ignored
        assert plane.record("AMD-USD", 102.0, now_ms + 1) is True
        dq = plane._series["AMD-USD"]
        assert [v for _, v in dq] == [101.0, 102.0]   # refreshed, appended

    def test_record_garbage_fail_closed(self):
        clock = _Clock()
        plane = _mk_plane(clock)
        assert plane.record("AMD-USD", "junk", 1) is False
        assert plane.record("AMD-USD", 1.0, "junk") is False
        assert plane.record("AMD-USD", float("nan"), 1) is False
        assert plane.record("AMD-USD", -1.0, 1) is False
        assert plane.record("", 1.0, 1) is False
        assert plane.series("AMD-USD") is None


# ── persistence ───────────────────────────────────────────────────────────────

class TestPersistence:
    def test_round_trip(self, tmp_path):
        clock = _Clock()
        plane = _mk_plane(clock, log_dir=str(tmp_path))
        _fill(plane, n=8, clock=clock)
        plane.record("TSLA-USD", 5.0, clock.t * 1000.0)
        plane.persist()
        assert (tmp_path / "equity_oi_history.json").exists()

        plane2 = _mk_plane(clock, log_dir=str(tmp_path))
        restored = plane2.load()
        assert restored == 2
        assert plane2.series("AMD-USD") == plane.series("AMD-USD")

    def test_snapshot_restore_contract(self):
        clock = _Clock()
        plane = _mk_plane(clock)
        _fill(plane, n=8, clock=clock)
        snap = plane.snapshot()
        plane2 = _mk_plane(clock)
        assert plane2.restore(snap) == 1
        assert plane2.series("AMD-USD") == plane.series("AMD-USD")
        # restore is defensive: garbage rows kill only themselves
        assert plane2.restore({"BAD": "junk", "BAD2": [["x", 1]], "": []}) == 0
        assert plane2.restore(None) == 0

    def test_load_missing_file_cold_start(self, tmp_path):
        clock = _Clock()
        plane = _mk_plane(clock, log_dir=str(tmp_path))
        assert plane.load() == 0
        assert plane.series("AMD-USD") is None


# ── poll failure = last good state ────────────────────────────────────────────

class TestPollFailure:
    def test_failed_poll_keeps_last_good_state(self):
        clock = _Clock()
        plane = _mk_plane(clock)
        _fill(plane, n=8, clock=clock)
        good = plane.series("AMD-USD")
        # A failed poll delivers nothing / garbage — state must be untouched.
        assert plane.ingest_rows(None) == 0
        assert plane.ingest_rows([]) == 0
        assert plane.ingest_rows([{"symbol": "AMD-USD",
                                   "openInterest": "garbage"}]) == 0
        assert plane.series("AMD-USD") == good

    def test_ingest_poll_emits_and_persists(self, tmp_path):
        clock = _Clock()
        plane = _mk_plane(clock, log_dir=str(tmp_path))
        n = _p.ingest_poll(plane, [
            {"symbol": "AMD-USD", "openInterest": "100"},
            {"symbol": "TSLA-USD", "openInterest": "50"},
        ])
        assert n == 2
        assert (tmp_path / "equity_oi_history.json").exists()
        assert plane.freshest_age_s() == 0.0

    def test_ingest_poll_never_raises_on_garbage(self, tmp_path):
        clock = _Clock()
        plane = _mk_plane(clock, log_dir=str(tmp_path))
        assert _p.ingest_poll(plane, "total garbage") == 0
        assert _p.ingest_poll(plane, None) == 0


# ── wiring pins ───────────────────────────────────────────────────────────────

class TestWiringPins:
    def test_oi_series_fn_no_longer_none_lambda(self):
        assert "oi_series_fn=lambda _s: None" not in _MAIN_SRC
        assert "oi_series_fn=_equity_oi_series" in _MAIN_SRC

    def test_kill_switch_env_pinned(self):
        assert "EQUITY_OI_PLANE_ENABLED" in _PLANE_SRC
        assert '"true"' in _PLANE_SRC                       # default on

    def test_plane_constructed_and_restored_in_main(self):
        assert "_eq_oi_plane.EquityOIPlane(" in _MAIN_SRC
        assert "_equity_oi_plane.load()" in _MAIN_SRC
        assert "equity_oi_plane_enabled" in _MAIN_SRC

    def test_funding_loop_piggyback_present(self):
        # The plane rides the existing mark-prices poll — zero new API cost.
        assert "latest_mark_price_rows" in _MAIN_SRC
        assert "ingest_poll(_equity_oi_plane, _rows)" in _MAIN_SRC
        assert "latest_mark_price_rows" in _SODEX_SRC      # stash + accessor
        assert "latest_mark_price_rows" in _BYBIT_SRC      # HybridFeed delegate

    def test_telemetry_contract_names(self):
        assert _p.EVENT_POLLED == "equity_oi_polled"
        assert _p.EVENT_FAILED == "equity_oi_poll_failed"
        assert '"equity_oi_polled"' in _PLANE_SRC
        assert '"equity_oi_poll_failed"' in _PLANE_SRC
        assert "EVENT_FAILED" in _MAIN_SRC

    def test_sodex_feed_stash_shape_defensive(self):
        # Both the Governor-verified bare list and the historical wrapper.
        assert "isinstance(payload, list)" in _SODEX_SRC
        assert '_last_mark_price_error' in _SODEX_SRC
