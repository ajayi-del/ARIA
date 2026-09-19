"""P0 hedge spine pins (2026-09-19 Governor-approved hedge venue).

A third venue (ByBit) will run intentional hedge SHORTS against Aster/SoDEX
longs. P0 is the inert spine: Position role tagging, risk/hedge_registry.py
(hedge legs NEVER enter the PositionManager — it nets opposite sides by
symbol), venue-grouped reconciliation, role guards across the portfolio
loops, and a (symbol, venue) close-path namespace. Everything is inert while
config.hedge_enabled=False; the primary book is bit-for-bit.

Guard-closure note (same doctrine as tests/test_loss_cooloff.py): the veto/
dust/treasury/coherence/conviction/cap guard sites live inside main()
closures and are not importable — this file pins the level that IS
importable: the module-level predicates/helpers the guards delegate to, the
config defaults, the registry, and the venue-split bit-for-bit contract.
"""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from core.config import Settings  # noqa: E402
from execution.schemas import Position  # noqa: E402
from risk.hedge_registry import HedgeRegistry  # noqa: E402
from risk.position_manager import PositionManager  # noqa: E402


def _pos(symbol="BTC-USD", side="long", size=0.01, entry=50000.0, **kw):
    return SimpleNamespace(symbol=symbol, side=side, size=size,
                           entry_price=entry, **kw)


def _make_position(**overrides):
    args = dict(symbol="BTC-USD", side="long", entry_price=50000.0,
                size=0.01, stop_price=49000.0, tp1_price=51000.0,
                tp2_price=52000.0, tp3_price=53000.0, liq_price=40000.0,
                initial_margin=100.0, leverage=5, opened_at_ms=1)
    args.update(overrides)
    return Position(**args)


# ── Position schema defaults ─────────────────────────────────────────────────

class TestPositionDefaults:
    def test_existing_constructions_default_primary(self):
        p = _make_position()
        assert p.role == "primary"
        assert p.hedge_of == ""
        assert p.venue == "sodex"   # pre-spine default untouched

    def test_role_and_hedge_of_settable(self):
        p = _make_position(role="hedge", hedge_of="plan-001", venue="bybit")
        assert p.role == "hedge"
        assert p.hedge_of == "plan-001"
        assert p.venue == "bybit"


# ── HedgeRegistry ────────────────────────────────────────────────────────────

class TestHedgeRegistry:
    def test_upsert_and_lookups(self):
        reg = HedgeRegistry()
        leg = reg.upsert("plan-1", symbol="BTC-USD", venue="bybit",
                         side="short", qty=0.01, entry_price=50000.0,
                         hedge_of="primary-1")
        assert leg.pair_id == "plan-1"
        assert reg.by_pair("plan-1") is leg
        assert reg.get("plan-1") is leg
        assert reg.by_symbol("BTC-USD") == [leg]
        assert reg.all() == [leg]
        assert len(reg) == 1

    def test_update_and_remove(self):
        reg = HedgeRegistry()
        reg.upsert("plan-1", symbol="BTC-USD", venue="bybit",
                   side="short", qty=0.01, entry_price=50000.0)
        leg = reg.update("plan-1", qty=0.02, state="closing")
        assert leg.qty == 0.02
        assert leg.state == "closing"
        # pair_id is immutable via update
        reg.update("plan-1", pair_id="plan-999")
        assert reg.get("plan-999") is None
        assert reg.get("plan-1") is not None
        # unknown pair id
        assert reg.update("nope", qty=1.0) is None
        removed = reg.remove("plan-1")
        assert removed.pair_id == "plan-1"
        assert reg.get("plan-1") is None
        assert reg.remove("plan-1") is None
        assert len(reg) == 0

    def test_upsert_same_pair_replaces_never_nets(self):
        reg = HedgeRegistry()
        reg.upsert("plan-1", symbol="BTC-USD", venue="bybit",
                   side="short", qty=0.01, entry_price=50000.0)
        reg.upsert("plan-1", symbol="BTC-USD", venue="bybit",
                   side="short", qty=0.02, entry_price=50100.0)
        assert len(reg) == 1
        assert reg.get("plan-1").qty == 0.02

    def test_no_netting_opposite_sides_coexist(self):
        """The primary-manager contrast case: PM.add() nets a short against a
        long (one-way mode); the registry keeps BOTH legs — a hedge short must
        never annihilate the long's record."""
        pm = PositionManager()
        pm.add(_make_position(side="long", size=0.01))
        pm.add(_make_position(side="short", size=0.01))
        assert pm.get("BTC-USD") == []          # exact cancel — the netting wound

        reg = HedgeRegistry()
        reg.upsert("primary-1", symbol="BTC-USD", venue="sodex",
                   side="long", qty=0.01, entry_price=50000.0)
        reg.upsert("plan-1", symbol="BTC-USD", venue="bybit",
                   side="short", qty=0.01, entry_price=50000.0,
                   hedge_of="primary-1")
        legs = reg.by_symbol("BTC-USD")
        assert len(legs) == 2
        assert {l.side for l in legs} == {"long", "short"}
        assert reg.by_pair("plan-1").hedge_of == "primary-1"


# ── Role guard predicate + cap counter ───────────────────────────────────────

class TestRoleGuard:
    def test_is_hedge_role(self):
        assert main._is_hedge_role(_pos(role="hedge")) is True
        assert main._is_hedge_role(_pos(role="primary")) is False
        assert main._is_hedge_role(_pos()) is False              # no role attr
        assert main._is_hedge_role(_pos(role=None)) is False     # fail-open primary
        assert main._is_hedge_role(None) is False

    def test_count_primary_positions(self):
        book = [_pos(), _pos(symbol="ETH-USD"), _pos(role="hedge")]
        assert main.count_primary_positions(book) == 2
        # Bit-for-bit with len() on a primary-only book (the inert contract)
        primary_only = [_pos(), _pos(symbol="ETH-USD")]
        assert main.count_primary_positions(primary_only) == len(primary_only)
        assert main.count_primary_positions([]) == 0
        assert main.count_primary_positions(None) == 0

    def test_dust_absorb_selection_skips_hedge(self):
        """The dust-scan absorb (main() closure) skips hedge rows via
        _is_hedge_role before the symbol/side match. Pin the selection
        contract: with only a hedge row present, nothing is absorbed."""
        hedge = _pos(side="short", size=0.00005, entry=50000.0, role="hedge")
        selected = 0.0
        for _dp in [hedge]:
            if main._is_hedge_role(_dp):
                continue
            if _dp.symbol != "BTC-USD" or _dp.side == "long":
                continue
            if float(_dp.size) * 50000.0 < 10.0:
                selected += float(_dp.size)
        assert selected == 0.0


# ── Cross-sleeve veto guard ──────────────────────────────────────────────────

class TestOpposingVetoGuard:
    def test_hedge_short_is_not_opposition(self):
        book = [_pos(side="short", size=0.01, entry=50000.0, role="hedge")]
        assert main._opposing_position_reason(book, "BTC-USD", "long") is None

    def test_primary_short_still_blocks(self):
        book = [_pos(side="short", size=0.01, entry=50000.0, role="primary")]
        assert main._opposing_position_reason(book, "BTC-USD", "long") == "opposing_short_500"

    def test_mixed_book_blocks_on_primary_only(self):
        book = [_pos(side="short", size=0.01, entry=50000.0, role="hedge"),
                _pos(symbol="ETH-USD", side="long", size=1.0, entry=3000.0)]
        assert main._opposing_position_reason(book, "BTC-USD", "long") is None
        assert main._opposing_position_reason(book, "ETH-USD", "short") == "opposing_long_3000"


# ── Venue-grouped reconciliation ─────────────────────────────────────────────

def _row(symbol, size, venue=None, side="long", entry=100.0):
    r = {"symbol": symbol, "coin": symbol, "size": size, "qty": size,
         "side": side, "avgPrice": entry}
    if venue is not None:
        r["venue"] = venue
    return r


class TestVenueSplit:
    def test_disabled_is_legacy_bit_for_bit(self):
        rows = [_row("BTC-USD", 0.01),                      # SoDEX: unstamped
                _row("ETH-USD", 1.0, venue="aster"),
                _row("SOL-USD", 2.0, venue="bybit")]
        primary, hedge = main.split_exchange_rows_by_venue(
            rows, hedge_venue="bybit", hedge_enabled=False)
        # Legacy construction, verbatim:
        legacy = {}
        for pos in rows:
            sym = pos.get("symbol", "") or pos.get("coin", "")
            size = abs(float(pos.get("size", 0) or pos.get("qty", 0) or 0))
            if size > 0 and sym:
                legacy[sym] = (size, pos)
        assert primary == legacy
        assert hedge == []

    def test_enabled_routes_hedge_venue_rows(self):
        rows = [_row("BTC-USD", 0.01),
                _row("ETH-USD", 1.0, venue="aster"),
                _row("SOL-USD", 2.0, venue="bybit")]
        primary, hedge = main.split_exchange_rows_by_venue(
            rows, hedge_venue="bybit", hedge_enabled=True)
        assert set(primary) == {"BTC-USD", "ETH-USD"}
        assert [h["symbol"] for h in hedge] == ["SOL-USD"]

    def test_same_symbol_different_venue_groups_by_venue(self):
        sodex_long = _row("BTC-USD", 0.01, side="long")
        bybit_short = _row("BTC-USD", 0.01, venue="bybit", side="short")
        primary, hedge = main.split_exchange_rows_by_venue(
            [sodex_long, bybit_short], hedge_venue="bybit", hedge_enabled=True)
        assert primary["BTC-USD"] == (0.01, sodex_long)   # primary matching intact
        assert hedge == [bybit_short]                     # hedge row never collides

    def test_dust_and_blank_rows_skipped(self):
        rows = [_row("BTC-USD", 0.0), _row("", 1.0, venue="bybit"),
                _row("ETH-USD", 1.0, venue="bybit")]
        primary, hedge = main.split_exchange_rows_by_venue(
            rows, hedge_venue="bybit", hedge_enabled=True)
        assert primary == {}
        assert [h["symbol"] for h in hedge] == ["ETH-USD"]

    def test_empty_hedge_venue_keeps_everything_primary(self):
        rows = [_row("SOL-USD", 2.0, venue="bybit")]
        primary, hedge = main.split_exchange_rows_by_venue(
            rows, hedge_venue="", hedge_enabled=True)
        assert set(primary) == {"SOL-USD"}
        assert hedge == []

    def test_hedge_reconcile_stub_is_noop(self):
        reg = HedgeRegistry()
        rows = [_row("SOL-USD", 2.0, venue="bybit")]
        assert main._hedge_reconcile_stub(rows, reg) == 1
        assert main._hedge_reconcile_stub([], reg) == 0
        assert main._hedge_reconcile_stub(None, reg) == 0
        assert len(reg) == 0            # no adoption, no side effects


# ── Close-path namespace ─────────────────────────────────────────────────────

class TestCloseNamespace:
    def test_hedge_entry_key(self):
        assert main.hedge_entry_key("BTC-USD", "Bybit") == ("BTC-USD", "bybit")
        assert main.hedge_entry_key("BTC-USD", "") == ("BTC-USD", "")

    def test_hedge_pop_never_touches_primary_map(self):
        primary_map = {"BTC-USD": "eid-primary"}
        hedge_map = {("BTC-USD", "bybit"): "eid-hedge"}
        hedge_pos = _pos(role="hedge", venue="bybit")
        assert main.pop_close_entry_id(primary_map, hedge_map, "BTC-USD", hedge_pos) == "eid-hedge"
        assert primary_map == {"BTC-USD": "eid-primary"}   # primary id intact
        assert hedge_map == {}

    def test_primary_pop_is_legacy_bit_for_bit(self):
        primary_map = {"BTC-USD": "eid-primary"}
        hedge_map = {("BTC-USD", "bybit"): "eid-hedge"}
        assert main.pop_close_entry_id(primary_map, hedge_map, "BTC-USD", _pos()) == "eid-primary"
        assert primary_map == {}
        assert hedge_map == {("BTC-USD", "bybit"): "eid-hedge"}
        # Missing symbol → None, same as dict.pop(sym, None)
        assert main.pop_close_entry_id(primary_map, hedge_map, "ETH-USD", _pos()) is None


# ── Config defaults (master kill switch) ─────────────────────────────────────

class TestConfig:
    def test_hedge_defaults_inert(self):
        s = Settings()
        assert s.hedge_enabled is False
        assert s.hedge_venue == "bybit"
