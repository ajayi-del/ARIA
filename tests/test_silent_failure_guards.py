"""Silent-failure guards (2026-08-21).

Fix 1 — consecutive-absence close confirmation (XAUT ghost): a successful-
but-partial venue poll booked a phantom exchange_close that cancelled a live
stop; the position ran naked 9h. absence_confirmed requires 3 straight passes.

Fix 2 — mark-discontinuity quarantine (SPCX rebase): a 5.7x synthetic rebase
fired a software stop on the post-rebase mark and journaled phantom PnL while
the balance was untouched. MarkPriceStore quarantines trigger consumers;
rebase_reanchor re-bases entry/stop/size from exchange economics.

Fix 3 — _record_close dedup (SOL double-journal): exchange_close and
external_close raced one fill and booked it twice. close_is_duplicate blocks
the second booking inside the 30s grace window with no live position.
"""
import time
from types import SimpleNamespace

import pytest

from data.mark_price_store import MarkPriceStore
from main import absence_confirmed, close_is_duplicate, rebase_reanchor


# ── Fix 1: absence_confirmed ────────────────────────────────────────────────

def test_absence_requires_three_consecutive_passes():
    counts = {}
    assert absence_confirmed(counts, "XAUT-USD") is False
    assert absence_confirmed(counts, "XAUT-USD") is False
    assert absence_confirmed(counts, "XAUT-USD") is True


def test_absence_presence_reset_forces_full_recount():
    counts = {}
    absence_confirmed(counts, "XAUT-USD")
    absence_confirmed(counts, "XAUT-USD")
    counts.pop("XAUT-USD", None)  # symbol seen on exchange → reset
    assert absence_confirmed(counts, "XAUT-USD") is False
    assert absence_confirmed(counts, "XAUT-USD") is False
    assert absence_confirmed(counts, "XAUT-USD") is True


def test_absence_counter_cleared_on_confirmation():
    counts = {}
    for _ in range(3):
        absence_confirmed(counts, "XAUT-USD")
    assert "XAUT-USD" not in counts


def test_absence_symbols_tracked_independently():
    counts = {}
    absence_confirmed(counts, "XAUT-USD")
    absence_confirmed(counts, "XAUT-USD")
    assert absence_confirmed(counts, "SOL-USD") is False
    assert absence_confirmed(counts, "XAUT-USD") is True


# ── Fix 2: mark discontinuity quarantine ─────────────────────────────────────

def _now_ms() -> int:
    return int(time.time() * 1000)


def test_small_moves_never_quarantine():
    s = MarkPriceStore("BTC-USD")
    s.update(100.0, 100.0, _now_ms())
    s.update(114.9, 114.9, _now_ms())  # +14.9% — under the 15% bar
    assert not s.is_quarantined()
    assert s.quarantine_factor == 0.0


def test_discontinuity_quarantines_and_records_factor():
    s = MarkPriceStore("SPCX-USD")
    s.update(133.0, 133.0, _now_ms())
    s.update(766.35, 766.35, _now_ms())  # the 2026-08-21 rebase: ~5.76x
    assert s.is_quarantined()
    assert s.quarantine_factor == pytest.approx(766.35 / 133.0)


def test_quarantine_expires():
    s = MarkPriceStore("SPCX-USD")
    s.update(133.0, 133.0, _now_ms())
    stale = _now_ms() - MarkPriceStore.QUARANTINE_MS - 1000
    s.update(766.35, 766.35, stale)
    assert not s.is_quarantined()


def test_first_update_never_quarantines():
    s = MarkPriceStore("ETH-USD")
    s.update(4300.0, 4300.0, _now_ms())
    assert not s.is_quarantined()


def test_down_rebase_also_quarantines():
    s = MarkPriceStore("XYZ-USD")
    s.update(100.0, 100.0, _now_ms())
    s.update(50.0, 50.0, _now_ms())  # -50% reverse split
    assert s.is_quarantined()
    assert s.quarantine_factor == pytest.approx(0.5)


# ── Fix 2: rebase_reanchor ───────────────────────────────────────────────────

def _pos(entry, stop, size, tp1=0.0, tp2=0.0, tp3=0.0):
    return SimpleNamespace(entry_price=entry, stop_price=stop, size=size,
                           tp1_price=tp1, tp2_price=tp2, tp3_price=tp3)


def test_reanchor_scales_entry_stop_size():
    p = _pos(entry=133.0, stop=120.0, size=10.0, tp1=150.0, tp2=170.0)
    k = 766.35 / 133.0
    assert rebase_reanchor(p, ex_size=10.0 / k, factor=k) is True
    assert p.entry_price == pytest.approx(766.35)
    assert p.stop_price == pytest.approx(120.0 * k)
    assert p.tp1_price == pytest.approx(150.0 * k)
    assert p.tp2_price == pytest.approx(170.0 * k)
    assert p.tp3_price == 0.0  # unset stays unset — no phantom level created
    assert p.size == pytest.approx(10.0 / k)
    # Notional preserved: entry * size unchanged through the rebase.
    assert p.entry_price * p.size == pytest.approx(133.0 * 10.0, rel=1e-4)


def test_reanchor_refuses_real_price_move():
    # Mark jumped but exchange size did NOT scale inversely → real move.
    p = _pos(entry=100.0, stop=90.0, size=5.0)
    assert rebase_reanchor(p, ex_size=5.0, factor=1.2) is False
    assert p.entry_price == 100.0
    assert p.stop_price == 90.0
    assert p.size == 5.0


def test_reanchor_handles_zero_stop():
    p = _pos(entry=133.0, stop=0.0, size=10.0)
    k = 5.0
    assert rebase_reanchor(p, ex_size=2.0, factor=k) is True
    assert p.stop_price == 0.0  # no phantom stop created


def test_reanchor_rejects_bad_inputs():
    p = _pos(entry=100.0, stop=90.0, size=5.0)
    assert rebase_reanchor(p, ex_size=0.0, factor=5.0) is False
    assert rebase_reanchor(p, ex_size=1.0, factor=0.0) is False
    p0 = _pos(entry=0.0, stop=0.0, size=5.0)
    assert rebase_reanchor(p0, ex_size=1.0, factor=5.0) is False


# ── Fix 3: close_is_duplicate ────────────────────────────────────────────────

def test_duplicate_inside_grace_without_live_position():
    rc = {"SOL-USD": time.time() + 30.0}
    assert close_is_duplicate(rc, "SOL-USD", has_live_position=False,
                              now=time.time()) is True


def test_first_close_with_live_position_never_duplicate():
    rc = {"SOL-USD": time.time() + 30.0}
    # Live position present → NOT a duplicate even inside grace: a fresh
    # re-entry inside the window is a NEW position and must book its close.
    assert close_is_duplicate(rc, "SOL-USD", has_live_position=True,
                              now=time.time()) is False


def test_expired_grace_allows_booking():
    rc = {"SOL-USD": time.time() - 1.0}
    assert close_is_duplicate(rc, "SOL-USD", has_live_position=False,
                              now=time.time()) is False


def test_no_prior_close_allows_booking():
    assert close_is_duplicate({}, "SOL-USD", has_live_position=False,
                              now=time.time()) is False


# ── Fix 4 (2026-08-22): mark-entry scale guard + risk_engine daily_pnl feed ──
# SPCX phantom: SoDEX markPrice served the pre-rebase scale (765.72) while
# klines/entries served ~135 — a persistent 5.66x split, not a tick jump, so
# the discontinuity quarantine never armed; software_tp journaled +$792/+$799
# phantom wins against a $126-notional position. And the Kant Gate-8 daily
# loss accumulator was never fed — daily_pnl read 0.00 all day.

from main import _mark_entry_scale_ok
from risk.risk_engine import RiskEngine


def _scale_pos(entry):
    return SimpleNamespace(entry_price=entry, size=1.0, side="long")


def test_spcx_phantom_scale_split_blocked():
    # The exact 2026-08-22 case: entry-scale ladder vs mark-scale price.
    assert _mark_entry_scale_ok("SPCX-USD", 765.72, _scale_pos(136.63)) is False


def test_mark_within_band_passes():
    assert _mark_entry_scale_ok("BTC-USD", 101.0, _scale_pos(100.0)) is True
    assert _mark_entry_scale_ok("BTC-USD", 129.9, _scale_pos(100.0)) is True
    assert _mark_entry_scale_ok("BTC-USD", 70.1, _scale_pos(100.0)) is True


def test_scale_split_symmetric_downside():
    # Mark at 18% of entry (reverse split) is just as impossible for a live
    # tracked position — yesterday's phantom-loss class (08-21, -$649.78).
    assert _mark_entry_scale_ok("SPCX-USD", 24.0, _scale_pos(135.0)) is False


def test_zero_or_missing_prices_defer():
    assert _mark_entry_scale_ok("X-USD", 0.0, _scale_pos(100.0)) is True
    assert _mark_entry_scale_ok("X-USD", 100.0, _scale_pos(0.0)) is True


def test_custom_limit_respected():
    assert _mark_entry_scale_ok("X-USD", 115.0, _scale_pos(100.0),
                                limit_pct=0.10) is False
    assert _mark_entry_scale_ok("X-USD", 109.0, _scale_pos(100.0),
                                limit_pct=0.10) is True


def _risk_engine():
    return RiskEngine(config=SimpleNamespace(mode="paper", balance_floor=50.0),
                      margin_engine=None, position_manager=None,
                      calendar_engine=None)


def test_risk_engine_daily_pnl_accumulates():
    re_ = _risk_engine()
    re_.record_close(-1.5, "2026-08-22")
    re_.record_close(0.5, "2026-08-22")
    assert re_.daily_pnl == -1.0


def test_risk_engine_daily_pnl_day_roll_resets():
    re_ = _risk_engine()
    re_.record_close(-4.0, "2026-08-22")
    re_.record_close(0.25, "2026-08-23")
    assert re_.daily_pnl == 0.25


def test_risk_engine_gate8_trips_on_fed_losses():
    re_ = _risk_engine()
    re_.record_close(-6.0, "2026-08-22")   # 6% of $100 — past the 5% breaker
    ok, reason = re_._gate_daily_loss(100.0)
    assert ok is False
    assert "daily_loss_limit" in reason
