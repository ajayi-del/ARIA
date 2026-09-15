"""Stock-carry shadow plane pins (2026-09-15; the B4 register consumer loop).

Covers every pure helper in intelligence/stock_carry_plane.py: 4h kline
parsing (string fields, newest-first flip, forming-bar exclusion at the
exact boundary), ohlc_legs insufficiency/exact-21, hourly funding series
(gap -> None, same-hour dedup keeps latest), basis z series (one-bad-line),
direction mapping, shadow open->exit transitions incl. funding_bps sign
math, and the atomic write roundtrip.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelligence.stock_carry_plane import (
    BAR_4H_MS, HOUR_MS, append_jsonl, atomic_write_json, basis_z_series,
    close_shadow, direction_for, funding_bps_accrued, hourly_funding_series,
    latest_perp_mark, latest_spread_bps, newest_bar_age_ms, ohlc_legs,
    open_shadow, open_slot, parse_klines_4h, pnl_bps, read_state,
    with_closed, with_open,
)

NOW_MS = 1_800_000_000_000   # fixed clock for determinism


def _krow(t, o="100.0", h="101.0", l="99.0", c="100.5", v="1234.5"):
    return {"t": str(t), "o": o, "h": h, "l": l, "c": c, "v": v,
            "q": "123450.0", "n": "42"}


def _bars(n, vol0=100.0):
    """n ascending closed bars ending well before NOW_MS."""
    out = []
    base = NOW_MS - (n + 2) * BAR_4H_MS
    for i in range(n):
        t = base + i * BAR_4H_MS
        out.append((t, 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i,
                    vol0 + i))
    return out


class TestParseKlines4h(unittest.TestCase):
    def test_string_fields_to_floats_and_newest_first_flip(self):
        t_old = NOW_MS - 3 * BAR_4H_MS
        t_new = NOW_MS - 2 * BAR_4H_MS
        rows = [_krow(t_new), _krow(t_old)]     # newest-first wire order
        bars = parse_klines_4h(rows, NOW_MS)
        self.assertEqual([b[0] for b in bars], [t_old, t_new])
        self.assertEqual(bars[0],
                         (t_old, 100.0, 101.0, 99.0, 100.5, 1234.5))
        for b in bars:
            for x in b[1:]:
                self.assertIsInstance(x, float)

    def test_forming_bar_excluded(self):
        # open_time + 4h > now -> forming, dropped
        bars = parse_klines_4h([_krow(NOW_MS - BAR_4H_MS + 1)], NOW_MS)
        self.assertEqual(bars, [])

    def test_boundary_bar_is_closed(self):
        # open_time + 4h == now exactly -> closed (inclusive boundary)
        t = NOW_MS - BAR_4H_MS
        bars = parse_klines_4h([_krow(t)], NOW_MS)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0][0], t)

    def test_malformed_rows_skipped(self):
        t = NOW_MS - 2 * BAR_4H_MS
        rows = [_krow(t), {"t": "abc"}, {"o": "1"}, "garbage", None,
                {"t": str(t + 1), "o": "x", "h": "1", "l": "1", "c": "1",
                 "v": "1"}]
        bars = parse_klines_4h(rows, NOW_MS)
        self.assertEqual(len(bars), 1)

    def test_empty_and_none(self):
        self.assertEqual(parse_klines_4h([], NOW_MS), [])
        self.assertEqual(parse_klines_4h(None, NOW_MS), [])


class TestOhlcLegs(unittest.TestCase):
    def test_exact_21_bars(self):
        bars = _bars(21)
        legs = ohlc_legs(bars)
        prior = bars[:20]
        self.assertEqual(legs["vol_now"], bars[-1][5])
        self.assertAlmostEqual(legs["vol_baseline"],
                               sum(b[5] for b in prior) / 20.0)
        self.assertEqual(legs["range_now"], bars[-1][2] - bars[-1][3])
        self.assertAlmostEqual(legs["range_baseline"],
                               sum(b[2] - b[3] for b in prior) / 20.0)
        self.assertEqual(len(legs["closes"]), 21)
        self.assertEqual(legs["closes"][-1], bars[-1][4])

    def test_more_than_21_bars_tail_windows(self):
        bars = _bars(30)
        legs = ohlc_legs(bars)
        prior = bars[-21:-1]
        self.assertAlmostEqual(legs["vol_baseline"],
                               sum(b[5] for b in prior) / 20.0)
        self.assertEqual(len(legs["closes"]), 21)
        self.assertEqual(legs["closes"][0], bars[-21][4])

    def test_insufficient_bars_baselines_dark(self):
        bars = _bars(20)      # 20 < 21: no 20-bar prior window
        legs = ohlc_legs(bars)
        self.assertIsNotNone(legs["vol_now"])
        self.assertIsNotNone(legs["range_now"])
        self.assertIsNone(legs["vol_baseline"])
        self.assertIsNone(legs["range_baseline"])
        self.assertEqual(len(legs["closes"]), 20)

    def test_empty_all_dark(self):
        legs = ohlc_legs([])
        self.assertIsNone(legs["vol_now"])
        self.assertIsNone(legs["vol_baseline"])
        self.assertIsNone(legs["range_now"])
        self.assertIsNone(legs["range_baseline"])
        self.assertEqual(legs["closes"], [])


class TestHourlyFundingSeries(unittest.TestCase):
    def _rec(self, hour_bucket, rate, extra_ms=0):
        return {"symbol": "X-USD", "rate": rate,
                "timestamp_ms": hour_bucket * HOUR_MS + extra_ms,
                "source": "test"}

    def test_full_series_ascending(self):
        cur = NOW_MS // HOUR_MS
        recs = [self._rec(cur - i, 0.0001 * (i + 1)) for i in range(12)]
        s = hourly_funding_series(recs, NOW_MS, hours=12)
        self.assertEqual(len(s), 12)
        # oldest slot first: i=11 record (rate 0.0012) leads
        self.assertAlmostEqual(s[0], 0.0012)
        self.assertAlmostEqual(s[-1], 0.0001)

    def test_gap_hour_is_none(self):
        cur = NOW_MS // HOUR_MS
        recs = [self._rec(cur, 0.0002), self._rec(cur - 2, 0.0003)]
        s = hourly_funding_series(recs, NOW_MS, hours=12)
        self.assertEqual(s[11], 0.0002)
        self.assertIsNone(s[10])          # cur-1 hour missing -> dark
        self.assertEqual(s[9], 0.0003)
        self.assertTrue(all(v is None for v in s[:9]))

    def test_same_hour_dedup_keeps_latest(self):
        cur = NOW_MS // HOUR_MS
        recs = [self._rec(cur, 0.0001, extra_ms=1000),
                self._rec(cur, 0.0009, extra_ms=5000)]   # later print wins
        s = hourly_funding_series(recs, NOW_MS, hours=12)
        self.assertEqual(s[-1], 0.0009)

    def test_malformed_records_skipped(self):
        cur = NOW_MS // HOUR_MS
        recs = [self._rec(cur, 0.0005), {"rate": "x"}, None,
                {"timestamp_ms": "nan", "rate": 1.0}, {"rate": 1.0}]
        s = hourly_funding_series(recs, NOW_MS, hours=12)
        self.assertEqual(s[-1], 0.0005)

    def test_empty_all_none(self):
        s = hourly_funding_series([], NOW_MS, hours=12)
        self.assertEqual(s, [None] * 12)


class TestBasisZSeries(unittest.TestCase):
    def _line(self, ts, z, symbol="ORCL-USD"):
        return json.dumps({"ts": ts, "symbol": symbol, "z": z,
                           "basis_bps": 12.3, "perp_mark": 143.7})

    def test_trailing_tail_sorted_by_ts(self):
        lines = [self._line(300, 1.3), self._line(100, 1.1),
                 self._line(200, None)]
        zs = basis_z_series(lines, "ORCL-USD", tail=10)
        self.assertEqual(zs, [1.1, None, 1.3])     # ts-sorted, None kept

    def test_bad_line_skipped_never_fatal(self):
        lines = [self._line(100, 1.1), "{not json", "", self._line(200, 1.2)]
        zs = basis_z_series(lines, "ORCL-USD", tail=10)
        self.assertEqual(zs, [1.1, 1.2])

    def test_symbol_filter_and_tail_cut(self):
        lines = ([self._line(t, 2.0, "META-USD") for t in (1, 2)]
                 + [self._line(100 + i, 0.1 * i) for i in range(15)])
        zs = basis_z_series(lines, "ORCL-USD", tail=10)
        self.assertEqual(len(zs), 10)
        self.assertAlmostEqual(zs[-1], 1.4)

    def test_empty(self):
        self.assertEqual(basis_z_series([], "ORCL-USD"), [])


class TestLatestMarkAndSpread(unittest.TestCase):
    def test_latest_perp_mark(self):
        lines = [json.dumps({"ts": 1, "symbol": "META-USD",
                             "perp_mark": 700.5}),
                 json.dumps({"ts": 2, "symbol": "META-USD",
                             "perp_mark": 701.25})]
        self.assertEqual(latest_perp_mark(lines, "META-USD"), 701.25)
        self.assertIsNone(latest_perp_mark(lines, "ORCL-USD"))
        self.assertIsNone(latest_perp_mark([], "META-USD"))

    def test_latest_spread_newest_print_is_the_opinion(self):
        lines = [json.dumps({"ts": 1, "symbol": "ORCL-USD",
                             "spread_bps": 3.2}),
                 json.dumps({"ts": 2, "symbol": "ORCL-USD",
                             "spread_bps": None})]
        self.assertIsNone(latest_spread_bps(lines, "ORCL-USD"))
        lines2 = [json.dumps({"ts": 1, "symbol": "ORCL-USD",
                              "spread_bps": 3.2})]
        self.assertEqual(latest_spread_bps(lines2, "ORCL-USD"), 3.2)


class TestDirectionFor(unittest.TestCase):
    def test_orcl_always_long(self):
        self.assertEqual(direction_for("orcl"), "long")
        self.assertEqual(direction_for("orcl", -0.5), "long")

    def test_meta_receiving_side(self):
        self.assertEqual(direction_for("meta", 0.0001), "short")
        self.assertEqual(direction_for("meta", 0.0), "long")
        self.assertEqual(direction_for("meta", -0.0001), "long")
        self.assertEqual(direction_for("meta", None), "long")

    def test_unknown_strategy_raises(self):
        with self.assertRaises(ValueError):
            direction_for("tsla", 1.0)


class TestPnlBps(unittest.TestCase):
    def test_long(self):
        self.assertAlmostEqual(pnl_bps("long", 100.0, 101.0), 100.0)
        self.assertAlmostEqual(pnl_bps("long", 100.0, 99.0), -100.0)

    def test_short(self):
        self.assertAlmostEqual(pnl_bps("short", 100.0, 101.0), -100.0)
        self.assertAlmostEqual(pnl_bps("short", 100.0, 99.0), 100.0)

    def test_dark_prices_none(self):
        self.assertIsNone(pnl_bps("long", None, 101.0))
        self.assertIsNone(pnl_bps("long", 100.0, None))
        self.assertIsNone(pnl_bps("long", 0.0, 101.0))
        self.assertIsNone(pnl_bps("long", 100.0, -5.0))


class TestFundingBpsAccrued(unittest.TestCase):
    def _recs(self, start_bucket, hours, rate):
        return [{"symbol": "X-USD", "rate": rate,
                 "timestamp_ms": (start_bucket + i) * HOUR_MS + 1000,
                 "source": "t"} for i in range(hours)]

    def test_short_receives_positive_rate(self):
        b0 = NOW_MS // HOUR_MS - 10
        opened = b0 * HOUR_MS
        closed = (b0 + 3) * HOUR_MS        # exactly 3 full hours held
        recs = self._recs(b0, 3, 0.0001)
        self.assertAlmostEqual(
            funding_bps_accrued("short", recs, opened, closed), 3.0)

    def test_long_pays_positive_rate(self):
        b0 = NOW_MS // HOUR_MS - 10
        opened = b0 * HOUR_MS
        closed = (b0 + 3) * HOUR_MS
        recs = self._recs(b0, 3, 0.0001)
        self.assertAlmostEqual(
            funding_bps_accrued("long", recs, opened, closed), -3.0)

    def test_only_full_hours_count(self):
        b0 = NOW_MS // HOUR_MS - 10
        opened = b0 * HOUR_MS + 1_800_000          # :30 into hour b0
        closed = (b0 + 3) * HOUR_MS + 1_800_000    # :30 into hour b0+3
        recs = self._recs(b0, 4, 0.0001)
        # full buckets strictly inside: b0+1, b0+2 -> 2 hours
        self.assertAlmostEqual(
            funding_bps_accrued("short", recs, opened, closed), 2.0)

    def test_dark_hours_contribute_nothing(self):
        b0 = NOW_MS // HOUR_MS - 10
        opened = b0 * HOUR_MS
        closed = (b0 + 3) * HOUR_MS
        recs = self._recs(b0, 1, 0.0001)   # only hour b0 has a print
        self.assertAlmostEqual(
            funding_bps_accrued("short", recs, opened, closed), 1.0)

    def test_zero_or_inverted_window(self):
        self.assertEqual(funding_bps_accrued("short", [], 1000, 1000), 0.0)
        self.assertEqual(funding_bps_accrued("short", [], 2000, 1000), 0.0)
        self.assertEqual(funding_bps_accrued("short", [], None, 1000), 0.0)


class TestShadowStateMachine(unittest.TestCase):
    def test_open_close_round_trip(self):
        now = 1_800_000_000.0
        row = open_shadow("meta", "META-USD", "short", 700.0, now,
                          {"legs": {"zero_streak": "pass"}})
        self.assertEqual(row["event"], "stock_carry_shadow_open")
        self.assertEqual(row["opened_ms"], int(now * 1000))
        st = with_open({"open": {}}, row)
        self.assertEqual(open_slot(st, "meta")["id"], row["id"])
        self.assertIsNone(open_slot(st, "orcl"))

        close = close_shadow(row, "funding_zero_2x", 699.0,
                             pnl_bps("short", 700.0, 699.0), 0.42, now + 7200)
        self.assertEqual(close["event"], "stock_carry_shadow_close")
        self.assertEqual(close["reason"], "funding_zero_2x")
        self.assertAlmostEqual(close["pnl_bps"], 14.2857, places=3)
        self.assertAlmostEqual(close["net_bps"],
                               close["pnl_bps"] + 0.42, places=3)
        self.assertEqual(close["age_hours"], 2.0)
        st = with_closed(st, "meta")
        self.assertIsNone(open_slot(st, "meta"))

    def test_close_with_dark_pnl_carries_none(self):
        row = open_shadow("orcl", "ORCL-USD", "long", 143.0, 1.0)
        close = close_shadow(row, "funding_returned_zero", 0.0,
                             None, 0.0, 2.0)
        self.assertIsNone(close["pnl_bps"])
        self.assertIsNone(close["net_bps"])

    def test_open_slot_rejects_non_dict(self):
        self.assertIsNone(open_slot({"open": {"orcl": "junk"}}, "orcl"))
        self.assertIsNone(open_slot({}, "orcl"))


class TestPersistence(unittest.TestCase):
    def test_atomic_write_roundtrip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "state.json")
            payload = {"open": {"orcl": {"id": "x", "strategy": "orcl"}},
                       "updated_ts": 123}
            atomic_write_json(p, payload)
            self.assertFalse(os.path.exists(p + ".tmp"))
            with open(p) as f:
                self.assertEqual(json.load(f), payload)

    def test_read_state_tolerant(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "state.json")
            self.assertEqual(read_state(p), {"open": {}})      # missing
            with open(p, "w") as f:
                f.write("{corrupt")
            self.assertEqual(read_state(p), {"open": {}})      # corrupt
            with open(p, "w") as f:
                json.dump(["not", "a", "dict"], f)
            self.assertEqual(read_state(p), {"open": {}})      # bad shape
            with open(p, "w") as f:
                json.dump({"open": {"meta": {"id": "m"}}}, f)
            self.assertEqual(read_state(p)["open"]["meta"]["id"], "m")

    def test_append_jsonl_roundtrip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "ledger.jsonl")
            append_jsonl(p, {"event": "a", "n": 1})
            append_jsonl(p, {"event": "b", "n": 2})
            with open(p) as f:
                rows = [json.loads(l) for l in f]
            self.assertEqual([r["event"] for r in rows], ["a", "b"])


class TestNewestBarAge(unittest.TestCase):
    def test_age_and_dark(self):
        bars = _bars(3)
        self.assertEqual(newest_bar_age_ms(bars, NOW_MS),
                         NOW_MS - bars[-1][0])
        self.assertIsNone(newest_bar_age_ms([], NOW_MS))


if __name__ == "__main__":
    unittest.main()
