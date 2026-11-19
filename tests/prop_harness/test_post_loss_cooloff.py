"""Pins: M4' post-loss same-(asset, side) cooloff (2026-11-17).

Kill off / no latch / expired latch / winning close all allow — default
ON but the kill state reproduces the pre-module system bit-for-bit. The
(asset, side) key with xyz: normalization is load-bearing: the revenge
re-entry pattern is asset-exact (GOLD/xyz:GOLD share one latch).
"""

from __future__ import annotations

from prop_harness.brains import post_loss_cooloff as plc


class TestTrigger:
    def test_any_loss_arms(self):
        assert plc.loss_qualifies(-0.01) is True
        assert plc.loss_qualifies(-3.0) is True

    def test_non_loss_never_arms(self):
        assert plc.loss_qualifies(0.0) is False
        assert plc.loss_qualifies(5.0) is False
        assert plc.loss_qualifies(None) is False
        assert plc.loss_qualifies("nan") is False


class TestLatchKey:
    def test_xyz_prefix_stripped(self):
        assert plc.norm_asset("xyz:GOLD") == "GOLD"
        assert plc.norm_asset("GOLD") == "GOLD"
        assert plc.latch_key("xyz:GOLD", "buy") == "GOLD:buy"
        assert plc.latch_key("CBRS", "sell") == "CBRS:sell"

    def test_xyz_and_plain_share_latch(self):
        l = plc.arm_latch({}, asset="xyz:GOLD", side="buy",
                          realized_pnl=-2.0, now=1000.0)
        reason, _ = plc.cooloff_verdict(True, l, "GOLD", "buy", 1100.0)
        assert reason == plc.COOLOFF_BLOCK


class TestArmAndExpiry:
    def test_arm_sets_expiry_and_counts(self):
        l = plc.arm_latch({}, asset="BTC", side="sell",
                          realized_pnl=-4.0, now=1000.0,
                          semantic_id="s1")
        lat = l["BTC:sell"]
        assert lat["armed"] is True
        assert lat["expires_at"] == 1000.0 + 1800.0
        assert lat["losses"] == 1
        assert lat["blocks"] == 0
        assert lat["loss_usd"] == -4.0

    def test_sliding_rearm(self):
        l = plc.arm_latch({}, asset="BTC", side="sell",
                          realized_pnl=-4.0, now=1000.0)
        l = plc.arm_latch(l, asset="BTC", side="sell",
                          realized_pnl=-1.0, now=1500.0)
        lat = l["BTC:sell"]
        assert lat["armed_at"] == 1500.0
        assert lat["expires_at"] == 1500.0 + 1800.0
        assert lat["losses"] == 2

    def test_lazy_expiry(self):
        l = plc.arm_latch({}, asset="BTC", side="sell",
                          realized_pnl=-4.0, now=1000.0)
        assert plc.latch_active(l["BTC:sell"], 2799.9) is True
        assert plc.latch_active(l["BTC:sell"], 2800.0) is False

    def test_prune_expired(self):
        l = plc.arm_latch({}, asset="BTC", side="sell",
                          realized_pnl=-4.0, now=1000.0)
        l = plc.arm_latch(l, asset="ETH", side="buy",
                          realized_pnl=-2.0, now=2500.0)
        out = plc.prune_expired(l, 2900.0)
        assert "BTC:sell" not in out
        assert "ETH:buy" in out


class TestVerdict:
    def _hot(self):
        return plc.arm_latch({}, asset="ETH", side="sell",
                             realized_pnl=-4.0, now=1000.0)

    def test_disabled_allows(self):
        reason, detail = plc.cooloff_verdict(False, self._hot(), "ETH",
                                             "sell", 1100.0)
        assert reason is None and detail == {}

    def test_no_latches_allows(self):
        assert plc.cooloff_verdict(True, None, "ETH", "sell",
                                   1100.0)[0] is None
        assert plc.cooloff_verdict(True, {}, "ETH", "sell",
                                   1100.0)[0] is None

    def test_hot_latch_blocks_same_asset_side(self):
        reason, detail = plc.cooloff_verdict(True, self._hot(), "ETH",
                                             "sell", 1100.0)
        assert reason == plc.COOLOFF_BLOCK
        assert detail["latch_asset"] == "ETH"
        assert detail["ttl_remaining_s"] > 0

    def test_other_side_allows(self):
        assert plc.cooloff_verdict(True, self._hot(), "ETH", "buy",
                                   1100.0)[0] is None

    def test_other_asset_allows(self):
        assert plc.cooloff_verdict(True, self._hot(), "BTC", "sell",
                                   1100.0)[0] is None

    def test_expired_allows(self):
        assert plc.cooloff_verdict(True, self._hot(), "ETH", "sell",
                                   2800.0)[0] is None

    def test_note_block_counts(self):
        l = self._hot()
        l = plc.note_cooloff_block(l, "ETH", "sell")
        assert l["ETH:sell"]["blocks"] == 1
