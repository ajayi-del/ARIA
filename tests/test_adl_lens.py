"""tests/test_adl_lens.py — pins for intelligence/adl_lens.py (AdlLens).

Doctrine: estimator-not-a-feed, advisory-only, >= binds at every rung,
master gate False = None verdicts with the staged profile reduced to the
small-position shape.
"""
from types import SimpleNamespace

from intelligence.adl_lens import AdlLens


def cfg(**kw):
    return SimpleNamespace(**kw)


LENS = AdlLens()


# ── exposure estimator ──────────────────────────────────────────────────────

def test_exposure_full_legs_score_one():
    # uPnL $50 at 38x → legs 1.0 × 1.0 → 1.0
    s = LENS.adl_exposure_score(cfg(), unrealized_pnl_usd=50.0, leverage=38.0,
                                position_notional_usd=1000.0)
    assert s == 1.0


def test_exposure_half_legs_score_quarter():
    # uPnL $25 at 19x → 0.5 × 0.5 → 0.25
    s = LENS.adl_exposure_score(cfg(), unrealized_pnl_usd=25.0, leverage=19.0,
                                position_notional_usd=1000.0)
    assert abs(s - 0.25) < 1e-12


def test_exposure_negative_upnl_zero():
    s = LENS.adl_exposure_score(cfg(), unrealized_pnl_usd=-12.5, leverage=20.0,
                                position_notional_usd=1000.0)
    assert s == 0.0


def test_exposure_zero_leverage_none():
    assert LENS.adl_exposure_score(cfg(), unrealized_pnl_usd=50.0,
                                   leverage=0.0,
                                   position_notional_usd=1000.0) is None


def test_exposure_non_numeric_none():
    assert LENS.adl_exposure_score(cfg(), unrealized_pnl_usd="fifty",
                                   leverage=38.0,
                                   position_notional_usd=1000.0) is None
    assert LENS.adl_exposure_score(cfg(), unrealized_pnl_usd=50.0,
                                   leverage=None,
                                   position_notional_usd=1000.0) is None


def test_exposure_zero_notional_none():
    assert LENS.adl_exposure_score(cfg(), unrealized_pnl_usd=50.0,
                                   leverage=38.0,
                                   position_notional_usd=0.0) is None


def test_exposure_clamps_over_reference():
    # uPnL $500 (10× ref) at 100x (clamped) → still 1.0
    s = LENS.adl_exposure_score(cfg(), unrealized_pnl_usd=500.0,
                                leverage=100.0, position_notional_usd=1000.0)
    assert s == 1.0


def test_exposure_rides_knobs():
    # refs halved → $25 at 19x now saturates both legs → 1.0
    c = cfg(adl_upnl_ref_usd=25.0, adl_leverage_ref=19.0)
    s = LENS.adl_exposure_score(c, unrealized_pnl_usd=25.0, leverage=19.0,
                                position_notional_usd=1000.0)
    assert s == 1.0


# ── partial-profit verdict ──────────────────────────────────────────────────

def test_verdict_fires_at_exact_rungs():
    # score 0.70 exactly + roe 80 exactly → advisory fires (>= binds, pinned)
    v = LENS.adl_partial_profit_verdict(cfg(), symbol="BTC-USD", side="long",
                                        score=0.70, roe_pct=80.0)
    assert v is not None
    assert v["action"] == "bank_partial_and_prepare_cross_side"
    assert v["symbol"] == "BTC-USD"
    assert v["side"] == "long"
    assert v["score"] == 0.70
    assert v["roe_pct"] == 80.0
    assert v["partial_frac"] == 0.50


def test_verdict_score_below_rung_holds():
    v = LENS.adl_partial_profit_verdict(cfg(), symbol="BTC-USD", side="long",
                                        score=0.69, roe_pct=200.0)
    assert v["action"] == "hold"
    assert "partial_frac" not in v


def test_verdict_roe_below_rung_holds():
    v = LENS.adl_partial_profit_verdict(cfg(), symbol="BTC-USD", side="long",
                                        score=1.0, roe_pct=79.0)
    assert v["action"] == "hold"


def test_verdict_degenerate_none():
    assert LENS.adl_partial_profit_verdict(
        cfg(), symbol="BTC-USD", side="long", score="high",
        roe_pct=80.0) is None
    assert LENS.adl_partial_profit_verdict(
        cfg(), symbol="BTC-USD", side="flat", score=0.9, roe_pct=90.0) is None
    assert LENS.adl_partial_profit_verdict(
        cfg(), symbol=None, side="long", score=0.9, roe_pct=90.0) is None


def test_verdict_partial_frac_rides_knob():
    c = cfg(adl_partial_frac=0.25)
    v = LENS.adl_partial_profit_verdict(c, symbol="ETH-USD", side="short",
                                        score=0.9, roe_pct=120.0)
    assert v["action"] == "bank_partial_and_prepare_cross_side"
    assert v["partial_frac"] == 0.25


def test_verdict_warn_rungs_ride_knobs():
    c = cfg(adl_score_warn=0.50, adl_roe_warn=50.0)
    v = LENS.adl_partial_profit_verdict(c, symbol="ETH-USD", side="short",
                                        score=0.50, roe_pct=50.0)
    assert v["action"] == "bank_partial_and_prepare_cross_side"


# ── observed-event signal ───────────────────────────────────────────────────

def test_event_long_liquidated_counter_short():
    v = LENS.adl_event_signal(cfg(), symbol="BTC-USD", liquidated_side="long")
    assert v == {"signal": "mean_reversion_watch", "counter_side": "short",
                 "symbol": "BTC-USD", "strength": "normal"}


def test_event_short_liquidated_counter_long():
    v = LENS.adl_event_signal(cfg(), symbol="BTC-USD", liquidated_side="short")
    assert v["counter_side"] == "long"


def test_event_big_notional_strength_high():
    v = LENS.adl_event_signal(cfg(), symbol="BTC-USD", liquidated_side="long",
                              event_notional_usd=250000.0)
    assert v["strength"] == "high"


def test_event_boundary_exactly_at_knob_is_high():
    # >= binds, DEFINED and pinned: exactly the staged-liquidation size class
    # reads "high".
    v = LENS.adl_event_signal(cfg(), symbol="BTC-USD", liquidated_side="long",
                              event_notional_usd=100000.0)
    assert v["strength"] == "high"


def test_event_just_below_knob_normal():
    v = LENS.adl_event_signal(cfg(), symbol="BTC-USD", liquidated_side="long",
                              event_notional_usd=99999.99)
    assert v["strength"] == "normal"


def test_event_missing_notional_normal():
    v = LENS.adl_event_signal(cfg(), symbol="BTC-USD", liquidated_side="long",
                              event_notional_usd=None)
    assert v["strength"] == "normal"


def test_event_bad_side_none():
    assert LENS.adl_event_signal(cfg(), symbol="BTC-USD",
                                 liquidated_side="flat") is None
    assert LENS.adl_event_signal(cfg(), symbol="BTC-USD",
                                 liquidated_side=None) is None


# ── staged-liquidation profile ──────────────────────────────────────────────

def test_staged_large_position():
    v = LENS.staged_liquidation_profile(cfg(), position_notional_usd=150000.0)
    assert v == {">100k": True, "first_tranche_frac": 0.20, "cooldown_s": 30}


def test_staged_small_position():
    v = LENS.staged_liquidation_profile(cfg(), position_notional_usd=50000.0)
    assert v == {">100k": False}


def test_staged_boundary_exactly_at_threshold_is_small():
    # strictly OVER the threshold stages; exactly 100k reads small (pinned)
    v = LENS.staged_liquidation_profile(cfg(), position_notional_usd=100000.0)
    assert v == {">100k": False}


def test_staged_threshold_rides_knob():
    c = cfg(adl_staged_threshold_usd=10000.0)
    v = LENS.staged_liquidation_profile(c, position_notional_usd=50000.0)
    assert v[">100k"] is True


# ── master gate ─────────────────────────────────────────────────────────────

def test_master_gate_false_none_verdicts():
    c = cfg(adl_lens_enabled=False)
    assert LENS.adl_exposure_score(c, unrealized_pnl_usd=50.0, leverage=38.0,
                                   position_notional_usd=1000.0) is None
    assert LENS.adl_partial_profit_verdict(c, symbol="BTC-USD", side="long",
                                           score=1.0, roe_pct=200.0) is None
    assert LENS.adl_event_signal(c, symbol="BTC-USD",
                                 liquidated_side="long",
                                 event_notional_usd=250000.0) is None


def test_master_gate_false_staged_returns_small_shape():
    # pure venue-mechanics shape — no doctrine, answered even when gated
    c = cfg(adl_lens_enabled=False)
    v = LENS.staged_liquidation_profile(c, position_notional_usd=150000.0)
    assert v == {">100k": False}
