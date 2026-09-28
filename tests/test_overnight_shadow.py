"""Pins for intelligence/overnight_shadow.py — the Strategy 8 resting-limit
shadow simulator (2026-09-22). Arm geometry, fill-at-level, spent-once,
48h cancel edge, 72h time-stop edge, fee-saving ledger, no-op safety."""
import pytest

from intelligence.overnight_shadow import OvernightShadow


def _mk(**kw):
    return OvernightShadow(**kw)


# ── arm geometry ─────────────────────────────────────────────────────────────

def test_arm_levels_and_fractions():
    sh = _mk()
    rec = sh.arm("BTC-USD", 100.0, 1000.0)
    assert [l["level"] for l in rec["levels"]] == [97.0, 94.0, 91.0]
    assert [l["fraction"] for l in rec["levels"]] == [0.5, 0.75, 1.0]


def test_injectable_levels():
    sh = _mk(levels=(0.95, 0.90), fractions=(0.4, 0.6))
    rec = sh.arm("ETH-USD", 200.0, 1000.0)
    assert [l["level"] for l in rec["levels"]] == [190.0, 180.0]
    assert [l["fraction"] for l in rec["levels"]] == [0.4, 0.6]


def test_misaligned_levels_rejected():
    with pytest.raises(ValueError):
        OvernightShadow(levels=(0.97,), fractions=(0.5, 0.75))


def test_rearm_while_armed_is_noop():
    sh = _mk()
    assert sh.arm("BTC-USD", 100.0, 1000.0) is not None
    assert sh.arm("BTC-USD", 90.0, 1001.0) is None
    assert sh.armed("BTC-USD")["ref_price"] == 100.0


def test_arm_degenerate_inputs():
    sh = _mk()
    assert sh.arm("", 100.0, 1000.0) is None
    assert sh.arm("BTC-USD", 0.0, 1000.0) is None
    assert sh.arm("BTC-USD", -5.0, 1000.0) is None


# ── fills ────────────────────────────────────────────────────────────────────

def test_fill_at_exactly_the_level_price():
    sh = _mk()
    sh.arm("BTC-USD", 100.0, 1000.0)
    assert sh.on_price("BTC-USD", 97.01, 1001.0) == []     # above: no fill
    fills = sh.on_price("BTC-USD", 97.0, 1002.0)           # <=: fills
    assert len(fills) == 1
    f = fills[0]
    assert f["level"] == 97.0 and f["fill_price"] == 97.0
    assert f["fraction"] == 0.5 and f["open"] is True


def test_one_tick_through_level_fills_at_the_level():
    sh = _mk()
    sh.arm("BTC-USD", 100.0, 1000.0)
    fills = sh.on_price("BTC-USD", 96.5, 1001.0)   # through 97, above 94
    assert len(fills) == 1 and fills[0]["fill_price"] == 97.0
    assert fills[0]["trigger_price"] == 96.5


def test_deep_print_fills_multiple_levels_in_one_tick():
    sh = _mk()
    sh.arm("BTC-USD", 100.0, 1000.0)
    fills = sh.on_price("BTC-USD", 90.0, 1001.0)   # through 97, 94, 91
    assert [f["level"] for f in fills] == [97.0, 94.0, 91.0]


def test_spent_level_never_refills():
    sh = _mk()
    sh.arm("BTC-USD", 100.0, 1000.0)
    assert len(sh.on_price("BTC-USD", 97.0, 1001.0)) == 1
    assert sh.on_price("BTC-USD", 96.0, 1002.0) == []      # 97 spent, 94 >
    assert len(sh.on_price("BTC-USD", 94.0, 1003.0)) == 1  # next level fills
    assert sh.on_price("BTC-USD", 93.0, 1004.0) == []      # both spent


# ── 48h cancel edge ──────────────────────────────────────────────────────────

def test_cancel_stale_edge_at_48h():
    sh = _mk()
    t0 = 1_000_000.0
    sh.arm("BTC-USD", 100.0, t0)
    assert sh.cancel_stale(t0 + 48 * 3600 - 1) == []       # 1s before: kept
    out = sh.cancel_stale(t0 + 48 * 3600)                  # exactly 48h: gone
    assert len(out) == 1 and out[0]["reason"] == "stale"
    assert sh.armed("BTC-USD") is None


def test_cancel_stale_reports_filled_levels():
    sh = _mk()
    t0 = 1_000_000.0
    sh.arm("BTC-USD", 100.0, t0)
    sh.on_price("BTC-USD", 97.0, t0 + 10)
    out = sh.cancel_stale(t0 + 48 * 3600)
    assert out[0]["levels_filled"] == 1


# ── 72h time-stop edge + marking ─────────────────────────────────────────────

def test_time_stop_edge_at_72h():
    sh = _mk()
    t0 = 1_000_000.0
    sh.arm("BTC-USD", 100.0, t0)
    sh.on_price("BTC-USD", 97.0, t0)
    # 1s before 72h: still open, ROE marked
    out = sh.mark("BTC-USD", 99.0, t0 + 72 * 3600 - 1)
    assert out == []
    f = sh.open_fills("BTC-USD")[0]
    assert f["roe_pct"] == pytest.approx((99.0 - 97.0) / 97.0 * 100.0)
    # exactly 72h: time-stopped at the mark
    out = sh.mark("BTC-USD", 99.0, t0 + 72 * 3600)
    assert len(out) == 1
    assert out[0]["exit_reason"] == "time_stop" and out[0]["exit_price"] == 99.0
    assert out[0]["roe_pct"] == pytest.approx(200.0 / 97.0)
    assert sh.open_fills("BTC-USD") == []


def test_open_fills_filter_by_symbol():
    sh = _mk()
    sh.arm("BTC-USD", 100.0, 1000.0)
    sh.arm("ETH-USD", 200.0, 1000.0)
    sh.on_price("BTC-USD", 97.0, 1001.0)
    sh.on_price("ETH-USD", 194.0, 1001.0)
    assert len(sh.open_fills()) == 2
    assert len(sh.open_fills("BTC-USD")) == 1


# ── fee-saving ledger ────────────────────────────────────────────────────────

def test_fee_saving_math():
    sh = _mk(taker_bps=5.5, maker_bps=0.0)
    sh.arm("BTC-USD", 100.0, 1000.0, notional_usd=1000.0)
    fills = sh.on_price("BTC-USD", 90.0, 1001.0)
    by_frac = {f["fraction"]: f for f in fills}
    # level notional = armed notional x fraction; saving = notional x 5.5bps
    assert by_frac[0.5]["notional_usd"] == 500.0
    assert by_frac[0.5]["fee_saving_usd"] == pytest.approx(500.0 * 5.5 / 1e4)
    assert by_frac[1.0]["fee_saving_usd"] == pytest.approx(1000.0 * 5.5 / 1e4)
    assert by_frac[1.0]["fee_saving_bps"] == 5.5


def test_fee_saving_usd_none_without_notional():
    sh = _mk()
    sh.arm("BTC-USD", 100.0, 1000.0)                       # no notional
    f = sh.on_price("BTC-USD", 97.0, 1001.0)[0]
    assert f["fee_saving_usd"] is None and f["fee_saving_bps"] == 5.5


def test_time_stop_pnl_usd_uses_level_notional():
    sh = _mk()
    t0 = 1_000_000.0
    sh.arm("BTC-USD", 100.0, t0, notional_usd=1000.0)
    sh.on_price("BTC-USD", 97.0, t0)                       # 0.5 frac -> $500
    out = sh.mark("BTC-USD", 98.940, t0 + 72 * 3600)       # +2% on the fill
    assert out[0]["pnl_usd"] == pytest.approx(500.0 * (98.940 - 97.0) / 97.0)


# ── empty / no-op safety ─────────────────────────────────────────────────────

def test_noop_safety():
    sh = _mk()
    assert sh.on_price("BTC-USD", 100.0, 1.0) == []        # no arm
    assert sh.on_price("BTC-USD", 0.0, 1.0) == []          # degenerate price
    assert sh.on_price("BTC-USD", None, 1.0) == []
    assert sh.mark("BTC-USD", 100.0, 1.0) == []            # no fills
    assert sh.mark("BTC-USD", 0.0, 1.0) == []
    assert sh.cancel_stale(1.0) == []                      # nothing armed
    assert sh.open_fills() == []
    assert sh.armed() == {}
