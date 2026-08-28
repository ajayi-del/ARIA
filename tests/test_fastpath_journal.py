"""Fast-path entry journaling + actionable-position (dust-aware) flatness."""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import _has_actionable_position, _journal_fastpath_entry  # noqa: E402
from memory.trade_journal import TradeJournal  # noqa: E402
from execution import venue  # noqa: E402


def _pm(positions_by_sym):
    # PositionManager.get_all() returns a FLAT LIST (risk/position_manager.py:25)
    flat = [p for plist in positions_by_sym.values() for p in plist]
    return SimpleNamespace(get_all=lambda: flat)


def _pos(entry_price, size, symbol="ETH-USD"):
    return SimpleNamespace(entry_price=entry_price, size=size, symbol=symbol)


# ── _has_actionable_position ─────────────────────────────────────────────────

def test_empty_book_is_flat():
    assert _has_actionable_position(_pm({})) is False


def test_sodex_dust_only_book_is_flat():
    # $0.25 ETH dust: 0.0001 × 2500 < $10 SoDEX close min — structurally
    # unclosable, must not hold the withdrawal detector shut
    assert _has_actionable_position(
        _pm({"ETH-USD": [_pos(2500.0, 0.0001)]})) is False


def test_sodex_real_position_is_open():
    assert _has_actionable_position(
        _pm({"ETH-USD": [_pos(2500.0, 0.01)]})) is True   # $25 notional


def test_sodex_boundary_10usd_is_open():
    assert _has_actionable_position(
        _pm({"BTC-USD": [_pos(100.0, 0.1, "BTC-USD")]})) is True     # exactly $10


def test_aster_dust_only_book_is_flat():
    venue.register_executor("aster", object())
    venue.assign_symbols(["LIT-USD"], "aster")
    try:
        assert _has_actionable_position(
            _pm({"LIT-USD": [_pos(2.0, 0.4, "LIT-USD")]})) is False  # $0.80 < $1 min
        assert _has_actionable_position(
            _pm({"LIT-USD": [_pos(2.0, 2.0, "LIT-USD")]})) is True   # $4
    finally:
        venue._executors.pop("aster", None)
        venue._venue_by_symbol.pop("LIT-USD", None)


def test_mixed_book_with_one_real_position_is_open():
    book = {"ETH-USD": [_pos(2500.0, 0.0001)],
            "BTC-USD": [_pos(100.0, 0.5, "BTC-USD")]}
    assert _has_actionable_position(_pm(book)) is True


# ── _journal_fastpath_entry ──────────────────────────────────────────────────

def _candidate():
    return SimpleNamespace(
        side="long", entry_price=100.0, stop_price=99.0,
        tp1_price=102.0, tp2_price=103.0, tp3_price=104.0,
        size=1.0, initial_margin=20.0, leverage=5, coherence=7.1,
    )


def test_fastpath_entry_journaled_and_registered(tmp_path):
    jrnl = TradeJournal(log_dir=str(tmp_path))
    ids = {}
    _journal_fastpath_entry(jrnl, ids, "BTC-USD", "long", _candidate(),
                            "cascade_momentum", "momentum", "APEX")
    assert ids["BTC-USD"] == jrnl.entries[-1]["entry_id"]
    rec = jrnl.entries[-1]
    assert rec["approved"] is True
    assert rec["personality"] == "APEX"
    assert rec["strategy_tag"] == "cascade_momentum"
    assert rec["cascade_phase"] == "momentum"
    assert rec["entry_price"] == 100.0
    assert rec["stop_price"] == 99.0
    assert rec["coherence_score"] == 7.1
    assert rec["outcome"] is None


def test_fastpath_aftermath_personality(tmp_path):
    jrnl = TradeJournal(log_dir=str(tmp_path))
    ids = {}
    _journal_fastpath_entry(jrnl, ids, "SOL-USD", "short", _candidate(),
                            "cascade_aftermath", "aftermath", "AFTERMATH")
    assert jrnl.entries[-1]["personality"] == "AFTERMATH"
    assert ids["SOL-USD"]


def test_fastpath_journal_failure_never_raises(tmp_path):
    class _Exploding:
        def log_decision(self, **kw):
            raise RuntimeError("disk on fire")

    ids = {}
    _journal_fastpath_entry(_Exploding(), ids, "BTC-USD", "long",
                            _candidate(), "cascade_momentum", "momentum", "APEX")
    assert ids == {}   # failed open: position still trades, close falls back
