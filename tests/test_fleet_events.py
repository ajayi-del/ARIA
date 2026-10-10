"""
tests/test_fleet_events.py — pins for the fleet event plane (Three-Layer
Network Phase 1, 2026-10-10).

Doctrine under test:
  - FleetEvent is a frozen fact: immutable once emitted.
  - make_event is the validated constructor: degenerate input -> None
    (the splice drops the row, never the loop); garbage numerics coerce
    to 0.0, never raise.
  - FleetEventBuffer: deque(maxlen) bound; window reads are
    now-injected (buffer never reads a clock); degenerate window/now
    fail closed to []; velocity is per-window event COUNT; latest() is
    newest-match; record() never raises and drops foreign input.

No network, no Settings() real env — repo idiom.
"""
import dataclasses

import pytest

from intelligence.fleet_events import (
    DEFAULT_BUFFER_MAXLEN,
    EVENT_TYPES,
    FleetEvent,
    FleetEventBuffer,
    make_event,
)

T0 = 1_760_000_000.0   # fixed epoch anchor


def ev(event_type="fill", symbol="BTC-USD", direction="long",
       price=100.0, ts=T0, **kw):
    return make_event(event_type, symbol, direction, price, ts, **kw)


# ── frozen fact + validated constructor ──────────────────────────────────────

class TestEventFacts:
    def test_frozen_immutable(self):
        e = ev()
        assert isinstance(e, FleetEvent)
        with pytest.raises(dataclasses.FrozenInstanceError):
            e.price = 101.0
        with pytest.raises(dataclasses.FrozenInstanceError):
            e.event_type = "tp_hit"

    def test_make_event_happy_path_coerces(self):
        e = make_event(" FILL ", "BTC-USD", "LONG", "100.5", str(T0),
                       margin_usd="2.5", leverage="8", fill_velocity="3")
        assert e.event_type == "fill"
        assert e.direction == "long"
        assert e.price == 100.5 and e.timestamp == T0
        assert (e.margin_usd, e.leverage, e.fill_velocity) == (2.5, 8.0, 3.0)

    def test_make_event_rejects_degenerate(self):
        # unknown event class
        assert make_event("melt", "BTC", "long", 1.0, T0) is None
        # direction outside long/short
        assert make_event("fill", "BTC", "flat", 1.0, T0) is None
        # empty / None-ish symbol
        assert make_event("fill", "", "long", 1.0, T0) is None
        assert make_event("fill", None, "long", 1.0, T0) is None
        assert make_event("fill", "None", "long", 1.0, T0) is None
        # price / timestamp must be positive finite floats
        assert make_event("fill", "BTC", "long", 0.0, T0) is None
        assert make_event("fill", "BTC", "long", -5.0, T0) is None
        assert make_event("fill", "BTC", "long", "abc", T0) is None
        assert make_event("fill", "BTC", "long", float("nan"), T0) is None
        assert make_event("fill", "BTC", "long", 1.0, 0.0) is None
        assert make_event("fill", "BTC", "long", 1.0, "junk") is None

    def test_make_event_garbage_numerics_coerce_to_zero(self):
        e = make_event("fill", "BTC", "long", 1.0, T0,
                       margin_usd="junk", leverage=None,
                       fill_velocity=float("nan"))
        assert e.margin_usd == 0.0 and e.leverage == 0.0
        assert e.fill_velocity == 0.0
        # negative optionals floor at 0
        e2 = make_event("fill", "BTC", "long", 1.0, T0,
                        margin_usd=-3.0, leverage=-1.0)
        assert e2.margin_usd == 0.0 and e2.leverage == 0.0

    def test_event_types_closed_set(self):
        assert EVENT_TYPES == frozenset(
            {"fill", "stop_hit", "tp_hit", "eviction"})


# ── buffer: bounding + window reads ──────────────────────────────────────────

class TestBuffer:
    def test_record_drops_foreign_never_raises(self):
        buf = FleetEventBuffer()
        assert buf.record(None) is False
        assert buf.record("not-an-event") is False
        assert buf.record({"event_type": "fill"}) is False
        assert len(buf) == 0
        assert buf.record(ev()) is True
        assert len(buf) == 1

    def test_maxlen_bound(self):
        buf = FleetEventBuffer(maxlen=5)
        for i in range(12):
            buf.record(ev(symbol="BTC", ts=T0 + i))
        assert len(buf) == 5
        # oldest evicted: newest 5 timestamps survive
        snap = buf.snapshot(10_000.0, T0 + 11)
        assert [e.timestamp for e in snap] == [T0 + i for i in range(7, 12)]

    def test_maxlen_garbage_falls_back(self):
        buf = FleetEventBuffer(maxlen="junk")
        assert buf._events.maxlen == DEFAULT_BUFFER_MAXLEN
        buf2 = FleetEventBuffer(maxlen=0)
        assert buf2._events.maxlen == 1

    def test_snapshot_window_math(self):
        buf = FleetEventBuffer()
        # events at T0+10, +20, +30; window 15s at now=T0+30 covers
        # (T0+15, T0+30] -> only +20, +30. Cutoff exclusive, now inclusive.
        for dt in (10, 20, 30):
            buf.record(ev(ts=T0 + dt))
        snap = buf.snapshot(15.0, T0 + 30)
        assert [e.timestamp for e in snap] == [T0 + 20, T0 + 30]
        # cutoff (now - window) is EXCLUSIVE: T0+10 falls out at window 20
        assert [e.timestamp for e in buf.snapshot(20.0, T0 + 30)] == \
            [T0 + 20, T0 + 30]
        # events in the future of `now` excluded; older ones survive
        assert [e.timestamp for e in buf.snapshot(100.0, T0 + 15)] == [T0 + 10]

    def test_snapshot_degenerate_fails_closed(self):
        buf = FleetEventBuffer()
        buf.record(ev())
        assert buf.snapshot(0.0, T0) == []
        assert buf.snapshot(-5.0, T0) == []
        assert buf.snapshot(10.0, 0.0) == []
        assert buf.snapshot("junk", T0) == []
        assert buf.snapshot(10.0, None) == []

    def test_snapshot_filters(self):
        buf = FleetEventBuffer()
        buf.record(ev(symbol="BTC", ts=T0 + 1))
        buf.record(ev(symbol="ETH", ts=T0 + 2))
        buf.record(ev(event_type="stop_hit", symbol="BTC", ts=T0 + 3))
        assert [e.symbol for e in buf.snapshot(100.0, T0 + 3,
                                               symbol="BTC")] == ["BTC", "BTC"]
        fills = buf.snapshot(100.0, T0 + 3, event_types=("fill",))
        assert all(e.event_type == "fill" for e in fills) and len(fills) == 2
        both = buf.snapshot(100.0, T0 + 3, symbol="BTC",
                            event_types=("stop_hit",))
        assert len(both) == 1 and both[0].event_type == "stop_hit"

    def test_velocity_counts_per_window(self):
        buf = FleetEventBuffer()
        for dt in (10, 20, 30, 40):
            buf.record(ev(symbol="BTC", ts=T0 + dt))
        buf.record(ev(event_type="stop_hit", symbol="BTC", ts=T0 + 41))
        # fills default: 4 in a wide window; 2 in the last 15s (30, 40)
        assert buf.velocity(100.0, T0 + 41) == 4
        assert buf.velocity(15.0, T0 + 41) == 2
        assert buf.velocity(15.0, T0 + 41, event_types=("stop_hit",)) == 1
        assert buf.velocity(15.0, T0 + 41, symbol="ETH") == 0

    def test_velocity_by_symbol(self):
        buf = FleetEventBuffer()
        for sym, dt in (("BTC", 1), ("BTC", 2), ("ETH", 3), ("SOL", 60)):
            buf.record(ev(symbol=sym, ts=T0 + dt))
        assert buf.velocity_by_symbol(100.0, T0 + 60) == \
            {"BTC": 2, "ETH": 1, "SOL": 1}
        assert buf.velocity_by_symbol(10.0, T0 + 60) == {"SOL": 1}

    def test_latest_newest_match(self):
        buf = FleetEventBuffer()
        buf.record(ev(symbol="BTC", ts=T0 + 1))
        buf.record(ev(symbol="ETH", ts=T0 + 2))
        buf.record(ev(event_type="tp_hit", symbol="BTC", ts=T0 + 3))
        assert buf.latest().event_type == "tp_hit"
        assert buf.latest(symbol="ETH").timestamp == T0 + 2
        assert buf.latest(event_types=("fill",)).symbol == "ETH"
        assert buf.latest(symbol="DOGE") is None
        assert buf.latest(symbol="BTC", event_types=("stop_hit",)) is None

    def test_reads_never_raise_on_degenerate_args(self):
        buf = FleetEventBuffer()
        assert buf.snapshot(None, None) == []
        assert buf.velocity(None, None) == 0
        assert buf.velocity_by_symbol(None, None) == {}
