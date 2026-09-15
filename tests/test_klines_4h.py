import time

from data.candle_buffer import Candle
from data.klines_4h import (
    closed_only, closes_of, parse_bybit_240, parse_sodex_4h, realized_vol_rank,
)

H = 3_600_000
BAR = 4 * H
T0 = 1_758_000_000_000  # fixed epoch ms, 4h-aligned


def _bar(i: int, o=100.0, h=101.0, l=99.0, c=100.5, v=10.0) -> Candle:
    t = T0 + i * BAR
    return Candle(open_time=t, open=o, high=h, low=l, close=c, volume=v,
                  close_time=t + BAR - 1)


def _bars(n: int, close0: float = 100.0, drift: float = 0.1) -> list[Candle]:
    out = []
    px = close0
    for i in range(n):
        c = px + drift
        out.append(_bar(i, o=px, h=max(px, c) * 1.005, l=min(px, c) * 0.995, c=c))
        px = c
    return out


def test_closed_only_drops_forming_tail():
    bars = _bars(5)
    now = T0 + 5 * BAR - 1  # last bar still forming
    closed = closed_only(bars, now_ms=now)
    assert len(closed) == 4
    # after the bar closes it is included
    assert len(closed_only(bars, now_ms=T0 + 5 * BAR)) == 5


def test_parse_sodex_4h_newest_first_strings_and_bad_line():
    payload = {"code": 0, "data": [
        {"t": str(T0 + BAR), "o": "100", "h": "101", "l": "99", "c": "100.5",
         "v": "7", "q": "0", "n": 3},
        {"t": str(T0), "o": "99", "h": "100", "l": "98", "c": "99.5",
         "v": "5", "q": "0", "n": 2},
        {"t": "garbage"},
    ]}
    bars = parse_sodex_4h(payload, now_ms=T0 + 2 * BAR)
    assert [b.open_time for b in bars] == [T0, T0 + BAR]
    assert bars[0].close == 99.5 and bars[1].volume == 7.0
    # forming tail excluded
    assert parse_sodex_4h(payload, now_ms=T0 + BAR + 1)[-1].open_time == T0


def test_parse_sodex_4h_empty_and_missing_data():
    assert parse_sodex_4h({}, now_ms=T0) == []
    assert parse_sodex_4h({"data": None}, now_ms=T0) == []


def test_parse_bybit_240_shape():
    r0 = [str(T0), "99", "100", "98", "99.5", "5", "0"]
    r1 = [str(T0 + BAR), "100", "101", "99", "100.5", "7", "0"]
    payload = {"retCode": 0, "result": {"list": [r1, r0, ["bad"]]}}
    bars = parse_bybit_240(payload, now_ms=T0 + 2 * BAR)
    assert [b.open_time for b in bars] == [T0, T0 + BAR]
    assert bars[1].close == 100.5
    assert parse_bybit_240({"retCode": 1, "result": {}}, now_ms=T0) == []


def test_closes_of():
    bars = _bars(3)
    assert closes_of(bars) == [b.close for b in bars]


def test_vol_rank_abstains_when_thin():
    assert realized_vol_rank(_bars(49)) is None


def test_vol_rank_100_on_monotone_vol_expansion():
    # quiet bars then a final hv_window of wide bars → current HV top-ranked
    quiet = [_bar(i, o=100, h=100.1, l=99.9, c=100.0) for i in range(100)]
    wide = [_bar(100 + i, o=100, h=104, l=96, c=100.0) for i in range(20)]
    rank = realized_vol_rank(quiet + wide)
    assert rank is not None and rank > 90.0


def test_vol_rank_low_on_vol_compression():
    wide = [_bar(i, o=100, h=104, l=96, c=100.0) for i in range(100)]
    quiet = [_bar(100 + i, o=100, h=100.1, l=99.9, c=100.0) for i in range(20)]
    rank = realized_vol_rank(wide + quiet)
    assert rank is not None and rank < 15.0


def test_vol_rank_rejects_degenerate_bars():
    bars = _bars(120)
    bars[50] = _bar(50, o=100, h=90, l=99, c=99.5)  # high < low
    assert realized_vol_rank(bars) is None


def test_vol_rank_constant_window_abstains():
    # dead-flat tape → every HV identical → ties would rank 100; must abstain
    flat = [_bar(i, o=100, h=100.1, l=99.9, c=100.0) for i in range(140)]
    assert realized_vol_rank(flat) is None
