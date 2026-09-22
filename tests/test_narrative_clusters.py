"""Narrative-propagation layer pins: cluster lookup, originator threshold
edges, decay table, late-node filter edges, retrace exit edge, UTC day
boundaries, tracker expiry/dedup, fail-open paths."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelligence.narrative_clusters import (  # noqa: E402
    DEFAULT_CLUSTERS, DEFAULT_DECAY_TABLE, OriginatorEvent, NarrativeTracker,
    cluster_of, detect_originator, propagation_day, propagation_decay,
    propagation_nodes, originator_retraced)

DAY = 86400.0
T0 = 20833.0 * DAY  # midnight-aligned epoch (day edges need alignment)


# ── Cluster registry ─────────────────────────────────────────────────────────

def test_cluster_membership_default():
    assert cluster_of("UNI-USD") == "DeFi_GOVERNANCE"
    assert cluster_of("OP-USD") == "L2_SHADOW"
    assert cluster_of("PYTH-USD") == "COSMOS"
    assert cluster_of("TAO-USD") == "AI"
    assert cluster_of("1000PEPE-USD") == "MEME"
    assert cluster_of("ONDO-USD") == "RWA"


def test_cluster_unclustered_and_blank_fail_open():
    assert cluster_of("BTC-USD") is None
    assert cluster_of("") is None
    assert cluster_of(None) is None


def test_clusters_knob_injectable():
    custom = {"X": ["AAA-USD"]}
    assert cluster_of("AAA-USD", custom) == "X"
    assert cluster_of("UNI-USD", custom) is None


# ── detect_originator ────────────────────────────────────────────────────────

def test_originator_threshold_edges():
    moves = {"UNI-USD": 15.0, "OP-USD": 14.99, "TAO-USD": -15.0, "SEI-USD": 15.01}
    evs = detect_originator(moves)
    syms = {e.symbol for e in evs}
    assert "UNI-USD" in syms      # exactly 15.0 qualifies (>=)
    assert "TAO-USD" in syms      # exactly -15.0 qualifies
    assert "SEI-USD" in syms
    assert "OP-USD" not in syms   # 14.99 below threshold


def test_originator_event_shape():
    evs = detect_originator({"UNI-USD": 17.5}, detected_ts=T0)
    assert len(evs) == 1
    e = evs[0]
    assert e.symbol == "UNI-USD" and e.cluster == "DeFi_GOVERNANCE"
    assert e.move_pct == 17.5 and e.detected_ts == T0


def test_originator_unclustered_skipped_and_custom_min():
    assert detect_originator({"BTC-USD": 25.0}) == []
    evs = detect_originator({"UNI-USD": 10.0}, min_move_pct=10.0)
    assert len(evs) == 1


def test_originator_fail_open_and_bad_values():
    assert detect_originator(None) == []
    assert detect_originator({}) == []
    evs = detect_originator({"UNI-USD": "junk", "OP-USD": None, "TAO-USD": 20.0})
    assert [e.symbol for e in evs] == ["TAO-USD"]


# ── propagation_day / decay ──────────────────────────────────────────────────

def test_propagation_day_boundaries_utc():
    d0 = T0
    assert propagation_day(d0, d0 + 1) == 0
    assert propagation_day(d0, d0 + DAY - 1) == 0
    assert propagation_day(d0, d0 + DAY) == 1
    assert propagation_day(d0, d0 + 3 * DAY) == 3
    assert propagation_day(d0, d0 + 30 * DAY) == 30


def test_propagation_day_midnight_edge():
    # originator at 23:59 UTC, read at 00:01 UTC next day -> day 1
    midnight = (T0 // DAY) * DAY
    originator = midnight - 60.0       # 23:59 previous day
    now = midnight + 60.0              # 00:01 current day
    assert propagation_day(originator, now) == 1
    assert propagation_day(now, originator) == 0   # negative fails open to 0


def test_propagation_day_bad_input_fails_open():
    assert propagation_day(None, T0) == 0
    assert propagation_day(T0, "junk") == 0


def test_decay_table_default_and_edges():
    assert propagation_decay(0) == 1.00
    assert propagation_decay(1) == 0.55
    assert propagation_decay(2) == 0.35
    assert propagation_decay(3) == 0.20
    assert propagation_decay(4) == 0.0
    assert propagation_decay(99) == 0.0
    assert propagation_decay(-1) == 0.0
    assert propagation_decay("x") == 0.0


def test_decay_table_injectable():
    custom = [1.0, 0.9]
    assert propagation_decay(1, custom) == 0.9
    assert propagation_decay(2, custom) == 0.0
    assert DEFAULT_DECAY_TABLE == [1.00, 0.55, 0.35, 0.20]


# ── propagation_nodes (late-entry filter) ────────────────────────────────────

def test_nodes_filter_at_exactly_8pct():
    moves = {"CRV-USD": 8.0, "SNX-USD": 8.01, "BAL-USD": -8.01}
    nodes = propagation_nodes("DeFi_GOVERNANCE", moves)
    assert "CRV-USD" in nodes       # exactly 8.0 not yet moved (>8 required)
    assert "SNX-USD" not in nodes
    assert "BAL-USD" not in nodes   # absolute value: -8.01 is moved
    assert "UNI-USD" in nodes
    assert "AAVE-USD" in nodes and "ENA-USD" in nodes


def test_nodes_accepts_set_form_and_excludes_originator():
    nodes = propagation_nodes("L2_SHADOW", {"OP-USD"}, exclude={"OP-USD"})
    assert nodes == ["ARB-USD"]
    nodes2 = propagation_nodes("L2_SHADOW", {"ARB-USD"})
    assert nodes2 == ["OP-USD"]


def test_nodes_unknown_cluster_and_empty_inputs():
    assert propagation_nodes("NOPE", {}) == []
    assert propagation_nodes("AI", None) == ["TAO-USD", "FET-USD", "RENDER-USD"]
    # bad move values tolerated
    nodes = propagation_nodes("AI", {"TAO-USD": "junk"})
    assert "TAO-USD" in nodes


# ── originator_retraced ──────────────────────────────────────────────────────

def test_retraced_edges_at_50pct():
    # +20% move; 10.0% back from peak = exactly 50% -> NOT retraced
    assert originator_retraced(20.0, 10.0) is False
    assert originator_retraced(20.0, 10.01) is True
    assert originator_retraced(-20.0, 10.01) is True   # short originator
    assert originator_retraced(-20.0, -10.01) is True  # sign-insensitive


def test_retraced_fail_open():
    assert originator_retraced(0.0, 5.0) is False
    assert originator_retraced(None, 5.0) is False
    assert originator_retraced(20.0, "junk") is False
    assert originator_retraced(20.0, 11.0, threshold=0.5) is True
    assert originator_retraced(20.0, 11.0, threshold=0.6) is False


# ── NarrativeTracker ─────────────────────────────────────────────────────────

def test_tracker_emits_new_events_once():
    t = NarrativeTracker(now_fn=lambda: T0)
    fresh = t.on_day_moves({"UNI-USD": 18.0}, T0)
    assert len(fresh) == 1 and fresh[0].symbol == "UNI-USD"
    again = t.on_day_moves({"UNI-USD": 22.0}, T0 + 3600)
    assert again == []   # unexpired originator does not re-fire
    other = t.on_day_moves({"TAO-USD": -16.0}, T0 + 3600)
    assert [e.symbol for e in other] == ["TAO-USD"]


def test_tracker_live_nodes_carry_day_decay_and_filter():
    t = NarrativeTracker(now_fn=lambda: T0)
    t.on_day_moves({"UNI-USD": 20.0, "CRV-USD": 9.0}, T0)
    nodes = t.live_nodes(T0)
    syms = {n["symbol"] for n in nodes}
    assert "UNI-USD" not in syms       # originator excluded from own nodes
    assert "CRV-USD" not in syms       # already moved 9% > 8% -> late
    assert "SNX-USD" in syms and "AAVE-USD" in syms
    n0 = next(n for n in nodes if n["symbol"] == "SNX-USD")
    assert n0["cluster"] == "DeFi_GOVERNANCE" and n0["originator"] == "UNI-USD"
    assert n0["day"] == 0 and n0["decay"] == 1.00
    # day 2 read: later moves no longer fed -> last_moves persist
    nodes2 = t.live_nodes(T0 + 2 * DAY)
    n2 = next(n for n in nodes2 if n["symbol"] == "SNX-USD")
    assert n2["day"] == 2 and n2["decay"] == 0.35
    # day 4 -> decay 0.0 (caller skips; doctrine: skip after Day 3)
    nodes4 = t.live_nodes(T0 + 4 * DAY)
    n4 = next(n for n in nodes4 if n["symbol"] == "SNX-USD")
    assert n4["day"] == 4 and n4["decay"] == 0.0


def test_tracker_expiry_after_7_days():
    t = NarrativeTracker(now_fn=lambda: T0)
    t.on_day_moves({"UNI-USD": 20.0}, T0)
    assert t.originator_events(T0 + 7 * DAY - 1) != []
    assert t.originator_events(T0 + 7 * DAY + 1) == []
    assert t.live_nodes(T0 + 7 * DAY + 1) == []
    # expired symbol may originate a fresh narrative
    fresh = t.on_day_moves({"UNI-USD": 30.0}, T0 + 8 * DAY)
    assert len(fresh) == 1


def test_tracker_fail_open_and_knobs():
    t = NarrativeTracker(now_fn=lambda: T0)
    assert t.on_day_moves(None, T0) == []
    assert t.on_day_moves({}, T0) == []
    assert t.live_nodes(T0) == []
    # injected clusters + thresholds
    t2 = NarrativeTracker(now_fn=lambda: T0,
                          clusters={"C": ["A-USD", "B-USD"]},
                          decay_table=[1.0, 0.1], min_move_pct=5.0,
                          max_moved_pct=1.0, expiry_s=3 * DAY)
    t2.on_day_moves({"A-USD": 5.0}, T0)
    nodes = t2.live_nodes(T0 + DAY)
    assert nodes == [{"symbol": "B-USD", "cluster": "C", "originator": "A-USD",
                      "day": 1, "decay": 0.1}]
    t3 = NarrativeTracker(now_fn=lambda: T0, clusters={"C": ["A-USD", "B-USD"]},
                          min_move_pct=5.0, expiry_s=3600.0)
    t3.on_day_moves({"A-USD": 6.0}, T0)
    assert t3.live_nodes(T0 + 3601.0) == []   # custom expiry


def test_tracker_now_fn_used_when_ts_omitted():
    clock = {"t": T0}
    t = NarrativeTracker(now_fn=lambda: clock["t"])
    t.on_day_moves({"UNI-USD": 20.0})
    clock["t"] = T0 + DAY
    nodes = t.live_nodes()
    assert all(n["day"] == 1 for n in nodes)
