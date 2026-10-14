"""
tests/test_stock_perp_atr_reference.py — pins for the 2026-10-14 stock-perp
ATR reference fallback (Fix A of the 637x signal_rejected_atr_sanity
repair).

Doctrine under test:
  - aggregate_1m_to_15m buckets Yahoo 1m arrays into [o,h,l,c] 15m bars;
    None rows dropped; bucket boundary = ts // 900; array order preserved.
  - wilder_atr: Wilder smoothing over TR; None below period+1 bars;
    known-series value pinned.
  - _stock_perp_reference_atr (main.py): Level 1 yahoo_reference maps the
    underlying's 15m ATR onto the entry plane by % vol (ratio >= 1e-4);
    Level 2 synthetic_floor = entry x category ratio (equity 0.003,
    commodity 0.0035) — the never-blocked guarantee; non-stock categories
    and degenerate entries return (None, None) — crypto bit-for-bit.
  - tradfi_feed.T tradfi_reference_atr accessor returns the feed's
    _ref_atr tuple; None when the feed is dark.

No network; the tradfi accessor is monkeypatched at the import site.
"""
import pytest

from data.tradfi_feed import aggregate_1m_to_15m, wilder_atr
from main import _STOCK_PERP_ATR_FLOOR_RATIO, _stock_perp_reference_atr


# ── aggregate_1m_to_15m ─────────────────────────────────────────────────

class TestAggregate:
    def test_single_bucket_merges(self):
        # 3 one-minute bars inside one 900s bucket: open of the first,
        # max high, min low, close of the last.
        ts = [900_000, 900_060, 900_120]
        o = [10.0, 11.0, 12.0]
        h = [10.5, 11.8, 12.4]
        l = [9.5, 10.9, 11.7]
        c = [10.4, 11.7, 12.3]
        bars = aggregate_1m_to_15m(ts, o, h, l, c)
        assert bars == [[10.0, 12.4, 9.5, 12.3]]

    def test_bucket_boundary_splits(self):
        ts = [900_000, 900_060, 900_900]     # third bar = next 15m bucket
        o = [10.0, 11.0, 20.0]
        h = [10.5, 11.5, 20.5]
        l = [9.5, 10.5, 19.5]
        c = [10.4, 11.4, 20.4]
        bars = aggregate_1m_to_15m(ts, o, h, l, c)
        assert bars == [[10.0, 11.5, 9.5, 11.4], [20.0, 20.5, 19.5, 20.4]]

    def test_none_rows_dropped(self):
        ts = [900_000, 900_060, 900_120]
        o = [10.0, None, 12.0]
        h = [10.5, None, 12.5]
        l = [9.5, None, 11.5]
        c = [10.4, None, 12.4]
        bars = aggregate_1m_to_15m(ts, o, h, l, c)
        assert bars == [[10.0, 12.5, 9.5, 12.4]]

    def test_empty_and_short_arrays(self):
        assert aggregate_1m_to_15m([], [], [], [], []) == []
        # arrays shorter than timestamps -> IndexError rows skipped
        bars = aggregate_1m_to_15m([900_000, 900_060], [1.0], [1.0],
                                   [1.0], [1.0])
        assert bars == [[1.0, 1.0, 1.0, 1.0]]


# ── wilder_atr ───────────────────────────────────────────────────────────

class TestWilderAtr:
    def _flat_bars(self, n, o=100.0):
        return [[o, o + 1.0, o - 1.0, o] for _ in range(n)]

    def test_none_below_period_plus_one(self):
        assert wilder_atr(self._flat_bars(14), 14) is None
        assert wilder_atr([], 14) is None

    def test_constant_range_bars(self):
        # every bar: high-low = 2.0, prev close = current open (gapless)
        # -> TR = 2.0 for all -> ATR = 2.0 regardless of smoothing.
        bars = [[float(100 + i), float(101 + i), float(99 + i),
                 float(100 + i)] for i in range(20)]
        # TR: max(2.0, |101+i - (100+i-1)|, |99+i - (100+i-1)|) = 2.0
        assert wilder_atr(bars, 14) == pytest.approx(2.0)

    def test_wilder_smoothing_known_series(self):
        # 15 bars: first 14 TRs = 1.0, then one TR of 15.0.
        # SMA seed = 1.0; Wilder step: (1.0*13 + 15.0) / 14 = 2.0.
        bars = []
        px = 100.0
        for _ in range(15):
            bars.append([px, px + 0.5, px - 0.5, px])
        # force the last bar's TR to 15: high = prev_close + 15
        bars[-1] = [px, px + 15.0, px, px]
        assert wilder_atr(bars, 14) == pytest.approx(2.0)

    def test_default_period(self):
        bars = [[float(100 + i), float(101 + i), float(99 + i),
                 float(100 + i)] for i in range(20)]
        assert wilder_atr(bars) == pytest.approx(2.0)


# ── _stock_perp_reference_atr ────────────────────────────────────────────

class TestStockPerpReferenceAtr:
    def test_yahoo_reference_maps_pct_vol(self, monkeypatch):
        # underlying: atr 3.0 on px 600 -> ratio 0.005; entry plane 110.0
        # (rebased SPCX-class) -> atr = 110 * 0.005 = 0.55.
        monkeypatch.setattr(
            "data.tradfi_feed.tradfi_reference_atr",
            lambda sym: (3.0, 600.0, 1_760_000_000.0))
        atr, src = _stock_perp_reference_atr("SPCX-USD", 110.0, "equity")
        assert src == "yahoo_reference"
        assert atr == pytest.approx(0.55)

    def test_ratio_below_gate_falls_to_floor(self, monkeypatch):
        # ratio 1e-5 < 1e-4 pathology detector -> floor, not the reference.
        monkeypatch.setattr(
            "data.tradfi_feed.tradfi_reference_atr",
            lambda sym: (0.003, 600.0, 1_760_000_000.0))
        atr, src = _stock_perp_reference_atr("MSFT-USD", 544.0, "equity")
        assert src == "synthetic_floor"
        assert atr == pytest.approx(544.0 * 0.003)

    def test_dark_feed_falls_to_floor(self, monkeypatch):
        monkeypatch.setattr(
            "data.tradfi_feed.tradfi_reference_atr", lambda sym: None)
        atr, src = _stock_perp_reference_atr("CL-USD", 60.0, "commodity")
        assert src == "synthetic_floor"
        assert atr == pytest.approx(60.0 * 0.0035)

    def test_accessor_raise_falls_to_floor(self, monkeypatch):
        def _boom(sym):
            raise RuntimeError("feed down")
        monkeypatch.setattr(
            "data.tradfi_feed.tradfi_reference_atr", _boom)
        atr, src = _stock_perp_reference_atr("NVDA-USD", 180.0, "equity")
        assert src == "synthetic_floor"
        assert atr == pytest.approx(180.0 * 0.003)

    def test_malformed_ref_tuple_falls_to_floor(self, monkeypatch):
        monkeypatch.setattr(
            "data.tradfi_feed.tradfi_reference_atr",
            lambda sym: ("junk", None))
        atr, src = _stock_perp_reference_atr("AAPL-USD", 250.0, "equity")
        assert src == "synthetic_floor"

    def test_non_stock_categories_none(self, monkeypatch):
        # crypto must never reach this helper's floors — bit-for-bit None.
        monkeypatch.setattr(
            "data.tradfi_feed.tradfi_reference_atr",
            lambda sym: (3.0, 600.0, 1.0))
        assert _stock_perp_reference_atr("BTC-USD", 100_000.0,
                                         "crypto") == (None, None)
        assert _stock_perp_reference_atr("ETH-USD", 4_000.0,
                                         "") == (None, None)

    def test_degenerate_entry_none(self):
        assert _stock_perp_reference_atr("MSFT-USD", 0.0, "equity") \
            == (None, None)
        assert _stock_perp_reference_atr("MSFT-USD", -5.0, "equity") \
            == (None, None)
        assert _stock_perp_reference_atr("MSFT-USD", "junk", "equity") \
            == (None, None)

    def test_floor_ratios_locked(self):
        assert _STOCK_PERP_ATR_FLOOR_RATIO == {"equity": 0.003,
                                               "commodity": 0.0035}

    def test_floor_never_below_sanity_gate(self, monkeypatch):
        # The whole point of the fix: resolved atr/entry must clear the
        # 1e-4 atr_sanity pathology ratio on BOTH levels for every stock
        # category.
        monkeypatch.setattr(
            "data.tradfi_feed.tradfi_reference_atr", lambda sym: None)
        for cat, ratio in _STOCK_PERP_ATR_FLOOR_RATIO.items():
            atr, src = _stock_perp_reference_atr("X-USD", 544.0, cat)
            assert src == "synthetic_floor"
            assert atr / 544.0 >= 0.0001
