"""Pins: post-win same-direction cooloff (Governor 2026-11-15).

Kill off / no latch / expired latch / wrong class / wrong side all allow —
the default state reproduces the pre-module system bit-for-bit. The
direction scope (class, side) is load-bearing: the 11-15 turbo cluster
crossed families (ETH majors, ONDO defi, ARB l1_alt) — family scope
catches 1/3, direction scope 3/3.
"""

from __future__ import annotations

from prop_harness.brains import post_win_cooloff as pwc


class TestTrigger:
    def test_non_positive_pnl_never_arms(self):
        assert pwc.win_qualifies(0.0, 7.0) is False
        assert pwc.win_qualifies(-3.0, 7.0) is False
        assert pwc.win_qualifies(None, 7.0) is False
        assert pwc.win_qualifies("nan", 7.0) is False

    def test_dollar_floor(self):
        assert pwc.win_qualifies(1.49, 0.5) is False
        assert pwc.win_qualifies(1.5, 0.5) is True

    def test_r_multiple_floor(self):
        # max(1.5, 0.20 x risk): risk 20 -> r floor 4.0 dominates
        assert pwc.win_qualifies(3.9, 20.0) is False
        assert pwc.win_qualifies(4.0, 20.0) is True

    def test_custom_thresholds(self):
        assert pwc.win_qualifies(0.5, 1.0, min_win_usd=0.25,
                                 min_win_r=0.1) is True
        assert pwc.win_qualifies(0.5, 100.0, min_win_usd=0.25,
                                 min_win_r=0.1) is False


class TestLatchKey:
    def test_class_of(self):
        assert pwc.class_of("xyz:MU") == "tradfi"
        assert pwc.class_of("BTC") == "crypto"
        assert pwc.class_of(None) == "crypto"

    def test_key_shape(self):
        assert pwc.latch_key("crypto", "sell") == "crypto:sell"
        assert pwc.latch_key("tradfi", "buy") == "tradfi:buy"


class TestArmAndExpiry:
    def test_arm_sets_expiry_and_counts(self):
        l = pwc.arm_latch({}, asset_class="crypto", side="sell",
                          realized_pnl=5.0, risk_usd=7.0, now=1000.0,
                          semantic_id="s1")
        lat = l["crypto:sell"]
        assert lat["armed"] is True
        assert lat["expires_at"] == 1000.0 + 1800.0
        assert lat["wins"] == 1
        assert lat["blocks"] == 0

    def test_sliding_rearm(self):
        l = pwc.arm_latch({}, asset_class="crypto", side="sell",
                          realized_pnl=5.0, risk_usd=7.0, now=1000.0)
        l = pwc.arm_latch(l, asset_class="crypto", side="sell",
                          realized_pnl=2.0, risk_usd=7.0, now=1500.0)
        lat = l["crypto:sell"]
        assert lat["armed_at"] == 1500.0
        assert lat["expires_at"] == 1500.0 + 1800.0
        assert lat["wins"] == 2
        assert lat["win_usd"] == 2.0

    def test_lazy_expiry(self):
        l = pwc.arm_latch({}, asset_class="crypto", side="sell",
                          realized_pnl=5.0, risk_usd=7.0, now=1000.0)
        assert pwc.latch_active(l["crypto:sell"], 2799.9) is True
        assert pwc.latch_active(l["crypto:sell"], 2800.0) is False

    def test_prune_expired(self):
        l = pwc.arm_latch({}, asset_class="crypto", side="sell",
                          realized_pnl=5.0, risk_usd=7.0, now=1000.0)
        l = pwc.arm_latch(l, asset_class="crypto", side="buy",
                          realized_pnl=5.0, risk_usd=7.0, now=2500.0)
        out = pwc.prune_expired(l, 2900.0)
        assert "crypto:sell" not in out
        assert "crypto:buy" in out


class TestVerdict:
    def _hot(self):
        return pwc.arm_latch({}, asset_class="crypto", side="sell",
                             realized_pnl=5.0, risk_usd=7.0, now=1000.0)

    def test_disabled_allows(self):
        reason, detail = pwc.cooloff_verdict(False, self._hot(), "ETH",
                                             "sell", 1100.0)
        assert reason is None and detail == {}

    def test_no_latches_allows(self):
        assert pwc.cooloff_verdict(True, None, "ETH", "sell",
                                   1100.0)[0] is None
        assert pwc.cooloff_verdict(True, {}, "ETH", "sell",
                                   1100.0)[0] is None

    def test_same_class_same_side_blocks(self):
        reason, detail = pwc.cooloff_verdict(True, self._hot(), "ETH",
                                             "sell", 1100.0)
        assert reason == pwc.COOLOFF_BLOCK
        assert detail["asset"] == "ETH"
        assert detail["ttl_remaining_s"] == 1700.0

    def test_cross_family_still_blocks(self):
        # the 11-15 cluster: BTC win -> ETH/ONDO/ARB shorts blocked
        hot = self._hot()
        for asset in ("ETH", "ONDO", "ARB"):
            assert pwc.cooloff_verdict(True, hot, asset, "sell",
                                       1100.0)[0] == pwc.COOLOFF_BLOCK

    def test_opposite_side_allows(self):
        assert pwc.cooloff_verdict(True, self._hot(), "ETH", "buy",
                                   1100.0)[0] is None

    def test_other_class_allows(self):
        # crypto latch does not touch tradfi sleeve candidates
        assert pwc.cooloff_verdict(True, self._hot(), "xyz:MU", "sell",
                                   1100.0)[0] is None

    def test_tradfi_latch_binds_xyz_only(self):
        l = pwc.arm_latch({}, asset_class="tradfi", side="buy",
                          realized_pnl=3.0, risk_usd=10.0, now=1000.0)
        assert pwc.cooloff_verdict(True, l, "xyz:GOLD", "buy",
                                   1100.0)[0] == pwc.COOLOFF_BLOCK
        assert pwc.cooloff_verdict(True, l, "BTC", "buy",
                                   1100.0)[0] is None

    def test_expired_allows(self):
        assert pwc.cooloff_verdict(True, self._hot(), "ETH", "sell",
                                   2800.0)[0] is None

    def test_serial_winners_outside_ttl_survive(self):
        # 11-14 BTC serial winners spaced >=42 min: 42min > 30min TTL
        assert pwc.cooloff_verdict(True, self._hot(), "BTC", "sell",
                                   1000.0 + 42 * 60)[0] is None


class TestBlockCounting:
    def test_note_block_increments(self):
        l = pwc.arm_latch({}, asset_class="crypto", side="sell",
                          realized_pnl=5.0, risk_usd=7.0, now=1000.0)
        l = pwc.note_cooloff_block(l, "ETH", "sell")
        l = pwc.note_cooloff_block(l, "ONDO", "sell")
        assert l["crypto:sell"]["blocks"] == 2

    def test_note_block_missing_key_safe(self):
        l = pwc.note_cooloff_block({}, "ETH", "sell")
        assert l == {}


from pathlib import Path

from prop_harness.brains.budget import BudgetBrain
from prop_harness.brains.drawdown import DrawdownBrain
from prop_harness.config import PropConfig
from prop_harness.executor import ProprExecutor


class _OkClient:
    account_id = "acct1"

    def get_open_orders(self, base=None):
        return []

    def place_orders(self, body):
        return [{"orderId": "o1"}]

    def cancel_order(self, order_id):
        return {}


def _exec(tmp_path: Path, **over) -> ProprExecutor:
    knobs = dict(
        enabled=True, state_root=tmp_path, profile="evaluation",
        asset_class="all", crypto_comove_enabled=False,
        family_cap_enabled=False, post_win_respend_enabled=False,
        cost_move_gate_enabled=False, max_positions=7,
        slot_reserve_enabled=False, eval_crypto_min_risk_enabled=False,
        crypto_min_coh_fire_enabled=False,
        eval_margin_cap_frac=0.99,
        eval_trend_budget_usd=5_000.0,
    )
    knobs.update(over)
    cfg = PropConfig(**knobs)
    (tmp_path / "journal").mkdir(exist_ok=True)
    dd = DrawdownBrain(tmp_path / "dd.json", floor_line=1000.0,
                       floor_usd=150.0)
    dd.update(5000.0)
    budget = BudgetBrain(tmp_path / "budget.json", hard_zero=30.0,
                         divisor=10.0, floor_budget=40.0)
    budget.roll(10_000.0)
    return ProprExecutor(cfg, _OkClient(), budget, dd)


class TestCooloffExecutor:
    def test_default_off_legacy(self, tmp_path):
        ex = _exec(tmp_path)  # post_win_cooloff_enabled defaults False
        assert ex.arm_post_win_cooloff(
            {"asset": "BTC", "side": "sell",
             "realized_pnl_final": 50.0, "risk_usd": 7.0}) is False
        res = ex.place_bracket("ETH", "sell", 14.0, 100.0, 108.0,
                               coherence=5.0)
        assert res.ok is True

    def test_arm_then_same_direction_blocked(self, tmp_path):
        ex = _exec(tmp_path, post_win_cooloff_enabled=True)
        assert ex.arm_post_win_cooloff(
            {"asset": "BTC", "side": "sell",
             "realized_pnl_final": 5.0, "risk_usd": 7.0,
             "semantic_id": "prop-acct1-BTC-1"}) is True
        res = ex.place_bracket("ETH", "sell", 14.0, 100.0, 108.0,
                               coherence=5.0)
        assert res.ok is False
        assert res.reason == pwc.COOLOFF_BLOCK

    def test_opposite_side_and_other_class_pass(self, tmp_path):
        ex = _exec(tmp_path, post_win_cooloff_enabled=True)
        ex.arm_post_win_cooloff(
            {"asset": "BTC", "side": "sell",
             "realized_pnl_final": 5.0, "risk_usd": 7.0})
        assert ex.place_bracket("ETH", "buy", 14.0, 100.0, 92.0,
                                coherence=5.0).ok is True
        assert ex.place_bracket("xyz:MU", "sell", 14.0, 100.0, 108.0,
                                coherence=5.0).ok is True

    def test_small_win_never_arms(self, tmp_path):
        ex = _exec(tmp_path, post_win_cooloff_enabled=True)
        assert ex.arm_post_win_cooloff(
            {"asset": "BTC", "side": "sell",
             "realized_pnl_final": 0.40, "risk_usd": 7.0}) is False
        assert ex.place_bracket("ETH", "sell", 14.0, 100.0, 108.0,
                                coherence=5.0).ok is True

    def test_latch_persists_to_disk(self, tmp_path):
        ex = _exec(tmp_path, post_win_cooloff_enabled=True)
        ex.arm_post_win_cooloff(
            {"asset": "BTC", "side": "sell",
             "realized_pnl_final": 5.0, "risk_usd": 7.0})
        ex2 = _exec(tmp_path, post_win_cooloff_enabled=True)
        res = ex2.place_bracket("ARB", "sell", 14.0, 100.0, 108.0,
                                coherence=5.0)
        assert res.ok is False
        assert res.reason == pwc.COOLOFF_BLOCK


class TestConfigParse:
    def test_defaults_are_legacy(self, monkeypatch, tmp_path):
        monkeypatch.delenv("PROP_POST_WIN_COOLOFF", raising=False)
        monkeypatch.setenv("PROP_STATE_ROOT", str(tmp_path))
        cfg = PropConfig.from_env()
        assert cfg.post_win_cooloff_enabled is False
        assert cfg.post_win_cooloff_min_win_usd == 1.5
        assert cfg.post_win_cooloff_min_win_r == 0.20
        assert cfg.post_win_cooloff_ttl_s == 1800.0

    def test_env_binds(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PROP_STATE_ROOT", str(tmp_path))
        monkeypatch.setenv("PROP_POST_WIN_COOLOFF", "1")
        monkeypatch.setenv("PROP_POST_WIN_COOLOFF_TTL_S", "900")
        cfg = PropConfig.from_env()
        assert cfg.post_win_cooloff_enabled is True
        assert cfg.post_win_cooloff_ttl_s == 900.0

    def test_zero_disables(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PROP_STATE_ROOT", str(tmp_path))
        monkeypatch.setenv("PROP_POST_WIN_COOLOFF", "0")
        cfg = PropConfig.from_env()
        assert cfg.post_win_cooloff_enabled is False
