"""Pin: formation router brain + ghost-150k floor (Governor 2026-11-14).

Formation tagging is telemetry-only precedence (docs/SLEEVE_FORMATION_
20261114.md §2); the ghost floor makes the demo sleeve place real $10k+
fills at the proven edge points so the journal measures avg return at
large capital BEFORE the account purchase. Kill switches reproduce the
pre-module system bit-for-bit.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from prop_harness.brains import formation as fm
from prop_harness.brains.budget import BudgetBrain
from prop_harness.brains.drawdown import DrawdownBrain
from prop_harness.config import PropConfig
from prop_harness.executor import ProprExecutor


def _ts(hour_utc: int) -> float:
    # 2026-11-14 is a fixed reference day; hour pinned per test.
    import calendar
    return float(calendar.timegm(
        time.struct_time((2026, 11, 14, hour_utc, 0, 0, 0, 0, 0, 0))))


class TestFormationVerdict:
    def test_cavalry_folds_into_giant_until_armed(self):
        # rule 1: coh>=7.5 macro -> CAVALRY only when armed; else GIANT
        tag = fm.formation_verdict("BTC", "buy", 7.8, _ts(15))
        assert tag["unit"] == fm.UNIT_GIANT
        assert tag["edge_point"] == "cavalry_macro"
        assert tag["macro"] is True
        assert tag["sniper_candidate"] is True
        armed = fm.formation_verdict("BTC", "buy", 7.8, _ts(15),
                                     cavalry_armed=True)
        assert armed["unit"] == fm.UNIT_CAVALRY
        assert armed["edge_point"] == "cavalry_macro"

    def test_macro_set_membership(self):
        for macro in ("BTC", "ETH", "xyz:GOLD", "xyz:SP500", "xyz:XYZ100"):
            tag = fm.formation_verdict(macro, "buy", 8.0, _ts(12),
                                       cavalry_armed=True)
            assert tag["unit"] == fm.UNIT_CAVALRY, macro
        # SOL is a Propr-line flow even at grand coherence
        tag = fm.formation_verdict("SOL", "buy", 8.0, _ts(12),
                                   cavalry_armed=True)
        assert tag["unit"] == fm.UNIT_GIANT

    def test_golden_window_longs(self):
        for hr in (23, 0, 1, 2):
            tag = fm.formation_verdict("SOL", "buy", 6.2, _ts(hr))
            assert tag["unit"] == fm.UNIT_GIANT, hr
            assert tag["edge_point"] == "golden_long", hr
            assert tag["golden_window"] is True
        # 22:59 UTC is NOT the window
        tag = fm.formation_verdict("SOL", "buy", 6.2, _ts(22))
        assert tag["edge_point"] == "giant_long"
        assert tag["golden_window"] is False
        # shorts never take the golden-long edge
        tag = fm.formation_verdict("SOL", "sell", 6.2, _ts(0))
        assert tag["unit"] == fm.UNIT_ARCHER

    def test_giant_long_off_golden(self):
        tag = fm.formation_verdict("TAO", "buy", 6.1, _ts(15))
        assert tag["unit"] == fm.UNIT_GIANT
        assert tag["edge_point"] == "giant_long"
        assert tag["sniper_candidate"] is False  # coh < 6.5

    def test_archer_shorts_and_probes(self):
        tag = fm.formation_verdict("xyz:MU", "sell", 7.0, _ts(15))
        assert tag["unit"] == fm.UNIT_ARCHER
        assert tag["edge_point"] == "archer_short"
        tag = fm.formation_verdict("TAO", "buy", 5.8, _ts(15))
        assert tag["unit"] == fm.UNIT_ARCHER
        assert tag["edge_point"] == "archer_probe"
        # 5.7 floor inclusive
        tag = fm.formation_verdict("TAO", "buy", 5.7, _ts(15))
        assert tag["unit"] == fm.UNIT_ARCHER
        # 6.0 boundary is GIANT, not archer
        tag = fm.formation_verdict("TAO", "buy", 6.0, _ts(15))
        assert tag["unit"] == fm.UNIT_GIANT

    def test_park_below_archer_floor(self):
        tag = fm.formation_verdict("TAO", "buy", 5.5, _ts(15))
        assert tag["unit"] == fm.UNIT_PARK
        assert tag["edge_point"] == "formation_park"

    def test_sniper_candidate_is_tag_only(self):
        # coh 6.5+ long carries the flag; unit never changes for it
        tag = fm.formation_verdict("SOL", "buy", 6.5, _ts(15))
        assert tag["sniper_candidate"] is True
        assert tag["unit"] == fm.UNIT_GIANT

    def test_unclassifiable_returns_none(self):
        assert fm.formation_verdict("BTC", "hold", 7.0, _ts(15)) is None
        assert fm.formation_verdict("BTC", "buy", None, _ts(15)) is None
        assert fm.formation_verdict("BTC", "buy", "nan", _ts(15)) is None
        # missing ts: hour unknown -> golden=False, still classifiable
        tag = fm.formation_verdict("SOL", "buy", 6.2, None)
        assert tag["unit"] == fm.UNIT_GIANT
        assert tag["golden_window"] is False


class TestDraftOrder:
    def test_sort_key_ordering(self):
        c = {"unit": fm.UNIT_CAVALRY}
        g = {"unit": fm.UNIT_GIANT}
        a = {"unit": fm.UNIT_ARCHER}
        p = {"unit": fm.UNIT_PARK}
        assert (fm.draft_sort_key(c) < fm.draft_sort_key(g)
                < fm.draft_sort_key(a) < fm.draft_sort_key(p))
        # untagged sorts after park
        assert fm.draft_sort_key(None) > fm.draft_sort_key(p)
        assert fm.draft_sort_key({"unit": "unknown"}) == fm.DRAFT_ORDER[
            fm.UNIT_PARK]


class TestArcherSlotVerdict:
    def test_standdown_only_on_reserve(self):
        archer = {"unit": fm.UNIT_ARCHER}
        # open 9 / max 10 / reserve 1 -> only reserve left -> stand down
        assert fm.archer_slot_verdict(archer, 9, 10, 1) == (
            fm.ARCHER_SLOT_REASON)
        # open 8 -> a non-reserve slot exists -> free to fire
        assert fm.archer_slot_verdict(archer, 8, 10, 1) is None
        # giants never stand down
        assert fm.archer_slot_verdict(
            {"unit": fm.UNIT_GIANT}, 9, 10, 1) is None
        # untagged never stands down
        assert fm.archer_slot_verdict(None, 9, 10, 1) is None

    def test_bad_inputs_abstain(self):
        archer = {"unit": fm.UNIT_ARCHER}
        assert fm.archer_slot_verdict(archer, "x", 10, 1) is None
        assert fm.archer_slot_verdict(archer, 9, 0, 1) is None
        assert fm.archer_slot_verdict(archer, 9, 10, 0) is None


class TestWrappers:
    def test_signal_wrapper_uses_ts_ms(self):
        sig = {"asset": "BTC", "side": "buy", "coherence": 7.8,
               "ts_ms": _ts(0) * 1000.0}
        tag = fm.formation_tag_for_signal(sig)
        assert tag["unit"] == fm.UNIT_GIANT
        assert tag["golden_window"] is True
        assert fm.formation_tag_for_signal(sig, enabled=False) is None
        assert fm.formation_tag_for_signal(None) is None

    def test_bracket_wrapper_uses_opened_ts(self):
        br = {"asset": "xyz:MU", "side": "sell", "coherence_at_entry": 7.0,
              "opened_ts": _ts(15)}
        tag = fm.formation_tag_for_bracket(br)
        assert tag["unit"] == fm.UNIT_ARCHER
        assert fm.formation_tag_for_bracket(None) is None


class _OkClient:
    account_id = "acct1"

    def get_open_orders(self, base=None):
        return []

    def place_orders(self, body):
        return [{"orderId": "o1"}]

    def cancel_order(self, order_id):
        return {}


def _exec(tmp_path: Path, ghost_floor: float, trend_budget: float = 5_000.0
          ) -> ProprExecutor:
    cfg = PropConfig(
        enabled=True, state_root=tmp_path, profile="evaluation",
        asset_class="crypto", crypto_comove_enabled=False,
        family_cap_enabled=False, post_win_respend_enabled=False,
        cost_move_gate_enabled=False, max_positions=7,
        slot_reserve_enabled=False, eval_crypto_min_risk_enabled=False,
        crypto_min_coh_fire_enabled=False,
        ghost_min_notional_usd=ghost_floor,
        eval_margin_cap_frac=0.99,
        eval_trend_budget_usd=trend_budget,
    )
    (tmp_path / "journal").mkdir(exist_ok=True)
    dd = DrawdownBrain(tmp_path / "dd.json", floor_line=1000.0,
                       floor_usd=150.0)
    dd.update(5000.0)
    budget = BudgetBrain(tmp_path / "budget.json", hard_zero=30.0,
                         divisor=10.0, floor_budget=40.0)
    budget.roll(10_000.0)
    return ProprExecutor(cfg, _OkClient(), budget, dd)


class TestGhostMinNotionalFloor:
    def test_ghost_bump_places_large_fill(self, tmp_path):
        # demo mirroring a 150k account: $10k minimum notional per entry
        ex = _exec(tmp_path, 10_000.0)
        # risk $14, stop dist 8% -> natural qty 1.75 = $175 notional
        res = ex.place_bracket("BTC", "buy", 14.0, 100.0, 92.0)
        assert res.ok is True
        br = ex.open_brackets()[0]
        assert float(br["quantity"]) * 100.0 == pytest.approx(10_000.0)
        # honest risk: 100 qty x $8 stop = $800 committed
        assert br["risk_usd"] == pytest.approx(800.0)
        assert ex.budget.state.committed == pytest.approx(800.0)
        journal = (tmp_path / "journal" / "events.jsonl").read_text()
        assert "ghost_min_notional_bump" in journal

    def test_above_floor_untouched(self, tmp_path):
        ex = _exec(tmp_path, 10_000.0)
        # stop dist 1 -> qty 150 = $15k notional > floor -> no bump
        res = ex.place_bracket("BTC", "buy", 150.0, 100.0, 99.0)
        assert res.ok is True
        assert ex.budget.state.committed == pytest.approx(150.0)
        journal = (tmp_path / "journal" / "events.jsonl").read_text()
        assert "ghost_min_notional_bump" not in journal

    def test_ghost_bump_rechecks_cap(self, tmp_path):
        # risk ceiling $100 (trend budget): initial $14 risk passes; the
        # ghost bump lifts qty to 100 x $2 stop = $200 risk -> honest
        # re-check refuses before any commit
        ex = _exec(tmp_path, 10_000.0, trend_budget=100.0)
        res = ex.place_bracket("BTC", "buy", 14.0, 100.0, 98.0)
        assert res.ok is False
        assert "ghost-bumped risk" in res.reason
        assert ex.budget.state.committed == pytest.approx(0.0)
        journal = (tmp_path / "journal" / "events.jsonl").read_text()
        assert "ghost_min_notional_bump" not in journal

    def test_zero_floor_is_legacy(self, tmp_path):
        ex = _exec(tmp_path, 0.0)
        res = ex.place_bracket("BTC", "buy", 14.0, 100.0, 92.0)
        assert res.ok is True
        assert ex.budget.state.committed == pytest.approx(14.0)
        journal = (tmp_path / "journal" / "events.jsonl").read_text()
        assert "ghost_min_notional_bump" not in journal

    def test_composes_with_tradfi_floor(self, tmp_path):
        # tradfi floor $400 + ghost floor $10k on a tradfi sleeve:
        # the tradfi bump runs first (qty -> $400), then the ghost floor
        # (all-asset) lifts to $10k; one honest commit at ghost risk.
        cfg = PropConfig(
            enabled=True, state_root=tmp_path, profile="evaluation",
            asset_class="tradfi", crypto_comove_enabled=False,
            family_cap_enabled=False, post_win_respend_enabled=False,
            cost_move_gate_enabled=False, max_positions=7,
            slot_reserve_enabled=False, eval_crypto_min_risk_enabled=False,
            crypto_min_coh_fire_enabled=False,
            eval_tradfi_min_notional_usd=400.0,
            ghost_min_notional_usd=10_000.0,
            eval_margin_cap_frac=0.99,
            eval_trend_budget_usd=5_000.0,
        )
        (tmp_path / "journal").mkdir(exist_ok=True)
        dd = DrawdownBrain(tmp_path / "dd.json", floor_line=1000.0,
                           floor_usd=150.0)
        dd.update(5000.0)
        budget = BudgetBrain(tmp_path / "budget.json", hard_zero=30.0,
                             divisor=10.0, floor_budget=40.0)
        budget.roll(10_000.0)
        ex = ProprExecutor(cfg, _OkClient(), budget, dd)
        res = ex.place_bracket("xyz:MU", "buy", 14.0, 100.0, 92.0)
        assert res.ok is True
        br = ex.open_brackets()[0]
        assert float(br["quantity"]) * 100.0 == pytest.approx(10_000.0)
        assert br["risk_usd"] == pytest.approx(800.0)
        journal = (tmp_path / "journal" / "events.jsonl").read_text()
        assert "ghost_min_notional_bump" in journal


class TestFormationRegistryCarry:
    def test_tagged_bracket_carries_formation_fields(self, tmp_path):
        ex = _exec(tmp_path, 0.0)
        tag = fm.formation_verdict("BTC", "buy", 7.8, _ts(15))
        res = ex.place_bracket("BTC", "buy", 14.0, 100.0, 92.0,
                               formation=tag)
        assert res.ok is True
        br = ex.open_brackets()[0]
        assert br["formation_unit"] == fm.UNIT_GIANT
        assert br["edge_point"] == "cavalry_macro"
        journal = (tmp_path / "journal" / "events.jsonl").read_text()
        assert "cavalry_macro" in journal

    def test_untagged_is_bit_for_bit(self, tmp_path):
        ex = _exec(tmp_path, 0.0)
        res = ex.place_bracket("BTC", "buy", 14.0, 100.0, 92.0)
        assert res.ok is True
        br = ex.open_brackets()[0]
        assert "formation_unit" not in br
        assert "edge_point" not in br
        journal = (tmp_path / "journal" / "events.jsonl").read_text()
        assert "formation_unit" not in journal
