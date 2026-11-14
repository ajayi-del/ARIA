"""Pins for the prop-harness crypto co-move / single-β gate.

Specimen (turbo 2026-11-10): BTC long + SOL long + AVAX short, then ETH
short opened the opposite-major wound (−$8.84 / ~42m). Gate must refuse
that ETH short (and any alt opposite to dominant) without touching
Kant/Nietzsche/Chancellor or ARIA tiers 1–6.
"""

from __future__ import annotations

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


def _exec(tmp_path: Path, **kw):
    kw.setdefault("profile", "evaluation")
    kw.setdefault("asset_class", "crypto")
    kw.setdefault("crypto_comove_enabled", True)
    kw.setdefault("max_positions", 7)
    kw.setdefault("slot_reserve_enabled", False)
    kw.setdefault("crypto_min_coh_fire_enabled", False)
    cfg = PropConfig(enabled=True, state_root=tmp_path, **kw)
    dd = DrawdownBrain(tmp_path / "dd.json", floor_line=4850.0,
                       floor_usd=150.0)
    dd.update(5000.0)
    budget = BudgetBrain(tmp_path / "budget.json", hard_zero=30.0,
                         divisor=10.0, floor_budget=40.0)
    budget.roll(150.0)
    return ProprExecutor(cfg, _FakeClient(), budget, dd)


def _leg(asset: str, side: str, risk: float = 7.0, i: int = 0) -> dict:
    return {
        "semantic_id": f"seed-{asset}-{side}-{i}",
        "status": "open",
        "asset": asset,
        "side": side,
        "family": "other",
        "quantity": "1",
        "risk_usd": risk,
        "entry_ref": "100",
        "stop_price": "99" if side == "buy" else "101",
        "opened_ts": 1.0,
        "be_armed": False,
    }


def _seed(ex: ProprExecutor, legs: list[dict]) -> None:
    reg = ex._registry()
    reg["brackets"].extend(legs)
    ex._save_registry(reg)


# ── pure verdict ─────────────────────────────────────────────────────

class TestCryptoComoveVerdict:
    def test_kill_switch_off_never_binds(self):
        legs = [_leg("BTC", "buy")]
        assert ccm.crypto_comove_verdict(
            False, "ETH", "sell", legs, asset_class="crypto") is None

    def test_tradfi_sleeve_never_binds(self):
        legs = [_leg("BTC", "buy")]
        assert ccm.crypto_comove_verdict(
            True, "ETH", "sell", legs, asset_class="tradfi") is None

    def test_xyz_candidate_never_binds(self):
        legs = [_leg("BTC", "buy")]
        assert ccm.crypto_comove_verdict(
            True, "xyz:NVDA", "sell", legs, asset_class="crypto") is None

    def test_empty_book_allows(self):
        assert ccm.crypto_comove_verdict(
            True, "ETH", "sell", [], asset_class="crypto") is None

    def test_same_direction_allows(self):
        legs = [_leg("BTC", "buy", 23.0), _leg("SOL", "buy", 7.0)]
        assert ccm.crypto_comove_verdict(
            True, "ETH", "buy", legs, asset_class="crypto") is None

    def test_opposite_to_dominant_refuses(self):
        # Wound specimen: BTC+SOL long dominant → ETH short dies
        legs = [_leg("BTC", "buy", 23.0), _leg("SOL", "buy", 7.0),
                _leg("AVAX", "sell", 7.0)]
        assert ccm.crypto_comove_verdict(
            True, "ETH", "sell", legs, asset_class="crypto"
        ) == ccm.CRYPTO_COMOVE_OPPOSITE

    def test_alt_opposite_also_refuses(self):
        # Structural: not majors-only — LINK short vs BTC long dies too
        legs = [_leg("BTC", "buy", 7.0)]
        assert ccm.crypto_comove_verdict(
            True, "LINK", "sell", legs, asset_class="crypto"
        ) == ccm.CRYPTO_COMOVE_OPPOSITE

    def test_majority_tiebreak_when_net_zero(self):
        # net = 7+7−14 = 0 → majority (2 buy vs 1 sell) decides
        legs = [_leg("BTC", "buy", 7.0), _leg("SOL", "buy", 7.0),
                _leg("ETH", "sell", 14.0)]
        assert ccm.dominant_crypto_side(legs) == "buy"
        assert ccm.crypto_comove_verdict(
            True, "AVAX", "sell", legs
        ) == ccm.CRYPTO_COMOVE_OPPOSITE

    def test_conflicted_fail_closed(self):
        legs = [_leg("BTC", "buy", 7.0), _leg("ETH", "sell", 7.0)]
        assert ccm.crypto_comove_verdict(
            True, "SOL", "buy", legs
        ) == ccm.CRYPTO_COMOVE_CONFLICTED

    def test_min_legs_gate(self):
        legs = [_leg("BTC", "buy", 7.0)]
        assert ccm.crypto_comove_verdict(
            True, "ETH", "sell", legs, min_legs=2) is None
        assert ccm.crypto_comove_verdict(
            True, "ETH", "sell", legs, min_legs=1
        ) == ccm.CRYPTO_COMOVE_OPPOSITE

    def test_ignores_xyz_legs_in_book(self):
        legs = [_leg("xyz:GOLD", "buy", 7.0)]
        assert ccm.crypto_comove_verdict(
            True, "ETH", "sell", legs, asset_class="all") is None

    def test_detail_telemetry(self):
        legs = [_leg("BTC", "buy", 23.0), _leg("ETH", "sell", 7.0)]
        d = ccm.crypto_comove_detail(legs)
        assert d["n_crypto"] == 2
        assert d["n_buy"] == 1 and d["n_sell"] == 1
        assert d["net_signed_risk"] == pytest.approx(16.0)
        assert d["dominant"] == "buy"


# ── executor splice ──────────────────────────────────────────────────

class TestCryptoComoveExecutor:
    def test_refuses_eth_short_into_long_book(self, tmp_path):
        ex = _exec(tmp_path)
        _seed(ex, [_leg("BTC", "buy", 23.0), _leg("SOL", "buy", 7.0)])
        res = ex.place_bracket("ETH", "sell", 7.0, 3500.0, 3535.0,
                               coherence=9.5)
        assert not res.ok
        assert res.reason == ccm.CRYPTO_COMOVE_OPPOSITE

    def test_allows_same_dir_add(self, tmp_path):
        ex = _exec(tmp_path, family_cap_enabled=False)
        _seed(ex, [_leg("BTC", "buy", 7.0)])
        # BCH 2x margin: risk 5 on ~1.5% stop stays under 30% equity
        res = ex.place_bracket("BCH", "buy", 5.0, 260.0, 256.0,
                               coherence=5.0)
        assert res.ok, res.reason

    def test_kill_switch_off_places_opposite(self, tmp_path):
        ex = _exec(tmp_path, crypto_comove_enabled=False,
                   family_cap_enabled=False)
        _seed(ex, [_leg("BTC", "buy", 7.0)])
        res = ex.place_bracket("BCH", "sell", 5.0, 260.0, 264.0,
                               coherence=5.0)
        assert res.ok, res.reason

    def test_tradfi_sleeve_unaffected(self, tmp_path):
        # turbo2 path: asset_class=tradfi, even with crypto legs present
        ex = _exec(tmp_path, asset_class="tradfi",
                   family_cap_enabled=False)
        _seed(ex, [_leg("BTC", "buy", 7.0)])
        res = ex.place_bracket("BCH", "sell", 5.0, 260.0, 264.0,
                               coherence=5.0)
        assert res.ok, res.reason

    def test_config_default_on(self):
        cfg = PropConfig(enabled=True)
        assert cfg.crypto_comove_enabled is True
        assert cfg.crypto_comove_min_legs == 1
