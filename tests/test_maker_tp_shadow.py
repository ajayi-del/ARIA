"""Pins for intelligence/maker_tp_shadow.py — the Strategy 3 maker-TP-at-
structure shadow simulator (2026-09-22). Exceed-not-touch fill rule, tranche
math, 72h taker fallback, fee ledger, no-fill open state."""
import pytest

from intelligence.maker_tp_shadow import MakerTpShadow


def _mk(**kw):
    return MakerTpShadow(**kw)


# ── registration + tranche math ──────────────────────────────────────────────

def test_tranche_math_default_half():
    sh = _mk()
    rec = sh.on_entry("BTC-USD", 100.0, 10.0, 105.0, 1000.0)
    assert rec["tranche_qty"] == 5.0
    assert sh.open("BTC-USD")["tranche_qty"] == 5.0


def test_tranche_injectable():
    sh = _mk(tranche=0.25)
    rec = sh.on_entry("ETH-USD", 200.0, 8.0, 210.0, 1000.0)
    assert rec["tranche_qty"] == 2.0


def test_tranche_bounds_rejected():
    with pytest.raises(ValueError):
        MakerTpShadow(tranche=0.0)
    with pytest.raises(ValueError):
        MakerTpShadow(tranche=1.5)


def test_register_over_open_shadow_is_noop():
    sh = _mk()
    assert sh.on_entry("BTC-USD", 100.0, 10.0, 105.0, 1000.0) is not None
    assert sh.on_entry("BTC-USD", 101.0, 10.0, 106.0, 1001.0) is None
    assert sh.open("BTC-USD")["entry_price"] == 100.0


def test_register_degenerate_inputs():
    sh = _mk()
    assert sh.on_entry("", 100.0, 10.0, 105.0, 1.0) is None
    assert sh.on_entry("BTC-USD", 0.0, 10.0, 105.0, 1.0) is None
    assert sh.on_entry("BTC-USD", 100.0, 0.0, 105.0, 1.0) is None
    assert sh.on_entry("BTC-USD", 100.0, 10.0, 0.0, 1.0) is None


# ── exceed-not-touch fill rule ───────────────────────────────────────────────

def test_a_touch_is_not_a_fill():
    sh = _mk()                                             # slop 1.0 bps
    sh.on_entry("BTC-USD", 100.0, 10.0, 105.0, 1000.0)
    assert sh.on_price("BTC-USD", 104.999, 99.0, 1001.0) == []
    assert sh.on_price("BTC-USD", 105.0, 99.0, 1002.0) == []   # exact touch
    assert sh.open("BTC-USD") is not None                      # still open


def test_fill_only_on_exceed():
    sh = _mk()
    sh.on_entry("BTC-USD", 100.0, 10.0, 105.0, 1000.0)
    just_under = 105.0 * (1.0 + 1.0 / 1e4) * (1 - 1e-9)
    assert sh.on_price("BTC-USD", just_under, 99.0, 1001.0) == []
    just_at = 105.0 * (1.0 + 1.0 / 1e4)
    recs = sh.on_price("BTC-USD", just_at, 99.0, 1002.0)
    assert len(recs) == 1 and recs[0]["exit_reason"] == "maker_fill"
    assert sh.open("BTC-USD") is None


def test_injectable_slop():
    sh = _mk(fill_slop_bps=10.0)                           # 10 bps
    sh.on_entry("BTC-USD", 100.0, 10.0, 105.0, 1000.0)
    assert sh.on_price("BTC-USD", 105.0 * 1.0005, 99.0, 1001.0) == []
    recs = sh.on_price("BTC-USD", 105.0 * 1.0010, 99.0, 1002.0)
    assert len(recs) == 1


# ── maker fill ledger ────────────────────────────────────────────────────────

def test_maker_fill_record_and_fee_ledger():
    sh = _mk()                                             # taker 5.5, maker 0
    sh.on_entry("BTC-USD", 100.0, 10.0, 105.0, 1000.0)
    rec = sh.on_price("BTC-USD", 105.5, 99.0, 1600.0)[0]
    assert rec["exit_reason"] == "maker_fill"
    assert rec["would_fill_ts"] == 1600.0
    assert rec["maker_price"] == 105.0
    assert rec["tranche_qty"] == 5.0
    assert rec["hold_s"] == 600.0
    # pnl = (tp1 - entry) x tranche qty
    assert rec["pnl_usd"] == pytest.approx(5.0 * 5.0)
    # fee saved = tranche notional x (taker - maker) bps
    assert rec["fee_saved_usd"] == pytest.approx(5.0 * 105.0 * 5.5 / 1e4)
    assert rec["fee_charged_usd"] == 0.0


# ── 72h taker fallback ───────────────────────────────────────────────────────

def test_taker_fallback_edge_at_72h():
    sh = _mk()
    t0 = 1_000_000.0
    sh.on_entry("BTC-USD", 100.0, 10.0, 105.0, t0)
    # 1s before 72h: still open
    assert sh.on_price("BTC-USD", 101.0, 100.0,
                       t0 + 72 * 3600 - 1) == []
    assert sh.open("BTC-USD") is not None
    # exactly 72h unresolved: hypothetical taker exit at the mid proxy
    recs = sh.on_price("BTC-USD", 102.0, 100.0, t0 + 72 * 3600)
    assert len(recs) == 1
    rec = recs[0]
    assert rec["exit_reason"] == "taker_fallback"
    assert rec["would_fill_ts"] is None
    assert rec["fallback_price"] == 101.0                  # (102+100)/2
    assert rec["pnl_usd"] == pytest.approx((101.0 - 100.0) * 5.0)
    assert rec["fee_saved_usd"] == 0.0
    assert rec["fee_charged_usd"] == pytest.approx(5.0 * 101.0 * 5.5 / 1e4)
    assert sh.open("BTC-USD") is None


def test_fallback_loses_to_a_late_fill():
    sh = _mk()
    t0 = 1_000_000.0
    sh.on_entry("BTC-USD", 100.0, 10.0, 105.0, t0)
    recs = sh.on_price("BTC-USD", 106.0, 99.0, t0 + 71 * 3600)
    assert recs[0]["exit_reason"] == "maker_fill"          # fill before 72h
    assert sh.open("BTC-USD") is None
    # a later print on a resolved shadow is a no-op
    assert sh.on_price("BTC-USD", 106.0, 99.0, t0 + 72 * 3600) == []


def test_fill_outranks_fallback_on_the_same_bar():
    sh = _mk()
    t0 = 1_000_000.0
    sh.on_entry("BTC-USD", 100.0, 10.0, 105.0, t0)
    # the 72h boundary bar ALSO exceeds tp1 -> maker fill, not fallback
    recs = sh.on_price("BTC-USD", 106.0, 100.0, t0 + 72 * 3600)
    assert recs[0]["exit_reason"] == "maker_fill"


# ── no-fill open state + no-op safety ────────────────────────────────────────

def test_no_fill_open_state():
    sh = _mk()
    sh.on_entry("BTC-USD", 100.0, 10.0, 105.0, 1000.0)
    sh.on_entry("ETH-USD", 200.0, 8.0, 210.0, 1000.0)
    assert set(sh.open().keys()) == {"BTC-USD", "ETH-USD"}
    assert sh.on_price("BTC-USD", 102.0, 99.0, 1001.0) == []
    assert set(sh.open().keys()) == {"BTC-USD", "ETH-USD"}


def test_noop_safety():
    sh = _mk()
    assert sh.on_price("BTC-USD", 100.0, 99.0, 1.0) == []   # nothing open
    assert sh.on_price("BTC-USD", 0.0, 0.0, 1.0) == []      # degenerate
    assert sh.on_price("BTC-USD", None, None, 1.0) == []
    assert sh.open() == {}
    assert sh.open("BTC-USD") is None
