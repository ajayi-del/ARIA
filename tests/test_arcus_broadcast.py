"""Pins for intelligence/arcus_broadcast.py — the Arcus → prop_outbox
signal bridge (Governor directive 2026-11-08).

Covers: kill-switch layering (ARCUS_BROADCAST_ENABLED × PROP_OUTBOX_ENABLED),
record shape vs the M4 outbox contract, native-TPSL inference from open
orders, synthesized-stop fallback, naked-signal ban, presence-transition
dedup, restart persistence, symbol filter, side flip, and fail-closed venue
errors. The venue client is a stub — the poller is observer-class and never
touches the trade path.
"""

import asyncio
import json
import os

import pytest

from intelligence.arcus_broadcast import (
    ArcusSignalPoller,
    build_signal_record,
    infer_tpsl,
    position_key,
    synthesize_stop,
)


def run(coro):
    # Fresh loop per call — the shared default loop is closed by async-using
    # sibling test files (the documented bitguard/arcus co-run pollution
    # class), leaving get_event_loop() raising RuntimeError.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class StubArcusClient:
    """Duck-typed venue client: canned positions/orders, call counting."""

    def __init__(self, positions=None, orders=None, raises=None):
        self._positions = list(positions or [])
        self._orders = list(orders or [])
        self._raises = raises
        self.position_calls = 0
        self.order_calls = 0

    async def get_positions(self):
        self.position_calls += 1
        if self._raises:
            raise self._raises
        return list(self._positions)

    async def get_open_orders(self):
        self.order_calls += 1
        return list(self._orders)


def _pos(symbol="NVDA-USD", side="long", entry=200.0):
    return {"symbol": symbol, "side": side, "size": 1.0, "qty": 1.0,
            "entry": entry, "avgEntryPrice": entry, "markPrice": entry,
            "upnl": 0.0, "leverage": 5, "venue": "arcus"}


def _sl_order(symbol, side, trigger):
    """Native positionTpsl SL leg shape (close side, reduce-only, trigger
    in stopPrice, protective bound in price)."""
    close_side = "SELL" if side == "long" else "BUY"
    bound = trigger * (0.95 if side == "long" else 1.05)
    return {"orderID": "1", "clOrdID": "scalmps", "symbol": symbol,
            "side": close_side, "price": bound, "quantity": 0.0,
            "stopPrice": trigger, "reduceOnly": True, "status": "OPEN",
            "venue": "arcus"}


def _tp_order(symbol, side, px):
    """Native TP leg: LIMIT, price == stopPrice (client doctrine)."""
    close_side = "SELL" if side == "long" else "BUY"
    return {"orderID": "2", "clOrdID": "scalmpt", "symbol": symbol,
            "side": close_side, "price": px, "quantity": 0.0,
            "stopPrice": px, "reduceOnly": True, "status": "OPEN",
            "venue": "arcus"}


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setenv("ARCUS_BROADCAST_ENABLED", "true")
    monkeypatch.setenv("PROP_OUTBOX_ENABLED", "true")
    monkeypatch.delenv("ARCUS_BROADCAST_SYMBOLS", raising=False)


@pytest.fixture
def paths(tmp_path):
    return (str(tmp_path / "prop_outbox.jsonl"),
            str(tmp_path / "arcus_broadcast_state.json"))


def _read_outbox(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


def _poller(client, paths, symbols=None):
    return ArcusSignalPoller(client=client, outbox_path=paths[0],
                             state_path=paths[1], symbols=symbols)


# ── Kill switches ────────────────────────────────────────────────────────────

class TestKillSwitches:
    def test_broadcast_flag_off_no_api_calls(self, monkeypatch, paths):
        monkeypatch.setenv("ARCUS_BROADCAST_ENABLED", "false")
        monkeypatch.setenv("PROP_OUTBOX_ENABLED", "true")
        client = StubArcusClient(positions=[_pos()])
        p = _poller(client, paths)
        out = run(p.poll_once())
        assert out["enabled"] is False
        assert client.position_calls == 0          # zero API cost while off
        assert _read_outbox(paths[0]) == []

    def test_outbox_master_gate_off_no_api_calls(self, monkeypatch, paths):
        monkeypatch.setenv("ARCUS_BROADCAST_ENABLED", "true")
        monkeypatch.setenv("PROP_OUTBOX_ENABLED", "false")
        client = StubArcusClient(positions=[_pos()])
        p = _poller(client, paths)
        out = run(p.poll_once())
        assert out["enabled"] is False
        assert client.position_calls == 0
        assert _read_outbox(paths[0]) == []

    def test_defaults_are_off(self, monkeypatch, paths):
        monkeypatch.delenv("ARCUS_BROADCAST_ENABLED", raising=False)
        monkeypatch.delenv("PROP_OUTBOX_ENABLED", raising=False)
        client = StubArcusClient(positions=[_pos()])
        p = _poller(client, paths)
        run(p.poll_once())
        assert client.position_calls == 0
        assert _read_outbox(paths[0]) == []


# ── Happy path / contract shape ──────────────────────────────────────────────

class TestEmission:
    def test_new_position_with_native_tpsl(self, armed, paths):
        client = StubArcusClient(
            positions=[_pos(entry=200.0)],
            orders=[_sl_order("NVDA-USD", "long", 198.0),
                    _tp_order("NVDA-USD", "long", 204.0)])
        p = _poller(client, paths)
        out = run(p.poll_once(now_ms=1_700_000_000_000))
        assert out["emitted"] == 1
        (rec,) = _read_outbox(paths[0])
        # M4 contract fields
        assert rec["id"] == "NVDA-USD_long_1700000000000"
        assert rec["symbol"] == "NVDA-USD"
        assert rec["side"] == "long"
        assert rec["entry_ref"] == 200.0
        assert rec["stop_price"] == 198.0          # native trigger, not the bound
        assert rec["tp1_price"] == 204.0
        assert rec["coherence"] == 5.0
        assert rec["src"] == "arcus_fill"
        assert rec["ts_ms"] == 1_700_000_000_000
        assert rec["arcus_position_id"].startswith("arcus_NVDA-USD_long_")

    def test_synthesized_stop_when_no_tpsl(self, armed, paths):
        client = StubArcusClient(positions=[_pos(side="short", entry=200.0)],
                                 orders=[])
        p = _poller(client, paths)               # default stop_pct = 0.5
        out = run(p.poll_once(now_ms=1_700_000_000_000))
        assert out["emitted"] == 1
        (rec,) = _read_outbox(paths[0])
        assert rec["stop_price"] == pytest.approx(200.0 * 1.005)
        assert rec["tp1_price"] is None

    def test_naked_position_never_emitted(self, armed, paths):
        # entry = 0 (degenerate parse) → no synthesizable stop → skip.
        client = StubArcusClient(positions=[_pos(entry=0.0)], orders=[])
        p = _poller(client, paths)
        out = run(p.poll_once(now_ms=1_700_000_000_000))
        assert out["emitted"] == 0
        assert _read_outbox(paths[0]) == []

    def test_symbol_filter(self, armed, paths):
        client = StubArcusClient(
            positions=[_pos("NVDA-USD"), _pos("PUMP-USD")], orders=[])
        p = _poller(client, paths, symbols=frozenset({"NVDA-USD"}))
        out = run(p.poll_once(now_ms=1_700_000_000_000))
        assert out["emitted"] == 1
        (rec,) = _read_outbox(paths[0])
        assert rec["symbol"] == "NVDA-USD"


# ── Dedup / state persistence ────────────────────────────────────────────────

class TestDedup:
    def test_same_position_broadcasts_once(self, armed, paths):
        client = StubArcusClient(positions=[_pos()], orders=[])
        p = _poller(client, paths)
        run(p.poll_once(now_ms=1_700_000_000_000))
        out = run(p.poll_once(now_ms=1_700_000_020_000))
        assert out["emitted"] == 0
        assert len(_read_outbox(paths[0])) == 1

    def test_pyramid_add_does_not_rebroadcast(self, armed, paths):
        # averageEntryPrice drifts on adds; the (symbol, side) key stands.
        client = StubArcusClient(positions=[_pos(entry=200.0)], orders=[])
        p = _poller(client, paths)
        run(p.poll_once(now_ms=1_700_000_000_000))
        client._positions = [_pos(entry=199.0)]   # add moved the average
        out = run(p.poll_once(now_ms=1_700_000_020_000))
        assert out["emitted"] == 0

    def test_close_then_reopen_rebroadcasts(self, armed, paths):
        client = StubArcusClient(positions=[_pos()], orders=[])
        p = _poller(client, paths)
        run(p.poll_once(now_ms=1_700_000_000_000))
        client._positions = []                     # position closed
        run(p.poll_once(now_ms=1_700_000_020_000))
        client._positions = [_pos(entry=201.0)]    # fresh position
        out = run(p.poll_once(now_ms=1_700_000_040_000))
        assert out["emitted"] == 1
        recs = _read_outbox(paths[0])
        assert len(recs) == 2
        assert recs[1]["entry_ref"] == 201.0

    def test_side_flip_is_a_new_position(self, armed, paths):
        client = StubArcusClient(positions=[_pos(side="long")], orders=[])
        p = _poller(client, paths)
        run(p.poll_once(now_ms=1_700_000_000_000))
        client._positions = [_pos(side="short")]
        out = run(p.poll_once(now_ms=1_700_000_020_000))
        assert out["emitted"] == 1
        recs = _read_outbox(paths[0])
        assert [r["side"] for r in recs] == ["long", "short"]

    def test_state_survives_restart(self, armed, paths):
        client = StubArcusClient(positions=[_pos()], orders=[])
        p1 = _poller(client, paths)
        run(p1.poll_once(now_ms=1_700_000_000_000))
        # Simulate restart: new poller, same paths, position still open.
        p2 = _poller(client, paths)
        out = run(p2.poll_once(now_ms=1_700_000_020_000))
        assert out["emitted"] == 0
        assert len(_read_outbox(paths[0])) == 1

    def test_corrupt_state_file_cold_starts(self, armed, paths):
        with open(paths[1], "w") as f:
            f.write("{not json")
        client = StubArcusClient(positions=[_pos()], orders=[])
        p = _poller(client, paths)                 # load must not raise
        out = run(p.poll_once(now_ms=1_700_000_000_000))
        assert out["emitted"] == 1


# ── Fail-closed venue errors ─────────────────────────────────────────────────

class TestFailClosed:
    def test_api_error_skips_cycle_never_raises(self, armed, paths):
        client = StubArcusClient(positions=[_pos()],
                                 raises=RuntimeError("arcus 503"))
        p = _poller(client, paths)
        out = run(p.poll_once(now_ms=1_700_000_000_000))
        assert out["emitted"] == 0
        assert "error" in out
        assert _read_outbox(paths[0]) == []
        # Error path must not poison dedup: next healthy poll emits.
        client._raises = None
        out2 = run(p.poll_once(now_ms=1_700_000_020_000))
        assert out2["emitted"] == 1


# ── Pure helpers ─────────────────────────────────────────────────────────────

class TestHelpers:
    def test_position_key(self):
        assert position_key(_pos("NVDA-USD", "long")) == ("NVDA-USD", "long")
        assert position_key({"symbol": "", "side": "long"}) is None
        assert position_key({"symbol": "NVDA-USD", "side": "flat"}) is None

    def test_infer_tpsl_long(self):
        pos = _pos(side="long", entry=200.0)
        stop, tp = infer_tpsl(pos, [_sl_order("NVDA-USD", "long", 198.0),
                                    _tp_order("NVDA-USD", "long", 204.0)])
        assert stop == 198.0
        assert tp == 204.0

    def test_infer_tpsl_short_mirrored(self):
        pos = _pos(side="short", entry=200.0)
        stop, tp = infer_tpsl(pos, [_sl_order("NVDA-USD", "short", 202.0),
                                    _tp_order("NVDA-USD", "short", 196.0)])
        assert stop == 202.0
        assert tp == 196.0

    def test_infer_tpsl_ignores_foreign_and_entry_side_orders(self):
        pos = _pos(side="long", entry=200.0)
        foreign = _sl_order("TSLA-USD", "long", 150.0)
        entry_side = _sl_order("NVDA-USD", "long", 190.0)
        entry_side["side"] = "BUY"             # not the close side
        non_ro = _sl_order("NVDA-USD", "long", 195.0)
        non_ro["reduceOnly"] = False
        stop, tp = infer_tpsl(pos, [foreign, entry_side, non_ro])
        assert stop is None and tp is None

    def test_synthesize_stop(self):
        assert synthesize_stop("long", 200.0, 0.5) == pytest.approx(199.0)
        assert synthesize_stop("short", 200.0, 0.5) == pytest.approx(201.0)
        assert synthesize_stop("long", 0.0, 0.5) is None
        assert synthesize_stop("flat", 200.0, 0.5) is None

    def test_build_record_rejects_bad_geometry(self):
        assert build_signal_record("NVDA-USD", "long", 200.0, 201.0,
                                   None, 1, "p") is None   # stop through entry
        assert build_signal_record("NVDA-USD", "short", 200.0, 199.0,
                                   None, 1, "p") is None
        assert build_signal_record("NVDA-USD", "long", 0.0, 199.0,
                                   None, 1, "p") is None
        assert build_signal_record("NVDA-USD", "flat", 200.0, 199.0,
                                   None, 1, "p") is None

    def test_build_record_drops_bad_tp_keeps_signal(self):
        rec = build_signal_record("NVDA-USD", "long", 200.0, 199.0,
                                  150.0, 1, "p")           # TP below entry
        assert rec is not None
        assert rec["tp1_price"] is None
