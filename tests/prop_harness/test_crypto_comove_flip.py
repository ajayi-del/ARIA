"""Pins for co-move underwater / MFE-decay flip (shadow-first).

Opposite-block stays ON. Flip journals would_flip; live prefer_flat still
refuses hedges. Never haircuts opposite size.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from prop_harness.brains import crypto_comove as ccm
from prop_harness.brains.budget import BudgetBrain
from prop_harness.brains.drawdown import DrawdownBrain
from prop_harness.config import PropConfig
from prop_harness.executor import ProprExecutor


class _FakeClient:
    def __init__(self):
        self.account_id = "acct1"
        self.placed: list = []

    def get_open_orders(self, base=None):
        return []

    def place_orders(self, body):
        self.placed.append(body)
        return [{"orderId": "urn:prp-order:new", **body["orders"][0]}]

    def cancel_order(self, order_id):
        return {}


def _exec(tmp_path: Path, **kw) -> ProprExecutor:
    kw.setdefault("profile", "evaluation")
    kw.setdefault("asset_class", "crypto")
    kw.setdefault("crypto_comove_enabled", True)
    kw.setdefault("crypto_comove_flip_enabled", True)
    kw.setdefault("crypto_comove_flip_live", False)
    kw.setdefault("crypto_comove_flip_prefer_flat", True)
    kw.setdefault("family_cap_enabled", False)
    kw.setdefault("post_win_respend_enabled", False)
    kw.setdefault("cost_move_gate_enabled", False)
    kw.setdefault("max_positions", 7)
    kw.setdefault("slot_reserve_enabled", False)
    kw.setdefault("crypto_min_coh_fire_enabled", False)
    cfg = PropConfig(enabled=True, state_root=tmp_path, **kw)
    (tmp_path / "journal").mkdir(exist_ok=True)
    dd = DrawdownBrain(tmp_path / "dd.json", floor_line=4850.0,
                       floor_usd=150.0)
    dd.update(5000.0)
    budget = BudgetBrain(tmp_path / "budget.json", hard_zero=30.0,
                         divisor=10.0, floor_budget=40.0)
    budget.roll(150.0)
    return ProprExecutor(cfg, _FakeClient(), budget, dd)


def _leg(asset, side, risk=7.0, *, upnl=-1.0, neg_since=None,
         peak_r=1.0, opened=None, i=0):
    now = time.time()
    return {
        "semantic_id": f"seed-{asset}-{side}-{i}",
        "status": "open", "asset": asset, "side": side,
        "family": "other", "quantity": "1", "risk_usd": risk,
        "entry_ref": "100",
        "stop_price": "99" if side == "buy" else "101",
        "opened_ts": opened if opened is not None else now - 1200,
        "be_armed": False,
        "last_upnl": upnl,
        "upnl_negative_since": (
            neg_since if neg_since is not None else now - 1200),
        "mfe_peak_r": peak_r,
    }


def _seed(ex, legs):
    reg = ex._registry()
    reg["brackets"].extend(legs)
    ex._save_registry(reg)


class TestFlipPredicate:
    def test_underwater_arms(self):
        now = time.time()
        legs = [
            _leg("BTC", "buy", 23.0, upnl=-2.0, neg_since=now - 1000),
            _leg("SOL", "buy", 7.0, upnl=-1.0, neg_since=now - 950),
        ]
        detail = ccm.flip_predicate(legs, underwater_s=900.0, now=now)
        assert detail is not None
        assert detail["underwater_arm"] is True
        assert detail["prefer_flat"] is True

    def test_fresh_underwater_cold(self):
        now = time.time()
        # peak_r=0 so giveback arm stays dark; only underwater clock matters
        legs = [_leg("BTC", "buy", 7.0, upnl=-1.0, neg_since=now - 60,
                     peak_r=0.0)]
        assert ccm.flip_predicate(
            legs, underwater_s=900.0, now=now) is None

    def test_giveback_arm(self):
        now = time.time()
        # peak 2R, current = upnl/risk = -0.1/7 ≈ -0.014 → giveback > 0.6
        legs = [_leg("ETH", "sell", 7.0, upnl=-0.1, peak_r=2.0,
                     neg_since=now - 10)]
        detail = ccm.flip_predicate(
            legs, underwater_s=900.0, giveback_frac=0.60, now=now)
        assert detail is not None
        assert detail["giveback_arm"] is True

    def test_green_book_no_flip(self):
        now = time.time()
        legs = [_leg("BTC", "buy", 7.0, upnl=3.0, peak_r=1.0,
                     neg_since=None)]
        legs[0].pop("upnl_negative_since", None)
        assert ccm.flip_predicate(legs, now=now) is None


class TestFlipShadowVerdict:
    def test_shadow_keeps_opposite_refuse(self):
        now = time.time()
        legs = [_leg("BTC", "buy", 23.0, upnl=-2.0, neg_since=now - 1000)]
        reason, detail = ccm.flip_shadow_verdict(
            True, ccm.CRYPTO_COMOVE_OPPOSITE, legs,
            flip_live=False, now=now)
        assert reason == ccm.CRYPTO_COMOVE_OPPOSITE
        assert detail is not None
        assert detail.get("shadow") is True

    def test_live_prefer_flat_distinct_reason(self):
        now = time.time()
        legs = [_leg("BTC", "buy", 23.0, upnl=-2.0, neg_since=now - 1000)]
        reason, detail = ccm.flip_shadow_verdict(
            True, ccm.CRYPTO_COMOVE_OPPOSITE, legs,
            flip_live=True, prefer_flat=True, now=now)
        assert reason == ccm.CRYPTO_COMOVE_FLIP_PREFER_FLAT
        assert detail["prefer_flat"] is True

    def test_live_allow_opposite_one_shot(self):
        now = time.time()
        legs = [_leg("BTC", "buy", 23.0, upnl=-2.0, neg_since=now - 1000)]
        reason, detail = ccm.flip_shadow_verdict(
            True, ccm.CRYPTO_COMOVE_OPPOSITE, legs,
            flip_live=True, prefer_flat=False, now=now)
        assert reason is None
        assert detail.get("allow_opposite") is True

    def test_no_haircut_path_without_predicate(self):
        # Opposite refuse with green book → no flip detail, no soft allow
        legs = [_leg("BTC", "buy", 7.0, upnl=5.0)]
        legs[0].pop("upnl_negative_since", None)
        reason, detail = ccm.flip_shadow_verdict(
            True, ccm.CRYPTO_COMOVE_OPPOSITE, legs,
            flip_live=True, prefer_flat=False)
        assert reason == ccm.CRYPTO_COMOVE_OPPOSITE
        assert detail is None


class TestFlipExecutor:
    def test_opposite_still_refused_and_would_flip_journaled(self, tmp_path):
        ex = _exec(tmp_path)
        now = time.time()
        _seed(ex, [_leg("BTC", "buy", 23.0, upnl=-5.0,
                        neg_since=now - 1200)])
        res = ex.place_bracket("ETH", "sell", 7.0, 3500.0, 3535.0,
                               coherence=9.5)
        assert not res.ok
        assert res.reason == ccm.CRYPTO_COMOVE_OPPOSITE
        # Journal carries would_flip
        events = (tmp_path / "journal" / "events.jsonl").read_text()
        assert ccm.CRYPTO_COMOVE_WOULD_FLIP in events

    def test_live_prefer_flat_reason(self, tmp_path):
        ex = _exec(tmp_path, crypto_comove_flip_live=True,
                   crypto_comove_flip_prefer_flat=True)
        now = time.time()
        _seed(ex, [_leg("BTC", "buy", 23.0, upnl=-5.0,
                        neg_since=now - 1200)])
        res = ex.place_bracket("ETH", "sell", 7.0, 3500.0, 3535.0,
                               coherence=9.5)
        assert not res.ok
        assert res.reason == ccm.CRYPTO_COMOVE_FLIP_PREFER_FLAT

    def test_opposite_block_without_flip_when_green(self, tmp_path):
        ex = _exec(tmp_path)
        leg = _leg("BTC", "buy", 23.0, upnl=5.0)
        leg.pop("upnl_negative_since", None)
        _seed(ex, [leg])
        res = ex.place_bracket("ETH", "sell", 7.0, 3500.0, 3535.0,
                               coherence=9.5)
        assert not res.ok
        assert res.reason == ccm.CRYPTO_COMOVE_OPPOSITE
        events = (tmp_path / "journal" / "events.jsonl").read_text()
        assert ccm.CRYPTO_COMOVE_WOULD_FLIP not in events

    def test_config_defaults_shadow(self):
        cfg = PropConfig(enabled=True)
        assert cfg.crypto_comove_flip_enabled is True
        assert cfg.crypto_comove_flip_live is False
        assert cfg.crypto_comove_flip_prefer_flat is True
