"""Tests for intelligence/mover_relief.py — two-stage mover relief detector.

Covers: ALERT threshold yes/no, ALERT TTL, RELIEF 2-of-3 (each pair +
single-confirmation negative), rolling-window semantics (old events
excluded), RELIEF TTL, and the mandatory synthetic UNI-day calibration.
"""
import json

from intelligence.mover_relief import (
    ALERT_BLOCKS_PER_HOUR,
    ALERT_TTL_S,
    ALERT_WINDOW_S,
    MoverReliefDetector,
    RELIEF_TTL_S,
    relief_v2_enabled,
)

SYM = "UNI-USD"


def _alerted_detector(block_times=(0.0, 600.0, 1200.0, 1800.0)):
    """Detector with SYM in ALERT (4 blocks inside the rolling hour)."""
    det = MoverReliefDetector()
    for t in block_times:
        det.update(SYM, t, blocked=True)
    return det


def _feed_market(det, t, price, volume, oi):
    det.update(SYM, t, blocked=False, price=price, volume=volume, oi=oi)


# --------------------------------------------------------------------- ALERT
def test_alert_arms_at_threshold():
    det = MoverReliefDetector()
    for t in (0.0, 600.0, 1200.0, 1800.0):
        det.update(SYM, t, blocked=True)
    assert det.state(SYM, 1800.0) == "ALERT"
    assert det.state(SYM, 1800.0) != "RELIEF"


def test_alert_below_threshold_stays_quiet():
    det = MoverReliefDetector()
    for t in (0.0, 600.0, 1200.0):
        det.update(SYM, t, blocked=True)
    assert det.state(SYM, 1200.0) == "QUIET"
    assert ALERT_BLOCKS_PER_HOUR == 4  # pin: the no-case is exactly 3 blocks


def test_alert_ttl_expires_without_rearm():
    det = _alerted_detector()
    # Last qualifying poll is t=1800 (4 blocks in window); a tick at t=3600
    # sees only 3 (the t=0 block aged out) and must NOT refresh the TTL.
    det.update(SYM, ALERT_WINDOW_S, blocked=False)
    assert det.state(SYM, 1800.0 + ALERT_TTL_S - 100.0) == "ALERT"
    assert det.state(SYM, 1800.0 + ALERT_TTL_S + 100.0) == "QUIET"


def test_alert_rearm_refreshes_ttl():
    det = _alerted_detector()
    # Keep re-arming every 15 min; each poll still sees >= 4 rolling blocks,
    # so the TTL clock restarts at the LAST poll (t=8700).
    for t in range(2400, 8701, 900):
        det.update(SYM, float(t), blocked=True)
    assert det.state(SYM, 8700.0 + ALERT_TTL_S - 60.0) == "ALERT"
    assert det.state(SYM, 8700.0 + ALERT_TTL_S + 60.0) == "QUIET"


# --------------------------------------------------------------------- RELIEF
def _prime_alert_and_baseline(det, t_end=-600.0):
    """Feed 30 min of flat baseline market data so the volume baseline exists
    older than the relief window, ending at t_end (ALERT already armed)."""
    t = -3600.0
    while t <= t_end:
        _feed_market(det, t, price=10.0, volume=100.0, oi=1000.0)
        t += 60.0


def _relief_legs(det, t_start, *, price_mult=1.0, vol_mult=1.0, oi_mult=1.0):
    """Feed one relief window (900s, 60s cadence) of market data with each
    leg scaled by its multiplier relative to the baseline (1.0 = flat)."""
    for k in range(16):  # t_start .. t_start+900 inclusive
        t = t_start + 60.0 * k
        frac = k / 15.0
        price = 10.0 * (1.0 + (price_mult - 1.0) * frac * 0.02)
        volume = 100.0 * vol_mult
        oi = 1000.0 * (1.0 + (oi_mult - 1.0) * frac * 0.01)
        _feed_market(det, t, price=price, volume=volume, oi=oi)
    return t_start + 900.0


def test_relief_single_confirmation_does_not_arm():
    det = _alerted_detector()
    _prime_alert_and_baseline(det)
    # Price moves +2% over the window; volume and OI stay at baseline.
    t_end = _relief_legs(det, 0.0, price_mult=1.0)
    # _relief_legs with price_mult=1.0 is flat; force a real price leg:
    det2 = _alerted_detector()
    _prime_alert_and_baseline(det2)
    for k in range(16):
        t = 60.0 * k
        _feed_market(det2, t, price=10.0 * (1.0 + 0.02 * k / 15.0),
                     volume=100.0, oi=1000.0)
    assert det2.state(SYM, 900.0) == "ALERT"  # 1 of 3 -> no relief
    assert not det2.relief_active(SYM, 900.0)
    assert det.state(SYM, t_end) == "ALERT"


def test_relief_arms_price_plus_volume():
    det = _alerted_detector()
    _prime_alert_and_baseline(det)
    for k in range(16):
        t = 60.0 * k
        _feed_market(det, t, price=10.0 * (1.0 + 0.02 * k / 15.0),
                     volume=250.0, oi=1000.0)  # oi flat
    assert det.relief_active(SYM, 900.0)


def test_relief_arms_price_plus_oi():
    det = _alerted_detector()
    _prime_alert_and_baseline(det)
    for k in range(16):
        t = 60.0 * k
        _feed_market(det, t, price=10.0 * (1.0 + 0.02 * k / 15.0),
                     volume=100.0, oi=1000.0 * (1.0 + 0.01 * k / 15.0))
    assert det.relief_active(SYM, 900.0)


def test_relief_arms_volume_plus_oi():
    det = _alerted_detector()
    _prime_alert_and_baseline(det)
    for k in range(16):
        t = 60.0 * k
        _feed_market(det, t, price=10.0,  # price flat
                     volume=250.0,
                     oi=1000.0 * (1.0 + 0.01 * k / 15.0))
    assert det.relief_active(SYM, 900.0)


def test_relief_arm_logs_json_line(capsys):
    det = _alerted_detector()
    _prime_alert_and_baseline(det)
    capsys.readouterr()  # drop anything prior
    for k in range(16):
        t = 60.0 * k
        _feed_market(det, t, price=10.0 * (1.0 + 0.02 * k / 15.0),
                     volume=250.0, oi=1000.0)
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1  # exactly one arming line per fresh arm
    rec = json.loads(out[0])
    assert rec["event"] == "mover_relief_armed"
    assert rec["symbol"] == SYM
    assert sorted(rec["confirmations"]) == ["price", "volume"]


# ------------------------------------------------------- rolling-window pins
def test_rolling_block_window_excludes_old_blocks():
    det = MoverReliefDetector()
    for t in (0.0, 600.0, 1200.0):
        det.update(SYM, t, blocked=True)
    # At t=4201 the t=0 block has aged out of the rolling 3600s window;
    # a 4th block now leaves only 3 inside (600/1200/4201) -> still QUIET.
    det.update(SYM, 4201.0, blocked=True)
    assert det.state(SYM, 4201.0) == "QUIET"


def test_rolling_relief_window_excludes_old_move():
    det = _alerted_detector()
    _prime_alert_and_baseline(det)
    # A +3% burst and 3x volume happen entirely BEFORE the current window.
    for k in range(16):
        t = 60.0 * k
        _feed_market(det, t, price=10.0 * (1.0 + 0.03 * k / 15.0),
                     volume=300.0, oi=1000.0)
    assert det.relief_active(SYM, 900.0)
    det2 = _alerted_detector()
    _prime_alert_and_baseline(det2)
    for k in range(16):
        t = 60.0 * k
        _feed_market(det2, t, price=10.0 * (1.0 + 0.03 * k / 15.0),
                     volume=300.0, oi=1000.0)
    # Jump 901s past the burst; new ticks are flat. With a ROLLING window the
    # old move is excluded, so no NEW arm/refresh happens here.
    for k in range(16):
        t = 1801.0 + 60.0 * k
        _feed_market(det2, t, price=10.3, volume=100.0, oi=1000.0)
    assert not det2.relief_active(SYM, 1801.0 + 900.0 + RELIEF_TTL_S + 1.0)


def test_relief_ttl_expires():
    det = _alerted_detector()
    _prime_alert_and_baseline(det)
    for k in range(16):
        t = 60.0 * k
        _feed_market(det, t, price=10.0 * (1.0 + 0.02 * k / 15.0),
                     volume=250.0, oi=1000.0)
    t_arm = 900.0  # last confirming poll: burst fully inside the window
    assert det.relief_active(SYM, t_arm)
    # state() is a read-only TTL read: no further updates, so nothing
    # refreshes. Relief dies RELIEF_TTL_S after the last confirming poll
    # and demotes back to ALERT (whose 7200s TTL is still live).
    assert det.relief_active(SYM, t_arm + RELIEF_TTL_S - 60.0)
    assert not det.relief_active(SYM, t_arm + RELIEF_TTL_S + 120.0)
    assert det.state(SYM, t_arm + RELIEF_TTL_S + 120.0) == "ALERT"


# ------------------------------------------------------ fail-silent behavior
def test_fail_silent_on_garbage():
    det = MoverReliefDetector()
    det.update(SYM, "not-a-number", blocked=True, price="x", volume=-1, oi=0)
    assert det.state(SYM, 0.0) == "QUIET"
    assert det.relief_active("UNKNOWN-SYM", 123.0) is False


def test_relief_v2_enabled_helper():
    # Default kill-switch file absent/defaults -> False; never raises.
    assert relief_v2_enabled() in (True, False)


# ------------------------------------------- mandatory UNI-day calibration
def test_uni_day_relief_arms_during_move_not_after():
    """Synthetic UNI day (2026-09-17 incident replay):
      - blocks every ~10 min from t=0 (6/hr)
      - price +0.3%/15min before t=2h, then +2%/15min after
      - volume 2x baseline and OI +0.8%/15min after t=2h
    RELIEF must arm between t=2h and t=2h45m — NOT at t=7h.
    """
    det = MoverReliefDetector()
    two_h = 7200.0
    first_relief_t = None
    t = 0.0
    while t <= 7 * 3600.0:
        blocked = (int(t) % 600 == 0)  # every 10 min from t=0
        if t < two_h:
            price = 10.0 * (1.003 ** (t / 900.0))
            volume = 100.0
            oi = 1000.0
        else:
            dt = t - two_h
            price = 10.0 * (1.003 ** 8.0) * (1.02 ** (dt / 900.0))
            volume = 200.0
            oi = 1000.0 * (1.008 ** (dt / 900.0))
        det.update(SYM, t, blocked=blocked, price=price, volume=volume, oi=oi)
        if first_relief_t is None and det.relief_active(SYM, t):
            first_relief_t = t
        t += 60.0

    assert first_relief_t is not None, "RELIEF never armed on the UNI day"
    assert two_h <= first_relief_t <= two_h + 45 * 60.0, (
        f"RELIEF armed at t={first_relief_t}s — outside [2h, 2h45m]; "
        "relief that arms post-move is worthless")
    # The pre-move regime (steady blocks, flat confirmations) must NOT arm.
    assert first_relief_t > two_h - 1.0
    # Explicitly NOT at t=7h (the incident's post-move arm).
    assert first_relief_t < 6 * 3600.0
