"""Catalyst-calendar pins: load validation with one-bad-line tolerance,
window edges (exactly +-72h inclusive), symbol vs cluster matching,
confidence ranking, fail-open paths, injected clock."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelligence.catalyst_calendar import (  # noqa: E402
    CATALYST_EVENT_TYPES, CatalystCalendar, CatalystEvent, load_catalysts)

H = 3600.0
T0 = 1_800_000_000.0


def _row(**kw):
    r = {"symbol_or_cluster": "UNI-USD", "event_type": "governance_vote",
         "ts_utc": T0, "source": "test", "confidence": 0.9, "note": "n"}
    r.update(kw)
    return r


# ── load_catalysts validation ────────────────────────────────────────────────

def test_load_valid_rows_epoch_and_iso():
    evs, errs = load_catalysts([_row(), _row(ts_utc="2026-09-18T12:00:00Z")])
    assert errs == []
    assert len(evs) == 2
    assert evs[0].ts_utc == T0
    # ISO parsed to epoch (tz-aware Z suffix)
    from datetime import datetime, timezone
    expect = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc).timestamp()
    assert evs[1].ts_utc == expect


def test_load_one_bad_line_tolerance():
    rows = [_row(), {"garbage": True}, _row(ts_utc="not-a-date"),
            _row(symbol_or_cluster=""), _row(event_type=None),
            "not a dict", _row(symbol_or_cluster="TIA-USD",
                               event_type="token_unlock", ts_utc=T0 + H)]
    evs, errs = load_catalysts(rows)
    assert len(evs) == 2            # row 0 and row 6 survive
    assert len(errs) == 5           # every bad row logged, never raised
    assert evs[1].symbol_or_cluster == "TIA-USD"


def test_load_defaults_and_confidence_clamp():
    evs, _ = load_catalysts([{"symbol_or_cluster": "ONDO-USD",
                              "event_type": "exchange_listing", "ts_utc": T0}])
    e = evs[0]
    assert e.source == "unknown" and e.confidence == 0.5 and e.note == ""
    evs2, _ = load_catalysts([_row(confidence=7.5), _row(confidence=-2.0),
                              _row(confidence="junk")])
    assert evs2[0].confidence == 1.0
    assert evs2[1].confidence == 0.0
    assert evs2[2].confidence == 0.5


def test_load_fail_open():
    assert load_catalysts(None) == ([], [])
    assert load_catalysts([]) == ([], [])
    assert CATALYST_EVENT_TYPES == frozenset(
        {"governance_vote", "token_unlock", "protocol_launch",
         "exchange_listing", "ai_news"})


# ── active_catalysts window edges ────────────────────────────────────────────

def _cal(rows, **kw):
    evs, errs = load_catalysts(rows)
    assert errs == []
    return CatalystCalendar(evs, **kw)


def test_active_window_edges_inclusive():
    cal = _cal([_row()])   # event at T0, default window +-72h
    assert cal.active_catalysts(T0 - 72 * H) != []     # exactly -72h
    assert cal.active_catalysts(T0 + 72 * H) != []     # exactly +72h
    assert cal.active_catalysts(T0 - 72 * H - 1) == []
    assert cal.active_catalysts(T0 + 72 * H + 1) == []
    assert cal.active_catalysts(T0) != []


def test_active_window_hours_override():
    cal = _cal([_row()])
    assert cal.active_catalysts(T0 + 24 * H, window_hours=24) != []
    assert cal.active_catalysts(T0 + 24 * H + 1, window_hours=24) == []


# ── catalyst_for / is_opportunity_window ─────────────────────────────────────

def test_catalyst_for_direct_symbol():
    cal = _cal([_row()])
    e = cal.catalyst_for("UNI-USD", T0)
    assert e is not None and e.symbol_or_cluster == "UNI-USD"
    assert cal.catalyst_for("CRV-USD", T0) is None   # same cluster, not covered


def test_catalyst_for_cluster_event_covers_members():
    cal = _cal([_row(symbol_or_cluster="AI", event_type="ai_news")])
    assert cal.catalyst_for("TAO-USD", T0) is not None
    assert cal.catalyst_for("RENDER-USD", T0) is not None
    assert cal.catalyst_for("UNI-USD", T0) is None
    assert cal.is_opportunity_window("FET-USD", T0) is True
    assert cal.is_opportunity_window("BTC-USD", T0) is False


def test_catalyst_for_ranking_confidence_then_soonest():
    rows = [_row(confidence=0.5, ts_utc=T0 + 10 * H),
            _row(confidence=0.9, ts_utc=T0 + 20 * H),
            _row(confidence=0.9, ts_utc=T0 + 5 * H)]
    cal = _cal(rows)
    e = cal.catalyst_for("UNI-USD", T0)
    assert e.ts_utc == T0 + 5 * H      # highest conf, tie -> soonest
    rows2 = [_row(confidence=0.4, ts_utc=T0 + 5 * H),
             _row(confidence=0.9, ts_utc=T0 + 30 * H)]
    e2 = _cal(rows2).catalyst_for("UNI-USD", T0)
    assert e2.ts_utc == T0 + 30 * H    # confidence outranks sooner


def test_catalyst_for_outside_window_and_fail_open():
    cal = _cal([_row()])
    assert cal.catalyst_for("UNI-USD", T0 + 73 * H) is None
    assert cal.catalyst_for("", T0) is None
    assert cal.catalyst_for(None, T0) is None
    assert cal.is_opportunity_window("UNI-USD", T0 - 73 * H) is False


def test_calendar_injected_clock_and_custom_window():
    clock = {"t": T0}
    cal = _cal([_row(ts_utc=T0 + 10 * H)], now_fn=lambda: clock["t"],
               window_hours=12.0)
    assert cal.is_opportunity_window("UNI-USD") is True
    clock["t"] = T0 + 23 * H
    assert cal.is_opportunity_window("UNI-USD") is False


def test_calendar_empty_events_fail_open():
    cal = CatalystCalendar([], now_fn=lambda: T0)
    assert cal.active_catalysts() == []
    assert cal.catalyst_for("UNI-USD") is None
    assert cal.is_opportunity_window("UNI-USD") is False
    cal2 = CatalystCalendar(None)
    assert cal2.active_catalysts(T0) == []


def test_seed_example_file_loads_clean():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "logs", "catalyst_events.SEED.example.json")
    with open(path) as f:
        rows = [__import__("json").loads(line) for line in f if line.strip()]
    evs, errs = load_catalysts(rows)
    assert errs == [] and len(evs) == 3
    assert all(e.source == "EXAMPLE" for e in evs)
