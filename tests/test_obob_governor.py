"""tests/test_obob_governor.py — hard pins for the OBOB governor.

Doctrine under test (2026-09-26): the two-system law (profit fills vs free
volume placements), the $30 loss cap, the $20 pool reserve, and the
conservative floor chain ($30/$2.24/0.70 -> 18, never 19).
"""
from types import SimpleNamespace

import pytest

from intelligence.obob_governor import ObobGovernor


def _cfg(**over):
    base = dict(
        obob_governor_enabled=True,
        obob_daily_loss_cap_usd=30.0,
        obob_pool_reserve_usd=20.0,
        obob_margin_per_trade_usd=8.0,
        obob_est_fill_rate=0.35,
        obob_daily_volume_target_usd=71500.0,
    )
    base.update(over)
    return SimpleNamespace(**base)


G = ObobGovernor


# ── ev_per_trade ─────────────────────────────────────────────────────────────

class TestEvPerTrade:
    def test_paste_worked_example(self):
        # w=0.30, W=$11.20, L=$2.24, fee=$0.112, carry=$0.112
        # 3.36 - 1.568 - 0.224 = +1.568 (paste prints +1.566, hand rounding)
        ev = G.ev_per_trade(_cfg(), win_rate=0.30, avg_win_usd=11.20,
                            avg_loss_usd=2.24, fee_rt_usd=0.112,
                            carry_usd=0.112)
        assert ev == pytest.approx(1.567, abs=0.005)
        assert ev > 0.0

    def test_no_carry_default(self):
        ev = G.ev_per_trade(_cfg(), win_rate=0.5, avg_win_usd=10.0,
                            avg_loss_usd=10.0, fee_rt_usd=0.5)
        assert ev == pytest.approx(-0.5, abs=1e-9)

    def test_negative_win_none(self):
        assert G.ev_per_trade(_cfg(), win_rate=0.5, avg_win_usd=-1.0,
                              avg_loss_usd=2.0, fee_rt_usd=0.1) is None

    def test_negative_loss_none(self):
        assert G.ev_per_trade(_cfg(), win_rate=0.5, avg_win_usd=1.0,
                              avg_loss_usd=-2.0, fee_rt_usd=0.1) is None

    def test_win_rate_above_one_none(self):
        assert G.ev_per_trade(_cfg(), win_rate=1.2, avg_win_usd=1.0,
                              avg_loss_usd=2.0, fee_rt_usd=0.1) is None

    def test_win_rate_below_zero_none(self):
        assert G.ev_per_trade(_cfg(), win_rate=-0.1, avg_win_usd=1.0,
                              avg_loss_usd=2.0, fee_rt_usd=0.1) is None

    def test_nan_none(self):
        assert G.ev_per_trade(_cfg(), win_rate=float("nan"),
                              avg_win_usd=1.0, avg_loss_usd=2.0,
                              fee_rt_usd=0.1) is None


# ── max_trades_by_loss_cap ───────────────────────────────────────────────────

class TestMaxTradesByLossCap:
    def test_doctrine_pin_18_not_19(self):
        # Floor chain: floor(30/2.24)=13 losers, floor(13/0.70)=18 trades.
        # Naive floor(13.39/0.70)=19 double-spends the fractional loser.
        t = G.max_trades_by_loss_cap(_cfg(), avg_loss_usd=2.24,
                                     loss_frac=0.70)
        assert t == 18

    def test_loss_frac_one_gives_loser_budget(self):
        # Every trade a loser: 13 losers = 13 trades.
        t = G.max_trades_by_loss_cap(_cfg(), avg_loss_usd=2.24,
                                     loss_frac=1.0)
        assert t == 13

    def test_explicit_cap_override(self):
        t = G.max_trades_by_loss_cap(_cfg(), loss_cap_usd=30.0,
                                     avg_loss_usd=2.24, loss_frac=0.70)
        assert t == 18

    def test_knob_cap_binds_when_unset(self):
        t = G.max_trades_by_loss_cap(_cfg(obob_daily_loss_cap_usd=10.0),
                                     avg_loss_usd=2.24, loss_frac=1.0)
        assert t == 4  # floor(10/2.24)=4

    def test_zero_avg_loss_none(self):
        assert G.max_trades_by_loss_cap(_cfg(), avg_loss_usd=0.0,
                                        loss_frac=0.7) is None

    def test_zero_loss_frac_none(self):
        assert G.max_trades_by_loss_cap(_cfg(), avg_loss_usd=2.24,
                                        loss_frac=0.0) is None

    def test_loss_frac_above_one_none(self):
        assert G.max_trades_by_loss_cap(_cfg(), avg_loss_usd=2.24,
                                        loss_frac=1.5) is None

    def test_negative_cap_none(self):
        assert G.max_trades_by_loss_cap(_cfg(), loss_cap_usd=-1.0,
                                        avg_loss_usd=2.24,
                                        loss_frac=0.7) is None


# ── placements_for_volume ────────────────────────────────────────────────────

class TestPlacementsForVolume:
    def test_paste_pin_666(self):
        # $65,000 / $280 = 232.14 -> 233 fills; 233 / 0.35 = 665.71 -> 666.
        p = G.placements_for_volume(_cfg(), volume_remaining_usd=65000.0,
                                    avg_rt_volume_per_fill_usd=280.0,
                                    est_fill_rate=0.35)
        assert p == 666

    def test_zero_volume_zero_placements(self):
        p = G.placements_for_volume(_cfg(), volume_remaining_usd=0.0,
                                    avg_rt_volume_per_fill_usd=280.0,
                                    est_fill_rate=0.35)
        assert p == 0

    def test_fill_rate_zero_none(self):
        assert G.placements_for_volume(
            _cfg(), volume_remaining_usd=1000.0,
            avg_rt_volume_per_fill_usd=280.0, est_fill_rate=0.0) is None

    def test_fill_rate_above_one_none(self):
        assert G.placements_for_volume(
            _cfg(), volume_remaining_usd=1000.0,
            avg_rt_volume_per_fill_usd=280.0, est_fill_rate=1.01) is None

    def test_negative_volume_none(self):
        assert G.placements_for_volume(
            _cfg(), volume_remaining_usd=-5.0,
            avg_rt_volume_per_fill_usd=280.0, est_fill_rate=0.35) is None

    def test_zero_rt_volume_none(self):
        assert G.placements_for_volume(
            _cfg(), volume_remaining_usd=1000.0,
            avg_rt_volume_per_fill_usd=0.0, est_fill_rate=0.35) is None

    def test_fill_rate_one_equals_fills(self):
        p = G.placements_for_volume(_cfg(), volume_remaining_usd=65000.0,
                                    avg_rt_volume_per_fill_usd=280.0,
                                    est_fill_rate=1.0)
        assert p == 233


# ── daily_budget — one pin per binding ───────────────────────────────────────

class TestDailyBudget:
    def test_pool_binding(self):
        # pool_free $36 - reserve $20 = $16; floor(16/8) = 2 trades.
        # loss cap bound 18, volume bound 233 -> pool binds.
        v = G.daily_budget(_cfg(), volume_remaining_usd=65000.0,
                           avg_rt_volume_per_fill_usd=280.0,
                           est_fill_rate=0.35,
                           loss_cap_remaining_usd=30.0,
                           avg_loss_usd=2.24, loss_frac=0.70,
                           pool_free_usd=36.0, margin_per_trade_usd=8.0)
        assert v["max_trades_pool"] == 2
        assert v["max_trades_loss_cap"] == 18
        assert v["fills_volume"] == 233
        assert v["placements"] == 666
        assert v["binding"] == "pool"
        assert v["budget"] == 2

    def test_loss_cap_binding(self):
        # floor(5/2.24)=2 losers -> floor(2/0.7)=2 trades; pool 10, vol 233.
        v = G.daily_budget(_cfg(), volume_remaining_usd=65000.0,
                           avg_rt_volume_per_fill_usd=280.0,
                           est_fill_rate=0.35,
                           loss_cap_remaining_usd=5.0,
                           avg_loss_usd=2.24, loss_frac=0.70,
                           pool_free_usd=100.0, margin_per_trade_usd=8.0)
        assert v["max_trades_loss_cap"] == 2
        assert v["binding"] == "loss_cap"
        assert v["budget"] == 2

    def test_volume_binding(self):
        # $280 remaining / $280 RT = 1 fill; placements ceil(1/0.35)=3.
        v = G.daily_budget(_cfg(), volume_remaining_usd=280.0,
                           avg_rt_volume_per_fill_usd=280.0,
                           est_fill_rate=0.35,
                           loss_cap_remaining_usd=30.0,
                           avg_loss_usd=2.24, loss_frac=0.70,
                           pool_free_usd=100.0, margin_per_trade_usd=8.0)
        assert v["fills_volume"] == 1
        assert v["placements"] == 3
        assert v["binding"] == "volume"
        assert v["budget"] == 1

    def test_pool_below_reserve_clamps_zero(self):
        v = G.daily_budget(_cfg(), volume_remaining_usd=65000.0,
                           avg_rt_volume_per_fill_usd=280.0,
                           est_fill_rate=0.35,
                           loss_cap_remaining_usd=30.0,
                           avg_loss_usd=2.24, loss_frac=0.70,
                           pool_free_usd=10.0)
        assert v["max_trades_pool"] == 0
        assert v["binding"] == "pool"
        assert v["budget"] == 0

    def test_margin_knob_binds_when_unset(self):
        v = G.daily_budget(_cfg(obob_margin_per_trade_usd=8.0),
                           volume_remaining_usd=65000.0,
                           avg_rt_volume_per_fill_usd=280.0,
                           est_fill_rate=0.35,
                           loss_cap_remaining_usd=30.0,
                           avg_loss_usd=2.24, loss_frac=0.70,
                           pool_free_usd=36.0)
        assert v["max_trades_pool"] == 2

    def test_degenerate_loss_inputs_none(self):
        assert G.daily_budget(_cfg(), volume_remaining_usd=65000.0,
                              avg_rt_volume_per_fill_usd=280.0,
                              est_fill_rate=0.35,
                              loss_cap_remaining_usd=30.0,
                              avg_loss_usd=0.0, loss_frac=0.70,
                              pool_free_usd=100.0) is None

    def test_degenerate_fill_rate_none(self):
        assert G.daily_budget(_cfg(), volume_remaining_usd=65000.0,
                              avg_rt_volume_per_fill_usd=280.0,
                              est_fill_rate=0.0,
                              loss_cap_remaining_usd=30.0,
                              avg_loss_usd=2.24, loss_frac=0.70,
                              pool_free_usd=100.0) is None

    def test_degenerate_margin_none(self):
        assert G.daily_budget(_cfg(), volume_remaining_usd=65000.0,
                              avg_rt_volume_per_fill_usd=280.0,
                              est_fill_rate=0.35,
                              loss_cap_remaining_usd=30.0,
                              avg_loss_usd=2.24, loss_frac=0.70,
                              pool_free_usd=100.0,
                              margin_per_trade_usd=0.0) is None

    def test_zero_volume_budget_zero(self):
        v = G.daily_budget(_cfg(), volume_remaining_usd=0.0,
                           avg_rt_volume_per_fill_usd=280.0,
                           est_fill_rate=0.35,
                           loss_cap_remaining_usd=30.0,
                           avg_loss_usd=2.24, loss_frac=0.70,
                           pool_free_usd=100.0)
        assert v["placements"] == 0
        assert v["fills_volume"] == 0
        assert v["binding"] == "volume"
        assert v["budget"] == 0


# ── master gate ──────────────────────────────────────────────────────────────

class TestMasterGate:
    def test_disabled_all_verdicts_none(self):
        cfg = _cfg(obob_governor_enabled=False)
        assert G.ev_per_trade(cfg, win_rate=0.3, avg_win_usd=11.2,
                              avg_loss_usd=2.24, fee_rt_usd=0.112) is None
        assert G.max_trades_by_loss_cap(cfg, avg_loss_usd=2.24,
                                        loss_frac=0.70) is None
        assert G.placements_for_volume(cfg, volume_remaining_usd=65000.0,
                                       avg_rt_volume_per_fill_usd=280.0,
                                       est_fill_rate=0.35) is None
        assert G.daily_budget(cfg, volume_remaining_usd=65000.0,
                              avg_rt_volume_per_fill_usd=280.0,
                              est_fill_rate=0.35,
                              loss_cap_remaining_usd=30.0,
                              avg_loss_usd=2.24, loss_frac=0.70,
                              pool_free_usd=100.0) is None

    def test_default_enabled_when_attr_missing(self):
        # getattr default: a bare cfg behaves as enabled.
        cfg = SimpleNamespace()
        ev = G.ev_per_trade(cfg, win_rate=1.0, avg_win_usd=1.0,
                            avg_loss_usd=1.0, fee_rt_usd=0.0)
        assert ev == pytest.approx(1.0, abs=1e-9)
