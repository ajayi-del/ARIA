"""Campaign book brain: membership, margin budget, daily counter, hedge reserve,
family hedge verdicts, self-portfolio handoff — doctrine pins."""
import os
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelligence.campaign_book import (  # noqa: E402
    campaign_members, is_campaign, campaign_min_notional, conviction_ladder,
    campaign_margin_budget, MarginBudget, CampaignDailyCounter, HedgeReserve,
    family_hedge_instrument, family_hedge_verdict, HedgeVerdict,
    self_portfolio_handoff, DEFAULT_FAMILY_HEDGE_MAP,
)


def _cfg(**kw):
    """Minimal cfg stub — campaign_book reads everything via getattr defaults."""
    return SimpleNamespace(**kw)


# ── 1. Membership ────────────────────────────────────────────────────────────

def test_members_master_gate_off_empty():
    cfg = _cfg(campaign_mode_enabled=False, campaign_symbol="SPCX-USD",
               campaign_multi_symbol_enabled=True,
               campaign_symbols=["SPCX-USD", "ETH-USD"])
    assert campaign_members(cfg) == frozenset()
    assert not is_campaign(cfg, "SPCX-USD")


def test_members_multi_enabled_with_symbols_list():
    cfg = _cfg(campaign_mode_enabled=True,
               campaign_multi_symbol_enabled=True,
               campaign_symbols=["SPCX-USD", "ETH-USD", "XRP-USD"],
               campaign_symbol="SPCX-USD")
    m = campaign_members(cfg)
    assert m == frozenset({"SPCX-USD", "ETH-USD", "XRP-USD"})
    assert is_campaign(cfg, "ETH-USD")
    assert not is_campaign(cfg, "BTC-USD")


def test_members_multi_enabled_empty_list_falls_back_legacy():
    cfg = _cfg(campaign_mode_enabled=True,
               campaign_multi_symbol_enabled=True,
               campaign_symbols=[],
               campaign_symbol="SPCX-USD")
    assert campaign_members(cfg) == frozenset({"SPCX-USD"})
    assert is_campaign(cfg, "SPCX-USD")
    assert not is_campaign(cfg, "ETH-USD")


def test_members_multi_disabled_legacy_single():
    cfg = _cfg(campaign_mode_enabled=True,
               campaign_multi_symbol_enabled=False,
               campaign_symbols=["SPCX-USD", "ETH-USD"],
               campaign_symbol="SPCX-USD")
    assert campaign_members(cfg) == frozenset({"SPCX-USD"})
    assert not is_campaign(cfg, "ETH-USD")


# ── 2. Venue-aware floors ────────────────────────────────────────────────────

def test_min_notional_aster_floor():
    cfg = _cfg()
    assert campaign_min_notional(cfg, "UNI-USD", "aster") == 3.0


def test_min_notional_sodex_floor():
    cfg = _cfg()
    assert campaign_min_notional(cfg, "SPCX-USD", "sodex") == 250.0


def test_min_notional_cfg_overrides():
    cfg = _cfg(campaign_venue_min_notional_aster=5.0,
               campaign_min_notional_usd=100.0)
    assert campaign_min_notional(cfg, "X", "aster") == 5.0
    assert campaign_min_notional(cfg, "X", "sodex") == 100.0


# ── 3. Conviction ladder boundaries ──────────────────────────────────────────

def test_ladder_at_4_5_full_conviction():
    assert conviction_ladder(4.5) == 1.0
    assert conviction_ladder(9.7) == 1.0


def test_ladder_at_3_0_mid_conviction():
    assert conviction_ladder(3.0) == 0.75
    assert conviction_ladder(4.49) == 0.75


def test_ladder_below_3_0_base_conviction():
    assert conviction_ladder(2.99) == 0.5
    assert conviction_ladder(0.0) == 0.5


# ── 4. Margin budget ─────────────────────────────────────────────────────────

def _budget_cfg(**kw):
    base = dict(campaign_margin_budget_enabled=True, campaign_leverage=10,
                campaign_margin_frac_base=0.12,
                campaign_stop_risk_clamp_pct=0.06,
                campaign_symbol_margin_cap=0.15,
                campaign_book_margin_cap=0.50)
    base.update(kw)
    return _cfg(**base)


def test_budget_base_math():
    b = campaign_margin_budget(
        _budget_cfg(), coherence=5.0, sleeve_equity=1000.0,
        combined_equity=2000.0, stop_dist_pct=0.01,
        open_campaign_margin=0.0, symbol_open_margin=0.0,
        min_notional=250.0)
    assert not b.standdown
    assert b.margin == 120.0            # 0.12 * 1000 * ladder(5.0)=1.0
    assert b.notional == 960.0          # 120 * 8
    assert b.leverage == 8              # min(campaign_leverage=10, 8)
    assert b.clamp_reasons == ()


def test_budget_stop_risk_clamp_non_binding_at_tight_stop():
    # stop 0.01 → clamp 0.06*1000/0.01 = 6000, far above base 120
    b = campaign_margin_budget(
        _budget_cfg(), coherence=5.0, sleeve_equity=1000.0,
        combined_equity=2000.0, stop_dist_pct=0.01,
        open_campaign_margin=0.0, symbol_open_margin=0.0,
        min_notional=250.0)
    assert "stop_risk_clamped" not in b.clamp_reasons


def test_budget_stop_risk_clamp_binding():
    # stop 0.06 → clamp 0.06*1000/0.06/lev8 = 125; frac_base 0.15 puts base 150 above it
    b = campaign_margin_budget(
        _budget_cfg(campaign_margin_frac_base=0.15), coherence=5.0,
        sleeve_equity=1000.0, combined_equity=2000.0, stop_dist_pct=0.06,
        open_campaign_margin=0.0, symbol_open_margin=0.0,
        min_notional=250.0)
    assert b.margin == 125.0
    assert "stop_risk_clamped" in b.clamp_reasons
    assert not b.standdown              # 125*8=1000 ≥ 250


def test_budget_unknown_stop_reads_as_max_clamp_fail_closed():
    # stop_dist_pct=None → distance 1.0 → clamp = 0.06*1000/1.0/8 = 7.5 →
    # notional 60 < floor 250 → fail-closed standdown (unknown stop = no trade)
    b = campaign_margin_budget(
        _budget_cfg(), coherence=5.0, sleeve_equity=1000.0,
        combined_equity=2000.0, stop_dist_pct=None,
        open_campaign_margin=0.0, symbol_open_margin=0.0,
        min_notional=250.0)
    assert b.margin == 7.5
    assert "stop_risk_clamped" in b.clamp_reasons
    assert b.standdown


def test_budget_symbol_cap_clamped():
    # sym cap = 0.15*1000 - 100 = 50 < base 120
    b = campaign_margin_budget(
        _budget_cfg(), coherence=5.0, sleeve_equity=1000.0,
        combined_equity=2000.0, stop_dist_pct=0.01,
        open_campaign_margin=0.0, symbol_open_margin=100.0,
        min_notional=250.0)
    assert b.margin == 50.0
    assert "symbol_cap_clamped" in b.clamp_reasons
    assert not b.standdown              # 50*8=400 ≥ 250


def test_budget_book_cap_clamped():
    # book cap = 0.5*2000 - 900 = 100 < base 120
    b = campaign_margin_budget(
        _budget_cfg(), coherence=5.0, sleeve_equity=1000.0,
        combined_equity=2000.0, stop_dist_pct=0.01,
        open_campaign_margin=900.0, symbol_open_margin=0.0,
        min_notional=250.0)
    assert b.margin == 100.0
    assert "book_cap_clamped" in b.clamp_reasons
    assert not b.standdown              # 100*8=800 ≥ 250


def test_budget_standdown_below_venue_min_notional():
    # base 120*8=960 < min_notional 1000 → affordability arbiter stands down
    b = campaign_margin_budget(
        _budget_cfg(), coherence=5.0, sleeve_equity=1000.0,
        combined_equity=2000.0, stop_dist_pct=0.01,
        open_campaign_margin=0.0, symbol_open_margin=0.0,
        min_notional=1000.0)
    assert b.standdown
    assert b.standdown_reason == "below_venue_min_notional"
    assert b.margin == 120.0            # budget still reported for telemetry


def test_budget_disabled_kill_switch():
    b = campaign_margin_budget(
        _budget_cfg(campaign_margin_budget_enabled=False), coherence=5.0,
        sleeve_equity=1000.0, combined_equity=2000.0, stop_dist_pct=0.01,
        open_campaign_margin=0.0, symbol_open_margin=0.0,
        min_notional=250.0)
    assert b.standdown
    assert b.standdown_reason == "budget_disabled"
    assert b.margin == 0.0 and b.notional == 0.0 and b.leverage == 0


def test_budget_leverage_clamped_at_8():
    b = campaign_margin_budget(
        _budget_cfg(campaign_leverage=10), coherence=5.0,
        sleeve_equity=1000.0, combined_equity=2000.0, stop_dist_pct=0.01,
        open_campaign_margin=0.0, symbol_open_margin=0.0,
        min_notional=250.0)
    assert b.leverage == 8


def test_budget_leverage_below_cap_passes_through():
    b = campaign_margin_budget(
        _budget_cfg(campaign_leverage=5), coherence=5.0,
        sleeve_equity=1000.0, combined_equity=2000.0, stop_dist_pct=0.01,
        open_campaign_margin=0.0, symbol_open_margin=0.0,
        min_notional=250.0)
    assert b.leverage == 5
    assert b.notional == 600.0


def test_budget_conviction_scales_budget():
    # coh 3.5 → ladder 0.75 → budget 0.12*1000*0.75 = 90
    b = campaign_margin_budget(
        _budget_cfg(), coherence=3.5, sleeve_equity=1000.0,
        combined_equity=2000.0, stop_dist_pct=0.01,
        open_campaign_margin=0.0, symbol_open_margin=0.0,
        min_notional=250.0)
    assert b.margin == 90.0
    assert not b.standdown              # 90*8=720 ≥ 250


# ── 5. Daily entry counter — UTC day roll ────────────────────────────────────

import calendar as _cal

# Deterministic UTC epochs across two different UTC days
EPOCH_D1 = 1790000000.0
_g1 = time.gmtime(EPOCH_D1)
EPOCH_D1 = float(_cal.timegm((_g1.tm_year, _g1.tm_mon, _g1.tm_mday, 12, 0, 0)))
EPOCH_D2 = EPOCH_D1 + 86400.0   # same clock time, next UTC day


def test_counter_increment_returns_running_count():
    c = CampaignDailyCounter()
    assert c.increment(EPOCH_D1) == 1
    assert c.increment(EPOCH_D1) == 2
    assert c.count(EPOCH_D1) == 2


def test_counter_resets_on_new_utc_day():
    c = CampaignDailyCounter()
    for _ in range(5):
        c.increment(EPOCH_D1)
    assert c.count(EPOCH_D2) == 0          # new day reads 0
    assert c.increment(EPOCH_D2) == 1      # and rolls the key on increment
    assert c.count(EPOCH_D2) == 1
    assert c.count(EPOCH_D1) == 0          # old day key is gone


def test_counter_allowed_against_daily_max():
    c = CampaignDailyCounter()
    cfg = _cfg(campaign_daily_entries_max=3)
    assert c.allowed(cfg, EPOCH_D1)
    c.increment(EPOCH_D1)
    c.increment(EPOCH_D1)
    assert c.allowed(cfg, EPOCH_D1)
    c.increment(EPOCH_D1)
    assert not c.allowed(cfg, EPOCH_D1)    # 3 >= 3
    assert c.allowed(cfg, EPOCH_D2)        # fresh day re-allows


def test_counter_default_max_20():
    c = CampaignDailyCounter()
    for _ in range(20):
        c.increment(EPOCH_D1)
    assert not c.allowed(_cfg(), EPOCH_D1)


# ── 6. Hedge reserve ledger ──────────────────────────────────────────────────

def _reserve_cfg(**kw):
    base = dict(campaign_book_margin_cap=0.50, campaign_hedge_reserve_frac=0.20)
    base.update(kw)
    return _cfg(**base)


def test_reserve_target_math():
    r = HedgeReserve()
    # target = 0.20 * (0.50 * 2000) = 200
    assert r.target(_reserve_cfg(), 2000.0) == 200.0
    assert r.available(_reserve_cfg(), 2000.0) == 200.0


def test_reserve_debit_reduces_available():
    r = HedgeReserve()
    r.debit(50.0)
    assert r.debited == 50.0
    assert r.available(_reserve_cfg(), 2000.0) == 150.0


def test_reserve_credit_restores_available():
    r = HedgeReserve()
    r.debit(50.0)
    r.credit(30.0)
    assert r.debited == 20.0
    assert r.available(_reserve_cfg(), 2000.0) == 180.0


def test_reserve_credit_floors_at_zero():
    r = HedgeReserve()
    r.debit(10.0)
    r.credit(500.0)
    assert r.debited == 0.0
    assert r.available(_reserve_cfg(), 2000.0) == 200.0


def test_reserve_rebuild_sets_debited():
    r = HedgeReserve()
    r.debit(50.0)
    r.rebuild(75.0)
    assert r.debited == 75.0
    assert r.available(_reserve_cfg(), 2000.0) == 125.0


# ── 7. Family hedge instrument map ───────────────────────────────────────────

def test_hedge_instrument_default_map():
    cfg = _cfg()
    assert family_hedge_instrument(cfg, "SPCX-USD") == "USTECH100-USD"
    assert family_hedge_instrument(cfg, "UNI-USD") == "ETH-USD"
    assert family_hedge_instrument(cfg, "ETH-USD") == "BTC-USD"


def test_hedge_instrument_unknown_symbol_none():
    assert family_hedge_instrument(_cfg(), "DOGE-USD") is None


def test_hedge_instrument_cfg_override_wins():
    cfg = _cfg(campaign_family_hedge_map={"AAA-USD": "BBB-USD"})
    assert family_hedge_instrument(cfg, "AAA-USD") == "BBB-USD"
    # override REPLACES the default map entirely
    assert family_hedge_instrument(cfg, "SPCX-USD") is None


# ── 8. Family hedge verdict ──────────────────────────────────────────────────

def _hedge_cfg(**kw):
    base = dict(campaign_family_hedge_enabled=True,
                campaign_family_hedge_map=None,
                campaign_hedge_max_harvests=4,
                campaign_hedge_cooloff_s=900.0,
                campaign_hedge_pain_frac=0.5,
                hedge_budget_stop_pct=0.02)
    base.update(kw)
    return _cfg(**base)

NOW = 1_790_000_000.0


def _verdict(cfg, **kw):
    base = dict(symbol="SPCX-USD", primary_side="long", entry_price=100.0,
                mark_price=109.5, stop_price=98.0, tp1_price=110.0,
                primary_qty=1.0,
                atr15=1.0, open_profit_frac=0.095, reserve_available=1000.0,
                harvests_done=0, last_harvest_ts=0.0, now=NOW)
    base.update(kw)
    return family_hedge_verdict(cfg, **base)


def test_verdict_disabled_standdown():
    v = _verdict(_hedge_cfg(campaign_family_hedge_enabled=False))
    assert v.action == "standdown"
    assert v.reason == "family_hedge_disabled"


def test_verdict_no_map_entry():
    v = _verdict(_hedge_cfg(), symbol="DOGE-USD")
    assert v.action == "standdown"
    assert v.reason == "no_family_instrument"


def test_verdict_harvest_cap():
    v = _verdict(_hedge_cfg(), harvests_done=4)
    assert v.action == "standdown"
    assert v.reason == "harvest_cap"


def test_verdict_cooloff_hold():
    v = _verdict(_hedge_cfg(), last_harvest_ts=NOW - 100.0)
    assert v.action == "hold"
    assert v.reason == "cooloff"


def test_verdict_bad_geometry():
    v = _verdict(_hedge_cfg(), entry_price=0.0)
    assert v.action == "standdown"
    assert v.reason == "bad_geometry"
    v2 = _verdict(_hedge_cfg(), stop_price=-1.0)
    assert v2.action == "standdown"
    assert v2.reason == "bad_geometry"


def test_verdict_green_arm_long():
    v = _verdict(_hedge_cfg())
    assert v.action == "arm"
    assert v.mode == "green"
    assert v.hedge_side == "short"
    assert v.hedge_symbol == "USTECH100-USD"
    assert v.leverage == 15
    # stop 98 < entry 100 → no locked floor; budget_frac = 0.7 * 0.095 = 0.0665
    # budget_usd = 0.0665 * entry * qty(1.0) = 6.65; notional = 6.65 / 0.02
    assert abs(v.budget_usd - 0.7 * 0.095 * 100.0) < 1e-9
    assert abs(v.stop_frac - 0.02) < 1e-9
    assert abs(v.notional - (0.7 * 0.095 * 100.0) / 0.02) < 1e-9


def test_verdict_green_locked_floor_dominant():
    # pyramid doctrine: at TP1-approach the stop is THROUGH entry — locked
    # floor max(0, (stop-entry)/entry) = 0.01 dominates the profit term
    v = _verdict(_hedge_cfg(), stop_price=101.0)
    assert v.action == "arm"
    assert v.mode == "green"
    expected = (0.01 + 0.7 * 0.095) * 100.0      # (locked + 0.7*opf) × entry × qty
    assert abs(v.budget_usd - expected) < 1e-9
    assert abs(v.notional - expected / 0.02) < 1e-9


def test_verdict_green_qty_scales_budget():
    # qty-aware USD sizing: 2.5 units multiplies budget AND the reserve claim
    v = _verdict(_hedge_cfg(), primary_qty=2.5)
    assert v.action == "arm"
    assert abs(v.budget_usd - 0.7 * 0.095 * 100.0 * 2.5) < 1e-9
    assert abs(v.notional - (0.7 * 0.095 * 100.0 * 2.5) / 0.02) < 1e-9


def test_verdict_green_stop_frac_atr_dominates():
    # atr15/mark = 5/109.5 ≈ 0.0457 > 0.02 floor
    v = _verdict(_hedge_cfg(), atr15=5.0)
    assert v.action == "arm"
    assert abs(v.stop_frac - 5.0 / 109.5) < 1e-9


def test_verdict_red_arm():
    # designed risk = |100-98|/100 = 0.02; red at profit ≤ -0.5*0.02 = -0.01
    v = _verdict(_hedge_cfg(), mark_price=97.0, open_profit_frac=-0.02)
    assert v.action == "arm"
    assert v.mode == "red"
    assert abs(v.budget_usd - 0.7 * 0.02 * 100.0) < 1e-9


def test_verdict_red_boundary_exact():
    # exactly at -0.5 * designed risk → red (<=)
    v = _verdict(_hedge_cfg(), mark_price=99.0, open_profit_frac=-0.01)
    assert v.action == "arm"
    assert v.mode == "red"


def test_verdict_no_trigger_hold():
    # mark mid-range, small profit, nowhere near TP1
    v = _verdict(_hedge_cfg(), mark_price=102.0, open_profit_frac=0.02)
    assert v.action == "hold"
    assert v.reason == "no_trigger"


def test_verdict_reserve_exhausted_fail_closed():
    # green arm margin claim = notional 332.5 / 15 ≈ 22.17 USD > reserve 1.0
    v = _verdict(_hedge_cfg(), reserve_available=1.0)
    assert v.action == "standdown"
    assert v.mode == "green"
    assert v.reason == "reserve_exhausted"


def test_verdict_short_mirror_green_arm():
    # primary short: entry 100, tp1 90, mark 90.5 (≥0.9 of the way down)
    v = _verdict(_hedge_cfg(), primary_side="short", entry_price=100.0,
                 mark_price=90.5, stop_price=102.0, tp1_price=90.0,
                 open_profit_frac=0.095, symbol="UNI-USD")
    assert v.action == "arm"
    assert v.mode == "green"
    assert v.hedge_side == "long"
    assert v.hedge_symbol == "ETH-USD"


# ── 9. Self-portfolio handoff ────────────────────────────────────────────────

def test_handoff_disabled():
    ok, reason = self_portfolio_handoff(
        _cfg(campaign_self_portfolio_enabled=False), symbol="SPCX-USD",
        pyramid_layers_done=3, pyramid_max_layers=4, tp2_hit=True)
    assert (ok, reason) == (False, "self_portfolio_disabled")


def test_handoff_staircase_complete():
    # layers_done >= max_layers - 1 → hand to treasury
    ok, reason = self_portfolio_handoff(
        _cfg(campaign_self_portfolio_enabled=True), symbol="SPCX-USD",
        pyramid_layers_done=3, pyramid_max_layers=4, tp2_hit=False)
    assert (ok, reason) == (True, "staircase_complete")


def test_handoff_tp2_banked():
    ok, reason = self_portfolio_handoff(
        _cfg(campaign_self_portfolio_enabled=True), symbol="SPCX-USD",
        pyramid_layers_done=0, pyramid_max_layers=4, tp2_hit=True)
    assert (ok, reason) == (True, "tp2_banked")


def test_handoff_building():
    ok, reason = self_portfolio_handoff(
        _cfg(campaign_self_portfolio_enabled=True), symbol="SPCX-USD",
        pyramid_layers_done=1, pyramid_max_layers=4, tp2_hit=False)
    assert (ok, reason) == (False, "building")
