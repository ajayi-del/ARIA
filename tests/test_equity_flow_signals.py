"""tests/test_equity_flow_signals.py — Axiom 7 equity-flow plane pins.

No network, no file I/O. Publisher tests use a fake set_param recorder.
Kill-switch tests use monkeypatched env (module reads env at call time).
"""
import pytest

from intelligence.equity_flow_signals import (
    DEFAULT_TILT,
    MOMENTUM_TTL_S,
    ROTATION_TILT,
    ROTATION_TTL_S,
    WINDOW_MAX_S,
    WINDOW_MIN_S,
    EquityFlowPublisher,
    EquityFlowVerdict,
    coin_lead_signal,
    equity_flow_signals_enabled,
    equity_flow_verdict,
    mstr_squeeze_signal,
    rotation_signal,
)


@pytest.fixture
def kill_off(monkeypatch):
    monkeypatch.setenv("EQUITY_FLOW_SIGNALS_ENABLED", "false")


# ── 1. COIN lead boundary ─────────────────────────────────────────────────────

class TestCoinLead:
    def test_spread_099_not_armed(self):
        # coin - btc = 0.99 < 1.0 -> no signal
        armed, conf, win = coin_lead_signal(2.49, 1.50)
        assert armed is False
        assert conf == 0.0
        assert win is None

    def test_spread_101_armed(self):
        # coin - btc = 1.01 > 1.0 -> signal
        armed, conf, win = coin_lead_signal(2.51, 1.50)
        assert armed is True
        assert conf > 0.0
        assert WINDOW_MIN_S <= win <= WINDOW_MAX_S

    def test_spread_exactly_100_not_armed(self):
        # strict > doctrine: 1.00 exactly does not arm
        armed, _, _ = coin_lead_signal(2.50, 1.50)
        assert armed is False

    def test_confidence_flat_btc_div_guard(self):
        # flat BTC: denominator floors at 1.0 (adaptation — never divides by 0)
        armed, conf, win = coin_lead_signal(2.0, 0.0)
        assert armed is True
        assert conf == 1.0  # 2.0 / 1.0, clamped

    def test_confidence_deflated_by_btc_move(self):
        # spread 4.0, |btc| = 8.0 -> conf = 4/8 = 0.5
        armed, conf, _ = coin_lead_signal(-4.0, -8.0)
        assert armed is True
        assert conf == pytest.approx(0.5)

    def test_confidence_clamped_to_one(self):
        armed, conf, _ = coin_lead_signal(50.0, 0.5)
        assert armed is True
        assert conf == 1.0

    def test_window_bounds(self):
        # minimal arming spread vs flat BTC -> conf ~ 1.0 -> near max window
        _, conf_hi, win_hi = coin_lead_signal(1.01, 0.0)
        assert win_hi <= WINDOW_MAX_S
        # spread < |btc| deflates confidence -> window pulls toward the 3h floor
        _, conf_lo, win_lo = coin_lead_signal(12.0, 10.0)  # spread 2 / denom 10 = 0.2
        assert WINDOW_MIN_S <= win_lo <= WINDOW_MAX_S
        assert conf_lo == pytest.approx(0.2)
        assert conf_lo < conf_hi
        assert win_lo < win_hi

    def test_window_floor_reachable(self):
        # conf -> 0 forces window -> WINDOW_MIN_S (3h)
        armed, conf, win = coin_lead_signal(1.000001 + 1e9, 1e9)
        assert armed is True
        assert conf < 0.01
        assert win == pytest.approx(WINDOW_MIN_S, abs=WINDOW_MAX_S * 0.01)

    def test_none_inputs_abstain(self):
        assert coin_lead_signal(None, 1.5) == (False, 0.0, None)
        assert coin_lead_signal(2.5, None) == (False, 0.0, None)
        assert coin_lead_signal("garbage", 1.5) == (False, 0.0, None)
        assert coin_lead_signal(float("nan"), 1.5) == (False, 0.0, None)


# ── 2. MSTR squeeze ───────────────────────────────────────────────────────────

class TestMstrSqueeze:
    def test_149x_not_armed(self):
        assert mstr_squeeze_signal(14.9, 10.0) is False

    def test_151x_armed(self):
        assert mstr_squeeze_signal(15.1, 10.0) is True

    def test_150x_exactly_not_armed(self):
        assert mstr_squeeze_signal(15.0, 10.0) is False  # strict >

    def test_avg_zero_abstains(self):
        assert mstr_squeeze_signal(100.0, 0.0) is False

    def test_avg_negative_abstains(self):
        assert mstr_squeeze_signal(100.0, -5.0) is False

    def test_avg_none_abstains(self):
        assert mstr_squeeze_signal(100.0, None) is False

    def test_current_none_abstains(self):
        assert mstr_squeeze_signal(None, 10.0) is False


# ── 3. Rotation ───────────────────────────────────────────────────────────────

class TestRotation:
    def test_both_legs_arm(self):
        # mean(-1.0, -0.5) = -0.75 < -0.5 AND coin > 0
        assert rotation_signal(-1.0, -0.5, 0.5) is True

    def test_tech_up_coin_up_no_rotation(self):
        # the negative leg: tech UP + COIN up -> no rotation
        assert rotation_signal(1.0, 0.5, 2.0) is False

    def test_tech_down_coin_down_no_rotation(self):
        assert rotation_signal(-1.0, -0.5, -0.2) is False

    def test_tech_down_coin_flat_no_rotation(self):
        assert rotation_signal(-1.0, -0.5, 0.0) is False  # strict coin > 0

    def test_mean_boundary(self):
        # mean exactly -0.5 -> not < -0.5 -> no rotation
        assert rotation_signal(-0.5, -0.5, 1.0) is False

    def test_none_inputs_abstain(self):
        assert rotation_signal(None, -1.0, 1.0) is False
        assert rotation_signal(-1.0, None, 1.0) is False
        assert rotation_signal(-1.0, -1.0, None) is False


# ── 4. Composite verdict ─────────────────────────────────────────────────────

class TestComposite:
    def test_all_legs_armed(self):
        v = equity_flow_verdict(
            coin_change_pct=3.0, btc_change_pct=1.0,          # lead armed
            googl_change_pct=-1.0, amzn_change_pct=-1.0,      # rotation tech leg
            mstr_funding_current_bps=16.0, mstr_funding_avg_bps=10.0)
        assert v.btc_long_signal is True
        assert v.btc_long_confidence > 0.0
        assert WINDOW_MIN_S <= v.btc_long_window_s <= WINDOW_MAX_S
        assert v.btc_momentum_squeeze is True
        assert v.rotation_signal is True
        assert v.size_tilt == ROTATION_TILT
        assert "coin_leads_btc" in v.notes
        assert "mstr_squeeze" in v.notes
        assert "bigtech_to_crypto_rotation" in v.notes

    def test_all_neutral(self):
        v = equity_flow_verdict(
            coin_change_pct=0.5, btc_change_pct=0.6,
            googl_change_pct=1.0, amzn_change_pct=1.0,
            mstr_funding_current_bps=10.0, mstr_funding_avg_bps=10.0)
        assert v == EquityFlowVerdict()
        assert v.size_tilt == DEFAULT_TILT
        assert v.btc_long_window_s is None
        assert v.notes == ()

    def test_partial_none_inputs_legs_abstain_independently(self):
        # COIN lead data present; rotation + squeeze inputs missing
        v = equity_flow_verdict(coin_change_pct=3.0, btc_change_pct=1.0)
        assert v.btc_long_signal is True
        assert v.btc_momentum_squeeze is False
        assert v.rotation_signal is False
        assert v.size_tilt == DEFAULT_TILT

    def test_all_none_never_raises(self):
        v = equity_flow_verdict()
        assert v == EquityFlowVerdict()

    def test_garbage_inputs_never_raise(self):
        v = equity_flow_verdict(coin_change_pct="x", btc_change_pct=object(),
                                googl_change_pct=float("nan"))
        assert v == EquityFlowVerdict()

    def test_size_tilt_only_on_rotation(self):
        v = equity_flow_verdict(coin_change_pct=0.5, btc_change_pct=5.0,
                                googl_change_pct=-2.0, amzn_change_pct=-1.0)
        # rotation legs hold while COIN trails BTC (no lead signal)
        assert v.rotation_signal is True
        assert v.size_tilt == ROTATION_TILT
        assert v.btc_long_signal is False

    def test_frozen(self):
        v = equity_flow_verdict()
        with pytest.raises(Exception):
            v.size_tilt = 9.9


# ── 5. Publisher ─────────────────────────────────────────────────────────────

class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, key, value, ttl):
        self.calls.append((key, value, ttl))


class TestPublisher:
    def test_writes_only_armed_keys(self):
        rec = _Recorder()
        pub = EquityFlowPublisher(rec, clock=lambda: 123.0)
        v = equity_flow_verdict(
            coin_change_pct=3.0, btc_change_pct=1.0,
            googl_change_pct=1.0, amzn_change_pct=1.0,       # rotation OFF
            mstr_funding_current_bps=16.0, mstr_funding_avg_bps=10.0)
        n = pub.publish(v)
        assert n == 2
        keys = [c[0] for c in rec.calls]
        assert keys == ["equity_flow:btc_long", "equity_flow:btc_momentum"]
        # btc_long TTL = the verdict window
        assert rec.calls[0][2] == v.btc_long_window_s
        assert rec.calls[0][1]["confidence"] == v.btc_long_confidence
        # momentum TTL = 4h
        assert rec.calls[1][2] == MOMENTUM_TTL_S

    def test_rotation_key_and_tilt_value(self):
        rec = _Recorder()
        pub = EquityFlowPublisher(rec)
        v = equity_flow_verdict(coin_change_pct=0.5, btc_change_pct=0.0,
                                googl_change_pct=-1.0, amzn_change_pct=-1.0)
        n = pub.publish(v)
        assert n == 1
        key, value, ttl = rec.calls[0]
        assert key == "equity_flow:rotation_tilt"
        assert value["size_tilt"] == ROTATION_TILT
        assert ttl == ROTATION_TTL_S

    def test_all_neutral_writes_nothing(self):
        rec = _Recorder()
        pub = EquityFlowPublisher(rec)
        n = pub.publish(equity_flow_verdict())
        assert n == 0
        assert rec.calls == []

    def test_none_verdict_noop(self):
        rec = _Recorder()
        pub = EquityFlowPublisher(rec)
        assert pub.publish(None) == 0
        assert rec.calls == []

    def test_set_param_failure_never_raises(self):
        def boom(key, value, ttl):
            raise RuntimeError("store down")
        pub = EquityFlowPublisher(boom)
        v = equity_flow_verdict(coin_change_pct=3.0, btc_change_pct=0.0)
        assert pub.publish(v) == 0  # swallowed, no exception


# ── 6. Kill switch ────────────────────────────────────────────────────────────

class TestKillSwitch:
    def test_default_enabled(self, monkeypatch):
        monkeypatch.delenv("EQUITY_FLOW_SIGNALS_ENABLED", raising=False)
        assert equity_flow_signals_enabled() is True

    def test_disabled_flag(self, kill_off):
        assert equity_flow_signals_enabled() is False

    def test_verdict_all_neutral_with_kill_switch_note(self, kill_off):
        v = equity_flow_verdict(
            coin_change_pct=3.0, btc_change_pct=1.0,
            googl_change_pct=-1.0, amzn_change_pct=-1.0,
            mstr_funding_current_bps=20.0, mstr_funding_avg_bps=10.0)
        assert v.btc_long_signal is False
        assert v.btc_momentum_squeeze is False
        assert v.rotation_signal is False
        assert v.size_tilt == DEFAULT_TILT
        assert "kill_switch" in v.notes

    def test_publish_noop_when_killed(self, kill_off):
        rec = _Recorder()
        pub = EquityFlowPublisher(rec)
        v = EquityFlowVerdict(btc_long_signal=True, btc_long_confidence=0.5,
                              btc_long_window_s=WINDOW_MIN_S,
                              btc_momentum_squeeze=True, rotation_signal=True,
                              size_tilt=ROTATION_TILT)
        assert pub.publish(v) == 0
        assert rec.calls == []
